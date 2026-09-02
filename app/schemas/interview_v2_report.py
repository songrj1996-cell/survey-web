"""HTTP contracts for evidence-bound V2 report versions and review actions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


_REPORT_VERSION_PATTERN = r"^report_[0-9a-f]{32}$"
_REAUDIT_JOB_PATTERN = r"^job_[0-9a-f]{32}$"


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InterviewV2ReportCreateRequest(_StrictRequest):
    base_report_version_id: str | None = Field(
        default=None,
        pattern=_REPORT_VERSION_PATTERN,
    )
    freeze_current: Literal[True] = True


class InterviewV2ReportSectionPatchRequest(_StrictRequest):
    base_section_revision: int = Field(strict=True, ge=1)
    content: str = Field(min_length=1, max_length=30000)
    locked: Literal[True] = True
    edit_reason: str | None = Field(default=None, max_length=500)

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value

    @field_validator("locked", mode="before")
    @classmethod
    def _locked_must_be_true_boolean(cls, value: Any) -> Any:
        if value is not True:
            raise ValueError("locked must be the boolean true")
        return value


class InterviewV2ReportSectionReauditRequest(_StrictRequest):
    base_section_revision: int = Field(strict=True, ge=1)
    reaudit_job_id: str = Field(pattern=_REAUDIT_JOB_PATTERN)


class InterviewV2ReportApproveRequest(_StrictRequest):
    base_report_version_id: str = Field(pattern=_REPORT_VERSION_PATTERN)
    decision: Literal["approved"]
    note: str | None = Field(default=None, max_length=500)


class InterviewV2ReportResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    report_version_id: str
    report_version_number: int
    status: str
    audit_status: str
    source: dict[str, Any] = Field(default_factory=dict)
    frozen_config: dict[str, Any] = Field(default_factory=dict)
    sections: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    audit_issues: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    is_current_version: bool = False
    approved_by: str | None = None
    approved_at: str | None = None


class InterviewV2ReportSectionMutationResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    report_version_id: str
    report_version_number: int
    section_id: str
    section_revision: int
    status: str
    audit_status: str
    locked: bool
    reaudit_job_id: str | None = Field(default=None, pattern=_REAUDIT_JOB_PATTERN)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InterviewV2ReportClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    report_version_id: str
    status: str
    claim: dict[str, Any]
    findings: list[dict[str, Any]] = Field(default_factory=list)
    stat_fact: dict[str, Any] | None = None
    audit_issues: list[dict[str, Any]] = Field(default_factory=list)
