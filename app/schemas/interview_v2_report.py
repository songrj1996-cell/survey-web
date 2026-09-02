"""HTTP contracts for Batch 5B evidence-bound report versions."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InterviewV2ReportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_report_version_id: str | None = Field(default=None, pattern=r"^report_[0-9a-f]{32}$")
    freeze_current: Literal[True] = True


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


class InterviewV2ReportClaimResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")
    project_id: str
    report_version_id: str
    status: str
    claim: dict[str, Any]
    findings: list[dict[str, Any]] = Field(default_factory=list)
    stat_fact: dict[str, Any] | None = None
    audit_issues: list[dict[str, Any]] = Field(default_factory=list)
