from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx

from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormImageCapture,
    GoogleFormImageFailure,
    GoogleFormsConnectorError,
    GoogleFormsErrorCode,
    GoogleFormsStage,
    GoogleImageContext,
)
from app.routers import questionnaire_sources as questionnaire_sources_router
from app.routers.questionnaire_sources import (
    create_google_forms_questionnaire_sources_router,
)
from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSourceMode,
)
from app.schemas.research_assets import ProcessingStatus, Provider
from app.services import google_forms_snapshot_api as google_api_module
from app.services.google_forms_snapshot_api import (
    GoogleFormsQuestionnaireConflictError,
    GoogleFormsQuestionnaireInternalError,
    GoogleFormsQuestionnaireSnapshotApi,
)
from app.storage.research_assets import FileResearchAssetStorage


LOGIN = {"email": "google-user@example.com", "name": "Google User"}
OWNER_REF = "email:google-user@example.com"
FORM_ID = "FORM_SYNTHETIC_001"
SERVICE_ACCOUNT_EMAIL = (
    "forms-reader@example-project.iam.gserviceaccount.com"
)
FIXED_TIME = datetime(2026, 8, 13, 10, 0, tzinfo=timezone.utc)
GOOGLE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "questionnaire_sources"
    / "google_forms_api.json"
)
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


def _capture(*, partial: bool = False, image_suffix: bytes = b"") -> GoogleFormCapture:
    raw_form = json.loads(GOOGLE_FIXTURE.read_text(encoding="utf-8"))
    definitions = (
        (
            ("items", 0, "imageItem", "image"),
            GoogleImageContext(0, "item-standalone-image", None, (), None),
        ),
        (
            (
                "items", 1, "questionItem", "question",
                "choiceQuestion", "options", 0, "image",
            ),
            GoogleImageContext(
                1, "item-choice", "question-choice", ("question-choice",), 0,
            ),
        ),
        (
            ("items", 1, "questionItem", "image"),
            GoogleImageContext(
                1, "item-choice", "question-choice", ("question-choice",), None,
            ),
        ),
        (
            ("items", 2, "questionGroupItem", "image"),
            GoogleImageContext(
                2,
                "item-grid",
                None,
                ("question-grid-usability", "question-grid-art"),
                None,
            ),
        ),
    )
    images: list[GoogleFormImageCapture] = []
    for index, (path, context) in enumerate(definitions):
        content = b"\x89PNG\r\n\x1a\n" + f"google-{index}".encode() + image_suffix
        images.append(GoogleFormImageCapture(
            json_path=path,
            context=context,
            content=content,
            mime_type="image/png",
            sha256=hashlib.sha256(content).hexdigest(),
        ))
    failures: tuple[GoogleFormImageFailure, ...] = ()
    if partial:
        failed = images.pop(1)
        failures = (GoogleFormImageFailure(
            json_path=failed.json_path,
            context=failed.context,
            code=GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
            stage=GoogleFormsStage.IMAGE_DOWNLOAD,
            retryable=True,
            status_code=503,
        ),)
    return GoogleFormCapture(FORM_ID, raw_form, tuple(images), failures)


class _SequenceClock:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> datetime:
        value = FIXED_TIME + timedelta(seconds=self.calls)
        self.calls += 1
        return value


class _CaptureClient:
    def __init__(self, captures: list[GoogleFormCapture] | None = None) -> None:
        self.captures = captures or [_capture()]
        self.calls: list[tuple[str, str]] = []

    async def fetch_form(
        self,
        owner_ref: str,
        form_id: str,
    ) -> GoogleFormCapture:
        self.calls.append((owner_ref, form_id))
        index = min(len(self.calls) - 1, len(self.captures) - 1)
        return self.captures[index]


class _ErrorClient:
    def __init__(
        self,
        error: Exception,
        service_account_email: str = SERVICE_ACCOUNT_EMAIL,
    ) -> None:
        self.error = error
        self.service_account_email = service_account_email

    async def fetch_form(
        self,
        owner_ref: str,
        form_id: str,
    ) -> GoogleFormCapture:
        raise self.error


class _CorruptStorage:
    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        raise RuntimeError("private /storage/path")

    def save_snapshot_package(self, owner_ref: str, package) -> None:
        raise AssertionError("load failure must stop before save")


class GoogleFormsQuestionnaireSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="google-forms-api-test-")
        self.storage = FileResearchAssetStorage(self.temporary.name)
        self.client = _CaptureClient()
        self.clock = _SequenceClock()
        self.api = GoogleFormsQuestionnaireSnapshotApi(
            self.client,
            self.storage,
            self.clock,
        )
        self.router = create_google_forms_questionnaire_sources_router(self.api)
        self.app = FastAPI()
        self.app.include_router(self.router)

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
                    "/api/questionnaire-sources/google-forms/snapshots",
                    **kwargs,
                )
        require_feature.assert_awaited_once()
        self.assertEqual(require_feature.await_args.args[1], "survey")
        return response

    def _endpoint(self):
        return next(route.endpoint for route in self.router.routes)

    @staticmethod
    def _request_object(body: bytes = b"") -> Request:
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return Request({
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1),
            "server": ("test", 80),
        }, receive)

    async def test_clean_capture_maps_and_persists_complete_media_snapshot(self):
        summary = await self.api.import_questionnaire(OWNER_REF, FORM_ID)
        package = self.storage.load_snapshot_package(OWNER_REF, summary.snapshot_id)

        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(summary.provider, Provider.GOOGLE_FORMS)
        self.assertEqual(summary.source_mode, QuestionnaireSourceMode.OFFICIAL_API)
        self.assertEqual(summary.mapping_status, MappingStatus.EXACT)
        self.assertEqual(summary.image_asset_count, 4)
        self.assertEqual(len(package.media), 4)
        self.assertNotIn("contentUri", json.dumps(
            package.bundle.snapshot.provider_raw_definition,
        ))
        source_id = package.bundle.collection.sources[0].source_id
        self.assertEqual(
            package.bundle.collection.sources[0].owner_ref,
            OWNER_REF,
        )
        self.assertEqual(
            package.bundle.snapshot.snapshot_id,
            summary.snapshot_id,
        )
        self.assertTrue(source_id.startswith("src_"))

    async def test_partial_image_failure_persists_structure_and_partial_attempt(self):
        api = GoogleFormsQuestionnaireSnapshotApi(
            _CaptureClient([_capture(partial=True)]),
            self.storage,
            self.clock,
        )
        summary = await api.import_questionnaire(OWNER_REF, FORM_ID)
        package = self.storage.load_snapshot_package(OWNER_REF, summary.snapshot_id)

        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(summary.mapping_status, MappingStatus.PARTIAL)
        self.assertEqual(summary.image_asset_count, 3)
        self.assertEqual(len(package.media), 3)
        self.assertTrue(any(
            warning.code == "google_forms_image_http_error"
            for warning in package.bundle.snapshot.warnings
        ))
        mapped = google_api_module._source_result(
            google_api_module._map_capture(
                _capture(partial=True),
                owner_ref=OWNER_REF,
                retrieved_at=FIXED_TIME,
            )
        )
        self.assertTrue(mapped.partial_success)
        self.assertEqual(mapped.attempts[0].status, ProcessingStatus.PARTIAL)

    async def test_nested_nonblocking_warning_is_not_reported_as_complete(self):
        mapped = google_api_module._map_capture(
            _capture(),
            owner_ref=OWNER_REF,
            retrieved_at=FIXED_TIME,
        )
        result = google_api_module._source_result(mapped)

        self.assertTrue(result.partial_success)
        self.assertEqual(result.attempts[0].status, ProcessingStatus.PARTIAL)
        self.assertIn(
            "drive_response_access_required",
            {warning.code for warning in result.attempts[0].warnings},
        )

    async def test_blocking_mapping_warning_requires_review(self):
        capture = _capture()
        raw_form = json.loads(json.dumps(capture.raw_form))
        raw_form["items"][3].pop("itemId")
        mapped = google_api_module._map_capture(
            GoogleFormCapture(
                capture.form_id,
                raw_form,
                capture.images,
                capture.image_failures,
            ),
            owner_ref=OWNER_REF,
            retrieved_at=FIXED_TIME,
        )
        result = google_api_module._source_result(mapped)

        self.assertTrue(result.partial_success)
        self.assertEqual(
            result.attempts[0].status,
            ProcessingStatus.NEEDS_REVIEW,
        )
        self.assertTrue(any(
            warning.code == "missing_google_item_id" and warning.blocking
            for warning in result.attempts[0].warnings
        ))

    async def test_repeated_capture_is_idempotent_across_clock_changes(self):
        with patch.object(
            self.storage,
            "save_snapshot_package",
            wraps=self.storage.save_snapshot_package,
        ) as save:
            first = await self.api.import_questionnaire(OWNER_REF, FORM_ID)
            second = await self.api.import_questionnaire(OWNER_REF, FORM_ID)

        self.assertEqual(first, second)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.clock.calls, 2)

    async def test_concurrent_same_capture_is_idempotent(self):
        first, second = await asyncio.gather(
            self.api.import_questionnaire(OWNER_REF, FORM_ID),
            self.api.import_questionnaire(OWNER_REF, FORM_ID),
        )

        self.assertEqual(first, second)
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            first.snapshot_id,
        )
        self.assertIsNotNone(package)

    async def test_closed_collection_state_is_preserved(self):
        capture = _capture()
        raw_form = json.loads(json.dumps(capture.raw_form))
        raw_form["publishSettings"]["publishState"].pop(
            "isAcceptingResponses"
        )
        api = GoogleFormsQuestionnaireSnapshotApi(
            _CaptureClient([GoogleFormCapture(
                capture.form_id,
                raw_form,
                capture.images,
                capture.image_failures,
            )]),
            self.storage,
            self.clock,
        )

        summary = await api.import_questionnaire(OWNER_REF, FORM_ID)

        self.assertEqual(summary.collection_state, CollectionState.CLOSED)

    async def test_same_definition_with_changed_image_bytes_creates_new_version(self):
        api = GoogleFormsQuestionnaireSnapshotApi(
            _CaptureClient([_capture(), _capture(image_suffix=b"changed")]),
            self.storage,
            self.clock,
        )
        first = await api.import_questionnaire(OWNER_REF, FORM_ID)
        second = await api.import_questionnaire(OWNER_REF, FORM_ID)

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertIsNotNone(self.storage.load_snapshot_package(
            OWNER_REF,
            first.snapshot_id,
        ))
        self.assertIsNotNone(self.storage.load_snapshot_package(
            OWNER_REF,
            second.snapshot_id,
        ))

    async def test_retry_after_partial_capture_creates_complete_new_version(self):
        api = GoogleFormsQuestionnaireSnapshotApi(
            _CaptureClient([_capture(partial=True), _capture()]),
            self.storage,
            self.clock,
        )

        first = await api.import_questionnaire(OWNER_REF, FORM_ID)
        second = await api.import_questionnaire(OWNER_REF, FORM_ID)

        self.assertEqual(first.mapping_status, MappingStatus.PARTIAL)
        self.assertEqual(second.mapping_status, MappingStatus.EXACT)
        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.image_asset_count, 3)
        self.assertEqual(second.image_asset_count, 4)

    async def test_owner_isolation_creates_distinct_snapshot_identity(self):
        first = await self.api.import_questionnaire(OWNER_REF, FORM_ID)
        second_owner = "email:other-google-user@example.com"
        second = await self.api.import_questionnaire(second_owner, FORM_ID)

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertIsNone(self.storage.load_snapshot_package(
            OWNER_REF,
            second.snapshot_id,
        ))
        self.assertIsNone(self.storage.load_snapshot_package(
            second_owner,
            first.snapshot_id,
        ))
        self.assertEqual(
            self.client.calls,
            [
                (OWNER_REF, FORM_ID),
                (second_owner, FORM_ID),
            ],
        )

    async def test_connector_errors_map_to_stable_safe_http_statuses(self):
        cases = (
            (GoogleFormsErrorCode.INVALID_FORM_ID, False, None, 422, "google_forms_invalid"),
            (GoogleFormsErrorCode.AUTHENTICATION_REQUIRED, False, 401, 401, "google_forms_auth_required"),
            (GoogleFormsErrorCode.AUTHORIZATION_FAILED, False, None, 401, "google_forms_auth_required"),
            (GoogleFormsErrorCode.PERMISSION_DENIED, False, 403, 403, "google_forms_permission_denied"),
            (GoogleFormsErrorCode.FORM_NOT_FOUND, False, 404, 404, "google_forms_not_found"),
            (GoogleFormsErrorCode.RATE_LIMITED, True, 429, 503, "google_forms_retryable"),
            (GoogleFormsErrorCode.PROVIDER_UNAVAILABLE, True, 503, 503, "google_forms_retryable"),
            (GoogleFormsErrorCode.FORMS_INVALID_JSON, False, 200, 502, "google_forms_provider_error"),
            (GoogleFormsErrorCode.INVALID_CONFIGURATION, False, None, 500, "google_forms_internal"),
        )
        for code, retryable, provider_status, expected, expected_code in cases:
            with self.subTest(code=code):
                error = GoogleFormsConnectorError(
                    code,
                    "private token=/secret/path",
                    stage=GoogleFormsStage.FORMS_GET,
                    retryable=retryable,
                    status_code=provider_status,
                )
                api = GoogleFormsQuestionnaireSnapshotApi(
                    _ErrorClient(error),
                    self.storage,
                    self.clock,
                )
                app = FastAPI()
                app.include_router(
                    create_google_forms_questionnaire_sources_router(api)
                )
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                ) as client:
                    with patch(
                        "app.routers.questionnaire_sources._require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ):
                        response = await client.post(
                            "/api/questionnaire-sources/google-forms/snapshots",
                            json={"form_id": FORM_ID},
                        )
                self.assertEqual(response.status_code, expected)
                detail = response.json()["detail"]
                self.assertEqual(detail["code"], expected_code)
                self.assertIsInstance(detail["message"], str)
                self.assertNotIn("private", response.text)
                self.assertNotIn("secret", response.text)
                if code in {
                    GoogleFormsErrorCode.PERMISSION_DENIED,
                    GoogleFormsErrorCode.FORM_NOT_FOUND,
                }:
                    self.assertIn(SERVICE_ACCOUNT_EMAIL, detail["message"])
                    self.assertIn("PDF", detail["message"])

    async def test_request_contract_rejects_token_url_duplicates_and_oversize(self):
        cases = (
            ({"form_id": FORM_ID, "access_token": "secret"}, 422),
            ({"form_id": "https://docs.google.com/forms/d/demo/edit"}, 422),
            ({"form_id": "bad id"}, 422),
            ({
                "form_id": FORM_ID,
                "form_url": f"https://docs.google.com/forms/d/{FORM_ID}/edit",
            }, 422),
            ({}, 422),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = await self._request(json=payload)
                self.assertEqual(response.status_code, expected)
        duplicate = await self._request(
            content=b'{"form_id":"one","form_id":"two"}',
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(duplicate.status_code, 422)
        oversized = await self._request(
            content=b"{" + b" " * 5000 + b"}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(self.client.calls, [])

    async def test_editor_link_request_extracts_form_id_before_provider_call(self):
        response = await self._request(json={
            "form_url": (
                f"https://docs.google.com/forms/d/{FORM_ID}/edit"
                "?usp=sharing"
            ),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.calls, [(OWNER_REF, FORM_ID)])

    async def test_non_api_links_return_material_upload_fallback(self):
        cases = (
            "https://forms.gle/short-id",
            "https://docs.google.com/forms/d/e/public-id/viewform",
        )
        for form_url in cases:
            with self.subTest(form_url=form_url):
                response = await self._request(json={"form_url": form_url})
                self.assertEqual(response.status_code, 422)
                self.assertIn("编辑链接", response.json()["detail"])
                self.assertIn("PDF", response.json()["detail"])
        self.assertEqual(self.client.calls, [])

    async def test_auth_and_owner_rejection_happen_before_body_or_provider(self):
        request = self._request_object(b'{"form_id":"' + FORM_ID.encode() + b'"}')
        with patch(
            "app.routers.questionnaire_sources._require_feature",
            new=AsyncMock(side_effect=HTTPException(status_code=403, detail="denied")),
        ):
            with patch.object(request, "stream", wraps=request.stream) as stream:
                with self.assertRaises(HTTPException) as caught:
                    await self._endpoint()(request)
        self.assertEqual(caught.exception.status_code, 403)
        stream.assert_not_called()
        self.assertEqual(self.client.calls, [])

        request = self._request_object(b'{"form_id":"' + FORM_ID.encode() + b'"}')
        with patch(
            "app.routers.questionnaire_sources._require_feature",
            new=AsyncMock(return_value=None),
        ):
            with patch.object(request, "stream", wraps=request.stream) as stream:
                with self.assertRaises(HTTPException) as caught:
                    await self._endpoint()(request)
        self.assertEqual(caught.exception.status_code, 401)
        stream.assert_not_called()
        self.assertEqual(self.client.calls, [])

    async def test_response_is_safe_summary_only(self):
        response = await self._request(json={"form_id": FORM_ID})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(set(response.json()), SUMMARY_FIELDS)
        serialized = response.text.casefold()
        for forbidden in ("owner", "token", "contenturi", "raw_form", "media"):
            self.assertNotIn(forbidden, serialized)

    async def test_busy_request_fails_before_consuming_body(self):
        started = asyncio.Event()
        release = asyncio.Event()

        class _SlowClient:
            async def fetch_form(
                self,
                owner_ref: str,
                form_id: str,
            ) -> GoogleFormCapture:
                started.set()
                await release.wait()
                return _capture()

        api = GoogleFormsQuestionnaireSnapshotApi(
            _SlowClient(),
            self.storage,
            self.clock,
        )
        router = create_google_forms_questionnaire_sources_router(api)
        endpoint = next(route.endpoint for route in router.routes)
        first = self._request_object(b'{"form_id":"' + FORM_ID.encode() + b'"}')
        second = self._request_object(b'{"form_id":"' + FORM_ID.encode() + b'"}')
        with patch(
            "app.routers.questionnaire_sources._require_feature",
            new=AsyncMock(return_value=LOGIN),
        ):
            first_task = asyncio.create_task(endpoint(first))
            await started.wait()
            with patch.object(second, "stream", wraps=second.stream) as stream:
                with self.assertRaises(HTTPException) as caught:
                    await endpoint(second)
            self.assertEqual(caught.exception.status_code, 429)
            stream.assert_not_called()
            release.set()
            await first_task

    async def test_persist_work_is_offloaded_from_event_loop(self):
        original = google_api_module._persist_capture
        thread_ids: list[int] = []

        def slow_persist(*args, **kwargs):
            thread_ids.append(threading.get_ident())
            time.sleep(0.2)
            return original(*args, **kwargs)

        started = time.monotonic()
        with patch.object(
            google_api_module,
            "_persist_capture",
            side_effect=slow_persist,
        ):
            task = asyncio.create_task(
                self.api.import_questionnaire(OWNER_REF, FORM_ID)
            )
            await asyncio.sleep(0.03)
            heartbeat = time.monotonic() - started
            await task
        self.assertLess(heartbeat, 0.15)
        self.assertTrue(thread_ids)
        self.assertNotEqual(thread_ids[0], threading.get_ident())

    async def test_cancellation_holds_admission_until_background_persist_finishes(self):
        started = threading.Event()
        release = threading.Event()
        original = google_api_module._persist_capture

        def slow_persist(*args, **kwargs):
            started.set()
            release.wait(2)
            return original(*args, **kwargs)

        with patch.object(
            google_api_module,
            "_persist_capture",
            side_effect=slow_persist,
        ), patch(
            "app.routers.questionnaire_sources._require_feature",
            new=AsyncMock(return_value=LOGIN),
        ):
            first = self._request_object(b'{"form_id":"' + FORM_ID.encode() + b'"}')
            endpoint = self._endpoint()
            first_task = asyncio.create_task(endpoint(first))
            await asyncio.to_thread(started.wait, 1)
            first_task.cancel()
            await asyncio.sleep(0.01)
            first_task.cancel()
            await asyncio.sleep(0.01)
            second = self._request_object(b'{"form_id":"' + FORM_ID.encode() + b'"}')
            with self.assertRaises(HTTPException) as busy:
                await endpoint(second)
            self.assertEqual(busy.exception.status_code, 429)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await first_task
            for _ in range(100):
                await asyncio.sleep(0.01)
                third = self._request_object(
                    b'{"form_id":"' + FORM_ID.encode() + b'"}'
                )
                try:
                    summary = await endpoint(third)
                except HTTPException as error:
                    if error.status_code == 429:
                        continue
                    raise
                self.assertEqual(summary.provider, Provider.GOOGLE_FORMS)
                break
            else:
                self.fail("background persist did not release admission")

    async def test_unknown_provider_and_storage_errors_are_safe_internal_errors(self):
        secret = "private-token-and-path"
        api = GoogleFormsQuestionnaireSnapshotApi(
            _ErrorClient(RuntimeError(secret)),
            self.storage,
            self.clock,
        )
        with self.assertRaises(GoogleFormsQuestionnaireInternalError) as caught:
            await api.import_questionnaire(OWNER_REF, FORM_ID)
        self.assertNotIn(secret, str(caught.exception))

        storage_api = GoogleFormsQuestionnaireSnapshotApi(
            _CaptureClient(),
            _CorruptStorage(),
            self.clock,
        )
        with self.assertRaises(GoogleFormsQuestionnaireInternalError) as caught:
            await storage_api.import_questionnaire(OWNER_REF, FORM_ID)
        self.assertNotIn("storage", str(caught.exception).casefold())
        self.assertNotIn("path", str(caught.exception).casefold())


if __name__ == "__main__":
    unittest.main()
