"""问卷快照 HTTP 适配所需的异步业务门面。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import re

from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_source_api import (
    MAX_QUESTIONNAIRE_SNAPSHOT_DISPLAY_TITLE_LENGTH,
    QuestionnaireSnapshotCatalogResponse,
    QuestionnaireSnapshotSummary,
)
from app.schemas.research_assets import MediaType, Provider
from app.services.questionnaire_source_service import (
    load_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES,
    ResearchAssetStorageError,
    ResearchSnapshotCatalogStorage,
    ResearchSnapshotStorage,
    SnapshotCatalogEntry,
    SnapshotCatalogPage,
    SnapshotConflictError,
    SnapshotPackage,
    SnapshotPackageError,
    _validated_bundle,
    _validated_media,
    build_snapshot_package,
    parse_snapshot_package,
)


MAX_SNAPSHOT_UPLOAD_BYTES = SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES
MAX_SNAPSHOT_CATALOG_LIMIT = 50
DEFAULT_SNAPSHOT_CATALOG_LIMIT = 20
_SNAPSHOT_CATALOG_CURSOR_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DISPLAY_TITLE_CONTROL_PATTERN = re.compile(
    r"[\x00-\x1f\x7f\u061c\u200b\u200e\u200f"
    r"\u202a-\u202e\u2060-\u206f\ufeff]+"
)


class QuestionnaireSnapshotApiError(RuntimeError):
    """问卷快照 API 可安全映射的业务错误基类。"""


class QuestionnaireSnapshotInvalidError(QuestionnaireSnapshotApiError):
    """上传内容不是当前用户可导入的有效快照包。"""


class QuestionnaireSnapshotConflictError(QuestionnaireSnapshotApiError):
    """同一快照 ID 已存在不同的不可变内容。"""


class QuestionnaireSnapshotNotFoundError(QuestionnaireSnapshotApiError):
    """当前用户范围内不存在目标快照。"""


class QuestionnaireSnapshotInternalError(QuestionnaireSnapshotApiError):
    """不应向 HTTP 响应暴露细节的内部失败。"""


class QuestionnaireSnapshotCatalogInvalidError(QuestionnaireSnapshotApiError):
    """快照目录查询参数不满足稳定公开合同。"""


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _snapshot_display_title(provider: Provider, title: object) -> str:
    """返回仅供 owner-scoped UI 识别 Google 问卷的安全短标题。"""
    if provider != Provider.GOOGLE_FORMS or not isinstance(title, str):
        return ""
    normalized = " ".join(
        _DISPLAY_TITLE_CONTROL_PATTERN.sub(" ", title).split()
    )
    return normalized[:MAX_QUESTIONNAIRE_SNAPSHOT_DISPLAY_TITLE_LENGTH]


def _summary(package: SnapshotPackage) -> QuestionnaireSnapshotSummary:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    return QuestionnaireSnapshotSummary(
        snapshot_id=snapshot.snapshot_id,
        display_title=_snapshot_display_title(
            snapshot.provider,
            snapshot.title,
        ),
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


def _validated_package_content(
    package: object,
    owner_ref: str,
    expected_snapshot_id: str | None = None,
) -> bytes:
    """对注入存储的输出失败关闭，并返回确定性完整包。"""
    if not isinstance(package, SnapshotPackage):
        raise QuestionnaireSnapshotInternalError()
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    if collection.owner_ref != owner_ref:
        raise QuestionnaireSnapshotInternalError()
    if (
        expected_snapshot_id is not None
        and snapshot.snapshot_id != expected_snapshot_id
    ):
        raise QuestionnaireSnapshotInternalError()
    try:
        return build_snapshot_package(
            owner_ref,
            package.bundle,
            package.media,
        )
    except Exception as error:
        raise QuestionnaireSnapshotInternalError() from error


def _validate_package_without_archive(
    package: object,
    owner_ref: str,
    expected_snapshot_id: str | None = None,
) -> SnapshotPackage:
    """完整校验聚合与媒体闭包，但不为摘要查询物化 ZIP。"""
    if not isinstance(package, SnapshotPackage):
        raise QuestionnaireSnapshotInternalError()
    if (
        expected_snapshot_id is not None
        and package.bundle.snapshot.snapshot_id != expected_snapshot_id
    ):
        raise QuestionnaireSnapshotInternalError()
    try:
        _validated_bundle(
            owner_ref,
            package.bundle,
            SnapshotPackageError,
        )
        _validated_media(package.bundle.collection, package.media)
    except Exception as error:
        raise QuestionnaireSnapshotInternalError() from error
    return package


def _validated_summary(
    package: object,
    owner_ref: str,
    expected_snapshot_id: str | None = None,
) -> QuestionnaireSnapshotSummary:
    """在线程内完整复核注入存储输出并构造安全摘要。"""
    validated = _validate_package_without_archive(
        package,
        owner_ref,
        expected_snapshot_id,
    )
    return _summary(validated)


def _list_snapshot_summaries(
    owner_ref: str,
    cursor: str | None,
    limit: int,
    storage: ResearchSnapshotCatalogStorage,
) -> QuestionnaireSnapshotCatalogResponse:
    page = storage.list_snapshot_catalog(
        owner_ref,
        cursor=cursor,
        limit=limit,
    )
    if not isinstance(page, SnapshotCatalogPage):
        raise QuestionnaireSnapshotInternalError()
    if (
        not isinstance(page.entries, tuple)
        or len(page.entries) > limit
    ):
        raise QuestionnaireSnapshotInternalError()
    if (
        page.next_cursor is not None
        and (
            not isinstance(page.next_cursor, str)
            or _SNAPSHOT_CATALOG_CURSOR_PATTERN.fullmatch(
                page.next_cursor
            ) is None
            or not page.entries
            or (cursor is not None and page.next_cursor <= cursor)
        )
    ):
        raise QuestionnaireSnapshotInternalError()

    items: list[QuestionnaireSnapshotSummary] = []
    storage_keys: list[str] = []
    snapshot_ids: list[str] = []
    for entry in page.entries:
        if not isinstance(entry, SnapshotCatalogEntry):
            raise QuestionnaireSnapshotInternalError()
        if (
            entry.owner_ref != owner_ref
            or not isinstance(entry.snapshot_id, str)
            or not entry.snapshot_id.strip()
            or entry.snapshot_id != entry.snapshot_id.strip()
            or not isinstance(entry.storage_key, str)
            or _SNAPSHOT_CATALOG_CURSOR_PATTERN.fullmatch(
                entry.storage_key
            ) is None
            or hashlib.sha256(entry.snapshot_id.encode("utf-8")).hexdigest()
            != entry.storage_key
            or not isinstance(entry.provider, Provider)
            or not isinstance(entry.source_mode, QuestionnaireSourceMode)
            or not isinstance(entry.collection_state, CollectionState)
            or not isinstance(entry.mapping_status, MappingStatus)
            or not isinstance(entry.title, str)
            or any(
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for value in (
                    entry.item_count,
                    entry.question_count,
                    entry.asset_count,
                    entry.image_asset_count,
                    entry.asset_reference_count,
                )
            )
        ):
            raise QuestionnaireSnapshotInternalError()
        storage_keys.append(entry.storage_key)
        snapshot_ids.append(entry.snapshot_id)
        try:
            items.append(QuestionnaireSnapshotSummary(
                snapshot_id=entry.snapshot_id,
                display_title=_snapshot_display_title(
                    entry.provider,
                    entry.title,
                ),
                provider=entry.provider,
                source_mode=entry.source_mode,
                collection_state=entry.collection_state,
                mapping_status=entry.mapping_status,
                item_count=entry.item_count,
                question_count=entry.question_count,
                asset_count=entry.asset_count,
                image_asset_count=entry.image_asset_count,
                asset_reference_count=entry.asset_reference_count,
            ))
        except Exception as error:
            raise QuestionnaireSnapshotInternalError() from error
    if (
        storage_keys != sorted(storage_keys)
        or len(storage_keys) != len(set(storage_keys))
        or len(snapshot_ids) != len(set(snapshot_ids))
        or (
            cursor is not None
            and storage_keys
            and storage_keys[0] <= cursor
        )
        or (
            page.next_cursor is not None
            and page.next_cursor != storage_keys[-1]
        )
    ):
        raise QuestionnaireSnapshotInternalError()
    try:
        return QuestionnaireSnapshotCatalogResponse(
            items=items,
            next_cursor=page.next_cursor,
        )
    except Exception as error:
        raise QuestionnaireSnapshotInternalError() from error


def _validated_package_bytes(
    package: object,
    owner_ref: str,
    expected_snapshot_id: str,
) -> tuple[QuestionnaireSnapshotSummary, bytes]:
    content = _validated_package_content(
        package,
        owner_ref,
        expected_snapshot_id,
    )
    assert isinstance(package, SnapshotPackage)
    return _summary(package), content


def _load_existing_package(
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
        raise QuestionnaireSnapshotInternalError() from error


def _resolve_existing_import(
    incoming_content: bytes,
    owner_ref: str,
    snapshot_id: str,
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary | None:
    existing = _load_existing_package(owner_ref, snapshot_id, storage)
    if existing is None:
        return None
    existing_summary, existing_content = _validated_package_bytes(
        existing,
        owner_ref,
        snapshot_id,
    )
    if existing_content == incoming_content:
        return existing_summary
    raise QuestionnaireSnapshotConflictError()


def _import_summary(
    owner_ref: str,
    archive: bytes,
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary:
    try:
        incoming = parse_snapshot_package(owner_ref, archive)
    except SnapshotPackageError as error:
        raise QuestionnaireSnapshotInvalidError() from error
    snapshot_id = incoming.bundle.snapshot.snapshot_id
    incoming_summary, incoming_content = _validated_package_bytes(
        incoming,
        owner_ref,
        snapshot_id,
    )

    existing_summary = _resolve_existing_import(
        incoming_content,
        owner_ref,
        snapshot_id,
        storage,
    )
    if existing_summary is not None:
        return existing_summary

    try:
        storage.save_snapshot_package(owner_ref, incoming)
    except SnapshotConflictError:
        raced_summary = _resolve_existing_import(
            incoming_content,
            owner_ref,
            snapshot_id,
            storage,
        )
        if raced_summary is None:
            raise QuestionnaireSnapshotInternalError()
        return raced_summary
    except Exception as error:
        raise QuestionnaireSnapshotInternalError() from error
    return incoming_summary


def _load_summary(
    owner_ref: str,
    snapshot_id: str,
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary | None:
    package = load_questionnaire_source_snapshot(
        owner_ref,
        snapshot_id,
        storage,
    )
    if package is None:
        return None
    return _validated_summary(package, owner_ref, snapshot_id)


def _load_export_content(
    owner_ref: str,
    snapshot_id: str,
    storage: ResearchSnapshotStorage,
) -> bytes | None:
    package = load_questionnaire_source_snapshot(
        owner_ref,
        snapshot_id,
        storage,
    )
    if package is None:
        return None
    return _validated_package_content(package, owner_ref, snapshot_id)


@dataclass(frozen=True, slots=True)
class QuestionnaireSnapshotApi:
    """通过注入的 owner-scoped 存储导入和查询完整问卷快照。"""

    storage: ResearchSnapshotStorage

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")

    async def import_snapshot(
        self,
        owner_ref: str,
        archive: bytes,
    ) -> QuestionnaireSnapshotSummary:
        owner = _require_owner(owner_ref)
        if not isinstance(archive, bytes) or not archive:
            raise QuestionnaireSnapshotInvalidError()
        try:
            summary = await asyncio.to_thread(
                _import_summary,
                owner,
                archive,
                self.storage,
            )
        except QuestionnaireSnapshotApiError:
            raise
        except Exception as error:
            raise QuestionnaireSnapshotInternalError() from error
        return summary

    async def list_snapshots(
        self,
        owner_ref: str,
        cursor: str | None = None,
        limit: int = DEFAULT_SNAPSHOT_CATALOG_LIMIT,
    ) -> QuestionnaireSnapshotCatalogResponse:
        owner = _require_owner(owner_ref)
        if (
            cursor is not None
            and (
                not isinstance(cursor, str)
                or _SNAPSHOT_CATALOG_CURSOR_PATTERN.fullmatch(cursor) is None
            )
        ):
            raise QuestionnaireSnapshotCatalogInvalidError()
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > MAX_SNAPSHOT_CATALOG_LIMIT
        ):
            raise QuestionnaireSnapshotCatalogInvalidError()
        if not isinstance(self.storage, ResearchSnapshotCatalogStorage):
            raise QuestionnaireSnapshotInternalError()
        try:
            return await asyncio.to_thread(
                _list_snapshot_summaries,
                owner,
                cursor,
                limit,
                self.storage,
            )
        except QuestionnaireSnapshotApiError:
            raise
        except Exception as error:
            raise QuestionnaireSnapshotInternalError() from error

    async def get_snapshot(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> QuestionnaireSnapshotSummary:
        owner = _require_owner(owner_ref)
        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id.strip()
            or snapshot_id != snapshot_id.strip()
        ):
            raise QuestionnaireSnapshotNotFoundError()
        try:
            summary = await asyncio.to_thread(
                _load_summary,
                owner,
                snapshot_id,
                self.storage,
            )
        except (ResearchAssetStorageError, ValueError, TypeError) as error:
            raise QuestionnaireSnapshotInternalError() from error
        except Exception as error:
            raise QuestionnaireSnapshotInternalError() from error
        if summary is None:
            raise QuestionnaireSnapshotNotFoundError()
        return summary

    async def export_snapshot(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> bytes:
        owner = _require_owner(owner_ref)
        if (
            not isinstance(snapshot_id, str)
            or not snapshot_id.strip()
            or snapshot_id != snapshot_id.strip()
        ):
            raise QuestionnaireSnapshotNotFoundError()
        try:
            content = await asyncio.to_thread(
                _load_export_content,
                owner,
                snapshot_id,
                self.storage,
            )
        except QuestionnaireSnapshotApiError:
            raise
        except Exception as error:
            raise QuestionnaireSnapshotInternalError() from error
        if content is None:
            raise QuestionnaireSnapshotNotFoundError()
        return content
