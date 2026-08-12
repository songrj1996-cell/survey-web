"""Provider 原始问卷与平台统一问卷的数据契约。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import Field, model_validator

from app.schemas.research_assets import (
    ContractModel,
    ImportWarning,
    Provider,
    SourceLocator,
)


class QuestionnaireSourceMode(str, Enum):
    OFFICIAL_API = "official_api"
    AUTHORIZED_EDIT = "authorized_edit"
    PUBLISHED_PAGE = "published_page"
    ORIGINAL_QUESTIONNAIRE_UPLOAD = "original_questionnaire_upload"
    MATERIAL_UPLOAD = "material_upload"
    PLATFORM_SNAPSHOT = "platform_snapshot"
    RESPONSE_EXPORT_FALLBACK = "response_export_fallback"


class CollectionState(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"


class MappingStatus(str, Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    UNSUPPORTED = "unsupported"
    SOURCE_MISSING = "source_missing"


class CanonicalQuestionType(str, Enum):
    SINGLE_CHOICE = "single_choice"
    MULTI_CHOICE = "multi_choice"
    DROPDOWN = "dropdown"
    OPEN_TEXT = "open_text"
    SCALE = "scale"
    RATING = "rating"
    MATRIX_SINGLE = "matrix_single"
    MATRIX_MULTI = "matrix_multi"
    DATE = "date"
    TIME = "time"
    FILE_UPLOAD = "file_upload"
    SECTION = "section"
    STATIC_TEXT = "static_text"
    UNKNOWN = "unknown"


class BranchAction(str, Enum):
    NEXT_SECTION = "next_section"
    GO_TO_SECTION = "go_to_section"
    RESTART_FORM = "restart_form"
    SUBMIT_FORM = "submit_form"


class ProviderQuestionDefinition(ContractModel):
    provider: Provider
    provider_question_id: str = Field(min_length=1)
    provider_item_id: str | None = None
    provider_question_type: str = Field(min_length=1)
    provider_position: int = Field(ge=0)
    raw_definition: dict[str, Any] = Field(default_factory=dict)
    source_locator: SourceLocator


class CanonicalOption(ContractModel):
    option_key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    label: str | None = None
    asset_reference_ids: list[str] = Field(default_factory=list)
    provider_option_id: str | None = None


class CanonicalRow(ContractModel):
    row_key: str = Field(min_length=1)
    label: str = Field(min_length=1)
    provider_question_id: str | None = None
    asset_reference_ids: list[str] = Field(default_factory=list)


class BranchRule(ContractModel):
    option_key: str | None = None
    action: BranchAction
    target_section_id: str | None = None
    provider_raw_action: str | None = None


class CanonicalQuestion(ContractModel):
    question_id: str = Field(min_length=1)
    provider_question_id: str | None = None
    canonical_type: CanonicalQuestionType
    title: str = ""
    description: str = ""
    required: bool = False
    rows: list[CanonicalRow] = Field(default_factory=list)
    options: list[CanonicalOption] = Field(default_factory=list)
    branching: list[BranchRule] = Field(default_factory=list)
    asset_reference_ids: list[str] = Field(default_factory=list)
    mapping_status: MappingStatus
    mapping_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[ImportWarning] = Field(default_factory=list)


class ResponseColumnMapping(ContractModel):
    question_id: str = Field(min_length=1)
    provider_question_id: str | None = None
    response_key: str | None = None
    column_indexes: list[int] = Field(default_factory=list)
    column_headers: list[str] = Field(default_factory=list)
    mapping_status: MappingStatus
    mapping_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[ImportWarning] = Field(default_factory=list)


class QuestionnaireSnapshot(ContractModel):
    schema_version: int = Field(default=1, ge=1)
    snapshot_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    provider: Provider
    provider_form_id: str | None = None
    source_mode: QuestionnaireSourceMode
    title: str = ""
    retrieved_at: datetime
    provider_modified_at: datetime | None = None
    provider_revision: str | None = None
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    collection_state: CollectionState = CollectionState.UNKNOWN
    question_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    mapping_status: MappingStatus
    provider_raw_definition: dict[str, Any] = Field(default_factory=dict)
    provider_questions: list[ProviderQuestionDefinition] = Field(default_factory=list)
    canonical_questions: list[CanonicalQuestion] = Field(default_factory=list)
    response_column_mappings: list[ResponseColumnMapping] = Field(default_factory=list)
    warnings: list[ImportWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_question_count(self) -> "QuestionnaireSnapshot":
        if self.question_count != len(self.canonical_questions):
            raise ValueError(
                "question_count 必须等于 canonical_questions 的数量"
            )
        return self

    @property
    def asset_reference_count(self) -> int:
        reference_ids: set[str] = set()
        for question in self.canonical_questions:
            reference_ids.update(question.asset_reference_ids)
            for row in question.rows:
                reference_ids.update(row.asset_reference_ids)
            for option in question.options:
                reference_ids.update(option.asset_reference_ids)
        return len(reference_ids)
