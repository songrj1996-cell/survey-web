"""访谈报告 V2 上传预检接口的数据结构。"""
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class InterviewV2Schema(BaseModel):
    """响应只保留明确声明的公共字段，避免内部元数据意外外泄。"""

    model_config = ConfigDict(extra="ignore")


class InterviewV2Error(InterviewV2Schema):
    code: str
    message: str
    retryable: bool = False
    suggested_action: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    trace_id: str


class InterviewV2ErrorResponse(InterviewV2Schema):
    error: InterviewV2Error


class InterviewV2Warning(InterviewV2Schema):
    code: str
    message: str
    level: str = "warning"
    retryable: bool = False
    suggested_action: str = ""
    context: dict[str, Any] = Field(default_factory=dict)


class InterviewV2PrecheckSummary(InterviewV2Schema):
    file_size_bytes: int | None = None
    sheet_count: int | None = None
    non_empty_cell_count: int | None = None
    text_char_count: int | None = None
    formula_count: int | None = None
    warnings: list[InterviewV2Warning] = Field(default_factory=list)


class InterviewV2SheetSummary(InterviewV2Schema):
    sheet_id: str
    index: int | None = None
    name: str
    state: str = "visible"
    declared_range: str | None = None
    content_range: str | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)
    hidden_row_count: int = 0
    hidden_column_count: int = 0
    merged_range_count: int = 0
    candidate_participant_region: dict[str, Any] | str | None = None


class InterviewV2ImportSummary(InterviewV2PrecheckSummary):
    sheets: list[InterviewV2SheetSummary] = Field(default_factory=list)


class InterviewV2UploadAttemptResponse(InterviewV2Schema):
    upload_attempt_id: str
    job_id: str | None = None
    status: str
    filename: str | None = None
    file_size: int | None = None
    content_sha256: str | None = None
    file_contract_version: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    project_id: str | None = None
    import_id: str | None = None
    workbook_revision_id: str | None = None
    precheck_summary: InterviewV2PrecheckSummary | None = None
    error: InterviewV2Error | None = None


class InterviewV2ImportResponse(InterviewV2Schema):
    import_id: str
    project_id: str
    workbook_revision_id: str
    status: str
    created_at: str | None = None
    updated_at: str | None = None
    physical_snapshot_version: str | None = None
    summary: InterviewV2ImportSummary | None = None
    warnings: list[InterviewV2Warning] = Field(default_factory=list)
