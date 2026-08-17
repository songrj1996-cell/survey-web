from __future__ import annotations

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from app.routers import questionnaire_snapshot_analysis as router_module
from app.routers.questionnaire_snapshot_analysis import (
    create_questionnaire_snapshot_analysis_router,
)
from app.schemas.questionnaire_source_api import (
    QuestionnaireSnapshotAnalysisSessionResponse,
)
from app.services import questionnaire_snapshot_analysis_api as api_module
from app.services.questionnaire_snapshot_analysis_api import (
    MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES,
    QuestionnaireSnapshotAnalysisApi,
    QuestionnaireSnapshotAnalysisInternalError,
    QuestionnaireSnapshotAnalysisInvalidError,
    QuestionnaireSnapshotAnalysisNotFoundError,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchAssetStorageError,
    SnapshotPackage,
    SnapshotPackageError,
)


LOGIN = {"email": "analysis-owner@example.com", "name": "Owner"}
OWNER_REF = "email:analysis-owner@example.com"
SNAPSHOT_ID = "snapshot-analysis-fixture"


def _response(
    *,
    snapshot_id: str = SNAPSHOT_ID,
    filename: str = "responses.csv",
) -> QuestionnaireSnapshotAnalysisSessionResponse:
    return QuestionnaireSnapshotAnalysisSessionResponse(
        session_id="12345678-1234-1234-1234-123456789abc",
        filename=filename,
        total_rows=2,
        headers=["player", "answer"],
        preview=[["p1", "yes"], ["p2", "no"]],
        source_type="google",
        questionnaire_used=True,
        matched_questions=1,
        questionnaire_snapshot_id=snapshot_id,
    )


class _Storage:
    def __init__(self, package: SnapshotPackage | None = None) -> None:
        self.package = package
        self.load_calls: list[tuple[str, str]] = []

    def load_snapshot_package(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> SnapshotPackage | None:
        self.load_calls.append((owner_ref, snapshot_id))
        return self.package

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        raise AssertionError("analysis API must not save a snapshot")


def _opaque_package(
    snapshot_id: str = SNAPSHOT_ID,
) -> SnapshotPackage:
    return SnapshotPackage(
        ResearchAssetBundle(
            SimpleNamespace(snapshot_id=snapshot_id),
            object(),
        ),
        {},
    )


class QuestionnaireSnapshotAnalysisServiceTests(
    unittest.IsolatedAsyncioTestCase
):
    def test_response_contract_is_strict_and_does_not_accept_hidden_fields(self):
        response = _response()
        self.assertEqual(
            set(response.model_dump(mode="json")),
            {
                "session_id",
                "filename",
                "total_rows",
                "headers",
                "preview",
                "source_type",
                "questionnaire_used",
                "matched_questions",
                "questionnaire_snapshot_id",
            },
        )
        with self.assertRaises(ValidationError):
            QuestionnaireSnapshotAnalysisSessionResponse.model_validate({
                **response.model_dump(mode="json"),
                "owner_ref": OWNER_REF,
            })
        with self.assertRaises(ValidationError):
            QuestionnaireSnapshotAnalysisSessionResponse.model_validate({
                **response.model_dump(mode="json"),
                "questionnaire_used": False,
            })

    async def test_owner_login_mismatch_fails_before_storage(self):
        storage = _Storage(_opaque_package())
        api = QuestionnaireSnapshotAnalysisApi(storage)

        with self.assertRaises(QuestionnaireSnapshotAnalysisInternalError):
            await api.create_session(
                OWNER_REF,
                SNAPSHOT_ID,
                "responses.csv",
                b"question\nanswer\n",
                {"email": "other@example.com"},
            )

        self.assertEqual(storage.load_calls, [])

    async def test_missing_and_foreign_snapshot_are_same_not_found_error(self):
        storage = _Storage(None)
        api = QuestionnaireSnapshotAnalysisApi(storage)

        for snapshot_id in (SNAPSHOT_ID, "foreign-owner-snapshot"):
            with self.subTest(snapshot_id=snapshot_id):
                with self.assertRaises(
                    QuestionnaireSnapshotAnalysisNotFoundError
                ):
                    await api.create_session(
                        OWNER_REF,
                        snapshot_id,
                        "responses.csv",
                        b"question\nanswer\n",
                        LOGIN,
                    )

        self.assertEqual(
            storage.load_calls,
            [
                (OWNER_REF, SNAPSHOT_ID),
                (OWNER_REF, "foreign-owner-snapshot"),
            ],
        )

    async def test_invalid_inputs_fail_before_storage(self):
        storage = _Storage(_opaque_package())
        api = QuestionnaireSnapshotAnalysisApi(storage)
        invalid_uploads = (
            ("", "responses.csv", b"a\nb\n"),
            (" whitespace ", "responses.csv", b"a\nb\n"),
            (SNAPSHOT_ID, "responses.xls", b"a\nb\n"),
            (SNAPSHOT_ID, "folder/responses.csv", b"a\nb\n"),
            (SNAPSHOT_ID, "responses.csv", b""),
            (
                SNAPSHOT_ID,
                "responses.csv",
                b"x" * (MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES + 1),
            ),
        )

        for snapshot_id, filename, content in invalid_uploads:
            with self.subTest(snapshot_id=snapshot_id, filename=filename):
                expected = (
                    QuestionnaireSnapshotAnalysisNotFoundError
                    if snapshot_id != SNAPSHOT_ID
                    else QuestionnaireSnapshotAnalysisInvalidError
                )
                with self.assertRaises(expected):
                    await api.create_session(
                        OWNER_REF,
                        snapshot_id,
                        filename,
                        content,
                        LOGIN,
                    )

        self.assertEqual(storage.load_calls, [])

    async def test_binding_invalid_and_integrity_errors_are_safely_classified(self):
        package = _opaque_package()
        storage = _Storage(package)
        api = QuestionnaireSnapshotAnalysisApi(storage)

        with patch.object(
            api_module,
            "_bind_snapshot",
            side_effect=ValueError("private response detail"),
        ):
            with self.assertRaises(
                QuestionnaireSnapshotAnalysisInvalidError
            ):
                await api.create_session(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    "responses.csv",
                    b"question\nanswer\n",
                    LOGIN,
                )

        with patch.object(
            api_module,
            "_bind_snapshot",
            side_effect=SnapshotPackageError("private storage detail"),
        ):
            with self.assertRaises(
                QuestionnaireSnapshotAnalysisInternalError
            ):
                await api.create_session(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    "responses.csv",
                    b"question\nanswer\n",
                    LOGIN,
                )

    async def test_storage_failure_is_internal_and_does_not_call_binding(self):
        class _BrokenStorage(_Storage):
            def load_snapshot_package(self, owner_ref, snapshot_id):
                raise ResearchAssetStorageError("private path")

        api = QuestionnaireSnapshotAnalysisApi(_BrokenStorage())
        bind = Mock()
        with patch.object(api_module, "_bind_snapshot", new=bind):
            with self.assertRaises(
                QuestionnaireSnapshotAnalysisInternalError
            ):
                await api.create_session(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    "responses.csv",
                    b"question\nanswer\n",
                    LOGIN,
                )
        bind.assert_not_called()

    async def test_storage_snapshot_id_mismatch_fails_before_binding_or_session(self):
        api = QuestionnaireSnapshotAnalysisApi(_Storage(
            _opaque_package("different-snapshot-id")
        ))
        bind = Mock()
        upload = AsyncMock()
        with (
            patch.object(api_module, "_bind_snapshot", new=bind),
            patch.object(api_module, "handle_survey_upload", new=upload),
        ):
            with self.assertRaises(
                QuestionnaireSnapshotAnalysisInternalError
            ):
                await api.create_session(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    "responses.csv",
                    b"question\nanswer\n",
                    LOGIN,
                )

        bind.assert_not_called()
        upload.assert_not_awaited()

    async def test_valid_binding_reuses_existing_upload_and_returns_safe_fields(self):
        package = _opaque_package()
        storage = _Storage(package)
        api = QuestionnaireSnapshotAnalysisApi(storage)
        binding = object()
        handler_result = {
            **_response().model_dump(mode="python"),
            "owner_ref": "must-not-leak",
            "storage_path": "/private/snapshot.zip",
            "raw_response": "must-not-leak",
        }
        bind = Mock(return_value=binding)
        upload = AsyncMock(return_value=handler_result)

        with (
            patch.object(api_module, "_bind_snapshot", new=bind),
            patch.object(api_module, "handle_survey_upload", new=upload),
        ):
            response = await api.create_session(
                OWNER_REF,
                SNAPSHOT_ID,
                "responses.csv",
                b"question\nanswer\n",
                LOGIN,
            )

        self.assertEqual(storage.load_calls, [(OWNER_REF, SNAPSHOT_ID)])
        bind.assert_called_once_with(
            package,
            owner_ref=OWNER_REF,
            filename="responses.csv",
            content=b"question\nanswer\n",
        )
        upload.assert_awaited_once_with(
            "responses.csv",
            b"question\nanswer\n",
            LOGIN,
            bound_questionnaire=binding,
        )
        payload = response.model_dump(mode="json")
        self.assertEqual(payload, _response().model_dump(mode="json"))
        self.assertNotIn("owner_ref", payload)
        self.assertNotIn("storage_path", payload)
        self.assertNotIn("raw_response", payload)

    async def test_old_upload_invalid_error_is_normalized(self):
        api = QuestionnaireSnapshotAnalysisApi(_Storage(_opaque_package()))
        with (
            patch.object(
                api_module,
                "_bind_snapshot",
                return_value=object(),
            ),
            patch.object(
                api_module,
                "handle_survey_upload",
                new=AsyncMock(side_effect=HTTPException(
                    status_code=400,
                    detail="private parse detail",
                )),
            ),
        ):
            with self.assertRaises(
                QuestionnaireSnapshotAnalysisInvalidError
            ):
                await api.create_session(
                    OWNER_REF,
                    SNAPSHOT_ID,
                    "responses.xlsx",
                    b"xlsx",
                    LOGIN,
                )


class QuestionnaireSnapshotAnalysisRouterTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self) -> None:
        self.storage = _Storage(None)
        self.api = QuestionnaireSnapshotAnalysisApi(self.storage)
        self.app = FastAPI()
        self.app.include_router(
            create_questionnaire_snapshot_analysis_router(self.api)
        )

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.request(method, path, **kwargs)

    async def test_auth_and_nonblank_owner_precede_body_and_service(self):
        parse = AsyncMock()
        create = AsyncMock()
        denied = AsyncMock(side_effect=HTTPException(
            status_code=403,
            detail="denied",
        ))
        with (
            patch.object(router_module, "_require_feature", new=denied),
            patch.object(router_module, "_parse_response_upload", new=parse),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
        ):
            response = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                content=b"not-read",
                headers={"content-type": "application/octet-stream"},
            )

        self.assertEqual(response.status_code, 403)
        parse.assert_not_awaited()
        create.assert_not_awaited()
        denied.assert_awaited_once()
        self.assertEqual(denied.await_args.args[1], "survey")

        blank_login = AsyncMock(return_value={"name": "no stable owner"})
        with (
            patch.object(
                router_module,
                "_require_feature",
                new=blank_login,
            ),
            patch.object(router_module, "_parse_response_upload", new=parse),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
        ):
            response = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                content=b"not-read",
                headers={"content-type": "application/octet-stream"},
            )
        self.assertEqual(response.status_code, 401)
        parse.assert_not_awaited()
        create.assert_not_awaited()

    async def test_valid_csv_uses_safe_filename_and_closes_upload(self):
        authorize = AsyncMock(return_value=LOGIN)
        create = AsyncMock(return_value=_response(filename="responses.csv"))
        closed: list[str] = []
        original_close = UploadFile.close

        async def track_close(upload: UploadFile) -> None:
            closed.append(str(upload.filename))
            await original_close(upload)

        with (
            patch.object(router_module, "_require_feature", new=authorize),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
            patch.object(UploadFile, "close", new=track_close),
        ):
            response = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={
                    "file": (
                        "folder\\responses.csv",
                        b"question\nanswer\n",
                        "text/csv",
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _response().model_dump(mode="json"))
        create.assert_awaited_once_with(
            OWNER_REF,
            SNAPSHOT_ID,
            "responses.csv",
            b"question\nanswer\n",
            LOGIN,
        )
        self.assertIn("folder\\responses.csv", closed)

    async def test_valid_xlsx_mime_reaches_session_service(self):
        authorize = AsyncMock(return_value=LOGIN)
        create = AsyncMock(return_value=_response(filename="responses.xlsx"))
        with (
            patch.object(router_module, "_require_feature", new=authorize),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
        ):
            response = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={
                    "file": (
                        "responses.xlsx",
                        b"xlsx-is-validated-by-the-service",
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet",
                    )
                },
            )

        self.assertEqual(response.status_code, 200)
        create.assert_awaited_once_with(
            OWNER_REF,
            SNAPSHOT_ID,
            "responses.xlsx",
            b"xlsx-is-validated-by-the-service",
            LOGIN,
        )

    async def test_request_shape_extension_mime_and_size_are_classified(self):
        authorize = AsyncMock(return_value=LOGIN)
        cases = (
            (
                {"content": b"plain", "headers": {"content-type": "text/plain"}},
                415,
            ),
            (
                {"files": {"file": ("responses.xls", b"x", "application/vnd.ms-excel")}},
                422,
            ),
            (
                {"files": {"file": ("responses.csv", b"x", "image/png")}},
                415,
            ),
            (
                {"files": {"file": ("responses.csv", b"", "text/csv")}},
                422,
            ),
            (
                {
                    "files": [
                        ("file", ("one.csv", b"x", "text/csv")),
                        ("file", ("two.csv", b"y", "text/csv")),
                    ]
                },
                422,
            ),
            (
                {
                    "files": {"other": ("responses.csv", b"x", "text/csv")}
                },
                422,
            ),
        )
        create = AsyncMock()
        with (
            patch.object(router_module, "_require_feature", new=authorize),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
        ):
            for kwargs, status_code in cases:
                with self.subTest(kwargs=kwargs):
                    response = await self._request(
                        "POST",
                        f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                        **kwargs,
                    )
                    self.assertEqual(response.status_code, status_code)

            response = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                content=b"",
                headers={
                    "content-type": "multipart/form-data; boundary=x",
                    "content-length": str(
                        MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES
                        + router_module._MULTIPART_OVERHEAD_BYTES
                        + 1
                    ),
                },
            )
            self.assertEqual(response.status_code, 413)
        create.assert_not_awaited()

    async def test_service_errors_have_fixed_safe_http_mapping(self):
        authorize = AsyncMock(return_value=LOGIN)
        cases = (
            (QuestionnaireSnapshotAnalysisNotFoundError("private"), 404),
            (QuestionnaireSnapshotAnalysisInvalidError("private"), 422),
            (QuestionnaireSnapshotAnalysisInternalError("private"), 500),
            (RuntimeError("private"), 500),
        )
        for error, status_code in cases:
            with self.subTest(error=type(error).__name__):
                create = AsyncMock(side_effect=error)
                with (
                    patch.object(
                        router_module,
                        "_require_feature",
                        new=authorize,
                    ),
                    patch.object(
                        QuestionnaireSnapshotAnalysisApi,
                        "create_session",
                        new=create,
                    ),
                ):
                    response = await self._request(
                        "POST",
                        f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                        files={
                            "file": (
                                "responses.csv",
                                b"question\nanswer\n",
                                "text/csv",
                            )
                        },
                    )
                self.assertEqual(response.status_code, status_code)
                self.assertNotIn("private", response.text)

    async def test_busy_fails_fast_without_starting_second_session(self):
        authorize = AsyncMock(return_value=LOGIN)
        started = asyncio.Event()
        finish = asyncio.Event()
        calls: list[str] = []

        async def create(self, owner_ref, snapshot_id, filename, content, login):
            calls.append(filename)
            started.set()
            await finish.wait()
            return _response(filename=filename)

        with (
            patch.object(router_module, "_require_feature", new=authorize),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
        ):
            first = asyncio.create_task(self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("first.csv", b"a\nb\n", "text/csv")},
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            second = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("second.csv", b"a\nb\n", "text/csv")},
            )
            self.assertEqual(second.status_code, 429)
            self.assertEqual(calls, ["first.csv"])
            finish.set()
            first_response = await asyncio.wait_for(first, timeout=1)

        self.assertEqual(first_response.status_code, 200)

    async def test_timeout_holds_gate_until_real_task_finishes(self):
        authorize = AsyncMock(return_value=LOGIN)
        started = asyncio.Event()
        finish = asyncio.Event()
        calls: list[str] = []

        async def create(self, owner_ref, snapshot_id, filename, content, login):
            calls.append(filename)
            started.set()
            await finish.wait()
            return _response(filename=filename)

        with (
            patch.object(router_module, "_require_feature", new=authorize),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
            patch.object(
                router_module,
                "_SESSION_CREATION_TIMEOUT_SECONDS",
                new=0.01,
            ),
        ):
            first = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("first.csv", b"a\nb\n", "text/csv")},
            )
            self.assertEqual(first.status_code, 504)
            await asyncio.wait_for(started.wait(), timeout=1)
            second = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("second.csv", b"a\nb\n", "text/csv")},
            )
            self.assertEqual(second.status_code, 429)
            self.assertEqual(calls, ["first.csv"])
            finish.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    async def test_cancellation_holds_gate_until_real_task_finishes(self):
        authorize = AsyncMock(return_value=LOGIN)
        started = asyncio.Event()
        finished = asyncio.Event()
        finish = asyncio.Event()
        calls: list[str] = []

        async def create(self, owner_ref, snapshot_id, filename, content, login):
            calls.append(filename)
            started.set()
            try:
                await finish.wait()
                return _response(filename=filename)
            finally:
                finished.set()

        with (
            patch.object(router_module, "_require_feature", new=authorize),
            patch.object(
                QuestionnaireSnapshotAnalysisApi,
                "create_session",
                new=create,
            ),
        ):
            first = asyncio.create_task(self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("first.csv", b"a\nb\n", "text/csv")},
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            second = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("second.csv", b"a\nb\n", "text/csv")},
            )
            self.assertEqual(second.status_code, 429)
            self.assertEqual(calls, ["first.csv"])

            finish.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await asyncio.sleep(0)
            third = await self._request(
                "POST",
                f"/api/questionnaire-sources/snapshots/{SNAPSHOT_ID}/analysis-sessions",
                files={"file": ("third.csv", b"a\nb\n", "text/csv")},
            )

        self.assertEqual(third.status_code, 200)
        self.assertEqual(calls, ["first.csv", "third.csv"])


if __name__ == "__main__":
    unittest.main()
