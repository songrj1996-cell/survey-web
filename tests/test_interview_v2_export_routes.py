import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.routers import interview_v2
from app.schemas.interview_v2_export import InterviewV2ExportArtifactResponse
from app.services.interview_v2_import_service import InterviewV2ImportError


REPORT_ID = "report_" + "1" * 32
ARTIFACT_ID = "export_" + "2" * 32
LOGIN = {"email": "owner@example.com"}


def _request(
    *,
    method: str = "GET",
    path: str = "/",
    body: bytes = b"",
    content_type: str = "application/json",
) -> Request:
    headers = []
    if content_type:
        headers.append((b"content-type", content_type.encode("ascii")))
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
    }
    reads = {"count": 0, "sent": False}

    async def receive():
        reads["count"] += 1
        if not reads["sent"]:
            reads["sent"] = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    request = Request(scope, receive)
    request.state.receive_reads = reads
    return request


def _artifact() -> dict:
    section_keys = (
        "scope_and_sample",
        "core_findings",
        "module_findings",
        "participant_differences",
        "participant_logics",
        "recommendations",
        "evidence_and_limitations",
    )
    export_profile = {
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
    return {
        "export_artifact_id": ARTIFACT_ID,
        "project_id": "project_" + "3" * 32,
        "report_version_id": REPORT_ID,
        "report_version_number": 4,
        "status": "READY",
        "format": "docx",
        "export_profile": export_profile,
        "manifest": {
            "schema_version": "interview-report-export/1.0",
            "format": "docx",
            "export_profile_version": export_profile["profile_version"],
            "report_version_id": REPORT_ID,
            "report_version_number": 4,
            "report_revision_payload_sha256": "6" * 64,
            "approval_status": "approved",
            "approved_at": "2026-09-02T08:00:00Z",
            "report_body_sha256": "8" * 64,
            "section_manifest": [
                {
                    "section_key": key,
                    "section_revision": 1,
                    "content_sha256": format(index, "064x"),
                }
                for index, key in enumerate(section_keys, 1)
            ],
            "appendix_sha256": "9" * 64,
            "document_markdown_sha256": "a" * 64,
            "section_count": 7,
            "claim_count": 3,
            "evidence_count": 4,
            "appendix_entry_count": 4,
        },
        "manifest_sha256": "5" * 64,
        "report_revision_payload_sha256": "6" * 64,
        "content_sha256": "7" * 64,
        "byte_size": 12,
        "file_name": "访谈研究报告-v4.docx",
        "media_type": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "download_url": f"/api/v1/interview-export-artifacts/{ARTIFACT_ID}/download",
        "created_at": "2026-09-02T08:01:00Z",
    }


def _json_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


class InterviewV2ExportRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_routes_are_registered_without_fastapi_body_dependencies(self):
        routes = {route.path: route for route in interview_v2.router.routes}
        expected_paths = {
            "/api/v1/interview-reports/{report_version_id}/exports",
            "/api/v1/interview-export-artifacts/{artifact_id}",
            "/api/v1/interview-export-artifacts/{artifact_id}/download",
        }
        self.assertTrue(expected_paths.issubset(routes))
        self.assertEqual(
            [],
            routes[
                "/api/v1/interview-reports/{report_version_id}/exports"
            ].dependant.body_params,
        )

    def test_create_route_applies_response_model_in_asgi(self):
        app = FastAPI()
        app.include_router(interview_v2.router)
        artifact = _artifact()
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "create_export", return_value=artifact),
            patch.object(interview_v2, "audit_log", new=AsyncMock()),
            TestClient(app) as client,
        ):
            response = client.post(
                f"/api/v1/interview-reports/{REPORT_ID}/exports",
                json={"format": "docx", "include_evidence_appendix": True},
            )
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(artifact, response.json())
        InterviewV2ExportArtifactResponse.model_validate(response.json())

    async def test_create_authenticates_before_reading_request_body(self):
        request = _request(
            method="POST",
            path=f"/api/v1/interview-reports/{REPORT_ID}/exports",
            body=b'{"private":"must-not-be-read"}',
        )
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "create_export") as create,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.create_interview_v2_report_export(
                REPORT_ID, request
            )
        self.assertEqual(0, request.state.receive_reads["count"])
        create.assert_not_called()

    async def test_create_accepts_only_fixed_docx_appendix_contract(self):
        payload = {"format": "docx", "include_evidence_appendix": True}
        request = _request(
            method="POST",
            path=f"/api/v1/interview-reports/{REPORT_ID}/exports",
            body=json.dumps(payload).encode("utf-8"),
        )
        artifact = _artifact()
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2, "create_export", return_value=artifact
            ) as create,
            patch.object(
                interview_v2, "audit_log", new=AsyncMock()
            ) as audit,
        ):
            response = await interview_v2.create_interview_v2_report_export(
                REPORT_ID, request
            )

        self.assertEqual(artifact, response)
        create.assert_called_once_with(REPORT_ID, payload, LOGIN)
        audit.assert_awaited_once()
        audit_text = repr(audit.await_args)
        self.assertIn(ARTIFACT_ID, audit_text)
        self.assertIn(REPORT_ID, audit_text)
        self.assertNotIn("研究报告", audit_text)
        self.assertNotIn("manifest", audit_text)

    async def test_create_rejects_extra_or_unsupported_input(self):
        for payload in (
            {"format": "pdf", "include_evidence_appendix": True},
            {
                "format": "docx",
                "include_evidence_appendix": True,
                "report_text": "private",
            },
        ):
            with self.subTest(payload=payload):
                request = _request(
                    method="POST",
                    path=f"/api/v1/interview-reports/{REPORT_ID}/exports",
                    body=json.dumps(payload).encode("utf-8"),
                )
                with (
                    patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
                    patch.object(
                        interview_v2,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ),
                    patch.object(interview_v2, "create_export") as create,
                ):
                    response = (
                        await interview_v2.create_interview_v2_report_export(
                            REPORT_ID, request
                        )
                    )
                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    "EXPORT_REQUEST_INVALID",
                    _json_body(response)["error"]["code"],
                )
                self.assertNotIn("private", repr(_json_body(response)))
                create.assert_not_called()

    async def test_metadata_lookup_uses_owner_scoped_service(self):
        request = _request(
            path=f"/api/v1/interview-export-artifacts/{ARTIFACT_ID}"
        )
        artifact = _artifact()
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ) as auth,
            patch.object(
                interview_v2, "get_export_artifact", return_value=artifact
            ) as get_artifact,
        ):
            response = await interview_v2.get_interview_v2_export_artifact(
                ARTIFACT_ID, request
            )
        self.assertEqual(artifact, response)
        auth.assert_awaited_once_with(request, "interview")
        get_artifact.assert_called_once_with(ARTIFACT_ID, LOGIN)

    async def test_download_returns_persisted_bytes_and_safe_audit_metadata(self):
        request = _request(
            path=f"/api/v1/interview-export-artifacts/{ARTIFACT_ID}/download"
        )
        artifact = _artifact()
        result = {
            "artifact": artifact,
            "content": b"fixed-docx",
            "file_name": artifact["file_name"],
            "media_type": artifact["media_type"],
        }
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2, "get_export_download", return_value=result
            ) as download,
            patch.object(
                interview_v2, "audit_log", new=AsyncMock()
            ) as audit,
        ):
            response = (
                await interview_v2.download_interview_v2_export_artifact(
                    ARTIFACT_ID, request
                )
            )

        chunks = [chunk async for chunk in response.body_iterator]
        self.assertEqual(b"fixed-docx", b"".join(chunks))
        self.assertEqual(str(len(b"fixed-docx")), response.headers["content-length"])
        self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
        self.assertEqual("private, no-store", response.headers["cache-control"])
        self.assertEqual("no-cache", response.headers["pragma"])
        download.assert_called_once_with(ARTIFACT_ID, LOGIN)
        audit.assert_awaited_once()
        audit_text = repr(audit.await_args)
        self.assertNotIn("研究报告", audit_text)
        self.assertNotIn("fixed-docx", audit_text)

    async def test_service_errors_keep_sanitized_v2_envelope(self):
        request = _request(path=f"/api/v1/interview-export-artifacts/{ARTIFACT_ID}")
        error = InterviewV2ImportError(
            status_code=404,
            code="INTERVIEW_EXPORT_NOT_FOUND",
            message="导出制品不存在。",
            suggested_action="return_to_report",
            context={},
        )
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2, "get_export_artifact", side_effect=error
            ),
        ):
            response = await interview_v2.get_interview_v2_export_artifact(
                ARTIFACT_ID, request
            )
        self.assertEqual(404, response.status_code)
        self.assertEqual(
            "INTERVIEW_EXPORT_NOT_FOUND", _json_body(response)["error"]["code"]
        )


if __name__ == "__main__":
    unittest.main()
