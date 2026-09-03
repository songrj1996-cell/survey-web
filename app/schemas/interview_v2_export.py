"""Strict HTTP contracts for approved Interview Report V2 DOCX artifacts."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_EXPORT_ARTIFACT_PATTERN = r"^export_[0-9a-f]{32}$"
_PROJECT_PATTERN = r"^project_[0-9a-f]{32}$"
_REPORT_PATTERN = r"^report_[0-9a-f]{32}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_UTC_TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?Z$"
)
_DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
_SECTION_KEYS = (
    "scope_and_sample",
    "core_findings",
    "module_findings",
    "participant_differences",
    "participant_logics",
    "recommendations",
    "evidence_and_limitations",
)
_VISIBLE_FIELDS = (
    "participant_label",
    "evidence_type",
    "normalized_content",
    "sheet_name",
    "cell_address",
)
_OMITTED_FIELDS = (
    "internal_ids",
    "raw_content",
    "display_content",
    "recorder_label",
    "owner",
)


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InterviewV2ExportCreateRequest(_StrictContract):
    format: Literal["docx"] = "docx"
    include_evidence_appendix: Literal[True] = True

    @field_validator("include_evidence_appendix", mode="before")
    @classmethod
    def _appendix_is_mandatory(cls, value: object) -> object:
        if value is not True:
            raise ValueError("include_evidence_appendix must be the boolean true")
        return value


class InterviewV2ExportProfileResponse(_StrictContract):
    profile_version: Literal["approved-docx-evidence-redacted/1.0"]
    format: Literal["docx"]
    include_evidence_appendix: Literal[True]
    visible_evidence_fields: list[
        Literal[
            "participant_label",
            "evidence_type",
            "normalized_content",
            "sheet_name",
            "cell_address",
        ]
    ] = Field(min_length=5, max_length=5)
    omitted_evidence_fields: list[
        Literal[
            "internal_ids",
            "raw_content",
            "display_content",
            "recorder_label",
            "owner",
        ]
    ] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def _fixed_privacy_profile(self):
        if tuple(self.visible_evidence_fields) != _VISIBLE_FIELDS:
            raise ValueError("visible_evidence_fields must use the fixed safe profile")
        if tuple(self.omitted_evidence_fields) != _OMITTED_FIELDS:
            raise ValueError("omitted_evidence_fields must use the fixed redaction profile")
        return self


class InterviewV2ExportSectionManifestResponse(_StrictContract):
    section_key: str = Field(min_length=1, max_length=80)
    section_revision: int = Field(ge=1)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)


class InterviewV2ExportManifestResponse(_StrictContract):
    schema_version: Literal["interview-report-export/1.0"]
    format: Literal["docx"]
    export_profile_version: Literal["approved-docx-evidence-redacted/1.0"]
    report_version_id: str = Field(pattern=_REPORT_PATTERN)
    report_version_number: int = Field(ge=1)
    report_revision_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    approval_status: Literal["approved"]
    approved_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)
    report_body_sha256: str = Field(pattern=_SHA256_PATTERN)
    section_manifest: list[InterviewV2ExportSectionManifestResponse] = Field(
        min_length=7,
        max_length=7,
    )
    appendix_sha256: str = Field(pattern=_SHA256_PATTERN)
    document_markdown_sha256: str = Field(pattern=_SHA256_PATTERN)
    section_count: Literal[7]
    claim_count: int = Field(ge=1)
    evidence_count: int = Field(ge=1)
    appendix_entry_count: int = Field(ge=1)

    @model_validator(mode="after")
    def _manifest_is_internally_consistent(self):
        if tuple(item.section_key for item in self.section_manifest) != _SECTION_KEYS:
            raise ValueError("section_manifest must use the fixed report section order")
        if self.section_count != len(self.section_manifest):
            raise ValueError("section_count does not match section_manifest")
        if self.appendix_entry_count < self.evidence_count:
            raise ValueError("appendix_entry_count cannot be below evidence_count")
        return self


class InterviewV2ExportArtifactResponse(_StrictContract):
    export_artifact_id: str = Field(pattern=_EXPORT_ARTIFACT_PATTERN)
    project_id: str = Field(pattern=_PROJECT_PATTERN)
    report_version_id: str = Field(pattern=_REPORT_PATTERN)
    report_version_number: int = Field(ge=1)
    status: Literal["READY"]
    format: Literal["docx"]
    export_profile: InterviewV2ExportProfileResponse
    manifest: InterviewV2ExportManifestResponse
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_revision_payload_sha256: str = Field(pattern=_SHA256_PATTERN)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    byte_size: int = Field(ge=1)
    file_name: str = Field(
        min_length=6,
        max_length=240,
        pattern=r"^[^/\\\r\n]{1,235}\.docx$",
    )
    media_type: Literal[
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ] = _DOCX_MEDIA_TYPE
    download_url: str = Field(min_length=1, max_length=500)
    created_at: str = Field(pattern=_UTC_TIMESTAMP_PATTERN)

    @model_validator(mode="after")
    def _artifact_matches_manifest(self):
        if self.report_version_id != self.manifest.report_version_id:
            raise ValueError("report_version_id does not match manifest")
        if self.report_version_number != self.manifest.report_version_number:
            raise ValueError("report_version_number does not match manifest")
        if (
            self.report_revision_payload_sha256
            != self.manifest.report_revision_payload_sha256
        ):
            raise ValueError("report revision digest does not match manifest")
        if self.export_profile.profile_version != self.manifest.export_profile_version:
            raise ValueError("export profile does not match manifest")
        if self.format != self.manifest.format or self.format != self.export_profile.format:
            raise ValueError("export format does not match manifest and profile")
        expected_download_url = (
            "/api/v1/interview-export-artifacts/"
            f"{self.export_artifact_id}/download"
        )
        if self.download_url != expected_download_url:
            raise ValueError("download_url does not match export_artifact_id")
        return self


__all__ = [
    "InterviewV2ExportArtifactResponse",
    "InterviewV2ExportCreateRequest",
    "InterviewV2ExportManifestResponse",
    "InterviewV2ExportProfileResponse",
    "InterviewV2ExportSectionManifestResponse",
]
