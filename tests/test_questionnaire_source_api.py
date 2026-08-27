from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request
import httpx

from app.routers.questionnaire_sources import (
    _parse_snapshot_upload,
    create_questionnaire_sources_router,
)
from app.routers import questionnaire_sources as questionnaire_sources_router
from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.questionnaire_source_api import (
    QuestionnaireSnapshotCatalogResponse,
)
from app.schemas.research_assets import MediaType, ResearchAssetCollection
from app.services import questionnaire_snapshot_api as snapshot_api_module
from app.services.questionnaire_snapshot_api import (
    QuestionnaireSnapshotApi,
    QuestionnaireSnapshotCatalogInvalidError,
    QuestionnaireSnapshotInternalError,
    QuestionnaireSnapshotNotFoundError,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetStorageError,
    ResearchAssetBundle,
    SnapshotCatalogEntry,
    SnapshotCatalogPage,
    SnapshotPackage,
    SnapshotPackageError,
    build_snapshot_package,
    parse_snapshot_package,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"
LOGIN = {"email": "snapshot-user@example.com", "name": "Snapshot User"}
OWNER_REF = "email:snapshot-user@example.com"


def _package_archive(
    owner_ref: str = OWNER_REF,
    *,
    snapshot_id: str | None = None,
    title: str | None = None,
) -> tuple[bytes, SnapshotPackage]:
    payload = json.loads(
        (FIXTURE_DIR / "google_forms.json").read_text(encoding="utf-8")
    )
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    if snapshot_id is not None:
        snapshot = snapshot.model_copy(update={"snapshot_id": snapshot_id})
    if title is not None:
        snapshot = snapshot.model_copy(update={"title": title})
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    media: dict[str, bytes] = {}
    assets = []
    for index, asset in enumerate(collection.assets):
        if asset.media_type == MediaType.IMAGE:
            content = f"api-snapshot-media-{index}".encode("utf-8")
            content_hash = hashlib.sha256(content).hexdigest()
            media[content_hash] = content
            asset = asset.model_copy(update={
                "content_hash": content_hash,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = collection.model_copy(update={
        "owner_ref": owner_ref,
        "sources": [
            source.model_copy(update={"owner_ref": owner_ref})
            for source in collection.sources
        ],
        "assets": assets,
    })
    bundle = ResearchAssetBundle(snapshot, collection)
    package = SnapshotPackage(bundle, media)
    return build_snapshot_package(owner_ref, bundle, media), package


def _catalog_entry(
    package: SnapshotPackage,
    owner_ref: str = OWNER_REF,
) -> SnapshotCatalogEntry:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    return SnapshotCatalogEntry(
        owner_ref=owner_ref,
        storage_key=hashlib.sha256(
            snapshot.snapshot_id.encode("utf-8")
        ).hexdigest(),
        snapshot_id=snapshot.snapshot_id,
        title=snapshot.title,
        provider=snapshot.provider,
        source_mode=snapshot.source_mode,
        collection_state=snapshot.collection_state,
        mapping_status=snapshot.mapping_status,
        item_count=snapshot.item_count,
        question_count=snapshot.question_count,
        asset_count=snapshot.asset_count,
        image_asset_count=sum(
            asset.media_type == MediaType.IMAGE
            for asset in collection.assets
        ),
        asset_reference_count=snapshot.asset_reference_count,
    )


class _SlowStorage:
    def __init__(self, inner: FileResearchAssetStorage) -> None:
        self.inner = inner

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        time.sleep(0.2)
        return self.inner.load_snapshot_package(owner_ref, snapshot_id)

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        time.sleep(0.2)
        self.inner.save_snapshot_package(owner_ref, package)


class _CorruptStoredPackageStorage:
    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        return None

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        raise SnapshotPackageError("private corrupt stored package path")


class _ReturningStorage:
    def __init__(self, returned: object) -> None:
        self.returned = returned

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        return self.returned

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        return None


class _CorruptLoadingStorage:
    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        raise SnapshotPackageError("private corrupt stored package path")

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        return None


class _CatalogReturningStorage:
    def __init__(
        self,
        page: object | None = None,
        *,
        error: Exception | None = None,
        packages: dict[str, object] | None = None,
    ) -> None:
        self.page = page
        self.error = error
        self.packages = packages or {}
        self.list_calls: list[tuple[str, str | None, int]] = []
        self.load_calls: list[tuple[str, str]] = []

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        self.load_calls.append((owner_ref, snapshot_id))
        return self.packages.get(snapshot_id)

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        return None

    def list_snapshot_catalog(
        self,
        owner_ref: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ):
        self.list_calls.append((owner_ref, cursor, limit))
        if self.error is not None:
            raise self.error
        return self.page


class _ChunkedReceive:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def __call__(self):
        index = self.calls
        self.calls += 1
        if index >= len(self.chunks):
            return {"type": "http.disconnect"}
        return {
            "type": "http.request",
            "body": self.chunks[index],
            "more_body": index < len(self.chunks) - 1,
        }


async def _call_asgi_upload(
    app: FastAPI,
    receive: _ChunkedReceive,
    content_type: bytes,
) -> tuple[int, dict]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    path = "/api/questionnaire-sources/snapshots"
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
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], json.loads(body)


def _multipart_body(
    content: bytes,
    *,
    filename: str = "snapshot.zip",
    field_name: str = "file",
) -> tuple[bytes, bytes]:
    boundary = b"snapshot-test-boundary"
    body = (
        b"--" + boundary + b"\r\n"
        + b'Content-Disposition: form-data; name="'
        + field_name.encode("ascii")
        + b'"; filename="'
        + filename.encode("ascii")
        + b'"\r\nContent-Type: application/zip\r\n\r\n'
        + content
        + b"\r\n--" + boundary + b"--\r\n"
    )
    return body, b"multipart/form-data; boundary=" + boundary


class QuestionnaireSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-source-api-test-",
        )
        self.storage = FileResearchAssetStorage(self.temporary.name)
        self.api = QuestionnaireSnapshotApi(self.storage)
        self.router = create_questionnaire_sources_router(self.api)
        self.app = FastAPI()
        self.app.include_router(self.router)
        self.archive, self.package = _package_archive()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def _request_api(self, method: str, path: str, **kwargs):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ) as require_feature:
                response = await client.request(method, path, **kwargs)
        require_feature.assert_awaited_once()
        self.assertEqual(require_feature.await_args.args[1], "survey")
        return response

    def _download_endpoint(self):
        path = "/api/questionnaire-sources/snapshots/{snapshot_id}/download"
        return next(
            route.endpoint
            for route in self.router.routes
            if route.path == path
        )

    def _snapshot_endpoint(self):
        path = "/api/questionnaire-sources/snapshots/{snapshot_id}"
        return next(
            route.endpoint
            for route in self.router.routes
            if route.path == path and "GET" in route.methods
        )

    def _catalog_endpoint(self):
        path = "/api/questionnaire-sources/snapshots"
        return next(
            route.endpoint
            for route in self.router.routes
            if route.path == path and "GET" in route.methods
        )

    @staticmethod
    def _download_request() -> Request:
        return Request({
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
        })

    @staticmethod
    async def _consume_download(response) -> bytes:
        return b"".join([
            chunk async for chunk in response.body_iterator
        ])

    async def test_upload_get_and_repeated_upload_are_safe_and_idempotent(self):
        first = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.ZIP", self.archive, "application/zip")},
        )
        second = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", self.archive, "application/zip")},
        )
        loaded = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots/"
            + self.package.bundle.snapshot.snapshot_id,
        )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(loaded.json(), first.json())
        self.assertEqual(set(first.json()), {
            "schema_version",
            "snapshot_id",
            "display_title",
            "provider",
            "source_mode",
            "collection_state",
            "mapping_status",
            "item_count",
            "question_count",
            "asset_count",
            "image_asset_count",
            "asset_reference_count",
        })
        self.assertEqual(
            first.json()["asset_reference_count"],
            self.package.bundle.snapshot.asset_reference_count,
        )
        self.assertEqual(
            first.json()["display_title"],
            self.package.bundle.snapshot.title,
        )
        serialized = json.dumps(first.json(), ensure_ascii=False).casefold()
        for forbidden in (
            "owner_ref",
            OWNER_REF,
            "provider_raw_definition",
            "manifest.json",
            "media/",
            self.temporary.name.casefold(),
        ):
            self.assertNotIn(forbidden.casefold(), serialized)

    async def test_google_display_title_is_normalized_without_rewriting_snapshot(self):
        raw_title = " \t研究\n问卷\x00\u202e标题  " + ("长" * 220)
        archive, package = _package_archive(title=raw_title)

        saved = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", archive, "application/zip")},
        )
        catalog = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots",
        )

        expected = ("研究 问卷 标题 " + ("长" * 220))[:200]
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["display_title"], expected)
        self.assertEqual(catalog.status_code, 200)
        self.assertEqual(catalog.json()["items"][0]["display_title"], expected)
        stored = self.storage.load_snapshot_package(
            OWNER_REF,
            package.bundle.snapshot.snapshot_id,
        )
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.bundle.snapshot.title, raw_title)

    async def test_catalog_empty_pagination_owner_isolation_and_safe_fields(self):
        empty = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots",
        )
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json(), {
            "schema_version": 1,
            "items": [],
            "next_cursor": None,
        })

        _, second_package = _package_archive(
            snapshot_id="qsn_catalog_second",
        )
        _, foreign_package = _package_archive(
            "email:other@example.com",
            snapshot_id="qsn_catalog_foreign",
        )
        self.storage.save_snapshot_package(OWNER_REF, self.package)
        self.storage.save_snapshot_package(OWNER_REF, second_package)
        self.storage.save_snapshot_package(
            "email:other@example.com",
            foreign_package,
        )

        first = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots?limit=1",
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(set(first.json()), {
            "schema_version",
            "items",
            "next_cursor",
        })
        self.assertEqual(len(first.json()["items"]), 1)
        cursor = first.json()["next_cursor"]
        self.assertRegex(cursor, r"^[0-9a-f]{64}$")

        second = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots",
            params={"limit": "1", "cursor": cursor},
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(second.json()["items"]), 1)
        self.assertIsNone(second.json()["next_cursor"])
        summaries = first.json()["items"] + second.json()["items"]
        self.assertEqual(
            {item["snapshot_id"] for item in summaries},
            {
                self.package.bundle.snapshot.snapshot_id,
                second_package.bundle.snapshot.snapshot_id,
            },
        )
        expected_summary_fields = {
            "schema_version",
            "snapshot_id",
            "display_title",
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
        self.assertTrue(all(
            set(item) == expected_summary_fields
            for item in summaries
        ))

        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value={"email": "other@example.com"}),
            ):
                foreign = await client.get(
                    "/api/questionnaire-sources/snapshots"
                )
        self.assertEqual(foreign.status_code, 200)
        self.assertEqual(
            [item["snapshot_id"] for item in foreign.json()["items"]],
            [foreign_package.bundle.snapshot.snapshot_id],
        )

        serialized = json.dumps(
            first.json() | {"second": second.json()},
            ensure_ascii=False,
        ).casefold()
        for forbidden in (
            "owner_ref",
            OWNER_REF,
            "other@example.com",
            "provider_raw_definition",
            "manifest.json",
            "media/",
            self.temporary.name.casefold(),
        ):
            self.assertNotIn(forbidden.casefold(), serialized)

    async def test_catalog_query_is_strict_and_forwards_valid_values(self):
        cursor = "a" * 64
        invalid_paths = (
            "/api/questionnaire-sources/snapshots?unknown=1",
            "/api/questionnaire-sources/snapshots?limit=1&limit=2",
            (
                "/api/questionnaire-sources/snapshots?cursor="
                f"{cursor}&cursor={'b' * 64}"
            ),
            "/api/questionnaire-sources/snapshots?limit=",
            "/api/questionnaire-sources/snapshots?limit=0",
            "/api/questionnaire-sources/snapshots?limit=51",
            "/api/questionnaire-sources/snapshots?limit=-1",
            "/api/questionnaire-sources/snapshots?limit=1.0",
            "/api/questionnaire-sources/snapshots?limit=%EF%BC%91",
            "/api/questionnaire-sources/snapshots?cursor=",
            "/api/questionnaire-sources/snapshots?cursor=ABCDEF",
            "/api/questionnaire-sources/snapshots?cursor=not-opaque",
        )
        list_snapshots = AsyncMock()
        with patch.object(
            QuestionnaireSnapshotApi,
            "list_snapshots",
            new=list_snapshots,
        ):
            for path in invalid_paths:
                with self.subTest(path=path):
                    response = await self._request_api("GET", path)
                    self.assertEqual(response.status_code, 422)
                    self.assertEqual(response.json(), {
                        "detail": "问卷快照目录查询参数无效",
                    })
        list_snapshots.assert_not_awaited()

        response = QuestionnaireSnapshotCatalogResponse(items=[])
        list_snapshots = AsyncMock(return_value=response)
        with patch.object(
            QuestionnaireSnapshotApi,
            "list_snapshots",
            new=list_snapshots,
        ):
            valid = await self._request_api(
                "GET",
                "/api/questionnaire-sources/snapshots",
                params={"cursor": cursor, "limit": "7"},
            )
        self.assertEqual(valid.status_code, 200)
        list_snapshots.assert_awaited_once_with(
            OWNER_REF,
            cursor=cursor,
            limit=7,
        )

    async def test_catalog_service_rejects_invalid_cursor_and_limit(self):
        storage = _CatalogReturningStorage(SnapshotCatalogPage((), None))
        api = QuestionnaireSnapshotApi(storage)
        empty = await api.list_snapshots(OWNER_REF)
        self.assertEqual(empty, QuestionnaireSnapshotCatalogResponse(items=[]))
        self.assertEqual(storage.list_calls, [(OWNER_REF, None, 20)])
        storage.list_calls.clear()

        invalid_arguments = (
            {"cursor": ""},
            {"cursor": "A" * 64},
            {"cursor": "g" * 64},
            {"cursor": "a" * 63},
            {"limit": 0},
            {"limit": 51},
            {"limit": True},
            {"limit": "20"},
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                with self.assertRaises(
                    QuestionnaireSnapshotCatalogInvalidError
                ):
                    await api.list_snapshots(OWNER_REF, **arguments)
        self.assertEqual(storage.list_calls, [])

    async def test_catalog_service_consumes_lightweight_entries_without_load(self):
        _, second_package = _package_archive(
            snapshot_id="qsn_catalog_lightweight_second",
        )
        entries = sorted(
            (_catalog_entry(self.package), _catalog_entry(second_package)),
            key=lambda entry: entry.storage_key,
        )
        storage = _CatalogReturningStorage(
            SnapshotCatalogPage(tuple(entries), None),
            packages={
                entry.snapshot_id: self.package
                for entry in entries
            },
        )

        response = await QuestionnaireSnapshotApi(storage).list_snapshots(
            OWNER_REF,
            limit=2,
        )

        self.assertEqual(
            [item.snapshot_id for item in response.items],
            [entry.snapshot_id for entry in entries],
        )
        self.assertEqual(storage.load_calls, [])

    async def test_catalog_fails_closed_for_corrupt_storage_output(self):
        _, foreign_package = _package_archive(
            "email:other@example.com",
            snapshot_id="qsn_catalog_foreign_corrupt",
        )
        local_entry = _catalog_entry(self.package)
        foreign_entry = _catalog_entry(
            foreign_package,
            "email:other@example.com",
        )
        cases = (
            _CatalogReturningStorage(
                {"private": "/secret/catalog"},
            ),
            _CatalogReturningStorage(
                SnapshotCatalogPage((foreign_entry,), None),
            ),
            _CatalogReturningStorage(
                SnapshotCatalogPage((local_entry._replace(
                    storage_key="0" * 64,
                ),), None),
            ),
            _CatalogReturningStorage(
                SnapshotCatalogPage((local_entry, local_entry), None),
            ),
            _CatalogReturningStorage(
                SnapshotCatalogPage(("private-invalid-entry",), None),
            ),
            _CatalogReturningStorage(
                SnapshotCatalogPage((local_entry,), "invalid-private-cursor"),
            ),
        )
        for storage in cases:
            with self.subTest(page_type=type(storage.page).__name__):
                api = QuestionnaireSnapshotApi(storage)
                with self.assertRaises(QuestionnaireSnapshotInternalError):
                    await api.list_snapshots(OWNER_REF)

        app = FastAPI()
        app.include_router(create_questionnaire_sources_router(
            QuestionnaireSnapshotApi(_CatalogReturningStorage(
                SnapshotCatalogPage((foreign_entry,), None),
            )),
        ))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ):
                response = await client.get(
                    "/api/questionnaire-sources/snapshots"
                )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {
            "detail": "问卷快照服务暂时不可用",
        })
        self.assertNotIn("other@example.com", response.text)

    async def test_catalog_storage_failure_returns_generic_500(self):
        storage = _CatalogReturningStorage(error=ResearchAssetStorageError(
            "private catalog storage path",
        ))
        app = FastAPI()
        app.include_router(create_questionnaire_sources_router(
            QuestionnaireSnapshotApi(storage),
        ))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ):
                response = await client.get(
                    "/api/questionnaire-sources/snapshots"
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {
            "detail": "问卷快照服务暂时不可用",
        })
        self.assertNotIn("private catalog storage path", response.text)

    async def test_catalog_authentication_precedes_query_and_storage(self):
        storage = _CatalogReturningStorage(SnapshotCatalogPage((), None))
        app = FastAPI()
        app.include_router(create_questionnaire_sources_router(
            QuestionnaireSnapshotApi(storage),
        ))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            for status, login_or_error in (
                (401, None),
                (403, HTTPException(status_code=403, detail="denied")),
            ):
                with self.subTest(status=status):
                    authorize = (
                        AsyncMock(return_value=login_or_error)
                        if status == 401
                        else AsyncMock(side_effect=login_or_error)
                    )
                    with patch(
                        "app.routers.questionnaire_sources._require_feature",
                        new=authorize,
                    ):
                        response = await client.get(
                            "/api/questionnaire-sources/snapshots"
                            "?unknown=private-query"
                        )
                    self.assertEqual(response.status_code, status)
        self.assertEqual(storage.list_calls, [])

    async def test_catalog_admission_rejects_concurrent_read_without_new_task(self):
        endpoint = self._catalog_endpoint()
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def fake_list(
            api,
            owner_ref: str,
            cursor: str | None = None,
            limit: int = 20,
        ) -> QuestionnaireSnapshotCatalogResponse:
            calls.append(owner_ref)
            started.set()
            await release.wait()
            return QuestionnaireSnapshotCatalogResponse(items=[])

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "list_snapshots",
                new=fake_list,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first = asyncio.create_task(endpoint(self._download_request()))
            await asyncio.wait_for(started.wait(), timeout=1)
            with self.assertRaises(HTTPException) as rejected:
                await endpoint(self._download_request())
            self.assertEqual(rejected.exception.status_code, 429)
            self.assertEqual(
                rejected.exception.detail,
                "已有问卷快照目录正在读取，请稍后重试",
            )
            self.assertEqual(calls, [OWNER_REF])

            release.set()
            self.assertEqual(
                await asyncio.wait_for(first, timeout=1),
                QuestionnaireSnapshotCatalogResponse(items=[]),
            )

    async def test_cancelled_catalog_read_holds_admission_until_task_finishes(self):
        endpoint = self._catalog_endpoint()
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def fake_list(
            api,
            owner_ref: str,
            cursor: str | None = None,
            limit: int = 20,
        ) -> QuestionnaireSnapshotCatalogResponse:
            calls.append(owner_ref)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return QuestionnaireSnapshotCatalogResponse(items=[])

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "list_snapshots",
                new=fake_list,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first = asyncio.create_task(endpoint(self._download_request()))
            await asyncio.wait_for(started.wait(), timeout=1)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first

            with self.assertRaises(HTTPException) as rejected:
                await endpoint(self._download_request())
            self.assertEqual(rejected.exception.status_code, 429)
            self.assertEqual(
                rejected.exception.detail,
                "已有问卷快照目录正在读取，请稍后重试",
            )
            self.assertEqual(calls, [OWNER_REF])

            release.set()
            for _ in range(100):
                await asyncio.sleep(0.005)
                try:
                    result = await endpoint(self._download_request())
                except HTTPException as error:
                    if error.status_code == 429:
                        continue
                    raise
                break
            else:
                self.fail("catalog admission was not released")
            self.assertEqual(
                result,
                QuestionnaireSnapshotCatalogResponse(items=[]),
            )
            self.assertEqual(calls, [OWNER_REF, OWNER_REF])

    async def test_catalog_timeout_keeps_admission_until_task_finishes(self):
        endpoint = self._catalog_endpoint()
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def fake_list(
            api,
            owner_ref: str,
            cursor: str | None = None,
            limit: int = 20,
        ) -> QuestionnaireSnapshotCatalogResponse:
            calls.append(owner_ref)
            if len(calls) == 1:
                started.set()
                await release.wait()
            return QuestionnaireSnapshotCatalogResponse(items=[])

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "list_snapshots",
                new=fake_list,
            ),
            patch.object(
                questionnaire_sources_router,
                "_CATALOG_TIMEOUT_SECONDS",
                0.02,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            with self.assertRaises(HTTPException) as timed_out:
                await endpoint(self._download_request())
            self.assertEqual(timed_out.exception.status_code, 504)
            self.assertEqual(
                timed_out.exception.detail,
                "问卷快照目录读取超时，请稍后重试",
            )
            self.assertTrue(started.is_set())

            with self.assertRaises(HTTPException) as rejected:
                await endpoint(self._download_request())
            self.assertEqual(rejected.exception.status_code, 429)
            self.assertEqual(
                rejected.exception.detail,
                "已有问卷快照目录正在读取，请稍后重试",
            )
            self.assertEqual(calls, [OWNER_REF])

            release.set()
            for _ in range(100):
                await asyncio.sleep(0.005)
                try:
                    result = await endpoint(self._download_request())
                except HTTPException as error:
                    if error.status_code == 429:
                        continue
                    raise
                break
            else:
                self.fail("timed-out catalog task did not release admission")
            self.assertEqual(
                result,
                QuestionnaireSnapshotCatalogResponse(items=[]),
            )
            self.assertEqual(calls, [OWNER_REF, OWNER_REF])

    async def test_download_roundtrips_full_bundle_and_is_deterministic(self):
        created = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", self.archive, "application/zip")},
        )
        path = (
            "/api/questionnaire-sources/snapshots/"
            + self.package.bundle.snapshot.snapshot_id
            + "/download"
        )
        first = await self._request_api("GET", path)
        second = await self._request_api("GET", path)

        self.assertEqual(created.status_code, 200)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.content, second.content)
        self.assertEqual(first.content, self.archive)
        exported = parse_snapshot_package(OWNER_REF, first.content)
        self.assertEqual(exported.bundle, self.package.bundle)
        self.assertEqual(exported.media, self.package.media)
        self.assertEqual(first.headers["content-type"], "application/zip")
        self.assertEqual(first.headers["cache-control"], "private, no-store")
        self.assertEqual(
            first.headers["content-disposition"],
            'attachment; filename="questionnaire-snapshot.zip"',
        )
        self.assertEqual(
            first.headers["content-length"],
            str(len(first.content)),
        )

    async def test_downloads_are_serialized_until_response_body_finishes(self):
        endpoint = self._download_endpoint()
        calls: list[str] = []

        async def fake_export(api, owner_ref: str, snapshot_id: str) -> bytes:
            self.assertIs(api, self.api)
            self.assertEqual(owner_ref, OWNER_REF)
            calls.append(snapshot_id)
            return f"content:{snapshot_id}".encode("ascii")

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "export_snapshot",
                new=fake_export,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first = await endpoint("first", self._download_request())
            second_task = asyncio.create_task(
                endpoint("second", self._download_request())
            )
            await asyncio.sleep(0.03)

            self.assertEqual(calls, ["first"])
            self.assertFalse(second_task.done())
            self.assertEqual(
                await self._consume_download(first),
                b"content:first",
            )

            second = await asyncio.wait_for(second_task, timeout=1)
            self.assertEqual(calls, ["first", "second"])
            self.assertEqual(
                await self._consume_download(second),
                b"content:second",
            )

    async def test_unstarted_download_iterator_close_releases_slot(self):
        endpoint = self._download_endpoint()

        async def fake_export(api, owner_ref: str, snapshot_id: str) -> bytes:
            return f"content:{snapshot_id}".encode("ascii")

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "export_snapshot",
                new=fake_export,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first = await endpoint("first", self._download_request())
            await first.body_iterator.aclose()
            second = await asyncio.wait_for(
                endpoint("second", self._download_request()),
                timeout=1,
            )

            self.assertEqual(
                await self._consume_download(second),
                b"content:second",
            )

    async def test_summary_and_download_share_package_read_limit(self):
        summary_endpoint = self._snapshot_endpoint()
        download_endpoint = self._download_endpoint()
        summary_started = asyncio.Event()
        finish_summary = asyncio.Event()
        export_calls: list[str] = []

        async def fake_get(api, owner_ref: str, snapshot_id: str):
            summary_started.set()
            await finish_summary.wait()
            return snapshot_api_module._summary(self.package)

        async def fake_export(api, owner_ref: str, snapshot_id: str) -> bytes:
            export_calls.append(snapshot_id)
            return b"download"

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "get_snapshot",
                new=fake_get,
            ),
            patch.object(
                QuestionnaireSnapshotApi,
                "export_snapshot",
                new=fake_export,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            summary_task = asyncio.create_task(
                summary_endpoint("summary", self._download_request())
            )
            await asyncio.wait_for(summary_started.wait(), timeout=1)
            download_task = asyncio.create_task(
                download_endpoint("download", self._download_request())
            )
            await asyncio.sleep(0.03)

            self.assertEqual(export_calls, [])
            self.assertFalse(download_task.done())
            finish_summary.set()
            summary = await asyncio.wait_for(summary_task, timeout=1)
            self.assertEqual(
                summary.snapshot_id,
                self.package.bundle.snapshot.snapshot_id,
            )

            download = await asyncio.wait_for(download_task, timeout=1)
            self.assertEqual(export_calls, ["download"])
            self.assertEqual(
                await self._consume_download(download),
                b"download",
            )

    async def test_cancelled_summary_holds_slot_until_load_task_finishes(self):
        summary_endpoint = self._snapshot_endpoint()
        download_endpoint = self._download_endpoint()
        summary_started = asyncio.Event()
        finish_summary = asyncio.Event()
        export_calls: list[str] = []

        async def fake_get(api, owner_ref: str, snapshot_id: str):
            summary_started.set()
            await finish_summary.wait()
            return snapshot_api_module._summary(self.package)

        async def fake_export(api, owner_ref: str, snapshot_id: str) -> bytes:
            export_calls.append(snapshot_id)
            return b"download"

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "get_snapshot",
                new=fake_get,
            ),
            patch.object(
                QuestionnaireSnapshotApi,
                "export_snapshot",
                new=fake_export,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            summary_task = asyncio.create_task(
                summary_endpoint("summary", self._download_request())
            )
            await asyncio.wait_for(summary_started.wait(), timeout=1)
            summary_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await summary_task

            download_task = asyncio.create_task(
                download_endpoint("download", self._download_request())
            )
            await asyncio.sleep(0.03)
            self.assertEqual(export_calls, [])
            self.assertFalse(download_task.done())

            finish_summary.set()
            download = await asyncio.wait_for(download_task, timeout=1)
            self.assertEqual(export_calls, ["download"])
            self.assertEqual(
                await self._consume_download(download),
                b"download",
            )

    async def test_cancelled_export_holds_slot_until_worker_task_finishes(self):
        endpoint = self._download_endpoint()
        export_started = asyncio.Event()
        finish_export = asyncio.Event()
        calls: list[str] = []

        async def fake_export(api, owner_ref: str, snapshot_id: str) -> bytes:
            calls.append(snapshot_id)
            if snapshot_id == "first":
                export_started.set()
                await finish_export.wait()
            return f"content:{snapshot_id}".encode("ascii")

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "export_snapshot",
                new=fake_export,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first_task = asyncio.create_task(
                endpoint("first", self._download_request())
            )
            await asyncio.wait_for(export_started.wait(), timeout=1)
            first_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_task

            second_task = asyncio.create_task(
                endpoint("second", self._download_request())
            )
            await asyncio.sleep(0.03)
            self.assertEqual(calls, ["first"])
            self.assertFalse(second_task.done())

            finish_export.set()
            second = await asyncio.wait_for(second_task, timeout=1)
            self.assertEqual(calls, ["first", "second"])
            self.assertEqual(
                await self._consume_download(second),
                b"content:second",
            )

    async def test_cancelled_response_send_releases_download_slot(self):
        endpoint = self._download_endpoint()
        calls: list[str] = []

        async def fake_export(api, owner_ref: str, snapshot_id: str) -> bytes:
            calls.append(snapshot_id)
            return f"content:{snapshot_id}".encode("ascii")

        async def receive() -> dict:
            return {"type": "http.disconnect"}

        async def cancel_on_body(message: dict) -> None:
            if message["type"] == "http.response.body":
                raise asyncio.CancelledError()

        with (
            patch.object(
                QuestionnaireSnapshotApi,
                "export_snapshot",
                new=fake_export,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first = await endpoint("first", self._download_request())
            second_task = asyncio.create_task(
                endpoint("second", self._download_request())
            )
            await asyncio.sleep(0.03)
            self.assertFalse(second_task.done())

            try:
                await first(
                    self._download_request().scope,
                    receive,
                    cancel_on_body,
                )
            except asyncio.CancelledError:
                pass

            second = await asyncio.wait_for(second_task, timeout=1)
            self.assertEqual(calls, ["first", "second"])
            self.assertEqual(
                await self._consume_download(second),
                b"content:second",
            )

    async def test_conflicting_immutable_snapshot_returns_safe_409(self):
        created = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", self.archive, "application/zip")},
        )
        changed_archive, _ = _package_archive(title="changed private title")
        conflict = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={
                "file": (
                    "snapshot.zip",
                    changed_archive,
                    "application/zip",
                ),
            },
        )

        self.assertEqual(created.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertNotIn("changed private title", conflict.text)
        self.assertEqual(conflict.json(), {
            "detail": "同一快照 ID 已存在不同内容",
        })

    async def test_damaged_and_wrong_owner_packages_return_safe_422(self):
        wrong_owner_archive, _ = _package_archive("email:other@example.com")
        damaged = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", b"not-a-zip", "application/zip")},
        )
        wrong_owner = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={
                "file": (
                    "snapshot.zip",
                    wrong_owner_archive,
                    "application/zip",
                ),
            },
        )

        self.assertEqual(damaged.status_code, 422)
        self.assertEqual(wrong_owner.status_code, 422)
        self.assertEqual(damaged.json(), wrong_owner.json())
        self.assertNotIn("other@example.com", wrong_owner.text)

    async def test_missing_and_cross_owner_snapshot_share_404(self):
        created = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", self.archive, "application/zip")},
        )
        self.assertEqual(created.status_code, 200)
        path = (
            "/api/questionnaire-sources/snapshots/"
            + self.package.bundle.snapshot.snapshot_id
        )
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value={"email": "other@example.com"}),
            ):
                cross_owner = await client.get(path)
        missing = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots/unknown-snapshot",
        )

        self.assertEqual(cross_owner.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(cross_owner.json(), missing.json())

    async def test_download_missing_and_cross_owner_snapshot_share_404(self):
        created = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", self.archive, "application/zip")},
        )
        self.assertEqual(created.status_code, 200)
        path = (
            "/api/questionnaire-sources/snapshots/"
            + self.package.bundle.snapshot.snapshot_id
            + "/download"
        )
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value={"email": "other@example.com"}),
            ):
                cross_owner = await client.get(path)
        missing = await self._request_api(
            "GET",
            "/api/questionnaire-sources/snapshots/unknown-snapshot/download",
        )

        self.assertEqual(cross_owner.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(cross_owner.json(), missing.json())

    async def test_export_invalid_snapshot_ids_are_not_found(self):
        for invalid_id in (None, "", " ", " padded-id "):
            with self.subTest(invalid_id=invalid_id):
                with self.assertRaises(QuestionnaireSnapshotNotFoundError):
                    await self.api.export_snapshot(OWNER_REF, invalid_id)

    async def test_download_forged_and_corrupt_storage_are_safe_500(self):
        tampered_media = dict(self.package.media)
        tampered_media[next(iter(tampered_media))] = b"private tampered media"
        cases = (
            (
                _ReturningStorage({"private": "/secret/forged-package.zip"}),
                "forged-snapshot",
                "/secret/forged-package.zip",
            ),
            (
                _ReturningStorage(SnapshotPackage(
                    self.package.bundle,
                    tampered_media,
                )),
                self.package.bundle.snapshot.snapshot_id,
                "private tampered media",
            ),
            (
                _CorruptLoadingStorage(),
                self.package.bundle.snapshot.snapshot_id,
                "private corrupt stored package path",
            ),
        )
        for storage, snapshot_id, private_detail in cases:
            with self.subTest(storage_type=type(storage).__name__):
                app = FastAPI()
                app.include_router(create_questionnaire_sources_router(
                    QuestionnaireSnapshotApi(storage),
                ))
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="http://test",
                ) as client:
                    with patch(
                        "app.routers.questionnaire_sources._require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ):
                        response = await client.get(
                            "/api/questionnaire-sources/snapshots/"
                            + snapshot_id
                            + "/download"
                        )

                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.json(), {
                    "detail": "问卷快照服务暂时不可用",
                })
                self.assertNotIn(private_detail, response.text)

    async def test_get_fails_closed_for_untrusted_storage_output(self):
        _, foreign_package = _package_archive("email:other@example.com")
        tampered_media = dict(self.package.media)
        tampered_media[next(iter(tampered_media))] = b"tampered"
        cases = (
            (foreign_package, foreign_package.bundle.snapshot.snapshot_id),
            (self.package, "different-snapshot-id"),
            ({"snapshot_id": "forged"}, "forged"),
            (
                SnapshotPackage(self.package.bundle, tampered_media),
                self.package.bundle.snapshot.snapshot_id,
            ),
        )
        for returned, requested_id in cases:
            with self.subTest(returned_type=type(returned).__name__):
                api = QuestionnaireSnapshotApi(_ReturningStorage(returned))
                with self.assertRaises(QuestionnaireSnapshotInternalError):
                    await api.get_snapshot(OWNER_REF, requested_id)

    async def test_empty_owner_is_401_for_upload_and_get(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=None),
            ):
                uploaded = await client.post(
                    "/api/questionnaire-sources/snapshots",
                    files={
                        "file": (
                            "snapshot.zip",
                            self.archive,
                            "application/zip",
                        ),
                    },
                )
                loaded = await client.get(
                    "/api/questionnaire-sources/snapshots/anything"
                )

        self.assertEqual(uploaded.status_code, 401)
        self.assertEqual(loaded.status_code, 401)
        self.assertIsNone(self.storage.load_snapshot_package(
            OWNER_REF,
            self.package.bundle.snapshot.snapshot_id,
        ))

    async def test_authorization_runs_before_upload_file_read(self):
        body, content_type = _multipart_body(self.archive)
        receive = _ChunkedReceive([body[:64], body[64:]])

        async def deny(request, feature: str):
            self.assertEqual(feature, "survey")
            self.assertEqual(receive.calls, 0)
            raise HTTPException(status_code=403, detail="denied")

        with patch(
            "app.routers.questionnaire_sources._require_feature",
            new=deny,
        ):
            status, payload = await _call_asgi_upload(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 403)
        self.assertEqual(payload, {"detail": "denied"})
        self.assertEqual(receive.calls, 0)

    async def test_download_authorization_runs_before_storage_load(self):
        transport = httpx.ASGITransport(app=self.app)

        async def deny(request, feature: str):
            self.assertEqual(feature, "survey")
            raise HTTPException(status_code=403, detail="denied")

        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with (
                patch(
                    "app.routers.questionnaire_sources._require_feature",
                    new=deny,
                ),
                patch.object(
                    self.storage,
                    "load_snapshot_package",
                    wraps=self.storage.load_snapshot_package,
                ) as load_snapshot,
            ):
                response = await client.get(
                    "/api/questionnaire-sources/snapshots/anything/download"
                )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "denied"})
        load_snapshot.assert_not_called()

    async def test_upload_extension_empty_and_bounded_size_validation(self):
        wrong = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.json", self.archive, "application/zip")},
        )
        blank = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"file": ("snapshot.zip", b"", "application/zip")},
        )
        with patch.object(
            questionnaire_sources_router,
            "MAX_SNAPSHOT_UPLOAD_BYTES",
            4,
        ):
            large = await self._request_api(
                "POST",
                "/api/questionnaire-sources/snapshots",
                files={"file": ("snapshot.zip", b"12345", "application/zip")},
            )

        self.assertEqual(wrong.status_code, 422)
        self.assertEqual(blank.status_code, 422)
        self.assertEqual(large.status_code, 413)

    async def test_multipart_total_limit_stops_request_stream_early(self):
        body, content_type = _multipart_body(self.archive)
        chunks = [body[:64], body[64:128], body[128:]]
        receive = _ChunkedReceive(chunks)
        with (
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                questionnaire_sources_router,
                "MAX_SNAPSHOT_UPLOAD_BYTES",
                4,
            ),
            patch.object(
                questionnaire_sources_router,
                "_MAX_MULTIPART_OVERHEAD_BYTES",
                32,
            ),
        ):
            status, payload = await _call_asgi_upload(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 413)
        self.assertEqual(payload, {
            "detail": "问卷快照包超过上传大小限制",
        })
        self.assertEqual(receive.calls, 1)
        self.assertLess(receive.calls, len(chunks))

    async def test_malformed_multipart_is_422_and_closes_temporary_file(self):
        boundary = b"snapshot-test-boundary"
        malformed = (
            b"--" + boundary + b"\r\n"
            b'Content-Disposition: form-data; name="file"; '
            b'filename="snapshot.zip"\r\n'
            b"Content-Type: application/zip\r\n\r\n"
            + self.archive
            + b"\r\n--"
            + boundary
            + b"\r\nbroken header\r\n\r\n"
        )
        content_type = b"multipart/form-data; boundary=" + boundary
        receive = _ChunkedReceive([malformed])
        tracked_files = []
        original_parser = questionnaire_sources_router.MultiPartParser

        class TrackingParser(original_parser):
            async def parse(self):
                try:
                    return await super().parse()
                finally:
                    tracked_files.extend(self._files_to_close_on_error)

        with (
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                questionnaire_sources_router,
                "MultiPartParser",
                TrackingParser,
            ),
        ):
            status, payload = await _call_asgi_upload(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 422)
        self.assertEqual(payload, {
            "detail": "问卷快照上传请求无效",
        })
        self.assertTrue(tracked_files)
        self.assertTrue(all(file.closed for file in tracked_files))

    async def test_multipart_parser_native_error_is_safe_422(self):
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ):
                response = await client.post(
                    "/api/questionnaire-sources/snapshots",
                    content=b"private malformed multipart detail",
                    headers={
                        "content-type": (
                            "multipart/form-data; boundary=broken-boundary"
                        ),
                    },
                )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {
            "detail": "问卷快照上传请求无效",
        })
        self.assertNotIn("private malformed", response.text)

    async def test_multipart_stream_failure_is_safe_500_and_closes_file(self):
        body, content_type = _multipart_body(self.archive)
        header_end = body.index(b"\r\n\r\n") + 4
        first_chunk = body[:header_end + 1]
        calls = 0

        async def failing_receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "type": "http.request",
                    "body": first_chunk,
                    "more_body": True,
                }
            raise RuntimeError("private stream failure")

        tracked_files = []
        original_parser = questionnaire_sources_router.MultiPartParser

        class TrackingParser(original_parser):
            async def parse(self):
                try:
                    return await super().parse()
                finally:
                    tracked_files.extend(self._files_to_close_on_error)

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/questionnaire-sources/snapshots",
                "raw_path": b"/api/questionnaire-sources/snapshots",
                "query_string": b"",
                "headers": [(b"content-type", content_type)],
                "client": ("test", 1),
                "server": ("test", 80),
            },
            receive=failing_receive,
        )
        with patch.object(
            questionnaire_sources_router,
            "MultiPartParser",
            TrackingParser,
        ):
            with self.assertRaises(HTTPException) as caught:
                await _parse_snapshot_upload(request)

        self.assertEqual(caught.exception.status_code, 500)
        self.assertNotIn("private stream failure", caught.exception.detail)
        self.assertTrue(tracked_files)
        self.assertTrue(all(file.closed for file in tracked_files))

    async def test_multipart_cancellation_propagates_and_closes_file(self):
        body, content_type = _multipart_body(self.archive)
        header_end = body.index(b"\r\n\r\n") + 4
        first_chunk = body[:header_end + 1]
        calls = 0

        async def cancelling_receive():
            nonlocal calls
            calls += 1
            if calls == 1:
                return {
                    "type": "http.request",
                    "body": first_chunk,
                    "more_body": True,
                }
            raise asyncio.CancelledError()

        tracked_files = []
        original_parser = questionnaire_sources_router.MultiPartParser

        class TrackingParser(original_parser):
            async def parse(self):
                try:
                    return await super().parse()
                finally:
                    tracked_files.extend(self._files_to_close_on_error)

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/questionnaire-sources/snapshots",
                "raw_path": b"/api/questionnaire-sources/snapshots",
                "query_string": b"",
                "headers": [(b"content-type", content_type)],
                "client": ("test", 1),
                "server": ("test", 80),
            },
            receive=cancelling_receive,
        )
        with patch.object(
            questionnaire_sources_router,
            "MultiPartParser",
            TrackingParser,
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _parse_snapshot_upload(request)

        self.assertTrue(tracked_files)
        self.assertTrue(all(file.closed for file in tracked_files))

    async def test_parser_constructor_supports_legacy_starlette_signature(self):
        constructed = []

        class LegacyParser:
            def __init__(
                self,
                headers,
                stream,
                *,
                max_files,
                max_fields,
            ) -> None:
                self._files_to_close_on_error = []
                constructed.append((max_files, max_fields))

            async def parse(self):
                return questionnaire_sources_router.FormData()

        request = Request({
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/questionnaire-sources/snapshots",
            "raw_path": b"/api/questionnaire-sources/snapshots",
            "query_string": b"",
            "headers": [
                (
                    b"content-type",
                    b"multipart/form-data; boundary=legacy-test",
                ),
            ],
            "client": ("test", 1),
            "server": ("test", 80),
        })
        with patch.object(
            questionnaire_sources_router,
            "MultiPartParser",
            LegacyParser,
        ):
            form = await _parse_snapshot_upload(request)
        await form.close()
        self.assertEqual(constructed, [(1, 0)])

    async def test_upload_requires_one_file_field_only(self):
        wrong_field = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files={"other": ("snapshot.zip", self.archive, "application/zip")},
        )
        extra_file = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            files=[
                ("file", ("snapshot.zip", self.archive, "application/zip")),
                ("file", ("extra.zip", self.archive, "application/zip")),
            ],
        )
        text_field = await self._request_api(
            "POST",
            "/api/questionnaire-sources/snapshots",
            data={"file": "not a file"},
        )

        self.assertEqual(wrong_field.status_code, 422)
        self.assertEqual(extra_file.status_code, 422)
        self.assertEqual(text_field.status_code, 422)

    async def test_unknown_failures_return_generic_500(self):
        with patch.object(
            QuestionnaireSnapshotApi,
            "import_snapshot",
            new=AsyncMock(side_effect=RuntimeError("private storage path")),
        ):
            failed = await self._request_api(
                "POST",
                "/api/questionnaire-sources/snapshots",
                files={
                    "file": (
                        "snapshot.zip",
                        self.archive,
                        "application/zip",
                    ),
                },
            )
        with patch.object(
            QuestionnaireSnapshotApi,
            "get_snapshot",
            new=AsyncMock(side_effect=RuntimeError("private read path")),
        ):
            failed_get = await self._request_api(
                "GET",
                "/api/questionnaire-sources/snapshots/private-id",
            )

        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.json(), {
            "detail": "问卷快照服务暂时不可用",
        })
        self.assertNotIn("private storage path", failed.text)
        self.assertEqual(failed_get.status_code, 500)
        self.assertEqual(failed_get.json(), failed.json())
        self.assertNotIn("private read path", failed_get.text)

    async def test_valid_upload_with_corrupt_stored_state_returns_safe_500(self):
        corrupt_api = QuestionnaireSnapshotApi(_CorruptStoredPackageStorage())
        app = FastAPI()
        app.include_router(create_questionnaire_sources_router(corrupt_api))
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
                    "/api/questionnaire-sources/snapshots",
                    files={
                        "file": (
                            "snapshot.zip",
                            self.archive,
                            "application/zip",
                        ),
                    },
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {
            "detail": "问卷快照服务暂时不可用",
        })
        self.assertNotIn("private corrupt stored package path", response.text)

    async def test_initial_package_classification_is_offloaded(self):
        corrupt_api = QuestionnaireSnapshotApi(_CorruptStoredPackageStorage())
        parse_started = threading.Event()
        parser_threads: list[int] = []
        event_loop_thread = threading.get_ident()

        def slow_valid_parse(owner_ref: str, archive: bytes):
            parser_threads.append(threading.get_ident())
            parse_started.set()
            time.sleep(0.2)
            return self.package

        with patch(
            "app.services.questionnaire_snapshot_api.parse_snapshot_package",
            side_effect=slow_valid_parse,
        ) as parse_package:
            task = asyncio.create_task(
                corrupt_api.import_snapshot(OWNER_REF, self.archive)
            )
            for _ in range(100):
                if parse_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(parse_started.is_set())
            started = time.monotonic()
            await asyncio.sleep(0.03)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.15)
            self.assertFalse(task.done())
            with self.assertRaises(QuestionnaireSnapshotInternalError):
                await task

        parse_package.assert_called_once_with(OWNER_REF, self.archive)
        self.assertEqual(len(parser_threads), 1)
        self.assertNotEqual(parser_threads[0], event_loop_thread)

    async def test_real_split_brain_storage_returns_safe_500_not_409(self):
        split_temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-source-api-split-brain-test-",
        )
        self.addAsyncCleanup(asyncio.to_thread, split_temporary.cleanup)
        split_storage = FileResearchAssetStorage(split_temporary.name)
        changed_snapshot = self.package.bundle.snapshot.model_copy(update={
            "title": "private split brain title",
        })
        split_storage.save_bundle(
            OWNER_REF,
            ResearchAssetBundle(
                changed_snapshot,
                self.package.bundle.collection,
            ),
        )
        split_app = FastAPI()
        split_app.include_router(create_questionnaire_sources_router(
            QuestionnaireSnapshotApi(split_storage),
        ))
        transport = httpx.ASGITransport(app=split_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ):
                response = await client.post(
                    "/api/questionnaire-sources/snapshots",
                    files={
                        "file": (
                            "snapshot.zip",
                            self.archive,
                            "application/zip",
                        ),
                    },
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {
            "detail": "问卷快照服务暂时不可用",
        })
        self.assertNotIn("private split brain title", response.text)

    async def test_import_and_get_summary_are_offloaded(self):
        original_summary = snapshot_api_module._summary
        event_loop_thread = threading.get_ident()

        async def assert_offloaded(operation):
            summary_started = threading.Event()
            summary_threads: list[int] = []

            def slow_summary(*args, **kwargs):
                summary_threads.append(threading.get_ident())
                summary_started.set()
                time.sleep(0.2)
                return original_summary(*args, **kwargs)

            with patch.object(
                snapshot_api_module,
                "_summary",
                side_effect=slow_summary,
            ):
                task = asyncio.create_task(operation())
                for _ in range(100):
                    if summary_started.is_set():
                        break
                    await asyncio.sleep(0.005)
                self.assertTrue(summary_started.is_set())
                started = time.monotonic()
                await asyncio.sleep(0.03)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 0.15)
                self.assertFalse(task.done())
                result = await task

            self.assertEqual(len(summary_threads), 1)
            self.assertNotEqual(summary_threads[0], event_loop_thread)
            return result

        imported = await assert_offloaded(
            lambda: self.api.import_snapshot(OWNER_REF, self.archive)
        )
        loaded = await assert_offloaded(
            lambda: self.api.get_snapshot(
                OWNER_REF,
                self.package.bundle.snapshot.snapshot_id,
            )
        )
        self.assertEqual(imported, loaded)

    async def test_get_summary_validates_without_materializing_zip(self):
        self.storage.save_snapshot_package(OWNER_REF, self.package)
        with patch.object(
            snapshot_api_module,
            "build_snapshot_package",
            side_effect=AssertionError("summary must not build zip"),
        ) as build_package:
            summary = await self.api.get_snapshot(
                OWNER_REF,
                self.package.bundle.snapshot.snapshot_id,
            )

        self.assertEqual(
            summary.snapshot_id,
            self.package.bundle.snapshot.snapshot_id,
        )
        build_package.assert_not_called()

    async def test_sync_storage_is_offloaded_from_event_loop(self):
        slow_api = QuestionnaireSnapshotApi(_SlowStorage(self.storage))
        task = asyncio.create_task(
            slow_api.import_snapshot(OWNER_REF, self.archive)
        )
        started = time.monotonic()
        await asyncio.sleep(0.03)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertFalse(task.done())
        summary = await task
        self.assertEqual(
            summary.snapshot_id,
            self.package.bundle.snapshot.snapshot_id,
        )

    async def test_export_load_and_build_are_offloaded_from_event_loop(self):
        self.storage.save_snapshot_package(OWNER_REF, self.package)
        slow_api = QuestionnaireSnapshotApi(_SlowStorage(self.storage))
        original_build = snapshot_api_module.build_snapshot_package
        build_started = threading.Event()
        build_threads: list[int] = []
        event_loop_thread = threading.get_ident()

        def slow_build(*args, **kwargs):
            build_threads.append(threading.get_ident())
            build_started.set()
            time.sleep(0.2)
            return original_build(*args, **kwargs)

        with patch.object(
            snapshot_api_module,
            "build_snapshot_package",
            side_effect=slow_build,
        ):
            task = asyncio.create_task(slow_api.export_snapshot(
                OWNER_REF,
                self.package.bundle.snapshot.snapshot_id,
            ))
            started = time.monotonic()
            await asyncio.sleep(0.03)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.15)
            self.assertFalse(task.done())
            for _ in range(100):
                if build_started.is_set():
                    break
                await asyncio.sleep(0.005)
            self.assertTrue(build_started.is_set())
            started = time.monotonic()
            await asyncio.sleep(0.03)
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.15)
            self.assertFalse(task.done())
            content = await task

        self.assertEqual(content, self.archive)
        self.assertEqual(len(build_threads), 1)
        self.assertNotEqual(build_threads[0], event_loop_thread)


if __name__ == "__main__":
    unittest.main()
