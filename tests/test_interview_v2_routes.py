import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import BackgroundTasks, HTTPException
from starlette.requests import Request

from app.routers import interview_v2
from app.schemas.interview_v2 import InterviewV2UploadAttemptResponse


def _request(
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    request_id: str = "trace_test",
) -> Request:
    raw_headers = {"x-request-id": request_id, **(headers or {})}
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/v1/interview-upload-attempts",
        "headers": [
            (name.lower().encode("ascii"), value.encode("latin-1"))
            for name, value in raw_headers.items()
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


def _multipart_body(
    file_content: bytes = b"xlsx",
    *,
    research_focus: str = "",
    file_contract_version: str = "interview-file-contract/1.0-draft",
    contract_acknowledged: str = "true",
) -> tuple[bytes, str]:
    boundary = "interview-v2-test-boundary"
    chunks: list[bytes] = []

    def add_field(name: str, value: str) -> None:
        chunks.extend(
            [
                f"--{boundary}\r\n".encode("ascii"),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(
                    "ascii"
                ),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    add_field("research_focus", research_focus)
    add_field("file_contract_version", file_contract_version)
    add_field("contract_acknowledged", contract_acknowledged)
    chunks.extend(
        [
            f"--{boundary}\r\n".encode("ascii"),
            b'Content-Disposition: form-data; name="file"; filename="records.xlsx"\r\n',
            b"Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n",
            file_content,
            b"\r\n",
            f"--{boundary}--\r\n".encode("ascii"),
        ]
    )
    return b"".join(chunks), boundary


def _upload_request(
    file_content: bytes = b"xlsx",
    *,
    research_focus: str = "",
    include_idempotency_key: bool = True,
    include_content_length: bool = True,
) -> Request:
    body, boundary = _multipart_body(
        file_content,
        research_focus=research_focus,
    )
    headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    if include_idempotency_key:
        headers["idempotency-key"] = "idem-01"
    if include_content_length:
        headers["content-length"] = str(len(body))
    return _request(method="POST", body=body, headers=headers)


def _response_body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


class InterviewV2RouteTests(unittest.IsolatedAsyncioTestCase):
    def test_response_schema_drops_internal_storage_and_owner_fields(self):
        public = InterviewV2UploadAttemptResponse.model_validate(
            {
                "upload_attempt_id": "upload_01",
                "status": "QUARANTINED",
                "owner_email": "owner@example.com",
                "storage_path": "private/source.xlsx",
                "raw_value": "private interview text",
            }
        ).model_dump()
        self.assertNotIn("owner_email", public)
        self.assertNotIn("storage_path", public)
        self.assertNotIn("raw_value", public)

    async def test_upload_authenticates_before_reading_file(self):
        denied = HTTPException(status_code=403, detail="当前账号没有此功能权限")
        request = _upload_request()

        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ) as require,
            patch.object(interview_v2, "create_upload_attempt") as create_attempt,
        ):
            with self.assertRaises(HTTPException):
                await interview_v2.create_interview_upload_attempt(
                    request,
                    BackgroundTasks(),
                )

        require.assert_awaited_once()
        self.assertEqual(require.await_args.args[1], "interview")
        self.assertEqual(request.state.receive_reads["count"], 0)
        create_attempt.assert_not_called()

    async def test_disabled_feature_returns_stable_error_before_file_read(self):
        request = _upload_request()

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", False),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "create_upload_attempt") as create_attempt,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(_response_body(response)["error"], {
            "code": "INTERVIEW_V2_DISABLED",
            "message": "访谈报告 V2 当前未启用。",
            "retryable": False,
            "suggested_action": "contact_administrator",
            "context": {},
            "trace_id": "trace_test",
        })
        self.assertEqual(request.state.receive_reads["count"], 0)
        create_attempt.assert_not_called()

    def test_upload_route_declares_no_body_dependencies(self):
        route = next(
            route
            for route in interview_v2.router.routes
            if getattr(route, "path", "") == "/api/v1/interview-upload-attempts"
        )
        self.assertIsNone(route.body_field)

    async def test_missing_idempotency_key_is_rejected_before_body_read(self):
        request = _upload_request(include_idempotency_key=False)
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "create_upload_attempt") as create_attempt,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            _response_body(response)["error"]["code"],
            "IDEMPOTENCY_KEY_INVALID",
        )
        self.assertEqual(request.state.receive_reads["count"], 0)
        create_attempt.assert_not_called()

    async def test_upload_is_bounded_and_schedules_precheck(self):
        request = _upload_request(research_focus="关注匹配体验")
        login = {"email": "owner@example.com"}
        result = {
            "upload_attempt_id": "upload_01",
            "job_id": "job_01",
            "status": "QUARANTINED",
            "project_id": None,
            "import_id": None,
            "workbook_revision_id": None,
        }
        background_tasks = BackgroundTasks()

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(interview_v2, "INTERVIEW_V2_MAX_FILE_BYTES", 10),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=login),
            ),
            patch.object(
                interview_v2,
                "create_upload_attempt",
                return_value=(result, True),
            ) as create_attempt,
            patch.object(interview_v2, "run_upload_precheck") as precheck,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                background_tasks,
            )

        self.assertIs(response, result)
        self.assertGreater(request.state.receive_reads["count"], 0)
        create_attempt.assert_called_once_with(
            filename="records.xlsx",
            content=b"xlsx",
            login=login,
            research_focus="关注匹配体验",
            file_contract_version="interview-file-contract/1.0-draft",
            contract_acknowledged=True,
            idempotency_key="idem-01",
        )
        self.assertEqual(len(background_tasks.tasks), 1)
        self.assertIs(background_tasks.tasks[0].func, precheck)
        self.assertEqual(background_tasks.tasks[0].args, ("upload_01",))

    async def test_research_focus_accepts_four_thousand_multibyte_characters(self):
        focus = "重" * 4000
        request = _upload_request(research_focus=focus)
        result = {"upload_attempt_id": "upload_01", "status": "QUARANTINED"}

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(
                interview_v2,
                "create_upload_attempt",
                return_value=(result, False),
            ) as create_attempt,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertIs(response, result)
        self.assertEqual(create_attempt.call_args.kwargs["research_focus"], focus)

    async def test_research_focus_over_four_thousand_characters_is_explicit(self):
        request = _upload_request(research_focus="重" * 4001)
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "create_upload_attempt") as create_attempt,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            _response_body(response)["error"]["code"],
            "RESEARCH_FOCUS_TOO_LONG",
        )
        create_attempt.assert_not_called()

    async def test_oversized_upload_returns_structured_error_without_service_call(self):
        request = _upload_request(b"123456")

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(interview_v2, "INTERVIEW_V2_MAX_FILE_BYTES", 5),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "create_upload_attempt") as create_attempt,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertEqual(response.status_code, 413)
        error = _response_body(response)["error"]
        self.assertEqual(error["code"], "WORKBOOK_LIMIT_EXCEEDED")
        self.assertEqual(error["context"], {"limit_bytes": 5})
        self.assertEqual(error["trace_id"], "trace_test")
        create_attempt.assert_not_called()

    async def test_chunked_request_body_is_bounded_before_multipart_spooling(self):
        request = _upload_request(
            b"123456",
            include_content_length=False,
        )
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(interview_v2, "INTERVIEW_V2_MAX_FILE_BYTES", 5),
            patch.object(interview_v2, "_MULTIPART_OVERHEAD_BYTES", 16),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "create_upload_attempt") as create_attempt,
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            _response_body(response)["error"]["code"],
            "WORKBOOK_LIMIT_EXCEEDED",
        )
        self.assertGreater(request.state.receive_reads["count"], 0)
        create_attempt.assert_not_called()

    async def test_idempotent_upload_does_not_schedule_duplicate_precheck(self):
        request = _upload_request()
        result = {
            "upload_attempt_id": "upload_existing",
            "status": "PRECHECKING",
        }
        background_tasks = BackgroundTasks()

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(
                interview_v2,
                "create_upload_attempt",
                return_value=(result, False),
            ),
            patch.object(interview_v2, "run_upload_precheck"),
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                background_tasks,
            )

        self.assertIs(response, result)
        self.assertEqual(background_tasks.tasks, [])

    async def test_service_error_is_returned_in_v2_error_envelope(self):
        request = _upload_request()
        error = interview_v2.InterviewV2ImportError(
            status_code=409,
            code="IDEMPOTENCY_KEY_CONFLICT",
            message="同一幂等键已用于其他上传内容。",
            retryable=False,
            suggested_action="use_new_idempotency_key",
            context={"upload_attempt_id": "upload_existing"},
            trace_id="trace_service",
        )

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(
                interview_v2,
                "create_upload_attempt",
                side_effect=error,
            ),
        ):
            response = await interview_v2.create_interview_upload_attempt(
                request,
                BackgroundTasks(),
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(_response_body(response)["error"], {
            "code": "IDEMPOTENCY_KEY_CONFLICT",
            "message": "同一幂等键已用于其他上传内容。",
            "retryable": False,
            "suggested_action": "use_new_idempotency_key",
            "context": {"upload_attempt_id": "upload_existing"},
            "trace_id": "trace_service",
        })

    async def test_upload_attempt_query_passes_authenticated_owner_to_service(self):
        login = {"email": "owner@example.com"}
        result = {"upload_attempt_id": "upload_01", "status": "ACCEPTED"}

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=login),
            ) as require,
            patch.object(
                interview_v2,
                "get_upload_attempt",
                return_value=result,
            ) as get_attempt,
            patch.object(
                interview_v2,
                "upload_attempt_needs_precheck",
                return_value=False,
            ) as needs_precheck,
        ):
            response = await interview_v2.get_interview_upload_attempt(
                "upload_01",
                _request(),
                BackgroundTasks(),
            )

        self.assertIs(response, result)
        require.assert_awaited_once()
        get_attempt.assert_called_once_with("upload_01", login)
        needs_precheck.assert_called_once_with("upload_01", login)

    async def test_upload_attempt_query_reschedules_recoverable_precheck(self):
        login = {"email": "owner@example.com"}
        result = {"upload_attempt_id": "upload_01", "status": "QUARANTINED"}
        background_tasks = BackgroundTasks()

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=login),
            ),
            patch.object(interview_v2, "get_upload_attempt", return_value=result),
            patch.object(
                interview_v2,
                "upload_attempt_needs_precheck",
                return_value=True,
            ),
            patch.object(interview_v2, "run_upload_precheck") as precheck,
        ):
            response = await interview_v2.get_interview_upload_attempt(
                "upload_01",
                _request(),
                background_tasks,
            )

        self.assertIs(response, result)
        self.assertEqual(len(background_tasks.tasks), 1)
        self.assertIs(background_tasks.tasks[0].func, precheck)
        self.assertEqual(background_tasks.tasks[0].args, ("upload_01",))

    async def test_upload_attempt_query_reschedules_stale_precheck(self):
        login = {"email": "owner@example.com"}
        result = {"upload_attempt_id": "upload_01", "status": "PRECHECKING"}
        background_tasks = BackgroundTasks()

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=login),
            ),
            patch.object(interview_v2, "get_upload_attempt", return_value=result),
            patch.object(
                interview_v2,
                "upload_attempt_needs_precheck",
                return_value=True,
            ) as needs_precheck,
            patch.object(interview_v2, "run_upload_precheck") as precheck,
        ):
            response = await interview_v2.get_interview_upload_attempt(
                "upload_01",
                _request(),
                background_tasks,
            )

        self.assertIs(response, result)
        needs_precheck.assert_called_once_with("upload_01", login)
        self.assertEqual(len(background_tasks.tasks), 1)
        self.assertIs(background_tasks.tasks[0].func, precheck)

    async def test_import_query_passes_authenticated_owner_to_service(self):
        login = {"email": "owner@example.com"}
        result = {
            "import_id": "import_01",
            "project_id": "project_01",
            "workbook_revision_id": "workbook_01",
            "status": "GROUP_CONFIRMATION_REQUIRED",
        }

        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value=login),
            ),
            patch.object(
                interview_v2,
                "get_interview_import",
                return_value=result,
            ) as get_import,
        ):
            response = await interview_v2.get_interview_import_status(
                "import_01",
                _request(),
            )

        self.assertIs(response, result)
        get_import.assert_called_once_with("import_01", login)


if __name__ == "__main__":
    unittest.main()
