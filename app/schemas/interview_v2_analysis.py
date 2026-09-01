"""HTTP contracts for Batch 5A cross-participant analysis runs."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InterviewV2AnalysisRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    base_analysis_run_id: str | None = Field(
        default=None, pattern=r"^analysis_[0-9a-f]{32}$"
    )
    freeze_current: Literal[True] = True


class InterviewV2AnalysisRunResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project_id: str
    analysis_run_id: str | None = None
    analysis_version_number: int = 0
    status: str
    source: dict[str, Any] = Field(default_factory=dict)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    stat_facts: list[dict[str, Any]] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    model_usage: dict[str, Any] = Field(default_factory=dict)
