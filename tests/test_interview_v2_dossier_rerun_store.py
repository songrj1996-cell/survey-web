from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
PARTICIPANT = "participant_" + "2" * 32
OTHER = "participant_" + "3" * 32
BASE = "dossier_" + "4" * 32
OWNER = "email:owner@example.com"
SOURCE = {
    "structure_revision_id": "structure_" + "5" * 32,
    "evidence_revision_id": "evidence_" + "6" * 32,
    "boundary_revision_id": "boundary_" + "7" * 32,
    "boundary_payload_sha256": "8" * 64,
    "coverage_revision_id": "coverage_" + "9" * 32,
    "coverage_payload_sha256": "a" * 64,
}


class DossierRerunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iv2-dossier-rerun-")
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(store.config, "INTERVIEW_V2_DATA_DIR", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        state = {"is_stale": False}
        for state_field, source_field in (
            ("current_structure_revision_id", "structure_revision_id"),
            ("current_evidence_revision_id", "evidence_revision_id"),
            ("current_boundary_revision_id", "boundary_revision_id"),
            ("current_boundary_payload_sha256", "boundary_payload_sha256"),
            ("current_coverage_revision_id", "coverage_revision_id"),
            ("current_coverage_payload_sha256", "coverage_payload_sha256"),
        ):
            state[state_field] = SOURCE[source_field]
        store._atomic_write_json(store._analysis_boundary_state_path(PROJECT), state)
        self.base = store.save_participant_dossier_cas(
            project_id=PROJECT, participant_id=PARTICIPANT,
            base_dossier_version_id=None,
            revision={
                "dossier_version_id": BASE, "import_id": "import_" + "b" * 32,
                "source": SOURCE, "attributes": {"facts": []},
                "dossier": {"claims": []}, "status": "approved",
                "review": {"decision": "approved"},
                "created_at": "2026-09-07T00:00:00Z",
            },
        )["revision"]
        store.save_participant_dossier_cas(
            project_id=PROJECT, participant_id=OTHER,
            base_dossier_version_id=None,
            revision={
                "dossier_version_id": "dossier_" + "c" * 32,
                "source": SOURCE, "attributes": {}, "dossier": {},
                "status": "approved", "created_at": "2026-09-07T00:00:00Z",
            },
        )
        self.args = {
            "owner_key": OWNER, "project_id": PROJECT,
            "idempotency_key": "dossier-key",
            "request_fingerprint": "d" * 64,
            "base_dossier_version_id": BASE,
            "base_revision_payload_sha256": self.base["revision_payload_sha256"],
            "participant_id": PARTICIPANT, "source": SOURCE,
            "frozen_participant_input": {"participant_id": PARTICIPANT},
            "prompt_snapshot": {"attribute": {"sha256": "e" * 64}},
            "model_configuration": {"attribute": {"models": ["model"]}},
            "created_at": "2026-09-07T00:01:00Z",
        }

    def revision(self, operation):
        return {
            "dossier_version_id": operation["dossier_version_id"],
            "import_id": "import_" + "b" * 32,
            "source": SOURCE, "attributes": {"facts": []},
            "dossier": {"claims": []}, "status": "generated", "review": {},
            "created_at": "2026-09-07T00:02:00Z",
            "rerun": {
                "rerun_id": operation["rerun_id"],
                "base_dossier_version_id": BASE,
                "input_fingerprint": self.args["request_fingerprint"],
            },
        }

    def operation_args(self):
        return {key: self.args[key] for key in (
            "owner_key", "project_id", "idempotency_key", "request_fingerprint"
        )}

    def save(self, operation):
        return store.save_participant_dossier_rerun_cas(
            **self.operation_args(), revision=self.revision(operation)
        )

    def test_claim_pending_conflict_and_owner_isolation(self):
        first = store.claim_participant_dossier_rerun(**self.args)
        self.assertTrue(first["_claim_acquired"])
        self.assertFalse(store.claim_participant_dossier_rerun(**self.args)["_claim_acquired"])
        with self.assertRaises(store.DossierRerunIdempotencyConflictError):
            store.claim_participant_dossier_rerun(
                **{**self.args, "request_fingerprint": "f" * 64}
            )
        other = store.claim_participant_dossier_rerun(
            **{**self.args, "owner_key": "email:other@example.com"}
        )
        self.assertNotEqual(first["dossier_version_id"], other["dossier_version_id"])

    def test_publish_replay_preserves_other_participant_and_old_approval(self):
        other_before = deepcopy(store.load_current_participant_dossier(PROJECT, OTHER))
        operation = store.claim_participant_dossier_rerun(**self.args)
        saved = self.save(operation)
        self.assertEqual("generated", saved["revision"]["status"])
        self.assertEqual({}, saved["revision"]["review"])
        self.assertEqual(other_before, store.load_current_participant_dossier(PROJECT, OTHER))
        replay = store.claim_participant_dossier_rerun(**self.args)
        self.assertEqual("completed", replay["status"])
        self.assertEqual("approved", store.load_participant_dossier(PROJECT, PARTICIPANT, BASE)["revision"]["status"])

    def test_head_digest_or_source_change_blocks_commit(self):
        operation = store.claim_participant_dossier_rerun(**self.args)
        state_path = store._analysis_boundary_state_path(PROJECT)
        state = store._read_json(state_path)
        store._atomic_write_json(state_path, {**state, "is_stale": True})
        with self.assertRaisesRegex(ValueError, "dossier input changed"):
            self.save(operation)
        store._atomic_write_json(state_path, state)
        reviewed = store.review_participant_dossier_cas(
            project_id=PROJECT, participant_id=PARTICIPANT,
            base_dossier_version_id=BASE, decision="needs_changes", note="moved",
            actor="owner", reviewed_at="2026-09-07T00:03:00Z",
        )
        with self.assertRaisesRegex(ValueError, "version conflict"):
            self.save(operation)
        self.assertEqual(reviewed["revision"]["dossier_version_id"], store.load_current_participant_dossier(PROJECT, PARTICIPANT)["revision"]["dossier_version_id"])

    def test_committed_result_recovers_after_completion_marker_failure(self):
        operation = store.claim_participant_dossier_rerun(**self.args)
        original = store._atomic_write_json

        def fail_completion(path, payload):
            if payload.get("operation_schema_version") and payload.get("status") == "completed":
                raise OSError("injected")
            return original(path, payload)

        with patch.object(store, "_atomic_write_json", side_effect=fail_completion), self.assertRaises(OSError):
            self.save(operation)
        self.assertFalse(store.release_participant_dossier_rerun(
            **self.operation_args(), dossier_version_id=operation["dossier_version_id"]
        ))
        self.assertEqual("completed", store.claim_participant_dossier_rerun(**self.args)["status"])

    def test_orphan_version_is_not_replayed(self):
        operation = store.claim_participant_dossier_rerun(**self.args)
        original = store._atomic_write_json

        def fail_head(path, payload):
            if payload.get("current_dossier_version_id") == operation["dossier_version_id"]:
                raise OSError("injected")
            return original(path, payload)

        with patch.object(store, "_atomic_write_json", side_effect=fail_head), self.assertRaises(OSError):
            self.save(operation)
        self.assertIsNone(store.load_participant_dossier(PROJECT, PARTICIPANT, operation["dossier_version_id"]))
        self.assertEqual("pending", store.claim_participant_dossier_rerun(**self.args)["status"])

    def test_missing_state_is_not_found_but_malformed_history_stays_invalid(self):
        empty_participant = "participant_" + "5" * 32
        self.assertIsNone(store.load_participant_dossier(PROJECT, empty_participant, BASE))
        directory = store._dossier_participant_dir(PROJECT, empty_participant)
        store._atomic_write_json(directory / "state.json", {})
        with self.assertRaisesRegex(ValueError, "history is invalid"):
            store.load_participant_dossier(PROJECT, empty_participant, BASE)


if __name__ == "__main__":
    unittest.main()
