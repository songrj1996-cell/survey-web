from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from pydantic import ValidationError

import app.core.interview_v2_evidence as evidence_core
from app.core.interview_v2_evidence import (
    InterviewV2StructureError,
    apply_review_resolutions,
    build_structure_and_evidence,
)
from app.schemas.interview_v2_structure import (
    InterviewV2EvidenceContextResponse,
    InterviewV2ReviewIssuePatchRequest,
    InterviewV2StructureBuildRequest,
    InterviewV2StructureBuildResponse,
)
from tests.test_interview_v2_structure import (
    IMPORT_ID,
    MAPPING_ID,
    PROJECT_ID,
    SNAPSHOT_SHA,
    WORKBOOK_ID,
    _sheet,
    fixture_bundle,
)


def build_fixture_checkpoint():
    snapshot, mapping, mapping_sha = fixture_bundle()
    result = build_structure_and_evidence(
        snapshot,
        mapping,
        project_id=PROJECT_ID,
        import_id=IMPORT_ID,
        workbook_revision_id=WORKBOOK_ID,
        mapping_revision_id=MAPPING_ID,
        mapping_sha256=mapping_sha,
    )
    return snapshot, mapping, mapping_sha, result


def single_record_bundle(*, prompt="如何组队？", answer="自己邀请好友"):
    snapshot = {
        "snapshot_sha256": SNAPSHOT_SHA,
        "sheets": [
            _sheet(
                "sheet_001",
                0,
                "记录",
                {
                    2: {1: "组队", 2: "模块"},
                    3: {2: "主问题", 3: prompt, 4: answer},
                },
                participant_columns=(4,),
            )
        ],
    }
    mapping = {
        "mapping_schema_version": "interview-group-mapping/1.0",
        "base_snapshot_sha256": SNAPSHOT_SHA,
        "project_id": PROJECT_ID,
        "import_id": IMPORT_ID,
        "workbook_revision_id": WORKBOOK_ID,
        "groups": [
            {
                "group_id": "group_" + "5" * 32,
                "display_name": "第1组",
                "decision_status": "confirmed",
                "sheets": [
                    {
                        "sheet_id": "sheet_001",
                        "index": 0,
                        "role": "record",
                        "recorder_label": "记录员1",
                        "decision_status": "confirmed",
                    }
                ],
                "participants": [
                    {
                        "participant_id": "participant_" + "6" * 32,
                        "participant_label": "P01",
                        "decision_status": "confirmed",
                        "columns": [
                            {"sheet_id": "sheet_001", "column_index": 4}
                        ],
                    }
                ],
            }
        ],
        "ignored_sheet_ids": [],
    }
    mapping_sha = hashlib.sha256(
        json.dumps(
            mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return snapshot, mapping, mapping_sha


def build_single_record_checkpoint(*, prompt="如何组队？", answer="自己邀请好友"):
    snapshot, mapping, mapping_sha = single_record_bundle(
        prompt=prompt, answer=answer
    )
    result = build_structure_and_evidence(
        snapshot,
        mapping,
        project_id=PROJECT_ID,
        import_id=IMPORT_ID,
        workbook_revision_id=WORKBOOK_ID,
        mapping_revision_id=MAPPING_ID,
        mapping_sha256=mapping_sha,
    )
    return snapshot, mapping, mapping_sha, result


def two_participant_bundle(*, answer_one="玩家一回答", answer_two="玩家二回答"):
    snapshot, mapping, _mapping_sha = single_record_bundle(answer=answer_one)
    sheet = snapshot["sheets"][0]
    sheet["dimensions"]["content_max_column"] = 5
    sheet["candidate_participant_region"]["end_column"] = 5
    sheet["cells"].append(
        {
            "address": "E1",
            "row": 1,
            "column": 5,
            "raw_value": "P02",
            "display_value": "P02",
            "normalized_text": "P02",
            "value_sha256": hashlib.sha256(b"P02").hexdigest(),
        }
    )
    if answer_two:
        sheet["cells"].append(
            {
                "address": "E3",
                "row": 3,
                "column": 5,
                "raw_value": answer_two,
                "display_value": answer_two,
                "normalized_text": answer_two,
                "value_sha256": hashlib.sha256(answer_two.encode("utf-8")).hexdigest(),
            }
        )
    group = mapping["groups"][0]
    group["participants"].append(
        {
            "participant_id": "participant_" + "7" * 32,
            "participant_label": "P02",
            "decision_status": "confirmed",
            "columns": [{"sheet_id": "sheet_001", "column_index": 5}],
        }
    )
    mapping_sha = hashlib.sha256(
        json.dumps(
            mapping,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return snapshot, mapping, mapping_sha


class InterviewV2EvidenceTests(unittest.TestCase):
    def test_empty_confirmed_participant_columns_block_dossier_readiness(self):
        snapshot, mapping, _mapping_sha = fixture_bundle()
        snapshot["sheets"][1]["cells"] = [
            cell
            for cell in snapshot["sheets"][1]["cells"]
            if cell["column"] not in {4, 5}
        ]
        group = mapping["groups"][0]
        group["sheets"] = [group["sheets"][1]]
        for participant in group["participants"]:
            participant["columns"] = [participant["columns"][1]]
        mapping["ignored_sheet_ids"] = ["sheet_001", "sheet_003", "sheet_004"]
        mapping_sha = hashlib.sha256(
            json.dumps(
                mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(result["evidence"]["entries"], [])
        self.assertEqual(result["status"], "STRUCTURE_REVIEW_REQUIRED")
        issue = next(
            item
            for item in result["review_issues"]
            if item["code"] == "PARTICIPANT_EVIDENCE_MISSING"
        )
        self.assertEqual(issue["severity"], "blocking")
        self.assertEqual(issue["allowed_resolutions"], [])
        self.assertEqual(
            issue["suggested_action"], "add_participant_evidence_and_reupload"
        )

    def test_each_confirmed_participant_without_evidence_gets_own_issue(self):
        snapshot, mapping, mapping_sha = two_participant_bundle(answer_two="")
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(
            result["evidence"]["expected_participants"],
            [
                {
                    "participant_id": "participant_" + "6" * 32,
                    "group_id": "group_" + "5" * 32,
                },
                {
                    "participant_id": "participant_" + "7" * 32,
                    "group_id": "group_" + "5" * 32,
                },
            ],
        )
        self.assertEqual(
            {entry["participant_id"] for entry in result["evidence"]["entries"]},
            {"participant_" + "6" * 32},
        )
        missing = [
            issue
            for issue in result["review_issues"]
            if issue["code"] == "PARTICIPANT_EVIDENCE_MISSING"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(
            missing[0]["affected_ids"],
            {
                "group_ids": ["group_" + "5" * 32],
                "participant_ids": ["participant_" + "7" * 32],
            },
        )
        self.assertEqual(missing[0]["allowed_resolutions"], [])
        self.assertEqual(result["status"], "STRUCTURE_REVIEW_REQUIRED")

    def test_multiple_participants_with_safe_evidence_are_not_blocked(self):
        snapshot, mapping, mapping_sha = two_participant_bundle()
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(result["status"], "READY_FOR_DOSSIERS")
        self.assertFalse(
            any(
                issue["code"] == "PARTICIPANT_EVIDENCE_MISSING"
                for issue in result["review_issues"]
            )
        )

    def test_formula_without_cache_is_blocking_and_not_reportable(self):
        snapshot, mapping, mapping_sha = single_record_bundle()
        target = next(
            cell
            for cell in snapshot["sheets"][0]["cells"]
            if cell["address"] == "D3"
        )
        target.update(
            {
                "raw_value": "=Other!A1",
                "display_value": None,
                "normalized_text": "=Other!A1",
                "formula_cache_status": "unavailable",
            }
        )
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(result["status"], "STRUCTURE_REVIEW_REQUIRED")
        self.assertEqual(len(result["evidence"]["entries"]), 1)
        entry = result["evidence"]["entries"][0]
        self.assertEqual(entry["normalized_content"], "=Other!A1")
        self.assertEqual(entry["formula_cache_status"], "unavailable")
        self.assertEqual(entry["identity_decision_status"], "needs_review")
        issue = next(
            item
            for item in result["review_issues"]
            if item["code"] == "EVIDENCE_FORMULA_CACHE_UNAVAILABLE"
        )
        self.assertEqual(issue["allowed_resolutions"], [])
        self.assertEqual(
            issue["suggested_action"], "open_and_save_in_excel_then_reupload"
        )
        with self.assertRaises(InterviewV2StructureError):
            apply_review_resolutions(
                result["structure"],
                result["evidence"],
                result["review_issues"],
                [
                    {
                        "issue_id": issue["issue_id"],
                        "resolution": "exclude_evidence",
                        "target_id": entry["evidence_id"],
                        "comment": "唯一证据不能直接排空",
                    }
                ],
                actor="researcher@example.com",
                resolved_at="2026-08-14T10:00:00+08:00",
                operation_fingerprint="f" * 64,
            )

    def test_formula_without_cache_can_be_excluded_when_other_evidence_remains(self):
        snapshot, mapping, mapping_sha = fixture_bundle()
        target = next(
            cell
            for cell in snapshot["sheets"][0]["cells"]
            if cell["address"] == "D3"
        )
        target.update(
            {
                "raw_value": "=Other!A1",
                "display_value": None,
                "normalized_text": "=Other!A1",
                "formula_cache_status": "unavailable",
            }
        )
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertGreater(len(result["evidence"]["entries"]), 1)
        issue = next(
            item
            for item in result["review_issues"]
            if item["code"] == "EVIDENCE_FORMULA_CACHE_UNAVAILABLE"
        )
        self.assertEqual(issue["allowed_resolutions"], ["exclude_evidence"])

    def test_formula_with_cache_uses_display_value_and_remains_reportable(self):
        snapshot, mapping, mapping_sha = single_record_bundle()
        target = next(
            cell
            for cell in snapshot["sheets"][0]["cells"]
            if cell["address"] == "D3"
        )
        target.update(
            {
                "raw_value": "=1+1",
                "display_value": "2",
                "normalized_text": "=1+1",
                "formula_cache_status": "available",
            }
        )
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(result["status"], "READY_FOR_DOSSIERS")
        entry = result["evidence"]["entries"][0]
        self.assertEqual(entry["raw_content"], "=1+1")
        self.assertEqual(entry["display_content"], "2")
        self.assertEqual(entry["normalized_content"], "2")
        self.assertEqual(entry["formula_cache_status"], "available")
        self.assertEqual(entry["identity_decision_status"], "system_verified")
        self.assertFalse(
            any(
                item["code"] == "EVIDENCE_FORMULA_CACHE_UNAVAILABLE"
                for item in result["review_issues"]
            )
        )

    def test_formula_with_empty_available_cache_is_not_evidence(self):
        snapshot, mapping, mapping_sha = single_record_bundle()
        target = next(
            cell
            for cell in snapshot["sheets"][0]["cells"]
            if cell["address"] == "D3"
        )
        target.update(
            {
                "raw_value": '=" "',
                "display_value": " ",
                "normalized_text": '=" "',
                "formula_cache_status": "available",
            }
        )
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(result["evidence"]["entries"], [])
        self.assertEqual(result["status"], "STRUCTURE_REVIEW_REQUIRED")
        self.assertEqual(
            [
                issue["affected_ids"]["participant_ids"]
                for issue in result["review_issues"]
                if issue["code"] == "PARTICIPANT_EVIDENCE_MISSING"
            ],
            [["participant_" + "6" * 32]],
        )
        self.assertNotIn('=" "', repr(result["evidence"]))

    def test_empty_formula_cache_blocks_only_its_participant(self):
        snapshot, mapping, mapping_sha = two_participant_bundle()
        target = next(
            cell
            for cell in snapshot["sheets"][0]["cells"]
            if cell["address"] == "D3"
        )
        target.update(
            {
                "raw_value": '=" "',
                "display_value": " ",
                "normalized_text": '=" "',
                "formula_cache_status": "available",
            }
        )
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )

        self.assertEqual(
            {entry["participant_id"] for entry in result["evidence"]["entries"]},
            {"participant_" + "7" * 32},
        )
        missing = next(
            issue
            for issue in result["review_issues"]
            if issue["code"] == "PARTICIPANT_EVIDENCE_MISSING"
        )
        self.assertEqual(
            missing["affected_ids"]["participant_ids"],
            ["participant_" + "6" * 32],
        )
        self.assertNotIn('=" "', repr(result["evidence"]))

    def test_missing_or_punctuation_only_main_question_requires_reupload(self):
        for prompt, expected_code in (
            ("", "MAIN_QUESTION_TEXT_MISSING"),
            ("!!!", "MAIN_QUESTION_TEXT_INVALID"),
        ):
            with self.subTest(prompt=prompt):
                _snapshot, _mapping, _sha, result = build_single_record_checkpoint(
                    prompt=prompt
                )
                issue = next(
                    item
                    for item in result["review_issues"]
                    if item["code"] == expected_code
                )
                self.assertEqual(result["status"], "STRUCTURE_REVIEW_REQUIRED")
                self.assertEqual(issue["allowed_resolutions"], [])
                self.assertEqual(
                    issue["suggested_action"], "fix_question_text_and_reupload"
                )
                self.assertFalse(
                    any(
                        not item["normalized_key"]
                        for item in result["structure"]["main_questions"]
                    )
                )

    def test_evidence_preserves_identity_and_complete_cell_provenance(self):
        _snapshot, _mapping, _sha, result = build_fixture_checkpoint()
        entries = result["evidence"]["entries"]
        main = next(
            item
            for item in entries
            if item["sheet_id"] == "sheet_001" and item["row"] == 3
        )
        self.assertEqual(main["evidence_type"], "participant_self_report")
        self.assertEqual(main["capture_context"], "main_answer")
        self.assertEqual(main["identity_decision_status"], "system_verified")
        self.assertRegex(main["evidence_id"], r"^ev_[0-9a-f]{32}$")
        self.assertRegex(main["source_cell_id"], r"^cell_[0-9a-f]{32}$")
        self.assertEqual(main["cell_address"], "D3")
        self.assertEqual(main["participant_id"], "participant_" + "6" * 32)
        self.assertEqual(main["group_id"], "group_" + "5" * 32)
        self.assertEqual(main["recorder_label"], "记录员1")
        self.assertRegex(main["source_value_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(main["fragment_text_field"], "normalized_content")
        self.assertEqual(main["fragment_start"], 0)
        self.assertEqual(main["fragment_end"], len(main["normalized_content"]))

        observation = next(
            item
            for item in entries
            if item["sheet_id"] == "sheet_001" and item["row"] == 5
        )
        self.assertEqual(observation["evidence_type"], "researcher_observation")
        self.assertEqual(observation["capture_context"], "observation")

        unknown = next(
            item
            for item in entries
            if item["sheet_id"] == "sheet_001" and item["row"] == 8
        )
        self.assertIsNone(unknown["evidence_type"])
        self.assertEqual(unknown["identity_decision_status"], "needs_review")
        self.assertEqual(result["status"], "STRUCTURE_REVIEW_REQUIRED")
        self.assertGreater(result["blocking_issue_count"], 0)
        self.assertFalse(any(item["row"] == 6 for item in entries))
        self.assertFalse(any(item["sheet_id"] == "sheet_003" for item in entries))
        self.assertFalse(any(item["sheet_id"] == "sheet_004" for item in entries))

        p01_recorders = {
            item["recorder_label"]
            for item in entries
            if item["participant_id"] == "participant_" + "6" * 32
        }
        self.assertEqual(p01_recorders, {"记录员1", "记录员2"})
        repeated = build_structure_and_evidence(
            _snapshot,
            _mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=_sha,
        )
        self.assertEqual(
            [item["evidence_id"] for item in entries],
            [item["evidence_id"] for item in repeated["evidence"]["entries"]],
        )

    def test_raw_display_and_normalized_content_are_distinct(self):
        snapshot, mapping, mapping_sha = fixture_bundle()
        target = next(
            cell
            for cell in snapshot["sheets"][0]["cells"]
            if cell["address"] == "D3"
        )
        target["raw_value"] = "  原始值  "
        target["display_value"] = "显示值"
        target["normalized_text"] = "显示值"
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )
        entry = next(
            item
            for item in result["evidence"]["entries"]
            if item["cell_address"] == "D3"
        )
        self.assertEqual(entry["raw_content"], "  原始值  ")
        self.assertEqual(entry["display_content"], "显示值")
        self.assertEqual(entry["normalized_content"], "显示值")
        self.assertEqual(entry["fragment_start"], 0)
        self.assertEqual(entry["fragment_end"], len("显示值"))

    def test_row_role_resolution_keeps_evidence_id_and_audit_has_no_raw_text(self):
        _snapshot, _mapping, _sha, built = build_fixture_checkpoint()
        issue = next(
            item
            for item in built["review_issues"]
            if item["code"] == "ROW_ROLE_UNKNOWN"
            and item["source_context"]["row"] == 8
        )
        before_entry = next(
            item
            for item in built["evidence"]["entries"]
            if item["row"] == 8
        )
        resolution = {
            "issue_id": issue["issue_id"],
            "resolution": "assign_row_role",
            "row_role": "follow_up",
            "comment": "确认是追问补充",
        }
        first = apply_review_resolutions(
            built["structure"],
            built["evidence"],
            built["review_issues"],
            [resolution],
            actor="researcher@example.com",
            resolved_at="2026-08-14T10:00:00+08:00",
            operation_fingerprint="1" * 64,
        )
        after_entry = next(
            item
            for item in first["evidence"]["entries"]
            if item["row"] == 8
        )
        self.assertEqual(before_entry["evidence_id"], after_entry["evidence_id"])
        self.assertEqual(after_entry["evidence_type"], "participant_self_report")
        self.assertEqual(after_entry["identity_decision_status"], "human_confirmed")
        self.assertNotIn("待确认内容", repr(first["manual_overrides"]))
        self.assertIn("created_by", first["manual_overrides"][0])
        self.assertIn("changes", first["manual_overrides"][0])

        retry = apply_review_resolutions(
            built["structure"],
            built["evidence"],
            built["review_issues"],
            [resolution],
            actor="researcher@example.com",
            resolved_at="2026-08-14T10:01:00+08:00",
            operation_fingerprint="1" * 64,
        )
        later_base = apply_review_resolutions(
            built["structure"],
            built["evidence"],
            built["review_issues"],
            [resolution],
            actor="researcher@example.com",
            resolved_at="2026-08-14T10:01:00+08:00",
            operation_fingerprint="2" * 64,
        )
        self.assertEqual(
            first["manual_overrides"][0]["manual_override_id"],
            retry["manual_overrides"][0]["manual_override_id"],
        )
        self.assertNotEqual(
            first["manual_overrides"][0]["manual_override_id"],
            later_base["manual_overrides"][0]["manual_override_id"],
        )

    def test_multiple_linked_evidence_close_only_after_mixed_resolution_is_complete(self):
        _snapshot, _mapping, _sha, built = build_fixture_checkpoint()
        issue = deepcopy(
            next(
                item
                for item in built["review_issues"]
                if item["code"] == "ROW_ROLE_UNKNOWN"
                and item["source_context"]["row"] == 13
            )
        )
        self.assertEqual(len(issue["affected_ids"]["evidence_ids"]), 2)
        issue["allowed_resolutions"].extend(
            ["exclude_evidence", "set_evidence_identity"]
        )
        first_id, second_id = issue["affected_ids"]["evidence_ids"]
        first = apply_review_resolutions(
            built["structure"],
            built["evidence"],
            [issue],
            [
                {
                    "issue_id": issue["issue_id"],
                    "resolution": "set_evidence_identity",
                    "target_id": first_id,
                    "evidence_type": "participant_self_report",
                    "comment": "只确认这一格",
                }
            ],
            actor="reviewer",
            resolved_at="2026-08-14T10:00:00+08:00",
            operation_fingerprint="3" * 64,
        )
        self.assertEqual(first["review_issues"][0]["status"], "open")

        second = apply_review_resolutions(
            first["structure"],
            first["evidence"],
            first["review_issues"],
            [
                {
                    "issue_id": issue["issue_id"],
                    "resolution": "exclude_evidence",
                    "target_id": second_id,
                    "comment": "排除剩余一格",
                }
            ],
            actor="reviewer",
            resolved_at="2026-08-14T10:02:00+08:00",
            operation_fingerprint="5" * 64,
        )
        self.assertEqual(second["review_issues"][0]["status"], "resolved")
        self.assertEqual(second["status"], "STRUCTURE_REVIEW_REQUIRED")

    def test_batch_is_atomic_when_later_resolution_is_invalid(self):
        _snapshot, _mapping, _sha, built = build_fixture_checkpoint()
        original_structure = deepcopy(built["structure"])
        original_evidence = deepcopy(built["evidence"])
        unknown_issue = next(
            item
            for item in built["review_issues"]
            if item["code"] == "ROW_ROLE_UNKNOWN"
            and item["source_context"]["row"] == 8
        )
        parent_issue = next(
            item
            for item in built["review_issues"]
            if item["code"] == "OBSERVATION_PARENT_MISSING"
        )
        with self.assertRaises(InterviewV2StructureError):
            apply_review_resolutions(
                built["structure"],
                built["evidence"],
                built["review_issues"],
                [
                    {
                        "issue_id": unknown_issue["issue_id"],
                        "resolution": "assign_row_role",
                        "row_role": "follow_up",
                        "comment": "先处理",
                    },
                    {
                        "issue_id": parent_issue["issue_id"],
                        "resolution": "assign_main_question",
                        "target_id": "question_" + "f" * 32,
                        "comment": "伪造目标",
                    },
                ],
                actor="reviewer",
                resolved_at="2026-08-14T10:00:00+08:00",
                operation_fingerprint="4" * 64,
            )
        self.assertEqual(built["structure"], original_structure)
        self.assertEqual(built["evidence"], original_evidence)

    def test_large_resolution_batch_builds_occurrence_index_once(self):
        _snapshot, _mapping, _sha, built = build_single_record_checkpoint()
        base_entry = built["evidence"]["entries"][0]
        entries = []
        issues = []
        resolutions = []
        for index in range(1, 241):
            occurrence_id = f"occurrence_{index:032x}"
            evidence_id = f"evidence_{index:032x}"
            entry = deepcopy(base_entry)
            entry.update(
                {
                    "evidence_id": evidence_id,
                    "occurrence_id": occurrence_id,
                    "identity_decision_status": "needs_review",
                }
            )
            entries.append(entry)
            if index <= 160:
                issue_id = f"issue_{index:032x}"
                issues.append(
                    {
                        "issue_id": issue_id,
                        "code": "ROW_ROLE_UNKNOWN",
                        "severity": "blocking",
                        "status": "open",
                        "allowed_resolutions": ["set_evidence_identity"],
                        "affected_ids": {
                            "occurrence_ids": [occurrence_id],
                            "evidence_ids": [],
                        },
                    }
                )
                resolutions.append(
                    {
                        "issue_id": issue_id,
                        "resolution": "set_evidence_identity",
                        "target_id": evidence_id,
                        "evidence_type": "participant_self_report",
                        "comment": "批量确认身份",
                    }
                )
        evidence = deepcopy(built["evidence"])
        evidence["entries"] = entries

        with patch.object(
            evidence_core,
            "_index_evidence_by_occurrence",
            wraps=evidence_core._index_evidence_by_occurrence,
        ) as index_builder:
            result = apply_review_resolutions(
                built["structure"],
                evidence,
                issues,
                resolutions,
                actor="reviewer",
                resolved_at="2026-08-14T10:00:00+08:00",
                operation_fingerprint="8" * 64,
            )

        index_builder.assert_called_once()
        self.assertEqual(len(result["manual_overrides"]), 160)
        self.assertTrue(
            all(issue["status"] == "resolved" for issue in result["review_issues"])
        )

    def test_participant_last_evidence_cannot_be_excluded(self):
        snapshot, mapping, _mapping_sha = single_record_bundle()
        sheet = snapshot["sheets"][0]
        sheet["cells"] = [cell for cell in sheet["cells"] if cell["row"] == 1]
        sheet["cells"].extend(
            _sheet(
                "unused",
                0,
                "unused",
                {
                    2: {1: "组队", 2: "模块"},
                    3: {2: "临时记录", 4: "待确认一"},
                    4: {2: "临时记录", 4: "待确认二"},
                },
                participant_columns=(4,),
            )["cells"][4:]
        )
        mapping_sha = hashlib.sha256(
            json.dumps(
                mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        built = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )
        issues = [
            issue
            for issue in built["review_issues"]
            if issue["code"] == "ROW_ROLE_UNKNOWN"
        ]
        self.assertEqual(len(issues), 2)
        self.assertTrue(
            all("exclude_evidence" in issue["allowed_resolutions"] for issue in issues)
        )
        first_issue, second_issue = sorted(
            issues, key=lambda issue: issue["source_context"]["row"]
        )
        current = apply_review_resolutions(
            built["structure"],
            built["evidence"],
            built["review_issues"],
            [
                {
                    "issue_id": first_issue["issue_id"],
                    "resolution": "exclude_evidence",
                    "target_id": first_issue["affected_ids"]["evidence_ids"][0],
                    "comment": "先排除一格",
                }
            ],
            actor="reviewer",
            resolved_at="2026-08-14T10:00:00+08:00",
            operation_fingerprint="6" * 64,
        )
        current_issue = next(
            issue
            for issue in current["review_issues"]
            if issue["issue_id"] == second_issue["issue_id"]
        )
        self.assertNotIn("exclude_evidence", current_issue["allowed_resolutions"])
        tampered_issues = deepcopy(current["review_issues"])
        next(
            issue
            for issue in tampered_issues
            if issue["issue_id"] == second_issue["issue_id"]
        )["allowed_resolutions"].append("exclude_evidence")
        with self.assertRaises(InterviewV2StructureError):
            apply_review_resolutions(
                current["structure"],
                current["evidence"],
                tampered_issues,
                [
                    {
                        "issue_id": second_issue["issue_id"],
                        "resolution": "exclude_evidence",
                        "target_id": second_issue["affected_ids"]["evidence_ids"][0],
                        "comment": "不能排空最后一格",
                    }
                ],
                actor="reviewer",
                resolved_at="2026-08-14T10:01:00+08:00",
                operation_fingerprint="7" * 64,
            )

    def test_assign_row_role_can_bind_cross_sheet_canonical_question(self):
        snapshot, mapping, _mapping_sha = single_record_bundle()
        snapshot["sheets"].append(
            _sheet(
                "sheet_002",
                1,
                "记录员2",
                {
                    2: {1: "组队", 2: "模块"},
                    3: {2: "临时记录", 4: "跨表补充回答"},
                },
                participant_columns=(4,),
            )
        )
        group = mapping["groups"][0]
        group["sheets"].append(
            {
                "sheet_id": "sheet_002",
                "index": 1,
                "role": "record",
                "recorder_label": "记录员2",
                "decision_status": "confirmed",
            }
        )
        group["participants"][0]["columns"].append(
            {"sheet_id": "sheet_002", "column_index": 4}
        )
        mapping_sha = hashlib.sha256(
            json.dumps(
                mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        built = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )
        issue = next(
            issue
            for issue in built["review_issues"]
            if issue["code"] == "ROW_ROLE_UNKNOWN"
        )
        question_id = built["structure"]["main_questions"][0]["main_question_id"]
        with self.assertRaises(InterviewV2StructureError):
            apply_review_resolutions(
                built["structure"],
                built["evidence"],
                built["review_issues"],
                [
                    {
                        "issue_id": issue["issue_id"],
                        "resolution": "assign_row_role",
                        "row_role": "main_question",
                        "target_id": question_id,
                        "comment": "主问题行不能携带目标",
                    }
                ],
                actor="reviewer",
                resolved_at="2026-08-14T10:00:00+08:00",
                operation_fingerprint="9" * 64,
            )

        resolved = apply_review_resolutions(
            built["structure"],
            built["evidence"],
            built["review_issues"],
            [
                {
                    "issue_id": issue["issue_id"],
                    "resolution": "assign_row_role",
                    "row_role": "follow_up",
                    "target_id": question_id,
                    "comment": "人工绑定已对齐的跨表主问题",
                }
            ],
            actor="reviewer",
            resolved_at="2026-08-14T10:01:00+08:00",
            operation_fingerprint="a" * 64,
        )
        occurrence = next(
            item
            for item in resolved["structure"]["occurrences"]
            if item["sheet_id"] == "sheet_002" and item["row"] == 3
        )
        self.assertEqual(occurrence["row_role"], "follow_up")
        self.assertEqual(occurrence["canonical_main_question_id"], question_id)
        self.assertIsNone(occurrence["parent_main_occurrence_id"])
        self.assertEqual(resolved["status"], "READY_FOR_DOSSIERS")

    def test_request_and_response_schemas_enforce_boundary(self):
        with self.assertRaises(ValidationError):
            InterviewV2StructureBuildRequest.model_validate(
                {
                    "base_mapping_revision_id": MAPPING_ID,
                    "base_mapping_sha256": "a" * 64,
                    "storage_path": "private",
                }
            )
        with self.assertRaises(ValidationError):
            InterviewV2ReviewIssuePatchRequest.model_validate(
                {
                    "base_structure_revision_id": "structure_" + "1" * 32,
                    "base_evidence_revision_id": "evidence_" + "2" * 32,
                    "resolution": "assign_main_question",
                    "target_id": "module_" + "3" * 32,
                    "comment": "wrong target type",
                }
            )
        with self.assertRaises(ValidationError):
            InterviewV2ReviewIssuePatchRequest.model_validate(
                {
                    "base_structure_revision_id": "structure_" + "1" * 32,
                    "base_evidence_revision_id": "evidence_" + "2" * 32,
                    "resolution": "assign_row_role",
                    "row_role": "follow_up",
                    "evidence_type": "researcher_observation",
                    "comment": "irrelevant field must be rejected",
                }
            )
        with self.assertRaises(ValidationError):
            InterviewV2ReviewIssuePatchRequest.model_validate(
                {
                    "base_structure_revision_id": "structure_" + "1" * 32,
                    "base_evidence_revision_id": "evidence_" + "2" * 32,
                    "resolution": "assign_row_role",
                    "row_role": "unknown",
                    "comment": "unknown is not a resolution",
                }
            )
        combined = InterviewV2ReviewIssuePatchRequest.model_validate(
            {
                "base_structure_revision_id": "structure_" + "1" * 32,
                "base_evidence_revision_id": "evidence_" + "2" * 32,
                "resolution": "assign_row_role",
                "row_role": "follow_up",
                "target_id": "question_" + "3" * 32,
                "comment": "显式绑定已确认主问题",
            }
        )
        self.assertEqual(combined.target_id, "question_" + "3" * 32)
        with self.assertRaises(ValidationError):
            InterviewV2ReviewIssuePatchRequest.model_validate(
                {
                    "base_structure_revision_id": "structure_" + "1" * 32,
                    "base_evidence_revision_id": "evidence_" + "2" * 32,
                    "resolution": "assign_row_role",
                    "row_role": "main_question",
                    "target_id": "question_" + "3" * 32,
                    "comment": "主问题行不能同时指定目标",
                }
            )
        with self.assertRaises(ValidationError):
            InterviewV2ReviewIssuePatchRequest.model_validate(
                {
                    "base_structure_revision_id": "structure_" + "1" * 32,
                    "base_evidence_revision_id": "evidence_" + "2" * 32,
                    "resolution": "assign_row_role",
                    "row_role": "follow_up",
                    "comment": "\ud800",
                }
            )

        _snapshot, _mapping, _sha, built = build_fixture_checkpoint()
        structure_payload = deepcopy(built["structure"])
        structure_payload["owner_email"] = "owner@example.com"
        structure_payload["source"]["storage_path"] = "private/path"
        structure_payload["occurrences"][0]["confirmed_by"] = "private@example.com"
        response = InterviewV2StructureBuildResponse.model_validate(
            {
                "import_id": IMPORT_ID,
                "project_id": PROJECT_ID,
                "status": built["status"],
                "structure_revision_id": "structure_" + "1" * 32,
                "evidence_revision_id": "evidence_" + "2" * 32,
                "structure": structure_payload,
                "evidence_summary": {"evidence_count": 9, "owner_email": "private"},
                "review_summary": {
                    "open_issue_count": len(built["review_issues"]),
                    "blocking_issue_count": built["blocking_issue_count"],
                    "storage_path": "private",
                },
                "owner_email": "private@example.com",
            }
        ).model_dump(mode="json")
        rendered = repr(response)
        self.assertNotIn("owner_email", rendered)
        self.assertNotIn("storage_path", rendered)
        self.assertNotIn("confirmed_by", rendered)

        evidence = deepcopy(built["evidence"]["entries"][0])
        occurrence = next(
            item
            for item in built["structure"]["occurrences"]
            if item["occurrence_id"] == evidence["occurrence_id"]
        )
        context = InterviewV2EvidenceContextResponse.model_validate(
            {
                "evidence_id": evidence["evidence_id"],
                "structure_revision_id": "structure_" + "1" * 32,
                "evidence_revision_id": "evidence_" + "2" * 32,
                "evidence": {**evidence, "owner_email": "private@example.com"},
                "occurrence": {**occurrence, "confirmed_by": "private@example.com"},
                "source_context": {
                    "source_cell_id": evidence["source_cell_id"],
                    "sheet_id": evidence["sheet_id"],
                    "sheet_name": evidence["sheet_name"],
                    "row": evidence["row"],
                    "column": evidence["column"],
                    "cell_address": evidence["cell_address"],
                    "neighboring_occurrences": [],
                    "raw_other_participant": "must not leak",
                    "storage_path": "private/path",
                },
            }
        ).model_dump(mode="json")
        rendered_context = repr(context)
        self.assertNotIn("owner_email", rendered_context)
        self.assertNotIn("confirmed_by", rendered_context)
        self.assertNotIn("raw_other_participant", rendered_context)
        self.assertNotIn("storage_path", rendered_context)


if __name__ == "__main__":
    unittest.main()
