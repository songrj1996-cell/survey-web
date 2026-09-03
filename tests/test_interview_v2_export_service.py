import hashlib
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from app.core import config
from app.core.interview_v2_export import InterviewV2ExportValidationError
from app.core.interview_v2_report import InterviewV2ReportValidationError
from app.schemas.interview_v2_export import InterviewV2ExportArtifactResponse
from app.services import interview_v2_export_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
REPORT = "report_" + "2" * 32
EVIDENCE_REVISION = "evidence_" + "3" * 32
EVIDENCE = "ev_" + "4" * 32
REPORT_SHA = "5" * 64


class InterviewV2ExportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="interview-v2-export-service-"
        )
        self.data_patch = patch.object(
            config, "INTERVIEW_V2_DATA_DIR", self.temp.name
        )
        self.data_patch.start()

    def tearDown(self):
        self.data_patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _saved_report(*, status="approved", current=True):
        revision = {
            "project_id": PROJECT,
            "report_version_id": REPORT,
            "version_number": 7,
            "revision_payload_sha256": REPORT_SHA,
            "status": status,
            "audit_status": "audited",
            "approved_by": "email:owner@example.com",
            "approved_at": "2026-09-02T01:02:03Z",
            "source": {
                "analysis_run_id": "analysis_" + "6" * 32,
                "analysis_revision_payload_sha256": "7" * 64,
                "analysis_source": {
                    "evidence_revision_id": EVIDENCE_REVISION,
                },
            },
            "sections": [],
            "claims": [],
            "audit_issues": [],
            "frozen_findings": [],
            "frozen_stat_facts": [],
        }
        return {
            "project_id": PROJECT,
            "state": {
                "current_report_version_id": (
                    REPORT if current else "report_" + "8" * 32
                )
            },
            "revision": revision,
        }

    @staticmethod
    def _profile():
        return {
            "profile_version": "approved-docx-evidence-redacted/1.0",
            "format": "docx",
            "include_evidence_appendix": True,
            "visible_evidence_fields": [
                "participant_label",
                "evidence_type",
                "normalized_content",
                "sheet_name",
                "cell_address",
            ],
            "omitted_evidence_fields": [
                "internal_ids",
                "raw_content",
                "display_content",
                "recorder_label",
                "owner",
            ],
        }

    @staticmethod
    def _manifest():
        section_keys = (
            "scope_and_sample",
            "core_findings",
            "module_findings",
            "participant_differences",
            "participant_logics",
            "recommendations",
            "evidence_and_limitations",
        )
        return {
            "schema_version": "interview-report-export/1.0",
            "format": "docx",
            "export_profile_version": "approved-docx-evidence-redacted/1.0",
            "report_version_id": REPORT,
            "report_version_number": 7,
            "report_revision_payload_sha256": REPORT_SHA,
            "approval_status": "approved",
            "approved_at": "2026-09-02T01:02:03Z",
            "report_body_sha256": "9" * 64,
            "section_manifest": [
                {
                    "section_key": key,
                    "section_revision": 1,
                    "content_sha256": format(index, "x") * 64,
                }
                for index, key in enumerate(section_keys, start=1)
            ],
            "appendix_sha256": "a" * 64,
            "document_markdown_sha256": "b" * 64,
            "section_count": 7,
            "claim_count": 1,
            "evidence_count": 1,
            "appendix_entry_count": 1,
        }

    def _package(self):
        manifest = self._manifest()
        return {
            "markdown": "# 已批准报告\n\n# 证据附录\n",
            "filename": "访谈研究报告_V7_已批准.docx",
            "export_profile": self._profile(),
            "manifest": manifest,
            "manifest_sha256": service._payload_sha256(manifest),
            "report_revision_payload_sha256": REPORT_SHA,
        }

    @staticmethod
    def _evidence_revision():
        return {
            "entries": [
                {
                    "evidence_id": EVIDENCE,
                    "participant_id": "participant_" + "c" * 32,
                    "participant_label": "玩家 A",
                    "normalized_content": "入口容易理解。",
                    "sheet_name": "记录表",
                    "cell_address": "D12",
                }
            ]
        }

    def _created_artifact(self, metadata=None, content=b"docx-bytes"):
        artifact = deepcopy(metadata or {})
        artifact["content_sha256"] = hashlib.sha256(content).hexdigest()
        artifact["byte_size"] = len(content)
        artifact["artifact_payload_sha256"] = "d" * 64
        return artifact

    def _creation_patches(self, *, saved_report=None):
        return (
            patch.object(
                service,
                "_load_accessible_report",
                return_value=saved_report or self._saved_report(),
            ),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "validate_report_approval", return_value={}),
            patch.object(
                service.store,
                "load_export_artifact_bytes",
                return_value=None,
            ),
            patch.object(
                service.store,
                "load_evidence_revision",
                return_value=self._evidence_revision(),
            ),
            patch.object(
                service,
                "build_export_package",
                return_value=self._package(),
            ),
            patch.object(
                service, "markdown_to_docx", return_value=b"docx-bytes"
            ),
            patch.object(service, "_validate_rendered_docx_privacy"),
        )

    def test_create_revalidates_renders_once_and_hides_internal_metadata(self):
        patches = self._creation_patches()

        def save_artifact(**kwargs):
            return {
                "project_id": PROJECT,
                "artifact": self._created_artifact(
                    kwargs["artifact"], kwargs["content"]
                ),
            }

        with (
            patches[0],
            patches[1] as current_mock,
            patches[2] as approval_mock,
            patches[3],
            patches[4],
            patches[5] as package_mock,
            patches[6] as render_mock,
            patches[7] as privacy_mock,
            patch.object(
                service.store,
                "save_export_artifact",
                side_effect=save_artifact,
            ) as save_mock,
        ):
            result = service.create_export(
                REPORT,
                {"format": "docx", "include_evidence_appendix": True},
                {"email": "owner@example.com"},
            )

        current_mock.assert_called_once_with(PROJECT, self._saved_report()["revision"])
        approval_mock.assert_called_once()
        package_mock.assert_called_once()
        self.assertEqual(EVIDENCE, next(iter(package_mock.call_args.args[1])))
        render_mock.assert_called_once_with(self._package()["markdown"])
        privacy_mock.assert_called_once_with(b"docx-bytes")
        self.assertEqual(b"docx-bytes", save_mock.call_args.kwargs["content"])
        saved_metadata = save_mock.call_args.kwargs["artifact"]
        self.assertEqual(REPORT_SHA, saved_metadata["report_revision_payload_sha256"])
        self.assertEqual("READY", result["status"])
        InterviewV2ExportArtifactResponse.model_validate(result)
        self.assertEqual(
            f"/api/v1/interview-export-artifacts/{result['export_artifact_id']}/download",
            result["download_url"],
        )
        for private_field in (
            "created_by",
            "request_fingerprint",
            "artifact_payload_sha256",
            "ready_at",
        ):
            self.assertNotIn(private_field, result)

    def test_identical_request_reuses_ready_artifact_without_rendering(self):
        saved_report = self._saved_report()
        request_profile = {
            "format": "docx",
            "include_evidence_appendix": True,
            "export_profile_version": "approved-docx-evidence-redacted/1.0",
        }
        fingerprint = service._payload_sha256(
            {
                "report_version_id": REPORT,
                "report_revision_payload_sha256": REPORT_SHA,
                **request_profile,
            }
        )
        artifact = {
            "export_artifact_id": f"export_{fingerprint[:32]}",
            "project_id": PROJECT,
            "report_version_id": REPORT,
            "report_version_number": 7,
            "report_revision_payload_sha256": REPORT_SHA,
            "request_fingerprint": fingerprint,
            "status": "READY",
            "format": "docx",
            "export_profile": self._profile(),
            "manifest": self._manifest(),
            "manifest_sha256": service._payload_sha256(self._manifest()),
            "content_sha256": "a" * 64,
            "byte_size": 123,
            "file_name": "访谈研究报告_V7_已批准.docx",
            "media_type": service.EXPORT_MEDIA_TYPE,
            "created_at": "2026-09-02T01:03:00Z",
            "created_by": "email:owner@example.com",
        }
        with (
            patch.object(
                service, "_load_accessible_report", return_value=saved_report
            ),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "validate_report_approval", return_value={}),
            patch.object(
                service.store,
                "load_export_artifact_bytes",
                return_value={
                    "project_id": PROJECT,
                    "artifact": artifact,
                    "content": b"persisted",
                },
            ),
            patch.object(service, "build_export_package") as package_mock,
            patch.object(service, "markdown_to_docx") as render_mock,
            patch.object(
                service.store,
                "save_export_artifact",
                return_value={"project_id": PROJECT, "artifact": artifact},
            ) as save_mock,
        ):
            result = service.create_export(REPORT, {}, None)
        self.assertEqual(artifact["export_artifact_id"], result["export_artifact_id"])
        package_mock.assert_not_called()
        render_mock.assert_not_called()
        save_mock.assert_called_once_with(
            project_id=PROJECT,
            artifact=artifact,
            content=b"persisted",
        )

    def test_identical_request_rechecks_current_input_under_store_lock(self):
        saved_report = self._saved_report()
        request_profile = {
            "format": "docx",
            "include_evidence_appendix": True,
            "export_profile_version": "approved-docx-evidence-redacted/1.0",
        }
        fingerprint = service._payload_sha256(
            {
                "report_version_id": REPORT,
                "report_revision_payload_sha256": REPORT_SHA,
                **request_profile,
            }
        )
        artifact = {
            "export_artifact_id": f"export_{fingerprint[:32]}",
            "project_id": PROJECT,
            "report_version_id": REPORT,
            "report_version_number": 7,
            "report_revision_payload_sha256": REPORT_SHA,
            "request_fingerprint": fingerprint,
        }
        with (
            patch.object(
                service, "_load_accessible_report", return_value=saved_report
            ),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(service, "validate_report_approval", return_value={}),
            patch.object(
                service.store,
                "load_export_artifact_bytes",
                return_value={
                    "project_id": PROJECT,
                    "artifact": artifact,
                    "content": b"persisted",
                },
            ),
            patch.object(
                service.store,
                "save_export_artifact",
                side_effect=store.ExportInputConflictError(),
            ),
            patch.object(service, "build_export_package") as package_mock,
            patch.object(service, "markdown_to_docx") as render_mock,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.create_export(REPORT, {}, None)
        self.assertEqual("EXPORT_INPUT_CHANGED", raised.exception.code)
        self.assertEqual(409, raised.exception.status_code)
        package_mock.assert_not_called()
        render_mock.assert_not_called()

    def test_create_blocks_draft_superseded_and_stale_reports(self):
        scenarios = (
            self._saved_report(status="draft"),
            self._saved_report(current=False),
        )
        for saved_report in scenarios:
            with self.subTest(saved_report=saved_report):
                with patch.object(
                    service,
                    "_load_accessible_report",
                    return_value=saved_report,
                ):
                    with self.assertRaises(InterviewV2ImportError) as raised:
                        service.create_export(REPORT, {}, None)
                self.assertEqual("REPORT_EXPORT_BLOCKED", raised.exception.code)

        with (
            patch.object(
                service,
                "_load_accessible_report",
                return_value=self._saved_report(),
            ),
            patch.object(service, "_is_report_current", return_value=False),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.create_export(REPORT, {}, None)
        self.assertEqual("REPORT_EXPORT_BLOCKED", raised.exception.code)

    def test_deterministic_or_evidence_validation_failure_blocks_export(self):
        with (
            patch.object(
                service,
                "_load_accessible_report",
                return_value=self._saved_report(),
            ),
            patch.object(service, "_is_report_current", return_value=True),
            patch.object(
                service,
                "validate_report_approval",
                side_effect=InterviewV2ReportValidationError("blocked"),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.create_export(REPORT, {}, None)
        self.assertEqual("REPORT_EXPORT_BLOCKED", raised.exception.code)

        patches = self._creation_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patch.object(
                service,
                "build_export_package",
                side_effect=InterviewV2ExportValidationError(
                    "EXPORT_EVIDENCE_MISSING", "missing"
                ),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.create_export(REPORT, {}, None)
        self.assertEqual("REPORT_EXPORT_BLOCKED", raised.exception.code)

    def test_render_and_publish_failures_have_stable_error_codes(self):
        patches = self._creation_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patches[5],
            patch.object(
                service, "markdown_to_docx", side_effect=RuntimeError("boom")
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.create_export(REPORT, {}, None)
        self.assertEqual("EXPORT_RENDER_FAILED", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

        for markdown in (
            "# 已批准报告\n\n内部编号 participant_**" + "a" * 32 + "**。",
            "# 证据附录\n\n- 规范化摘录：\n> participant_**"
            + "b" * 32
            + "**",
        ):
            with self.subTest(markdown=markdown):
                patches = self._creation_patches()
                package = self._package()
                package["markdown"] = markdown
                with (
                    patches[0],
                    patches[1],
                    patches[2],
                    patches[3],
                    patches[4],
                    patch.object(
                        service,
                        "build_export_package",
                        return_value=package,
                    ),
                    patch.object(service.store, "save_export_artifact") as save_mock,
                ):
                    with self.assertRaises(InterviewV2ImportError) as raised:
                        service.create_export(REPORT, {}, None)
                self.assertEqual("REPORT_EXPORT_BLOCKED", raised.exception.code)
                self.assertEqual(409, raised.exception.status_code)
                save_mock.assert_not_called()

        patches = self._creation_patches()
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patches[5], patches[6], patches[7],
            patch.object(
                service.store,
                "save_export_artifact",
                side_effect=store.ExportInputConflictError(),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.create_export(REPORT, {}, None)
        self.assertEqual("EXPORT_INPUT_CHANGED", raised.exception.code)

    def test_get_metadata_and_download_enforce_owner_and_reuse_saved_bytes(self):
        artifact = {
            "export_artifact_id": "export_" + "e" * 32,
            "project_id": PROJECT,
            "report_version_id": REPORT,
            "report_version_number": 7,
            "status": "READY",
            "format": "docx",
            "export_profile": self._profile(),
            "manifest": self._manifest(),
            "manifest_sha256": "a" * 64,
            "report_revision_payload_sha256": REPORT_SHA,
            "content_sha256": hashlib.sha256(b"persisted").hexdigest(),
            "byte_size": len(b"persisted"),
            "file_name": "访谈研究报告_V7_已批准.docx",
            "media_type": service.EXPORT_MEDIA_TYPE,
            "created_at": "2026-09-02T01:03:00Z",
            "created_by": "email:owner@example.com",
            "request_fingerprint": "f" * 64,
        }
        saved = {"project_id": PROJECT, "artifact": artifact}
        locator = {
            "project_id": PROJECT,
            "export_artifact_id": artifact["export_artifact_id"],
        }
        with (
            patch.object(
                service.store, "locate_export_artifact", return_value=locator
            ),
            patch.object(service.store, "load_export_artifact", return_value=saved),
            patch.object(
                service.store,
                "load_project",
                return_value={"owner_key": "email:owner@example.com"},
            ),
            patch.object(service, "_visible_to_owner", return_value=True),
        ):
            metadata = service.get_export_artifact(
                artifact["export_artifact_id"],
                {"email": "owner@example.com"},
            )
        self.assertEqual("READY", metadata["status"])
        self.assertNotIn("created_by", metadata)

        with (
            patch.object(
                service.store, "locate_export_artifact", return_value=locator
            ),
            patch.object(service.store, "load_export_artifact", return_value=saved),
            patch.object(
                service.store,
                "load_project",
                return_value={"owner_key": "email:owner@example.com"},
            ),
            patch.object(service, "_visible_to_owner", return_value=True),
            patch.object(
                service.store,
                "load_export_artifact_bytes",
                return_value={**saved, "content": b"persisted"},
            ) as bytes_mock,
            patch.object(service, "markdown_to_docx") as render_mock,
        ):
            download = service.get_export_download(
                artifact["export_artifact_id"],
                {"email": "owner@example.com"},
            )
        self.assertEqual(b"persisted", download["content"])
        bytes_mock.assert_called_once_with(artifact["export_artifact_id"])
        render_mock.assert_not_called()

        with (
            patch.object(
                service.store, "locate_export_artifact", return_value=locator
            ),
            patch.object(
                service.store,
                "load_export_artifact",
                side_effect=ValueError("corrupt private metadata"),
            ) as metadata_mock,
            patch.object(
                service.store,
                "load_project",
                return_value={"owner_key": "email:owner@example.com"},
            ),
            patch.object(service, "_visible_to_owner", return_value=False),
            patch.object(service.store, "load_export_artifact_bytes") as bytes_mock,
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.get_export_download(
                    artifact["export_artifact_id"],
                    {"email": "other@example.com"},
                )
        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("INTERVIEW_EXPORT_NOT_FOUND", raised.exception.code)
        metadata_mock.assert_not_called()
        bytes_mock.assert_not_called()

    def test_request_contract_and_corrupt_download_are_rejected(self):
        with self.assertRaises(InterviewV2ImportError) as raised:
            service.create_export(
                REPORT,
                {"format": "pdf", "include_evidence_appendix": True},
                None,
            )
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("EXPORT_REQUEST_INVALID", raised.exception.code)

        with patch.object(service.store, "locate_export_artifact") as locate:
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.get_export_artifact("export_../../private", None)
        self.assertEqual(400, raised.exception.status_code)
        self.assertEqual("EXPORT_REQUEST_INVALID", raised.exception.code)
        locate.assert_not_called()

        artifact_id = "export_" + "f" * 32
        saved = {
            "project_id": PROJECT,
            "artifact": {"export_artifact_id": artifact_id},
        }
        with (
            patch.object(
                service.store,
                "locate_export_artifact",
                return_value={
                    "project_id": PROJECT,
                    "export_artifact_id": artifact_id,
                },
            ),
            patch.object(service.store, "load_export_artifact", return_value=saved),
            patch.object(service.store, "load_project", return_value={}),
            patch.object(service, "_visible_to_owner", return_value=True),
            patch.object(
                service.store,
                "load_export_artifact_bytes",
                side_effect=ValueError("digest mismatch"),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.get_export_download(artifact_id, None)
        self.assertEqual("EXPORT_PERSISTENCE_FAILED", raised.exception.code)
        self.assertTrue(raised.exception.retryable)

        with patch.object(
            service.store,
            "locate_export_artifact",
            side_effect=ValueError(
                "export artifact locator integrity check failed"
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as raised:
                service.get_export_artifact(artifact_id, None)
        self.assertEqual(404, raised.exception.status_code)
        self.assertEqual("INTERVIEW_EXPORT_NOT_FOUND", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
