"""Typed API contracts for interview V2 analysis boundaries and coverage."""

from __future__ import annotations

import unicodedata
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_STRUCTURE_REVISION_PATTERN = r"^structure_[0-9a-f]{32}$"
_EVIDENCE_REVISION_PATTERN = r"^evidence_[0-9a-f]{32}$"
_BOUNDARY_REVISION_PATTERN = r"^boundary_[0-9a-f]{32}$"
_EVALUATION_PATTERN = r"^evaluation_[0-9a-f]{32}$"
_SCOPE_PATTERN = r"^scope_[0-9a-f]{32}$"
_LABEL_SCOPE_PATTERN = r"^label_scope_[0-9a-f]{32}$"

BoundaryDecisionStatus = Literal[
    "proposed", "draft", "needs_review", "confirmed", "superseded"
]
BoundaryDecisionSource = Literal[
    "deterministic_rule", "user_selection", "manual_override"
]


class InterviewV2AnalysisBoundaryRequestSchema(BaseModel):
    """Strict requests reject unknown fields and malformed Unicode recursively."""

    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _reject_invalid_unicode(cls, value: Any) -> Any:
        def validate(item: Any) -> None:
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        "text must contain valid Unicode scalar values"
                    ) from exc
            elif isinstance(item, dict):
                for key, child in item.items():
                    validate(key)
                    validate(child)
            elif isinstance(item, list):
                for child in item:
                    validate(child)

        validate(value)
        return value


class InterviewV2AnalysisBoundaryResponseSchema(BaseModel):
    """Responses intentionally ignore persistence and audit-only fields."""

    model_config = ConfigDict(extra="ignore")


def _normalize_required_text(value: str) -> str:
    value = unicodedata.normalize("NFC", value.strip())
    if not value:
        raise ValueError("text must not be blank")
    return value


class InterviewV2EvaluationObjectRequest(InterviewV2AnalysisBoundaryRequestSchema):
    evaluation_object_id: str = Field(pattern=_EVALUATION_PATTERN)
    module_id: str = Field(pattern=r"^module_[0-9a-f]{32}$")
    parent_evaluation_object_id: str | None = Field(
        default=None, pattern=_EVALUATION_PATTERN
    )
    object_type: Literal["concept", "variant"]
    display_name: str = Field(min_length=1, max_length=300)
    display_order: int = Field(ge=1, le=100_000)
    main_question_ids: list[str] = Field(min_length=1, max_length=10_000)
    occurrence_ids: list[str] = Field(min_length=1, max_length=100_000)
    supersedes_evaluation_object_ids: list[str] = Field(
        default_factory=list, max_length=10_000
    )
    decision_status: BoundaryDecisionStatus = "draft"
    decision_source: BoundaryDecisionSource = "user_selection"

    @field_validator("display_name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        return _normalize_required_text(value)

    @field_validator("main_question_ids")
    @classmethod
    def _question_ids_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"question_[0-9a-f]{32}", item)
            for item in value
        ):
            raise ValueError("main_question_ids must be unique question IDs")
        return value

    @field_validator("occurrence_ids")
    @classmethod
    def _occurrence_ids_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"occ_[0-9a-f]{32}", item)
            for item in value
        ):
            raise ValueError("occurrence_ids must be unique occurrence IDs")
        return value

    @field_validator("supersedes_evaluation_object_ids")
    @classmethod
    def _supersedes_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"evaluation_[0-9a-f]{32}", item)
            for item in value
        ):
            raise ValueError("supersedes IDs must be unique evaluation IDs")
        return value

    @model_validator(mode="after")
    def _hierarchy_shape(self):
        if self.object_type == "concept" and self.parent_evaluation_object_id:
            raise ValueError("concept must not have a parent")
        if self.object_type == "variant" and not self.parent_evaluation_object_id:
            raise ValueError("variant requires a parent")
        if self.evaluation_object_id in self.supersedes_evaluation_object_ids:
            raise ValueError("an evaluation object cannot supersede itself")
        return self


class InterviewV2SourceScopeRuleRequest(InterviewV2AnalysisBoundaryRequestSchema):
    source_scope_rule_id: str = Field(pattern=_SCOPE_PATTERN)
    group_id: str | None = Field(default=None, pattern=r"^group_[0-9a-f]{32}$")
    sheet_id: str = Field(min_length=1, max_length=200)
    start_row: int = Field(ge=1, le=1_048_576)
    end_row: int = Field(ge=1, le=1_048_576)
    scope_type: Literal["interview_body", "participant_background", "excluded"]
    allowed_split_rows: list[int] = Field(default_factory=list, max_length=100_000)
    display_order: int = Field(ge=1, le=100_000)
    decision_status: Literal["proposed", "draft", "needs_review", "confirmed"] = (
        "draft"
    )
    decision_source: BoundaryDecisionSource = "user_selection"

    @model_validator(mode="after")
    def _ordered_range(self):
        if self.end_row < self.start_row:
            raise ValueError("end_row must be greater than or equal to start_row")
        if any(
            isinstance(row, bool) or row < 1 or row > 1_048_576
            for row in self.allowed_split_rows
        ) or len(self.allowed_split_rows) != len(set(self.allowed_split_rows)):
            raise ValueError("allowed_split_rows must contain unique Excel row numbers")
        return self


class InterviewV2LabelScopeRuleRequest(InterviewV2AnalysisBoundaryRequestSchema):
    label_scope_rule_id: str = Field(pattern=_LABEL_SCOPE_PATTERN)
    label_key: str = Field(min_length=1, max_length=200)
    label_name: str = Field(min_length=1, max_length=300)
    scope_mode: Literal[
        "disabled",
        "all_analysis",
        "selected_modules",
        "selected_evaluation_objects",
    ]
    module_ids: list[str] = Field(default_factory=list, max_length=10_000)
    evaluation_object_ids: list[str] = Field(default_factory=list, max_length=10_000)
    decision_status: Literal["proposed", "draft", "needs_review", "confirmed"] = (
        "draft"
    )
    decision_source: BoundaryDecisionSource = "user_selection"

    @field_validator("label_key", "label_name")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        return _normalize_required_text(value)

    @field_validator("module_ids")
    @classmethod
    def _module_ids_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"module_[0-9a-f]{32}", item)
            for item in value
        ):
            raise ValueError("module_ids must be unique module IDs")
        return value

    @field_validator("evaluation_object_ids")
    @classmethod
    def _object_ids_valid(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            not re.fullmatch(r"evaluation_[0-9a-f]{32}", item)
            for item in value
        ):
            raise ValueError("evaluation_object_ids must be unique evaluation IDs")
        return value

    @model_validator(mode="after")
    def _scope_shape(self):
        valid = (
            (
                self.scope_mode in {"disabled", "all_analysis"}
                and not self.module_ids
                and not self.evaluation_object_ids
            )
            or (
                self.scope_mode == "selected_modules"
                and bool(self.module_ids)
                and not self.evaluation_object_ids
            )
            or (
                self.scope_mode == "selected_evaluation_objects"
                and bool(self.evaluation_object_ids)
                and not self.module_ids
            )
        )
        if not valid:
            raise ValueError("scope_mode and target IDs are inconsistent")
        return self


class InterviewV2AnalysisBoundaryPutRequest(InterviewV2AnalysisBoundaryRequestSchema):
    base_boundary_revision_id: str | None = Field(
        default=None, pattern=_BOUNDARY_REVISION_PATTERN
    )
    base_coverage_revision_id: str | None = Field(
        default=None, pattern=r"^coverage_[0-9a-f]{32}$"
    )
    base_structure_revision_id: str = Field(pattern=_STRUCTURE_REVISION_PATTERN)
    base_evidence_revision_id: str = Field(pattern=_EVIDENCE_REVISION_PATTERN)
    evaluation_objects: list[InterviewV2EvaluationObjectRequest] = Field(
        default_factory=list, max_length=10_000
    )
    source_scope_rules: list[InterviewV2SourceScopeRuleRequest] = Field(
        default_factory=list, max_length=100_000
    )
    label_scope_rules: list[InterviewV2LabelScopeRuleRequest] = Field(
        default_factory=list, max_length=10_000
    )
    change_reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _paired_boundary_and_coverage_heads(self):
        if (self.base_boundary_revision_id is None) != (
            self.base_coverage_revision_id is None
        ):
            raise ValueError(
                "base boundary and coverage revision IDs must be supplied together"
            )
        return self


class InterviewV2AnalysisBoundaryConfirmRequest(
    InterviewV2AnalysisBoundaryRequestSchema
):
    boundary_revision_id: str = Field(pattern=_BOUNDARY_REVISION_PATTERN)
    coverage_revision_id: str = Field(pattern=r"^coverage_[0-9a-f]{32}$")
    boundary_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    coverage_payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InterviewV2AnalysisBoundarySourceResponse(
    InterviewV2AnalysisBoundaryResponseSchema
):
    project_id: str
    import_id: str
    structure_revision_id: str
    evidence_revision_id: str
    rules_version: str


class InterviewV2EvaluationObjectResponse(InterviewV2AnalysisBoundaryResponseSchema):
    evaluation_object_id: str
    module_id: str
    parent_evaluation_object_id: str | None = None
    object_type: str
    display_name: str
    display_order: int
    main_question_ids: list[str] = Field(default_factory=list)
    occurrence_ids: list[str] = Field(default_factory=list)
    supersedes_evaluation_object_ids: list[str] = Field(default_factory=list)
    decision_status: str
    decision_source: str


class InterviewV2SourceScopeRuleResponse(InterviewV2AnalysisBoundaryResponseSchema):
    source_scope_rule_id: str
    group_id: str | None = None
    sheet_id: str
    start_row: int
    end_row: int
    scope_type: str
    allowed_split_rows: list[int] = Field(default_factory=list)
    display_order: int
    decision_status: str
    decision_source: str


class InterviewV2LabelScopeRuleResponse(InterviewV2AnalysisBoundaryResponseSchema):
    label_scope_rule_id: str
    label_key: str
    label_name: str
    scope_mode: str
    module_ids: list[str] = Field(default_factory=list)
    evaluation_object_ids: list[str] = Field(default_factory=list)
    decision_status: str
    decision_source: str


class InterviewV2AnalysisBoundaryPayloadResponse(
    InterviewV2AnalysisBoundaryResponseSchema
):
    analysis_boundary_schema_version: str
    source: InterviewV2AnalysisBoundarySourceResponse
    status: str
    evaluation_objects: list[InterviewV2EvaluationObjectResponse] = Field(
        default_factory=list
    )
    source_scope_rules: list[InterviewV2SourceScopeRuleResponse] = Field(
        default_factory=list
    )
    label_scope_rules: list[InterviewV2LabelScopeRuleResponse] = Field(
        default_factory=list
    )


class InterviewV2CoverageRowResponse(InterviewV2AnalysisBoundaryResponseSchema):
    coverage_id: str
    participant_id: str
    group_id: str
    evaluation_object_id: str
    module_id: str
    main_question_id: str
    source_presence: str
    asked_status: str
    applicability: str
    self_report_count: int = 0
    follow_up_count: int = 0
    observation_count: int = 0
    review_status: str
    derived_status: str
    source_occurrence_ids: list[str] = Field(default_factory=list)
    self_report_evidence_ids: list[str] = Field(default_factory=list)
    observation_evidence_ids: list[str] = Field(default_factory=list)


class InterviewV2CoverageSummaryResponse(InterviewV2AnalysisBoundaryResponseSchema):
    module_id: str
    evaluation_object_id: str
    main_question_id: str
    participant_count: int = 0
    covered_participant_count: int = 0
    observation_only_participant_count: int = 0
    no_record_participant_count: int = 0
    denominator_reliable: bool = False
    denominator_participant_count: int | None = None
    proportion: float | None = None


class InterviewV2CoveragePreviewPayloadResponse(
    InterviewV2AnalysisBoundaryResponseSchema
):
    coverage_schema_version: str
    source: dict[str, Any] = Field(default_factory=dict)
    participant_count: int = 0
    row_count: int = 0
    rows: list[InterviewV2CoverageRowResponse] = Field(default_factory=list)
    summaries: list[InterviewV2CoverageSummaryResponse] = Field(default_factory=list)


class InterviewV2AnalysisBoundaryResponse(InterviewV2AnalysisBoundaryResponseSchema):
    import_id: str
    project_id: str
    status: str
    structure_revision_id: str | None = None
    evidence_revision_id: str | None = None
    boundary_revision_id: str | None = None
    boundary_revision_number: int | None = None
    boundary_payload_sha256: str | None = None
    analysis_boundary: InterviewV2AnalysisBoundaryPayloadResponse
    coverage_revision_id: str | None = None
    coverage_revision_number: int | None = None
    coverage_payload_sha256: str | None = None
    coverage_preview: InterviewV2CoveragePreviewPayloadResponse | None = None
    confirmation_ready: bool = False
    open_issue_count: int = 0
    blocking_issue_count: int = 0
    is_stale: bool = False


class InterviewV2CoveragePreviewResponse(InterviewV2AnalysisBoundaryResponseSchema):
    import_id: str
    project_id: str
    status: str
    structure_revision_id: str | None = None
    evidence_revision_id: str | None = None
    boundary_revision_id: str | None = None
    boundary_payload_sha256: str | None = None
    coverage_revision_id: str | None = None
    coverage_payload_sha256: str | None = None
    coverage_preview: InterviewV2CoveragePreviewPayloadResponse
    is_stale: bool = False


__all__ = [
    "InterviewV2AnalysisBoundaryConfirmRequest",
    "InterviewV2AnalysisBoundaryPayloadResponse",
    "InterviewV2AnalysisBoundaryPutRequest",
    "InterviewV2AnalysisBoundaryResponse",
    "InterviewV2CoveragePreviewResponse",
    "InterviewV2EvaluationObjectRequest",
    "InterviewV2LabelScopeRuleRequest",
    "InterviewV2SourceScopeRuleRequest",
]
