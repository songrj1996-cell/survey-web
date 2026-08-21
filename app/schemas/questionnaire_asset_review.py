"""Safe public DTOs for owner-scoped questionnaire asset review."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.research_assets import (
    AssetContextType,
    AssetRole,
    BindingStatus,
    ContractModel,
    MediaType,
)
from app.schemas.questionnaire_asset_review_state import (
    MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS,
)


MAX_QUESTIONNAIRE_ASSET_THUMBNAIL_BYTES = 16 * 1024 * 1024

QuestionnaireAssetToken = Annotated[
    str,
    StringConstraints(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]
QuestionnaireAssetWarningCode = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9_.-]{0,127}$",
    ),
]


class _FrozenReviewDto(ContractModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class QuestionnaireAssetPreviewStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class QuestionnaireAssetReviewDecision(str, Enum):
    """One public review command; reset restores the snapshot binding."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    RESET = "reset"


class QuestionnaireAssetActiveReviewDecision(str, Enum):
    """An effective persisted override; reset is represented by ``None``."""

    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class QuestionnaireAssetReviewDecisionRequest(_FrozenReviewDto):
    """Strict optimistic command using only owner-bound opaque tokens."""

    schema_version: Literal[1]
    expected_revision: int = Field(
        ge=0,
        le=MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS,
    )
    idempotency_key: QuestionnaireAssetToken
    base_version_token: QuestionnaireAssetToken
    reference_token: QuestionnaireAssetToken
    asset_token: QuestionnaireAssetToken
    decision: QuestionnaireAssetReviewDecision

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version_type(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version 必须是整数 1")
        return value

    @field_validator("expected_revision", mode="before")
    @classmethod
    def validate_expected_revision_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("expected_revision 必须是整数")
        return value

    @field_validator("decision", mode="before")
    @classmethod
    def validate_decision_type(cls, value: object) -> object:
        if isinstance(value, QuestionnaireAssetReviewDecision):
            return value
        if type(value) is str:
            try:
                return QuestionnaireAssetReviewDecision(value)
            except ValueError:
                pass
        raise ValueError("decision 必须是支持的审阅决定")


class QuestionnaireAssetReviewItem(_FrozenReviewDto):
    """One safe reference projection with opaque local review tokens."""

    reference_token: QuestionnaireAssetToken
    asset_token: QuestionnaireAssetToken
    context_type: AssetContextType
    context_label: str = Field(min_length=1, max_length=500)
    role: AssetRole
    binding_status: BindingStatus
    active_review_decision: QuestionnaireAssetActiveReviewDecision | None
    binding_confidence: float = Field(ge=0.0, le=1.0)
    review_required: bool
    media_type: MediaType
    preview_status: QuestionnaireAssetPreviewStatus
    warning_codes: tuple[QuestionnaireAssetWarningCode, ...] = Field(
        default_factory=tuple,
        max_length=64,
    )

    @field_validator("binding_confidence", mode="before")
    @classmethod
    def validate_confidence_type(cls, value: object) -> object:
        if type(value) not in {int, float}:
            raise ValueError("binding_confidence 必须是数字")
        return value

    @field_validator("review_required", mode="before")
    @classmethod
    def validate_review_required_type(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("review_required 必须是布尔值")
        return value

    @model_validator(mode="after")
    def validate_safe_projection(self) -> "QuestionnaireAssetReviewItem":
        if (
            self.active_review_decision
            == QuestionnaireAssetActiveReviewDecision.CONFIRMED
            and self.binding_status != BindingStatus.CONFIRMED
        ):
            raise ValueError("confirmed 审阅决定与 binding_status 不一致")
        if (
            self.active_review_decision
            == QuestionnaireAssetActiveReviewDecision.REJECTED
            and self.binding_status != BindingStatus.REJECTED
        ):
            raise ValueError("rejected 审阅决定与 binding_status 不一致")
        if self.review_required != (
            self.binding_status
            in {BindingStatus.PROPOSED, BindingStatus.NEEDS_REVIEW}
        ):
            raise ValueError("review_required 与 binding_status 不一致")
        if (
            self.preview_status == QuestionnaireAssetPreviewStatus.AVAILABLE
            and self.media_type != MediaType.IMAGE
        ):
            raise ValueError("只有 IMAGE 素材可以提供预览")
        if len(self.warning_codes) != len(set(self.warning_codes)):
            raise ValueError("warning_codes 不能重复")
        return self


class QuestionnaireAssetReviewProjection(_FrozenReviewDto):
    """Review-safe projection that excludes raw provider and storage fields."""

    schema_version: Literal[1] = 1
    review_revision: int = Field(
        ge=0,
        le=MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS,
    )
    base_version_token: QuestionnaireAssetToken
    total_references: int = Field(ge=0, le=2000)
    review_required_references: int = Field(ge=0, le=2000)
    items: tuple[QuestionnaireAssetReviewItem, ...] = Field(max_length=2000)

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version_type(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version 必须是整数 1")
        return value

    @field_validator(
        "review_revision",
        "total_references",
        "review_required_references",
        mode="before",
    )
    @classmethod
    def validate_count_type(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("引用计数必须是整数")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> "QuestionnaireAssetReviewProjection":
        if self.total_references != len(self.items):
            raise ValueError("total_references 与 items 数量不一致")
        expected_review_required = sum(
            item.review_required for item in self.items
        )
        if self.review_required_references != expected_review_required:
            raise ValueError("review_required_references 与 items 不一致")
        return self


class QuestionnaireAssetThumbnailResult(_FrozenReviewDto):
    """Internal raw-response DTO; ``content`` must never be JSON-serialized."""

    media_type: Literal["image/png"] = "image/png"
    content: bytes = Field(
        min_length=1,
        max_length=MAX_QUESTIONNAIRE_ASSET_THUMBNAIL_BYTES,
    )
