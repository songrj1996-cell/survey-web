"""Owner-scoped, review-safe projections for questionnaire assets.

The service loads and validates a complete snapshot package inside a
worker thread.  Public DTOs deliberately expose neither storage/provider
locators nor raw domain identifiers.  Thumbnail generation is in-memory only.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from app.schemas.questionnaire import (
    CanonicalQuestion,
    CollectionState,
    MappingStatus,
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_asset_review import (
    QuestionnaireAssetPreviewStatus,
    QuestionnaireAssetReviewItem,
    QuestionnaireAssetReviewProjection,
    QuestionnaireAssetThumbnailResult,
)
from app.schemas.research_assets import (
    AccessStatus,
    AssetContextType,
    AssetReference,
    BindingStatus,
    MediaType,
    ProcessingStatus,
    Provider,
    ResearchAsset,
    ResearchAssetCollection,
)
from app.services.research_image_preprocessing import (
    MAX_THUMBNAIL_OUTPUT_BYTES,
    ResearchImagePreprocessError,
    preprocess_research_image,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchSnapshotCatalogStorage,
    SnapshotCatalogEntry,
    SnapshotCatalogPage,
    SnapshotPackage,
)


MAX_QUESTIONNAIRE_ASSET_REVIEW_REFERENCES = 2000

_MAX_PUBLIC_IDENTIFIER_BYTES = 4096
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_WARNING_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$")
_TOKEN_ROOT_DOMAIN = b"questionnaire-asset-review-token:v1"
_NESTED_CONTEXT_QUESTION_MAX_CHARS = 249
_NESTED_CONTEXT_DETAIL_MAX_CHARS = 248
_TEXT_SCAN_BUDGET_MULTIPLIER = 8
_MAX_SOURCE_WARNINGS_PER_SUBJECT = 256
_SUPPORTED_IMAGE_MIME_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "image/bmp",
    }
)


class QuestionnaireAssetReviewApiError(RuntimeError):
    """Safe public service error whose text never contains internal details."""

    _safe_message = "问卷素材审阅处理失败"

    def __init__(self) -> None:
        super().__init__(self._safe_message)


class QuestionnaireAssetReviewInvalidError(QuestionnaireAssetReviewApiError):
    """The owner, snapshot id, or opaque asset token is malformed."""

    _safe_message = "问卷素材审阅请求无效"


class QuestionnaireAssetReviewNotFoundError(QuestionnaireAssetReviewApiError):
    """The owner-scoped snapshot or previewable image does not exist."""

    _safe_message = "问卷素材审阅资源不存在"


class QuestionnaireAssetReviewInternalError(QuestionnaireAssetReviewApiError):
    """A dependency or stored snapshot failed closed validation."""


def _require_owner_ref(value: object) -> str:
    if type(value) is not str:
        raise QuestionnaireAssetReviewInvalidError()
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_PUBLIC_IDENTIFIER_BYTES:
        raise QuestionnaireAssetReviewInvalidError()
    try:
        encoded_length = len(normalized.encode("utf-8"))
    except UnicodeEncodeError:
        raise QuestionnaireAssetReviewInvalidError() from None
    if encoded_length > _MAX_PUBLIC_IDENTIFIER_BYTES:
        raise QuestionnaireAssetReviewInvalidError()
    return normalized


def _require_snapshot_id(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_PUBLIC_IDENTIFIER_BYTES
    ):
        raise QuestionnaireAssetReviewInvalidError()
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise QuestionnaireAssetReviewInvalidError() from None
    if encoded_length > _MAX_PUBLIC_IDENTIFIER_BYTES:
        raise QuestionnaireAssetReviewInvalidError()
    return value


def _require_asset_token(value: object) -> str:
    if type(value) is not str or _TOKEN_PATTERN.fullmatch(value) is None:
        raise QuestionnaireAssetReviewInvalidError()
    return value


def _opaque_token(
    domain: bytes,
    *,
    owner_ref: str,
    snapshot_id: str,
    raw_identifier: str,
) -> str:
    """Hash length-framed identity parts under distinct asset/reference domains."""

    digest = hashlib.sha256()
    for part in (
        _TOKEN_ROOT_DOMAIN,
        domain,
        owner_ref.encode("utf-8"),
        snapshot_id.encode("utf-8"),
        raw_identifier.encode("utf-8"),
    ):
        digest.update(len(part).to_bytes(8, "big"))
        digest.update(part)
    return digest.hexdigest()


def _asset_token(owner_ref: str, snapshot_id: str, asset_id: str) -> str:
    return _opaque_token(
        b"asset",
        owner_ref=owner_ref,
        snapshot_id=snapshot_id,
        raw_identifier=asset_id,
    )


def _reference_token(
    owner_ref: str,
    snapshot_id: str,
    reference_id: str,
) -> str:
    return _opaque_token(
        b"reference",
        owner_ref=owner_ref,
        snapshot_id=snapshot_id,
        raw_identifier=reference_id,
    )


def _load_validated_package(
    storage: ResearchSnapshotCatalogStorage,
    owner_ref: str,
    snapshot_id: str,
) -> SnapshotPackage:
    _require_catalog_match(storage, owner_ref, snapshot_id)
    try:
        loaded = storage.load_snapshot_package(owner_ref, snapshot_id)
    except Exception:
        raise QuestionnaireAssetReviewInternalError() from None
    if loaded is None:
        raise QuestionnaireAssetReviewInternalError()
    if not isinstance(loaded, SnapshotPackage):
        raise QuestionnaireAssetReviewInternalError()

    try:
        if not isinstance(loaded.bundle, ResearchAssetBundle):
            raise QuestionnaireAssetReviewInternalError()
        if type(loaded.media) is not dict:
            raise QuestionnaireAssetReviewInternalError()
        snapshot = loaded.bundle.snapshot
        collection = loaded.bundle.collection
        if (
            not isinstance(snapshot, QuestionnaireSnapshot)
            or not isinstance(collection, ResearchAssetCollection)
        ):
            raise QuestionnaireAssetReviewInternalError()
        if snapshot.snapshot_id != snapshot_id:
            raise QuestionnaireAssetReviewInternalError()
        if collection.owner_ref != owner_ref:
            raise QuestionnaireAssetReviewInternalError()
        if len(collection.references) > MAX_QUESTIONNAIRE_ASSET_REVIEW_REFERENCES:
            raise QuestionnaireAssetReviewInternalError()
    except QuestionnaireAssetReviewApiError:
        raise
    except Exception:
        raise QuestionnaireAssetReviewInternalError() from None
    return loaded


def _snapshot_storage_key(snapshot_id: str) -> str:
    return hashlib.sha256(snapshot_id.encode("utf-8")).hexdigest()


def _storage_key_predecessor(storage_key: str) -> str | None:
    numeric_key = int(storage_key, 16)
    if numeric_key == 0:
        return None
    return f"{numeric_key - 1:064x}"


def _validate_catalog_entry(
    entry: object,
    *,
    owner_ref: str,
) -> SnapshotCatalogEntry:
    if not isinstance(entry, SnapshotCatalogEntry):
        raise QuestionnaireAssetReviewInternalError()
    if (
        type(entry.owner_ref) is not str
        or entry.owner_ref != owner_ref
        or type(entry.snapshot_id) is not str
        or not entry.snapshot_id
        or entry.snapshot_id != entry.snapshot_id.strip()
        or len(entry.snapshot_id.encode("utf-8")) > _MAX_PUBLIC_IDENTIFIER_BYTES
        or type(entry.storage_key) is not str
        or _TOKEN_PATTERN.fullmatch(entry.storage_key) is None
        or _snapshot_storage_key(entry.snapshot_id) != entry.storage_key
        or not isinstance(entry.provider, Provider)
        or not isinstance(entry.source_mode, QuestionnaireSourceMode)
        or not isinstance(entry.collection_state, CollectionState)
        or not isinstance(entry.mapping_status, MappingStatus)
        or any(
            type(value) is not int or value < 0
            for value in (
                entry.item_count,
                entry.question_count,
                entry.asset_count,
                entry.image_asset_count,
                entry.asset_reference_count,
            )
        )
        or entry.image_asset_count > entry.asset_count
        or (
            entry.asset_reference_count
            > MAX_QUESTIONNAIRE_ASSET_REVIEW_REFERENCES
        )
    ):
        raise QuestionnaireAssetReviewInternalError()
    return entry


def _require_catalog_match(
    storage: ResearchSnapshotCatalogStorage,
    owner_ref: str,
    snapshot_id: str,
) -> None:
    target_key = _snapshot_storage_key(snapshot_id)
    cursor = _storage_key_predecessor(target_key)
    try:
        page = storage.list_snapshot_catalog(
            owner_ref,
            cursor=cursor,
            limit=1,
        )
    except Exception:
        raise QuestionnaireAssetReviewInternalError() from None
    if (
        not isinstance(page, SnapshotCatalogPage)
        or type(page.entries) is not tuple
        or len(page.entries) > 1
        or (
            page.next_cursor is not None
            and (
                type(page.next_cursor) is not str
                or _TOKEN_PATTERN.fullmatch(page.next_cursor) is None
            )
        )
    ):
        raise QuestionnaireAssetReviewInternalError()
    if not page.entries:
        if page.next_cursor is not None:
            raise QuestionnaireAssetReviewInternalError()
        raise QuestionnaireAssetReviewNotFoundError()

    entry = _validate_catalog_entry(page.entries[0], owner_ref=owner_ref)
    if cursor is not None and entry.storage_key <= cursor:
        raise QuestionnaireAssetReviewInternalError()
    if page.next_cursor is not None and page.next_cursor != entry.storage_key:
        raise QuestionnaireAssetReviewInternalError()
    if entry.storage_key > target_key:
        raise QuestionnaireAssetReviewNotFoundError()
    if entry.storage_key != target_key or entry.snapshot_id != snapshot_id:
        raise QuestionnaireAssetReviewInternalError()


def _tokenized_assets(
    package: SnapshotPackage,
    *,
    owner_ref: str,
    snapshot_id: str,
) -> tuple[dict[str, ResearchAsset], dict[str, str]]:
    by_token: dict[str, ResearchAsset] = {}
    by_id: dict[str, str] = {}
    token_identity: dict[str, str] = {}
    for asset in package.bundle.collection.assets:
        token = _asset_token(owner_ref, snapshot_id, asset.asset_id)
        prior_identity = token_identity.get(token)
        if prior_identity is not None and prior_identity != asset.asset_id:
            raise QuestionnaireAssetReviewInternalError()
        token_identity[token] = asset.asset_id
        by_token[token] = asset
        by_id[asset.asset_id] = token
    return by_token, by_id


def _bounded_normalized_text(
    value: object,
    fallback: str,
    *,
    max_chars: int,
) -> str:
    """Normalize one prefix without allocating intermediates for the full input."""

    source = value if type(value) is str else fallback
    for candidate in (source, fallback):
        result: list[str] = []
        pending_space = False
        scanned_chars = 0
        for character in candidate:
            if scanned_chars >= max_chars * _TEXT_SCAN_BUDGET_MULTIPLIER:
                break
            scanned_chars += 1
            if (
                character.isspace()
                or unicodedata.category(character).startswith("C")
            ):
                if result:
                    pending_space = True
                continue
            if pending_space:
                if len(result) + 1 >= max_chars:
                    break
                result.append(" ")
                pending_space = False
            result.append(character)
            if len(result) >= max_chars:
                break
        if result:
            return "".join(result)
    return fallback[:max_chars]


def _safe_text(value: object, fallback: str) -> str:
    return _bounded_normalized_text(value, fallback, max_chars=500)


def _question_label(question: CanonicalQuestion | None) -> str:
    if question is None:
        return "问卷题目"
    return _safe_text(question.title, "问卷题目")


def _nested_context_label(
    question: CanonicalQuestion,
    detail: object,
    detail_fallback: str,
) -> str:
    question_text = _bounded_normalized_text(
        question.title,
        "问卷题目",
        max_chars=_NESTED_CONTEXT_QUESTION_MAX_CHARS,
    )
    return _nested_context_label_from_title(
        question_text,
        detail,
        detail_fallback,
    )


def _nested_context_label_from_title(
    question_text: str,
    detail: object,
    detail_fallback: str,
) -> str:
    detail_text = _bounded_normalized_text(
        detail,
        detail_fallback,
        max_chars=_NESTED_CONTEXT_DETAIL_MAX_CHARS,
    )
    return "{} · {}".format(
        question_text,
        detail_text,
    )


_NestedContextKey = tuple[AssetContextType, str, str | None]


def _nested_context_labels(
    references: list[AssetReference],
    questions: dict[str, CanonicalQuestion],
) -> dict[_NestedContextKey, str]:
    required_options: dict[str, set[str | None]] = {}
    required_rows: dict[str, set[str | None]] = {}
    for reference in references:
        if reference.context_type == AssetContextType.SURVEY_OPTION:
            required_options.setdefault(reference.context_id, set()).add(
                reference.option_key
            )
        elif reference.context_type == AssetContextType.SURVEY_ROW:
            required_rows.setdefault(reference.context_id, set()).add(
                reference.row_key
            )

    result: dict[_NestedContextKey, str] = {}
    question_titles: dict[str, str] = {}

    def normalized_question_title(
        question_id: str,
        question: CanonicalQuestion,
    ) -> str:
        if question_id not in question_titles:
            question_titles[question_id] = _bounded_normalized_text(
                question.title,
                "问卷题目",
                max_chars=_NESTED_CONTEXT_QUESTION_MAX_CHARS,
            )
        return question_titles[question_id]

    for question_id, required_keys in required_options.items():
        question = questions.get(question_id)
        if question is None:
            for option_key in required_keys:
                result[(
                    AssetContextType.SURVEY_OPTION,
                    question_id,
                    option_key,
                )] = "问卷选项"
            continue
        option_details = {
            option.option_key: option.label or option.value
            for option in question.options
            if option.option_key in required_keys
        }
        for option_key in required_keys:
            result[(
                AssetContextType.SURVEY_OPTION,
                question_id,
                option_key,
            )] = _nested_context_label_from_title(
                normalized_question_title(question_id, question),
                option_details.get(option_key),
                "问卷选项",
            )

    for question_id, required_keys in required_rows.items():
        question = questions.get(question_id)
        if question is None:
            for row_key in required_keys:
                result[(
                    AssetContextType.SURVEY_ROW,
                    question_id,
                    row_key,
                )] = "问卷矩阵行"
            continue
        row_details = {
            row.row_key: row.label
            for row in question.rows
            if row.row_key in required_keys
        }
        for row_key in required_keys:
            result[(
                AssetContextType.SURVEY_ROW,
                question_id,
                row_key,
            )] = _nested_context_label_from_title(
                normalized_question_title(question_id, question),
                row_details.get(row_key),
                "问卷矩阵行",
            )
    return result


def _context_label(
    reference: AssetReference,
    questions: dict[str, CanonicalQuestion],
    nested_labels: dict[_NestedContextKey, str],
    question_labels: dict[str, str],
) -> str:
    context_type = reference.context_type
    if context_type == AssetContextType.SURVEY_QUESTION:
        if reference.context_id not in question_labels:
            question_labels[reference.context_id] = _question_label(
                questions.get(reference.context_id)
            )
        return question_labels[reference.context_id]
    if context_type == AssetContextType.SURVEY_OPTION:
        return nested_labels.get(
            (context_type, reference.context_id, reference.option_key),
            "问卷选项",
        )
    if context_type == AssetContextType.SURVEY_ROW:
        return nested_labels.get(
            (context_type, reference.context_id, reference.row_key),
            "问卷矩阵行",
        )
    if context_type == AssetContextType.RESEARCH_DOCUMENT:
        return "研究文档"
    if context_type == AssetContextType.INTERVIEW_POSITION:
        return "访谈位置"
    if context_type == AssetContextType.REPORT:
        return "报告素材"
    return "调研素材"


def _validated_warning_codes(
    warnings: object,
    *,
    initial: tuple[str, ...] = (),
) -> tuple[str, ...]:
    if (
        type(warnings) is not list
        or len(warnings) > _MAX_SOURCE_WARNINGS_PER_SUBJECT
    ):
        raise QuestionnaireAssetReviewInternalError()
    result = list(initial)
    seen = set(initial)
    for warning in warnings:
        code = warning.code
        if type(code) is not str or _WARNING_CODE_PATTERN.fullmatch(code) is None:
            raise QuestionnaireAssetReviewInternalError()
        if code not in seen:
            if len(result) >= 64:
                raise QuestionnaireAssetReviewInternalError()
            seen.add(code)
            result.append(code)
    return tuple(result)


def _warning_codes(
    reference: object,
    asset: ResearchAsset,
    asset_cache: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    if asset.asset_id not in asset_cache:
        asset_cache[asset.asset_id] = _validated_warning_codes(asset.warnings)
    return _validated_warning_codes(
        reference.warnings,
        initial=asset_cache[asset.asset_id],
    )


def _image_is_previewable(asset: ResearchAsset, media: dict[str, bytes]) -> bool:
    if asset.media_type != MediaType.IMAGE:
        return False
    if asset.access_status != AccessStatus.ACCESSIBLE:
        return False
    if asset.processing_status not in {
        ProcessingStatus.COMPLETED,
        ProcessingStatus.PARTIAL,
        ProcessingStatus.NEEDS_REVIEW,
    }:
        return False
    if asset.mime_type not in _SUPPORTED_IMAGE_MIME_TYPES:
        return False
    if type(asset.size_bytes) is not int or asset.content_hash is None:
        return False
    content = media.get(asset.content_hash)
    return type(content) is bytes and len(content) == asset.size_bytes


def _project_package(
    storage: ResearchSnapshotCatalogStorage,
    owner_ref: object,
    snapshot_id: object,
) -> QuestionnaireAssetReviewProjection:
    owner = _require_owner_ref(owner_ref)
    target_snapshot_id = _require_snapshot_id(snapshot_id)
    package = _load_validated_package(storage, owner, target_snapshot_id)
    _assets_by_token, asset_tokens_by_id = _tokenized_assets(
        package,
        owner_ref=owner,
        snapshot_id=target_snapshot_id,
    )
    assets_by_id = {
        asset.asset_id: asset for asset in package.bundle.collection.assets
    }
    questions = {
        question.question_id: question
        for question in package.bundle.snapshot.canonical_questions
    }
    nested_labels = _nested_context_labels(
        package.bundle.collection.references,
        questions,
    )

    reference_token_identity: dict[str, str] = {}
    asset_warning_codes: dict[str, tuple[str, ...]] = {}
    question_labels: dict[str, str] = {}
    items: list[QuestionnaireAssetReviewItem] = []
    for reference in package.bundle.collection.references:
        asset = assets_by_id.get(reference.asset_id)
        asset_token = asset_tokens_by_id.get(reference.asset_id)
        if asset is None or asset_token is None:
            raise QuestionnaireAssetReviewInternalError()
        opaque_reference_token = _reference_token(
            owner,
            target_snapshot_id,
            reference.reference_id,
        )
        prior_identity = reference_token_identity.get(opaque_reference_token)
        if prior_identity is not None and prior_identity != reference.reference_id:
            raise QuestionnaireAssetReviewInternalError()
        reference_token_identity[opaque_reference_token] = reference.reference_id
        preview_status = (
            QuestionnaireAssetPreviewStatus.AVAILABLE
            if _image_is_previewable(asset, package.media)
            else QuestionnaireAssetPreviewStatus.UNAVAILABLE
        )
        items.append(
            QuestionnaireAssetReviewItem(
                reference_token=opaque_reference_token,
                asset_token=asset_token,
                context_type=reference.context_type,
                context_label=_context_label(
                    reference,
                    questions,
                    nested_labels,
                    question_labels,
                ),
                role=reference.role,
                binding_status=reference.binding_status,
                binding_confidence=reference.binding_confidence,
                review_required=(
                    reference.binding_status
                    in {BindingStatus.PROPOSED, BindingStatus.NEEDS_REVIEW}
                ),
                media_type=asset.media_type,
                preview_status=preview_status,
                warning_codes=_warning_codes(
                    reference,
                    asset,
                    asset_warning_codes,
                ),
            )
        )

    return QuestionnaireAssetReviewProjection(
        total_references=len(items),
        review_required_references=sum(
            item.review_required for item in items
        ),
        items=tuple(items),
    )


def _thumbnail_from_package(
    storage: ResearchSnapshotCatalogStorage,
    owner_ref: object,
    snapshot_id: object,
    asset_token: object,
) -> QuestionnaireAssetThumbnailResult:
    owner = _require_owner_ref(owner_ref)
    target_snapshot_id = _require_snapshot_id(snapshot_id)
    target_token = _require_asset_token(asset_token)
    package = _load_validated_package(storage, owner, target_snapshot_id)
    assets_by_token, _asset_tokens_by_id = _tokenized_assets(
        package,
        owner_ref=owner,
        snapshot_id=target_snapshot_id,
    )
    asset = assets_by_token.get(target_token)
    if asset is None or not _image_is_previewable(asset, package.media):
        raise QuestionnaireAssetReviewNotFoundError()

    content_hash = asset.content_hash
    if content_hash is None:
        raise QuestionnaireAssetReviewNotFoundError()
    content = package.media.get(content_hash)
    if type(content) is not bytes:
        raise QuestionnaireAssetReviewInternalError()
    try:
        processed = preprocess_research_image(
            asset,
            content,
            created_at=package.bundle.snapshot.retrieved_at,
        )
        payload = processed.thumbnail_derivative.payload
        thumbnail_hash = payload.get("content_hash")
        if (
            type(thumbnail_hash) is not str
            or _TOKEN_PATTERN.fullmatch(thumbnail_hash) is None
            or payload.get("mime_type") != "image/png"
            or type(payload.get("size_bytes")) is not int
        ):
            raise QuestionnaireAssetReviewInternalError()
        thumbnail = processed.media.get(thumbnail_hash)
        if (
            type(thumbnail) is not bytes
            or not thumbnail
            or len(thumbnail) != payload["size_bytes"]
            or len(thumbnail) > MAX_THUMBNAIL_OUTPUT_BYTES
            or hashlib.sha256(thumbnail).hexdigest() != thumbnail_hash
        ):
            raise QuestionnaireAssetReviewInternalError()
    except QuestionnaireAssetReviewApiError:
        raise
    except ResearchImagePreprocessError:
        raise QuestionnaireAssetReviewInvalidError() from None
    except Exception:
        raise QuestionnaireAssetReviewInternalError() from None
    return QuestionnaireAssetThumbnailResult(
        content=thumbnail,
    )


_ResultT = TypeVar("_ResultT")


async def _drain_after_cancellation(task: asyncio.Task[_ResultT]) -> None:
    """Wait for a non-cancellable worker thread and consume its outcome."""

    current = asyncio.current_task()
    while not task.done():
        if current is not None and hasattr(current, "uncancel"):
            current.uncancel()
        try:
            await asyncio.wait((task,))
        except asyncio.CancelledError:
            continue
    try:
        if not task.cancelled():
            task.exception()
    except BaseException:
        pass


async def _run_in_thread_cancellation_safe(
    function: Callable[..., _ResultT],
    *args: object,
) -> _ResultT:
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        await asyncio.wait((task,))
        return task.result()
    except asyncio.CancelledError:
        await _drain_after_cancellation(task)
        raise


@dataclass(frozen=True, slots=True)
class QuestionnaireAssetReviewApi:
    """Load safe projections and deterministic image thumbnails by owner."""

    storage: ResearchSnapshotCatalogStorage

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotCatalogStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotCatalogStorage")

    async def get_projection(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> QuestionnaireAssetReviewProjection:
        try:
            return await _run_in_thread_cancellation_safe(
                _project_package,
                self.storage,
                owner_ref,
                snapshot_id,
            )
        except QuestionnaireAssetReviewApiError:
            raise
        except Exception:
            raise QuestionnaireAssetReviewInternalError() from None

    async def get_asset_thumbnail(
        self,
        owner_ref: str,
        snapshot_id: str,
        asset_token: str,
    ) -> QuestionnaireAssetThumbnailResult:
        try:
            return await _run_in_thread_cancellation_safe(
                _thumbnail_from_package,
                self.storage,
                owner_ref,
                snapshot_id,
                asset_token,
            )
        except QuestionnaireAssetReviewApiError:
            raise
        except Exception:
            raise QuestionnaireAssetReviewInternalError() from None
