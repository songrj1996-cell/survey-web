from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
import zlib

from fastapi import FastAPI, HTTPException, Request, Response
import httpx

from app.routers import questionnaire_asset_reviews as review_router_module
from app.routers.questionnaire_asset_reviews import (
    create_questionnaire_asset_reviews_router,
)
from app.schemas.research_assets import BindingStatus
from app.services.questionnaire_asset_review_api import (
    QuestionnaireAssetReviewApi,
    QuestionnaireAssetReviewInternalError,
)
from app.services.questionnaire_material_snapshot_api import (
    QuestionnaireMaterialScreenshot,
    QuestionnaireMaterialSnapshotApi,
)
from app.storage.questionnaire_asset_reviews import (
    FileQuestionnaireAssetReviewStorage,
)
from app.storage.research_assets import FileResearchAssetStorage


LOGIN = {"email": "decision-owner@example.com", "name": "Decision Owner"}
OTHER_LOGIN = {"email": "other-decision-owner@example.com"}
OWNER_REF = "email:decision-owner@example.com"
DECISION_PATH_TEMPLATE = (
    "/api/questionnaire-sources/snapshots/{snapshot_id}"
    "/asset-review/decisions"
)
SAFE_HEADERS = {
    "cache-control": "private, no-store",
    "x-content-type-options": "nosniff",
}


def _token(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _png(red: int, green: int, blue: int) -> bytes:
    def chunk(kind: bytes, content: bytes) -> bytes:
        checksum = zlib.crc32(kind + content) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(content))
            + kind
            + content
            + struct.pack(">I", checksum)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(bytes((0, red, green, blue)))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


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


class _DisconnectingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        return {"type": "http.disconnect"}


class _StallingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0
        self.never = asyncio.Event()

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        await self.never.wait()
        raise AssertionError("unreachable")


def _request_object(
    receive,
    *,
    content_type: bytes | tuple[bytes, ...] = b"application/json",
    content_length: bytes | tuple[bytes, ...] | None = None,
) -> Request:
    content_types = (
        content_type if isinstance(content_type, tuple) else (content_type,)
    )
    headers = [(b"content-type", value) for value in content_types]
    if content_length is not None:
        content_lengths = (
            content_length
            if isinstance(content_length, tuple)
            else (content_length,)
        )
        headers.extend(
            (b"content-length", value) for value in content_lengths
        )
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": headers,
        "client": ("test", 1),
        "server": ("test", 80),
    }, receive)


def _assert_safe_headers(test: unittest.TestCase, response_or_error) -> None:
    source = response_or_error.headers or {}
    headers = {str(key).casefold(): value for key, value in source.items()}
    for name, expected in SAFE_HEADERS.items():
        test.assertEqual(headers.get(name), expected)


class QuestionnaireAssetReviewDecisionApiTests(
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-asset-review-decision-api-",
        )
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.snapshot_storage = FileResearchAssetStorage(root / "snapshots")
        self.review_storage = FileQuestionnaireAssetReviewStorage(
            root / "review-state"
        )
        material_api = QuestionnaireMaterialSnapshotApi(self.snapshot_storage)
        summary = await material_api.import_screenshots(
            OWNER_REF,
            (
                QuestionnaireMaterialScreenshot(
                    "page-a.png",
                    "image/png",
                    _png(17, 34, 51),
                ),
                QuestionnaireMaterialScreenshot(
                    "page-b.png",
                    "image/png",
                    _png(68, 85, 102),
                ),
            ),
        )
        self.snapshot_id = summary.snapshot_id
        self.api = QuestionnaireAssetReviewApi(
            self.snapshot_storage,
            self.review_storage,
        )
        self.router = create_questionnaire_asset_reviews_router(self.api)
        self.app = FastAPI()
        self.app.include_router(self.router)
        self.path = DECISION_PATH_TEMPLATE.format(snapshot_id=self.snapshot_id)

    def _endpoint(self):
        return next(
            route.endpoint
            for route in self.router.routes
            if route.path == DECISION_PATH_TEMPLATE
            and "POST" in route.methods
        )

    async def _projection(self):
        return await self.api.get_projection(OWNER_REF, self.snapshot_id)

    async def _payload(
        self,
        *,
        decision: str = "confirmed",
        key: str = "decision-1",
        expected_revision: int | None = None,
    ) -> dict:
        projection = await self._projection()
        item = projection.items[0]
        return {
            "schema_version": 1,
            "expected_revision": (
                projection.review_revision
                if expected_revision is None
                else expected_revision
            ),
            "idempotency_key": _token(key),
            "base_version_token": projection.base_version_token,
            "reference_token": item.reference_token,
            "asset_token": item.asset_token,
            "decision": decision,
        }

    async def _post(
        self,
        *,
        login: dict | None = LOGIN,
        json_value: object | None = None,
        content: bytes | None = None,
        content_type: str = "application/json",
    ) -> httpx.Response:
        auth = AsyncMock(return_value=login)
        kwargs: dict[str, object] = {
            "headers": {"Content-Type": content_type},
        }
        if content is not None:
            kwargs["content"] = content
        else:
            kwargs["json"] = json_value
        with patch.object(review_router_module, "_require_feature", new=auth):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                return await client.post(self.path, **kwargs)

    async def test_confirm_reject_reset_and_idempotent_replay_return_full_projection(
        self,
    ) -> None:
        initial = await self._projection()
        self.assertEqual(initial.review_revision, 0)
        self.assertEqual(initial.items[0].binding_status, BindingStatus.NEEDS_REVIEW)
        self.assertTrue(initial.items[0].review_required)
        self.assertIsNone(initial.items[0].active_review_decision)

        confirm_payload = await self._payload(key="confirm")
        confirmed = await self._post(json_value=confirm_payload)
        self.assertEqual(confirmed.status_code, 200)
        _assert_safe_headers(self, confirmed)
        confirmed_body = confirmed.json()
        self.assertEqual(confirmed_body["review_revision"], 1)
        self.assertEqual(
            confirmed_body["items"][0]["active_review_decision"],
            "confirmed",
        )
        self.assertEqual(
            confirmed_body["items"][0]["binding_status"],
            "confirmed",
        )
        self.assertFalse(confirmed_body["items"][0]["review_required"])

        replay = await self._post(json_value=confirm_payload)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), confirmed_body)

        duplicate_confirm = await self._post(json_value=await self._payload(
            decision="confirmed",
            key="confirm-with-new-key",
        ))
        self.assertEqual(duplicate_confirm.status_code, 409)
        self.assertEqual((await self._projection()).review_revision, 1)

        rejected = await self._post(json_value=await self._payload(
            decision="rejected",
            key="reject",
        ))
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()["review_revision"], 2)
        self.assertEqual(
            rejected.json()["items"][0]["active_review_decision"],
            "rejected",
        )
        self.assertEqual(
            rejected.json()["items"][0]["binding_status"],
            "rejected",
        )
        self.assertFalse(rejected.json()["items"][0]["review_required"])

        duplicate_reject = await self._post(json_value=await self._payload(
            decision="rejected",
            key="reject-with-new-key",
        ))
        self.assertEqual(duplicate_reject.status_code, 409)
        self.assertEqual((await self._projection()).review_revision, 2)

        reset = await self._post(json_value=await self._payload(
            decision="reset",
            key="reset",
        ))
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(reset.json()["review_revision"], 3)
        self.assertIsNone(
            reset.json()["items"][0]["active_review_decision"]
        )
        self.assertEqual(
            reset.json()["items"][0]["binding_status"],
            "needs_review",
        )
        self.assertTrue(reset.json()["items"][0]["review_required"])
        self.assertEqual(
            reset.json()["base_version_token"],
            initial.base_version_token,
        )

        serialized = json.dumps(reset.json(), ensure_ascii=False).casefold()
        for forbidden in (
            OWNER_REF,
            "base_package_sha256",
            "base_package_size_bytes",
            "reviewer_token",
            "event_sha256",
            "recorded_at",
            ".asset-reviews-v1",
        ):
            self.assertNotIn(forbidden.casefold(), serialized)

    async def test_reset_without_active_override_is_conflict_and_zero_write(
        self,
    ) -> None:
        initial = await self._projection()
        self.assertEqual(initial.review_revision, 0)
        self.assertFalse(self.review_storage.root.exists())

        response = await self._post(json_value=await self._payload(
            decision="reset",
            key="no-op-reset",
        ))
        self.assertEqual(response.status_code, 409)
        _assert_safe_headers(self, response)
        self.assertFalse(self.review_storage.root.exists())

        current = await self._projection()
        self.assertEqual(current.review_revision, 0)
        self.assertIsNone(current.items[0].active_review_decision)

    async def test_owner_token_base_cas_and_idempotency_conflicts_fail_closed(
        self,
    ) -> None:
        projection = await self._projection()
        first, second = projection.items
        payload = await self._payload(key="first-write")

        other_owner = await self._post(
            login=OTHER_LOGIN,
            json_value=payload,
        )
        self.assertEqual(other_owner.status_code, 404)

        mismatched_pair = {
            **payload,
            "asset_token": second.asset_token,
        }
        invalid_pair = await self._post(json_value=mismatched_pair)
        self.assertEqual(invalid_pair.status_code, 404)

        unknown_reference = {
            **payload,
            "reference_token": "0" * 64,
        }
        unknown = await self._post(json_value=unknown_reference)
        self.assertEqual(unknown.status_code, 404)

        wrong_base = {
            **payload,
            "base_version_token": "0" * 64,
        }
        base_conflict = await self._post(json_value=wrong_base)
        self.assertEqual(base_conflict.status_code, 409)

        accepted = await self._post(json_value=payload)
        self.assertEqual(accepted.status_code, 200)

        stale = {
            **payload,
            "idempotency_key": _token("stale-write"),
        }
        stale_response = await self._post(json_value=stale)
        self.assertEqual(stale_response.status_code, 409)

        changed_replay = {
            **payload,
            "decision": "rejected",
        }
        idempotency_conflict = await self._post(json_value=changed_replay)
        self.assertEqual(idempotency_conflict.status_code, 409)

        for response in (
            other_owner,
            invalid_pair,
            unknown,
            base_conflict,
            stale_response,
            idempotency_conflict,
        ):
            _assert_safe_headers(self, response)
            serialized = response.text.casefold()
            self.assertNotIn(OWNER_REF.casefold(), serialized)
            self.assertNotIn(first.reference_token, serialized)
            self.assertNotIn(first.asset_token, serialized)

    async def test_auth_and_owner_checks_precede_gate_and_body_consumption(
        self,
    ) -> None:
        endpoint = self._endpoint()
        denied_receive = _ChunkedReceive([b"not-json"])
        denied = AsyncMock(side_effect=HTTPException(
            status_code=403,
            detail="denied",
        ))
        with patch.object(review_router_module, "_require_feature", new=denied):
            with self.assertRaises(HTTPException) as raised:
                await endpoint(
                    self.snapshot_id,
                    _request_object(
                        denied_receive,
                        content_type=b"text/plain",
                    ),
                    Response(),
                )
        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(denied_receive.calls, 0)
        _assert_safe_headers(self, raised.exception)

        owner_receive = _ChunkedReceive([b"not-json"])
        with patch.object(
            review_router_module,
            "_require_feature",
            new=AsyncMock(return_value={}),
        ):
            with self.assertRaises(HTTPException) as missing_owner:
                await endpoint(
                    self.snapshot_id,
                    _request_object(
                        owner_receive,
                        content_type=b"text/plain",
                    ),
                    Response(),
                )
        self.assertEqual(missing_owner.exception.status_code, 401)
        self.assertEqual(owner_receive.calls, 0)
        _assert_safe_headers(self, missing_owner.exception)

    async def test_json_body_contract_is_strict_and_rejects_unsupported_media(
        self,
    ) -> None:
        valid = await self._payload()
        invalid_values = (
            {key: value for key, value in valid.items() if key != "schema_version"},
            {**valid, "unknown": "field"},
            {**valid, "schema_version": True},
            {**valid, "expected_revision": True},
            {**valid, "expected_revision": 0.0},
            {**valid, "idempotency_key": valid["idempotency_key"].upper()},
            {**valid, "decision": "approved"},
            [],
        )
        for value in invalid_values:
            with self.subTest(value=value):
                response = await self._post(json_value=value)
                self.assertEqual(response.status_code, 422)
                _assert_safe_headers(self, response)

        duplicate = json.dumps(valid, separators=(",", ":"))[:-1]
        duplicate += ',"decision":"rejected"}'
        malformed_payloads = (
            duplicate.encode("utf-8"),
            b'{"schema_version":1',
            b'{"schema_version":NaN}',
            b"\xff",
        )
        for content in malformed_payloads:
            response = await self._post(content=content)
            self.assertEqual(response.status_code, 422)
            _assert_safe_headers(self, response)

        wrong_media_type = await self._post(
            content=json.dumps(valid).encode("utf-8"),
            content_type="text/plain",
        )
        self.assertEqual(wrong_media_type.status_code, 415)
        _assert_safe_headers(self, wrong_media_type)

        duplicate_media_receive = _ChunkedReceive([b"{}"])
        with patch.object(
            review_router_module,
            "_require_feature",
            new=AsyncMock(return_value=LOGIN),
        ):
            with self.assertRaises(HTTPException) as duplicate_media:
                await self._endpoint()(
                    self.snapshot_id,
                    _request_object(
                        duplicate_media_receive,
                        content_type=(b"application/json", b"text/plain"),
                    ),
                    Response(),
                )
        self.assertEqual(duplicate_media.exception.status_code, 415)
        self.assertEqual(duplicate_media_receive.calls, 0)
        _assert_safe_headers(self, duplicate_media.exception)

    async def test_bounded_stream_disconnect_timeout_and_retry_release_gate(
        self,
    ) -> None:
        endpoint = self._endpoint()
        auth = AsyncMock(return_value=LOGIN)
        with patch.object(review_router_module, "_require_feature", new=auth):
            oversized_receive = _ChunkedReceive([
                b"{" + b" " * 3000,
                b" " * 3000,
                b"}",
            ])
            with self.assertRaises(HTTPException) as oversized:
                await endpoint(
                    self.snapshot_id,
                    _request_object(oversized_receive),
                    Response(),
                )
            self.assertEqual(oversized.exception.status_code, 413)
            self.assertEqual(oversized_receive.calls, 2)
            _assert_safe_headers(self, oversized.exception)

            declared_receive = _ChunkedReceive([b"{}"])
            with self.assertRaises(HTTPException) as declared:
                await endpoint(
                    self.snapshot_id,
                    _request_object(
                        declared_receive,
                        content_length=b"4097",
                    ),
                    Response(),
                )
            self.assertEqual(declared.exception.status_code, 413)
            self.assertEqual(declared_receive.calls, 0)

            invalid_length_receive = _ChunkedReceive([b"{}"])
            with self.assertRaises(HTTPException) as invalid_length:
                await endpoint(
                    self.snapshot_id,
                    _request_object(
                        invalid_length_receive,
                        content_length=b"-1",
                    ),
                    Response(),
                )
            self.assertEqual(invalid_length.exception.status_code, 422)
            self.assertEqual(invalid_length_receive.calls, 0)

            duplicate_length_receive = _ChunkedReceive([b"{}"])
            with self.assertRaises(HTTPException) as duplicate_length:
                await endpoint(
                    self.snapshot_id,
                    _request_object(
                        duplicate_length_receive,
                        content_length=(b"2", b"2"),
                    ),
                    Response(),
                )
            self.assertEqual(duplicate_length.exception.status_code, 422)
            self.assertEqual(duplicate_length_receive.calls, 0)
            _assert_safe_headers(self, duplicate_length.exception)

            for declared_length in (b"1", b"99"):
                with self.subTest(declared_length=declared_length):
                    mismatched_length_receive = _ChunkedReceive([b"{}"])
                    with self.assertRaises(HTTPException) as mismatch:
                        await endpoint(
                            self.snapshot_id,
                            _request_object(
                                mismatched_length_receive,
                                content_length=declared_length,
                            ),
                            Response(),
                        )
                    self.assertEqual(mismatch.exception.status_code, 400)
                    _assert_safe_headers(self, mismatch.exception)

            with self.assertRaises(HTTPException) as disconnected:
                await endpoint(
                    self.snapshot_id,
                    _request_object(_DisconnectingReceive(b"{")),
                    Response(),
                )
            self.assertEqual(disconnected.exception.status_code, 400)
            _assert_safe_headers(self, disconnected.exception)

            with patch.object(
                review_router_module,
                "_ASSET_REVIEW_DECISION_BODY_TIMEOUT_SECONDS",
                0.01,
            ):
                with self.assertRaises(HTTPException) as body_timeout:
                    await endpoint(
                        self.snapshot_id,
                        _request_object(_StallingReceive(b"{")),
                        Response(),
                    )
            self.assertEqual(body_timeout.exception.status_code, 408)
            _assert_safe_headers(self, body_timeout.exception)

        retry = await self._post(json_value=await self._payload(key="retry"))
        self.assertEqual(retry.status_code, 200)

    async def test_write_gate_is_independent_and_busy_precedes_body_parsing(
        self,
    ) -> None:
        endpoint = self._endpoint()
        projection = await self._projection()
        payload = await self._payload()
        encoded = json.dumps(payload).encode("utf-8")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_submit(_api, _owner, _snapshot, _request):
            started.set()
            await release.wait()
            return projection

        with (
            patch.object(
                QuestionnaireAssetReviewApi,
                "submit_decision",
                new=slow_submit,
            ),
            patch.object(
                review_router_module,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first_task = asyncio.create_task(endpoint(
                self.snapshot_id,
                _request_object(_ChunkedReceive([encoded])),
                Response(),
            ))
            await asyncio.wait_for(started.wait(), timeout=1)

            busy_receive = _ChunkedReceive([b"not-json"])
            with self.assertRaises(HTTPException) as busy:
                await endpoint(
                    self.snapshot_id,
                    _request_object(
                        busy_receive,
                        content_type=b"text/plain",
                    ),
                    Response(),
                )
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(busy_receive.calls, 0)

            read_response = await self.api.get_projection(
                OWNER_REF,
                self.snapshot_id,
            )
            self.assertEqual(read_response, projection)
            release.set()
            result = await first_task
        self.assertEqual(result, projection)

    async def test_processing_timeout_keeps_task_and_gate_until_real_completion(
        self,
    ) -> None:
        projection = await self._projection()
        payload = await self._payload()
        started = asyncio.Event()
        finished = asyncio.Event()
        release = asyncio.Event()

        async def slow_submit(_api, _owner, _snapshot, _request):
            started.set()
            try:
                await release.wait()
                return projection
            finally:
                finished.set()

        with (
            patch.object(
                QuestionnaireAssetReviewApi,
                "submit_decision",
                new=slow_submit,
            ),
            patch.object(
                review_router_module,
                "_ASSET_REVIEW_DECISION_TIMEOUT_SECONDS",
                0.01,
            ),
        ):
            timed_out = await self._post(json_value=payload)
            self.assertTrue(started.is_set())
            self.assertEqual(timed_out.status_code, 504)
            self.assertIn("结果可能已经生效", timed_out.json()["detail"])
            self.assertIn("相同幂等键", timed_out.json()["detail"])
            _assert_safe_headers(self, timed_out)
            self.assertFalse(finished.is_set())

            busy = await self._post(
                content=b"not-json",
                content_type="text/plain",
            )
            self.assertEqual(busy.status_code, 429)
            release.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await asyncio.sleep(0)

            retry = await self._post(json_value=payload)
            self.assertEqual(retry.status_code, 200)

    async def test_request_cancellation_keeps_service_task_and_gate_until_done(
        self,
    ) -> None:
        endpoint = self._endpoint()
        projection = await self._projection()
        payload = await self._payload()
        encoded = json.dumps(payload).encode("utf-8")
        started = asyncio.Event()
        finished = asyncio.Event()
        release = asyncio.Event()

        async def slow_submit(_api, _owner, _snapshot, _request):
            started.set()
            try:
                await release.wait()
                return projection
            finally:
                finished.set()

        with (
            patch.object(
                QuestionnaireAssetReviewApi,
                "submit_decision",
                new=slow_submit,
            ),
            patch.object(
                review_router_module,
                "_require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            request_task = asyncio.create_task(endpoint(
                self.snapshot_id,
                _request_object(_ChunkedReceive([encoded])),
                Response(),
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            request_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await request_task
            self.assertFalse(finished.is_set())

            busy_receive = _ChunkedReceive([b"not-json"])
            with self.assertRaises(HTTPException) as busy:
                await endpoint(
                    self.snapshot_id,
                    _request_object(busy_receive),
                    Response(),
                )
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(busy_receive.calls, 0)

            release.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await asyncio.sleep(0)
            retry = await endpoint(
                self.snapshot_id,
                _request_object(_ChunkedReceive([encoded])),
                Response(),
            )
        self.assertEqual(retry, projection)

    async def test_late_failure_after_timeout_is_consumed_without_loop_warning(
        self,
    ) -> None:
        payload = await self._payload(key="late-failure")
        started = asyncio.Event()
        release = asyncio.Event()
        contexts: list[dict] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))

        async def late_failure(_api, _owner, _snapshot, _request):
            started.set()
            await release.wait()
            raise RuntimeError("late-secret-exception")

        try:
            with (
                patch.object(
                    QuestionnaireAssetReviewApi,
                    "submit_decision",
                    new=late_failure,
                ),
                patch.object(
                    review_router_module,
                    "_ASSET_REVIEW_DECISION_TIMEOUT_SECONDS",
                    0.01,
                ),
            ):
                timed_out = await self._post(json_value=payload)
                self.assertTrue(started.is_set())
                self.assertEqual(timed_out.status_code, 504)
                release.set()
                for _ in range(20):
                    await asyncio.sleep(0)
                self.assertEqual(contexts, [])
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_internal_service_failure_is_safe_500(self) -> None:
        payload = await self._payload()
        with patch.object(
            QuestionnaireAssetReviewApi,
            "submit_decision",
            new=AsyncMock(side_effect=QuestionnaireAssetReviewInternalError()),
        ):
            response = await self._post(json_value=payload)
        self.assertEqual(response.status_code, 500)
        _assert_safe_headers(self, response)
        serialized = response.text.casefold()
        self.assertNotIn(OWNER_REF.casefold(), serialized)
        self.assertNotIn(payload["reference_token"], serialized)
        self.assertNotIn(payload["asset_token"], serialized)

        with patch.object(
            QuestionnaireAssetReviewApi,
            "submit_decision",
            new=AsyncMock(side_effect=HTTPException(
                status_code=418,
                detail="internal-owner-secret-token",
                headers={"X-Internal-Debug": "sensitive"},
            )),
        ):
            unknown_http = await self._post(json_value=payload)
        self.assertEqual(unknown_http.status_code, 500)
        _assert_safe_headers(self, unknown_http)
        self.assertNotIn("internal-owner-secret-token", unknown_http.text)
        self.assertNotIn("x-internal-debug", unknown_http.headers)


if __name__ == "__main__":
    unittest.main()
