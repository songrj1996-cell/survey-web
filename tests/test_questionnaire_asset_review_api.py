from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
from io import BytesIO
from pathlib import Path
import struct
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, patch
import zlib

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import ValidationError
from pypdf import PdfWriter

from app.routers import questionnaire_asset_reviews as review_router_module
from app.routers.questionnaire_asset_reviews import (
    create_questionnaire_asset_reviews_router,
)
from app.schemas.questionnaire_asset_review import (
    QuestionnaireAssetPreviewStatus,
    QuestionnaireAssetReviewProjection,
)
from app.schemas.research_assets import BindingStatus, MediaType
from app.services.questionnaire_asset_review_api import (
    QuestionnaireAssetReviewApi,
    QuestionnaireAssetReviewInternalError,
    QuestionnaireAssetReviewNotFoundError,
)
from app.services.questionnaire_material_snapshot_api import (
    QuestionnaireMaterialScreenshot,
    QuestionnaireMaterialSnapshotApi,
)
from app.services.questionnaire_pdf_material_snapshot_api import (
    QuestionnairePdfMaterial,
    QuestionnairePdfMaterialSnapshotApi,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    SnapshotCatalogEntry,
    SnapshotCatalogPage,
    SnapshotPackage,
)


LOGIN = {"email": "asset-review@example.com", "name": "Asset Review"}
OWNER_REF = "email:asset-review@example.com"
OTHER_LOGIN = {"email": "other-asset-review@example.com"}
FIXED_TIME = datetime(2026, 8, 17, 8, 0, tzinfo=timezone.utc)
SAFE_HEADERS = {
    "cache-control": "private, no-store",
    "x-content-type-options": "nosniff",
}


def _png(red: int = 17, green: int = 34, blue: int = 51) -> bytes:
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


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def _catalog_entry(owner_ref: str, package: SnapshotPackage) -> SnapshotCatalogEntry:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    return SnapshotCatalogEntry(
        owner_ref=owner_ref,
        storage_key=hashlib.sha256(snapshot.snapshot_id.encode("utf-8")).hexdigest(),
        snapshot_id=snapshot.snapshot_id,
        provider=snapshot.provider,
        source_mode=snapshot.source_mode,
        collection_state=snapshot.collection_state,
        mapping_status=snapshot.mapping_status,
        item_count=snapshot.item_count,
        question_count=snapshot.question_count,
        asset_count=snapshot.asset_count,
        image_asset_count=sum(
            asset.media_type == MediaType.IMAGE for asset in collection.assets
        ),
        asset_reference_count=snapshot.asset_reference_count,
    )


class _CatalogStorage:
    def __init__(
        self,
        owner_ref: str,
        package: SnapshotPackage | None,
        *,
        disappear_after_catalog: bool = False,
        block_catalog: threading.Event | None = None,
        release_catalog: threading.Event | None = None,
    ) -> None:
        self.owner_ref = owner_ref
        self.package = package
        self.disappear_after_catalog = disappear_after_catalog
        self.block_catalog = block_catalog
        self.release_catalog = release_catalog
        self.catalog_calls = 0
        self.load_calls = 0
        self.save_calls = 0

    def list_snapshot_catalog(
        self,
        owner_ref: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SnapshotCatalogPage:
        self.catalog_calls += 1
        if self.block_catalog is not None:
            self.block_catalog.set()
        if self.release_catalog is not None:
            self.release_catalog.wait(timeout=5)
        if owner_ref != self.owner_ref or self.package is None:
            return SnapshotCatalogPage((), None)
        entry = _catalog_entry(owner_ref, self.package)
        self.asserted_cursor = cursor
        self.asserted_limit = limit
        return SnapshotCatalogPage((entry,), None)

    def load_snapshot_package(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> SnapshotPackage | None:
        self.load_calls += 1
        if self.disappear_after_catalog:
            return None
        if owner_ref != self.owner_ref or self.package is None:
            return None
        if self.package.bundle.snapshot.snapshot_id != snapshot_id:
            return None
        return self.package

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        self.save_calls += 1
        raise AssertionError("只读审阅 API 不得保存快照")


class QuestionnaireAssetReviewApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "snapshots"
        self.storage = FileResearchAssetStorage(self.root)
        screenshot_api = QuestionnaireMaterialSnapshotApi(
            self.storage,
            clock=lambda: FIXED_TIME,
        )
        summary = await screenshot_api.import_screenshots(
            OWNER_REF,
            (QuestionnaireMaterialScreenshot(
                "page.png",
                "image/png",
                _png(),
            ),),
        )
        self.snapshot_id = summary.snapshot_id
        self.api = QuestionnaireAssetReviewApi(self.storage)
        self.app = FastAPI()
        self.app.include_router(create_questionnaire_asset_reviews_router(self.api))

    async def _get(
        self,
        path: str,
        *,
        login: dict | None = LOGIN,
    ) -> httpx.Response:
        auth = AsyncMock(return_value=login)
        with patch.object(review_router_module, "_require_feature", new=auth):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                return await client.get(path)

    async def test_projection_and_thumbnail_are_safe_read_only_outputs(self) -> None:
        before = {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }

        projection = await self.api.get_projection(OWNER_REF, self.snapshot_id)
        self.assertEqual(projection.total_references, 1)
        self.assertEqual(projection.review_required_references, 1)
        self.assertIsInstance(projection.items, tuple)
        item = projection.items[0]
        self.assertIsInstance(item.warning_codes, tuple)
        self.assertEqual(item.binding_status, BindingStatus.NEEDS_REVIEW)
        self.assertTrue(item.review_required)
        self.assertEqual(item.preview_status, QuestionnaireAssetPreviewStatus.AVAILABLE)
        self.assertEqual(item.media_type, MediaType.IMAGE)

        thumbnail = await self.api.get_asset_thumbnail(
            OWNER_REF,
            self.snapshot_id,
            item.asset_token,
        )
        self.assertEqual(thumbnail.media_type, "image/png")
        self.assertTrue(thumbnail.content.startswith(b"\x89PNG\r\n\x1a\n"))
        with self.assertRaises(AttributeError):
            projection.items.clear()  # type: ignore[attr-defined]
        with self.assertRaises(AttributeError):
            item.warning_codes.append("later.code")  # type: ignore[attr-defined]

        payload = projection.model_dump(mode="json")
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "total_references",
                "review_required_references",
                "items",
            },
        )
        self.assertEqual(
            set(payload["items"][0]),
            {
                "reference_token",
                "asset_token",
                "context_type",
                "context_label",
                "role",
                "binding_status",
                "binding_confidence",
                "review_required",
                "media_type",
                "preview_status",
                "warning_codes",
            },
        )
        serialized = str(payload).lower()
        for forbidden in (
            OWNER_REF,
            "content_hash",
            "filename",
            "source_locator",
            "provider_resource_id",
            "original_url",
        ):
            self.assertNotIn(forbidden.lower(), serialized)

        after = {
            path.relative_to(self.root): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

    async def test_http_projection_and_png_headers_are_private_and_safe(self) -> None:
        projection_response = await self._get(
            f"/api/questionnaire-sources/snapshots/{self.snapshot_id}/asset-review"
        )
        self.assertEqual(projection_response.status_code, 200)
        for key, value in SAFE_HEADERS.items():
            self.assertEqual(projection_response.headers[key], value)
        token = projection_response.json()["items"][0]["asset_token"]

        thumbnail_response = await self._get(
            f"/api/questionnaire-sources/snapshots/{self.snapshot_id}"
            f"/asset-review/thumbnails/{token}.png"
        )
        self.assertEqual(thumbnail_response.status_code, 200)
        self.assertEqual(thumbnail_response.headers["content-type"], "image/png")
        self.assertEqual(
            int(thumbnail_response.headers["content-length"]),
            len(thumbnail_response.content),
        )
        self.assertNotIn("etag", thumbnail_response.headers)
        for key, value in SAFE_HEADERS.items():
            self.assertEqual(thumbnail_response.headers[key], value)

    async def test_pdf_reference_is_visible_but_never_previewed_as_an_image(self) -> None:
        pdf_api = QuestionnairePdfMaterialSnapshotApi(
            self.storage,
            clock=lambda: FIXED_TIME,
        )
        summary = await pdf_api.import_pdf(
            OWNER_REF,
            QuestionnairePdfMaterial("questionnaire.pdf", "application/pdf", _pdf()),
        )
        projection = await self.api.get_projection(OWNER_REF, summary.snapshot_id)
        self.assertEqual(projection.total_references, 1)
        item = projection.items[0]
        self.assertEqual(item.media_type, MediaType.DOCUMENT)
        self.assertEqual(item.preview_status, QuestionnaireAssetPreviewStatus.UNAVAILABLE)
        with self.assertRaises(QuestionnaireAssetReviewNotFoundError):
            await self.api.get_asset_thumbnail(
                OWNER_REF,
                summary.snapshot_id,
                item.asset_token,
            )

    async def test_missing_and_cross_owner_are_same_404_and_do_not_write(self) -> None:
        before = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        missing = await self._get(
            "/api/questionnaire-sources/snapshots/missing/asset-review"
        )
        other = await self._get(
            f"/api/questionnaire-sources/snapshots/{self.snapshot_id}/asset-review",
            login=OTHER_LOGIN,
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(other.status_code, 404)
        self.assertEqual(missing.json(), other.json())
        for response in (missing, other):
            for key, value in SAFE_HEADERS.items():
                self.assertEqual(response.headers[key], value)
        after = {
            path.relative_to(self.root): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after, before)

        fresh_root = Path(self.temporary.name) / "fresh"
        fresh_api = QuestionnaireAssetReviewApi(FileResearchAssetStorage(fresh_root))
        with self.assertRaises(QuestionnaireAssetReviewNotFoundError):
            await fresh_api.get_projection(OWNER_REF, "never-existed")
        self.assertFalse(fresh_root.exists())

    async def test_catalog_miss_never_loads_and_catalog_to_load_race_fails_closed(self) -> None:
        missing_storage = _CatalogStorage(OWNER_REF, None)
        missing_api = QuestionnaireAssetReviewApi(missing_storage)
        with self.assertRaises(QuestionnaireAssetReviewNotFoundError):
            await missing_api.get_projection(OWNER_REF, "missing")
        self.assertEqual(missing_storage.load_calls, 0)
        self.assertEqual(missing_storage.save_calls, 0)

        package = self.storage.load_snapshot_package(OWNER_REF, self.snapshot_id)
        self.assertIsNotNone(package)
        disappearing_storage = _CatalogStorage(
            OWNER_REF,
            package,
            disappear_after_catalog=True,
        )
        disappearing_api = QuestionnaireAssetReviewApi(disappearing_storage)
        with self.assertRaises(QuestionnaireAssetReviewInternalError):
            await disappearing_api.get_projection(OWNER_REF, self.snapshot_id)
        self.assertEqual(disappearing_storage.load_calls, 1)
        self.assertEqual(disappearing_storage.save_calls, 0)

    async def test_schema_rejects_coercion_and_keeps_count_invariants(self) -> None:
        projection = await self.api.get_projection(OWNER_REF, self.snapshot_id)
        payload = projection.model_dump()
        for invalid_version in (True, 1.0, "1"):
            invalid = {**payload, "schema_version": invalid_version}
            with self.assertRaises(ValidationError):
                QuestionnaireAssetReviewProjection.model_validate(invalid)
        with self.assertRaises(ValidationError):
            QuestionnaireAssetReviewProjection.model_validate({
                **payload,
                "review_required_references": 0,
            })

    async def test_authentication_and_all_router_errors_have_no_store_headers(self) -> None:
        denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="denied"))
        with patch.object(review_router_module, "_require_feature", new=denied):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
            ) as client:
                response = await client.get(
                    f"/api/questionnaire-sources/snapshots/{self.snapshot_id}/asset-review"
                )
        self.assertEqual(response.status_code, 403)
        for key, value in SAFE_HEADERS.items():
            self.assertEqual(response.headers[key], value)

        projection = await self.api.get_projection(OWNER_REF, self.snapshot_id)
        invalid = await self._get(
            f"/api/questionnaire-sources/snapshots/{self.snapshot_id}"
            "/asset-review/thumbnails/not-a-token.png"
        )
        unknown = await self._get(
            f"/api/questionnaire-sources/snapshots/{self.snapshot_id}"
            f"/asset-review/thumbnails/{'0' * 64}.png"
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(unknown.status_code, 404)
        self.assertNotEqual(projection.items[0].asset_token, "0" * 64)
        for error_response in (invalid, unknown):
            for key, value in SAFE_HEADERS.items():
                self.assertEqual(error_response.headers[key], value)

    async def test_admission_timeout_and_cancellation_hold_the_slot_until_done(self) -> None:
        async def exercise(cancel: bool) -> None:
            semaphore = asyncio.Semaphore(1)
            started = asyncio.Event()
            release = asyncio.Event()
            factory_calls = 0

            async def operation() -> str:
                started.set()
                await release.wait()
                return "done"

            task = asyncio.create_task(review_router_module._run_admitted(
                semaphore,
                operation,
                timeout_seconds=5.0 if cancel else 0.01,
            ))
            await started.wait()
            if cancel:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            else:
                with self.assertRaises(HTTPException) as raised:
                    await task
                self.assertEqual(raised.exception.status_code, 504)
            self.assertTrue(semaphore.locked())

            def second_factory():
                nonlocal factory_calls
                factory_calls += 1
                return operation()

            with self.assertRaises(HTTPException) as busy:
                await review_router_module._run_admitted(
                    semaphore,
                    second_factory,
                    timeout_seconds=1.0,
                )
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(factory_calls, 0)
            release.set()
            for _ in range(100):
                if not semaphore.locked():
                    break
                await asyncio.sleep(0.001)
            self.assertFalse(semaphore.locked())

        await exercise(cancel=False)
        await exercise(cancel=True)

    async def test_service_cancellation_waits_for_real_catalog_thread_completion(self) -> None:
        package = self.storage.load_snapshot_package(OWNER_REF, self.snapshot_id)
        self.assertIsNotNone(package)
        started = threading.Event()
        release = threading.Event()
        storage = _CatalogStorage(
            OWNER_REF,
            package,
            block_catalog=started,
            release_catalog=release,
        )
        api = QuestionnaireAssetReviewApi(storage)
        task = asyncio.create_task(api.get_projection(OWNER_REF, self.snapshot_id))
        self.assertTrue(await asyncio.to_thread(started.wait, 2))
        task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(storage.load_calls, 1)
        self.assertEqual(storage.save_calls, 0)

    def test_router_is_strictly_get_only(self) -> None:
        router = create_questionnaire_asset_reviews_router(self.api)
        routes = {
            (method, route.path)
            for route in router.routes
            for method in route.methods
        }
        self.assertEqual(routes, {
            (
                "GET",
                "/api/questionnaire-sources/snapshots/{snapshot_id}/asset-review",
            ),
            (
                "GET",
                "/api/questionnaire-sources/snapshots/{snapshot_id}"
                "/asset-review/thumbnails/{asset_token}.png",
            ),
        })


if __name__ == "__main__":
    unittest.main()
