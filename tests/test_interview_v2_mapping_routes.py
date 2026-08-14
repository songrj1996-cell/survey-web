import json
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app.routers import interview_v2


IMPORT_ID = "import_" + "2" * 32


def _request(
    *,
    method: str = "PUT",
    body: bytes = b"{}",
    content_length: int | None = None,
    content_type: str = "application/json",
) -> Request:
    headers = {"content-type": content_type}
    if content_length is not None:
        headers["content-length"] = str(content_length)
    scope = {
        "type": "http",
        "method": method,
        "path": "/api/v1/interview-imports/x/group-mapping",
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


class InterviewV2MappingRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_put_authenticates_before_reading_body(self):
        request = _request(body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "save_group_mapping") as save,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.put_interview_group_mapping(IMPORT_ID, request)
        self.assertEqual(request.state.receive_reads["count"], 0)
        save.assert_not_called()

    async def test_confirm_authenticates_before_reading_body(self):
        request = _request(method="POST", body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "confirm_group_mapping") as confirm,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.confirm_interview_group_mapping(IMPORT_ID, request)
        self.assertEqual(request.state.receive_reads["count"], 0)
        confirm.assert_not_called()

    async def test_restore_authenticates_before_reading_body(self):
        request = _request(method="POST", body=b'{"secret":"must-not-be-read"}')
        denied = HTTPException(status_code=403, detail="无权限")
        with (
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(side_effect=denied),
            ),
            patch.object(interview_v2, "restore_group_mapping") as restore,
            self.assertRaises(HTTPException),
        ):
            await interview_v2.restore_interview_group_mapping(IMPORT_ID, request)
        self.assertEqual(request.state.receive_reads["count"], 0)
        restore.assert_not_called()

    async def test_oversized_put_is_rejected_before_body_read_or_service(self):
        request = _request(
            content_length=interview_v2._MAPPING_JSON_MAX_BYTES + 1
        )
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "save_group_mapping") as save,
        ):
            response = await interview_v2.put_interview_group_mapping(
                IMPORT_ID, request
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(_body(response)["error"]["code"], "MAPPING_REQUEST_INVALID")
        self.assertEqual(request.state.receive_reads["count"], 0)
        save.assert_not_called()

    async def test_chunked_oversized_put_is_rejected_without_service_call(self):
        request = _request(body=b"{" + b" " * 32 + b"}")
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(interview_v2, "_MAPPING_JSON_MAX_BYTES", 16),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "save_group_mapping") as save,
        ):
            response = await interview_v2.put_interview_group_mapping(
                IMPORT_ID, request
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(_body(response)["error"]["context"]["limit_bytes"], 16)
        save.assert_not_called()

    async def test_invalid_json_uses_v2_error_envelope(self):
        request = _request(body=b'{"raw_value":"private"')
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "save_group_mapping") as save,
        ):
            response = await interview_v2.put_interview_group_mapping(
                IMPORT_ID, request
            )
        self.assertEqual(response.status_code, 422)
        body = _body(response)
        self.assertEqual(body["error"]["code"], "MAPPING_REQUEST_INVALID")
        self.assertNotIn("private", repr(body))
        save.assert_not_called()

    async def test_non_json_content_type_is_rejected_without_service_call(self):
        request = _request(body=b"{}", content_type="text/plain")
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(interview_v2, "save_group_mapping") as save,
        ):
            response = await interview_v2.put_interview_group_mapping(
                IMPORT_ID, request
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(_body(response)["error"]["code"], "MAPPING_REQUEST_INVALID")
        save.assert_not_called()

    async def test_documented_json_contract_reaches_service_with_internal_keys(self):
        body = {
            "base_mapping_revision": 0,
            "groups": [
                {
                    "display_name": "第1组",
                    "sheets": [
                        {
                            "sheet_id": "sheet_001",
                            "role": "record",
                            "recorder_label": "记录1",
                        }
                    ],
                    "participant_bindings": [
                        {
                            "participant_label": "P01",
                            "columns": [{"sheet_id": "sheet_001", "column": 4}],
                        }
                    ],
                }
            ],
            "ignored_sheet_ids": [],
        }
        request = _request(body=json.dumps(body).encode("utf-8"))
        expected = {"status": "GROUP_CONFIRMATION_REQUIRED"}
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(
                interview_v2, "save_group_mapping", return_value=expected
            ) as save,
        ):
            result = await interview_v2.put_interview_group_mapping(
                IMPORT_ID, request
            )
        self.assertEqual(result, expected)
        payload = save.call_args.args[1]
        self.assertIn("participants", payload["groups"][0])
        self.assertEqual(
            payload["groups"][0]["participants"][0]["columns"][0][
                "column_index"
            ],
            4,
        )

    async def test_restore_contract_reaches_service(self):
        body = {
            "base_mapping_revision": 2,
            "target_mapping_revision_id": "mapping_" + "a" * 32,
            "target_mapping_sha256": "b" * 64,
            "change_kind": "undo",
            "change_reason": "撤销上一版",
        }
        request = _request(
            method="POST", body=json.dumps(body).encode("utf-8")
        )
        expected = {"status": "GROUP_CONFIRMATION_REQUIRED"}
        with (
            patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
            patch.object(
                interview_v2,
                "_require_feature",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(
                interview_v2, "restore_group_mapping", return_value=expected
            ) as restore,
        ):
            result = await interview_v2.restore_interview_group_mapping(
                IMPORT_ID, request
            )
        self.assertEqual(result, expected)
        self.assertEqual(restore.call_args.args[1], body)

    async def test_invalid_unicode_and_boolean_revision_use_v2_error(self):
        for body in (
            {"base_mapping_revision": False, "groups": [], "ignored_sheet_ids": []},
            {
                "base_mapping_revision": 0,
                "groups": [
                    {
                        "display_name": "\ud800",
                        "sheets": [
                            {
                                "sheet_id": "sheet_001",
                                "role": "guide_reference",
                            }
                        ],
                    }
                ],
                "ignored_sheet_ids": [],
            },
        ):
            request = _request(
                body=json.dumps(body, ensure_ascii=True).encode("ascii")
            )
            with (
                patch.object(interview_v2, "INTERVIEW_V2_ENABLED", True),
                patch.object(
                    interview_v2,
                    "_require_feature",
                    new=AsyncMock(return_value={"email": "owner@example.com"}),
                ),
                patch.object(interview_v2, "save_group_mapping") as save,
            ):
                response = await interview_v2.put_interview_group_mapping(
                    IMPORT_ID, request
                )
            self.assertEqual(response.status_code, 422)
            self.assertEqual(
                _body(response)["error"]["code"], "MAPPING_REQUEST_INVALID"
            )
            save.assert_not_called()

    def test_mapping_write_routes_declare_no_body_dependencies(self):
        routes = {
            route.path: route
            for route in interview_v2.router.routes
            if getattr(route, "path", "").endswith(
                (
                    "/group-mapping",
                    "/group-mapping:restore",
                    "/group-mapping:confirm",
                )
            )
        }
        self.assertEqual(len(routes), 3)
        for route in routes.values():
            self.assertEqual(route.dependant.body_params, [])


if __name__ == "__main__":
    unittest.main()
