from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import io
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx
import openpyxl
from openpyxl.drawing.image import Image

from app.integrations.bested_questionnaire_client import (
    BestedQuestionnaireMediaIssue,
    parse_bested_questionnaire_upload,
)
from app.routers import questionnaire_sources as questionnaire_sources_router
from app.routers.questionnaire_sources import (
    create_bested_questionnaire_sources_router,
)
from app.schemas.questionnaire import MappingStatus, QuestionnaireSourceMode
from app.schemas.research_assets import MediaType, ProcessingStatus, Provider
from app.services import bested_questionnaire_snapshot_api as bested_api_module
from app.services.bested_questionnaire_snapshot_api import (
    BestedQuestionnaireConflictError,
    BestedQuestionnaireInternalError,
    BestedQuestionnaireInvalidError,
    BestedQuestionnaireSnapshotApi,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    SnapshotPackageError,
)


LOGIN = {"email": "bested-user@example.com", "name": "Bested User"}
OWNER_REF = "email:bested-user@example.com"
FIXED_TIME = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
SUMMARY_FIELDS = {
    "schema_version",
    "snapshot_id",
    "provider",
    "source_mode",
    "collection_state",
    "mapping_status",
    "item_count",
    "question_count",
    "asset_count",
    "image_asset_count",
    "asset_reference_count",
}


def _questionnaire_bytes(*, title: str = "是否满意") -> bytes:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "问卷内容"
    for row in (
        ("题号", "题目"),
        ("Q1[单选题]", title),
        ("选项", ""),
        ("1", "是"),
        ("2", "否"),
        ("Q2[填空题]", "其他建议"),
    ):
        worksheet.append(row)
    worksheet.add_image(Image(io.BytesIO(png)), "C3")
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


class _SequenceClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        value = FIXED_TIME + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class _CorruptStorage:
    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        raise SnapshotPackageError("private /storage/corrupt")

    def save_snapshot_package(self, owner_ref: str, package) -> None:
        raise AssertionError("corrupt load must fail before save")


class _ChunkedReceive:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict:
        index = self.calls
        self.calls += 1
        if index >= len(self.chunks):
            return {"type": "http.disconnect"}
        return {
            "type": "http.request",
            "body": self.chunks[index],
            "more_body": index < len(self.chunks) - 1,
        }


class _StallingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0
        self.first_sent = asyncio.Event()
        self.never = asyncio.Event()

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            self.first_sent.set()
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        await self.never.wait()
        raise AssertionError("unreachable")


def _multipart_body(
    content: bytes,
    *,
    filename: str = "questionnaire.xlsx",
) -> tuple[bytes, bytes]:
    boundary = b"bested-source-test"
    body = (
        b"--" + boundary + b"\r\n"
        + b'Content-Disposition: form-data; name="file"; filename="'
        + filename.encode("ascii")
        + b'"\r\nContent-Type: application/octet-stream\r\n\r\n'
        + content
        + b"\r\n--" + boundary + b"--\r\n"
    )
    return body, b"multipart/form-data; boundary=" + boundary


async def _call_asgi(
    app: FastAPI,
    receive: _ChunkedReceive,
    content_type: bytes,
) -> tuple[int, bytes]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    path = "/api/questionnaire-sources/bested/snapshots"
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"content-type", content_type)],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    status = next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, body


class BestedQuestionnaireSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="bested-questionnaire-api-test-",
        )
        self.storage = FileResearchAssetStorage(self.temporary.name)
        self.clock = _SequenceClock()
        self.api = BestedQuestionnaireSnapshotApi(self.storage, self.clock)
        self.router = create_bested_questionnaire_sources_router(self.api)
        self.app = FastAPI()
        self.app.include_router(self.router)
        self.content = _questionnaire_bytes()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _request(self, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ) as require_feature:
                response = await client.post(
                    "/api/questionnaire-sources/bested/snapshots",
                    **kwargs,
                )
        require_feature.assert_awaited_once()
        self.assertEqual(require_feature.await_args.args[1], "survey")
        return response

    def _endpoint(self):
        return next(route.endpoint for route in self.router.routes)

    @staticmethod
    def _request_object() -> Request:
        return Request({
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
        })

    async def test_valid_xlsx_maps_and_persists_complete_media_snapshot(self):
        summary = await self.api.import_questionnaire(
            OWNER_REF,
            "QUESTIONNAIRE.XLSX",
            self.content,
        )
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            summary.snapshot_id,
        )

        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(summary.provider, Provider.BESTED)
        self.assertEqual(
            summary.source_mode,
            QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD,
        )
        self.assertEqual(summary.mapping_status, MappingStatus.PARTIAL)
        self.assertEqual(summary.question_count, 2)
        self.assertEqual(summary.image_asset_count, 1)
        self.assertEqual(len(package.media), 1)
        self.assertTrue(all(
            package.media[asset.content_hash]
            for asset in package.bundle.collection.assets
            if asset.media_type == MediaType.IMAGE
        ))

    async def test_repeated_file_is_idempotent_across_clock_changes(self):
        with patch.object(
            self.storage,
            "save_snapshot_package",
            wraps=self.storage.save_snapshot_package,
        ) as save:
            first = await self.api.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            )
            second = await self.api.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            )

        self.assertEqual(first, second)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.clock.calls, 2)

    async def test_concurrent_same_file_import_is_idempotent(self):
        first, second = await asyncio.gather(
            self.api.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            ),
            self.api.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            ),
        )

        self.assertEqual(first, second)
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            first.snapshot_id,
        )
        self.assertIsNotNone(package)

    async def test_concurrent_different_filename_has_one_conflict(self):
        outcomes = await asyncio.gather(
            self.api.import_questionnaire(
                OWNER_REF,
                "first.xlsx",
                self.content,
            ),
            self.api.import_questionnaire(
                OWNER_REF,
                "second.xlsx",
                self.content,
            ),
            return_exceptions=True,
        )

        self.assertEqual(
            sum(not isinstance(item, Exception) for item in outcomes),
            1,
        )
        self.assertEqual(
            sum(isinstance(item, BestedQuestionnaireConflictError)
                for item in outcomes),
            1,
        )

    async def test_same_content_with_changed_filename_is_conflict(self):
        await self.api.import_questionnaire(
            OWNER_REF,
            "first.xlsx",
            self.content,
        )
        with self.assertRaises(BestedQuestionnaireConflictError):
            await self.api.import_questionnaire(
                OWNER_REF,
                "second.xlsx",
                self.content,
            )

    async def test_owner_scope_changes_snapshot_identity(self):
        first = await self.api.import_questionnaire(
            OWNER_REF,
            "questionnaire.xlsx",
            self.content,
        )
        second = await self.api.import_questionnaire(
            "email:other@example.com",
            "questionnaire.xlsx",
            self.content,
        )

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertIsNone(self.storage.load_snapshot_package(
            OWNER_REF,
            second.snapshot_id,
        ))

    async def test_media_issue_is_saved_as_partial_without_losing_structure(self):
        parsed = parse_bested_questionnaire_upload(
            "questionnaire.xlsx",
            self.content,
        )
        issue = BestedQuestionnaireMediaIssue(
            code="image_extraction_failed",
            sheet_name=parsed.sheet_name,
            source_cell="C3",
            source_row=3,
        )
        partial = replace(parsed, images=(), media_issues=(issue,))
        with patch.object(
            bested_api_module,
            "parse_bested_questionnaire_upload",
            return_value=partial,
        ):
            summary = await self.api.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            )
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            summary.snapshot_id,
        )

        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(summary.question_count, 2)
        self.assertEqual(summary.image_asset_count, 0)
        self.assertEqual(package.media, {})
        self.assertEqual(
            package.bundle.collection.sources[0].acquisition_status,
            ProcessingStatus.PARTIAL,
        )

    async def test_invalid_input_and_corrupt_storage_use_safe_errors(self):
        for filename, content in (
            ("questionnaire.xls", self.content),
            ("https://example.test/questionnaire.xlsx", self.content),
            ("questionnaire.xlsx", b"not an xlsx"),
            ("questionnaire.xlsx", b""),
        ):
            with self.subTest(filename=filename, content_size=len(content)):
                with self.assertRaises(BestedQuestionnaireInvalidError):
                    await self.api.import_questionnaire(
                        OWNER_REF,
                        filename,
                        content,
                    )

        corrupt = BestedQuestionnaireSnapshotApi(
            _CorruptStorage(),
            lambda: FIXED_TIME,
        )
        with self.assertRaises(BestedQuestionnaireInternalError) as caught:
            await corrupt.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            )
        self.assertNotIn("/storage/corrupt", str(caught.exception))

    async def test_parse_map_and_storage_are_offloaded(self):
        original_parse = bested_api_module.parse_bested_questionnaire_upload
        started = threading.Event()
        worker_threads: list[int] = []
        event_loop_thread = threading.get_ident()

        def slow_parse(*args, **kwargs):
            worker_threads.append(threading.get_ident())
            started.set()
            time.sleep(0.2)
            return original_parse(*args, **kwargs)

        with patch.object(
            bested_api_module,
            "parse_bested_questionnaire_upload",
            side_effect=slow_parse,
        ):
            task = asyncio.create_task(self.api.import_questionnaire(
                OWNER_REF,
                "questionnaire.xlsx",
                self.content,
            ))
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
            before = time.monotonic()
            await asyncio.sleep(0.03)
            self.assertLess(time.monotonic() - before, 0.15)
            self.assertFalse(task.done())
            await task

        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], event_loop_thread)

    async def test_injected_clock_is_also_offloaded(self):
        clock_threads: list[int] = []
        event_loop_thread = threading.get_ident()

        def slow_clock() -> datetime:
            clock_threads.append(threading.get_ident())
            time.sleep(0.2)
            return FIXED_TIME

        api = BestedQuestionnaireSnapshotApi(self.storage, slow_clock)
        task = asyncio.create_task(api.import_questionnaire(
            OWNER_REF,
            "questionnaire.xlsx",
            self.content,
        ))
        before = time.monotonic()
        await asyncio.sleep(0.03)
        self.assertLess(time.monotonic() - before, 0.15)
        self.assertFalse(task.done())
        await task

        self.assertEqual(len(clock_threads), 1)
        self.assertNotEqual(clock_threads[0], event_loop_thread)

    async def test_http_success_returns_only_safe_summary(self):
        response = await self._request(files={
            "file": (
                "QUESTIONNAIRE.XLSX",
                self.content,
                "application/octet-stream",
            ),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), SUMMARY_FIELDS)
        self.assertEqual(response.json()["provider"], "bested")
        serialized = json.dumps(response.json(), ensure_ascii=False)
        for forbidden in (
            OWNER_REF,
            "是否满意",
            "questionnaire.xlsx",
            self.temporary.name,
        ):
            self.assertNotIn(forbidden.casefold(), serialized.casefold())

    async def test_http_authentication_runs_before_body_consumption(self):
        body, content_type = _multipart_body(self.content)
        receive = _ChunkedReceive([body[:64], body[64:]])

        async def deny(request, feature: str):
            self.assertEqual(feature, "survey")
            self.assertEqual(receive.calls, 0)
            raise HTTPException(status_code=403, detail="denied")

        with patch(
            "app.routers.questionnaire_sources._require_feature",
            new=deny,
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload), {"detail": "denied"})
        self.assertEqual(receive.calls, 0)

    async def test_http_total_limit_stops_request_stream_early(self):
        body, content_type = _multipart_body(self.content)
        receive = _ChunkedReceive([body[:64], body[64:128], body[128:]])
        with (
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                questionnaire_sources_router,
                "MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES",
                4,
            ),
            patch.object(
                questionnaire_sources_router,
                "_MAX_MULTIPART_OVERHEAD_BYTES",
                32,
            ),
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 413)
        self.assertEqual(receive.calls, 1)
        self.assertEqual(
            json.loads(payload),
            {"detail": "倍市得原问卷超过上传大小限制"},
        )

    async def test_slow_upload_times_out_and_releases_admission_slot(self):
        body, content_type = _multipart_body(self.content)
        receive = _StallingReceive(body[:256])
        with (
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                questionnaire_sources_router,
                "_BESTED_UPLOAD_TIMEOUT_SECONDS",
                0.03,
            ),
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 408)
        self.assertEqual(
            json.loads(payload),
            {"detail": "倍市得原问卷上传超时，请重试"},
        )
        retry = await self._request(files={
            "file": (
                "questionnaire.xlsx",
                self.content,
                "application/octet-stream",
            ),
        })
        self.assertEqual(retry.status_code, 200)

    async def test_http_rejects_shape_extension_empty_and_limits(self):
        wrong = await self._request(files={
            "file": ("questionnaire.xls", self.content, "application/octet-stream")
        })
        blank = await self._request(files={
            "file": ("questionnaire.xlsx", b"", "application/octet-stream")
        })
        extra = await self._request(files=[
            ("file", ("one.xlsx", self.content, "application/octet-stream")),
            ("file", ("two.xlsx", self.content, "application/octet-stream")),
        ])
        with patch.object(
            questionnaire_sources_router,
            "MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES",
            4,
        ):
            too_large = await self._request(files={
                "file": ("questionnaire.xlsx", b"12345", "application/octet-stream")
            })

        self.assertEqual(wrong.status_code, 422)
        self.assertEqual(blank.status_code, 422)
        self.assertEqual(extra.status_code, 422)
        self.assertEqual(too_large.status_code, 413)

    async def test_http_errors_are_stable_and_redacted(self):
        cases = (
            (BestedQuestionnaireInvalidError("private title"), 422),
            (BestedQuestionnaireConflictError("private conflict"), 409),
            (BestedQuestionnaireInternalError("private /path"), 500),
            (RuntimeError("private secret"), 500),
        )
        for error, expected_status in cases:
            with self.subTest(error=type(error).__name__):
                async def fail(*args, current_error=error, **kwargs):
                    raise current_error

                with patch.object(
                    BestedQuestionnaireSnapshotApi,
                    "import_questionnaire",
                    new=fail,
                ):
                    response = await self._request(files={
                        "file": (
                            "questionnaire.xlsx",
                            self.content,
                            "application/octet-stream",
                        )
                    })
                self.assertEqual(response.status_code, expected_status)
                self.assertNotIn(str(error), response.text)

    async def test_busy_import_is_rejected_before_body_and_cancel_is_safe(self):
        endpoint = self._endpoint()
        expected = await self.api.import_questionnaire(
            OWNER_REF,
            "questionnaire.xlsx",
            self.content,
        )
        first_started = asyncio.Event()
        first_finished = asyncio.Event()
        finish_first = asyncio.Event()
        calls: list[str] = []

        async def fake_import(api, owner_ref, filename, content):
            calls.append(filename)
            if len(calls) == 1:
                first_started.set()
                await finish_first.wait()
                first_finished.set()
            return expected

        body, content_type = _multipart_body(self.content, filename="first.xlsx")
        first_request = Request(
            self._request_object().scope,
            receive=_ChunkedReceive([body]),
        )
        second_body, _ = _multipart_body(self.content, filename="second.xlsx")
        second_receive = _ChunkedReceive([second_body])
        second_request = Request(
            self._request_object().scope
            | {"headers": [(b"content-type", content_type)]},
            receive=second_receive,
        )
        first_request.scope["headers"] = [(b"content-type", content_type)]

        with (
            patch.object(
                BestedQuestionnaireSnapshotApi,
                "import_questionnaire",
                new=fake_import,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first_task = asyncio.create_task(endpoint(first_request))
            await asyncio.wait_for(first_started.wait(), timeout=1)
            first_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_task
            with self.assertRaises(HTTPException) as busy:
                await endpoint(second_request)
            self.assertEqual(calls, ["first.xlsx"])
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)
            finish_first.set()
            await asyncio.wait_for(first_finished.wait(), timeout=1)
            await asyncio.sleep(0)

            retry_receive = _ChunkedReceive([second_body])
            retry_request = Request(
                self._request_object().scope
                | {"headers": [(b"content-type", content_type)]},
                receive=retry_receive,
            )
            second = await asyncio.wait_for(endpoint(retry_request), timeout=2)

        self.assertEqual(calls, ["first.xlsx", "second.xlsx"])
        self.assertEqual(second.provider, Provider.BESTED)


if __name__ == "__main__":
    unittest.main()
