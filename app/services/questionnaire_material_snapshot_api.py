"""问卷连续截图到低可信素材快照的异步业务门面。"""

from __future__ import annotations

import asyncio
import io
import struct
import unicodedata
import warnings
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

from PIL import Image, UnidentifiedImageError

from app.core.research_assets import content_sha256, structured_sha256
from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_source_api import (
    QuestionnaireMaterialTrustLevel,
    QuestionnaireMaterialUploadSummary,
    SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceResult,
    questionnaire_source_priority,
)
from app.schemas.research_assets import (
    AccessStatus,
    AssetContextType,
    AssetReference,
    AssetRole,
    BindingStatus,
    DocumentType,
    ExportPolicy,
    ImportWarning,
    MediaType,
    ProcessingStatus,
    Provider,
    ResearchAsset,
    ResearchAssetCollection,
    ResearchDocument,
    ResearchSource,
    SensitivityStatus,
    SnapshotPolicy,
    SourceKind,
    SourceLocator,
)
from app.services.questionnaire_snapshot_api import (
    _validate_package_without_archive,
)
from app.services.questionnaire_source_service import (
    load_questionnaire_source_snapshot,
    save_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
)


SUPPORTED_MATERIAL_SCREENSHOT_MIME_TYPES = frozenset({
    "image/jpeg",
    "image/png",
    "image/webp",
})
MAX_MATERIAL_SCREENSHOT_FILES = 20
MAX_MATERIAL_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES = 50 * 1024 * 1024
MAX_MATERIAL_SCREENSHOT_PIXELS = 40_000_000

_MIME_EXTENSIONS = {
    "image/jpeg": frozenset({".jpeg", ".jpg"}),
    "image/png": frozenset({".png"}),
    "image/webp": frozenset({".webp"}),
}
_CANONICAL_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_PIL_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
}
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF_MARKERS = frozenset({
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
})


class QuestionnaireMaterialSnapshotApiError(RuntimeError):
    """可由 HTTP 层安全分类的截图素材导入错误基类。"""


class QuestionnaireMaterialInvalidError(
    QuestionnaireMaterialSnapshotApiError
):
    """截图数量、声明类型、扩展名、大小或容器结构无效。"""


class QuestionnaireMaterialConflictError(
    QuestionnaireMaterialSnapshotApiError
):
    """同一不可变快照身份已经对应不同内容。"""


class QuestionnaireMaterialInternalError(
    QuestionnaireMaterialSnapshotApiError
):
    """不得向 HTTP 响应暴露细节的建模或持久化失败。"""


class _ScreenshotValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class QuestionnaireMaterialScreenshot:
    """一张按调用顺序提交的问卷截图。"""

    filename: str
    mime_type: str
    content: bytes


@dataclass(frozen=True, slots=True)
class _ValidatedScreenshot:
    mime_type: str
    content: bytes
    content_hash: str
    canonical_extension: str


@dataclass(frozen=True, slots=True)
class _MaterialSnapshotMapping:
    result: QuestionnaireSourceResult
    media: dict[str, bytes]

    @property
    def package(self) -> SnapshotPackage:
        return SnapshotPackage(
            ResearchAssetBundle(
                self.result.snapshot,
                self.result.collection,
            ),
            dict(self.media),
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_id(prefix: str, *parts: object) -> str:
    return f"{prefix}_{structured_sha256(list(parts))[:24]}"


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _retrieved_at(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise QuestionnaireMaterialInternalError() from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise QuestionnaireMaterialInternalError()
    try:
        if value.utcoffset() is None:
            raise QuestionnaireMaterialInternalError()
    except (OverflowError, ValueError) as error:
        raise QuestionnaireMaterialInternalError() from error
    return value


def _normalized_filename(filename: object) -> str:
    if not isinstance(filename, str) or not filename:
        raise _ScreenshotValidationError()
    if filename != filename.strip():
        raise _ScreenshotValidationError()
    try:
        encoded = filename.encode("utf-8")
    except UnicodeEncodeError as error:
        raise _ScreenshotValidationError() from error
    if len(encoded) > 255:
        raise _ScreenshotValidationError()
    if filename in {".", ".."} or "/" in filename or "\\" in filename:
        raise _ScreenshotValidationError()
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in filename
    ):
        raise _ScreenshotValidationError()
    return filename


def _normalized_mime_type(mime_type: object) -> str:
    if not isinstance(mime_type, str):
        raise _ScreenshotValidationError()
    normalized = mime_type.strip().casefold()
    if normalized not in SUPPORTED_MATERIAL_SCREENSHOT_MIME_TYPES:
        raise _ScreenshotValidationError()
    return normalized


def _validate_pixel_dimensions(width: object, height: object) -> None:
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
        or width * height > MAX_MATERIAL_SCREENSHOT_PIXELS
    ):
        raise _ScreenshotValidationError()


def _validate_png(content: bytes) -> None:
    if not content.startswith(_PNG_SIGNATURE):
        raise _ScreenshotValidationError()
    offset = len(_PNG_SIGNATURE)
    chunk_index = 0
    saw_idat = False
    ended_idat = False
    saw_iend = False
    while offset < len(content):
        if offset + 12 > len(content):
            raise _ScreenshotValidationError()
        chunk_length = struct.unpack(">I", content[offset:offset + 4])[0]
        chunk_type = content[offset + 4:offset + 8]
        chunk_end = offset + 12 + chunk_length
        if chunk_end > len(content):
            raise _ScreenshotValidationError()
        if (
            len(chunk_type) != 4
            or not all(
                65 <= character <= 90 or 97 <= character <= 122
                for character in chunk_type
            )
            or not 65 <= chunk_type[2] <= 90
        ):
            raise _ScreenshotValidationError()
        chunk_data = content[offset + 8:offset + 8 + chunk_length]
        expected_crc = struct.unpack(">I", content[chunk_end - 4:chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type + chunk_data) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            raise _ScreenshotValidationError()

        if chunk_index == 0:
            if chunk_type != b"IHDR" or chunk_length != 13:
                raise _ScreenshotValidationError()
            width, height = struct.unpack(">II", chunk_data[:8])
            _validate_pixel_dimensions(width, height)
            bit_depth, color_type, compression, filtering, interlace = chunk_data[8:]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            if (
                bit_depth not in valid_depths.get(color_type, set())
                or compression != 0
                or filtering != 0
                or interlace not in {0, 1}
            ):
                raise _ScreenshotValidationError()
        elif chunk_type == b"IHDR":
            raise _ScreenshotValidationError()

        if chunk_type == b"IDAT":
            if ended_idat:
                raise _ScreenshotValidationError()
            saw_idat = True
        elif saw_idat and chunk_type != b"IEND":
            ended_idat = True
        if chunk_type == b"IEND":
            if chunk_length != 0 or not saw_idat or chunk_end != len(content):
                raise _ScreenshotValidationError()
            saw_iend = True
            offset = chunk_end
            break
        offset = chunk_end
        chunk_index += 1
    if not saw_iend or offset != len(content):
        raise _ScreenshotValidationError()


def _validate_jpeg_sof(segment: bytes) -> None:
    if len(segment) < 6:
        raise _ScreenshotValidationError()
    height = struct.unpack(">H", segment[1:3])[0]
    width = struct.unpack(">H", segment[3:5])[0]
    _validate_pixel_dimensions(width, height)
    component_count = segment[5]
    if (
        component_count == 0
        or len(segment) != 6 + 3 * component_count
    ):
        raise _ScreenshotValidationError()


def _validate_jpeg_sos(segment: bytes) -> None:
    if not segment:
        raise _ScreenshotValidationError()
    component_count = segment[0]
    if component_count == 0 or len(segment) != 4 + 2 * component_count:
        raise _ScreenshotValidationError()


def _validate_jpeg(content: bytes) -> None:
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        raise _ScreenshotValidationError()
    offset = 2
    pending_marker: int | None = None
    in_scan = False
    saw_sof = False
    saw_sos = False
    while offset < len(content):
        if in_scan:
            while offset < len(content):
                if content[offset] != 0xFF:
                    offset += 1
                    continue
                offset += 1
                while offset < len(content) and content[offset] == 0xFF:
                    offset += 1
                if offset >= len(content):
                    raise _ScreenshotValidationError()
                marker = content[offset]
                offset += 1
                if marker == 0x00 or 0xD0 <= marker <= 0xD7:
                    continue
                pending_marker = marker
                in_scan = False
                break
            if in_scan:
                raise _ScreenshotValidationError()

        if pending_marker is not None:
            marker = pending_marker
            pending_marker = None
        else:
            if content[offset] != 0xFF:
                raise _ScreenshotValidationError()
            while offset < len(content) and content[offset] == 0xFF:
                offset += 1
            if offset >= len(content):
                raise _ScreenshotValidationError()
            marker = content[offset]
            offset += 1

        if marker == 0xD9:
            if offset != len(content) or not saw_sof or not saw_sos:
                raise _ScreenshotValidationError()
            return
        if marker in {0x00, 0xD8} or 0xD0 <= marker <= 0xD7:
            raise _ScreenshotValidationError()
        if marker == 0x01:
            continue
        if offset + 2 > len(content):
            raise _ScreenshotValidationError()
        segment_length = struct.unpack(">H", content[offset:offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(content):
            raise _ScreenshotValidationError()
        segment = content[offset + 2:offset + segment_length]
        offset += segment_length
        if marker in _JPEG_SOF_MARKERS:
            _validate_jpeg_sof(segment)
            saw_sof = True
        elif marker == 0xDA:
            _validate_jpeg_sos(segment)
            saw_sos = True
            in_scan = True
        elif marker == 0xDC:
            if not saw_sos:
                raise _ScreenshotValidationError()
            in_scan = True
    raise _ScreenshotValidationError()


def _validate_webp_image_chunk(chunk_type: bytes, chunk_data: bytes) -> None:
    if chunk_type == b"VP8 ":
        if len(chunk_data) < 10 or chunk_data[3:6] != b"\x9d\x01\x2a":
            raise _ScreenshotValidationError()
        width = struct.unpack("<H", chunk_data[6:8])[0] & 0x3FFF
        height = struct.unpack("<H", chunk_data[8:10])[0] & 0x3FFF
        _validate_pixel_dimensions(width, height)
    elif chunk_type == b"VP8L":
        if len(chunk_data) < 5 or chunk_data[0] != 0x2F:
            raise _ScreenshotValidationError()
        dimensions = int.from_bytes(chunk_data[1:5], "little")
        if dimensions >> 29:
            raise _ScreenshotValidationError()
        width = (dimensions & 0x3FFF) + 1
        height = ((dimensions >> 14) & 0x3FFF) + 1
        _validate_pixel_dimensions(width, height)


def _validate_webp(content: bytes) -> None:
    if (
        len(content) < 20
        or content[:4] != b"RIFF"
        or content[8:12] != b"WEBP"
        or struct.unpack("<I", content[4:8])[0] + 8 != len(content)
    ):
        raise _ScreenshotValidationError()
    offset = 12
    first_chunk: bytes | None = None
    saw_vp8x = False
    saw_image = False
    while offset < len(content):
        if offset + 8 > len(content):
            raise _ScreenshotValidationError()
        chunk_type = content[offset:offset + 4]
        chunk_length = struct.unpack("<I", content[offset + 4:offset + 8])[0]
        data_start = offset + 8
        data_end = data_start + chunk_length
        padded_end = data_end + (chunk_length & 1)
        if padded_end > len(content):
            raise _ScreenshotValidationError()
        if chunk_length & 1 and content[data_end:padded_end] != b"\x00":
            raise _ScreenshotValidationError()
        chunk_data = content[data_start:data_end]
        if first_chunk is None:
            first_chunk = chunk_type
            if first_chunk not in {b"VP8 ", b"VP8L", b"VP8X"}:
                raise _ScreenshotValidationError()
        if chunk_type == b"VP8X":
            if saw_vp8x or first_chunk != b"VP8X" or chunk_length != 10:
                raise _ScreenshotValidationError()
            flags = chunk_data[0]
            if flags & 0xC3 or chunk_data[1:4] != b"\x00\x00\x00":
                raise _ScreenshotValidationError()
            width = int.from_bytes(chunk_data[4:7], "little") + 1
            height = int.from_bytes(chunk_data[7:10], "little") + 1
            _validate_pixel_dimensions(width, height)
            saw_vp8x = True
        elif chunk_type in {b"VP8 ", b"VP8L"}:
            if saw_image:
                raise _ScreenshotValidationError()
            _validate_webp_image_chunk(chunk_type, chunk_data)
            saw_image = True
        offset = padded_end
    if offset != len(content) or first_chunk is None or not saw_image:
        raise _ScreenshotValidationError()
    if first_chunk == b"VP8X" and not saw_vp8x:
        raise _ScreenshotValidationError()


def _validate_image_container(mime_type: str, content: bytes) -> None:
    if mime_type == "image/png":
        _validate_png(content)
    elif mime_type == "image/jpeg":
        _validate_jpeg(content)
    elif mime_type == "image/webp":
        _validate_webp(content)
    else:
        raise _ScreenshotValidationError()
    _validate_decodable_static_image(mime_type, content)


def _validate_pillow_metadata(
    image: Image.Image,
    *,
    expected_format: str,
) -> None:
    if image.format != expected_format:
        raise _ScreenshotValidationError()
    try:
        width, height = image.size
        frame_count = getattr(image, "n_frames", 1)
    except (AttributeError, OSError, TypeError, ValueError) as error:
        raise _ScreenshotValidationError() from error
    _validate_pixel_dimensions(width, height)
    if frame_count != 1:
        raise _ScreenshotValidationError()


def _validate_decodable_static_image(
    mime_type: str,
    content: bytes,
) -> None:
    expected_format = _PIL_FORMATS.get(mime_type)
    if expected_format is None:
        raise _ScreenshotValidationError()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                _validate_pillow_metadata(
                    image,
                    expected_format=expected_format,
                )
                image.verify()
            with Image.open(io.BytesIO(content)) as decoded:
                _validate_pillow_metadata(
                    decoded,
                    expected_format=expected_format,
                )
                decoded.load()
    except _ScreenshotValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        EOFError,
        OSError,
        SyntaxError,
        TypeError,
        ValueError,
    ) as error:
        raise _ScreenshotValidationError() from error


def _validated_screenshots(
    screenshots: tuple[QuestionnaireMaterialScreenshot, ...],
) -> tuple[_ValidatedScreenshot, ...]:
    if not 1 <= len(screenshots) <= MAX_MATERIAL_SCREENSHOT_FILES:
        raise _ScreenshotValidationError()
    total_size = 0
    validated: list[_ValidatedScreenshot] = []
    for screenshot in screenshots:
        if not isinstance(screenshot, QuestionnaireMaterialScreenshot):
            raise _ScreenshotValidationError()
        filename = _normalized_filename(screenshot.filename)
        mime_type = _normalized_mime_type(screenshot.mime_type)
        extension = PurePosixPath(filename).suffix.casefold()
        if extension not in _MIME_EXTENSIONS[mime_type]:
            raise _ScreenshotValidationError()
        content = screenshot.content
        if (
            not isinstance(content, bytes)
            or not content
            or len(content) > MAX_MATERIAL_SCREENSHOT_BYTES
        ):
            raise _ScreenshotValidationError()
        total_size += len(content)
        if total_size > MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES:
            raise _ScreenshotValidationError()
        _validate_image_container(mime_type, content)
        validated.append(_ValidatedScreenshot(
            mime_type=mime_type,
            content=content,
            content_hash=content_sha256(content),
            canonical_extension=_CANONICAL_EXTENSIONS[mime_type],
        ))
    return tuple(validated)


def _review_warning() -> ImportWarning:
    return ImportWarning(
        code=SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE,
        message=(
            "连续截图只提供低可信视觉素材，缺少稳定题目 ID、完整结构、"
            "跳转规则和回答列映射，必须人工复核后才能作为问卷定义使用"
        ),
        blocking=True,
    )


def _material_identity(
    screenshots: tuple[_ValidatedScreenshot, ...],
) -> str:
    return structured_sha256({
        "schema_version": 1,
        "format": "ordered_questionnaire_screenshots",
        "images": [
            {
                "position": index,
                "mime_type": screenshot.mime_type,
                "size_bytes": len(screenshot.content),
                "sha256": screenshot.content_hash,
            }
            for index, screenshot in enumerate(screenshots)
        ],
    })


def _build_mapping(
    owner_ref: str,
    screenshots: tuple[_ValidatedScreenshot, ...],
    retrieved_at: datetime,
) -> _MaterialSnapshotMapping:
    identity_hash = _material_identity(screenshots)
    source_id = _stable_id("src", "questionnaire_screenshots", owner_ref, identity_hash)
    document_id = _stable_id("doc", source_id, identity_hash)
    snapshot_id = _stable_id("qsn", owner_ref, document_id, identity_hash)
    collection_id = _stable_id("rac", owner_ref, snapshot_id)
    warning = _review_warning()
    document_locator = SourceLocator(
        source_id=source_id,
        document_id=document_id,
        provider=Provider.LOCAL_UPLOAD,
        local_file_id=document_id,
    )
    source = ResearchSource(
        source_id=source_id,
        source_kind=SourceKind.LOCAL_UPLOAD,
        provider=Provider.LOCAL_UPLOAD,
        original_name="问卷连续截图",
        owner_ref=owner_ref,
        created_at=retrieved_at,
        acquisition_status=ProcessingStatus.NEEDS_REVIEW,
        access_status=AccessStatus.ACCESSIBLE,
        warnings=[warning],
    )
    document = ResearchDocument(
        document_id=document_id,
        source_id=source_id,
        document_type=DocumentType.DOCUMENT,
        title="问卷连续截图",
        size_bytes=sum(len(screenshot.content) for screenshot in screenshots),
        content_hash=identity_hash,
        retrieved_at=retrieved_at,
        snapshot_policy=SnapshotPolicy.FULL_COPY,
        parse_status=ProcessingStatus.NEEDS_REVIEW,
        source_locator=document_locator,
        warnings=[warning],
    )
    assets: list[ResearchAsset] = []
    references: list[AssetReference] = []
    media: dict[str, bytes] = {}
    for index, screenshot in enumerate(screenshots):
        asset_id = _stable_id(
            "asset",
            document_id,
            index,
            screenshot.mime_type,
            screenshot.content_hash,
        )
        locator = SourceLocator(
            source_id=source_id,
            document_id=document_id,
            provider=Provider.LOCAL_UPLOAD,
            local_file_id=asset_id,
            material_position=index,
        )
        assets.append(ResearchAsset(
            asset_id=asset_id,
            document_id=document_id,
            media_type=MediaType.IMAGE,
            mime_type=screenshot.mime_type,
            filename=(
                f"screenshot-{index + 1:03d}"
                f"{screenshot.canonical_extension}"
            ),
            display_name=f"问卷截图 {index + 1}",
            size_bytes=len(screenshot.content),
            content_hash=screenshot.content_hash,
            provider=Provider.LOCAL_UPLOAD,
            access_status=AccessStatus.ACCESSIBLE,
            processing_status=ProcessingStatus.NEEDS_REVIEW,
            sensitivity_status=SensitivityStatus.UNKNOWN,
            export_policy=ExportPolicy.MANUAL_CONFIRMATION,
            source_locator=locator,
            warnings=[warning],
        ))
        references.append(AssetReference(
            reference_id=_stable_id("aref", asset_id, document_id, index),
            asset_id=asset_id,
            context_type=AssetContextType.RESEARCH_DOCUMENT,
            context_id=document_id,
            role=AssetRole.RESEARCHER_MATERIAL,
            source_locator=locator,
            binding_status=BindingStatus.NEEDS_REVIEW,
            binding_confidence=0.0,
            warnings=[warning],
        ))
        media.setdefault(screenshot.content_hash, screenshot.content)

    snapshot = QuestionnaireSnapshot(
        snapshot_id=snapshot_id,
        document_id=document_id,
        provider=Provider.LOCAL_UPLOAD,
        source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
        title="问卷连续截图",
        retrieved_at=retrieved_at,
        content_hash=identity_hash,
        collection_state=CollectionState.UNKNOWN,
        item_count=0,
        question_count=0,
        asset_count=len(assets),
        mapping_status=MappingStatus.NEEDS_REVIEW,
        provider_raw_definition={
            "format": "ordered_questionnaire_screenshots",
            "trust_level": QuestionnaireMaterialTrustLevel.LOW.value,
            "image_count": len(screenshots),
            "images": [
                {
                    "position": index,
                    "mime_type": screenshot.mime_type,
                    "size_bytes": len(screenshot.content),
                }
                for index, screenshot in enumerate(screenshots)
            ],
        },
        warnings=[warning],
    )
    collection = ResearchAssetCollection(
        collection_id=collection_id,
        owner_ref=owner_ref,
        sources=[source],
        documents=[document],
        assets=assets,
        references=references,
    )
    attempt = QuestionnaireSourceAttempt(
        source_id=source_id,
        source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
        priority=questionnaire_source_priority(
            QuestionnaireSourceMode.MATERIAL_UPLOAD
        ),
        acquisition_route=(
            QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
        ),
        status=ProcessingStatus.NEEDS_REVIEW,
        snapshot_id=snapshot_id,
        warnings=[warning],
    )
    result = QuestionnaireSourceResult(
        snapshot=snapshot,
        collection=collection,
        selected_source_ids=[source_id],
        attempts=[attempt],
        partial_success=True,
    )
    return _MaterialSnapshotMapping(result=result, media=media)


def _summary(package: SnapshotPackage) -> QuestionnaireMaterialUploadSummary:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    total_size = sum(
        asset.size_bytes or 0
        for asset in collection.assets
        if asset.media_type == MediaType.IMAGE
    )
    return QuestionnaireMaterialUploadSummary(
        snapshot_id=snapshot.snapshot_id,
        provider=Provider.LOCAL_UPLOAD,
        source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
        mapping_status=MappingStatus.NEEDS_REVIEW,
        processing_status=ProcessingStatus.NEEDS_REVIEW,
        trust_level=QuestionnaireMaterialTrustLevel.LOW,
        file_count=len(collection.assets),
        total_size_bytes=total_size,
        image_count=sum(
            asset.media_type == MediaType.IMAGE
            for asset in collection.assets
        ),
        requires_human_review=True,
        warning_codes=[SCREENSHOT_MATERIAL_REVIEW_WARNING_CODE],
    )


def _load_existing(
    owner_ref: str,
    snapshot_id: str,
    storage: ResearchSnapshotStorage,
) -> SnapshotPackage | None:
    try:
        return load_questionnaire_source_snapshot(
            owner_ref,
            snapshot_id,
            storage,
        )
    except Exception as error:
        raise QuestionnaireMaterialInternalError() from error


def _validated_existing_summary(
    existing: object,
    *,
    owner_ref: str,
    screenshots: tuple[_ValidatedScreenshot, ...],
    expected_snapshot_id: str,
) -> QuestionnaireMaterialUploadSummary:
    if not isinstance(existing, SnapshotPackage):
        raise QuestionnaireMaterialInternalError()
    try:
        _validate_package_without_archive(
            existing,
            owner_ref,
            expected_snapshot_id,
        )
    except Exception as error:
        raise QuestionnaireMaterialInternalError() from error
    remapped = _build_mapping(
        owner_ref,
        screenshots,
        existing.bundle.snapshot.retrieved_at,
    )
    if remapped.package != existing:
        raise QuestionnaireMaterialConflictError()
    return _summary(existing)


def _import_screenshots(
    owner_ref: str,
    screenshots: tuple[QuestionnaireMaterialScreenshot, ...],
    clock: Callable[[], datetime],
    storage: ResearchSnapshotStorage,
) -> QuestionnaireMaterialUploadSummary:
    try:
        validated = _validated_screenshots(screenshots)
    except _ScreenshotValidationError as error:
        raise QuestionnaireMaterialInvalidError() from error
    retrieved_at = _retrieved_at(clock)
    try:
        mapped = _build_mapping(owner_ref, validated, retrieved_at)
    except QuestionnaireMaterialSnapshotApiError:
        raise
    except Exception as error:
        raise QuestionnaireMaterialInternalError() from error
    snapshot_id = mapped.result.snapshot.snapshot_id
    existing = _load_existing(owner_ref, snapshot_id, storage)
    if existing is not None:
        return _validated_existing_summary(
            existing,
            owner_ref=owner_ref,
            screenshots=validated,
            expected_snapshot_id=snapshot_id,
        )
    try:
        package = save_questionnaire_source_snapshot(
            mapped.result,
            mapped.media,
            storage,
        )
    except SnapshotConflictError:
        raced = _load_existing(owner_ref, snapshot_id, storage)
        if raced is None:
            raise QuestionnaireMaterialInternalError()
        return _validated_existing_summary(
            raced,
            owner_ref=owner_ref,
            screenshots=validated,
            expected_snapshot_id=snapshot_id,
        )
    except QuestionnaireMaterialSnapshotApiError:
        raise
    except Exception as error:
        raise QuestionnaireMaterialInternalError() from error
    return _summary(package)


async def _await_uncancelled(
    task: asyncio.Task[QuestionnaireMaterialUploadSummary],
) -> None:
    """外层取消后等待线程内原子保存结束，再传播取消。"""
    current = asyncio.current_task()
    while not task.done():
        if current is not None and hasattr(current, "uncancel"):
            current.uncancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    try:
        if not task.cancelled():
            task.exception()
    except BaseException:
        pass


@dataclass(frozen=True, slots=True)
class QuestionnaireMaterialSnapshotApi:
    """校验有序截图并原子保存 owner-scoped 低可信问卷素材。"""

    storage: ResearchSnapshotStorage
    clock: Callable[[], datetime] = field(default=_utc_now, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")
        if not callable(self.clock):
            raise TypeError("clock 必须可调用")

    async def import_screenshots(
        self,
        owner_ref: str,
        screenshots: Sequence[QuestionnaireMaterialScreenshot],
    ) -> QuestionnaireMaterialUploadSummary:
        owner = _require_owner(owner_ref)
        if (
            not isinstance(screenshots, Sequence)
            or isinstance(screenshots, (str, bytes, bytearray))
        ):
            raise QuestionnaireMaterialInvalidError()
        try:
            screenshot_count = len(screenshots)
        except Exception as error:
            raise QuestionnaireMaterialInvalidError() from error
        if not 1 <= screenshot_count <= MAX_MATERIAL_SCREENSHOT_FILES:
            raise QuestionnaireMaterialInvalidError()
        try:
            frozen_screenshots = tuple(screenshots)
        except Exception as error:
            raise QuestionnaireMaterialInvalidError() from error
        if len(frozen_screenshots) != screenshot_count:
            raise QuestionnaireMaterialInvalidError()
        persist_task = asyncio.create_task(asyncio.to_thread(
            _import_screenshots,
            owner,
            frozen_screenshots,
            self.clock,
            self.storage,
        ))
        try:
            return await asyncio.shield(persist_task)
        except asyncio.CancelledError:
            await _await_uncancelled(persist_task)
            raise
        except QuestionnaireMaterialSnapshotApiError:
            raise
        except Exception as error:
            raise QuestionnaireMaterialInternalError() from error
