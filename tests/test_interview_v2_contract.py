import json
import re
import unittest
from pathlib import Path

from pydantic import ValidationError

from app.schemas.interview_v2_structure import (
    InterviewV2ReviewIssueBatchRequest,
    InterviewV2ReviewIssuePatchRequest,
    InterviewV2StructureBuildRequest,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "interview_v2"
RANGE_RE = re.compile(r"^[A-Z]+[1-9][0-9]*:[A-Z]+[1-9][0-9]*$")
COLUMN_RANGE_RE = re.compile(r"^[A-Z]+:[A-Z]+$")
FORBIDDEN_RAW_KEYS = {
    "raw_text",
    "raw_value",
    "player_quote",
    "participant_name",
    "interview_content",
}


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _all_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _all_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_keys(child)


class InterviewV2ContractTests(unittest.TestCase):
    def test_fixtures_are_structural_and_contain_no_raw_interview_text(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            with self.subTest(path=path.name):
                fixture = _load_fixture(path.name)
                self.assertFalse(fixture["source"]["contains_raw_interview_text"])
                self.assertEqual(fixture["source"]["validation_mode"], "read_only")
                self.assertTrue(FORBIDDEN_RAW_KEYS.isdisjoint(set(_all_keys(fixture))))
                self.assertRegex(fixture["source"]["sha256"], r"^[0-9a-f]{64}$")

    def test_physical_sheet_counts_and_ranges_are_self_consistent(self):
        for path in sorted(FIXTURE_DIR.glob("*.json")):
            with self.subTest(path=path.name):
                fixture = _load_fixture(path.name)
                workbook = fixture["workbook"]
                self.assertEqual(workbook["sheet_count"], len(workbook["sheets"]))
                self.assertEqual(workbook["formula_count"], 0)
                self.assertEqual(
                    len({sheet["sheet_id"] for sheet in workbook["sheets"]}),
                    workbook["sheet_count"],
                )
                for sheet in workbook["sheets"]:
                    self.assertRegex(sheet["used_range"], RANGE_RE)
                    self.assertRegex(sheet["structure_columns"], COLUMN_RANGE_RE)
                    self.assertRegex(
                        sheet["candidate_participant_columns"], COLUMN_RANGE_RE
                    )
                    self.assertGreater(sheet["candidate_participant_count"], 0)
                    self.assertGreaterEqual(sheet["column_count"], 4)
                    self.assertGreater(sheet["row_count"], 1)

    def test_system1_requires_manual_group_and_participant_confirmation(self):
        fixture = _load_fixture("system1_structure.json")
        decisions = fixture["decisions"]
        self.assertEqual(decisions["group_mapping"], "research_confirmation_required")
        self.assertEqual(
            decisions["participant_bindings"], "research_confirmation_required"
        )
        self.assertFalse(decisions["automatic_column_position_binding_allowed"])
        self.assertEqual(
            [
                sheet["candidate_participant_columns"]
                for sheet in fixture["workbook"]["sheets"]
            ],
            ["D:S", "H:T"],
        )

    def test_system3_preserves_spacer_and_participant_boundaries(self):
        fixture = _load_fixture("system3_structure.json")
        sheet = fixture["workbook"]["sheets"][0]
        self.assertEqual(sheet["structure_columns"], "A:C")
        self.assertEqual(sheet["spacer_columns"], "D:D")
        self.assertEqual(sheet["candidate_participant_columns"], "E:O")
        self.assertEqual(sheet["candidate_participant_count"], 11)

    def test_same_player_label_in_different_groups_produces_distinct_ids(self):
        def participant_id(group_key: str, participant_key: str) -> str:
            return f"{group_key}-{participant_key}"

        self.assertNotEqual(
            participant_id("group_01", "P01"),
            participant_id("group_02", "P01"),
        )

    def test_multiple_recorder_evidence_counts_one_internal_participant(self):
        evidence = [
            {"participant_id": "group_01-P01", "source": "recorder_a"},
            {"participant_id": "group_01-P01", "source": "recorder_b"},
            {"participant_id": "group_01-P02", "source": "recorder_a"},
        ]
        self.assertEqual(
            len({entry["participant_id"] for entry in evidence}),
            2,
        )

    def test_ratio_denominator_requires_all_three_confirmed_dimensions(self):
        coverages = [
            {
                "participant_id": "group_01-P01",
                "source_presence": "present",
                "asked_status": "asked",
                "applicability": "applicable",
            },
            {
                "participant_id": "group_01-P02",
                "source_presence": "present",
                "asked_status": "unknown",
                "applicability": "applicable",
            },
            {
                "participant_id": "group_01-P03",
                "source_presence": "not_present",
                "asked_status": "unknown",
                "applicability": "unknown",
            },
        ]
        denominator = {
            item["participant_id"]
            for item in coverages
            if item["source_presence"] == "present"
            and item["asked_status"] == "asked"
            and item["applicability"] == "applicable"
        }
        self.assertEqual(denominator, {"group_01-P01"})

    def test_structure_build_is_bound_to_an_exact_confirmed_mapping_head(self):
        request = InterviewV2StructureBuildRequest.model_validate(
            {
                "base_mapping_revision_id": "mapping_" + "1" * 32,
                "base_mapping_sha256": "a" * 64,
            }
        )
        self.assertTrue(request.base_mapping_revision_id.startswith("mapping_"))
        with self.assertRaises(ValidationError):
            InterviewV2StructureBuildRequest.model_validate(
                {
                    "base_mapping_revision_id": "mapping_" + "1" * 32,
                    "base_mapping_sha256": "a" * 64,
                    "force_rebuild": True,
                }
            )

    def test_review_resolution_requires_both_structure_and_evidence_heads(self):
        valid = {
            "base_structure_revision_id": "structure_" + "2" * 32,
            "base_evidence_revision_id": "evidence_" + "3" * 32,
            "resolution": "assign_row_role",
            "row_role": "follow_up",
            "comment": "确认现场追问",
        }
        InterviewV2ReviewIssuePatchRequest.model_validate(valid)
        for missing in (
            "base_structure_revision_id",
            "base_evidence_revision_id",
        ):
            malformed = dict(valid)
            malformed.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ValidationError):
                InterviewV2ReviewIssuePatchRequest.model_validate(malformed)

    def test_batch_review_contract_rejects_duplicate_issue_ids(self):
        resolution = {
            "issue_id": "issue_" + "4" * 32,
            "resolution": "accept_suggestion",
            "comment": "接受确定性建议",
        }
        with self.assertRaises(ValidationError):
            InterviewV2ReviewIssueBatchRequest.model_validate(
                {
                    "base_structure_revision_id": "structure_" + "2" * 32,
                    "base_evidence_revision_id": "evidence_" + "3" * 32,
                    "resolutions": [resolution, resolution],
                }
            )


if __name__ == "__main__":
    unittest.main()
