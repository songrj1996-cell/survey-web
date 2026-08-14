import unittest
from copy import deepcopy

from pydantic import ValidationError

from app.core.interview_v2_mapping import (
    InterviewV2MappingError,
    build_group_proposals,
    normalize_and_validate_mapping,
)
from app.schemas.interview_v2_mapping import (
    InterviewV2GroupMappingDraftRequest,
)


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
WORKBOOK_ID = "workbook_" + "3" * 32


def _sheet(sheet_id, index, name, headers, *, start=4):
    profiles = []
    for offset, header in enumerate(headers):
        column = start + offset
        profiles.append(
            {
                "column": column,
                "column_letter": chr(64 + column),
                "header_address": f"{chr(64 + column)}1",
                "header_value": header,
                "first_non_empty_row": 1,
                "non_empty_count": 3,
            }
        )
    return {
        "sheet_id": sheet_id,
        "index": index,
        "name": name,
        "state": "visible",
        "candidate_participant_region": {
            "start_column": start,
            "end_column": start + len(headers) - 1,
            "candidate_count": len(headers),
            "header_row": 1,
            "basis": ["participant_like_column_headers"],
        },
        "column_profiles": profiles,
        "cells": [
            {
                "address": "D2",
                "raw_value": "private answer must never enter an issue context",
            }
        ],
    }


def _snapshot():
    return {
        "snapshot_sha256": "a" * 64,
        "sheets": [
            _sheet("sheet_01", 0, "1组记录1", ["P01", "P02"]),
            _sheet("sheet_02", 1, "1组记录2", ["P02", "P01"]),
            _sheet("sheet_03", 2, "2组记录1", ["P01", "P02"]),
            {
                "sheet_id": "sheet_04",
                "index": 3,
                "name": "说明页",
                "state": "visible",
                "candidate_participant_region": None,
                "column_profiles": [],
            },
        ],
    }


def _complete_request():
    return {
        "base_mapping_revision": 0,
        "groups": [
            {
                "group_id": "client-temp-1",
                "display_name": "第1组",
                "sheets": [
                    {
                        "sheet_id": "sheet_01",
                        "role": "record",
                        "recorder_label": "记录1",
                    },
                    {
                        "sheet_id": "sheet_02",
                        "role": "record",
                        "recorder_label": "记录2",
                    },
                ],
                "participants": [
                    {
                        "participant_label": "P01",
                        "columns": [
                            {"sheet_id": "sheet_01", "column_index": 4},
                            {"sheet_id": "sheet_02", "column_index": 5},
                        ],
                    },
                    {
                        "participant_label": "P02",
                        "columns": [
                            {"sheet_id": "sheet_01", "column_index": 5},
                            {"sheet_id": "sheet_02", "column_index": 4},
                        ],
                    },
                ],
            },
            {
                "group_id": "client-temp-2",
                "display_name": "第2组",
                "sheets": [
                    {
                        "sheet_id": "sheet_03",
                        "role": "record",
                        "recorder_label": "记录1",
                    }
                ],
                "participants": [
                    {
                        "participant_label": "P01",
                        "columns": [
                            {"sheet_id": "sheet_03", "column_index": 4}
                        ],
                    },
                    {
                        "participant_label": "P02",
                        "columns": [
                            {"sheet_id": "sheet_03", "column_index": 5}
                        ],
                    },
                ],
            },
        ],
        "ignored_sheet_ids": ["sheet_04"],
        "change_kind": "manual_edit",
        "change_reason": "确认两组玩家关系",
    }


def _request_from_normalized(mapping, *, base_mapping_revision=1):
    return {
        "base_mapping_revision": base_mapping_revision,
        "groups": [
            {
                "group_id": group["group_id"],
                "display_name": group["display_name"],
                "sheets": [
                    {
                        "sheet_id": sheet["sheet_id"],
                        "role": sheet["role"],
                        "recorder_label": sheet["recorder_label"],
                    }
                    for sheet in group["sheets"]
                ],
                "participants": [
                    {
                        "participant_id": participant["participant_id"],
                        "participant_label": participant["participant_label"],
                        "columns": [
                            {
                                "sheet_id": column["sheet_id"],
                                "column_index": column["column_index"],
                            }
                            for column in participant["columns"]
                        ],
                    }
                    for participant in group["participants"]
                ],
            }
            for group in mapping["groups"]
        ],
        "ignored_sheet_ids": list(mapping["ignored_sheet_ids"]),
        "change_kind": "manual_edit",
        "change_reason": "继续编辑当前映射",
    }


class InterviewV2MappingCoreTests(unittest.TestCase):
    def test_sheet_names_and_exact_headers_only_create_proposals(self):
        proposals = build_group_proposals(
            _snapshot(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )

        self.assertEqual(len(proposals["groups"]), 2)
        first, second = proposals["groups"]
        self.assertEqual(
            [sheet["sheet_id"] for sheet in first["sheets"]],
            ["sheet_01", "sheet_02"],
        )
        self.assertTrue(
            all(group["decision_status"] == "proposed" for group in proposals["groups"])
        )
        first_p01 = next(
            participant
            for participant in first["participants"]
            if participant["participant_label"] == "P01"
        )
        self.assertEqual(
            {(item["sheet_id"], item["column_index"]) for item in first_p01["columns"]},
            {("sheet_01", 4), ("sheet_02", 5)},
        )
        second_p01 = next(
            participant
            for participant in second["participants"]
            if participant["participant_label"] == "P01"
        )
        self.assertNotEqual(first_p01["participant_id"], second_p01["participant_id"])
        self.assertEqual(
            {item["code"] for item in proposals["issues"]},
            {
                "GROUP_MAPPING_CONFIRMATION_REQUIRED",
                "PARTICIPANT_MAPPING_CONFIRMATION_REQUIRED",
                "SHEET_ROLE_AMBIGUOUS",
            },
        )
        self.assertFalse(proposals["confirmation_ready"])

    def test_complete_user_mapping_confirms_columns_without_position_alignment(self):
        result = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )

        self.assertTrue(result["confirmation_ready"])
        self.assertEqual(result["issues"], [])
        groups = result["mapping"]["groups"]
        self.assertEqual(len(groups), 2)
        self.assertTrue(
            all(
                column["decision_status"] == "confirmed"
                for group in groups
                for participant in group["participants"]
                for column in participant["columns"]
            )
        )
        p01_ids = [
            participant["participant_id"]
            for group in groups
            for participant in group["participants"]
            if participant["participant_label"] == "P01"
        ]
        self.assertEqual(len(p01_ids), 2)
        self.assertEqual(len(set(p01_ids)), 2)
        first_p01 = next(
            participant
            for participant in groups[0]["participants"]
            if participant["participant_label"] == "P01"
        )
        self.assertEqual(
            {(item["sheet_id"], item["column_index"]) for item in first_p01["columns"]},
            {("sheet_01", 4), ("sheet_02", 5)},
        )
        self.assertEqual(result["final_participant_preview"]["participant_count"], 4)

    def test_draft_can_save_missing_columns_but_cannot_be_confirmed(self):
        request = _complete_request()
        request["groups"][0]["participants"][1]["columns"].pop()
        result = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )

        self.assertFalse(result["confirmation_ready"])
        missing = [
            item for item in result["issues"] if item["code"] == "PARTICIPANT_COLUMN_MISSING"
        ]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["level"], "blocking")
        self.assertEqual(missing[0]["context"]["sheet_id"], "sheet_02")
        self.assertNotIn("private answer", repr(result["issues"]))

    def test_cross_group_binding_is_rejected_without_raw_content(self):
        request = _complete_request()
        request["groups"][0]["participants"][0]["columns"].append(
            {"sheet_id": "sheet_03", "column_index": 4}
        )
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                request,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
            )
        self.assertEqual(caught.exception.code, "CROSS_GROUP_PARTICIPANT_MERGE_ATTEMPT")
        self.assertNotIn("private answer", repr(caught.exception.context))

    def test_duplicate_sheet_and_duplicate_column_are_rejected(self):
        duplicate_sheet = _complete_request()
        duplicate_sheet["ignored_sheet_ids"].append("sheet_01")
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                duplicate_sheet,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
            )
        self.assertEqual(caught.exception.code, "SHEET_ASSIGNMENT_DUPLICATE")

        duplicate_column = _complete_request()
        duplicate_column["groups"][0]["participants"][1]["columns"][0] = {
            "sheet_id": "sheet_01",
            "column_index": 4,
        }
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                duplicate_column,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
            )
        self.assertEqual(caught.exception.code, "PARTICIPANT_COLUMN_DUPLICATE")

    def test_reference_sheet_does_not_require_player_binding(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        request = _complete_request()
        request["ignored_sheet_ids"] = []
        request["groups"][0]["sheets"].append(
            {
                "sheet_id": "sheet_04",
                "role": "guide_reference",
                "recorder_label": "",
            }
        )
        result = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        self.assertTrue(result["confirmation_ready"])
        self.assertEqual(
            baseline["mapping"]["groups"][0]["group_id"],
            result["mapping"]["groups"][0]["group_id"],
        )
        self.assertEqual(
            [
                participant["participant_id"]
                for participant in baseline["mapping"]["groups"][0]["participants"]
            ],
            [
                participant["participant_id"]
                for participant in result["mapping"]["groups"][0]["participants"]
            ],
        )

        second_request = _request_from_normalized(
            result["mapping"], base_mapping_revision=2
        )
        moved_reference = second_request["groups"][0]["sheets"].pop()
        second_request["groups"][1]["sheets"].append(moved_reference)
        moved = normalize_and_validate_mapping(
            _snapshot(),
            second_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=result["mapping"],
        )
        self.assertTrue(moved["confirmation_ready"])
        self.assertEqual(
            [group["group_id"] for group in result["mapping"]["groups"]],
            [group["group_id"] for group in moved["mapping"]["groups"]],
        )

    def test_participant_identity_does_not_change_with_source_column_set(self):
        complete = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        partial_request = _complete_request()
        partial_request["groups"][0]["participants"][0]["columns"].pop()
        partial = normalize_and_validate_mapping(
            _snapshot(),
            partial_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        complete_p01 = complete["mapping"]["groups"][0]["participants"][0]
        partial_p01 = partial["mapping"]["groups"][0]["participants"][0]
        self.assertEqual(complete_p01["participant_id"], partial_p01["participant_id"])

    def test_participant_identity_normalizes_unicode_equivalents(self):
        nfc_request = _complete_request()
        nfc_request["groups"][0]["participants"][0]["participant_label"] = "José"
        nfd_request = _complete_request()
        nfd_request["groups"][0]["participants"][0]["participant_label"] = "Jose\u0301"
        nfc = normalize_and_validate_mapping(
            _snapshot(),
            nfc_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        nfd = normalize_and_validate_mapping(
            _snapshot(),
            nfd_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        nfc_participant = nfc["mapping"]["groups"][0]["participants"][0]
        nfd_participant = nfd["mapping"]["groups"][0]["participants"][0]
        self.assertEqual(nfc_participant["participant_id"], nfd_participant["participant_id"])
        self.assertEqual(nfd_participant["participant_label"], "José")

    def test_existing_group_and_participants_keep_identity_when_record_sheet_is_added_then_removed(self):
        baseline_request = _complete_request()
        first_group = baseline_request["groups"][0]
        first_group["sheets"] = first_group["sheets"][:1]
        for participant in first_group["participants"]:
            participant["columns"] = [
                column
                for column in participant["columns"]
                if column["sheet_id"] == "sheet_01"
            ]
        baseline_request["ignored_sheet_ids"] = ["sheet_02", "sheet_04"]
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            baseline_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        baseline_mapping = baseline["mapping"]
        baseline_group = baseline_mapping["groups"][0]
        baseline_participant_ids = {
            participant["participant_label"]: participant["participant_id"]
            for participant in baseline_group["participants"]
        }

        add_request = _request_from_normalized(baseline_mapping)
        add_request["ignored_sheet_ids"].remove("sheet_02")
        add_group = add_request["groups"][0]
        add_group["sheets"].append(
            {
                "sheet_id": "sheet_02",
                "role": "record",
                "recorder_label": "记录2",
            }
        )
        second_sheet_columns = {"P01": 5, "P02": 4}
        for participant in add_group["participants"]:
            participant["columns"].append(
                {
                    "sheet_id": "sheet_02",
                    "column_index": second_sheet_columns[participant["participant_label"]],
                }
            )
        added = normalize_and_validate_mapping(
            _snapshot(),
            add_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline_mapping,
        )
        added_group = added["mapping"]["groups"][0]
        self.assertEqual(baseline_group["group_id"], added_group["group_id"])
        self.assertEqual(
            baseline_participant_ids,
            {
                participant["participant_label"]: participant["participant_id"]
                for participant in added_group["participants"]
            },
        )

        remove_request = _request_from_normalized(
            added["mapping"], base_mapping_revision=2
        )
        remove_group = remove_request["groups"][0]
        remove_group["sheets"] = [
            sheet for sheet in remove_group["sheets"] if sheet["sheet_id"] != "sheet_02"
        ]
        for participant in remove_group["participants"]:
            participant["columns"] = [
                column
                for column in participant["columns"]
                if column["sheet_id"] != "sheet_02"
            ]
        remove_request["ignored_sheet_ids"].append("sheet_02")
        removed = normalize_and_validate_mapping(
            _snapshot(),
            remove_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=added["mapping"],
        )
        self.assertEqual(
            baseline_group["group_id"],
            removed["mapping"]["groups"][0]["group_id"],
        )
        self.assertEqual(
            baseline_participant_ids,
            {
                participant["participant_label"]: participant["participant_id"]
                for participant in removed["mapping"]["groups"][0]["participants"]
            },
        )

    def test_existing_participant_id_preserves_identity_when_label_and_sources_change(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        request = _request_from_normalized(baseline["mapping"])
        participant = request["groups"][0]["participants"][0]
        existing_id = participant["participant_id"]
        participant["participant_label"] = "核心玩家A"
        participant["columns"] = participant["columns"][:1]

        result = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
        )
        renamed = next(
            item
            for item in result["mapping"]["groups"][0]["participants"]
            if item["participant_label"] == "核心玩家A"
        )
        self.assertEqual(existing_id, renamed["participant_id"])

        new_participant_request = _request_from_normalized(baseline["mapping"])
        new_participant = new_participant_request["groups"][0]["participants"][0]
        new_participant.pop("participant_id")
        new_participant["participant_label"] = "新玩家A"
        regenerated = normalize_and_validate_mapping(
            _snapshot(),
            new_participant_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
        )
        created = next(
            item
            for item in regenerated["mapping"]["groups"][0]["participants"]
            if item["participant_label"] == "新玩家A"
        )
        self.assertNotEqual(existing_id, created["participant_id"])

    def test_new_player_cannot_reuse_a_renamed_existing_player_id(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        request = _request_from_normalized(baseline["mapping"])
        group = request["groups"][0]
        existing = next(
            participant
            for participant in group["participants"]
            if participant["participant_label"] == "P01"
        )
        existing_id = existing["participant_id"]
        freed_column = existing["columns"].pop()
        existing["participant_label"] = "ExistingRenamed"
        group["participants"].append(
            {
                "participant_label": "P01",
                "columns": [freed_column],
            }
        )

        result = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
        )
        participants = result["mapping"]["groups"][0]["participants"]
        participant_ids = [participant["participant_id"] for participant in participants]
        new_player = next(
            participant
            for participant in participants
            if participant["participant_label"] == "P01"
        )
        self.assertTrue(result["confirmation_ready"])
        self.assertEqual(len(participant_ids), len(set(participant_ids)))
        self.assertNotEqual(existing_id, new_player["participant_id"])

    def test_recreated_player_gets_a_new_id_in_a_later_revision(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        add_request = _request_from_normalized(baseline["mapping"])
        group = add_request["groups"][0]
        existing = group["participants"][0]
        freed_column = existing["columns"].pop()
        group["participants"].append(
            {"participant_label": "New P01", "columns": [freed_column]}
        )
        added = normalize_and_validate_mapping(
            _snapshot(),
            add_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=2,
        )
        first_new_id = next(
            participant["participant_id"]
            for participant in added["mapping"]["groups"][0]["participants"]
            if participant["participant_label"] == "New P01"
        )

        recreate_request = deepcopy(add_request)
        recreated = normalize_and_validate_mapping(
            _snapshot(),
            recreate_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=4,
        )
        later_new_id = next(
            participant["participant_id"]
            for participant in recreated["mapping"]["groups"][0]["participants"]
            if participant["participant_label"] == "New P01"
        )
        self.assertNotEqual(first_new_id, later_new_id)

    def test_new_player_identity_does_not_depend_on_request_order(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        request = _request_from_normalized(baseline["mapping"])
        group = request["groups"][0]
        freed_columns = []
        for participant in group["participants"]:
            freed_columns.append(participant["columns"].pop())
        new_players = [
            {"participant_label": f"New{index}", "columns": [column]}
            for index, column in enumerate(freed_columns)
        ]
        group["participants"].extend(new_players)
        first = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=2,
        )
        group["participants"][-2:] = reversed(group["participants"][-2:])
        second = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=2,
        )
        self.assertEqual(first["mapping"], second["mapping"])

    def test_forged_and_cross_group_identity_claims_are_rejected(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )

        forged_group = _request_from_normalized(baseline["mapping"])
        forged_group["groups"][0]["group_id"] = "group_" + "f" * 32
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                forged_group,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=baseline["mapping"],
            )
        self.assertEqual(caught.exception.code, "GROUP_ID_INVALID")

        forged_participant = _request_from_normalized(baseline["mapping"])
        forged_participant["groups"][0]["participants"][0]["participant_id"] = (
            "participant_" + "f" * 32
        )
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                forged_participant,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=baseline["mapping"],
            )
        self.assertEqual(caught.exception.code, "PARTICIPANT_ID_INVALID")

        cross_group = _request_from_normalized(baseline["mapping"])
        cross_group["groups"][0]["participants"][0]["participant_id"] = (
            cross_group["groups"][1]["participants"][0]["participant_id"]
        )
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                cross_group,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=baseline["mapping"],
            )
        self.assertEqual(caught.exception.code, "PARTICIPANT_ID_INVALID")

    def test_merged_or_split_groups_cannot_inherit_an_old_group_id(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        base_mapping = baseline["mapping"]

        merged = _request_from_normalized(base_mapping)
        merged_first = merged["groups"][0]
        merged_second = merged["groups"].pop(1)
        merged_first["sheets"].extend(merged_second["sheets"])
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                merged,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=base_mapping,
            )
        self.assertEqual(caught.exception.code, "GROUP_ID_INHERITANCE_INVALID")

        split = _request_from_normalized(base_mapping)
        first = split["groups"][0]
        moved_sheet = first["sheets"].pop()
        moved_participants = []
        for participant in first["participants"]:
            moved_columns = [
                column
                for column in participant["columns"]
                if column["sheet_id"] == moved_sheet["sheet_id"]
            ]
            participant["columns"] = [
                column
                for column in participant["columns"]
                if column["sheet_id"] != moved_sheet["sheet_id"]
            ]
            moved_participants.append(
                {
                    "participant_label": participant["participant_label"],
                    "columns": moved_columns,
                }
            )
        split["groups"].insert(
            1,
            {
                "display_name": "拆出的新组",
                "sheets": [moved_sheet],
                "participants": moved_participants,
            },
        )
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                split,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=base_mapping,
            )
        self.assertEqual(caught.exception.code, "GROUP_ID_INHERITANCE_INVALID")

        merged_without_claim = deepcopy(merged)
        merged_group = merged_without_claim["groups"][0]
        merged_group.pop("group_id")
        for participant in merged_group["participants"]:
            participant.pop("participant_id")
        for moved_participant in merged_second["participants"]:
            target = next(
                participant
                for participant in merged_group["participants"]
                if participant["participant_label"]
                == moved_participant["participant_label"]
            )
            target["columns"].extend(moved_participant["columns"])
        regenerated = normalize_and_validate_mapping(
            _snapshot(),
            merged_without_claim,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=base_mapping,
        )
        self.assertNotIn(
            regenerated["mapping"]["groups"][0]["group_id"],
            {group["group_id"] for group in base_mapping["groups"]},
        )

    def test_record_sheet_demoted_to_reference_cannot_anchor_group_identity(self):
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            _complete_request(),
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        request = _request_from_normalized(baseline["mapping"])
        group = request["groups"][0]
        for sheet in group["sheets"]:
            sheet["role"] = "guide_reference"
        group["participants"] = []
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                _snapshot(),
                request,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=baseline["mapping"],
                target_mapping_revision=2,
            )
        self.assertEqual(caught.exception.code, "GROUP_ID_INHERITANCE_INVALID")

        group.pop("group_id")
        regenerated = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=2,
        )
        self.assertNotIn(
            regenerated["mapping"]["groups"][0]["group_id"],
            {group["group_id"] for group in baseline["mapping"]["groups"]},
        )

    def test_reference_only_group_can_round_trip_its_own_identity(self):
        request = _complete_request()
        request["ignored_sheet_ids"] = []
        request["groups"].append(
            {
                "display_name": "参考资料",
                "sheets": [
                    {
                        "sheet_id": "sheet_04",
                        "role": "guide_reference",
                        "recorder_label": "",
                    }
                ],
                "participants": [],
            }
        )
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        round_trip_request = _request_from_normalized(
            baseline["mapping"], base_mapping_revision=1
        )
        round_trip = normalize_and_validate_mapping(
            _snapshot(),
            round_trip_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=2,
        )
        self.assertTrue(round_trip["confirmation_ready"])
        self.assertEqual(
            [group["group_id"] for group in baseline["mapping"]["groups"]],
            [group["group_id"] for group in round_trip["mapping"]["groups"]],
        )

    def test_reference_sheet_promoted_to_record_cannot_anchor_group_identity(self):
        request = _complete_request()
        request["ignored_sheet_ids"] = []
        request["groups"].append(
            {
                "display_name": "参考资料",
                "sheets": [
                    {
                        "sheet_id": "sheet_04",
                        "role": "guide_reference",
                        "recorder_label": "",
                    }
                ],
                "participants": [],
            }
        )
        baseline = normalize_and_validate_mapping(
            _snapshot(),
            request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        promoted_request = _request_from_normalized(
            baseline["mapping"], base_mapping_revision=1
        )
        promoted_group = next(
            group
            for group in promoted_request["groups"]
            if group["display_name"] == "参考资料"
        )
        promoted_group["sheets"][0].update(
            {"role": "record", "recorder_label": "记录1"}
        )
        promoted_group["participants"] = [
            {
                "participant_label": "P01",
                "columns": [{"sheet_id": "sheet_04", "column_index": 4}],
            }
        ]

        snapshot = _snapshot()
        snapshot["sheets"][-1] = _sheet(
            "sheet_04", 3, "说明页", ["P01"]
        )
        with self.assertRaises(InterviewV2MappingError) as caught:
            normalize_and_validate_mapping(
                snapshot,
                promoted_request,
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                workbook_revision_id=WORKBOOK_ID,
                base_mapping=baseline["mapping"],
                target_mapping_revision=2,
            )
        self.assertEqual(caught.exception.code, "GROUP_ID_INHERITANCE_INVALID")

        old_group_id = promoted_group.pop("group_id")
        regenerated = normalize_and_validate_mapping(
            snapshot,
            promoted_request,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
            base_mapping=baseline["mapping"],
            target_mapping_revision=2,
        )
        new_group = next(
            group
            for group in regenerated["mapping"]["groups"]
            if group["display_name"] == "参考资料"
        )
        self.assertNotEqual(new_group["group_id"], old_group_id)

    def test_fallback_candidate_values_are_never_exposed_as_headers(self):
        snapshot = {
            "snapshot_sha256": "b" * 64,
            "sheets": [
                {
                    "sheet_id": "sheet_fallback",
                    "index": 0,
                    "name": "记录",
                    "candidate_participant_region": {
                        "start_column": 4,
                        "end_column": 5,
                        "header_row": 2,
                        "basis": [
                            "parallel_columns_with_shared_header_row_and_right_block_extension"
                        ],
                    },
                    "column_profiles": [
                        {
                            "column": 4,
                            "column_letter": "D",
                            "first_non_empty_row": 2,
                            "header_value": "SECRET_ANSWER_A",
                        },
                        {
                            "column": 5,
                            "column_letter": "E",
                            "first_non_empty_row": 2,
                            "header_value": "SECRET_ANSWER_B",
                        },
                    ],
                }
            ],
        }
        proposals = build_group_proposals(
            snapshot,
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            workbook_revision_id=WORKBOOK_ID,
        )
        participants = proposals["groups"][0]["participants"]
        self.assertEqual(
            [participant["participant_label"] for participant in participants],
            ["D", "E"],
        )
        self.assertTrue(
            all(
                not column["raw_header"]
                for participant in participants
                for column in participant["columns"]
            )
        )
        self.assertNotIn("SECRET_ANSWER", repr(proposals))

    def test_request_schema_rejects_extra_fields_and_invalid_bounds(self):
        request = _complete_request()
        request["raw_value"] = "private"
        with self.assertRaises(ValidationError):
            InterviewV2GroupMappingDraftRequest.model_validate(request)

        request = _complete_request()
        request["base_mapping_revision"] = False
        with self.assertRaises(ValidationError):
            InterviewV2GroupMappingDraftRequest.model_validate(request)

        request = _complete_request()
        request["groups"][0]["participants"][0]["participant_label"] = "\ud800"
        with self.assertRaises(ValidationError):
            InterviewV2GroupMappingDraftRequest.model_validate(request)

        request = _complete_request()
        request["change_kind"] = "undo"
        with self.assertRaises(ValidationError):
            InterviewV2GroupMappingDraftRequest.model_validate(request)

    def test_documented_request_aliases_are_accepted_but_core_keys_are_stable(self):
        request = _complete_request()
        request["groups"][0]["participants"][0]["participant_id"] = (
            "participant_" + "1" * 32
        )
        for group in request["groups"]:
            group["participant_bindings"] = group.pop("participants")
            for participant in group["participant_bindings"]:
                for column in participant["columns"]:
                    column["column"] = column.pop("column_index")

        validated = InterviewV2GroupMappingDraftRequest.model_validate(request)
        internal = validated.model_dump(mode="json")
        self.assertIn("participants", internal["groups"][0])
        self.assertEqual(
            internal["groups"][0]["participants"][0]["participant_id"],
            "participant_" + "1" * 32,
        )
        self.assertIn(
            "column_index",
            internal["groups"][0]["participants"][0]["columns"][0],
        )

        request = _complete_request()
        request["groups"][0]["participants"][0]["columns"][0]["column_index"] = 257
        with self.assertRaises(ValidationError):
            InterviewV2GroupMappingDraftRequest.model_validate(request)


if __name__ == "__main__":
    unittest.main()
