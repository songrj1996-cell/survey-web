"""Deterministic, in-memory preprocessing for research image assets.

This service validates a ``ResearchAsset`` against its source bytes, snapshots
the required identity fields, fully decodes one static image, and emits metadata
plus a bounded PNG thumbnail.
It rejects missing canonical container boundaries and damage detected by Pillow
with truncated-image loading disabled. It does not independently parse JPEG
entropy semantics that Pillow/libjpeg accepts as a completed image. It performs
no persistence, OCR, or model calls.
"""

from __future__ import annotations

import io
import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from PIL import Image, ImageFile, ImageOps, __version__ as PILLOW_VERSION

from app.core.research_assets import content_sha256, structured_sha256
from app.schemas.research_assets import (
    AssetDerivative,
    DerivativeType,
    MediaType,
    ProcessingStatus,
    ResearchAsset,
    ReviewStatus,
)


MAX_IMAGE_INPUT_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_THUMBNAIL_EDGE_PIXELS = 1600
MAX_THUMBNAIL_OUTPUT_BYTES = 16 * 1024 * 1024

_EXIF_ORIENTATION_TAG = 274
_PROCESSOR_MODEL = "pillow"
_SUPPORTED_MIME_FORMATS = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
    "image/bmp": "BMP",
}
_TRANSPOSED_ORIENTATIONS = frozenset({2, 3, 4, 5, 6, 7, 8})
_SWAPPED_ORIENTATIONS = frozenset({5, 6, 7, 8})


class ImagePreprocessProfile(str, Enum):
    """Versioned deterministic preprocessing profiles."""

    V1 = "v1"


V1 = ImagePreprocessProfile.V1


class ImagePreprocessErrorCode(str, Enum):
    """Stable public failure codes; decoder details never cross this boundary."""

    INVALID_ASSET = "invalid_asset"
    INVALID_CONTENT = "invalid_content"
    CONTENT_TOO_LARGE = "content_too_large"
    CONTENT_SIZE_MISMATCH = "content_size_mismatch"
    CONTENT_HASH_MISMATCH = "content_hash_mismatch"
    UNSUPPORTED_MIME_TYPE = "unsupported_mime_type"
    FORMAT_MISMATCH = "format_mismatch"
    INVALID_IMAGE = "invalid_image"
    PIXEL_LIMIT_EXCEEDED = "pixel_limit_exceeded"
    MULTI_FRAME_NOT_SUPPORTED = "multi_frame_not_supported"
    OUTPUT_TOO_LARGE = "output_too_large"
    INVALID_CREATED_AT = "invalid_created_at"
    UNSUPPORTED_PROFILE = "unsupported_profile"
    PROCESSING_FAILED = "processing_failed"


class ResearchImagePreprocessError(ValueError):
    """A safe preprocessing error whose text is exactly its stable code."""

    def __init__(self, code: ImagePreprocessErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


@dataclass(frozen=True, slots=True)
class ImagePreprocessResult:
    """Two immutable derivative references and thumbnail bytes by SHA-256."""

    profile: ImagePreprocessProfile
    metadata_derivative: AssetDerivative
    thumbnail_derivative: AssetDerivative
    media: Mapping[str, bytes]

    @property
    def derivatives(self) -> tuple[AssetDerivative, AssetDerivative]:
        return (self.metadata_derivative, self.thumbnail_derivative)


@dataclass(frozen=True, slots=True)
class _DecodedImage:
    image: Image.Image
    source_format: str
    source_mode: str
    source_width: int
    source_height: int
    oriented_width: int
    oriented_height: int
    exif_orientation: int
    had_exif: bool
    had_icc_profile: bool


@dataclass(frozen=True, slots=True)
class _ValidatedImageInput:
    asset_id: str
    content: bytes
    source_hash: str
    mime_type: str


class _ThumbnailOutputLimitError(Exception):
    pass


class _BoundedBytesIO(io.BytesIO):
    """A BytesIO that refuses to allocate beyond a fixed output contract."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self._limit = limit

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self.tell() + len(data) > self._limit:
            raise _ThumbnailOutputLimitError
        return super().write(data)

    def writelines(
        self,
        lines: list[bytes | bytearray | memoryview],
    ) -> None:
        for line in lines:
            self.write(line)

    def truncate(self, size: int | None = None) -> int:
        target_size = self.tell() if size is None else size
        if target_size > self._limit:
            raise _ThumbnailOutputLimitError
        return super().truncate(size)


def _fail(code: ImagePreprocessErrorCode) -> ResearchImagePreprocessError:
    return ResearchImagePreprocessError(code)


def _require_profile(profile: object) -> ImagePreprocessProfile:
    if profile is not V1:
        raise _fail(ImagePreprocessErrorCode.UNSUPPORTED_PROFILE)
    return V1


def _require_created_at(created_at: object) -> datetime:
    if not isinstance(created_at, datetime) or created_at.tzinfo is None:
        raise _fail(ImagePreprocessErrorCode.INVALID_CREATED_AT)
    try:
        offset = created_at.utcoffset()
    except Exception:
        raise _fail(ImagePreprocessErrorCode.INVALID_CREATED_AT) from None
    if offset is None:
        raise _fail(ImagePreprocessErrorCode.INVALID_CREATED_AT)
    return created_at


def _validate_asset_and_content(
    asset: object,
    content: object,
) -> _ValidatedImageInput:
    if not isinstance(asset, ResearchAsset):
        raise _fail(ImagePreprocessErrorCode.INVALID_ASSET)
    asset_id = asset.asset_id
    media_type = asset.media_type
    declared_size = asset.size_bytes
    declared_hash = asset.content_hash
    mime_type = asset.mime_type
    if media_type != MediaType.IMAGE:
        raise _fail(ImagePreprocessErrorCode.INVALID_ASSET)
    if type(content) is not bytes or not content:
        raise _fail(ImagePreprocessErrorCode.INVALID_CONTENT)
    if len(content) > MAX_IMAGE_INPUT_BYTES:
        raise _fail(ImagePreprocessErrorCode.CONTENT_TOO_LARGE)
    if type(declared_size) is not int or declared_size != len(content):
        raise _fail(ImagePreprocessErrorCode.CONTENT_SIZE_MISMATCH)

    source_hash = content_sha256(content)
    if declared_hash is None or declared_hash != source_hash:
        raise _fail(ImagePreprocessErrorCode.CONTENT_HASH_MISMATCH)

    if type(mime_type) is not str or mime_type not in _SUPPORTED_MIME_FORMATS:
        raise _fail(ImagePreprocessErrorCode.UNSUPPORTED_MIME_TYPE)
    return _ValidatedImageInput(
        asset_id=asset_id,
        content=content,
        source_hash=source_hash,
        mime_type=mime_type,
    )


def _validated_image_details(
    image: Image.Image,
    *,
    expected_format: str,
) -> tuple[str, str, int, int]:
    image_format = image.format
    if image_format != expected_format:
        raise _fail(ImagePreprocessErrorCode.FORMAT_MISMATCH)
    try:
        width, height = image.size
        source_mode = image.mode
        frame_count = getattr(image, "n_frames", 1)
        is_animated = bool(getattr(image, "is_animated", False))
    except Exception:
        raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE) from None
    if (
        not isinstance(width, int)
        or isinstance(width, bool)
        or not isinstance(height, int)
        or isinstance(height, bool)
        or width <= 0
        or height <= 0
        or not isinstance(source_mode, str)
        or not source_mode
    ):
        raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)
    if width * height > MAX_IMAGE_PIXELS:
        raise _fail(ImagePreprocessErrorCode.PIXEL_LIMIT_EXCEEDED)
    if (
        not isinstance(frame_count, int)
        or isinstance(frame_count, bool)
        or frame_count != 1
        or is_animated
    ):
        raise _fail(ImagePreprocessErrorCode.MULTI_FRAME_NOT_SUPPORTED)
    return image_format, source_mode, width, height


def _validate_container_boundaries(mime_type: str, content: bytes) -> None:
    """Check canonical outer boundaries, not format-specific inner semantics."""

    valid = False
    if mime_type == "image/png":
        valid = (
            content.startswith(b"\x89PNG\r\n\x1a\n")
            and content.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        )
    elif mime_type == "image/jpeg":
        valid = content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9")
    elif mime_type == "image/webp":
        valid = (
            len(content) >= 12
            and content[:4] == b"RIFF"
            and content[8:12] == b"WEBP"
            and int.from_bytes(content[4:8], "little") + 8 == len(content)
        )
    elif mime_type == "image/gif":
        valid = (
            content[:6] in {b"GIF87a", b"GIF89a"}
            and content.endswith(b";")
        )
    elif mime_type == "image/bmp":
        valid = (
            len(content) >= 14
            and content[:2] == b"BM"
            and int.from_bytes(content[2:6], "little") == len(content)
        )
    if not valid:
        raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)


def _require_strict_pillow_decode() -> None:
    if ImageFile.LOAD_TRUNCATED_IMAGES is not False:
        raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)


def _resize_before_orientation(
    image: Image.Image,
    target_size: tuple[int, int],
) -> Image.Image:
    if image.size == target_size:
        return image
    return image.resize(
        target_size,
        resample=Image.Resampling.LANCZOS,
        reducing_gap=3.0,
    )


def _decode_and_normalize(
    content: bytes,
    *,
    mime_type: str,
) -> _DecodedImage:
    expected_format = _SUPPORTED_MIME_FORMATS[mime_type]
    first_details: tuple[str, str, int, int]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _require_strict_pillow_decode()
            with Image.open(io.BytesIO(content)) as opened:
                first_details = _validated_image_details(
                    opened,
                    expected_format=expected_format,
                )
                _validate_container_boundaries(mime_type, content)
                _require_strict_pillow_decode()
                opened.verify()
    except ResearchImagePreprocessError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _fail(ImagePreprocessErrorCode.PIXEL_LIMIT_EXCEEDED) from None
    except Exception:
        raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE) from None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _require_strict_pillow_decode()
            with Image.open(io.BytesIO(content)) as decoded:
                second_details = _validated_image_details(
                    decoded,
                    expected_format=expected_format,
                )
                if second_details != first_details:
                    raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)
                _require_strict_pillow_decode()
                decoded.load()
                _require_strict_pillow_decode()
                loaded_details = _validated_image_details(
                    decoded,
                    expected_format=expected_format,
                )
                if loaded_details != first_details:
                    raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)

                exif = decoded.getexif()
                orientation_value = exif.get(_EXIF_ORIENTATION_TAG, 1)
                if (
                    not isinstance(orientation_value, int)
                    or isinstance(orientation_value, bool)
                    or orientation_value not in range(1, 9)
                ):
                    raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)
                had_exif = bool(exif)
                had_icc_profile = bool(decoded.info.get("icc_profile"))
                oriented_size = (
                    (first_details[3], first_details[2])
                    if orientation_value in _SWAPPED_ORIENTATIONS
                    else (first_details[2], first_details[3])
                )
                thumbnail_size = _thumbnail_dimensions(*oriented_size)
                pre_orientation_size = (
                    (thumbnail_size[1], thumbnail_size[0])
                    if orientation_value in _SWAPPED_ORIENTATIONS
                    else thumbnail_size
                )
                resized = _resize_before_orientation(
                    decoded,
                    pre_orientation_size,
                )
                oriented = ImageOps.exif_transpose(resized)
                if oriented.size != thumbnail_size:
                    raise _fail(ImagePreprocessErrorCode.INVALID_IMAGE)

                has_transparency = (
                    "A" in oriented.getbands()
                    or bool(oriented.has_transparency_data)
                )
                output_mode = "RGBA" if has_transparency else "RGB"
                normalized = oriented.convert(output_mode)
                normalized.info.clear()
                normalized.load()
    except ResearchImagePreprocessError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise _fail(ImagePreprocessErrorCode.PIXEL_LIMIT_EXCEEDED) from None
    except Exception:
        raise _fail(ImagePreprocessErrorCode.PROCESSING_FAILED) from None

    return _DecodedImage(
        image=normalized,
        source_format=first_details[0],
        source_mode=first_details[1],
        source_width=first_details[2],
        source_height=first_details[3],
        oriented_width=oriented_size[0],
        oriented_height=oriented_size[1],
        exif_orientation=orientation_value,
        had_exif=had_exif,
        had_icc_profile=had_icc_profile,
    )


def _thumbnail_dimensions(width: int, height: int) -> tuple[int, int]:
    longest_edge = max(width, height)
    if longest_edge <= MAX_THUMBNAIL_EDGE_PIXELS:
        return width, height
    if width >= height:
        thumbnail_width = MAX_THUMBNAIL_EDGE_PIXELS
        thumbnail_height = max(
            1,
            (height * MAX_THUMBNAIL_EDGE_PIXELS + width // 2) // width,
        )
    else:
        thumbnail_height = MAX_THUMBNAIL_EDGE_PIXELS
        thumbnail_width = max(
            1,
            (width * MAX_THUMBNAIL_EDGE_PIXELS + height // 2) // height,
        )
    return thumbnail_width, thumbnail_height


def _encode_thumbnail(image: Image.Image) -> tuple[bytes, int, int, str]:
    width, height = _thumbnail_dimensions(*image.size)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            if image.size == (width, height):
                resized = image
            else:
                resized = image.resize(
                    (width, height),
                    resample=Image.Resampling.LANCZOS,
                    reducing_gap=3.0,
                )
            resized.info.clear()
            output = _BoundedBytesIO(MAX_THUMBNAIL_OUTPUT_BYTES)
            resized.save(
                output,
                format="PNG",
                optimize=False,
                compress_level=9,
                pnginfo=None,
                icc_profile=None,
            )
            thumbnail = output.getvalue()
    except _ThumbnailOutputLimitError:
        raise _fail(ImagePreprocessErrorCode.OUTPUT_TOO_LARGE) from None
    except ResearchImagePreprocessError:
        raise
    except Exception:
        raise _fail(ImagePreprocessErrorCode.PROCESSING_FAILED) from None
    if not thumbnail or len(thumbnail) > MAX_THUMBNAIL_OUTPUT_BYTES:
        raise _fail(ImagePreprocessErrorCode.OUTPUT_TOO_LARGE)
    return thumbnail, width, height, image.mode


def _stable_derivative_id(
    *,
    asset_id: str,
    source_hash: str,
    profile: ImagePreprocessProfile,
    processor_model: str,
    processor_version: str,
    derivative_type: DerivativeType,
    output_hash: str,
) -> str:
    identity_hash = structured_sha256({
        "schema_version": 1,
        "asset_id": asset_id,
        "source_content_hash": source_hash,
        "profile": profile.value,
        "model": processor_model,
        "model_version": processor_version,
        "derivative_type": derivative_type.value,
        "output_hash": output_hash,
    })
    return f"der_{identity_hash[:24]}"


def _preprocess_research_image_v1(
    asset: ResearchAsset,
    content: bytes,
    *,
    created_at: datetime,
    profile: ImagePreprocessProfile = V1,
) -> ImagePreprocessResult:
    """Preprocess one image after strict declarations and Pillow checks."""

    selected_profile = _require_profile(profile)
    derivative_created_at = _require_created_at(created_at)
    processor_model = _PROCESSOR_MODEL
    processor_version = PILLOW_VERSION
    validated_input = _validate_asset_and_content(asset, content)
    decoded = _decode_and_normalize(
        validated_input.content,
        mime_type=validated_input.mime_type,
    )
    thumbnail, thumbnail_width, thumbnail_height, thumbnail_mode = (
        _encode_thumbnail(decoded.image)
    )
    thumbnail_hash = content_sha256(thumbnail)
    metadata_payload = {
        "profile": selected_profile.value,
        "source_mime_type": validated_input.mime_type,
        "source_format": decoded.source_format,
        "source_size_bytes": len(validated_input.content),
        "source_content_hash": validated_input.source_hash,
        "source_width": decoded.source_width,
        "source_height": decoded.source_height,
        "source_mode": decoded.source_mode,
        "frame_count": 1,
        "exif_orientation": decoded.exif_orientation,
        "orientation_applied": (
            decoded.exif_orientation in _TRANSPOSED_ORIENTATIONS
        ),
        "oriented_width": decoded.oriented_width,
        "oriented_height": decoded.oriented_height,
        "had_exif": decoded.had_exif,
        "had_icc_profile": decoded.had_icc_profile,
        "metadata_stripped": True,
    }
    metadata_hash = structured_sha256(metadata_payload)
    thumbnail_payload = {
        "profile": selected_profile.value,
        "mime_type": "image/png",
        "size_bytes": len(thumbnail),
        "content_hash": thumbnail_hash,
        "width": thumbnail_width,
        "height": thumbnail_height,
        "mode": thumbnail_mode,
        "max_edge_pixels": MAX_THUMBNAIL_EDGE_PIXELS,
        "metadata_stripped": True,
    }
    created_by_ref = f"research-image-preprocessor:{selected_profile.value}"
    metadata_derivative = AssetDerivative(
        derivative_id=_stable_derivative_id(
            asset_id=validated_input.asset_id,
            source_hash=validated_input.source_hash,
            profile=selected_profile,
            processor_model=processor_model,
            processor_version=processor_version,
            derivative_type=DerivativeType.METADATA,
            output_hash=metadata_hash,
        ),
        asset_id=validated_input.asset_id,
        derivative_type=DerivativeType.METADATA,
        status=ProcessingStatus.COMPLETED,
        model=processor_model,
        model_version=processor_version,
        created_at=derivative_created_at,
        payload=metadata_payload,
        review_status=ReviewStatus.NOT_REQUIRED,
        created_by_ref=created_by_ref,
    )
    thumbnail_derivative = AssetDerivative(
        derivative_id=_stable_derivative_id(
            asset_id=validated_input.asset_id,
            source_hash=validated_input.source_hash,
            profile=selected_profile,
            processor_model=processor_model,
            processor_version=processor_version,
            derivative_type=DerivativeType.THUMBNAIL,
            output_hash=thumbnail_hash,
        ),
        asset_id=validated_input.asset_id,
        derivative_type=DerivativeType.THUMBNAIL,
        status=ProcessingStatus.COMPLETED,
        model=processor_model,
        model_version=processor_version,
        created_at=derivative_created_at,
        payload=thumbnail_payload,
        review_status=ReviewStatus.NOT_REQUIRED,
        created_by_ref=created_by_ref,
    )
    return ImagePreprocessResult(
        profile=selected_profile,
        metadata_derivative=metadata_derivative,
        thumbnail_derivative=thumbnail_derivative,
        media=MappingProxyType({thumbnail_hash: thumbnail}),
    )


def preprocess_research_image(
    asset: ResearchAsset,
    content: bytes,
    *,
    created_at: datetime,
    profile: ImagePreprocessProfile = V1,
) -> ImagePreprocessResult:
    """Preprocess one image after strict declarations and Pillow checks."""

    try:
        return _preprocess_research_image_v1(
            asset,
            content,
            created_at=created_at,
            profile=profile,
        )
    except ResearchImagePreprocessError:
        raise
    except Exception:
        raise _fail(ImagePreprocessErrorCode.PROCESSING_FAILED) from None
