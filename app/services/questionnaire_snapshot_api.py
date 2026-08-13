"""问卷快照 HTTP 适配所需的异步业务门面。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.schemas.questionnaire_source_api import QuestionnaireSnapshotSummary
from app.schemas.research_assets import MediaType
from app.services.questionnaire_source_service import (
    load_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES,
    ResearchAssetStorageError,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
    SnapshotPackageError,
    _validated_bundle,
    _validated_media,
    build_snapshot_package,
    parse_snapshot_package,
)


MAX_SNAPSHOT_UPLOAD_BYTES = SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES


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


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _summary(package: SnapshotPackage) -> QuestionnaireSnapshotSummary:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    return QuestionnaireSnapshotSummary(
        snapshot_id=snapshot.snapshot_id,
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
