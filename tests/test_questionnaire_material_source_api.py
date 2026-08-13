from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
import json
import struct
import tempfile
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import zlib

from fastapi import FastAPI, HTTPException, Request
import httpx

from app.routers import questionnaire_sources as questionnaire_sources_router
from app.routers.questionnaire_sources import (
    create_questionnaire_material_sources_router,
)
from app.schemas.questionnaire import MappingStatus, QuestionnaireSourceMode
from app.schemas.questionnaire_source_api import (
    QuestionnaireMaterialTrustLevel,
    QuestionnaireMaterialUploadSummary,
    SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE,
)
from app.schemas.research_assets import (
    AccessStatus,
    AssetContextType,
    AssetRole,
    BindingStatus,
    DocumentType,
    ExportPolicy,
    MediaType,
    ProcessingStatus,
    Provider,
    SensitivityStatus,
    SnapshotPolicy,
    SourceKind,
)
from app.services import (
    questionnaire_material_snapshot_api as material_api_module,
)
from app.services.questionnaire_material_snapshot_api import (
    MAX_MATERIAL_SCREENSHOT_PIXELS,
    QuestionnaireMaterialConflictError,
    QuestionnaireMaterialInternalError,
    QuestionnaireMaterialInvalidError,
    QuestionnaireMaterialScreenshot,
    QuestionnaireMaterialSnapshotApi,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    SnapshotPackageError,
)


LOGIN = {"email": "material-user@example.com", "name": "Material User"}
OWNER_REF = "email:material-user@example.com"
OTHER_OWNER_REF = "email:other-material-user@example.com"
FIXED_TIME = datetime(2026, 8, 13, 8, 0, tzinfo=timezone.utc)
MATERIAL_PATH = "/api/questionnaire-sources/materials/snapshots"
SUMMARY_FIELDS = {
    "schema_version",
    "snapshot_id",
    "provider",
    "source_mode",
    "mapping_status",
    "processing_status",
    "trust_level",
    "file_count",
    "total_size_bytes",
    "image_count",
    "requires_human_review",
    "warning_codes",
}


def _png(red: int = 17, green: int = 34, blue: int = 51) -> bytes:
    """Build a real, minimal 1x1 RGB PNG without a test-time image dependency."""

    def chunk(chunk_type: bytes, content: bytes) -> bytes:
        checksum = zlib.crc32(chunk_type + content) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(content))
            + chunk_type
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


def _png_with_dimensions(
    width: int,
    height: int,
    *,
    idat: bytes = b"not-a-decodable-pixel-stream",
) -> bytes:
    """Build a structurally valid PNG header for pre-decode limit tests."""

    def chunk(chunk_type: bytes, content: bytes) -> bytes:
        checksum = zlib.crc32(chunk_type + content) & 0xFFFFFFFF
        return (
            struct.pack(">I", len(content))
            + chunk_type
            + content
            + struct.pack(">I", checksum)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


PNG = _png()
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
    "DBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/"
    "2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAADAAIDASIAAhEBAxEB/8QA"
    "FQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QA"
    "FAEBAAAAAAAAAAAAAAAAAAAAAf/EABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAM"
    "AwEAAhEDEQA/AICAQ//Z"
)
WEBP = base64.b64decode(
    "UklGRjAAAABXRUJQVlA4ICQAAABwAQCdASoCAAMAAUAmJYwCdAFAAAD++xnL"
    "AkrVm6cszhXnwAA="
)


INVALID_DECODING_FIXTURES = (
    (
        "png",
        "broken.png",
        "image/png",
        _png_with_dimensions(1, 1),
    ),
    (
        "jpeg",
        "broken.jpg",
        "image/jpeg",
        (
            b"\xff\xd8"
            + b"\xff\xc0"
            + struct.pack(">H", 11)
            + bytes((8, 0, 1, 0, 1, 1, 1, 0x11, 0))
            + b"\xff\xda"
            + struct.pack(">H", 8)
            + bytes((1, 1, 0, 0, 63, 0))
            + b"garbage-scan"
            + b"\xff\xd9"
        ),
    ),
    (
        "webp",
        "broken.webp",
        "image/webp",
        (
            lambda payload: (
                b"RIFF"
                + struct.pack(
                    "<I",
                    4 + 8 + len(payload) + (len(payload) & 1),
                )
                + b"WEBP"
                + b"VP8 "
                + struct.pack("<I", len(payload))
                + payload
                + (b"\x00" if len(payload) & 1 else b"")
            )
        )(
            b"\x00\x00\x00\x9d\x01\x2a\x01\x00\x01\x00broken",
        ),
    ),
)


def _screenshots() -> tuple[QuestionnaireMaterialScreenshot, ...]:
    return (
        QuestionnaireMaterialScreenshot("page-001.png", "image/png", PNG),
        QuestionnaireMaterialScreenshot("PAGE-002.JPEG", "image/jpeg", JPEG),
        QuestionnaireMaterialScreenshot("page-003.webp", "image/webp", WEBP),
    )


def _safe_summary() -> QuestionnaireMaterialUploadSummary:
    return QuestionnaireMaterialUploadSummary(
        snapshot_id="qsn_material_test",
        file_count=1,
        total_size_bytes=len(PNG),
        image_count=1,
        warning_codes=[SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE],
    )


class _SequenceClock:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            value = FIXED_TIME + timedelta(seconds=self.calls)
            self.calls += 1
        return value


class _CorruptStorage:
    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        raise SnapshotPackageError("private /storage/material-corrupt")

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


def _multipart_body(
    files: list[tuple[str, str, bytes, str]],
    *,
    fields: list[tuple[str, str]] | None = None,
) -> tuple[bytes, bytes]:
    boundary = b"questionnaire-material-source-test"
    parts: list[bytes] = []
    for name, filename, content, mime_type in files:
        parts.extend((
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="'
            + name.encode("ascii")
            + b'"; filename="'
            + filename.encode("ascii")
            + b'"\r\n',
            b"Content-Type: " + mime_type.encode("ascii") + b"\r\n\r\n",
            content,
            b"\r\n",
        ))
    for name, value in fields or []:
        parts.extend((
            b"--" + boundary + b"\r\n",
            b'Content-Disposition: form-data; name="'
            + name.encode("ascii")
            + b'"\r\n\r\n',
            value.encode("utf-8"),
            b"\r\n",
        ))
    parts.append(b"--" + boundary + b"--\r\n")
    return b"".join(parts), b"multipart/form-data; boundary=" + boundary


async def _call_asgi(
    app: FastAPI,
    receive,
    content_type: bytes,
) -> tuple[int, bytes]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": MATERIAL_PATH,
            "raw_path": MATERIAL_PATH.encode("ascii"),
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


class QuestionnaireMaterialSourceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-material-source-api-test-",
        )
        self.storage = FileResearchAssetStorage(self.temporary.name)
        self.clock = _SequenceClock()
        self.api = QuestionnaireMaterialSnapshotApi(self.storage, self.clock)
        self.router = create_questionnaire_material_sources_router(self.api)
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
                response = await client.post(MATERIAL_PATH, **kwargs)
        require_feature.assert_awaited_once()
        self.assertEqual(require_feature.await_args.args[1], "survey")
        return response

    def _endpoint(self):
        return next(route.endpoint for route in self.router.routes)

    @staticmethod
    def _request_object(receive, content_type: bytes) -> Request:
        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": MATERIAL_PATH,
                "raw_path": MATERIAL_PATH.encode("ascii"),
                "query_string": b"",
                "headers": [(b"content-type", content_type)],
                "client": ("test", 1),
                "server": ("test", 80),
            },
            receive=receive,
        )

    async def test_service_persists_ordered_complete_low_trust_media(self):
        screenshots = _screenshots()
        summary = await self.api.import_screenshots(OWNER_REF, screenshots)
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            summary.snapshot_id,
        )

        self.assertIsNotNone(package)
        assert package is not None
        snapshot = package.bundle.snapshot
        collection = package.bundle.collection
        self.assertEqual(summary.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(
            summary.source_mode,
            QuestionnaireSourceMode.MATERIAL_UPLOAD,
        )
        self.assertEqual(summary.mapping_status, MappingStatus.NEEDS_REVIEW)
        self.assertEqual(summary.processing_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(summary.trust_level, QuestionnaireMaterialTrustLevel.LOW)
        self.assertEqual(summary.file_count, 3)
        self.assertEqual(summary.image_count, 3)
        self.assertEqual(
            summary.total_size_bytes,
            sum(len(screenshot.content) for screenshot in screenshots),
        )
        self.assertTrue(summary.requires_human_review)
        self.assertEqual(
            summary.warning_codes,
            [SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE],
        )

        self.assertEqual(snapshot.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(snapshot.source_mode, QuestionnaireSourceMode.MATERIAL_UPLOAD)
        self.assertEqual(snapshot.mapping_status, MappingStatus.NEEDS_REVIEW)
        self.assertEqual(snapshot.item_count, 0)
        self.assertEqual(snapshot.question_count, 0)
        self.assertEqual(snapshot.asset_count, 3)
        self.assertEqual(snapshot.provider_items, [])
        self.assertEqual(snapshot.canonical_questions, [])
        self.assertEqual(snapshot.response_column_mappings, [])
        self.assertEqual(collection.owner_ref, OWNER_REF)
        self.assertEqual(len(collection.sources), 1)
        self.assertEqual(len(collection.documents), 1)
        self.assertEqual(len(collection.assets), 3)
        self.assertEqual(len(collection.references), 3)

        source = collection.sources[0]
        document = collection.documents[0]
        self.assertEqual(source.source_kind, SourceKind.LOCAL_UPLOAD)
        self.assertEqual(source.provider, Provider.LOCAL_UPLOAD)
        self.assertEqual(source.owner_ref, OWNER_REF)
        self.assertEqual(source.acquisition_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(source.access_status, AccessStatus.ACCESSIBLE)
        self.assertEqual(document.document_type, DocumentType.DOCUMENT)
        self.assertEqual(document.snapshot_policy, SnapshotPolicy.FULL_COPY)
        self.assertEqual(document.parse_status, ProcessingStatus.NEEDS_REVIEW)
        self.assertEqual(document.size_bytes, summary.total_size_bytes)

        self.assertEqual(
            [asset.filename for asset in collection.assets],
            ["screenshot-001.png", "screenshot-002.jpg", "screenshot-003.webp"],
        )
        self.assertEqual(
            [asset.mime_type for asset in collection.assets],
            ["image/png", "image/jpeg", "image/webp"],
        )
        self.assertEqual(
            [reference.asset_id for reference in collection.references],
            [asset.asset_id for asset in collection.assets],
        )
        self.assertEqual(
            set(package.media),
            {asset.content_hash for asset in collection.assets},
        )
        for index, (asset, reference, screenshot) in enumerate(zip(
            collection.assets,
            collection.references,
            screenshots,
            strict=True,
        )):
            self.assertEqual(asset.media_type, MediaType.IMAGE)
            self.assertEqual(asset.provider, Provider.LOCAL_UPLOAD)
            self.assertEqual(asset.access_status, AccessStatus.ACCESSIBLE)
            self.assertEqual(asset.processing_status, ProcessingStatus.NEEDS_REVIEW)
            self.assertEqual(asset.sensitivity_status, SensitivityStatus.UNKNOWN)
            self.assertEqual(asset.export_policy, ExportPolicy.MANUAL_CONFIRMATION)
            self.assertEqual(asset.size_bytes, len(screenshot.content))
            self.assertEqual(package.media[asset.content_hash], screenshot.content)
            self.assertEqual(asset.source_locator.local_file_id, asset.asset_id)
            self.assertIsNone(asset.source_locator.question_position)
            self.assertEqual(
                asset.source_locator.__pydantic_extra__["material_position"],
                index,
            )
            self.assertEqual(reference.context_type, AssetContextType.RESEARCH_DOCUMENT)
            self.assertEqual(reference.context_id, document.document_id)
            self.assertEqual(reference.role, AssetRole.RESEARCHER_MATERIAL)
            self.assertEqual(reference.binding_status, BindingStatus.NEEDS_REVIEW)
            self.assertEqual(reference.binding_confidence, 0.0)
            self.assertIsNone(reference.source_locator.question_position)
            self.assertEqual(
                reference.source_locator.__pydantic_extra__["material_position"],
                index,
            )

        warned_objects = [source, document, snapshot]
        warned_objects.extend(collection.assets)
        warned_objects.extend(collection.references)
        for value in warned_objects:
            self.assertEqual(
                [warning.code for warning in value.warnings],
                [SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE],
            )
            self.assertTrue(value.warnings[0].blocking)

    async def test_http_accepts_one_and_twenty_files_and_preserves_order(self):
        one = await self._request(files=[
            ("files", ("only.png", PNG, "image/png")),
        ])
        self.assertEqual(one.status_code, 200)
        self.assertEqual(one.json()["file_count"], 1)

        contents = [
            _png(index, (index * 7) % 256, (index * 13) % 256)
            for index in range(20)
        ]
        twenty = await self._request(files=[
            ("files", (f"page-{index + 1}.png", content, "image/png"))
            for index, content in enumerate(contents)
        ])
        self.assertEqual(twenty.status_code, 200)
        self.assertEqual(twenty.json()["file_count"], 20)
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            twenty.json()["snapshot_id"],
        )
        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(
            [asset.filename for asset in package.bundle.collection.assets],
            [f"screenshot-{index + 1:03d}.png" for index in range(20)],
        )
        self.assertEqual(
            [
                package.media[asset.content_hash]
                for asset in package.bundle.collection.assets
            ],
            contents,
        )
        self.assertEqual(
            [
                reference.source_locator.__pydantic_extra__["material_position"]
                for reference in package.bundle.collection.references
            ],
            list(range(20)),
        )

    async def test_repeated_upload_is_idempotent_across_clock_changes(self):
        with patch.object(
            self.storage,
            "save_snapshot_package",
            wraps=self.storage.save_snapshot_package,
        ) as save:
            first = await self.api.import_screenshots(OWNER_REF, _screenshots())
            second = await self.api.import_screenshots(OWNER_REF, _screenshots())

        self.assertEqual(first, second)
        self.assertEqual(save.call_count, 1)
        self.assertEqual(self.clock.calls, 2)

    async def test_concurrent_repeated_upload_is_idempotent(self):
        first, second = await asyncio.gather(
            self.api.import_screenshots(OWNER_REF, _screenshots()),
            self.api.import_screenshots(OWNER_REF, _screenshots()),
        )

        self.assertEqual(first, second)
        self.assertIsNotNone(self.storage.load_snapshot_package(
            OWNER_REF,
            first.snapshot_id,
        ))

    async def test_owner_scope_changes_identity_and_prevents_cross_owner_load(self):
        first = await self.api.import_screenshots(OWNER_REF, _screenshots())
        second = await self.api.import_screenshots(OTHER_OWNER_REF, _screenshots())

        self.assertNotEqual(first.snapshot_id, second.snapshot_id)
        self.assertIsNone(self.storage.load_snapshot_package(
            OWNER_REF,
            second.snapshot_id,
        ))
        self.assertIsNone(self.storage.load_snapshot_package(
            OTHER_OWNER_REF,
            first.snapshot_id,
        ))

    async def test_service_rejects_invalid_name_mime_extension_and_container(self):
        cases = (
            ("path", QuestionnaireMaterialScreenshot(
                "../page.png", "image/png", PNG,
            )),
            ("mime", QuestionnaireMaterialScreenshot(
                "page.png", "application/octet-stream", PNG,
            )),
            ("extension", QuestionnaireMaterialScreenshot(
                "page.jpg", "image/png", PNG,
            )),
            ("empty", QuestionnaireMaterialScreenshot(
                "page.png", "image/png", b"",
            )),
            ("signature", QuestionnaireMaterialScreenshot(
                "page.png", "image/png", b"not-a-png",
            )),
            ("truncated", QuestionnaireMaterialScreenshot(
                "page.png", "image/png", PNG[:-1],
            )),
        )
        for label, screenshot in cases:
            with self.subTest(label=label):
                with self.assertRaises(QuestionnaireMaterialInvalidError):
                    await self.api.import_screenshots(OWNER_REF, (screenshot,))

        with self.assertRaises(QuestionnaireMaterialInvalidError):
            await self.api.import_screenshots(OWNER_REF, ())
        with self.assertRaises(QuestionnaireMaterialInvalidError):
            await self.api.import_screenshots(
                OWNER_REF,
                tuple(
                    QuestionnaireMaterialScreenshot(
                        f"page-{index}.png",
                        "image/png",
                        PNG,
                    )
                    for index in range(21)
                ),
            )

    async def test_undecodable_structural_images_are_rejected_without_persistence(self):
        for label, filename, mime_type, content in INVALID_DECODING_FIXTURES:
            with self.subTest(label=label):
                with (
                    patch.object(
                        self.storage,
                        "save_snapshot_package",
                        wraps=self.storage.save_snapshot_package,
                    ) as save,
                    self.assertRaises(QuestionnaireMaterialInvalidError),
                ):
                    await self.api.import_screenshots(
                        OWNER_REF,
                        (QuestionnaireMaterialScreenshot(
                            filename,
                            mime_type,
                            content,
                        ),),
                    )
                save.assert_not_called()

    async def test_undecodable_structural_images_return_422_without_persistence(self):
        for label, filename, mime_type, content in INVALID_DECODING_FIXTURES:
            with self.subTest(label=label), patch.object(
                self.storage,
                "save_snapshot_package",
                wraps=self.storage.save_snapshot_package,
            ) as save:
                response = await self._request(files=[
                    ("files", (filename, content, mime_type)),
                ])
                self.assertEqual(response.status_code, 422)
                self.assertEqual(
                    response.json(),
                    {"detail": "问卷截图材料无效或不受支持"},
                )
                save.assert_not_called()

    async def test_pixel_limit_is_rejected_before_image_decoder_opens(self):
        too_wide = _png_with_dimensions(
            MAX_MATERIAL_SCREENSHOT_PIXELS + 1,
            1,
        )
        with (
            patch.object(
                material_api_module.Image,
                "open",
                side_effect=AssertionError("oversized image reached decoder"),
            ) as image_open,
            self.assertRaises(QuestionnaireMaterialInvalidError),
        ):
            await self.api.import_screenshots(
                OWNER_REF,
                (QuestionnaireMaterialScreenshot(
                    "oversized.png",
                    "image/png",
                    too_wide,
                ),),
            )
        image_open.assert_not_called()

    async def test_long_screenshot_at_pixel_limit_is_accepted(self):
        exact_limit_header = _png_with_dimensions(
            1,
            MAX_MATERIAL_SCREENSHOT_PIXELS,
        )
        image = MagicMock()
        image.__enter__.return_value = image
        image.__exit__.return_value = False
        image.format = "PNG"
        image.size = (1, MAX_MATERIAL_SCREENSHOT_PIXELS)
        image.n_frames = 1
        with patch.object(
            material_api_module.Image,
            "open",
            side_effect=(image, image),
        ) as image_open:
            summary = await self.api.import_screenshots(
                OWNER_REF,
                (QuestionnaireMaterialScreenshot(
                    "long.png",
                    "image/png",
                    exact_limit_header,
                ),),
            )

        self.assertEqual(summary.image_count, 1)
        self.assertEqual(image_open.call_count, 2)
        image.verify.assert_called_once_with()
        image.load.assert_called_once_with()

    async def test_decoder_format_must_match_declared_mime(self):
        image = MagicMock()
        image.__enter__.return_value = image
        image.__exit__.return_value = False
        image.format = "JPEG"
        image.size = (1, 1)
        image.n_frames = 1
        with (
            patch.object(
                material_api_module.Image,
                "open",
                return_value=image,
            ),
            self.assertRaises(QuestionnaireMaterialInvalidError),
        ):
            await self.api.import_screenshots(
                OWNER_REF,
                (QuestionnaireMaterialScreenshot(
                    "declared.png",
                    "image/png",
                    PNG,
                ),),
            )

    async def test_animated_webp_is_rejected(self):
        image = MagicMock()
        image.__enter__.return_value = image
        image.__exit__.return_value = False
        image.format = "WEBP"
        image.size = (2, 3)
        image.n_frames = 2
        with (
            patch.object(
                material_api_module.Image,
                "open",
                return_value=image,
            ),
            self.assertRaises(QuestionnaireMaterialInvalidError),
        ):
            await self.api.import_screenshots(
                OWNER_REF,
                (QuestionnaireMaterialScreenshot(
                    "animated.webp",
                    "image/webp",
                    WEBP,
                ),),
            )
        image.verify.assert_not_called()
        image.load.assert_not_called()

    async def test_service_enforces_per_file_and_total_byte_limits(self):
        with (
            patch.object(
                material_api_module,
                "MAX_MATERIAL_SCREENSHOT_BYTES",
                len(PNG) - 1,
            ),
            self.assertRaises(QuestionnaireMaterialInvalidError),
        ):
            await self.api.import_screenshots(
                OWNER_REF,
                (QuestionnaireMaterialScreenshot(
                    "page.png", "image/png", PNG,
                ),),
            )

        second = _png(91, 92, 93)
        with (
            patch.object(
                material_api_module,
                "MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES",
                len(PNG) + len(second) - 1,
            ),
            self.assertRaises(QuestionnaireMaterialInvalidError),
        ):
            await self.api.import_screenshots(
                OWNER_REF,
                (
                    QuestionnaireMaterialScreenshot(
                        "page-1.png", "image/png", PNG,
                    ),
                    QuestionnaireMaterialScreenshot(
                        "page-2.png", "image/png", second,
                    ),
                ),
            )

    async def test_corrupt_storage_becomes_redacted_internal_error(self):
        api = QuestionnaireMaterialSnapshotApi(
            _CorruptStorage(),
            lambda: FIXED_TIME,
        )
        with self.assertRaises(QuestionnaireMaterialInternalError) as caught:
            await api.import_screenshots(OWNER_REF, _screenshots())

        self.assertNotIn("/storage/material-corrupt", str(caught.exception))

    async def test_service_work_is_offloaded_and_cancellation_waits_for_thread(self):
        started = threading.Event()
        finish = threading.Event()
        worker_threads: list[int] = []
        event_loop_thread = threading.get_ident()

        def slow_import(owner_ref, screenshots, clock, storage):
            worker_threads.append(threading.get_ident())
            started.set()
            if not finish.wait(timeout=2):
                raise AssertionError("test did not release worker")
            return _safe_summary()

        with patch.object(
            material_api_module,
            "_import_screenshots",
            new=slow_import,
        ):
            task = asyncio.create_task(self.api.import_screenshots(
                OWNER_REF,
                _screenshots(),
            ))
            await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1)
            task.cancel()
            before = time.monotonic()
            await asyncio.sleep(0.03)
            self.assertLess(time.monotonic() - before, 0.15)
            self.assertFalse(task.done())
            finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)

        self.assertEqual(len(worker_threads), 1)
        self.assertNotEqual(worker_threads[0], event_loop_thread)

    async def test_http_success_returns_only_safe_low_trust_summary(self):
        response = await self._request(files=[
            ("files", ("secret-page.png", PNG, "image/png")),
            ("files", ("secret-page.jpeg", JPEG, "image/jpeg")),
            ("files", ("secret-page.webp", WEBP, "image/webp")),
        ])

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), SUMMARY_FIELDS)
        self.assertEqual(payload["provider"], "local_upload")
        self.assertEqual(payload["source_mode"], "material_upload")
        self.assertEqual(payload["mapping_status"], "needs_review")
        self.assertEqual(payload["processing_status"], "needs_review")
        self.assertEqual(payload["trust_level"], "low")
        self.assertTrue(payload["requires_human_review"])
        serialized = json.dumps(payload, ensure_ascii=False)
        package = self.storage.load_snapshot_package(
            OWNER_REF,
            payload["snapshot_id"],
        )
        self.assertIsNotNone(package)
        assert package is not None
        forbidden = [
            OWNER_REF,
            "secret-page",
            self.temporary.name,
            *(asset.content_hash for asset in package.bundle.collection.assets),
        ]
        for value in forbidden:
            self.assertNotIn(value.casefold(), serialized.casefold())

    async def test_http_authentication_runs_before_body_consumption(self):
        body, content_type = _multipart_body([
            ("files", "page.png", PNG, "image/png"),
        ])
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

    async def test_http_rejects_mime_extension_signature_and_empty_file(self):
        cases = (
            (
                "mime",
                [("files", ("page.png", PNG, "application/octet-stream"))],
                415,
            ),
            (
                "extension",
                [("files", ("page.jpg", PNG, "image/png"))],
                422,
            ),
            (
                "signature",
                [("files", ("page.png", b"not-a-png", "image/png"))],
                422,
            ),
            (
                "empty",
                [("files", ("page.png", b"", "image/png"))],
                422,
            ),
        )
        for label, files, expected_status in cases:
            with self.subTest(label=label):
                response = await self._request(files=files)
                self.assertEqual(response.status_code, expected_status)

    async def test_http_rejects_zero_unknown_text_and_twenty_one_files(self):
        boundary = "empty-material-source-test"
        empty = await self._request(
            content=f"--{boundary}--\r\n".encode("ascii"),
            headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        )
        unknown_file = await self._request(files=[
            ("unexpected", ("page.png", PNG, "image/png")),
        ])
        unknown_text = await self._request(
            files=[("files", ("page.png", PNG, "image/png"))],
            data={"caption": "private caption"},
        )
        twenty_one = await self._request(files=[
            ("files", (f"page-{index}.png", PNG, "image/png"))
            for index in range(21)
        ])

        self.assertEqual(empty.status_code, 422)
        self.assertEqual(unknown_file.status_code, 422)
        self.assertEqual(unknown_text.status_code, 422)
        self.assertEqual(twenty_one.status_code, 422)

    async def test_http_enforces_per_file_and_total_byte_limits(self):
        with patch.object(
            questionnaire_sources_router,
            "MAX_MATERIAL_SCREENSHOT_BYTES",
            len(PNG) - 1,
        ):
            per_file = await self._request(files=[
                ("files", ("page.png", PNG, "image/png")),
            ])

        second = _png(41, 42, 43)
        with patch.object(
            questionnaire_sources_router,
            "MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES",
            len(PNG) + len(second) - 1,
        ):
            total = await self._request(files=[
                ("files", ("page-1.png", PNG, "image/png")),
                ("files", ("page-2.png", second, "image/png")),
            ])

        self.assertEqual(per_file.status_code, 413)
        self.assertEqual(total.status_code, 413)
        self.assertEqual(
            per_file.json(),
            {"detail": "问卷截图材料超过上传大小限制"},
        )
        self.assertEqual(
            total.json(),
            {"detail": "问卷截图材料超过上传大小限制"},
        )

    async def test_slow_upload_times_out_and_releases_upload_gate(self):
        body, content_type = _multipart_body([
            ("files", "page.png", PNG, "image/png"),
        ])
        receive = _StallingReceive(body[:64])
        with (
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                questionnaire_sources_router,
                "_MATERIAL_UPLOAD_TIMEOUT_SECONDS",
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
            {"detail": "问卷截图材料上传超时，请重试"},
        )
        retry = await self._request(files=[
            ("files", ("page.png", PNG, "image/png")),
        ])
        self.assertEqual(retry.status_code, 200)

    async def test_disconnected_upload_is_safe_and_releases_upload_gate(self):
        body, content_type = _multipart_body([
            ("files", "page.png", PNG, "image/png"),
        ])
        receive = _DisconnectingReceive(body[:64])
        with patch(
            "app.routers.questionnaire_sources._require_feature",
            new=AsyncMock(return_value=LOGIN),
        ):
            status, payload = await _call_asgi(
                self.app,
                receive,
                content_type,
            )

        self.assertEqual(status, 400)
        self.assertEqual(
            json.loads(payload),
            {"detail": "问卷截图材料上传未完整发送"},
        )
        retry = await self._request(files=[
            ("files", ("page.png", PNG, "image/png")),
        ])
        self.assertEqual(retry.status_code, 200)

    async def test_busy_import_precedes_body_and_cancelled_request_defers_release(self):
        endpoint = self._endpoint()
        expected = _safe_summary()
        first_started = asyncio.Event()
        first_finished = asyncio.Event()
        finish_first = asyncio.Event()
        calls: list[str] = []

        async def fake_import(api, owner_ref, screenshots):
            calls.append(screenshots[0].filename)
            if len(calls) == 1:
                first_started.set()
                await finish_first.wait()
                first_finished.set()
            return expected

        first_body, content_type = _multipart_body([
            ("files", "first.png", PNG, "image/png"),
        ])
        second_body, _ = _multipart_body([
            ("files", "second.png", PNG, "image/png"),
        ])
        first_request = self._request_object(
            _ChunkedReceive([first_body]),
            content_type,
        )
        second_receive = _ChunkedReceive([second_body])
        second_request = self._request_object(second_receive, content_type)

        with (
            patch.object(
                QuestionnaireMaterialSnapshotApi,
                "import_screenshots",
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
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)
            self.assertEqual(calls, ["first.png"])

            finish_first.set()
            await asyncio.wait_for(first_finished.wait(), timeout=1)
            await asyncio.sleep(0)
            retry = await endpoint(self._request_object(
                _ChunkedReceive([second_body]),
                content_type,
            ))

        self.assertEqual(calls, ["first.png", "second.png"])
        self.assertEqual(retry, expected)

    async def test_import_timeout_holds_gate_until_cancelled_task_finishes(self):
        endpoint = self._endpoint()
        expected = _safe_summary()
        cancelled = asyncio.Event()
        finish = asyncio.Event()
        finished = asyncio.Event()
        calls = 0

        async def fake_import(api, owner_ref, screenshots):
            nonlocal calls
            calls += 1
            if calls == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await finish.wait()
                    finished.set()
            return expected

        first_body, content_type = _multipart_body([
            ("files", "first.png", PNG, "image/png"),
        ])
        second_body, _ = _multipart_body([
            ("files", "second.png", PNG, "image/png"),
        ])
        second_receive = _ChunkedReceive([second_body])
        with (
            patch.object(
                QuestionnaireMaterialSnapshotApi,
                "import_screenshots",
                new=fake_import,
            ),
            patch(
                "app.routers.questionnaire_sources._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch.object(
                questionnaire_sources_router,
                "_MATERIAL_IMPORT_TIMEOUT_SECONDS",
                0.03,
            ),
        ):
            with self.assertRaises(HTTPException) as timed_out:
                await endpoint(self._request_object(
                    _ChunkedReceive([first_body]),
                    content_type,
                ))
            self.assertEqual(timed_out.exception.status_code, 504)
            await asyncio.wait_for(cancelled.wait(), timeout=1)

            with self.assertRaises(HTTPException) as busy:
                await endpoint(self._request_object(
                    second_receive,
                    content_type,
                ))
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)

            finish.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await asyncio.sleep(0)
            retry = await endpoint(self._request_object(
                _ChunkedReceive([second_body]),
                content_type,
            ))

        self.assertEqual(calls, 2)
        self.assertEqual(retry, expected)

    async def test_http_errors_are_stable_and_redacted(self):
        cases = (
            (
                QuestionnaireMaterialInvalidError("private invalid filename"),
                422,
                "问卷截图材料无效或不受支持",
            ),
            (
                QuestionnaireMaterialConflictError("private conflict hash"),
                409,
                "同一截图材料快照 ID 已存在不同内容",
            ),
            (
                QuestionnaireMaterialInternalError("private /storage/path"),
                500,
                "问卷截图材料导入暂时不可用",
            ),
            (
                RuntimeError("private token=material-secret"),
                500,
                "问卷截图材料导入暂时不可用",
            ),
        )
        for error, expected_status, expected_detail in cases:
            with self.subTest(error=type(error).__name__):
                async def fail(*args, current_error=error, **kwargs):
                    raise current_error

                with patch.object(
                    QuestionnaireMaterialSnapshotApi,
                    "import_screenshots",
                    new=fail,
                ):
                    response = await self._request(files=[
                        ("files", ("page.png", PNG, "image/png")),
                    ])
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(response.json(), {"detail": expected_detail})
                self.assertNotIn(str(error), response.text)


if __name__ == "__main__":
    unittest.main()
