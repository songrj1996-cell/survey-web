"""访谈报告 V2 Sheet 分组与玩家映射接口结构。"""

from __future__ import annotations

import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class InterviewV2MappingRequestSchema(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)

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


class InterviewV2MappingResponseSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")


class InterviewV2MappingColumnRequest(InterviewV2MappingRequestSchema):
    sheet_id: str = Field(min_length=1, max_length=200)
    column_index: int = Field(strict=True, ge=1, le=256, alias="column")


class InterviewV2MappingParticipantRequest(InterviewV2MappingRequestSchema):
    participant_id: str | None = Field(default=None, min_length=1, max_length=200)
    participant_label: str = Field(min_length=1, max_length=200)
    columns: list[InterviewV2MappingColumnRequest] = Field(min_length=1, max_length=64)

    @field_validator("participant_label")
    @classmethod
    def _label_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("participant_label must not be blank")
        return unicodedata.normalize("NFC", value.strip())


class InterviewV2MappingSheetRequest(InterviewV2MappingRequestSchema):
    sheet_id: str = Field(min_length=1, max_length=200)
    role: Literal["record", "guide_reference", "attribute_reference"]
    recorder_label: str = Field(default="", max_length=200)


class InterviewV2MappingGroupRequest(InterviewV2MappingRequestSchema):
    group_id: str | None = Field(default=None, min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    sheets: list[InterviewV2MappingSheetRequest] = Field(min_length=1, max_length=64)
    participants: list[InterviewV2MappingParticipantRequest] = Field(
        default_factory=list,
        max_length=16384,
        alias="participant_bindings",
    )

    @field_validator("display_name")
    @classmethod
    def _display_name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name must not be blank")
        return unicodedata.normalize("NFC", value.strip())


class InterviewV2GroupMappingDraftRequest(InterviewV2MappingRequestSchema):
    base_mapping_revision: int = Field(strict=True, ge=0)
    groups: list[InterviewV2MappingGroupRequest] = Field(
        default_factory=list, max_length=64
    )
    ignored_sheet_ids: list[str] = Field(default_factory=list, max_length=64)
    change_kind: Literal["manual_edit"] = "manual_edit"
    change_reason: str = Field(default="", max_length=500)


class InterviewV2GroupMappingConfirmRequest(InterviewV2MappingRequestSchema):
    base_mapping_revision: int = Field(strict=True, ge=1)
    mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InterviewV2GroupMappingRestoreRequest(InterviewV2MappingRequestSchema):
    base_mapping_revision: int = Field(strict=True, ge=1)
    target_mapping_revision_id: str = Field(
        pattern=r"^mapping_[0-9a-f]{32}$"
    )
    target_mapping_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    change_kind: Literal["undo", "redo", "restore"]
    change_reason: str = Field(min_length=1, max_length=500)

    @field_validator("change_reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("change_reason must not be blank")
        return unicodedata.normalize("NFC", value.strip())


class InterviewV2MappingRevisionHistory(InterviewV2MappingResponseSchema):
    revision_number: int
    mapping_revision_id: str
    mapping_sha256: str
    change_kind: str = "manual_edit"
    restored_from_mapping_revision_id: str | None = None
    restored_from_revision_number: int | None = None
    created_at: str | None = None
    confirmed: bool = False
    confirmed_at: str | None = None


class InterviewV2GroupProposalResponse(InterviewV2MappingResponseSchema):
    import_id: str
    project_id: str
    status: str
    proposals: dict[str, Any] = Field(default_factory=dict)
    revision_number: int = 0
    mapping_revision_id: str | None = None
    mapping_sha256: str | None = None
    mapping: dict[str, Any] = Field(default_factory=dict)
    issues: list[dict[str, Any]] = Field(default_factory=list)
    confirmation_ready: bool = False
    final_participant_preview: dict[str, Any] = Field(default_factory=dict)
    history: list[InterviewV2MappingRevisionHistory] = Field(default_factory=list)


class InterviewV2GroupMappingResponse(InterviewV2GroupProposalResponse):
    pass
