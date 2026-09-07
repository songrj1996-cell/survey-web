import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import config
from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
ANALYSIS = "analysis_" + "2" * 32
BASE_REPORT = "report_" + "3" * 32
NEXT_REPORT = "report_" + "4" * 32
SECTION = "section_" + "5" * 32
RERUN = "rerun_" + "6" * 32
OWNER = "email:owner@example.com"
KEY = "rerun-request-01"
FINGERPRINT = "a" * 64


class InterviewV2ReportRerunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="interview-v2-report-rerun-store-"
        )
        self.data_patch = patch.object(
            config, "INTERVIEW_V2_DATA_DIR", self.temp.name
        )
        self.data_patch.start()
        analysis = {
            "analysis_run_id": ANALYSIS,
            "project_id": PROJECT,
            "status": "completed",
            "source": {},
            "created_at": "2026-09-03T00:00:00Z",
        }
        analysis["revision_payload_sha256"] = store._analysis_digest(analysis)
        directory = Path(self.temp.name) / "projects" / PROJECT / "analysis_runs"
        store._atomic_write_json(
            directory / "versions" / f"{ANALYSIS}.json", analysis
        )
        store._atomic_write_json(directory / "state.json", {
            "project_id": PROJECT,
            "current_analysis_run_id": ANALYSIS,
            "current_version_number": 1,
            "history": [],
        })
        self.source_patch = patch.object(
            store, "_require_analysis_source_current_locked", return_value=None
        )
        self.source_patch.start()

    def tearDown(self):
        self.source_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    def _revision(
        self,
        report_version_id=BASE_REPORT,
        section_revision=1,
        content="旧正文",
    ):
        analysis = store.load_current_analysis_run(PROJECT)["revision"]
        return {
            "report_version_id": report_version_id,
            "report_schema_version": "interview-report/1.0",
            "source": {
                "analysis_run_id": ANALYSIS,
                "analysis_revision_payload_sha256": analysis[
                    "revision_payload_sha256"
                ],
            },
            "status": "draft",
            "audit_status": "audited",
            "sections": [{
                "section_id": SECTION,
                "report_version_id": report_version_id,
                "section_key": "scope_and_sample",
                "title": "研究范围与样本说明",
                "order": 1,
                "section_revision": section_revision,
                "content": content,
                "content_sha256": store._canonical_payload_sha256(content),
                "claim_ids": [],
                "locked": False,
                "audit_status": "audit_passed",
            }],
            "claims": [],
            "audit_issues": [],
            "created_at": "2026-09-03T00:01:00Z",
        }

    def _claim(self, fingerprint=FINGERPRINT):
        return store.claim_report_rerun_operation(
            owner_key=OWNER,
            project_id=PROJECT,
            idempotency_key=KEY,
            request_fingerprint=fingerprint,
            rerun_id=RERUN,
            report_version_id=NEXT_REPORT,
            base_report_version_id=BASE_REPORT,
            section_id=SECTION,
            base_section_revision=1,
            created_at="2026-09-03T00:02:00Z",
        )

    def test_claim_is_owner_scoped_hashed_and_conflict_safe(self):
        claimed = self._claim()
        replay = self._claim()

        self.assertTrue(claimed["_claim_acquired"])
        self.assertFalse(replay["_claim_acquired"])
        self.assertEqual("pending", replay["status"])
        path = store._report_rerun_operation_path(OWNER, PROJECT, KEY)
        persisted = path.read_text(encoding="utf-8")
        self.assertNotIn(OWNER, persisted)
        self.assertNotIn(KEY, persisted)
        self.assertRegex(
            store._read_json(path)["operation_payload_sha256"],
            r"^[0-9a-f]{64}$",
        )

        with self.assertRaisesRegex(
            store.ReportRerunIdempotencyConflictError,
            r"^report rerun idempotency conflict$",
        ):
            self._claim("b" * 64)

    def test_failed_precommit_claim_can_be_released_and_reclaimed(self):
        self._claim()
        released = store.release_report_rerun_operation(
            owner_key=OWNER,
            project_id=PROJECT,
            idempotency_key=KEY,
            request_fingerprint=FINGERPRINT,
            report_version_id=NEXT_REPORT,
        )
        self.assertTrue(released)
        self.assertFalse(
            store._report_rerun_operation_path(OWNER, PROJECT, KEY).exists()
        )
        self.assertTrue(self._claim()["_claim_acquired"])

    def test_pending_operation_recovers_completed_committed_report(self):
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=self._revision(),
        )
        self._claim()
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=BASE_REPORT,
            section_id=SECTION,
            base_section_revision=1,
            revision=self._revision(
                report_version_id=NEXT_REPORT,
                section_revision=2,
                content="新正文",
            ),
        )

        recovered = self._claim()

        self.assertEqual("completed", recovered["status"])
        self.assertFalse(recovered["_claim_acquired"])
        self.assertRegex(
            recovered["revision_payload_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertFalse(store.release_report_rerun_operation(
            owner_key=OWNER,
            project_id=PROJECT,
            idempotency_key=KEY,
            request_fingerprint=FINGERPRINT,
            report_version_id=NEXT_REPORT,
        ))

    def test_complete_operation_requires_and_records_committed_report(self):
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=self._revision(),
        )
        self._claim()
        with self.assertRaisesRegex(ValueError, "not committed"):
            store.complete_report_rerun_operation(
                owner_key=OWNER,
                project_id=PROJECT,
                idempotency_key=KEY,
                request_fingerprint=FINGERPRINT,
                report_version_id=NEXT_REPORT,
                completed_at="2026-09-03T00:03:00Z",
            )
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=BASE_REPORT,
            section_id=SECTION,
            base_section_revision=1,
            revision=self._revision(
                report_version_id=NEXT_REPORT,
                section_revision=2,
                content="已提交正文",
            ),
        )

        completed = store.complete_report_rerun_operation(
            owner_key=OWNER,
            project_id=PROJECT,
            idempotency_key=KEY,
            request_fingerprint=FINGERPRINT,
            report_version_id=NEXT_REPORT,
            completed_at="2026-09-03T00:03:00Z",
        )

        self.assertEqual("completed", completed["status"])
        self.assertEqual("2026-09-03T00:03:00Z", completed["completed_at"])
        self.assertRegex(
            completed["revision_payload_sha256"], r"^[0-9a-f]{64}$"
        )
        self.assertEqual("completed", self._claim()["status"])

    def test_tampered_operation_digest_is_rejected(self):
        self._claim()
        path = store._report_rerun_operation_path(OWNER, PROJECT, KEY)
        record = store._read_json(path)
        record["section_id"] = "section_" + "9" * 32
        store._atomic_write_json(path, record)

        with self.assertRaisesRegex(ValueError, "integrity"):
            self._claim()


if __name__ == "__main__":
    unittest.main()
