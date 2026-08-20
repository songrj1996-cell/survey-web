import hashlib
import json
import unittest

from app.core.interview_v2_structure import (
    InterviewV2StructureError,
    build_structure,
)


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
WORKBOOK_ID = "workbook_" + "3" * 32
MAPPING_ID = "mapping_" + "4" * 32
SNAPSHOT_SHA = "a" * 64


def _cell(address, row, column, value):
    text = str(value)
    return {
        "address": address,
        "row": row,
        "column": column,
        "raw_value": value,
        "display_value": text,
        "normalized_text": text.strip(),
        "value_sha256": hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
    }


def _sheet(sheet_id, index, name, rows, *, participant_columns=(4, 5)):
    cells = [
        _cell("A1", 1, 1, "功能模块"),
        _cell("B1", 1, 2, "行类型"),
        _cell("C1", 1, 3, "问题/备注"),
    ]
    for column in participant_columns:
        letter = chr(64 + column)
        cells.append(_cell(f"{letter}1", 1, column, f"P{column - 3:02d}"))
    for row, values in rows.items():
        for column, value in values.items():
            if value is None or value == "":
                continue
            letter = chr(64 + column)
            cells.append(_cell(f"{letter}{row}", row, column, value))
    return {
        "sheet_id": sheet_id,
        "index": index,
        "name": name,
        "dimensions": {
            "content_min_column": 1,
            "content_max_column": max(participant_columns, default=3),
        },
        "candidate_structure": {"start_column": 1, "end_column": 3},
        "candidate_participant_region": {
            "start_column": min(participant_columns, default=4),
            "end_column": max(participant_columns, default=4),
            "header_row": 1,
        },
        "cells": cells,
    }


def fixture_bundle():
    sheet_one = _sheet(
        "sheet_001",
        0,
        "1组记录1",
        {
            2: {1: "组队", 2: "模块"},
            3: {2: "主问题", 3: "1. 你如何组队？", 4: "自己邀请好友"},
            4: {2: "追问", 3: "为什么？", 4: "配合稳定", 5: "更有趣"},
            5: {2: "观察备注", 4: "操作时停顿", 5: "一次成功"},
            6: {2: "追问", 3: "还有吗？"},
            8: {2: "临时记录", 4: "待确认内容"},
            9: {1: "新模块", 2: "观察备注", 4: "无父观察"},
            10: {1: "收尾", 2: "模块标题", 4: "标题行中的内容"},
            11: {2: "主问题", 3: "问题一共有多少方案？"},
            12: {2: "主问题", 3: "共有多少方案？"},
            13: {2: "临时记录", 4: "待确认甲", 5: "待确认乙"},
        },
    )
    sheet_two = _sheet(
        "sheet_002",
        1,
        "1组记录2",
        {
            2: {1: "组队", 2: "模块"},
            3: {
                2: "主问题",
                3: "1、你如何组队？",
                4: "随机匹配",
                5: "固定队友",
            },
            4: {2: "追问", 3: "为什么？", 4: "更省时间"},
        },
    )
    guide = _sheet(
        "sheet_003",
        2,
        "提纲参考",
        {
            2: {1: "组队", 2: "模块"},
            3: {2: "主问题", 3: "你如何组队？"},
        },
        participant_columns=(),
    )
    attributes = _sheet(
        "sheet_004",
        3,
        "属性参考",
        {2: {1: "不应进入", 2: "主问题", 3: "年龄？"}},
        participant_columns=(),
    )
    snapshot = {
        "snapshot_sha256": SNAPSHOT_SHA,
        "sheets": [sheet_one, sheet_two, guide, attributes],
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
                    },
                    {
                        "sheet_id": "sheet_002",
                        "index": 1,
                        "role": "record",
                        "recorder_label": "记录员2",
                        "decision_status": "confirmed",
                    },
                    {
                        "sheet_id": "sheet_003",
                        "index": 2,
                        "role": "guide_reference",
                        "recorder_label": "",
                        "decision_status": "confirmed",
                    },
                    {
                        "sheet_id": "sheet_004",
                        "index": 3,
                        "role": "attribute_reference",
                        "recorder_label": "",
                        "decision_status": "confirmed",
                    },
                ],
                "participants": [
                    {
                        "participant_id": "participant_" + "6" * 32,
                        "participant_label": "P01",
                        "decision_status": "confirmed",
                        "columns": [
                            {"sheet_id": "sheet_001", "column_index": 4},
                            {"sheet_id": "sheet_002", "column_index": 4},
                        ],
                    },
                    {
                        "participant_id": "participant_" + "7" * 32,
                        "participant_label": "P02",
                        "decision_status": "confirmed",
                        "columns": [
                            {"sheet_id": "sheet_001", "column_index": 5},
                            {"sheet_id": "sheet_002", "column_index": 5},
                        ],
                    },
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


def build_fixture_structure():
    snapshot, mapping, mapping_sha = fixture_bundle()
    result = build_structure(
        snapshot,
        mapping,
        project_id=PROJECT_ID,
        import_id=IMPORT_ID,
        workbook_revision_id=WORKBOOK_ID,
        mapping_revision_id=MAPPING_ID,
        mapping_sha256=mapping_sha,
    )
    return snapshot, mapping, mapping_sha, result


class InterviewV2StructureTests(unittest.TestCase):
    def test_explicit_unknown_type_is_not_silently_promoted_to_module(self):
        snapshot, mapping, mapping_sha = fixture_bundle()
        snapshot["sheets"][0]["cells"].extend(
            [
                _cell("A20", 20, 1, "明确文本"),
                _cell("B20", 20, 2, "未识别类型"),
            ]
        )
        result = build_structure(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )
        occurrence = next(
            item
            for item in result["structure"]["occurrences"]
            if item["sheet_id"] == "sheet_001" and item["row"] == 20
        )
        self.assertEqual(occurrence["row_role"], "unknown")
        self.assertTrue(
            any(
                item["code"] == "ROW_ROLE_UNKNOWN"
                and item["affected_ids"]["occurrence_ids"]
                == [occurrence["occurrence_id"]]
                for item in result["review_issues"]
            )
        )

    def test_explicit_rows_align_only_normalized_exact_main_questions(self):
        _snapshot, _mapping, _sha, result = build_fixture_structure()
        structure = result["structure"]
        main_occurrences = [
            item
            for item in structure["occurrences"]
            if item["row_role"] == "main_question"
            and item["raw_prompt_text"]
            and "如何组队" in item["raw_prompt_text"]
        ]
        self.assertEqual(len(main_occurrences), 3)
        self.assertEqual(
            len({item["canonical_main_question_id"] for item in main_occurrences}),
            1,
        )

        natural_numbering = [
            item
            for item in structure["occurrences"]
            if item.get("raw_prompt_text")
            in {"问题一共有多少方案？", "共有多少方案？"}
        ]
        self.assertEqual(len(natural_numbering), 2)
        self.assertNotEqual(
            natural_numbering[0]["canonical_main_question_id"],
            natural_numbering[1]["canonical_main_question_id"],
        )
        self.assertFalse(
            any(item["sheet_id"] == "sheet_004" for item in structure["occurrences"])
        )

    def test_followups_and_observations_inherit_only_same_sheet_current_main(self):
        _snapshot, _mapping, _sha, result = build_fixture_structure()
        occurrences = result["structure"]["occurrences"]
        sheet_one_main = next(
            item
            for item in occurrences
            if item["sheet_id"] == "sheet_001" and item["row"] == 3
        )
        for row in (4, 5, 6):
            occurrence = next(
                item
                for item in occurrences
                if item["sheet_id"] == "sheet_001" and item["row"] == row
            )
            self.assertEqual(
                occurrence["parent_main_occurrence_id"],
                sheet_one_main["occurrence_id"],
            )
        orphan = next(
            item
            for item in occurrences
            if item["sheet_id"] == "sheet_001" and item["row"] == 9
        )
        self.assertIsNone(orphan["parent_main_occurrence_id"])
        issue_codes = {item["code"] for item in result["review_issues"]}
        self.assertIn("OBSERVATION_PARENT_MISSING", issue_codes)
        self.assertIn("ROW_ROLE_UNKNOWN", issue_codes)
        self.assertNotIn("assign_structure_columns", repr(result["review_issues"]))

    def test_build_is_stable_and_rejects_unfrozen_or_unconfirmed_mapping(self):
        snapshot, mapping, mapping_sha, first = build_fixture_structure()
        second = build_structure(
            snapshot,
            mapping,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            mapping_revision_id=MAPPING_ID,
            mapping_sha256=mapping_sha,
        )
        self.assertEqual(first, second)

        with self.assertRaises(InterviewV2StructureError) as caught:
            build_structure(
                snapshot,
                mapping,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                mapping_revision_id=MAPPING_ID,
                mapping_sha256="f" * 64,
            )
        self.assertEqual(caught.exception.code, "MAPPING_DIGEST_MISMATCH")

        mapping["groups"][0]["decision_status"] = "proposed"
        changed_sha = hashlib.sha256(
            json.dumps(
                mapping,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaises(InterviewV2StructureError) as caught:
            build_structure(
                snapshot,
                mapping,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                mapping_revision_id=MAPPING_ID,
                mapping_sha256=changed_sha,
            )
        self.assertEqual(caught.exception.code, "MAPPING_NOT_CONFIRMED")


if __name__ == "__main__":
    unittest.main()
