from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.core import interview_v2_analysis as core
from app.core.security import _owner_from_login
from app.storage import interview_v2_store as store
from tests import test_interview_v2_analysis_rerun as f


def seed_project():
    ready = f.ready_fixture()
    project_dir = store._root() / "projects" / f.PROJECT
    project_dir.mkdir(parents=True, exist_ok=True)
    store._atomic_write_json(project_dir / "project.json", {
        "project_id": f.PROJECT, "import_id": f.fixtures.IMPORT, **_owner_from_login(f.LOGIN),
    })
    source = ready[4]
    store._atomic_write_json(store._analysis_boundary_state_path(f.PROJECT), {
        "current_structure_revision_id": source["structure_revision_id"],
        "current_evidence_revision_id": source["evidence_revision_id"],
        "current_boundary_revision_id": source["boundary_revision_id"],
        "current_boundary_payload_sha256": source["boundary_payload_sha256"],
        "current_coverage_revision_id": source["coverage_revision_id"],
        "current_coverage_payload_sha256": source["coverage_payload_sha256"], "is_stale": False,
    })
    dossiers = []
    for pid in (f.fixtures.P1, f.fixtures.P2):
        revision = f.fixtures.current_dossier(pid, "approved")["revision"]
        dossiers.append(store.save_participant_dossier_cas(
            project_id=f.PROJECT, participant_id=pid, base_dossier_version_id=None, revision=revision,
        )["revision"])
    ready, inputs, base = f.analysis_fixture(ready, dossiers)
    saved = store.save_analysis_run_cas(project_id=f.PROJECT, base_analysis_run_id=None, revision=base)
    return ready, inputs, saved["revision"]


def claim_args(base, module_id=f.MODULE_A, key="module-key", owner=None):
    return {
        "owner_key": owner or _owner_from_login(f.LOGIN)["owner_key"], "project_id": f.PROJECT,
        "idempotency_key": key,
        "request_fingerprint": core.analysis_module_rerun_fingerprint(
            base_revision=base, module_id=module_id, prompt_snapshot=f.PROMPTS, model_configuration=f.MODELS,
        ),
        "base_analysis_run_id": base["analysis_run_id"],
        "base_revision_payload_sha256": base["revision_payload_sha256"],
        "module_id": module_id, "created_at": "2026-09-06T01:00:00Z",
    }


def replacement(base, inputs, operation):
    module = next(m for m in inputs["modules"] if m["module_id"] == operation["module_id"])
    result = core.validate_module_findings(f.raw_output(module, "新发现"), module_input=module, analysis_run_id=operation["analysis_run_id"])
    revision = deepcopy(base)
    revision.update(core.merge_analysis_module_result(base_revision=base, module_id=operation["module_id"], result=result, analysis_run_id=operation["analysis_run_id"]))
    revision.update({
        "analysis_run_id": operation["analysis_run_id"], "created_at": "2026-09-06T01:00:01Z",
        "rerun": {
            "rerun_id": operation["rerun_id"], "module_id": operation["module_id"],
            "input_fingerprint": operation["request_fingerprint"], "base_analysis_run_id": base["analysis_run_id"],
        },
    })
    return revision


def operation_args(args):
    return {key: args[key] for key in ("owner_key", "project_id", "idempotency_key", "request_fingerprint")}


class AnalysisModuleRerunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iv2-module-")
        self.addCleanup(self.temp.cleanup)
        patcher = patch.object(store.config, "INTERVIEW_V2_DATA_DIR", Path(self.temp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ready, self.inputs, self.base = seed_project()
        self.args = claim_args(self.base)

    def save(self, operation, revision=None):
        return store.save_analysis_module_rerun_cas(
            **operation_args(self.args), revision=revision or replacement(self.base, self.inputs, operation),
        )

    def test_claim_pending_conflict_and_owner_isolation(self):
        first = store.claim_analysis_module_rerun(**self.args)
        self.assertTrue(first["_claim_acquired"])
        self.assertFalse(store.claim_analysis_module_rerun(**self.args)["_claim_acquired"])
        with self.assertRaisesRegex(
            store.AnalysisRerunIdempotencyConflictError,
            r"^analysis rerun idempotency conflict$",
        ):
            store.claim_analysis_module_rerun(**claim_args(self.base, f.MODULE_B))
        second = store.claim_analysis_module_rerun(**claim_args(self.base, owner="other-owner"))
        self.assertNotEqual(first["analysis_run_id"], second["analysis_run_id"])

    def test_publish_replay_history_and_release_protection(self):
        operation = store.claim_analysis_module_rerun(**self.args)
        saved = self.save(operation)
        self.assertEqual(2, saved["revision"]["version_number"])
        completed = store.claim_analysis_module_rerun(**self.args)
        self.assertEqual("completed", completed["status"])
        self.assertFalse(store.release_analysis_module_rerun(**operation_args(self.args), analysis_run_id=operation["analysis_run_id"]))
        self.assertEqual(self.base, store.load_analysis_run(f.PROJECT, f.BASE)["revision"])

    def test_storage_rejects_non_target_content_or_statistics_changes(self):
        operation = store.claim_analysis_module_rerun(**self.args)
        for field in ("content", "number", "provenance"):
            revision = replacement(self.base, self.inputs, operation)
            if field == "content":
                next(x for x in revision["findings"] if x["module_id"] == f.MODULE_B)["statement"] = "bad"
            elif field == "number":
                revision["stat_facts"][-1]["numerator"] = 55
            else:
                revision["model_usage"]["modules"][-1]["model"] = "changed"
            with self.assertRaises(ValueError):
                self.save(operation, revision)
        self.assertEqual(f.BASE, store.load_current_analysis_run(f.PROJECT)["revision"]["analysis_run_id"])

    def test_head_or_upstream_change_blocks_publication(self):
        operation = store.claim_analysis_module_rerun(**self.args)
        state_path = store._analysis_boundary_state_path(f.PROJECT)
        state = store._read_json(state_path)
        store._atomic_write_json(state_path, {**state, "is_stale": True})
        with self.assertRaisesRegex(ValueError, "analysis input changed"):
            self.save(operation)
        store._atomic_write_json(state_path, state)
        store.save_analysis_run_cas(project_id=f.PROJECT, base_analysis_run_id=f.BASE, revision={**self.base, "analysis_run_id": f.NEXT})
        with self.assertRaisesRegex(ValueError, "analysis version conflict"):
            self.save(operation)

    def test_committed_result_recovers_after_operation_write_failure(self):
        operation = store.claim_analysis_module_rerun(**self.args)
        original = store._atomic_write_json

        def fail_completion(path, payload):
            if "operation_schema_version" in payload and payload.get("status") == "completed":
                raise OSError("injected completion write failure")
            return original(path, payload)

        with patch.object(store, "_atomic_write_json", side_effect=fail_completion):
            with self.assertRaises(OSError):
                self.save(operation)
        self.assertFalse(store.release_analysis_module_rerun(**operation_args(self.args), analysis_run_id=operation["analysis_run_id"]))
        self.assertEqual("completed", store.claim_analysis_module_rerun(**self.args)["status"])
        self.assertEqual(2, store.load_current_analysis_run(f.PROJECT)["state"]["current_version_number"])

    def test_orphan_version_is_not_replayed_and_retry_gets_new_identity(self):
        operation = store.claim_analysis_module_rerun(**self.args)
        original = store._atomic_write_json

        def fail_head(path, payload):
            if payload.get("current_analysis_run_id") == operation["analysis_run_id"]:
                raise OSError("injected head write failure")
            return original(path, payload)

        with patch.object(store, "_atomic_write_json", side_effect=fail_head), self.assertRaises(OSError):
            self.save(operation)
        self.assertIsNone(store.load_analysis_run(f.PROJECT, operation["analysis_run_id"]))
        self.assertEqual("pending", store.claim_analysis_module_rerun(**self.args)["status"])
        self.assertTrue(store.release_analysis_module_rerun(**operation_args(self.args), analysis_run_id=operation["analysis_run_id"]))
        retry = store.claim_analysis_module_rerun(**self.args)
        self.assertNotEqual(operation["analysis_run_id"], retry["analysis_run_id"])
        self.assertEqual("completed", self.save(retry)["operation"]["status"])

    def test_corrupt_operation_and_committed_result_fail_closed(self):
        operation = store.claim_analysis_module_rerun(**self.args)
        path = store._analysis_rerun_operation_path(self.args["owner_key"], f.PROJECT, self.args["idempotency_key"])
        record = store._read_json(path)
        store._atomic_write_json(path, {**record, "module_id": f.MODULE_B})
        with self.assertRaises(ValueError):
            store.claim_analysis_module_rerun(**self.args)
        store._atomic_write_json(path, record)
        saved = self.save(operation)
        version_path = store._analysis_dir(f.PROJECT) / "versions" / f"{operation['analysis_run_id']}.json"
        store._atomic_write_json(version_path, {**saved["revision"], "limitations": []})
        with self.assertRaises(ValueError):
            store.claim_analysis_module_rerun(**self.args)

    def test_idempotency_namespace_is_shared_with_report_sections(self):
        report_args = {
            "owner_key": self.args["owner_key"], "project_id": f.PROJECT,
            "idempotency_key": "section-first", "request_fingerprint": "1" * 64,
            "rerun_id": "rerun_" + "1" * 32, "report_version_id": "report_" + "1" * 32,
            "base_report_version_id": "report_" + "2" * 32, "section_id": "section_" + "1" * 32,
            "base_section_revision": 1, "created_at": self.args["created_at"],
        }
        store.claim_report_rerun_operation(**report_args)
        with self.assertRaisesRegex(
            store.AnalysisRerunIdempotencyConflictError,
            r"^analysis rerun idempotency conflict$",
        ):
            store.claim_analysis_module_rerun(**{**self.args, "idempotency_key": "section-first"})
        store.claim_analysis_module_rerun(**self.args)
        with self.assertRaisesRegex(
            store.ReportRerunIdempotencyConflictError,
            r"^report rerun idempotency conflict$",
        ):
            store.claim_report_rerun_operation(**{**report_args, "idempotency_key": self.args["idempotency_key"]})

        dossier = store.load_current_participant_dossier(f.PROJECT, f.fixtures.P1)["revision"]
        dossier_args = {
            "owner_key": self.args["owner_key"], "project_id": f.PROJECT,
            "idempotency_key": "dossier-first", "request_fingerprint": "2" * 64,
            "base_dossier_version_id": dossier["dossier_version_id"],
            "base_revision_payload_sha256": dossier["revision_payload_sha256"],
            "participant_id": f.fixtures.P1, "source": dossier["source"],
            "frozen_participant_input": {"participant_id": f.fixtures.P1},
            "prompt_snapshot": {}, "model_configuration": {},
            "created_at": self.args["created_at"],
        }
        store.claim_participant_dossier_rerun(**dossier_args)
        with self.assertRaises(store.AnalysisRerunIdempotencyConflictError):
            store.claim_analysis_module_rerun(**{
                **self.args, "idempotency_key": "dossier-first"
            })
