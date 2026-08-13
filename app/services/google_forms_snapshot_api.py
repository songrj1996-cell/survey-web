"""Google Forms 授权读取到完整媒体快照的异步业务门面。"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormsConnectorError,
    GoogleFormsErrorCode,
)
from app.schemas.questionnaire import MappingStatus
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceResult,
    questionnaire_source_priority,
)
from app.schemas.research_assets import (
    ImportWarning,
    ProcessingStatus,
    Provider,
    SourceKind,
)
from app.services.questionnaire_mapping import (
    QuestionnaireMappingResult,
    map_google_form_capture,
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
    ResearchAssetBundle,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
)


_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def _capture_identity_hash(capture: GoogleFormCapture) -> str:
    digest = hashlib.sha256()
    digest.update(capture.form_id.encode("utf-8"))
    for image in sorted(capture.images, key=lambda item: repr(item.json_path)):
        digest.update(repr(image.json_path).encode("utf-8"))
        digest.update(image.sha256.encode("ascii"))
    for failure in sorted(
        capture.image_failures,
        key=lambda item: repr(item.json_path),
    ):
        digest.update(repr(failure.json_path).encode("utf-8"))
        digest.update(failure.code.value.encode("ascii"))
        digest.update(str(failure.retryable).encode("ascii"))
        digest.update(str(failure.status_code).encode("ascii"))
    return digest.hexdigest()


def _versioned_snapshot_id(base_snapshot_id: str, capture: GoogleFormCapture) -> str:
    return f"{base_snapshot_id}_{_capture_identity_hash(capture)[:16]}"


def _versioned_mapping(
    mapped: QuestionnaireMappingResult,
    capture: GoogleFormCapture,
) -> QuestionnaireMappingResult:
    snapshot_id = _versioned_snapshot_id(
        mapped.bundle.snapshot.snapshot_id,
        capture,
    )
    snapshot = mapped.bundle.snapshot.model_copy(update={
        "snapshot_id": snapshot_id,
    })
    owner = mapped.bundle.collection.owner_ref
    collection_digest = hashlib.sha256(
        f"{owner}:{snapshot_id}".encode("utf-8")
    ).hexdigest()
    collection = mapped.bundle.collection.model_copy(update={
        "collection_id": f"rac_{collection_digest[:24]}",
    })
    return QuestionnaireMappingResult(
        bundle=ResearchAssetBundle(snapshot, collection),
        media=dict(mapped.media),
    )


@runtime_checkable
class GoogleFormsCaptureClient(Protocol):
    """只读获取一个 Google Forms 定义及其即时图片的最小端口。"""

    async def fetch_form(
        self,
        owner_ref: str,
        form_id: str,
    ) -> GoogleFormCapture:
        ...


class GoogleFormsQuestionnaireSnapshotApiError(RuntimeError):
    """可由 HTTP 层安全分类的 Google Forms 导入错误基类。"""


class GoogleFormsQuestionnaireInvalidError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """问卷 ID 或调用输入无效。"""


class GoogleFormsQuestionnaireAuthRequiredError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """Google 授权缺失、失效或无法准备。"""


class GoogleFormsQuestionnairePermissionError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """当前授权无权读取目标问卷。"""


class GoogleFormsQuestionnaireNotFoundError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """目标 Google Forms 问卷不存在。"""


class GoogleFormsQuestionnaireRetryableError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """上游暂时失败，调用方可安全重试。"""


class GoogleFormsQuestionnaireProviderError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """上游返回了无法安全映射的数据。"""


class GoogleFormsQuestionnaireConflictError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """同一不可变快照身份已对应不同内容。"""


class GoogleFormsQuestionnaireInternalError(
    GoogleFormsQuestionnaireSnapshotApiError
):
    """不得向外暴露细节的本地配置或持久化失败。"""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    return owner_ref.strip()


def _require_form_id(form_id: str) -> str:
    if not isinstance(form_id, str):
        raise GoogleFormsQuestionnaireInvalidError()
    normalized = form_id.strip()
    if not _FORM_ID_RE.fullmatch(normalized):
        raise GoogleFormsQuestionnaireInvalidError()
    return normalized


def _retrieved_at(clock: Callable[[], datetime]) -> datetime:
    try:
        value = clock()
    except Exception as error:
        raise GoogleFormsQuestionnaireInternalError() from error
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise GoogleFormsQuestionnaireInternalError()
    try:
        if value.utcoffset() is None:
            raise GoogleFormsQuestionnaireInternalError()
    except (OverflowError, ValueError) as error:
        raise GoogleFormsQuestionnaireInternalError() from error
    return value


def _map_capture(
    capture: GoogleFormCapture,
    *,
    owner_ref: str,
    retrieved_at: datetime,
) -> QuestionnaireMappingResult:
    try:
        mapped = map_google_form_capture(
            capture,
            owner_ref=owner_ref,
            retrieved_at=retrieved_at,
        )
        return _versioned_mapping(mapped, capture)
    except (TypeError, ValueError) as error:
        raise GoogleFormsQuestionnaireProviderError() from error
    except Exception as error:
        raise GoogleFormsQuestionnaireInternalError() from error


def _primary_source_id(mapped: QuestionnaireMappingResult) -> str:
    matches = [
        source.source_id
        for source in mapped.bundle.collection.sources
        if source.provider == Provider.GOOGLE_FORMS
        and source.source_kind == SourceKind.PROVIDER_CONNECTION
    ]
    if len(matches) != 1:
        raise GoogleFormsQuestionnaireInternalError()
    return matches[0]


def _attempt_warnings(mapped: QuestionnaireMappingResult) -> list[ImportWarning]:
    snapshot = mapped.bundle.snapshot
    warnings: list[ImportWarning] = []
    seen: set[str] = set()
    groups = [
        snapshot.warnings,
        *(question.warnings for question in snapshot.canonical_questions),
        *(mapping.warnings for mapping in snapshot.response_column_mappings),
    ]
    for group in groups:
        for warning in group:
            identity = warning.model_dump_json()
            if identity in seen:
                continue
            seen.add(identity)
            warnings.append(warning)
    return warnings


def _source_result(mapped: QuestionnaireMappingResult) -> QuestionnaireSourceResult:
    snapshot = mapped.bundle.snapshot
    warnings = _attempt_warnings(mapped)
    partial = snapshot.mapping_status != MappingStatus.EXACT or bool(warnings)
    needs_review = any(warning.blocking for warning in warnings) or (
        snapshot.mapping_status
        in {
            MappingStatus.NEEDS_REVIEW,
            MappingStatus.UNSUPPORTED,
            MappingStatus.SOURCE_MISSING,
        }
    )
    status = (
        ProcessingStatus.NEEDS_REVIEW
        if needs_review
        else ProcessingStatus.PARTIAL
        if partial
        else ProcessingStatus.COMPLETED
    )
    source_id = _primary_source_id(mapped)
    attempt = QuestionnaireSourceAttempt(
        source_id=source_id,
        source_mode=snapshot.source_mode,
        priority=questionnaire_source_priority(snapshot.source_mode),
        acquisition_route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
        status=status,
        snapshot_id=snapshot.snapshot_id,
        warnings=warnings,
    )
    return QuestionnaireSourceResult(
        snapshot=snapshot,
        collection=mapped.bundle.collection,
        selected_source_ids=[source_id],
        attempts=[attempt],
        partial_success=partial,
    )


def _package_from_mapping(mapped: QuestionnaireMappingResult) -> SnapshotPackage:
    return SnapshotPackage(mapped.bundle, dict(mapped.media))


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
        raise GoogleFormsQuestionnaireInternalError() from error


def _validated_existing_summary(
    existing: object,
    *,
    owner_ref: str,
    capture: GoogleFormCapture,
    expected_snapshot_id: str,
) -> QuestionnaireSnapshotSummary:
    if not isinstance(existing, SnapshotPackage):
        raise GoogleFormsQuestionnaireInternalError()
    try:
        _validate_package_without_archive(
            existing,
            owner_ref,
            expected_snapshot_id,
        )
    except Exception as error:
        raise GoogleFormsQuestionnaireInternalError() from error
    remapped = _map_capture(
        capture,
        owner_ref=owner_ref,
        retrieved_at=existing.bundle.snapshot.retrieved_at,
    )
    if _package_from_mapping(remapped) != existing:
        raise GoogleFormsQuestionnaireConflictError()
    return _summary(existing)


def _persist_capture(
    owner_ref: str,
    capture: GoogleFormCapture,
    retrieved_at: datetime,
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary:
    mapped = _map_capture(
        capture,
        owner_ref=owner_ref,
        retrieved_at=retrieved_at,
    )
    snapshot_id = mapped.bundle.snapshot.snapshot_id
    existing = _load_existing(owner_ref, snapshot_id, storage)
    if existing is not None:
        return _validated_existing_summary(
            existing,
            owner_ref=owner_ref,
            capture=capture,
            expected_snapshot_id=snapshot_id,
        )
    try:
        package = save_questionnaire_source_snapshot(
            _source_result(mapped),
            mapped.media,
            storage,
        )
    except SnapshotConflictError:
        raced = _load_existing(owner_ref, snapshot_id, storage)
        if raced is None:
            raise GoogleFormsQuestionnaireInternalError()
        return _validated_existing_summary(
            raced,
            owner_ref=owner_ref,
            capture=capture,
            expected_snapshot_id=snapshot_id,
        )
    except GoogleFormsQuestionnaireSnapshotApiError:
        raise
    except Exception as error:
        raise GoogleFormsQuestionnaireInternalError() from error
    return _summary(package)


def _translate_connector_error(
    error: GoogleFormsConnectorError,
) -> GoogleFormsQuestionnaireSnapshotApiError:
    if error.code == GoogleFormsErrorCode.INVALID_FORM_ID:
        return GoogleFormsQuestionnaireInvalidError()
    if error.code in {
        GoogleFormsErrorCode.AUTHORIZATION_FAILED,
        GoogleFormsErrorCode.AUTHENTICATION_REQUIRED,
    }:
        return GoogleFormsQuestionnaireAuthRequiredError()
    if error.code == GoogleFormsErrorCode.PERMISSION_DENIED:
        return GoogleFormsQuestionnairePermissionError()
    if error.code == GoogleFormsErrorCode.FORM_NOT_FOUND:
        return GoogleFormsQuestionnaireNotFoundError()
    if error.retryable or error.code in {
        GoogleFormsErrorCode.RATE_LIMITED,
        GoogleFormsErrorCode.PROVIDER_UNAVAILABLE,
        GoogleFormsErrorCode.TRANSPORT_ERROR,
    }:
        return GoogleFormsQuestionnaireRetryableError()
    if error.code == GoogleFormsErrorCode.INVALID_CONFIGURATION:
        return GoogleFormsQuestionnaireInternalError()
    return GoogleFormsQuestionnaireProviderError()


async def _await_uncancelled(task: asyncio.Task[QuestionnaireSnapshotSummary]) -> None:
    """外层取消后等待线程持久化结束，再把取消传播给调用方。"""
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
class GoogleFormsQuestionnaireSnapshotApi:
    """授权获取、规范化并原子保存一个 owner-scoped Google Form。"""

    client: GoogleFormsCaptureClient
    storage: ResearchSnapshotStorage
    clock: Callable[[], datetime] = field(default=_utc_now, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.client, GoogleFormsCaptureClient):
            raise TypeError("client 必须实现 GoogleFormsCaptureClient")
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")
        if not callable(self.clock):
            raise TypeError("clock 必须可调用")

    async def import_questionnaire(
        self,
        owner_ref: str,
        form_id: str,
    ) -> QuestionnaireSnapshotSummary:
        owner = _require_owner(owner_ref)
        normalized_form_id = _require_form_id(form_id)
        try:
            capture = await self.client.fetch_form(owner, normalized_form_id)
        except GoogleFormsConnectorError as error:
            raise _translate_connector_error(error) from error
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise GoogleFormsQuestionnaireInternalError() from error
        if not isinstance(capture, GoogleFormCapture):
            raise GoogleFormsQuestionnaireProviderError()

        retrieved_at = _retrieved_at(self.clock)
        persist_task = asyncio.create_task(asyncio.to_thread(
            _persist_capture,
            owner,
            capture,
            retrieved_at,
            self.storage,
        ))
        try:
            return await asyncio.shield(persist_task)
        except asyncio.CancelledError:
            await _await_uncancelled(persist_task)
            raise
        except GoogleFormsQuestionnaireSnapshotApiError:
            raise
        except Exception as error:
            raise GoogleFormsQuestionnaireInternalError() from error
