import inspect
import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.routers import interview_v2


IMPORT_ID = "import_" + "2" * 32
ISSUE_ID = "issue_" + "4" * 32
MAPPING_ID = "mapping_" + "5" * 32
STRUCTURE_ID = "structure_" + "6" * 32
EVIDENCE_ID = "evidence_" + "7" * 32
LOGIN = {"email": "owner@example.com"}


def _request(
    *,
    method: str = "POST",
    body: bytes = b"{}",
    content_length: int | None = None,
    content_type: str = "application/json",
) -> Request:
    headers = {"content-type": content_type, "x-request-id": "trace_structure"}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/v1/interview-imports/x/structure:build",
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


def _patch_body() -> dict:
    return {
        "base_structure_revision_id": STRUCTURE_ID,
        "base_evidence_revision_id": EVIDENCE_ID,
        "resolution": "assign_row_role",
        "row_role": "follow_up",
        "comment": "确认这是现场追问",
    }


class InterviewV2StructureRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_authenticates_before_reading_json(self):
        request = _request(body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "build_structure") as build,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.build_interview_structure(IMPORT_ID, request)

        self.assertEqual(request.state.receive_reads["count"], 0)
        build.assert_not_called()

    async def test_patch_authenticates_before_reading_json(self):
        request = _request(method="PATCH", body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "resolve_review_issue") as resolve,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.patch_interview_review_issue(ISSUE_ID, request)

        self.assertEqual(request.state.receive_reads["count"], 0)
        resolve.assert_not_called()

    async def test_batch_authenticates_before_reading_json(self):
        request = _request(body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "resolve_review_issues_batch") as resolve,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.resolve_interview_review_issues_batch(
                IMPORT_ID, request
            )

        self.assertEqual(request.state.receive_reads["count"], 0)
        resolve.assert_not_called()

    async def test_disabled_feature_rejects_before_body_read(self):
        request = _request(body=b'{"secret":"must-not-be-read"}')
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", False),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "build_structure") as build,
        ):
            response = await interview_v2.build_interview_structure(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(_body(response)["error"]["code"], "INTERVIEW_V2_DISABLED")
        self.assertEqual(request.state.receive_reads["count"], 0)
        build.assert_not_called()

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
            patch.object(interview_v2, "build_structure") as build,
        ):
            response = await interview_v2.build_interview_structure(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(_body(response)["error"]["code"], "STRUCTURE_REQUEST_INVALID")
        self.assertEqual(request.state.receive_reads["count"], 0)
        build.assert_not_called()

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
            patch.object(interview_v2, "build_structure") as build,
        ):
            response = await interview_v2.build_interview_structure(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(_body(response)["error"]["context"]["limit_bytes"], 16)
        build.assert_not_called()

    async def test_build_validates_frozen_mapping_head_and_calls_service(self):
        payload = {
            "base_mapping_revision_id": MAPPING_ID,
            "base_mapping_sha256": "a" * 64,
        }
        request = _request(body=json.dumps(payload).encode("utf-8"))
        expected = {"status": "READY_FOR_DOSSIERS"}
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                interview_v2, "build_structure", return_value=expected
            ) as build,
        ):
            result = await interview_v2.build_interview_structure(
                IMPORT_ID, request
            )

        self.assertEqual(result, expected)
        build.assert_called_once_with(IMPORT_ID, payload, LOGIN)

    async def test_patch_validates_contract_and_calls_service(self):
        payload = _patch_body()
        request = _request(
            method="PATCH", body=json.dumps(payload).encode("utf-8")
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
                interview_v2, "resolve_review_issue", return_value=expected
            ) as resolve,
        ):
            result = await interview_v2.patch_interview_review_issue(
                ISSUE_ID, request
            )

        self.assertEqual(result, expected)
        normalized = resolve.call_args.args[1]
        self.assertEqual(normalized["resolution"], "assign_row_role")
        self.assertEqual(normalized["row_role"], "follow_up")
        self.assertIsNone(normalized["target_id"])
        self.assertIsNone(normalized["evidence_type"])
        resolve.assert_called_once()
        self.assertEqual(resolve.call_args.args[0], ISSUE_ID)
        self.assertEqual(resolve.call_args.args[2], LOGIN)

    async def test_duplicate_batch_issue_ids_are_rejected_atomically(self):
        resolution = {"issue_id": ISSUE_ID, **_patch_body()}
        resolution.pop("base_structure_revision_id")
        resolution.pop("base_evidence_revision_id")
        payload = {
            "base_structure_revision_id": STRUCTURE_ID,
            "base_evidence_revision_id": EVIDENCE_ID,
            "resolutions": [resolution, resolution],
        }
        request = _request(body=json.dumps(payload).encode("utf-8"))
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(interview_v2, "resolve_review_issues_batch") as resolve,
        ):
            response = await interview_v2.resolve_interview_review_issues_batch(
                IMPORT_ID, request
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(_body(response)["error"]["code"], "STRUCTURE_REQUEST_INVALID")
        resolve.assert_not_called()

    async def test_get_routes_authenticate_then_call_services(self):
        cases = [
            (interview_v2.get_interview_structure, interview_v2, "get_structure", IMPORT_ID),
            (
                interview_v2.get_interview_review_issues,
                interview_v2,
                "get_review_issues",
                IMPORT_ID,
            ),
            (
                interview_v2.get_interview_evidence_context,
                interview_v2,
                "get_evidence_context",
                "ev_" + "8" * 32,
            ),
        ]
        for endpoint, owner, service_name, resource_id in cases:
            with self.subTest(endpoint=endpoint.__name__):
                request = _request(method="GET", body=b"")
                with (
                    patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
                    patch.object(
                        interview_v2,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ),
                    patch.object(owner, service_name, return_value={"ok": True}) as call,
                ):
                    result = await endpoint(resource_id, request)
                self.assertEqual(result, {"ok": True})
                call.assert_called_once_with(resource_id, LOGIN)

    def test_structure_write_routes_declare_no_body_dependencies(self):
        paths = {
            "/api/v1/interview-imports/{import_id}/structure:build",
            "/api/v1/interview-review-issues/{issue_id}",
            "/api/v1/interview-imports/{import_id}/review-issues:resolve-batch",
        }
        routes = {
            route.path: route
            for route in interview_v2.router.routes
            if getattr(route, "path", "") in paths
        }
        self.assertEqual(set(routes), paths)
        for route in routes.values():
            self.assertEqual(route.dependant.body_params, [])

    def test_structure_io_is_dispatched_outside_the_event_loop(self):
        endpoints = (
            interview_v2.get_interview_import_status,
            interview_v2.build_interview_structure,
            interview_v2.get_interview_structure,
            interview_v2.get_interview_review_issues,
            interview_v2.patch_interview_review_issue,
            interview_v2.resolve_interview_review_issues_batch,
            interview_v2.get_interview_evidence_context,
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint.__name__):
                self.assertIn(
                    "await run_in_threadpool(", inspect.getsource(endpoint)
                )


if __name__ == "__main__":
    unittest.main()
