import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import interview_v2_mapping_service as service
from app.storage import interview_v2_store as store


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
WORKBOOK_ID = "workbook_" + "3" * 32
OWNER = {"email": "owner@example.com", "name": "Owner"}
OTHER = {"email": "other@example.com", "name": "Other"}


def _snapshot() -> dict:
    snapshot = {
        "schema_version": "interview-workbook-physical-truth/1.0",
        "content_sha256": "c" * 64,
        "snapshot_sha256": "a" * 64,
        "sheets": [
            {
                "sheet_id": "sheet_001",
                "name": "1组记录1",
                "cells": [
                    {
                        "address": "D2",
                        "raw_value": "不得公开的玩家回答",
                    }
                ],
                "candidate_participant_region": {
                    "start_column": 4,
                    "end_column": 4,
                    "candidate_columns": ["D"],
                    "candidate_count": 1,
                },
                "column_profiles": [
                    {
                        "column": 4,
                        "column_letter": "D",
                        "header_value": "P01",
                    }
                ],
            }
        ],
    }
    snapshot["snapshot_sha256"] = store.physical_snapshot_sha256(snapshot)
    return snapshot


def _normalized(*, ready: bool = True) -> dict:
    return {
        "mapping": {
            "groups": [
                {
                    "group_id": "group_01",
                    "display_name": "第1组",
                    "participant_bindings": [
                        {
                            "participant_label": "P01",
                            "raw_header": "P01",
                            "columns": [
                                {"sheet_id": "sheet_001", "column": 4}
                            ],
                            "raw_value": "不得公开的玩家回答",
                        }
                    ],
                }
            ],
            "cells": [{"raw_value": "不得公开"}],
            "source_filename": "source.xlsx",
        },
        "issues": [] if ready else [{"code": "PARTICIPANT_MAPPING_AMBIGUOUS"}],
        "confirmation_ready": ready,
        "final_participant_preview": {
            "participants": [
                {
                    "participant_id": "group_01-P01",
                    "raw_header": "P01",
                    "normalized_text": "不得公开",
                }
            ]
        },
    }


def _cross_group_snapshot() -> dict:
    snapshot = {
        "schema_version": "interview-workbook-physical-truth/1.0",
        "content_sha256": "c" * 64,
        "snapshot_sha256": "a" * 64,
        "sheets": [
            {
                "sheet_id": sheet_id,
                "index": index,
                "name": f"Group {index + 1}",
                "candidate_participant_region": {
                    "start_column": 4,
                    "end_column": 4,
                },
                "column_profiles": [
                    {
                        "column": 4,
                        "column_letter": "D",
                        "header_value": "P01",
                    }
                ],
                "cells": [{"address": "D2", "raw_value": "private answer"}],
            }
            for index, sheet_id in enumerate(("sheet_001", "sheet_002"))
        ],
    }
    snapshot["snapshot_sha256"] = store.physical_snapshot_sha256(snapshot)
    return snapshot


def _cross_group_request() -> dict:
    return {
        "base_mapping_revision": 0,
        "groups": [
            {
                "display_name": "Group 1",
                "sheets": [
                    {
                        "sheet_id": "sheet_001",
                        "role": "record",
                        "recorder_label": "Recorder 1",
                    }
                ],
                "participants": [
                    {
                        "participant_label": "P01",
                        "columns": [
                            {"sheet_id": "sheet_002", "column_index": 4}
                        ],
                    }
                ],
            },
            {
                "display_name": "Group 2",
                "sheets": [
                    {
                        "sheet_id": "sheet_002",
                        "role": "record",
                        "recorder_label": "Recorder 2",
                    }
                ],
                "participants": [],
            },
        ],
        "ignored_sheet_ids": [],
        "change_kind": "manual_edit",
        "change_reason": "cross-group regression",
    }


class InterviewV2MappingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="interview-v2-mapping-")
        self.patches = [
            patch.object(
                store.config,
                "INTERVIEW_V2_DATA_DIR",
                Path(self.temp_dir.name) / "interview_v2",
            ),
            patch("app.core.security.FEISHU_LOGIN_REQUIRED", True),
        ]
        for item in self.patches:
            item.start()
        owner = {
            "owner_key": "email:owner@example.com",
            "owner_email": "owner@example.com",
            "owner_open_id": "",
            "owner_name": "Owner",
        }
        snapshot = _snapshot()
        store.publish_accepted_bundle(
            project={
                "project_id": PROJECT_ID,
                "status": "GROUP_CONFIRMATION_REQUIRED",
                "current_workbook_revision_id": WORKBOOK_ID,
                "current_import_id": IMPORT_ID,
                **owner,
            },
            workbook_revision={
                "workbook_revision_id": WORKBOOK_ID,
                "project_id": PROJECT_ID,
                "content_sha256": snapshot["content_sha256"],
                "physical_snapshot_version": snapshot["schema_version"],
                "snapshot_sha256": snapshot["snapshot_sha256"],
                **owner,
            },
            interview_import={
                "import_id": IMPORT_ID,
                "project_id": PROJECT_ID,
                "workbook_revision_id": WORKBOOK_ID,
                "status": "GROUP_CONFIRMATION_REQUIRED",
                "physical_snapshot_version": snapshot["schema_version"],
                **owner,
            },
            source_content=b"xlsx",
            physical_snapshot=snapshot,
        )

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _request(base: int = 0) -> dict:
        return {
            "base_mapping_revision": base,
            "groups": [],
            "ignored_sheet_ids": [],
            "change_kind": "manual_edit",
            "change_reason": "校对 Sheet 分组",
        }

    def _replace_snapshot(self, snapshot: dict) -> None:
        snapshot["snapshot_sha256"] = store.physical_snapshot_sha256(snapshot)
        project_root = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
        )
        workbook_root = project_root / "workbook_revisions" / WORKBOOK_ID
        metadata_path = workbook_root / "metadata.json"
        metadata = store._read_json(metadata_path)
        metadata["snapshot_sha256"] = snapshot["snapshot_sha256"]
        metadata["content_sha256"] = snapshot["content_sha256"]
        metadata["physical_snapshot_version"] = snapshot["schema_version"]
        store._atomic_write_json(metadata_path, metadata)
        store._atomic_write_json(workbook_root / "physical_snapshot.json", snapshot)

    def test_owner_isolation_and_group_proposals_are_redacted(self):
        with (
            patch.object(store, "load_mapping_input_bundle") as load_bundle,
            self.assertRaises(service.InterviewV2ImportError) as caught,
        ):
            service.get_group_proposals(IMPORT_ID, OTHER)
        self.assertEqual(caught.exception.code, "INTERVIEW_IMPORT_NOT_FOUND")
        load_bundle.assert_not_called()

        proposal = {
            "groups": [{"raw_header": "P01", "raw_value": "不得公开"}],
            "cells": [{"display_value": "不得公开"}],
            "source_filename": "source.xlsx",
        }
        with patch.object(service, "build_group_proposals", return_value=proposal):
            result = service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(result["revision_number"], 0)
        self.assertIn("raw_header", repr(result))
        self.assertNotIn("不得公开", repr(result))
        self.assertNotIn("cells", repr(result))
        self.assertNotIn("source.xlsx", repr(result))

    def test_save_is_cas_versioned_and_response_loss_retry_is_idempotent(self):
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            first = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
            retried = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)

        self.assertEqual(first["revision_number"], 1)
        self.assertEqual(first["mapping_revision_id"], retried["mapping_revision_id"])
        self.assertEqual(first["mapping_sha256"], retried["mapping_sha256"])
        self.assertEqual(len(retried["history"]), 1)
        self.assertEqual(retried["status"], "GROUP_CONFIRMATION_REQUIRED")
        self.assertNotIn("不得公开", repr(retried))
        revision = store.load_mapping_revision(
            PROJECT_ID, first["mapping_revision_id"]
        )
        self.assertNotIn("不得公开", repr(revision))
        self.assertEqual(revision["change_reason"], "校对 Sheet 分组")
        self.assertNotIn("change_reason", repr(retried["history"]))

        changed_reason = self._request()
        changed_reason["change_reason"] = "different audit action"
        with (
            patch.object(
                service, "normalize_and_validate_mapping", return_value=_normalized()
            ),
            self.assertRaises(service.InterviewV2ImportError) as caught,
        ):
            service.save_group_mapping(IMPORT_ID, changed_reason, OWNER)
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")

        invalid_kind = self._request(base=1)
        invalid_kind["change_kind"] = "undo"
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.save_group_mapping(IMPORT_ID, invalid_kind, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_REQUEST_INVALID")

        changed = _normalized()
        changed["mapping"]["groups"][0]["display_name"] = "另一版本"
        with (
            patch.object(
                service, "normalize_and_validate_mapping", return_value=changed
            ),
            self.assertRaises(service.InterviewV2ImportError) as caught,
        ):
            service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")
        self.assertEqual(
            set(caught.exception.context),
            {
                "current_revision_number",
                "current_mapping_revision_id",
                "current_mapping_sha256",
            },
        )

        replay_with_different_audit = self._request()
        replay_with_different_audit["change_reason"] = "另一项人工操作"
        with (
            patch.object(
                service, "normalize_and_validate_mapping", return_value=_normalized()
            ),
            self.assertRaises(service.InterviewV2ImportError) as caught,
        ):
            service.save_group_mapping(
                IMPORT_ID, replay_with_different_audit, OWNER
            )
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")

    def test_revision_write_then_state_failure_recovers_deterministically(self):
        real_write = store._atomic_write_json
        failed = {"done": False}

        def fail_state_once(path, value):
            if path.name == "mapping_state.json" and not failed["done"]:
                failed["done"] = True
                raise OSError("simulated state write failure")
            return real_write(path, value)

        with (
            patch.object(
                service, "normalize_and_validate_mapping", return_value=_normalized()
            ),
            patch.object(store, "_atomic_write_json", side_effect=fail_state_once),
            self.assertRaises(service.InterviewV2ImportError),
        ):
            service.save_group_mapping(IMPORT_ID, self._request(), OWNER)

        revision_files = list(
            (
                Path(self.temp_dir.name)
                / "interview_v2"
                / "projects"
                / PROJECT_ID
                / "mapping_revisions"
            ).glob("mapping_*.json")
        )
        self.assertEqual(len(revision_files), 1)
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            recovered = service.save_group_mapping(
                IMPORT_ID, self._request(), OWNER
            )
        self.assertEqual(recovered["revision_number"], 1)
        self.assertEqual(recovered["mapping_revision_id"], revision_files[0].stem)
        self.assertEqual(len(recovered["history"]), 1)

    def test_confirmation_gate_checkpoint_and_repeat_are_idempotent(self):
        blocked = _normalized(ready=False)
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=blocked
        ):
            saved = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.confirm_group_mapping(
                IMPORT_ID,
                {
                    "base_mapping_revision": 1,
                    "mapping_sha256": saved["mapping_sha256"],
                },
                OWNER,
            )
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(caught.exception.code, "GROUP_MAPPING_CONFIRMATION_REQUIRED")

        ready_request = self._request(base=1)
        ready_request["change_reason"] = "补齐玩家绑定"
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            ready = service.save_group_mapping(IMPORT_ID, ready_request, OWNER)
        payload = {
            "base_mapping_revision": 2,
            "mapping_sha256": ready["mapping_sha256"],
        }
        with patch.object(
            store,
            "load_mapping_input_bundle",
            side_effect=AssertionError("confirmation must not load the workbook snapshot"),
        ):
            confirmed = service.confirm_group_mapping(IMPORT_ID, payload, OWNER)
            confirmed_again = service.confirm_group_mapping(IMPORT_ID, payload, OWNER)
        self.assertEqual(confirmed["status"], "GROUP_MAPPING_CONFIRMED")
        self.assertEqual(confirmed, confirmed_again)
        state = store.load_mapping_state(PROJECT_ID)
        self.assertEqual(len(state["confirmation_events"]), 1)
        with patch.object(
            store,
            "load_mapping_input_bundle",
            side_effect=AssertionError("status polling must not load the workbook snapshot"),
        ):
            public_import = service.get_interview_import_with_mapping_status(
                IMPORT_ID, OWNER
            )
        self.assertEqual(public_import["status"], "GROUP_MAPPING_CONFIRMED")

        third_request = self._request(base=2)
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            third = service.save_group_mapping(IMPORT_ID, third_request, OWNER)
        self.assertEqual(third["status"], "GROUP_CONFIRMATION_REQUIRED")
        self.assertTrue(third["history"][1]["confirmed"])
        state = store.load_mapping_state(PROJECT_ID)
        self.assertEqual(len(state["confirmation_events"]), 1)
        self.assertIsNone(state["confirmed_mapping_revision_id"])
        self.assertIsNone(state["confirmed_mapping_sha256"])
        self.assertIsNone(state["confirmed_mapping_revision_number"])

    def test_restore_historical_mapping_appends_audited_revision(self):
        first_value = _normalized()
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=first_value
        ):
            first = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        second_value = _normalized()
        second_value["mapping"]["groups"][0]["display_name"] = "第二版"
        second_request = self._request(base=1)
        second_request["change_reason"] = "修改分组名"
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=second_value
        ):
            second = service.save_group_mapping(IMPORT_ID, second_request, OWNER)

        payload = {
            "base_mapping_revision": 2,
            "target_mapping_revision_id": first["mapping_revision_id"],
            "target_mapping_sha256": first["mapping_sha256"],
            "change_kind": "undo",
            "change_reason": "撤销分组名修改",
        }
        restored = service.restore_group_mapping(IMPORT_ID, payload, OWNER)
        restored_again = service.restore_group_mapping(IMPORT_ID, payload, OWNER)
        self.assertEqual(restored, restored_again)
        self.assertEqual(restored["revision_number"], 3)
        self.assertEqual(restored["mapping"], first["mapping"])
        self.assertEqual(restored["status"], "GROUP_CONFIRMATION_REQUIRED")
        self.assertNotEqual(
            restored["mapping_revision_id"], first["mapping_revision_id"]
        )
        history = restored["history"][-1]
        self.assertEqual(history["change_kind"], "undo")
        self.assertNotIn("change_reason", history)
        self.assertEqual(
            history["restored_from_mapping_revision_id"],
            first["mapping_revision_id"],
        )
        self.assertEqual(history["restored_from_revision_number"], 1)
        revision = store.load_mapping_revision(
            PROJECT_ID, restored["mapping_revision_id"]
        )
        self.assertNotIn("owner@example.com", repr(restored))
        self.assertIn("email:owner@example.com", repr(revision))

        confirmed = service.confirm_group_mapping(
            IMPORT_ID,
            {
                "base_mapping_revision": 3,
                "mapping_sha256": restored["mapping_sha256"],
            },
            OWNER,
        )
        self.assertEqual(confirmed["status"], "GROUP_MAPPING_CONFIRMED")
        self.assertNotEqual(second["mapping_sha256"], restored["mapping_sha256"])

    def test_restore_rejects_foreign_target_and_stale_base(self):
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            first = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        invalid = {
            "base_mapping_revision": 1,
            "target_mapping_revision_id": "mapping_" + "f" * 32,
            "target_mapping_sha256": "f" * 64,
            "change_kind": "restore",
            "change_reason": "尝试伪造历史",
        }
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.restore_group_mapping(IMPORT_ID, invalid, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_RESTORE_TARGET_INVALID")

        second_request = self._request(base=1)
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            service.save_group_mapping(IMPORT_ID, second_request, OWNER)
        stale = {
            "base_mapping_revision": 1,
            "target_mapping_revision_id": first["mapping_revision_id"],
            "target_mapping_sha256": first["mapping_sha256"],
            "change_kind": "undo",
            "change_reason": "过期撤销",
        }
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.restore_group_mapping(IMPORT_ID, stale, OWNER)
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")

    def test_snapshot_integrity_mismatch_is_blocked(self):
        metadata_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "workbook_revisions"
            / WORKBOOK_ID
            / "metadata.json"
        )
        metadata = store._read_json(metadata_path)
        metadata["snapshot_sha256"] = "b" * 64
        store._atomic_write_json(metadata_path, metadata)
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_INPUT_UNAVAILABLE")

        metadata["content_sha256"] = "d" * 64
        metadata["physical_snapshot_version"] = "unexpected-version"
        store._atomic_write_json(metadata_path, metadata)
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_INPUT_UNAVAILABLE")

    def test_snapshot_content_and_version_mismatches_are_blocked(self):
        project_root = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
        )
        metadata_path = (
            project_root
            / "workbook_revisions"
            / WORKBOOK_ID
            / "metadata.json"
        )
        snapshot_path = (
            project_root
            / "workbook_revisions"
            / WORKBOOK_ID
            / "physical_snapshot.json"
        )
        metadata = store._read_json(metadata_path)
        snapshot = store._read_json(snapshot_path)
        metadata["content_sha256"] = "c" * 64
        snapshot["content_sha256"] = "d" * 64
        store._atomic_write_json(metadata_path, metadata)
        store._atomic_write_json(snapshot_path, snapshot)
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_INPUT_UNAVAILABLE")

    def test_snapshot_content_tampering_and_missing_contract_fields_are_blocked(self):
        project_root = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
        )
        workbook_root = project_root / "workbook_revisions" / WORKBOOK_ID
        metadata_path = workbook_root / "metadata.json"
        snapshot_path = workbook_root / "physical_snapshot.json"
        import_path = project_root / "imports" / f"{IMPORT_ID}.json"

        snapshot = store._read_json(snapshot_path)
        snapshot["sheets"][0]["name"] = "TAMPERED_GROUP_99"
        store._atomic_write_json(snapshot_path, snapshot)
        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_INPUT_UNAVAILABLE")

        original_snapshot = _snapshot()
        store._atomic_write_json(snapshot_path, original_snapshot)
        for path, field in (
            (metadata_path, "content_sha256"),
            (metadata_path, "physical_snapshot_version"),
            (import_path, "physical_snapshot_version"),
            (snapshot_path, "content_sha256"),
        ):
            value = store._read_json(path)
            original = value.pop(field)
            store._atomic_write_json(path, value)
            with self.assertRaises(service.InterviewV2ImportError) as caught:
                service.get_group_proposals(IMPORT_ID, OWNER)
            self.assertEqual(caught.exception.code, "MAPPING_INPUT_UNAVAILABLE")
            value[field] = original
            if path == snapshot_path:
                value["snapshot_sha256"] = store.physical_snapshot_sha256(value)
                metadata = store._read_json(metadata_path)
                metadata["snapshot_sha256"] = value["snapshot_sha256"]
                store._atomic_write_json(metadata_path, metadata)
            store._atomic_write_json(path, value)

    def test_actual_core_cross_group_error_is_a_redacted_422(self):
        self._replace_snapshot(_cross_group_snapshot())

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.save_group_mapping(IMPORT_ID, _cross_group_request(), OWNER)
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(
            caught.exception.code, "CROSS_GROUP_PARTICIPANT_MERGE_ATTEMPT"
        )
        self.assertNotIn("private answer", repr(caught.exception.to_error_body()))
        self.assertIsNone(store.load_mapping_state(PROJECT_ID))

    def test_actual_core_invalid_proposal_input_returns_generic_500(self):
        snapshot = _cross_group_snapshot()
        snapshot["sheets"][1]["sheet_id"] = "sheet_001"
        self._replace_snapshot(snapshot)

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.code, "MAPPING_INPUT_INVALID")
        self.assertEqual(caught.exception.context, {})
        self.assertNotIn("private answer", repr(caught.exception.to_error_body()))

    def test_current_revision_integrity_mismatch_fails_closed(self):
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            saved = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        revision_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "mapping_revisions"
            / f"{saved['mapping_revision_id']}.json"
        )
        revision = store._read_json(revision_path)
        revision["mapping"]["groups"][0]["display_name"] = "tampered"
        store._atomic_write_json(revision_path, revision)

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_PERSISTENCE_FAILED")
        self.assertNotIn("tampered", repr(caught.exception.to_error_body()))

    def test_confirmation_gate_cannot_be_changed_outside_revision_cas(self):
        with patch.object(
            service,
            "normalize_and_validate_mapping",
            return_value=_normalized(ready=False),
        ):
            saved = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        revision_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "mapping_revisions"
            / f"{saved['mapping_revision_id']}.json"
        )
        revision = store._read_json(revision_path)
        revision["confirmation_ready"] = True
        store._atomic_write_json(revision_path, revision)

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.confirm_group_mapping(
                IMPORT_ID,
                {
                    "base_mapping_revision": 1,
                    "mapping_sha256": saved["mapping_sha256"],
                },
                OWNER,
            )
        self.assertEqual(caught.exception.code, "MAPPING_PERSISTENCE_FAILED")
        self.assertEqual(
            store.load_mapping_state(PROJECT_ID)["effective_status"],
            "GROUP_CONFIRMATION_REQUIRED",
        )

    def test_mapping_status_requires_a_matching_confirmation_event(self):
        with patch.object(
            service,
            "normalize_and_validate_mapping",
            return_value=_normalized(ready=False),
        ):
            service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        state_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "mapping_state.json"
        )
        state = store._read_json(state_path)
        state["effective_status"] = "GROUP_MAPPING_CONFIRMED"
        state["confirmed_mapping_revision_id"] = state[
            "current_mapping_revision_id"
        ]
        state["confirmed_mapping_sha256"] = state["current_mapping_sha256"]
        state["confirmed_mapping_revision_number"] = 1
        store._atomic_write_json(state_path, state)

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_interview_import_with_mapping_status(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_PERSISTENCE_FAILED")

    def test_mapping_history_audit_fields_are_bound_to_revision_payload(self):
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        state_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "mapping_state.json"
        )
        state = store._read_json(state_path)
        state["revision_history"][0]["change_kind"] = "undo"
        state["revision_history"][0]["change_reason"] = "forged audit"
        state["revision_history"][0]["restored_from_mapping_revision_id"] = (
            "mapping_" + "f" * 32
        )
        state["revision_history"][0]["restored_from_revision_number"] = 999
        store._atomic_write_json(state_path, state)

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_group_proposals(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_PERSISTENCE_FAILED")

    def test_confirmed_event_cannot_be_silently_downgraded(self):
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            saved = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        service.confirm_group_mapping(
            IMPORT_ID,
            {
                "base_mapping_revision": 1,
                "mapping_sha256": saved["mapping_sha256"],
            },
            OWNER,
        )
        state_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "mapping_state.json"
        )
        state = store._read_json(state_path)
        state["effective_status"] = "GROUP_CONFIRMATION_REQUIRED"
        store._atomic_write_json(state_path, state)

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_interview_import_with_mapping_status(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_PERSISTENCE_FAILED")

    def test_status_poll_validates_the_current_revision_file(self):
        with patch.object(
            service, "normalize_and_validate_mapping", return_value=_normalized()
        ):
            saved = service.save_group_mapping(IMPORT_ID, self._request(), OWNER)
        service.confirm_group_mapping(
            IMPORT_ID,
            {
                "base_mapping_revision": 1,
                "mapping_sha256": saved["mapping_sha256"],
            },
            OWNER,
        )
        revision_path = (
            Path(self.temp_dir.name)
            / "interview_v2"
            / "projects"
            / PROJECT_ID
            / "mapping_revisions"
            / f"{saved['mapping_revision_id']}.json"
        )
        revision_path.unlink()

        with self.assertRaises(service.InterviewV2ImportError) as caught:
            service.get_interview_import_with_mapping_status(IMPORT_ID, OWNER)
        self.assertEqual(caught.exception.code, "MAPPING_PERSISTENCE_FAILED")


if __name__ == "__main__":
    unittest.main()
