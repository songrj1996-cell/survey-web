from __future__ import annotations

import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.storage import interview_v2_store as store


PROJECT_ID = "project_" + "1" * 32
IMPORT_ID = "import_" + "2" * 32
STRUCTURE_ID_1 = "structure_" + "3" * 32
EVIDENCE_ID_1 = "evidence_" + "4" * 32
BOUNDARY_ID_1 = "boundary_" + "5" * 32
COVERAGE_ID_1 = "coverage_" + "6" * 32
STRUCTURE_ID_2 = "structure_" + "7" * 32
EVIDENCE_ID_2 = "evidence_" + "8" * 32
BOUNDARY_ID_2 = "boundary_" + "9" * 32
COVERAGE_ID_2 = "coverage_" + "a" * 32
REQUEST_1 = "b" * 64
REQUEST_2 = "c" * 64


class InterviewV2AnalysisBoundaryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="iv2-boundary-")
        self.root = Path(self.temp_dir.name) / "v2"
        self.config_patch = patch.object(
            store.config, "INTERVIEW_V2_DATA_DIR", self.root
        )
        self.config_patch.start()
        (self.root / "projects" / PROJECT_ID).mkdir(parents=True)
        self.current_upstream = {
            "source": self._source(),
            "status": "READY_FOR_DOSSIERS",
            "is_stale": False,
        }
        self.upstream_patch = patch.object(
            store,
            "_current_analysis_boundary_source_locked",
            side_effect=lambda _project_id, _import_id: deepcopy(
                self.current_upstream
            ),
        )
        self.upstream_patch.start()

    def tearDown(self):
        self.upstream_patch.stop()
        self.config_patch.stop()
        self.temp_dir.cleanup()

    @staticmethod
    def _source(
        *,
        structure_revision_id: str = STRUCTURE_ID_1,
        evidence_revision_id: str = EVIDENCE_ID_1,
        structure_digest: str = "d" * 64,
        evidence_digest: str = "e" * 64,
    ) -> dict:
        return {
            "structure_revision_id": structure_revision_id,
            "structure_payload_sha256": structure_digest,
            "evidence_revision_id": evidence_revision_id,
            "evidence_payload_sha256": evidence_digest,
        }

    def _revisions(
        self,
        *,
        revision_number: int = 1,
        boundary_revision_id: str = BOUNDARY_ID_1,
        coverage_revision_id: str = COVERAGE_ID_1,
        request_fingerprint: str = REQUEST_1,
        source: dict | None = None,
        created_at: str = "2026-08-20T00:00:00Z",
        boundary_status: str = "draft",
        coverage_boundary_sha256: str | None = None,
        coverage_rows: list[dict] | None = None,
    ) -> tuple[dict, dict]:
        frozen_source = deepcopy(source or self._source())
        analysis_boundary = {
            "status": boundary_status,
            "evaluation_objects": [
                    {
                        "evaluation_object_id": "evaluation_" + "f" * 32,
                        "name": "方案 A",
                        "decision_status": (
                            "confirmed"
                            if boundary_status == "confirmed"
                            else "draft"
                        ),
                }
            ],
            "source_scopes": [],
            "label_scopes": [],
        }
        boundary = {
            "schema_version": "interview-analysis-boundary-revision/1.0",
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "revision_number": revision_number,
            "boundary_revision_id": boundary_revision_id,
            "request_fingerprint": request_fingerprint,
            "source": frozen_source,
            "status": "draft",
            "analysis_boundary": analysis_boundary,
            "created_at": created_at,
            "created_by": "email:owner@example.com",
        }
        boundary["revision_payload_sha256"] = (
            store.analysis_boundary_revision_payload_sha256(boundary)
        )
        coverage = {
            "schema_version": "interview-coverage-revision/1.0",
            "project_id": PROJECT_ID,
            "import_id": IMPORT_ID,
            "revision_number": revision_number,
            "coverage_revision_id": coverage_revision_id,
            "boundary_revision_id": boundary_revision_id,
            "boundary_payload_sha256": boundary[
                "revision_payload_sha256"
            ],
            "request_fingerprint": request_fingerprint,
            "source": frozen_source,
            "coverage_preview": {
                "source": {
                    "analysis_boundary_sha256": (
                        coverage_boundary_sha256
                        or store._canonical_payload_sha256(analysis_boundary)
                    )
                },
                "rows": deepcopy(coverage_rows or []),
                "denominator_reliable": False,
                "proportion": None,
            },
            "created_at": created_at,
            "created_by": "email:owner@example.com",
        }
        coverage["revision_payload_sha256"] = (
            store.coverage_revision_payload_sha256(coverage)
        )
        return boundary, coverage

    def _save(
        self,
        *,
        base_boundary_revision_id: str | None = None,
        base_coverage_revision_id: str | None = None,
        revisions: tuple[dict, dict] | None = None,
        request_fingerprint: str = REQUEST_1,
        updated_at: str = "2026-08-20T00:00:00Z",
    ):
        boundary, coverage = revisions or self._revisions(
            request_fingerprint=request_fingerprint,
            created_at=updated_at,
        )
        return store.save_analysis_boundary_bundle_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            base_boundary_revision_id=base_boundary_revision_id,
            base_coverage_revision_id=base_coverage_revision_id,
            boundary_revision=boundary,
            coverage_revision=coverage,
            request_fingerprint=request_fingerprint,
            updated_at=updated_at,
        )

    def _confirm(self, state: dict):
        return store.confirm_analysis_boundary_cas(
            project_id=PROJECT_ID,
            import_id=IMPORT_ID,
            boundary_revision_id=state["current_boundary_revision_id"],
            coverage_revision_id=state["current_coverage_revision_id"],
            boundary_payload_sha256=state[
                "current_boundary_payload_sha256"
            ],
            coverage_payload_sha256=state[
                "current_coverage_payload_sha256"
            ],
            confirmed_by="email:owner@example.com",
            confirmed_at="2026-08-20T00:00:01Z",
        )

    def test_load_none_does_not_persist_a_proposal(self):
        self.assertIsNone(store.load_analysis_boundary_state(PROJECT_ID))
        project_dir = self.root / "projects" / PROJECT_ID
        self.assertFalse((project_dir / "analysis_boundary_state.json").exists())
        self.assertFalse((project_dir / "analysis_boundary_revisions").exists())
        self.assertFalse((project_dir / "coverage_revisions").exists())

    def test_save_load_pair_digests_history_and_project_scoped_lock(self):
        boundary, coverage, state = self._save()
        self.assertEqual(
            state["effective_status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED"
        )
        self.assertEqual(
            state["derived_status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED"
        )
        self.assertFalse(state["is_stale"])
        self.assertEqual(state["artifact_status"], "CURRENT")
        self.assertEqual(
            state["current_structure_revision_id"], STRUCTURE_ID_1
        )
        self.assertEqual(state["current_evidence_revision_id"], EVIDENCE_ID_1)
        self.assertEqual(
            state["current_boundary_payload_sha256"],
            boundary["revision_payload_sha256"],
        )
        self.assertEqual(
            state["current_coverage_payload_sha256"],
            coverage["revision_payload_sha256"],
        )
        self.assertEqual(len(state["revision_history"]), 1)
        self.assertEqual(
            state["state_payload_sha256"],
            store.analysis_boundary_state_payload_sha256(state),
        )
        self.assertEqual(
            store.load_analysis_boundary_revision(PROJECT_ID, BOUNDARY_ID_1),
            boundary,
        )
        self.assertEqual(
            store.load_coverage_revision(PROJECT_ID, COVERAGE_ID_1), coverage
        )
        loaded = store.load_current_analysis_boundary_bundle(
            PROJECT_ID, IMPORT_ID
        )
        self.assertEqual(loaded["boundary_revision"], boundary)
        self.assertEqual(loaded["coverage_revision"], coverage)
        lock_path = self.root / "projects" / PROJECT_ID / ".mapping.lock"
        self.assertTrue(lock_path.is_file())
        self.assertEqual(lock_path.resolve().parent, lock_path.parent.resolve())

    def test_save_and_confirm_are_idempotent_but_cas_fails_closed(self):
        confirmed_revisions = self._revisions(boundary_status="confirmed")
        first = self._save(revisions=confirmed_revisions)
        retried = self._save(revisions=confirmed_revisions)
        self.assertEqual(retried, first)
        confirmed = self._confirm(first[2])
        self.assertEqual(confirmed["effective_status"], "READY_FOR_DOSSIERS")
        self.assertEqual(confirmed["derived_status"], "READY_FOR_DOSSIERS")
        self.assertEqual(len(confirmed["confirmation_events"]), 1)
        repeated = self._confirm(confirmed)
        self.assertEqual(len(repeated["confirmation_events"]), 1)
        with self.assertRaisesRegex(FileExistsError, "revision conflict"):
            store.confirm_analysis_boundary_cas(
                project_id=PROJECT_ID,
                import_id=IMPORT_ID,
                boundary_revision_id=BOUNDARY_ID_2,
                coverage_revision_id=COVERAGE_ID_1,
                boundary_payload_sha256=confirmed[
                    "current_boundary_payload_sha256"
                ],
                coverage_payload_sha256=confirmed[
                    "current_coverage_payload_sha256"
                ],
                confirmed_by="email:owner@example.com",
                confirmed_at="2026-08-20T00:00:02Z",
            )
        second = self._revisions(
            revision_number=2,
            boundary_revision_id=BOUNDARY_ID_2,
            coverage_revision_id=COVERAGE_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-20T00:01:00Z",
        )
        with self.assertRaisesRegex(FileExistsError, "revision conflict"):
            self._save(
                revisions=second,
                request_fingerprint=REQUEST_2,
                updated_at="2026-08-20T00:01:00Z",
            )

    def test_draft_pair_cannot_be_confirmed_and_state_is_unchanged(self):
        _, _, draft_state = self._save()
        state_path = store._analysis_boundary_state_path(PROJECT_ID)
        before = store._read_json(state_path)
        with self.assertRaisesRegex(ValueError, "payload is not confirmed"):
            self._confirm(draft_state)
        after = store._read_json(state_path)
        self.assertEqual(after, before)
        self.assertEqual(
            after["effective_status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED"
        )
        self.assertEqual(after["confirmation_events"], [])

    def test_coverage_must_bind_canonical_confirmed_boundary(self):
        revisions = self._revisions(
            boundary_status="confirmed",
            coverage_boundary_sha256="0" * 64,
        )
        _, _, state = self._save(revisions=revisions)
        state_path = store._analysis_boundary_state_path(PROJECT_ID)
        before = store._read_json(state_path)
        with self.assertRaisesRegex(ValueError, "not bound"):
            self._confirm(state)
        self.assertEqual(store._read_json(state_path), before)

    def test_proposed_coverage_rows_cannot_be_confirmed(self):
        revisions = self._revisions(
            boundary_status="confirmed",
            coverage_rows=[
                {
                    "coverage_id": "coverage_" + "0" * 32,
                    "review_status": "proposed",
                }
            ],
        )
        _, _, state = self._save(revisions=revisions)
        state_path = store._analysis_boundary_state_path(PROJECT_ID)
        before = store._read_json(state_path)
        with self.assertRaisesRegex(ValueError, "proposed rows"):
            self._confirm(state)
        self.assertEqual(store._read_json(state_path), before)

    def test_upstream_head_change_derives_stale_and_blocks_confirmation(self):
        _, _, state = self._save(
            revisions=self._revisions(boundary_status="confirmed")
        )
        new_source = self._source(
            structure_revision_id=STRUCTURE_ID_2,
            evidence_revision_id=EVIDENCE_ID_2,
            structure_digest="1" * 64,
            evidence_digest="2" * 64,
        )
        self.current_upstream["source"] = new_source
        stale = store.load_analysis_boundary_state(PROJECT_ID)
        self.assertTrue(stale["is_stale"])
        self.assertEqual(stale["artifact_status"], "STALE")
        self.assertEqual(stale["derived_status"], "ANALYSIS_BOUNDARY_REQUIRED")
        with self.assertRaises(store.AnalysisBoundaryInputConflictError) as ctx:
            self._confirm(state)
        self.assertEqual(
            ctx.exception.current_structure_revision_id, STRUCTURE_ID_2
        )
        self.assertEqual(
            ctx.exception.current_evidence_revision_id, EVIDENCE_ID_2
        )
        self.assertEqual(
            ctx.exception.current_structure_status, "READY_FOR_DOSSIERS"
        )

        second = self._revisions(
            revision_number=2,
            boundary_revision_id=BOUNDARY_ID_2,
            coverage_revision_id=COVERAGE_ID_2,
            request_fingerprint=REQUEST_2,
            source=new_source,
            created_at="2026-08-20T00:01:00Z",
        )
        _, _, rebuilt = self._save(
            base_boundary_revision_id=BOUNDARY_ID_1,
            base_coverage_revision_id=COVERAGE_ID_1,
            revisions=second,
            request_fingerprint=REQUEST_2,
            updated_at="2026-08-20T00:01:00Z",
        )
        self.assertFalse(rebuilt["is_stale"])
        self.assertEqual(rebuilt["current_boundary_revision_number"], 2)
        self.assertEqual(len(rebuilt["revision_history"]), 2)
        self.assertIsNone(rebuilt["confirmed_boundary_revision_id"])

    def test_non_ready_or_mismatched_upstream_fails_before_immutable_write(self):
        self.current_upstream["status"] = "STRUCTURE_REVIEW_REQUIRED"
        with self.assertRaises(store.AnalysisBoundaryInputConflictError) as ctx:
            self._save()
        self.assertEqual(
            ctx.exception.current_structure_status,
            "STRUCTURE_REVIEW_REQUIRED",
        )
        project_dir = self.root / "projects" / PROJECT_ID
        self.assertFalse((project_dir / "analysis_boundary_revisions").exists())
        self.assertFalse((project_dir / "coverage_revisions").exists())

        self.current_upstream["status"] = "READY_FOR_DOSSIERS"
        boundary, coverage = self._revisions()
        boundary["source"]["structure_payload_sha256"] = "0" * 64
        boundary["revision_payload_sha256"] = (
            store.analysis_boundary_revision_payload_sha256(boundary)
        )
        coverage["source"] = deepcopy(boundary["source"])
        coverage["boundary_payload_sha256"] = boundary[
            "revision_payload_sha256"
        ]
        coverage["revision_payload_sha256"] = (
            store.coverage_revision_payload_sha256(coverage)
        )
        with self.assertRaises(store.AnalysisBoundaryInputConflictError):
            self._save(revisions=(boundary, coverage))
        self.assertFalse((project_dir / "analysis_boundary_revisions").exists())

    def test_interrupted_publication_reuses_same_request_payloads(self):
        original_atomic_write = store._atomic_write_json
        interrupted = False

        def fail_state_once(path, value):
            nonlocal interrupted
            if path.name == "analysis_boundary_state.json" and not interrupted:
                interrupted = True
                raise OSError("simulated hard interruption")
            return original_atomic_write(path, value)

        with patch.object(
            store, "_atomic_write_json", side_effect=fail_state_once
        ):
            with self.assertRaisesRegex(OSError, "hard interruption"):
                self._save()
        self.assertIsNone(store.load_analysis_boundary_state(PROJECT_ID))
        self.assertIsNotNone(
            store.load_analysis_boundary_revision(PROJECT_ID, BOUNDARY_ID_1)
        )
        self.assertIsNotNone(
            store.load_coverage_revision(PROJECT_ID, COVERAGE_ID_1)
        )
        regenerated = self._revisions(
            created_at="2026-08-20T00:00:09Z"
        )
        durable_boundary, durable_coverage, recovered = self._save(
            revisions=regenerated,
            updated_at="2026-08-20T00:00:09Z",
        )
        self.assertEqual(
            durable_boundary["created_at"], "2026-08-20T00:00:00Z"
        )
        self.assertEqual(
            durable_coverage["created_at"], "2026-08-20T00:00:00Z"
        )
        self.assertEqual(recovered["current_boundary_revision_id"], BOUNDARY_ID_1)
        self.assertEqual(len(recovered["revision_history"]), 1)

    def test_interrupted_after_boundary_rebinds_coverage_on_retry(self):
        original_atomic_write = store._atomic_write_json
        interrupted = False

        def fail_coverage_once(path, value):
            nonlocal interrupted
            if path.parent.name == "coverage_revisions" and not interrupted:
                interrupted = True
                raise OSError("simulated coverage interruption")
            return original_atomic_write(path, value)

        with patch.object(
            store, "_atomic_write_json", side_effect=fail_coverage_once
        ):
            with self.assertRaisesRegex(OSError, "coverage interruption"):
                self._save()
        durable_boundary = store.load_analysis_boundary_revision(
            PROJECT_ID, BOUNDARY_ID_1
        )
        self.assertIsNotNone(durable_boundary)
        self.assertIsNone(
            store.load_coverage_revision(PROJECT_ID, COVERAGE_ID_1)
        )

        regenerated = self._revisions(
            created_at="2026-08-20T00:00:09Z"
        )
        recovered_boundary, recovered_coverage, recovered_state = self._save(
            revisions=regenerated,
            updated_at="2026-08-20T00:00:09Z",
        )
        self.assertEqual(recovered_boundary, durable_boundary)
        self.assertEqual(
            recovered_coverage["boundary_payload_sha256"],
            durable_boundary["revision_payload_sha256"],
        )
        self.assertEqual(
            recovered_state["current_boundary_payload_sha256"],
            durable_boundary["revision_payload_sha256"],
        )

    def test_confirm_generated_n_plus_one_recovers_with_new_timestamp(self):
        _, _, draft_state = self._save()
        confirmed_revisions = self._revisions(
            revision_number=2,
            boundary_revision_id=BOUNDARY_ID_2,
            coverage_revision_id=COVERAGE_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-20T00:01:00Z",
            boundary_status="confirmed",
        )
        original_atomic_write = store._atomic_write_json
        interrupted = False

        def fail_second_state_once(path, value):
            nonlocal interrupted
            if path.name == "analysis_boundary_state.json" and not interrupted:
                interrupted = True
                raise OSError("simulated confirm publication interruption")
            return original_atomic_write(path, value)

        with patch.object(
            store, "_atomic_write_json", side_effect=fail_second_state_once
        ):
            with self.assertRaisesRegex(OSError, "confirm publication"):
                self._save(
                    base_boundary_revision_id=draft_state[
                        "current_boundary_revision_id"
                    ],
                    base_coverage_revision_id=draft_state[
                        "current_coverage_revision_id"
                    ],
                    revisions=confirmed_revisions,
                    request_fingerprint=REQUEST_2,
                    updated_at="2026-08-20T00:01:00Z",
                )
        regenerated = self._revisions(
            revision_number=2,
            boundary_revision_id=BOUNDARY_ID_2,
            coverage_revision_id=COVERAGE_ID_2,
            request_fingerprint=REQUEST_2,
            created_at="2026-08-20T00:01:09Z",
            boundary_status="confirmed",
        )
        boundary, coverage, review_state = self._save(
            base_boundary_revision_id=BOUNDARY_ID_1,
            base_coverage_revision_id=COVERAGE_ID_1,
            revisions=regenerated,
            request_fingerprint=REQUEST_2,
            updated_at="2026-08-20T00:01:09Z",
        )
        self.assertEqual(boundary["created_at"], "2026-08-20T00:01:00Z")
        self.assertEqual(coverage["created_at"], "2026-08-20T00:01:00Z")
        self.assertEqual(review_state["current_boundary_revision_number"], 2)
        ready = self._confirm(review_state)
        self.assertEqual(ready["effective_status"], "READY_FOR_DOSSIERS")
        self.assertEqual(len(ready["revision_history"]), 2)

    def test_full_state_and_head_tampering_fail_closed(self):
        boundary, _, _ = self._save()
        state_path = store._analysis_boundary_state_path(PROJECT_ID)
        durable_state = store._read_json(state_path)
        tampered_state = deepcopy(durable_state)
        tampered_state["revision_history"][0]["request_fingerprint"] = "0" * 64
        store._atomic_write_json(state_path, tampered_state)
        with self.assertRaisesRegex(ValueError, "state payload digest"):
            store.load_analysis_boundary_state(PROJECT_ID)

        store._atomic_write_json(state_path, durable_state)
        revision_path = store._analysis_boundary_revision_path(
            PROJECT_ID, BOUNDARY_ID_1
        )
        tampered_boundary = deepcopy(boundary)
        tampered_boundary["analysis_boundary"]["evaluation_objects"][0][
            "name"
        ] = "被篡改"
        store._atomic_write_json(revision_path, tampered_boundary)
        with self.assertRaisesRegex(ValueError, "revision payload digest"):
            store.load_analysis_boundary_state(PROJECT_ID)

    def test_resource_ids_and_paths_cannot_escape_storage_root(self):
        with self.assertRaises(ValueError):
            store.load_analysis_boundary_state("../outside")
        with self.assertRaises(ValueError):
            store.load_analysis_boundary_revision(PROJECT_ID, "boundary_..")
        with self.assertRaises(ValueError):
            store.load_coverage_revision(PROJECT_ID, "coverage_..")
        outside = Path(self.temp_dir.name) / "outside"
        self.assertFalse(outside.exists())


if __name__ == "__main__":
    unittest.main()
