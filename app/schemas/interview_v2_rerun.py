"""Strict stage-discriminated requests for the shared V2 rerun endpoint."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from app.schemas.interview_v2_analysis import InterviewV2AnalysisRunResponse
from app.schemas.interview_v2_report import (
    InterviewV2ReportRerunRequest,
    InterviewV2ReportRerunResponse,
)


class InterviewV2AnalysisModuleRerunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    from_stage: Literal["analysis_module"]
    base_analysis_run_id: str = Field(pattern=r"^analysis_[0-9a-f]{32}$")
    module_id: str = Field(pattern=r"^module_[0-9a-f]{32}$")
    preserve_manual_report_edits: Literal[True] = True
    reuse_unchanged_artifacts: Literal[True] = True
    force: Literal[False] = False

    @field_validator("preserve_manual_report_edits", "reuse_unchanged_artifacts", "force", mode="before")
    @classmethod
    def _boolean_flags(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("rerun protection flags must be boolean")
        return value


class InterviewV2AnalysisModuleRerunResponse(InterviewV2AnalysisRunResponse):
    analysis_run_id: str = Field(pattern=r"^analysis_[0-9a-f]{32}$")
    is_current_version: bool
    rerun: dict[str, Any]


InterviewV2RerunRequest = Annotated[
    InterviewV2ReportRerunRequest | InterviewV2AnalysisModuleRerunRequest,
    Field(discriminator="from_stage"),
]
RERUN_REQUEST_ADAPTER = TypeAdapter(InterviewV2RerunRequest)
InterviewV2RerunResponse = InterviewV2ReportRerunResponse | InterviewV2AnalysisModuleRerunResponse
