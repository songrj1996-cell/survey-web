import hashlib
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app.core import config
from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
ANALYSIS = "analysis_" + "2" * 32
REPORT = "report_" + "3" * 32
REPORT_TWO = "report_" + "4" * 32


class InterviewV2ExportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="interview-v2-export-store-"
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
            "created_at": "2026-09-02T00:00:00Z",
        }
        analysis["revision_payload_sha256"] = store._analysis_digest(analysis)
        analysis_dir = (
            Path(self.temp.name)
            / "projects"
            / PROJECT
            / "analysis_runs"
        )
        store._atomic_write_json(
            analysis_dir / "versions" / f"{ANALYSIS}.json", analysis
        )
        store._atomic_write_json(
            analysis_dir / "state.json",
            {
                "project_id": PROJECT,
                "current_analysis_run_id": ANALYSIS,
                "current_version_number": 1,
                "history": [],
            },
        )
        self.source_patch = patch.object(
            store, "_require_analysis_source_current_locked", return_value=None
        )
        self.source_check = self.source_patch.start()
        report_revision = self._report_revision()
        report_revision["source"]["analysis_revision_payload_sha256"] = analysis[
            "revision_payload_sha256"
        ]
        self.report = store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=None,
            revision=report_revision,
        )["revision"]
        self.request_fingerprint = store._canonical_payload_sha256(
            {
                "report_version_id": REPORT,
                "report_revision_payload_sha256": self.report[
                    "revision_payload_sha256"
                ],
                "format": "docx",
                "include_evidence_appendix": True,
                "export_profile_version": (
                    "approved-docx-evidence-redacted/1.0"
                ),
            }
        )
        self.artifact_id = f"export_{self.request_fingerprint[:32]}"

    def tearDown(self):
        self.source_patch.stop()
        self.data_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _report_revision(report_version_id=REPORT, status="approved"):
        return {
            "report_version_id": report_version_id,
            "report_schema_version": "interview-report/1.0",
            "source": {
                "analysis_run_id": ANALYSIS,
                "analysis_revision_payload_sha256": "",
            },
            "status": status,
            "audit_status": "audited",
            "sections": [],
            "claims": [],
            "audit_issues": [],
            "created_at": "2026-09-02T00:01:00Z",
        }

    def _artifact(self, **overrides):
        manifest = {
            "schema_version": "interview-export-manifest/1.0",
            "format": "docx",
            "export_profile_version": (
                "approved-docx-evidence-redacted/1.0"
            ),
            "report_version_id": REPORT,
            "report_version_number": self.report["version_number"],
            "report_revision_payload_sha256": self.report[
                "revision_payload_sha256"
            ],
            "approval_status": "approved",
            "section_count": 0,
        }
        profile = {
            "profile_version": "approved-docx-evidence-redacted/1.0",
            "format": "docx",
            "include_evidence_appendix": True,
        }
        value = {
            "export_artifact_id": self.artifact_id,
            "project_id": PROJECT,
            "report_version_id": REPORT,
            "report_version_number": self.report["version_number"],
            "report_revision_payload_sha256": self.report[
                "revision_payload_sha256"
            ],
            "request_fingerprint": self.request_fingerprint,
            "status": "READY",
            "format": "docx",
            "export_profile": profile,
            "export_profile_sha256": store._canonical_payload_sha256(profile),
            "manifest": manifest,
            "manifest_sha256": store._canonical_payload_sha256(manifest),
            "file_name": "访谈研究报告-v1.docx",
            "media_type": store._DOCX_MEDIA_TYPE,
            "created_at": "2026-09-02T00:02:00Z",
            "ready_at": "2026-09-02T00:02:00Z",
            "created_by": "email:owner@example.com",
        }
        value.update(overrides)
        return value

    @property
    def content(self):
        return b"PK\x03\x04immutable-docx"

    def _save(self, artifact=None, content=None):
        return store.save_export_artifact(
            project_id=PROJECT,
            artifact=artifact or self._artifact(),
            content=self.content if content is None else content,
        )

    def test_saves_metadata_bytes_and_global_locator(self):
        saved = self._save()
        artifact = saved["artifact"]
        self.assertEqual(hashlib.sha256(self.content).hexdigest(), artifact["content_sha256"])
        self.assertEqual(len(self.content), artifact["byte_size"])
        self.assertEqual(
            store.export_artifact_payload_sha256(artifact),
            artifact["artifact_payload_sha256"],
        )

        loaded = store.load_export_artifact(self.artifact_id)
        downloaded = store.load_export_artifact_bytes(self.artifact_id)
        self.assertEqual(artifact, loaded["artifact"])
        self.assertEqual(self.content, downloaded["content"])
        locator = store._read_json(
            store._export_artifact_locator_path(self.artifact_id)
        )
        self.assertEqual(
            {
                "export_artifact_id",
                "project_id",
                "locator_payload_sha256",
            },
            set(locator),
        )

    def test_exact_replay_reuses_immutable_artifact(self):
        artifact = self._artifact()
        first = self._save(artifact=artifact)
        second = self._save(artifact=deepcopy(artifact))
        self.assertEqual(first, second)

        changed = deepcopy(artifact)
        changed["created_at"] = "2026-09-02T00:03:00Z"
        changed["ready_at"] = changed["created_at"]
        replayed = self._save(artifact=changed)
        self.assertEqual(first, replayed)

        conflicting = deepcopy(artifact)
        conflicting["manifest"]["section_count"] = 1
        conflicting["manifest_sha256"] = store._canonical_payload_sha256(
            conflicting["manifest"]
        )
        with self.assertRaisesRegex(FileExistsError, "identity collision"):
            self._save(artifact=conflicting)

    def test_content_metadata_and_locator_tampering_are_rejected(self):
        self._save()
        artifact_dir = store._export_artifact_dir(PROJECT, self.artifact_id)
        (artifact_dir / "report.docx").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "content integrity"):
            store.load_export_artifact_bytes(self.artifact_id)

        (artifact_dir / "report.docx").write_bytes(self.content)
        metadata = store._read_json(artifact_dir / "metadata.json")
        metadata["file_name"] = "tampered.docx"
        store._atomic_write_json(artifact_dir / "metadata.json", metadata)
        self.assertEqual(
            PROJECT,
            store.locate_export_artifact(self.artifact_id)["project_id"],
        )
        with self.assertRaisesRegex(ValueError, "metadata digest mismatch"):
            store.load_export_artifact(self.artifact_id)

        store._atomic_write_json(artifact_dir / "metadata.json", self._saveable_metadata())
        locator_path = store._export_artifact_locator_path(self.artifact_id)
        locator = store._read_json(locator_path)
        locator["locator_payload_sha256"] = "0" * 64
        store._atomic_write_json(locator_path, locator)
        with self.assertRaisesRegex(ValueError, "locator integrity"):
            store.locate_export_artifact(self.artifact_id)

    def _saveable_metadata(self):
        value = self._artifact()
        value["content_sha256"] = hashlib.sha256(self.content).hexdigest()
        value["byte_size"] = len(self.content)
        value["artifact_payload_sha256"] = store.export_artifact_payload_sha256(value)
        return value

    def test_locator_failure_keeps_partial_artifact_hidden_and_retry_recovers(self):
        locator_path = store._export_artifact_locator_path(self.artifact_id)
        original_write = store._atomic_write_json

        def fail_locator(path, value):
            if Path(path) == locator_path:
                raise OSError("injected locator failure")
            return original_write(path, value)

        with patch.object(store, "_atomic_write_json", side_effect=fail_locator):
            with self.assertRaisesRegex(OSError, "injected locator failure"):
                self._save()
        self.assertIsNone(store.load_export_artifact(self.artifact_id))

        retried_artifact = self._artifact(
            created_at="2026-09-02T00:04:00Z",
            ready_at="2026-09-02T00:04:00Z",
        )
        recovered = self._save(artifact=retried_artifact)
        self.assertEqual(
            self.artifact_id, recovered["artifact"]["export_artifact_id"]
        )
        self.assertEqual(
            "2026-09-02T00:02:00Z", recovered["artifact"]["created_at"]
        )
        self.assertEqual(
            self.content,
            store.load_export_artifact_bytes(self.artifact_id)["content"],
        )

    def test_report_binding_and_source_are_rechecked_before_publish(self):
        wrong_digest = self._artifact()
        wrong_digest["report_revision_payload_sha256"] = "0" * 64
        wrong_digest["manifest"]["report_revision_payload_sha256"] = "0" * 64
        wrong_digest["manifest_sha256"] = store._canonical_payload_sha256(
            wrong_digest["manifest"]
        )
        wrong_digest["request_fingerprint"] = store._canonical_payload_sha256(
            {
                "report_version_id": REPORT,
                "report_revision_payload_sha256": "0" * 64,
                "format": "docx",
                "include_evidence_appendix": True,
                "export_profile_version": (
                    "approved-docx-evidence-redacted/1.0"
                ),
            }
        )
        wrong_digest["export_artifact_id"] = (
            f"export_{wrong_digest['request_fingerprint'][:32]}"
        )
        with self.assertRaises(store.ExportInputConflictError):
            self._save(artifact=wrong_digest)

        self.source_check.side_effect = ValueError("analysis source changed")
        with self.assertRaises(store.ExportInputConflictError):
            self._save()

    def test_manifest_and_request_fingerprint_must_match_artifact_metadata(self):
        mutations = (
            ("report_version_number", 99),
            ("report_revision_payload_sha256", "0" * 64),
            ("format", "pdf"),
            ("export_profile_version", "other-profile/1.0"),
        )
        for field, value in mutations:
            artifact = self._artifact()
            artifact["manifest"][field] = value
            artifact["manifest_sha256"] = store._canonical_payload_sha256(
                artifact["manifest"]
            )
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "manifest binding"):
                    self._save(artifact=artifact)

        artifact = self._artifact()
        artifact["request_fingerprint"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "manifest binding"):
            self._save(artifact=artifact)

    def test_artifact_remains_downloadable_after_report_head_advances(self):
        self._save()
        next_revision = self._report_revision(REPORT_TWO, status="draft")
        next_revision["source"]["analysis_revision_payload_sha256"] = self.report[
            "source"
        ]["analysis_revision_payload_sha256"]
        store.save_report_version_cas(
            project_id=PROJECT,
            base_report_version_id=REPORT,
            revision=next_revision,
        )
        self.assertEqual(
            self.content,
            store.load_export_artifact_bytes(self.artifact_id)["content"],
        )

    def test_invalid_ids_and_file_names_never_escape_storage_root(self):
        with self.assertRaisesRegex(ValueError, "invalid interview V2 resource id"):
            store.load_export_artifact("export_../../outside")
        with self.assertRaisesRegex(ValueError, "metadata is invalid"):
            self._save(artifact=self._artifact(file_name="../report.docx"))


if __name__ == "__main__":
    unittest.main()
