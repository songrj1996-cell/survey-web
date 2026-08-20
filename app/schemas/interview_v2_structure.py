"""Typed API contracts for interview V2 structure and evidence review."""

from __future__ import annotations

import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_MAPPING_REVISION_PATTERN = r"^mapping_[0-9a-f]{32}$"
_STRUCTURE_REVISION_PATTERN = r"^structure_[0-9a-f]{32}$"
_EVIDENCE_REVISION_PATTERN = r"^evidence_[0-9a-f]{32}$"
_ISSUE_PATTERN = r"^issue_[0-9a-f]{32}$"
_RESOURCE_PATTERN = (
    r"^(?:occ|module|question|ev|cell)_[0-9a-f]{32}$"
)
ResolutionAction = Literal[
    "assign_row_role",
    "assign_module",
    "assign_main_question",
    "set_evidence_identity",
    "exclude_evidence",
    "accept_suggestion",
]


class InterviewV2StructureRequestSchema(BaseModel):
    """Strict request base that rejects malformed Unicode recursively."""

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


class InterviewV2StructureResponseSchema(BaseModel):
    """Response base ignores internal persistence and audit-only fields."""

    model_config = ConfigDict(extra="ignore")


class InterviewV2StructureSourceResponse(InterviewV2StructureResponseSchema):
    project_id: str
    import_id: str
    workbook_revision_id: str
    base_snapshot_sha256: str
    mapping_revision_id: str
    mapping_sha256: str
    rules_version: str


class InterviewV2ModuleResponse(InterviewV2StructureResponseSchema):
    module_id: str
    canonical_name: str
    normalized_key: str
    raw_titles: list[str] = Field(default_factory=list)
    occurrence_ids: list[str] = Field(default_factory=list)
    mapping_method: str
    decision_status: str
    decision_source: str
    confidence: float
    confirmed_at: str | None = None


class InterviewV2MainQuestionResponse(InterviewV2StructureResponseSchema):
    main_question_id: str
    module_id: str
    canonical_text: str
    normalized_key: str
    raw_prompts: list[str] = Field(default_factory=list)
    occurrence_ids: list[str] = Field(default_factory=list)
    alignment_method: str
    decision_status: str
    decision_source: str
    confidence: float
    confirmed_at: str | None = None


class InterviewV2OccurrenceResponse(InterviewV2StructureResponseSchema):
    occurrence_id: str
    group_id: str
    sheet_id: str
    sheet_name: str = ""
    recorder_label: str = ""
    row: int
    row_role: str
    raw_module_text: str | None = None
    raw_type_text: str | None = None
    raw_prompt_text: str | None = None
    canonical_module_id: str | None = None
    canonical_main_question_id: str | None = None
    parent_main_occurrence_id: str | None = None
    mapping_method: str
    confidence: float
    decision_status: str
    decision_source: str
    confirmed_at: str | None = None
    has_participant_content: bool = False


class InterviewV2StructurePayloadResponse(InterviewV2StructureResponseSchema):
    structure_schema_version: str
    source: InterviewV2StructureSourceResponse
    modules: list[InterviewV2ModuleResponse] = Field(default_factory=list)
    main_questions: list[InterviewV2MainQuestionResponse] = Field(default_factory=list)
    occurrences: list[InterviewV2OccurrenceResponse] = Field(default_factory=list)


class InterviewV2EvidenceSummaryResponse(InterviewV2StructureResponseSchema):
    evidence_count: int = 0
    self_report_count: int = 0
    observation_count: int = 0
    needs_review_count: int = 0


class InterviewV2ReviewSummaryResponse(InterviewV2StructureResponseSchema):
    open_issue_count: int = 0
    blocking_issue_count: int = 0
    recommended_issue_count: int = 0


class InterviewV2AffectedIdsResponse(InterviewV2StructureResponseSchema):
    group_ids: list[str] = Field(default_factory=list)
    participant_ids: list[str] = Field(default_factory=list)
    sheet_ids: list[str] = Field(default_factory=list)
    occurrence_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    module_ids: list[str] = Field(default_factory=list)
    main_question_ids: list[str] = Field(default_factory=list)


class InterviewV2IssueSourceContextResponse(InterviewV2StructureResponseSchema):
    group_id: str | None = None
    sheet_id: str | None = None
    row: int | None = None


class InterviewV2SuggestedResolutionResponse(InterviewV2StructureResponseSchema):
    resolution: ResolutionAction | None = None
    target_id: str | None = None
    row_role: str | None = None
    evidence_type: str | None = None


class InterviewV2IssueResolutionResponse(InterviewV2StructureResponseSchema):
    action: str
    target_id: str | None = None
    row_role: str | None = None
    evidence_type: str | None = None
    comment: str = ""
    resolved_at: str | None = None


class InterviewV2ReviewIssueResponse(InterviewV2StructureResponseSchema):
    issue_id: str
    code: str
    severity: str
    status: str
    message: str
    affected_ids: InterviewV2AffectedIdsResponse = Field(
        default_factory=InterviewV2AffectedIdsResponse
    )
    source_context: InterviewV2IssueSourceContextResponse = Field(
        default_factory=InterviewV2IssueSourceContextResponse
    )
    suggested_action: str = ""
    allowed_resolutions: list[ResolutionAction] = Field(default_factory=list)
    reason: str = ""
    report_impact: str = ""
    suggested_resolution: InterviewV2SuggestedResolutionResponse = Field(
        default_factory=InterviewV2SuggestedResolutionResponse
    )
    resolution: InterviewV2IssueResolutionResponse | None = None


class InterviewV2EvidenceResponse(InterviewV2StructureResponseSchema):
    evidence_id: str
    participant_id: str
    participant_label: str = ""
    group_id: str
    recorder_label: str = ""
    module_id: str | None = None
    main_question_id: str | None = None
    occurrence_id: str
    evidence_type: str | None = None
    capture_context: str
    prompt_text: str | None = None
    raw_content: str
    display_content: str = ""
    normalized_content: str
    fragment_text_field: Literal["normalized_content"] = "normalized_content"
    fragment_start: int = Field(default=0, ge=0)
    fragment_end: int = Field(ge=0)
    source_cell_id: str
    sheet_id: str
    sheet_name: str = ""
    row: int
    column: int
    cell_address: str
    source_value_sha256: str
    formula_cache_status: Literal["not_applicable", "available", "unavailable"]
    inclusion_status: str
    identity_decision_status: str
    decision_source: str
    confidence: float
    confirmed_at: str | None = None

    @model_validator(mode="after")
    def _fragment_range_is_valid(self):
        if (
            self.fragment_start > self.fragment_end
            or self.fragment_end > len(self.normalized_content)
        ):
            raise ValueError("fragment range must be within normalized_content")
        return self


class InterviewV2EvidenceSourceContextResponse(InterviewV2StructureResponseSchema):
    source_cell_id: str | None = None
    sheet_id: str
    sheet_name: str = ""
    row: int
    column: int
    cell_address: str = ""
    neighboring_occurrences: list[InterviewV2OccurrenceResponse] = Field(
        default_factory=list
    )


class InterviewV2StructureBuildRequest(InterviewV2StructureRequestSchema):
    base_mapping_revision_id: str = Field(pattern=_MAPPING_REVISION_PATTERN)
    base_mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InterviewV2ReviewResolutionFields(InterviewV2StructureRequestSchema):
    resolution: ResolutionAction
    target_id: str | None = Field(default=None, pattern=_RESOURCE_PATTERN)
    row_role: Literal[
        "module_header",
        "main_question",
        "follow_up",
        "observation_row",
    ] | None = None
    evidence_type: Literal[
        "participant_self_report",
        "researcher_observation",
    ] | None = None
    comment: str = Field(min_length=1, max_length=500)

    @field_validator("comment")
    @classmethod
    def _comment_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("comment must not be blank")
        return unicodedata.normalize("NFC", value.strip())

    @model_validator(mode="after")
    def _require_action_fields(self):
        if self.resolution == "assign_row_role":
            if (
                self.row_role is None
                or self.row_role == "unknown"
                or self.evidence_type is not None
                or (
                    self.target_id is not None
                    and (
                        self.row_role not in {"follow_up", "observation_row"}
                        or not self.target_id.startswith("question_")
                    )
                )
            ):
                raise ValueError(
                    "assign_row_role requires row_role; only follow-up or observation may target a question"
                )
        elif self.resolution in {"assign_module", "assign_main_question"}:
            expected_prefix = (
                "module_" if self.resolution == "assign_module" else "question_"
            )
            if (
                self.target_id is None
                or not self.target_id.startswith(expected_prefix)
                or self.row_role is not None
                or self.evidence_type is not None
            ):
                raise ValueError(
                    f"{self.resolution} requires a matching target_id"
                )
        elif self.resolution == "set_evidence_identity":
            if (
                self.target_id is None
                or not self.target_id.startswith("ev_")
                or self.evidence_type is None
                or self.row_role is not None
            ):
                raise ValueError(
                    "set_evidence_identity requires an evidence target and type"
                )
        elif self.resolution == "exclude_evidence":
            if (
                self.target_id is None
                or not self.target_id.startswith("ev_")
                or self.row_role is not None
                or self.evidence_type is not None
            ):
                raise ValueError("exclude_evidence requires an evidence target")
        elif any(
            value is not None
            for value in (self.target_id, self.row_role, self.evidence_type)
        ):
            raise ValueError("accept_suggestion does not accept override fields")
        return self


class InterviewV2ReviewIssuePatchRequest(InterviewV2ReviewResolutionFields):
    base_structure_revision_id: str = Field(
        pattern=_STRUCTURE_REVISION_PATTERN
    )
    base_evidence_revision_id: str = Field(pattern=_EVIDENCE_REVISION_PATTERN)


class InterviewV2ReviewResolutionItem(InterviewV2ReviewResolutionFields):
    issue_id: str = Field(pattern=_ISSUE_PATTERN)


class InterviewV2ReviewIssueBatchRequest(InterviewV2StructureRequestSchema):
    base_structure_revision_id: str = Field(
        pattern=_STRUCTURE_REVISION_PATTERN
    )
    base_evidence_revision_id: str = Field(pattern=_EVIDENCE_REVISION_PATTERN)
    resolutions: list[InterviewV2ReviewResolutionItem] = Field(
        min_length=1,
        max_length=200,
    )

    @field_validator("resolutions")
    @classmethod
    def _issue_ids_are_unique(
        cls, value: list[InterviewV2ReviewResolutionItem]
    ) -> list[InterviewV2ReviewResolutionItem]:
        issue_ids = [item.issue_id for item in value]
        if len(issue_ids) != len(set(issue_ids)):
            raise ValueError("each review issue may be resolved at most once")
        return value


class InterviewV2StructureBuildResponse(InterviewV2StructureResponseSchema):
    import_id: str
    project_id: str
    status: str
    structure_revision_id: str
    evidence_revision_id: str
    structure: InterviewV2StructurePayloadResponse
    evidence_summary: InterviewV2EvidenceSummaryResponse = Field(
        default_factory=InterviewV2EvidenceSummaryResponse
    )
    review_summary: InterviewV2ReviewSummaryResponse = Field(
        default_factory=InterviewV2ReviewSummaryResponse
    )


class InterviewV2StructureCurrentResponse(InterviewV2StructureBuildResponse):
    pass


class InterviewV2ReviewIssuesResponse(InterviewV2StructureResponseSchema):
    import_id: str
    project_id: str
    status: str
    structure_revision_id: str
    evidence_revision_id: str
    issues: list[InterviewV2ReviewIssueResponse] = Field(default_factory=list)
    open_issue_count: int = 0
    blocking_issue_count: int = 0


class InterviewV2ReviewResolutionResponse(InterviewV2StructureResponseSchema):
    import_id: str
    project_id: str
    status: str
    structure_revision_id: str
    evidence_revision_id: str
    resolved_issue_ids: list[str] = Field(default_factory=list)
    manual_override_ids: list[str] = Field(default_factory=list)
    open_issue_count: int = 0
    blocking_issue_count: int = 0


class InterviewV2EvidenceContextResponse(InterviewV2StructureResponseSchema):
    evidence_id: str
    structure_revision_id: str
    evidence_revision_id: str
    evidence: InterviewV2EvidenceResponse
    occurrence: InterviewV2OccurrenceResponse
    source_context: InterviewV2EvidenceSourceContextResponse


__all__ = [
    "InterviewV2EvidenceContextResponse",
    "InterviewV2EvidenceResponse",
    "InterviewV2ReviewIssueBatchRequest",
    "InterviewV2ReviewIssuePatchRequest",
    "InterviewV2ReviewIssuesResponse",
    "InterviewV2ReviewIssueResponse",
    "InterviewV2ReviewResolutionItem",
    "InterviewV2ReviewResolutionResponse",
    "InterviewV2StructureBuildRequest",
    "InterviewV2StructureBuildResponse",
    "InterviewV2StructureCurrentResponse",
]
