"""Immutable internal contracts for questionnaire asset review decisions.

The sidecar deliberately stores only opaque tokens and snapshot identity.  Raw
owner, provider locator, asset metadata, and reviewer free text do not belong in
this contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


QUESTIONNAIRE_ASSET_REVIEW_STATE_SCHEMA_VERSION = 1
MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS = 10_000

_Sha256 = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]
_PositivePackageSize = Annotated[
    int,
    Field(strict=True, ge=1, le=128 * 1024 * 1024),
]
_Revision = Annotated[
    int,
    Field(strict=True, ge=0, le=MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS),
]


class _FrozenStrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QuestionnaireAssetReviewDecision(str, Enum):
    """A durable override; ``RESET`` clears the override during projection."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    RESET = "reset"


class QuestionnaireAssetReviewCommand(_FrozenStrictModel):
    """One optimistic append request bound to an immutable snapshot package."""

    expected_revision: _Revision
    idempotency_key: _Sha256
    reference_token: _Sha256
    asset_token: _Sha256
    decision: QuestionnaireAssetReviewDecision
    reviewer_token: _Sha256
    base_package_sha256: _Sha256
    base_package_size_bytes: _PositivePackageSize


class QuestionnaireAssetReviewEvent(_FrozenStrictModel):
    """One hash-chained review decision persisted in the sidecar."""

    revision: Annotated[
        int,
        Field(strict=True, ge=1, le=MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS),
    ]
    idempotency_key: _Sha256
    reference_token: _Sha256
    asset_token: _Sha256
    decision: QuestionnaireAssetReviewDecision
    reviewer_token: _Sha256
    recorded_at: datetime
    command_sha256: _Sha256
    previous_event_sha256: _Sha256
    event_sha256: _Sha256

    @field_validator("recorded_at")
    @classmethod
    def require_utc_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("recorded_at 必须包含 UTC 时区")
        if value.utcoffset() != timedelta(0):
            raise ValueError("recorded_at 必须使用 UTC 时区")
        return value


class QuestionnaireAssetReviewState(_FrozenStrictModel):
    """Current immutable sidecar envelope and its complete append-only log."""

    schema_version: Literal[1]
    owner_scope_key: _Sha256
    snapshot_storage_key: _Sha256
    base_package_sha256: _Sha256
    base_package_size_bytes: _PositivePackageSize
    revision: _Revision
    head_event_sha256: _Sha256 | None
    events: tuple[QuestionnaireAssetReviewEvent, ...] = Field(
        max_length=MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS,
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_exact_schema_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version 必须是整数 1")
        return value

    @model_validator(mode="after")
    def validate_log_shape(self) -> "QuestionnaireAssetReviewState":
        if self.revision != len(self.events):
            raise ValueError("revision 与 events 数量不一致")
        if not self.events:
            if self.head_event_sha256 is not None:
                raise ValueError("空日志的 head_event_sha256 必须为空")
            return self
        if self.head_event_sha256 != self.events[-1].event_sha256:
            raise ValueError("head_event_sha256 与末尾事件不一致")
        expected_revisions = tuple(range(1, len(self.events) + 1))
        if tuple(event.revision for event in self.events) != expected_revisions:
            raise ValueError("事件 revision 必须连续递增")
        keys = tuple(event.idempotency_key for event in self.events)
        if len(keys) != len(set(keys)):
            raise ValueError("idempotency_key 不能重复")
        return self
