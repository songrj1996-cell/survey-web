"""标准问卷报告历史中的快照来源安全契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.schemas.research_assets import ContractModel


MAX_SNAPSHOT_HISTORY_QUESTION_COUNT = 1_024
MAX_SNAPSHOT_HISTORY_ASSET_COUNT = 4_096
MAX_SNAPSHOT_HISTORY_ASSET_REFERENCE_COUNT = 16_384
MAX_SNAPSHOT_HISTORY_BINDINGS = 1_024
MAX_SNAPSHOT_HISTORY_COLUMNS_PER_BINDING = 1_024
MAX_SNAPSHOT_HISTORY_COLUMN_INDEX = 1_023
MAX_SNAPSHOT_HISTORY_WARNING_CODES = 32

SnapshotHistoryIdentifier = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=1_024,
        pattern=r"^[^\x00-\x1f\x7f]+$",
    ),
]
SnapshotHistorySha256 = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    ),
]
SnapshotHistoryWarningCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9_.:-]+$",
    ),
]
SnapshotHistoryColumnIndex = Annotated[
    int,
    Field(strict=True, ge=0, le=MAX_SNAPSHOT_HISTORY_COLUMN_INDEX),
]


class QuestionnaireSnapshotHistoryRef(ContractModel):
    """可在已鉴权历史详情中返回的最小快照来源。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    snapshot_id: SnapshotHistoryIdentifier
    package_sha256: SnapshotHistorySha256
    definition_sha256: SnapshotHistorySha256
    provider: Literal["google_forms", "bested"]
    source_mode: Literal["official_api", "original_questionnaire_upload"]
    mapping_status: Literal[
        "exact",
        "normalized",
        "partial",
        "needs_review",
        "unsupported",
        "source_missing",
    ]
    question_count: int = Field(
        strict=True,
        ge=1,
        le=MAX_SNAPSHOT_HISTORY_QUESTION_COUNT,
    )
    asset_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_SNAPSHOT_HISTORY_ASSET_COUNT,
    )
    asset_reference_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_SNAPSHOT_HISTORY_ASSET_REFERENCE_COUNT,
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def validate_schema_version_type(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version 必须是整数 1")
        return value

    @model_validator(mode="after")
    def validate_provider_source_mode(self) -> "QuestionnaireSnapshotHistoryRef":
        expected_mode = {
            "google_forms": "official_api",
            "bested": "original_questionnaire_upload",
        }[self.provider]
        if self.source_mode != expected_mode:
            raise ValueError("provider 与 source_mode 不一致")
        return self


class QuestionnaireSnapshotResponseBinding(ContractModel):
    """不含原始表头、路径或外部定位信息的回答列绑定。"""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
    )

    question_id: SnapshotHistoryIdentifier
    column_indexes: list[SnapshotHistoryColumnIndex] = Field(
        min_length=1,
        max_length=MAX_SNAPSHOT_HISTORY_COLUMNS_PER_BINDING,
    )
    mapping_method: str = Field(
        strict=True,
        min_length=1,
        max_length=256,
        pattern=r"^[a-z0-9_]+(?:\+[a-z0-9_]+)*$",
    )
    mapping_status: Literal["exact", "normalized"]
    confidence: float = Field(strict=True, ge=0.0, le=1.0)
    warning_codes: list[SnapshotHistoryWarningCode] = Field(
        max_length=MAX_SNAPSHOT_HISTORY_WARNING_CODES,
    )

    @model_validator(mode="after")
    def validate_safe_binding(self) -> "QuestionnaireSnapshotResponseBinding":
        methods = self.mapping_method.split("+")
        allowed_google_methods = {
            "provider_response_key",
            "declared_column_index",
            "declared_header",
            "normalized_header_fallback",
        }
        allowed_bested_methods = {
            "bested_code_and_header",
            "bested_code_and_matrix_headers",
        }
        if not (
            self.mapping_method in allowed_bested_methods
            or (
                len(methods) == len(set(methods))
                and all(method in allowed_google_methods for method in methods)
            )
        ):
            raise ValueError("mapping_method 不在安全白名单")
        if len(self.column_indexes) != len(set(self.column_indexes)):
            raise ValueError("column_indexes 不能重复")
        if self.column_indexes != sorted(self.column_indexes):
            raise ValueError("column_indexes 必须升序排列")
        if len(self.warning_codes) != len(set(self.warning_codes)):
            raise ValueError("warning_codes 不能重复")
        return self


class QuestionnaireSnapshotHistoryPayload(ContractModel):
    """写入或返回历史详情的三字段快照来源白名单。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    questionnaire_input_kind: Literal["saved_snapshot"]
    questionnaire_snapshot_ref: QuestionnaireSnapshotHistoryRef
    questionnaire_response_bindings: list[QuestionnaireSnapshotResponseBinding] = (
        Field(min_length=1, max_length=MAX_SNAPSHOT_HISTORY_BINDINGS)
    )

    @model_validator(mode="after")
    def validate_binding_coverage(self) -> "QuestionnaireSnapshotHistoryPayload":
        bindings = self.questionnaire_response_bindings
        if len(bindings) != self.questionnaire_snapshot_ref.question_count:
            raise ValueError("bindings 必须完整覆盖 question_count")
        question_ids = [binding.question_id for binding in bindings]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("question_id 不能重复")
        column_indexes = [
            index
            for binding in bindings
            for index in binding.column_indexes
        ]
        if len(column_indexes) != len(set(column_indexes)):
            raise ValueError("回答列不能绑定多个问题")
        bested_methods = {
            "bested_code_and_header",
            "bested_code_and_matrix_headers",
        }
        if self.questionnaire_snapshot_ref.provider == "google_forms":
            if any(
                binding.mapping_method in bested_methods
                for binding in bindings
            ):
                raise ValueError("Google Forms 不能使用倍市得绑定方法")
        elif any(
            binding.mapping_method not in bested_methods
            for binding in bindings
        ):
            raise ValueError("倍市得不能使用 Google Forms 绑定方法")
        return self


class QuestionnaireSnapshotHistorySummary(ContractModel):
    """历史列表允许公开的快照结构摘要。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["google_forms", "bested"]
    question_count: int = Field(
        strict=True,
        ge=1,
        le=MAX_SNAPSHOT_HISTORY_QUESTION_COUNT,
    )
    asset_count: int = Field(
        strict=True,
        ge=0,
        le=MAX_SNAPSHOT_HISTORY_ASSET_COUNT,
    )
