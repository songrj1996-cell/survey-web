"""调研素材领域数据契约。

这里只定义可序列化的数据结构，不包含获取、解析或持久化流程。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ContractModel(BaseModel):
    """统一采用严格字段和稳定 JSON 序列化的契约基类。"""

    model_config = ConfigDict(extra="forbid")


class Provider(str, Enum):
    GOOGLE_FORMS = "google_forms"
    BESTED = "bested"
    EXCEL = "excel"
    GOOGLE_DRIVE = "google_drive"
    YOUTUBE = "youtube"
    LOCAL_UPLOAD = "local_upload"
    PLATFORM = "platform"
    UNKNOWN = "unknown"


class SourceKind(str, Enum):
    LOCAL_UPLOAD = "local_upload"
    REMOTE_URL = "remote_url"
    PROVIDER_CONNECTION = "provider_connection"
    EMBEDDED_DOCUMENT_OBJECT = "embedded_document_object"
    GENERATED_SNAPSHOT = "generated_snapshot"


class ProcessingStatus(str, Enum):
    PENDING = "pending"
    ACQUIRING = "acquiring"
    PARSING = "parsing"
    PROCESSING = "processing"
    NEEDS_REVIEW = "needs_review"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class AccessStatus(str, Enum):
    ACCESSIBLE = "accessible"
    LOGIN_REQUIRED = "login_required"
    PERMISSION_REQUIRED = "permission_required"
    NOT_FOUND = "not_found"
    DELETED = "deleted"
    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    DOWNLOAD_FAILED = "download_failed"


class DocumentType(str, Enum):
    QUESTIONNAIRE = "questionnaire"
    SPREADSHEET = "spreadsheet"
    PRESENTATION = "presentation"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"
    EXTERNAL_RESOURCE = "external_resource"


class SnapshotPolicy(str, Enum):
    FULL_COPY = "full_copy"
    METADATA_ONLY = "metadata_only"
    REFERENCE_ONLY = "reference_only"
    EPHEMERAL = "ephemeral"


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SLIDE = "slide"
    DOCUMENT = "document"
    EXTERNAL_LINK = "external_link"


class SensitivityStatus(str, Enum):
    UNKNOWN = "unknown"
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    PERSONAL_DATA = "personal_data"
    RESTRICTED = "restricted"


class ExportPolicy(str, Enum):
    NEVER = "never"
    MANUAL_CONFIRMATION = "manual_confirmation"
    ALLOWED = "allowed"


class AssetContextType(str, Enum):
    SURVEY_QUESTION = "survey_question"
    SURVEY_OPTION = "survey_option"
    INTERVIEW_POSITION = "interview_position"
    RESEARCH_DOCUMENT = "research_document"
    REPORT = "report"


class AssetRole(str, Enum):
    QUESTION_STIMULUS = "question_stimulus"
    QUESTION_INSTRUCTION = "question_instruction"
    OPTION_STIMULUS = "option_stimulus"
    PARTICIPANT_RESPONSE = "participant_response"
    INTERVIEW_EVIDENCE = "interview_evidence"
    RESEARCHER_MATERIAL = "researcher_material"
    ANALYSIS_TARGET = "analysis_target"
    REPORT_ATTACHMENT = "report_attachment"


class BindingStatus(str, Enum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


class DerivativeType(str, Enum):
    THUMBNAIL = "thumbnail"
    OCR = "ocr"
    IMAGE_UNDERSTANDING = "image_understanding"
    KEYFRAME = "keyframe"
    METADATA = "metadata"
    HUMAN_REVISION = "human_revision"


class ReviewStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REVISED = "revised"
    REJECTED = "rejected"


class ImportErrorCode(str, Enum):
    INVALID_SOURCE = "invalid_source"
    LOGIN_REQUIRED = "login_required"
    PERMISSION_REQUIRED = "permission_required"
    NOT_FOUND = "not_found"
    DELETED = "deleted"
    UNSUPPORTED_TYPE = "unsupported_type"
    TOO_LARGE = "too_large"
    DOWNLOAD_FAILED = "download_failed"
    PARSE_FAILED = "parse_failed"
    MAPPING_CONFLICT = "mapping_conflict"
    INTEGRITY_ERROR = "integrity_error"
    PROVIDER_ERROR = "provider_error"


class SourceLocator(BaseModel):
    """统一来源定位；允许保留新增 Provider 的扩展定位字段。"""

    model_config = ConfigDict(extra="allow")

    source_id: str | None = None
    document_id: str | None = None
    provider: Provider | None = None
    provider_form_id: str | None = None
    provider_question_id: str | None = None
    provider_option_id: str | None = None
    provider_item_id: str | None = None
    question_position: int | None = Field(default=None, ge=0)
    file_id: str | None = None
    sheet_name: str | None = None
    cell: str | None = None
    anchor: str | None = None
    coverage: str | None = None
    slide_number: int | None = Field(default=None, ge=1)
    object_id: str | None = None
    page_region: str | None = None
    drive_file_id: str | None = None
    provider_revision: str | None = None
    video_id: str | None = None
    time_start_seconds: float | None = Field(default=None, ge=0)
    time_end_seconds: float | None = Field(default=None, ge=0)
    local_file_id: str | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> "SourceLocator":
        if (
            self.time_start_seconds is not None
            and self.time_end_seconds is not None
            and self.time_end_seconds < self.time_start_seconds
        ):
            raise ValueError("time_end_seconds 不能早于 time_start_seconds")
        return self


class ImportWarning(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    field_path: str | None = None
    blocking: bool = False
    source_locator: SourceLocator | None = None


class ImportIssue(ContractModel):
    code: ImportErrorCode
    message: str = Field(min_length=1)
    retryable: bool = False
    suggested_action: str | None = None
    safe_log_ref: str | None = None
    source_locator: SourceLocator | None = None


class ResearchSource(ContractModel):
    source_id: str = Field(min_length=1)
    source_kind: SourceKind
    provider: Provider
    original_name: str = ""
    original_url: str | None = None
    owner_ref: str = Field(min_length=1)
    created_at: datetime
    acquisition_status: ProcessingStatus = ProcessingStatus.PENDING
    access_status: AccessStatus = AccessStatus.ACCESSIBLE
    warnings: list[ImportWarning] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)


class ResearchDocument(ContractModel):
    document_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    document_type: DocumentType
    title: str = ""
    filename: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_modified_at: datetime | None = None
    retrieved_at: datetime
    snapshot_policy: SnapshotPolicy
    parse_status: ProcessingStatus = ProcessingStatus.PENDING
    source_locator: SourceLocator | None = None
    warnings: list[ImportWarning] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)


class ResearchAsset(ContractModel):
    asset_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    media_type: MediaType
    mime_type: str | None = None
    filename: str | None = None
    display_name: str = ""
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider: Provider
    provider_resource_id: str | None = None
    provider_version: str | None = None
    access_status: AccessStatus = AccessStatus.ACCESSIBLE
    processing_status: ProcessingStatus = ProcessingStatus.PENDING
    sensitivity_status: SensitivityStatus = SensitivityStatus.UNKNOWN
    export_policy: ExportPolicy = ExportPolicy.MANUAL_CONFIRMATION
    source_locator: SourceLocator | None = None
    warnings: list[ImportWarning] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)


class AssetReference(ContractModel):
    reference_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    context_type: AssetContextType
    context_id: str = Field(min_length=1)
    role: AssetRole
    option_key: str | None = None
    source_locator: SourceLocator
    binding_status: BindingStatus = BindingStatus.PROPOSED
    binding_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    warnings: list[ImportWarning] = Field(default_factory=list)


class AssetDerivative(ContractModel):
    derivative_id: str = Field(min_length=1)
    asset_id: str = Field(min_length=1)
    derivative_type: DerivativeType
    status: ProcessingStatus = ProcessingStatus.PENDING
    model: str | None = None
    model_version: str | None = None
    created_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    review_status: ReviewStatus = ReviewStatus.PENDING
    revised_from_derivative_id: str | None = None


class ResearchAssetCollection(ContractModel):
    schema_version: int = Field(default=1, ge=1)
    sources: list[ResearchSource] = Field(default_factory=list)
    documents: list[ResearchDocument] = Field(default_factory=list)
    assets: list[ResearchAsset] = Field(default_factory=list)
    references: list[AssetReference] = Field(default_factory=list)
    derivatives: list[AssetDerivative] = Field(default_factory=list)
