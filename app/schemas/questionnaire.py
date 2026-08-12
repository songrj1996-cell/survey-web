"""Provider 原始问卷与平台统一问卷的数据契约。"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, JsonValue, model_validator

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
    MATRIX_SCALE = "matrix_scale"
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


class ProviderItemDefinition(ContractModel):
    """Provider 原始 Item；一个 Item 可以没有题目或包含多个题目。"""

    provider: Provider
    provider_item_id: str = Field(min_length=1)
    provider_item_type: str = Field(min_length=1)
    provider_position: int = Field(ge=0)
    provider_question_ids: list[str] = Field(default_factory=list)
    raw_definition: dict[str, JsonValue] = Field(default_factory=dict)
    source_locator: SourceLocator

    @model_validator(mode="after")
    def validate_provider_question_ids(self) -> "ProviderItemDefinition":
        if any(not question_id for question_id in self.provider_question_ids):
            raise ValueError("provider_question_ids 不能包含空 ID")
        if len(self.provider_question_ids) != len(set(self.provider_question_ids)):
            raise ValueError("provider_question_ids 不能包含重复 ID")
        return self


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

    @model_validator(mode="after")
    def validate_action_target(self) -> "BranchRule":
        if self.action == BranchAction.GO_TO_SECTION:
            if not self.target_section_id:
                raise ValueError("go_to_section 必须指定 target_section_id")
        elif self.target_section_id is not None:
            raise ValueError(
                f"{self.action.value} 不能指定 target_section_id"
            )
        return self


class CanonicalQuestion(ContractModel):
    question_id: str = Field(min_length=1)
    provider_question_id: str | None = None
    provider_item_id: str | None = Field(default=None, min_length=1)
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


class ResponseColumnBinding(ContractModel):
    """一个 Provider 回答键到一列回答数据的定位。"""

    provider_question_id: str = Field(min_length=1)
    row_key: str | None = Field(default=None, min_length=1)
    response_key: str | None = Field(default=None, min_length=1)
    column_index: int | None = Field(default=None, ge=0)
    column_header: str | None = Field(default=None, min_length=1)
    source_locator: SourceLocator | None = None

    @model_validator(mode="after")
    def validate_response_location(self) -> "ResponseColumnBinding":
        if self.response_key is None and self.column_index is None:
            raise ValueError(
                "ResponseColumnBinding 至少需要 response_key 或 column_index"
            )
        return self


class ResponseColumnMapping(ContractModel):
    question_id: str = Field(min_length=1)
    bindings: list[ResponseColumnBinding] = Field(default_factory=list)
    mapping_status: MappingStatus
    mapping_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[ImportWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_bindings_for_status(self) -> "ResponseColumnMapping":
        if (
            self.mapping_status
            in {
                MappingStatus.EXACT,
                MappingStatus.NORMALIZED,
                MappingStatus.PARTIAL,
            }
            and not self.bindings
        ):
            raise ValueError(
                f"{self.mapping_status.value} 映射必须至少包含一个 binding"
            )
        return self


class QuestionnaireSnapshot(ContractModel):
    schema_version: Literal[1] = 1
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
    item_count: int = Field(ge=0)
    question_count: int = Field(ge=0)
    asset_count: int = Field(ge=0)
    mapping_status: MappingStatus
    provider_raw_definition: dict[str, JsonValue] = Field(default_factory=dict)
    provider_items: list[ProviderItemDefinition] = Field(default_factory=list)
    canonical_questions: list[CanonicalQuestion] = Field(default_factory=list)
    response_column_mappings: list[ResponseColumnMapping] = Field(default_factory=list)
    warnings: list[ImportWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_item_and_question_counts(self) -> "QuestionnaireSnapshot":
        if self.item_count != len(self.provider_items):
            raise ValueError("item_count 必须等于 provider_items 的数量")
        counted_questions = sum(
            question.canonical_type
            not in {
                CanonicalQuestionType.SECTION,
                CanonicalQuestionType.STATIC_TEXT,
            }
            for question in self.canonical_questions
        )
        if self.question_count != counted_questions:
            raise ValueError(
                "question_count 必须等于非 SECTION/STATIC_TEXT 的 Canonical 题目数量"
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
