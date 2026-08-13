"""倍市得原问卷上传到完整媒体快照的异步业务门面。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import PurePosixPath

from app.integrations.bested_questionnaire_client import (
    BestedQuestionnaireParseResult,
    _XLSX_MAX_ARCHIVE_BYTES,
    parse_bested_questionnaire_upload,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceResult,
    questionnaire_source_priority,
)
from app.schemas.research_assets import (
    ProcessingStatus,
    Provider,
    SourceKind,
)
from app.services.questionnaire_mapping import (
    QuestionnaireMappingResult,
    map_bested_questionnaire_upload,
)
from app.services.questionnaire_snapshot_api import (
    QuestionnaireSnapshotSummary,
    _summary,
    _validate_package_without_archive,
)
from app.services.questionnaire_source_service import (
    load_questionnaire_source_snapshot,
    save_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
)


MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES = _XLSX_MAX_ARCHIVE_BYTES


class BestedQuestionnaireSnapshotApiError(RuntimeError):
    """倍市得问卷 API 可安全映射的业务错误基类。"""


class BestedQuestionnaireInvalidError(BestedQuestionnaireSnapshotApiError):
    """上传内容不是受支持且安全的倍市得原问卷。"""


class BestedQuestionnaireConflictError(BestedQuestionnaireSnapshotApiError):
    """同一不可变快照身份已对应不同内容。"""


class BestedQuestionnaireInternalError(BestedQuestionnaireSnapshotApiError):
    """不应向 HTTP 响应暴露细节的内部失败。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _safe_upload_name(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise BestedQuestionnaireInvalidError()
    normalized = filename.strip().replace("\\", "/")
    if PurePosixPath(normalized).suffix.casefold() != ".xlsx":
        raise BestedQuestionnaireInvalidError()
    return normalized


def _retrieved_at(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise BestedQuestionnaireInternalError() from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BestedQuestionnaireInternalError()
    try:
        if value.utcoffset() is None:
            raise BestedQuestionnaireInternalError()
    except (OverflowError, ValueError) as error:
        raise BestedQuestionnaireInternalError() from error
    return value


def _map_upload(
    parsed: BestedQuestionnaireParseResult,
    *,
    owner_ref: str,
    filename: str,
    content: bytes,
    retrieved_at: datetime,
) -> QuestionnaireMappingResult:
    try:
        return map_bested_questionnaire_upload(
            parsed,
            owner_ref=owner_ref,
            filename=filename,
            questionnaire_content=content,
            retrieved_at=retrieved_at,
        )
    except (TypeError, ValueError) as error:
        raise BestedQuestionnaireInvalidError() from error
    except Exception as error:
        raise BestedQuestionnaireInternalError() from error


def _primary_source_id(mapped: QuestionnaireMappingResult) -> str:
    matches = [
        source.source_id
        for source in mapped.bundle.collection.sources
        if source.provider == Provider.BESTED
        and source.source_kind == SourceKind.LOCAL_UPLOAD
    ]
    if len(matches) != 1:
        raise BestedQuestionnaireInternalError()
    return matches[0]


def _package_from_mapping(mapped: QuestionnaireMappingResult) -> SnapshotPackage:
    return SnapshotPackage(mapped.bundle, dict(mapped.media))


def _source_result(mapped: QuestionnaireMappingResult) -> QuestionnaireSourceResult:
    snapshot = mapped.bundle.snapshot
    collection = mapped.bundle.collection
    source_id = _primary_source_id(mapped)
    attempt = QuestionnaireSourceAttempt(
        source_id=source_id,
        source_mode=snapshot.source_mode,
        priority=questionnaire_source_priority(snapshot.source_mode),
        acquisition_route=(
            QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
        ),
        status=ProcessingStatus.NEEDS_REVIEW,
        snapshot_id=snapshot.snapshot_id,
        warnings=list(snapshot.warnings),
    )
    return QuestionnaireSourceResult(
        snapshot=snapshot,
        collection=collection,
        selected_source_ids=[source_id],
        attempts=[attempt],
        partial_success=True,
    )


def _validated_existing_summary(
    existing: object,
    *,
    owner_ref: str,
    filename: str,
    content: bytes,
    parsed: BestedQuestionnaireParseResult,
    expected_snapshot_id: str,
) -> QuestionnaireSnapshotSummary:
    if not isinstance(existing, SnapshotPackage):
        raise BestedQuestionnaireInternalError()
    try:
        _validate_package_without_archive(
            existing,
            owner_ref,
            expected_snapshot_id,
        )
    except Exception as error:
        raise BestedQuestionnaireInternalError() from error

    remapped = _map_upload(
        parsed,
        owner_ref=owner_ref,
        filename=filename,
        content=content,
        retrieved_at=existing.bundle.snapshot.retrieved_at,
    )
    if _package_from_mapping(remapped) != existing:
        raise BestedQuestionnaireConflictError()
    return _summary(existing)


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
        raise BestedQuestionnaireInternalError() from error


def _import_questionnaire(
    owner_ref: str,
    filename: str,
    content: bytes,
    clock: Callable[[], datetime],
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary:
    retrieved_at = _retrieved_at(clock)
    try:
        parsed = parse_bested_questionnaire_upload(filename, content)
    except (TypeError, ValueError) as error:
        raise BestedQuestionnaireInvalidError() from error
    except Exception as error:
        raise BestedQuestionnaireInternalError() from error

    mapped = _map_upload(
        parsed,
        owner_ref=owner_ref,
        filename=filename,
        content=content,
        retrieved_at=retrieved_at,
    )
    snapshot_id = mapped.bundle.snapshot.snapshot_id
    existing = _load_existing(owner_ref, snapshot_id, storage)
    if existing is not None:
        return _validated_existing_summary(
            existing,
            owner_ref=owner_ref,
            filename=filename,
            content=content,
            parsed=parsed,
            expected_snapshot_id=snapshot_id,
        )

    result = _source_result(mapped)
    try:
        package = save_questionnaire_source_snapshot(
            result,
            mapped.media,
            storage,
        )
    except SnapshotConflictError:
        raced = _load_existing(owner_ref, snapshot_id, storage)
        if raced is None:
            raise BestedQuestionnaireInternalError()
        return _validated_existing_summary(
            raced,
            owner_ref=owner_ref,
            filename=filename,
            content=content,
            parsed=parsed,
            expected_snapshot_id=snapshot_id,
        )
    except BestedQuestionnaireSnapshotApiError:
        raise
    except Exception as error:
        raise BestedQuestionnaireInternalError() from error
    return _summary(package)


@dataclass(frozen=True, slots=True)
class BestedQuestionnaireSnapshotApi:
    """解析、规范化并原子保存一个 owner-scoped 倍市得原问卷。"""

    storage: ResearchSnapshotStorage
    clock: Callable[[], datetime] = field(default=_utc_now, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")
        if not callable(self.clock):
            raise TypeError("clock 必须可调用")

    async def import_questionnaire(
        self,
        owner_ref: str,
        filename: str,
        content: bytes,
    ) -> QuestionnaireSnapshotSummary:
        owner = _require_owner(owner_ref)
        safe_name = _safe_upload_name(filename)
        if not isinstance(content, bytes) or not content:
            raise BestedQuestionnaireInvalidError()
        if len(content) > MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES:
            raise BestedQuestionnaireInvalidError()
        try:
            return await asyncio.to_thread(
                _import_questionnaire,
                owner,
                safe_name,
                content,
                self.clock,
                self.storage,
            )
        except BestedQuestionnaireSnapshotApiError:
            raise
        except Exception as error:
            raise BestedQuestionnaireInternalError() from error
