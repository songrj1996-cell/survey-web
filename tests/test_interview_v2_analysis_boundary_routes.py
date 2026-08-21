import inspect
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.routers import interview_v2
from app.services.interview_v2_import_service import InterviewV2ImportError


IMPORT_ID = "import_" + "2" * 32
STRUCTURE_ID = "structure_" + "3" * 32
EVIDENCE_ID = "evidence_" + "4" * 32
BOUNDARY_ID = "boundary_" + "5" * 32
COVERAGE_ID = "coverage_" + "6" * 32
BOUNDARY_SHA = "c" * 64
COVERAGE_SHA = "d" * 64
LOGIN = {"email": "owner@example.com"}


def _request(
    *,
    method: str = "PUT",
    path: str = "/api/v1/interview-imports/x/analysis-boundary",
    body: bytes = b"{}",
    content_length: int | None = None,
    content_type: str = "application/json",
) -> Request:
    headers = {
        "content-type": content_type,
        "x-request-id": "trace_analysis_boundary",
    }
    if content_length is not None:
        headers["content-length"] = str(content_length)
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [
            (key.encode("ascii"), value.encode("ascii"))
            for key, value in headers.items()
        ],
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


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _put_payload() -> dict:
    return {
        "base_boundary_revision_id": None,
        "base_coverage_revision_id": None,
        "base_structure_revision_id": STRUCTURE_ID,
        "base_evidence_revision_id": EVIDENCE_ID,
        "evaluation_objects": [],
        "source_scope_rules": [],
        "label_scope_rules": [],
        "change_reason": "确认边界",
    }


def _confirm_payload() -> dict:
    return {
        "boundary_revision_id": BOUNDARY_ID,
        "coverage_revision_id": COVERAGE_ID,
        "boundary_payload_sha256": BOUNDARY_SHA,
        "coverage_payload_sha256": COVERAGE_SHA,
    }


class InterviewV2AnalysisBoundaryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_put_authenticates_before_reading_json(self):
        request = _request(body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "save_analysis_boundary") as save,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.put_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(request.state.receive_reads["count"], 0)
        save.assert_not_called()

    async def test_confirm_authenticates_before_reading_json(self):
        request = _request(
            method="POST",
            path=(
                "/api/v1/interview-imports/x/analysis-boundary:confirm"
            ),
            body=b'{"secret":"must-not-be-read"}',
        )
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "confirm_analysis_boundary") as confirm,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.confirm_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(request.state.receive_reads["count"], 0)
        confirm.assert_not_called()

    async def test_disabled_feature_rejects_write_before_body_read(self):
        for endpoint, service_name, method, path in (
            (
                interview_v2.put_interview_analysis_boundary,
                "save_analysis_boundary",
                "PUT",
                "/api/v1/interview-imports/x/analysis-boundary",
            ),
            (
                interview_v2.confirm_interview_analysis_boundary,
                "confirm_analysis_boundary",
                "POST",
                "/api/v1/interview-imports/x/analysis-boundary:confirm",
            ),
        ):
            with self.subTest(endpoint=endpoint.__name__):
                request = _request(
                    method=method,
                    path=path,
                    body=b'{"secret":"must-not-be-read"}',
                )
                with (
                    patch.object(interview_v2, "INTERVIEW_V2_ENABLED", False),
                    patch.object(
                        interview_v2,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ),
                    patch.object(interview_v2, service_name) as service_call,
                ):
                    response = await endpoint(IMPORT_ID, request)

                self.assertEqual(response.status_code, 503)
                self.assertEqual(
                    _body(response)["error"]["code"], "INTERVIEW_V2_DISABLED"
                )
                self.assertEqual(request.state.receive_reads["count"], 0)
                service_call.assert_not_called()

    async def test_declared_oversize_is_rejected_before_body_read(self):
        request = _request(
            content_length=interview_v2._STRUCTURE_JSON_MAX_BYTES + 1
        )
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "save_analysis_boundary") as save,
        ):
            response = await interview_v2.put_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 413)
        error = _body(response)["error"]
        self.assertEqual(error["code"], "ANALYSIS_BOUNDARY_REQUEST_INVALID")
        self.assertEqual(
            error["context"]["limit_bytes"],
            interview_v2._STRUCTURE_JSON_MAX_BYTES,
        )
        self.assertEqual(request.state.receive_reads["count"], 0)
        save.assert_not_called()

    async def test_chunked_oversize_is_rejected_without_service_call(self):
        request = _request(body=b"{" + b" " * 32 + b"}")
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(interview_v2, "_STRUCTURE_JSON_MAX_BYTES", 16),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "save_analysis_boundary") as save,
        ):
            response = await interview_v2.put_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(_body(response)["error"]["context"]["limit_bytes"], 16)
        save.assert_not_called()

    async def test_put_strict_schema_rejects_unknown_fields_atomically(self):
        payload = _put_payload()
        payload["created_by"] = "email:attacker@example.com"
        request = _request(body=json.dumps(payload).encode("utf-8"))
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "save_analysis_boundary") as save,
        ):
            response = await interview_v2.put_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            _body(response)["error"]["code"],
            "ANALYSIS_BOUNDARY_REQUEST_INVALID",
        )
        save.assert_not_called()

    async def test_confirm_strict_schema_requires_paired_revision_digests(self):
        payload = _confirm_payload()
        payload.pop("coverage_payload_sha256")
        request = _request(
            method="POST",
            path="/api/v1/interview-imports/x/analysis-boundary:confirm",
            body=json.dumps(payload).encode("utf-8"),
        )
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "confirm_analysis_boundary") as confirm,
        ):
            response = await interview_v2.confirm_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            _body(response)["error"]["code"],
            "ANALYSIS_BOUNDARY_REQUEST_INVALID",
        )
        confirm.assert_not_called()

    async def test_valid_put_normalizes_payload_and_calls_service(self):
        payload = _put_payload()
        request = _request(body=json.dumps(payload).encode("utf-8"))
        expected = {"status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED"}
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2, "save_analysis_boundary", return_value=expected
            ) as save,
        ):
            response = await interview_v2.put_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response, expected)
        save.assert_called_once_with(IMPORT_ID, payload, LOGIN)

    async def test_valid_confirm_normalizes_payload_and_calls_service(self):
        payload = _confirm_payload()
        request = _request(
            method="POST",
            path="/api/v1/interview-imports/x/analysis-boundary:confirm",
            body=json.dumps(payload).encode("utf-8"),
        )
        expected = {"status": "READY_FOR_DOSSIERS"}
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2,
                "confirm_analysis_boundary",
                return_value=expected,
            ) as confirm,
        ):
            response = await interview_v2.confirm_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response, expected)
        confirm.assert_called_once_with(IMPORT_ID, payload, LOGIN)

    async def test_get_routes_authenticate_then_call_services(self):
        for endpoint, service_name in (
            (
                interview_v2.get_interview_analysis_boundary,
                "get_analysis_boundary",
            ),
            (
                interview_v2.get_interview_coverage_preview,
                "get_coverage_preview",
            ),
        ):
            with self.subTest(endpoint=endpoint.__name__):
                request = _request(method="GET", body=b"")
                with (
                    patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
                    patch.object(
                        interview_v2,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ) as auth,
                    patch.object(
                        interview_v2, service_name, return_value={"ok": True}
                    ) as call,
                ):
                    response = await endpoint(IMPORT_ID, request)

                self.assertEqual(response, {"ok": True})
                auth.assert_awaited_once_with(request, "interview")
                call.assert_called_once_with(IMPORT_ID, LOGIN)

    async def test_service_error_uses_standard_sanitized_envelope(self):
        payload = _put_payload()
        request = _request(body=json.dumps(payload).encode("utf-8"))
        error = InterviewV2ImportError(
            status_code=409,
            code="ANALYSIS_BOUNDARY_INPUT_CONFLICT",
            message="结构或证据已更新。",
            suggested_action="refresh_structure_review",
            context={"current_structure_revision_id": "structure_" + "9" * 32},
        )
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2, "save_analysis_boundary", side_effect=error
            ),
        ):
            response = await interview_v2.put_interview_analysis_boundary(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 409)
        envelope = _body(response)["error"]
        self.assertEqual(envelope["code"], "ANALYSIS_BOUNDARY_INPUT_CONFLICT")
        self.assertEqual(envelope["suggested_action"], "refresh_structure_review")
        self.assertEqual(
            envelope["context"]["current_structure_revision_id"],
            "structure_" + "9" * 32,
        )
        self.assertRegex(envelope["trace_id"], r"^trace_[0-9a-f]{32}$")
        self.assertNotIn("owner@example.com", repr(envelope))

    def test_write_routes_declare_no_body_dependencies(self):
        paths = {
            "/api/v1/interview-imports/{import_id}/analysis-boundary",
            "/api/v1/interview-imports/{import_id}/analysis-boundary:confirm",
        }
        routes = [
            route
            for route in interview_v2.router.routes
            if getattr(route, "path", "") in paths
            and "GET" not in getattr(route, "methods", set())
        ]
        self.assertEqual({route.path for route in routes}, paths)
        for route in routes:
            self.assertEqual(route.dependant.body_params, [])

    def test_all_analysis_boundary_io_runs_outside_event_loop(self):
        endpoints = (
            interview_v2.get_interview_analysis_boundary,
            interview_v2.put_interview_analysis_boundary,
            interview_v2.confirm_interview_analysis_boundary,
            interview_v2.get_interview_coverage_preview,
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint.__name__):
                self.assertIn(
                    "await run_in_threadpool(", inspect.getsource(endpoint)
                )


if __name__ == "__main__":
    unittest.main()
