"""Contracts for multi-variant questionnaire families and unified responses."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from app.schemas.questionnaire import CanonicalQuestionType
from app.schemas.research_assets import ContractModel


LanguageCode = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        to_lower=True,
        pattern=r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$",
        min_length=2,
        max_length=35,
    ),
]


class QuestionnaireFamilyStatus(str, Enum):
    READY = "ready"
    NEEDS_REVIEW = "needs_review"


class FamilyQuestionRole(str, Enum):
    CORE = "core"
    OPTIONAL_RESPONDENT_METADATA = "optional_respondent_metadata"


class FamilyDiagnosticSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class QuestionnaireFamilyVariantRequest(ContractModel):
    language: LanguageCode
    form_url: str = Field(strip_whitespace=True, min_length=1, max_length=2048)


class QuestionnaireFamilyCreateRequest(ContractModel):
    title: str = Field(strip_whitespace=True, min_length=1, max_length=200)
    variants: list[QuestionnaireFamilyVariantRequest] = Field(
        min_length=2,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_unique_variants(self) -> "QuestionnaireFamilyCreateRequest":
        urls = [item.form_url for item in self.variants]
        if len(urls) != len(set(urls)):
            raise ValueError("同一 Google Form 不能重复添加")
        languages = [item.language for item in self.variants]
        if len(languages) != len(set(languages)):
            raise ValueError("同一问卷家族的语言标记不能重复")
        return self


class QuestionnaireFamilyVariant(ContractModel):
    variant_id: str = Field(min_length=1, max_length=128)
    language: LanguageCode
    snapshot_id: str = Field(min_length=1, max_length=1024)
    provider_form_id: str = Field(min_length=1, max_length=256)
    question_count: int = Field(ge=0)


class FamilyOptionMapping(ContractModel):
    canonical_option_key: str = Field(min_length=1, max_length=128)
    provider_value: str = Field(min_length=1, max_length=100_000)


class FamilyRowMapping(ContractModel):
    canonical_row_key: str = Field(min_length=1, max_length=128)
    provider_question_id: str = Field(min_length=1, max_length=1024)
    provider_label: str = Field(min_length=1, max_length=100_000)


class FamilyVariantQuestionMapping(ContractModel):
    variant_id: str = Field(min_length=1, max_length=128)
    provider_question_id: str | None = Field(default=None, max_length=1024)
    mapping_confidence: float = Field(ge=0.0, le=1.0)
    option_mappings: list[FamilyOptionMapping] = Field(default_factory=list)
    row_mappings: list[FamilyRowMapping] = Field(default_factory=list)


class FamilyCanonicalOption(ContractModel):
    canonical_option_key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=100_000)


class FamilyCanonicalRow(ContractModel):
    canonical_row_key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=100_000)


class FamilyCanonicalQuestion(ContractModel):
    canonical_question_key: str = Field(min_length=1, max_length=128)
    canonical_type: CanonicalQuestionType
    role: FamilyQuestionRole
    title: str = Field(max_length=100_000)
    required: bool = False
    options: list[FamilyCanonicalOption] = Field(default_factory=list)
    rows: list[FamilyCanonicalRow] = Field(default_factory=list)
    variant_mappings: list[FamilyVariantQuestionMapping] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_mapping_identity(self) -> "FamilyCanonicalQuestion":
        variant_ids = [item.variant_id for item in self.variant_mappings]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("同一规范题不能重复映射同一 variant")
        option_keys = [item.canonical_option_key for item in self.options]
        if len(option_keys) != len(set(option_keys)):
            raise ValueError("规范选项 key 不能重复")
        row_keys = [item.canonical_row_key for item in self.rows]
        if len(row_keys) != len(set(row_keys)):
            raise ValueError("规范矩阵行 key 不能重复")
        return self


class QuestionnaireFamilyDiagnostic(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)
    severity: FamilyDiagnosticSeverity
    blocking: bool
    variant_id: str | None = Field(default=None, max_length=128)
    language: LanguageCode | None = None
    canonical_question_key: str | None = Field(default=None, max_length=128)
    question_title: str | None = Field(default=None, max_length=100_000)
    related_question_title: str | None = Field(default=None, max_length=100_000)
    affected_count: int = Field(default=1, ge=0)


class QuestionnaireFamily(ContractModel):
    schema_version: Literal[1] = 1
    family_id: str = Field(min_length=1, max_length=128)
    owner_ref: str = Field(min_length=1, max_length=1024)
    title: str = Field(min_length=1, max_length=200)
    created_at: datetime
    updated_at: datetime
    status: QuestionnaireFamilyStatus
    variants: list[QuestionnaireFamilyVariant] = Field(min_length=1, max_length=10)
    canonical_questions: list[FamilyCanonicalQuestion] = Field(default_factory=list)
    diagnostics: list[QuestionnaireFamilyDiagnostic] = Field(default_factory=list)
    mapping_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_family(self) -> "QuestionnaireFamily":
        variant_ids = [item.variant_id for item in self.variants]
        if len(variant_ids) != len(set(variant_ids)):
            raise ValueError("family variant_id 不能重复")
        form_ids = [item.provider_form_id for item in self.variants]
        if len(form_ids) != len(set(form_ids)):
            raise ValueError("同一 Google Form 不能属于多个 variant")
        languages = [item.language for item in self.variants]
        if len(languages) != len(set(languages)):
            raise ValueError("family language 不能重复")
        question_keys = [item.canonical_question_key for item in self.canonical_questions]
        if len(question_keys) != len(set(question_keys)):
            raise ValueError("canonical_question_key 不能重复")
        known_variants = set(variant_ids)
        if any(
            mapping.variant_id not in known_variants
            for question in self.canonical_questions
            for mapping in question.variant_mappings
        ):
            raise ValueError("规范题映射引用了未知 variant")
        blocking = any(item.blocking for item in self.diagnostics)
        if blocking != (self.status == QuestionnaireFamilyStatus.NEEDS_REVIEW):
            raise ValueError("family status 必须与 blocking diagnostics 一致")
        return self


class QuestionnaireFamilySummary(ContractModel):
    schema_version: Literal[1] = 1
    family_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    status: QuestionnaireFamilyStatus
    variant_count: int = Field(ge=1, le=10)
    languages: list[LanguageCode] = Field(min_length=1, max_length=10)
    canonical_question_count: int = Field(ge=0)
    blocking_issue_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    diagnostics: list[QuestionnaireFamilyDiagnostic] = Field(default_factory=list)


class UnifiedAnswerEvidence(ContractModel):
    canonical_question_key: str = Field(min_length=1, max_length=128)
    original_question_id: str = Field(min_length=1, max_length=1024)
    original_values: list[str] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)


class UnifiedResponseProvenance(ContractModel):
    row_index: int = Field(ge=1)
    language: LanguageCode
    variant_id: str = Field(min_length=1, max_length=128)
    provider_form_id: str = Field(min_length=1, max_length=256)
    response_id: str = Field(min_length=1, max_length=1024)
    create_time: str = Field(min_length=1, max_length=128)
    last_submitted_time: str = Field(min_length=1, max_length=128)
    respondent_email: str | None = Field(default=None, max_length=320)
    answers: list[UnifiedAnswerEvidence] = Field(default_factory=list)


class QuestionnaireFamilyAnalysisSessionResponse(ContractModel):
    session_id: str = Field(min_length=32, max_length=36)
    filename: str = Field(min_length=1, max_length=255)
    total_rows: int = Field(ge=1)
    headers: list[str]
    preview: list[list[str]]
    source_type: Literal["google"]
    questionnaire_used: Literal[True]
    matched_questions: int = Field(ge=1)
    questionnaire_family_id: str = Field(min_length=1, max_length=128)
    languages: list[LanguageCode] = Field(min_length=1, max_length=10)
    duplicate_response_count: int = Field(ge=0)
    unmatched_answer_count: int = Field(ge=0)
    file_upload_answer_count: int = Field(ge=0)


def family_summary(family: QuestionnaireFamily) -> QuestionnaireFamilySummary:
    return QuestionnaireFamilySummary(
        family_id=family.family_id,
        title=family.title,
        status=family.status,
        variant_count=len(family.variants),
        languages=[item.language for item in family.variants],
        canonical_question_count=len(family.canonical_questions),
        blocking_issue_count=sum(
            item.affected_count for item in family.diagnostics if item.blocking
        ),
        warning_count=sum(
            item.affected_count for item in family.diagnostics if not item.blocking
        ),
        diagnostics=family.diagnostics,
    )


__all__ = [
    "FamilyCanonicalOption",
    "FamilyCanonicalQuestion",
    "FamilyCanonicalRow",
    "FamilyDiagnosticSeverity",
    "FamilyOptionMapping",
    "FamilyQuestionRole",
    "FamilyRowMapping",
    "FamilyVariantQuestionMapping",
    "LanguageCode",
    "QuestionnaireFamily",
    "QuestionnaireFamilyAnalysisSessionResponse",
    "QuestionnaireFamilyCreateRequest",
    "QuestionnaireFamilyDiagnostic",
    "QuestionnaireFamilyStatus",
    "QuestionnaireFamilySummary",
    "QuestionnaireFamilyVariant",
    "QuestionnaireFamilyVariantRequest",
    "UnifiedAnswerEvidence",
    "UnifiedResponseProvenance",
    "family_summary",
]
