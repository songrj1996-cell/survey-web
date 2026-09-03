"""Google Forms read-only capture to an owner-scoped immutable snapshot."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import Field

from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormsConnectorError,
    GoogleFormsErrorCode,
)
from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSourceMode,
)
from app.schemas.research_assets import ContractModel, MediaType, Provider
from app.services.google_forms_questionnaire_mapping import (
    QuestionnaireMappingResult,
    map_google_form_capture,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
)


_FORM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


class QuestionnaireSnapshotSummary(ContractModel):
    """Safe projection used while composing a family; never exposes provider data."""

    schema_version: int = 1
    snapshot_id: str = Field(min_length=1)
    display_title: str = Field(default="", max_length=200)
    provider: Provider
    source_mode: QuestionnaireSourceMode
    collection_state: CollectionState
    mapping_status: MappingStatus
    item_count: int = Field(ge=0)
    question_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    image_asset_count: int = Field(ge=0)
    asset_reference_count: int = Field(ge=0)


def _capture_identity_hash(capture: GoogleFormCapture) -> str:
    digest = hashlib.sha256(capture.form_id.encode("utf-8"))
    for image in sorted(capture.images, key=lambda item: repr(item.json_path)):
        digest.update(repr(image.json_path).encode("utf-8"))
        digest.update(image.sha256.encode("ascii"))
    for failure in sorted(capture.image_failures, key=lambda item: repr(item.json_path)):
        digest.update(repr(failure.json_path).encode("utf-8"))
        digest.update(failure.code.value.encode("ascii"))
        digest.update(str(failure.retryable).encode("ascii"))
        digest.update(str(failure.status_code).encode("ascii"))
    return digest.hexdigest()


def _versioned_mapping(
    mapped: QuestionnaireMappingResult,
    capture: GoogleFormCapture,
) -> QuestionnaireMappingResult:
    snapshot_id = (
        f"{mapped.bundle.snapshot.snapshot_id}_"
        f"{_capture_identity_hash(capture)[:16]}"
    )
    snapshot = mapped.bundle.snapshot.model_copy(update={"snapshot_id": snapshot_id})
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
    async def fetch_form(self, owner_ref: str, form_id: str) -> GoogleFormCapture: ...


class GoogleFormsQuestionnaireSnapshotApiError(RuntimeError):
    pass


class GoogleFormsQuestionnaireInvalidError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnaireAuthRequiredError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnairePermissionError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnaireNotFoundError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnaireRetryableError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnaireProviderError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnaireConflictError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


class GoogleFormsQuestionnaireInternalError(GoogleFormsQuestionnaireSnapshotApiError):
    pass


def _summary(package: SnapshotPackage) -> QuestionnaireSnapshotSummary:
    snapshot = package.bundle.snapshot
    collection = package.bundle.collection
    title = " ".join(str(snapshot.title or "").split())[:200]
    return QuestionnaireSnapshotSummary(
        snapshot_id=snapshot.snapshot_id,
        display_title=title,
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
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError
        if value.utcoffset() is None:
            raise ValueError
        return value
    except Exception as error:
        raise GoogleFormsQuestionnaireInternalError() from error


def _map_capture(
    capture: GoogleFormCapture,
    *,
    owner_ref: str,
    retrieved_at: datetime,
) -> QuestionnaireMappingResult:
    try:
        return _versioned_mapping(
            map_google_form_capture(
                capture,
                owner_ref=owner_ref,
                retrieved_at=retrieved_at,
            ),
            capture,
        )
    except (TypeError, ValueError) as error:
        raise GoogleFormsQuestionnaireProviderError() from error
    except Exception as error:
        raise GoogleFormsQuestionnaireInternalError() from error


def _persist_capture(
    owner_ref: str,
    capture: GoogleFormCapture,
    retrieved_at: datetime,
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary:
    mapped = _map_capture(capture, owner_ref=owner_ref, retrieved_at=retrieved_at)
    package = SnapshotPackage(mapped.bundle, dict(mapped.media))
    snapshot_id = package.bundle.snapshot.snapshot_id
    try:
        existing = storage.load_snapshot_package(owner_ref, snapshot_id)
        if existing is not None:
            remapped = _map_capture(
                capture,
                owner_ref=owner_ref,
                retrieved_at=existing.bundle.snapshot.retrieved_at,
            )
            if SnapshotPackage(remapped.bundle, dict(remapped.media)) != existing:
                raise GoogleFormsQuestionnaireConflictError()
            return _summary(existing)
        storage.save_snapshot_package(owner_ref, package)
    except SnapshotConflictError:
        raced = storage.load_snapshot_package(owner_ref, snapshot_id)
        if raced is None:
            raise GoogleFormsQuestionnaireInternalError()
        remapped = _map_capture(
            capture,
            owner_ref=owner_ref,
            retrieved_at=raced.bundle.snapshot.retrieved_at,
        )
        if SnapshotPackage(remapped.bundle, dict(remapped.media)) != raced:
            raise GoogleFormsQuestionnaireConflictError()
        return _summary(raced)
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


@dataclass(frozen=True, slots=True)
class GoogleFormsQuestionnaireSnapshotApi:
    client: GoogleFormsCaptureClient
    storage: ResearchSnapshotStorage
    clock: Callable[[], datetime] = field(
        default=lambda: datetime.now(timezone.utc), repr=False
    )

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
        try:
            return await asyncio.to_thread(
                _persist_capture,
                owner,
                capture,
                _retrieved_at(self.clock),
                self.storage,
            )
        except GoogleFormsQuestionnaireSnapshotApiError:
            raise
        except Exception as error:
            raise GoogleFormsQuestionnaireInternalError() from error


__all__ = [
    "GoogleFormsQuestionnaireAuthRequiredError",
    "GoogleFormsQuestionnaireConflictError",
    "GoogleFormsQuestionnaireInternalError",
    "GoogleFormsQuestionnaireInvalidError",
    "GoogleFormsQuestionnaireNotFoundError",
    "GoogleFormsQuestionnairePermissionError",
    "GoogleFormsQuestionnaireProviderError",
    "GoogleFormsQuestionnaireRetryableError",
    "GoogleFormsQuestionnaireSnapshotApi",
    "GoogleFormsQuestionnaireSnapshotApiError",
    "QuestionnaireSnapshotSummary",
]
