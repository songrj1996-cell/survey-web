from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import struct
import unittest
from unittest.mock import patch
import zlib

from PIL import Image, ImageCms, ImageFile, ImageOps, PngImagePlugin, features
from PIL import __version__ as PILLOW_VERSION

from app.core.research_assets import validate_research_asset_collection
from app.schemas.research_assets import (
    DerivativeType,
    DocumentType,
    MediaType,
    ProcessingStatus,
    Provider,
    ResearchAsset,
    ResearchAssetCollection,
    ResearchDocument,
    ResearchSource,
    ReviewStatus,
    SnapshotPolicy,
    SourceKind,
)
from app.services import research_image_preprocessing as preprocessing
from app.services.research_image_preprocessing import (
    ImagePreprocessErrorCode,
    ResearchImagePreprocessError,
    V1,
    preprocess_research_image,
)


UTC = timezone.utc
CREATED_AT = datetime(2026, 8, 17, 10, 30, tzinfo=UTC)
_FORMAT_MIME_TYPES = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "BMP": "image/bmp",
}
_FORMAT_FILENAMES = {
    "PNG": "sample.png",
    "JPEG": "sample.jpg",
    "WEBP": "sample.webp",
    "GIF": "sample.gif",
    "BMP": "sample.bmp",
}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _asset(
    content: bytes,
    mime_type: str,
    *,
    asset_id: str = "asset-image",
    filename: str = "sample.bin",
) -> ResearchAsset:
    return ResearchAsset(
        asset_id=asset_id,
        document_id="document-image",
        media_type=MediaType.IMAGE,
        mime_type=mime_type,
        filename=filename,
        display_name="测试图片",
        size_bytes=len(content),
        content_hash=_sha256(content),
        provider=Provider.LOCAL_UPLOAD,
        processing_status=ProcessingStatus.PENDING,
    )


def _static_image_bytes(
    image_format: str,
    *,
    size: tuple[int, int] = (23, 17),
    color: tuple[int, int, int] = (12, 98, 203),
) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    save_options: dict[str, object] = {}
    if image_format == "JPEG":
        save_options.update(quality=92, subsampling=0)
    elif image_format == "WEBP":
        save_options.update(lossless=True, quality=100, method=6)
    image.save(output, format=image_format, **save_options)
    return output.getvalue()


def _animated_image_bytes(image_format: str) -> bytes:
    frames = [
        Image.new("RGBA", (5, 4), (255, 0, 0, 255)),
        Image.new("RGBA", (5, 4), (0, 0, 255, 255)),
    ]
    output = BytesIO()
    save_options: dict[str, object] = {
        "save_all": True,
        "append_images": frames[1:],
        "duration": 100,
        "loop": 0,
    }
    if image_format == "WEBP":
        save_options.update(lossless=True, quality=100, method=6)
    frames[0].save(output, format=image_format, **save_options)
    return output.getvalue()


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", zlib.crc32(chunk_type + payload) & 0xFFFFFFFF)
    )


def _oversized_png_header(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IEND", b"")
    )


def _thumbnail_bytes(result: object) -> bytes:
    media = result.media
    if len(media) != 1:
        raise AssertionError(f"expected one thumbnail media item, got {len(media)}")
    return next(iter(media.values()))


class ResearchImagePreprocessingTests(unittest.TestCase):
    def assertStableError(
        self,
        callable_obj,
        *args,
        expected_code: str | None = None,
        **kwargs,
    ) -> ImagePreprocessErrorCode:
        with self.assertRaises(ResearchImagePreprocessError) as raised:
            callable_obj(*args, **kwargs)
        error = raised.exception
        self.assertIsInstance(error.code, ImagePreprocessErrorCode)
        self.assertEqual(str(error), error.code.value)
        self.assertNotIn("Traceback", str(error))
        if expected_code is not None:
            self.assertEqual(error.code.value, expected_code)
        return error.code

    def test_static_png_jpeg_webp_gif_and_bmp_are_normalized_to_png(self):
        formats = ["PNG", "JPEG", "WEBP", "GIF", "BMP"]
        for image_format in formats:
            if image_format == "WEBP" and not features.check("webp"):
                continue
            with self.subTest(image_format=image_format):
                content = _static_image_bytes(image_format)
                asset = _asset(
                    content,
                    _FORMAT_MIME_TYPES[image_format],
                    filename=_FORMAT_FILENAMES[image_format],
                )

                result = preprocess_research_image(
                    asset,
                    content,
                    created_at=CREATED_AT,
                )

                self.assertEqual(
                    [item.derivative_type for item in result.derivatives],
                    [DerivativeType.METADATA, DerivativeType.THUMBNAIL],
                )
                for derivative in result.derivatives:
                    self.assertEqual(derivative.asset_id, asset.asset_id)
                    self.assertEqual(derivative.status, ProcessingStatus.COMPLETED)
                    self.assertEqual(
                        derivative.review_status,
                        ReviewStatus.NOT_REQUIRED,
                    )
                    self.assertEqual(derivative.created_at, CREATED_AT)

                thumbnail = _thumbnail_bytes(result)
                self.assertEqual(list(result.media), [_sha256(thumbnail)])
                with Image.open(BytesIO(thumbnail)) as normalized:
                    self.assertEqual(normalized.format, "PNG")
                    self.assertEqual(normalized.size, (23, 17))
                    normalized.load()

    def test_asset_media_mime_size_and_hash_must_match_content(self):
        png = _static_image_bytes("PNG")
        valid_asset = _asset(png, "image/png", filename="sensitive-name.png")
        invalid_assets = {
            "media_type": valid_asset.model_copy(
                update={"media_type": MediaType.VIDEO}
            ),
            "missing_mime": valid_asset.model_copy(update={"mime_type": None}),
            "wrong_mime": valid_asset.model_copy(update={"mime_type": "image/jpeg"}),
            "missing_size": valid_asset.model_copy(update={"size_bytes": None}),
            "wrong_size": valid_asset.model_copy(
                update={"size_bytes": len(png) + 1}
            ),
            "missing_hash": valid_asset.model_copy(update={"content_hash": None}),
            "wrong_hash": valid_asset.model_copy(update={"content_hash": "0" * 64}),
        }

        expected_codes = {
            "media_type": ImagePreprocessErrorCode.INVALID_ASSET,
            "missing_mime": ImagePreprocessErrorCode.UNSUPPORTED_MIME_TYPE,
            "wrong_mime": ImagePreprocessErrorCode.FORMAT_MISMATCH,
            "missing_size": ImagePreprocessErrorCode.CONTENT_SIZE_MISMATCH,
            "wrong_size": ImagePreprocessErrorCode.CONTENT_SIZE_MISMATCH,
            "missing_hash": ImagePreprocessErrorCode.CONTENT_HASH_MISMATCH,
            "wrong_hash": ImagePreprocessErrorCode.CONTENT_HASH_MISMATCH,
        }
        for case, asset in invalid_assets.items():
            with self.subTest(case=case):
                self.assertStableError(
                    preprocess_research_image,
                    asset,
                    png,
                    created_at=CREATED_AT,
                    expected_code=expected_codes[case].value,
                )

    def test_content_must_be_exact_bytes_and_created_at_timezone_aware(self):
        png = _static_image_bytes("PNG")
        asset = _asset(png, "image/png")

        class BytesSubclass(bytes):
            pass

        self.assertStableError(
            preprocess_research_image,
            asset,
            bytearray(png),
            created_at=CREATED_AT,
            expected_code=ImagePreprocessErrorCode.INVALID_CONTENT.value,
        )
        self.assertStableError(
            preprocess_research_image,
            asset,
            memoryview(png),
            created_at=CREATED_AT,
            expected_code=ImagePreprocessErrorCode.INVALID_CONTENT.value,
        )
        self.assertStableError(
            preprocess_research_image,
            asset,
            BytesSubclass(png),
            created_at=CREATED_AT,
            expected_code=ImagePreprocessErrorCode.INVALID_CONTENT.value,
        )
        self.assertStableError(
            preprocess_research_image,
            _asset(b"", "image/png"),
            b"",
            created_at=CREATED_AT,
            expected_code=ImagePreprocessErrorCode.INVALID_CONTENT.value,
        )
        self.assertStableError(
            preprocess_research_image,
            asset,
            png,
            created_at=CREATED_AT.replace(tzinfo=None),
            expected_code=ImagePreprocessErrorCode.INVALID_CREATED_AT.value,
        )

    def test_input_byte_limit_and_v1_profile_are_strict(self):
        content = _static_image_bytes("PNG")
        asset = _asset(content, "image/png")

        with patch.object(
            preprocessing,
            "MAX_IMAGE_INPUT_BYTES",
            len(content) - 1,
        ):
            self.assertStableError(
                preprocess_research_image,
                asset,
                content,
                created_at=CREATED_AT,
                expected_code=ImagePreprocessErrorCode.CONTENT_TOO_LARGE.value,
            )

        class ForeignProfile(str):
            pass

        for profile in ("v1", "v2", ForeignProfile("v1"), object()):
            with self.subTest(profile=repr(profile)):
                self.assertStableError(
                    preprocess_research_image,
                    asset,
                    content,
                    created_at=CREATED_AT,
                    profile=profile,
                    expected_code=(
                        ImagePreprocessErrorCode.UNSUPPORTED_PROFILE.value
                    ),
                )

        result = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
            profile=V1,
        )
        self.assertIs(result.profile, V1)

    def test_format_spoof_truncation_and_decoder_details_are_rejected_safely(self):
        png = _static_image_bytes("PNG")
        jpeg = _static_image_bytes("JPEG", size=(64, 48))

        spoof = _asset(png, "image/jpeg", filename="private-secret-name.jpg")
        spoof_error = self.assertStableError(
            preprocess_research_image,
            spoof,
            png,
            created_at=CREATED_AT,
        )

        truncated = jpeg[:-32]
        truncated_asset = _asset(
            truncated,
            "image/jpeg",
            filename="/Users/private/interview-secret.jpg",
        )
        truncated_error = self.assertStableError(
            preprocess_research_image,
            truncated_asset,
            truncated,
            created_at=CREATED_AT,
        )

        malformed = b"not-an-image:/Users/private/material.png\x00\xff"
        malformed_asset = _asset(
            malformed,
            "image/png",
            filename="/Users/private/material.png",
        )
        malformed_error = self.assertStableError(
            preprocess_research_image,
            malformed_asset,
            malformed,
            created_at=CREATED_AT,
        )

        for error in (spoof_error, truncated_error, malformed_error):
            rendered = error.value
            self.assertNotIn("private", rendered.lower())
            self.assertNotIn("users", rendered.lower())

    def test_truncated_containers_stay_rejected_when_pillow_flag_is_enabled(self):
        formats = ["PNG", "JPEG", "GIF", "BMP"]
        if features.check("webp"):
            formats.append("WEBP")

        for image_format in formats:
            with self.subTest(image_format=image_format):
                content = _static_image_bytes(image_format)[:-1]
                asset = _asset(
                    content,
                    _FORMAT_MIME_TYPES[image_format],
                    filename=_FORMAT_FILENAMES[image_format],
                )
                with patch.object(ImageFile, "LOAD_TRUNCATED_IMAGES", True):
                    self.assertStableError(
                        preprocess_research_image,
                        asset,
                        content,
                        created_at=CREATED_AT,
                        expected_code=(
                            ImagePreprocessErrorCode.INVALID_IMAGE.value
                        ),
                    )

    def test_permissive_pillow_truncation_mode_rejects_valid_content(self):
        content = _static_image_bytes("PNG")
        asset = _asset(content, "image/png", filename="complete.png")

        preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )
        with patch.object(ImageFile, "LOAD_TRUNCATED_IMAGES", True):
            self.assertStableError(
                preprocess_research_image,
                asset,
                content,
                created_at=CREATED_AT,
                expected_code=ImagePreprocessErrorCode.INVALID_IMAGE.value,
            )

    def test_pixel_limit_is_checked_before_decoder_load(self):
        content = _oversized_png_header(8_001, 5_000)
        asset = _asset(content, "image/png", filename="large.png")

        with patch.object(
            PngImagePlugin.PngImageFile,
            "load",
            side_effect=AssertionError("decoder load must not run"),
        ) as load:
            self.assertStableError(
                preprocess_research_image,
                asset,
                content,
                created_at=CREATED_AT,
            )

        load.assert_not_called()

    def test_decompression_bomb_warning_is_promoted_to_stable_error(self):
        content = _static_image_bytes("PNG", size=(3, 2))
        asset = _asset(content, "image/png")

        with patch.object(Image, "MAX_IMAGE_PIXELS", 4):
            self.assertStableError(
                preprocess_research_image,
                asset,
                content,
                created_at=CREATED_AT,
                expected_code=(
                    ImagePreprocessErrorCode.PIXEL_LIMIT_EXCEEDED.value
                ),
            )

    def test_pillow_bomb_warning_and_error_map_to_pixel_limit(self):
        for width, height in ((10_000, 10_000), (20_000, 10_000)):
            with self.subTest(width=width, height=height):
                content = _oversized_png_header(width, height)
                asset = _asset(content, "image/png", filename="bomb.png")
                self.assertStableError(
                    preprocess_research_image,
                    asset,
                    content,
                    created_at=CREATED_AT,
                    expected_code=(
                        ImagePreprocessErrorCode.PIXEL_LIMIT_EXCEEDED.value
                    ),
                )

    def test_gif_apng_and_webp_multiframe_content_is_rejected(self):
        cases = [
            ("GIF", "image/gif"),
            ("PNG", "image/png"),
        ]
        if features.check("webp"):
            cases.append(("WEBP", "image/webp"))

        for image_format, mime_type in cases:
            with self.subTest(image_format=image_format):
                content = _animated_image_bytes(image_format)
                asset = _asset(
                    content,
                    mime_type,
                    filename=f"animated.{image_format.lower()}",
                )
                self.assertStableError(
                    preprocess_research_image,
                    asset,
                    content,
                    created_at=CREATED_AT,
                )

    def test_second_open_revalidates_format_size_and_frame_count(self):
        first_content = _static_image_bytes("PNG", size=(6, 4))
        asset = _asset(first_content, "image/png")
        original_open = Image.open
        replacements = {
            "format": _static_image_bytes("JPEG", size=(6, 4)),
            "size": _static_image_bytes("PNG", size=(7, 4)),
            "frames": _animated_image_bytes("PNG"),
        }

        for case, second_content in replacements.items():
            with self.subTest(case=case):
                call_count = 0

                def changing_open(fp, *args, **kwargs):
                    nonlocal call_count
                    call_count += 1
                    if call_count == 1:
                        return original_open(BytesIO(first_content))
                    if call_count == 2:
                        return original_open(BytesIO(second_content))
                    return original_open(fp, *args, **kwargs)

                with patch.object(Image, "open", side_effect=changing_open):
                    self.assertStableError(
                        preprocess_research_image,
                        asset,
                        first_content,
                        created_at=CREATED_AT,
                    )
                self.assertEqual(call_count, 2)

    def test_exif_orientation_is_applied_and_metadata_is_stripped(self):
        image = Image.new("RGB", (2, 3))
        image.putdata(
            [
                (255, 0, 0),
                (0, 255, 0),
                (0, 0, 255),
                (255, 255, 0),
                (0, 255, 255),
                (255, 0, 255),
            ]
        )
        exif = image.getexif()
        exif[274] = 6
        exif[315] = "private researcher"
        icc_profile = ImageCms.ImageCmsProfile(
            ImageCms.createProfile("sRGB")
        ).tobytes()
        png_info = PngImagePlugin.PngInfo()
        png_info.add_text("PrivateNote", "private interview material")
        output = BytesIO()
        image.save(
            output,
            format="PNG",
            exif=exif.tobytes(),
            icc_profile=icc_profile,
            pnginfo=png_info,
        )
        content = output.getvalue()
        asset = _asset(content, "image/png", filename="oriented.png")

        result = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )

        thumbnail = _thumbnail_bytes(result)
        self.assertNotIn(b"private researcher", thumbnail)
        self.assertNotIn(b"private interview material", thumbnail)
        with Image.open(BytesIO(thumbnail)) as normalized:
            normalized.load()
            self.assertEqual(normalized.size, (3, 2))
            self.assertEqual(normalized.getpixel((0, 0)), (0, 255, 255))
            self.assertEqual(len(normalized.getexif()), 0)
            self.assertNotIn("icc_profile", normalized.info)
            self.assertNotIn("PrivateNote", normalized.info)

    def test_all_exif_orientations_match_pillow_reference_pixels(self):
        source_pixels = [
            (255, 0, 0),
            (0, 255, 0),
            (0, 0, 255),
            (255, 255, 0),
            (0, 255, 255),
            (255, 0, 255),
        ]

        for orientation in range(1, 9):
            with self.subTest(orientation=orientation):
                image = Image.new("RGB", (2, 3))
                image.putdata(source_pixels)
                exif = image.getexif()
                exif[274] = orientation
                output = BytesIO()
                image.save(output, format="PNG", exif=exif.tobytes())
                content = output.getvalue()
                asset = _asset(
                    content,
                    "image/png",
                    filename=f"orientation-{orientation}.png",
                )

                with Image.open(BytesIO(content)) as reference_source:
                    reference = ImageOps.exif_transpose(reference_source).convert(
                        "RGB"
                    )
                    reference.load()
                    expected_size = reference.size
                    expected_pixels = [
                        reference.getpixel((x, y))
                        for y in range(reference.height)
                        for x in range(reference.width)
                    ]

                result = preprocess_research_image(
                    asset,
                    content,
                    created_at=CREATED_AT,
                )
                with Image.open(BytesIO(_thumbnail_bytes(result))) as thumbnail:
                    thumbnail.load()
                    actual_pixels = [
                        thumbnail.getpixel((x, y))
                        for y in range(thumbnail.height)
                        for x in range(thumbnail.width)
                    ]
                    self.assertEqual(thumbnail.size, expected_size)
                    self.assertEqual(actual_pixels, expected_pixels)

                metadata = result.metadata_derivative.payload
                self.assertEqual(metadata["exif_orientation"], orientation)
                self.assertEqual(
                    metadata["orientation_applied"],
                    orientation != 1,
                )
                self.assertEqual(
                    (metadata["oriented_width"], metadata["oriented_height"]),
                    expected_size,
                )

    def test_thumbnail_longest_edge_is_1600_and_transparent_pixels_survive(self):
        image = Image.new("RGBA", (3_200, 800), (10, 20, 30, 0))
        image.putpixel((0, 0), (200, 100, 50, 255))
        output = BytesIO()
        image.save(output, format="PNG")
        content = output.getvalue()
        asset = _asset(content, "image/png", filename="wide.png")

        result = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )

        with Image.open(BytesIO(_thumbnail_bytes(result))) as thumbnail:
            thumbnail.load()
            self.assertEqual(thumbnail.size, (1_600, 400))
            self.assertEqual(thumbnail.mode, "RGBA")
            self.assertLess(thumbnail.getpixel((1_599, 399))[3], 255)

    def test_large_image_is_downscaled_before_mode_conversion(self):
        content = _static_image_bytes("PNG", size=(3_200, 800))
        asset = _asset(content, "image/png", filename="large-source.png")
        original_convert = Image.Image.convert
        conversion_sizes: list[tuple[int, int]] = []

        def recording_convert(image, *args, **kwargs):
            conversion_sizes.append(image.size)
            return original_convert(image, *args, **kwargs)

        with patch.object(Image.Image, "convert", new=recording_convert):
            result = preprocess_research_image(
                asset,
                content,
                created_at=CREATED_AT,
            )

        self.assertTrue(conversion_sizes)
        self.assertTrue(
            all(max(size) <= 1_600 for size in conversion_sizes),
            conversion_sizes,
        )
        with Image.open(BytesIO(_thumbnail_bytes(result))) as thumbnail:
            thumbnail.load()
            self.assertEqual(thumbnail.size, (1_600, 400))

    def test_bytes_and_derivative_ids_are_deterministic_across_created_at(self):
        content = _static_image_bytes("PNG", size=(2_000, 1_000))
        asset = _asset(content, "image/png")

        first = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
            profile=V1,
        )
        second = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT + timedelta(days=7),
            profile=V1,
        )

        self.assertEqual(_thumbnail_bytes(first), _thumbnail_bytes(second))
        self.assertEqual(
            [item.derivative_id for item in first.derivatives],
            [item.derivative_id for item in second.derivatives],
        )
        self.assertEqual(first.metadata_derivative.created_at, CREATED_AT)
        self.assertEqual(
            second.metadata_derivative.created_at,
            CREATED_AT + timedelta(days=7),
        )

    def test_asset_identity_is_snapshotted_before_image_decode(self):
        content = _static_image_bytes("PNG")
        baseline_asset = _asset(content, "image/png", asset_id="asset-original")
        baseline = preprocess_research_image(
            baseline_asset,
            content,
            created_at=CREATED_AT,
        )
        attacked_asset = _asset(
            content,
            "image/png",
            asset_id="asset-original",
        )
        original_decode = preprocessing._decode_and_normalize

        def mutate_asset_then_decode(*args, **kwargs):
            attacked_asset.asset_id = "asset-mutated-during-decode"
            return original_decode(*args, **kwargs)

        with patch.object(
            preprocessing,
            "_decode_and_normalize",
            side_effect=mutate_asset_then_decode,
        ):
            result = preprocess_research_image(
                attacked_asset,
                content,
                created_at=CREATED_AT,
            )

        self.assertEqual(
            [item.asset_id for item in result.derivatives],
            ["asset-original", "asset-original"],
        )
        self.assertEqual(
            [item.derivative_id for item in result.derivatives],
            [item.derivative_id for item in baseline.derivatives],
        )

    def test_pillow_version_participates_in_both_derivative_ids(self):
        content = _static_image_bytes("PNG")
        asset = _asset(content, "image/png")
        baseline = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )
        alternate_version = f"{PILLOW_VERSION}-contract-test"

        with patch.object(preprocessing, "PILLOW_VERSION", alternate_version):
            changed = preprocess_research_image(
                asset,
                content,
                created_at=CREATED_AT,
            )

        self.assertEqual(_thumbnail_bytes(changed), _thumbnail_bytes(baseline))
        for before, after in zip(
            baseline.derivatives,
            changed.derivatives,
            strict=True,
        ):
            self.assertNotEqual(after.derivative_id, before.derivative_id)
            self.assertEqual(after.model, "pillow")
            self.assertEqual(after.model_version, alternate_version)

    def test_derivative_payloads_are_complete_and_match_emitted_bytes(self):
        content = _static_image_bytes("JPEG", size=(2_000, 1_000))
        asset = _asset(content, "image/jpeg", filename="source.jpg")

        result = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )
        thumbnail = _thumbnail_bytes(result)
        metadata = result.metadata_derivative
        thumbnail_derivative = result.thumbnail_derivative

        self.assertEqual(
            set(metadata.payload),
            {
                "profile",
                "source_mime_type",
                "source_format",
                "source_size_bytes",
                "source_content_hash",
                "source_width",
                "source_height",
                "source_mode",
                "frame_count",
                "exif_orientation",
                "orientation_applied",
                "oriented_width",
                "oriented_height",
                "had_exif",
                "had_icc_profile",
                "metadata_stripped",
            },
        )
        self.assertEqual(
            metadata.payload,
            {
                "profile": "v1",
                "source_mime_type": "image/jpeg",
                "source_format": "JPEG",
                "source_size_bytes": len(content),
                "source_content_hash": _sha256(content),
                "source_width": 2_000,
                "source_height": 1_000,
                "source_mode": "RGB",
                "frame_count": 1,
                "exif_orientation": 1,
                "orientation_applied": False,
                "oriented_width": 2_000,
                "oriented_height": 1_000,
                "had_exif": False,
                "had_icc_profile": False,
                "metadata_stripped": True,
            },
        )
        self.assertEqual(
            thumbnail_derivative.payload,
            {
                "profile": "v1",
                "mime_type": "image/png",
                "size_bytes": len(thumbnail),
                "content_hash": _sha256(thumbnail),
                "width": 1_600,
                "height": 800,
                "mode": "RGB",
                "max_edge_pixels": 1_600,
                "metadata_stripped": True,
            },
        )
        for derivative in result.derivatives:
            self.assertEqual(derivative.model, "pillow")
            self.assertEqual(derivative.model_version, PILLOW_VERSION)
            self.assertEqual(
                derivative.created_by_ref,
                "research-image-preprocessor:v1",
            )

    def test_thumbnail_writer_enforces_output_limit(self):
        content = _static_image_bytes("PNG", size=(100, 100))
        asset = _asset(content, "image/png")

        with patch.object(preprocessing, "MAX_THUMBNAIL_OUTPUT_BYTES", 64):
            self.assertStableError(
                preprocess_research_image,
                asset,
                content,
                created_at=CREATED_AT,
                expected_code=ImagePreprocessErrorCode.OUTPUT_TOO_LARGE.value,
            )

    def test_source_asset_is_not_mutated_and_result_media_is_immutable(self):
        content = _static_image_bytes("BMP")
        asset = _asset(content, "image/bmp", filename="source.bmp")
        before = asset.model_dump(mode="python")

        result = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )

        self.assertEqual(asset.model_dump(mode="python"), before)
        media_key = next(iter(result.media))
        with self.assertRaises(TypeError):
            result.media[media_key] = b"changed"
        with self.assertRaises(FrozenInstanceError):
            result.media = {}

    def test_derivatives_pass_existing_collection_integrity_validator(self):
        content = _static_image_bytes("PNG")
        asset = _asset(content, "image/png")
        result = preprocess_research_image(
            asset,
            content,
            created_at=CREATED_AT,
        )
        source = ResearchSource(
            source_id="source-image",
            source_kind=SourceKind.LOCAL_UPLOAD,
            provider=Provider.LOCAL_UPLOAD,
            original_name="sample.png",
            owner_ref="test-owner",
            created_at=CREATED_AT,
            acquisition_status=ProcessingStatus.COMPLETED,
        )
        document = ResearchDocument(
            document_id=asset.document_id,
            source_id=source.source_id,
            document_type=DocumentType.IMAGE,
            title="测试图片",
            filename="sample.png",
            mime_type="image/png",
            size_bytes=len(content),
            content_hash=_sha256(content),
            retrieved_at=CREATED_AT,
            snapshot_policy=SnapshotPolicy.FULL_COPY,
            parse_status=ProcessingStatus.COMPLETED,
        )
        collection = ResearchAssetCollection(
            collection_id="collection-image",
            owner_ref="test-owner",
            sources=[source],
            documents=[document],
            assets=[asset],
            derivatives=list(result.derivatives),
        )

        validate_research_asset_collection(collection)


if __name__ == "__main__":
    unittest.main()
