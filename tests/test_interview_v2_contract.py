import json
import re
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
