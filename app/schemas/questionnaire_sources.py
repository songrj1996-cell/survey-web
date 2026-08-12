"""问卷来源编排、冲突和统一导入结果的数据契约。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from app.schemas.questionnaire import QuestionnaireSnapshot, QuestionnaireSourceMode
from app.schemas.research_assets import (
    ContractModel,
    ImportIssue,
    ImportWarning,
    ProcessingStatus,
    ResearchAssetCollection,
)


_SOURCE_MODE_PRIORITY = {
    QuestionnaireSourceMode.PLATFORM_SNAPSHOT: 1,
    QuestionnaireSourceMode.OFFICIAL_API: 2,
    QuestionnaireSourceMode.AUTHORIZED_EDIT: 2,
    QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD: 3,
    QuestionnaireSourceMode.MATERIAL_UPLOAD: 3,
    QuestionnaireSourceMode.PUBLISHED_PAGE: 4,
    QuestionnaireSourceMode.RESPONSE_EXPORT_FALLBACK: 5,
}


def questionnaire_source_priority(mode: QuestionnaireSourceMode) -> int:
    """返回不可由请求调用方改写的来源可信级别。"""
    return _SOURCE_MODE_PRIORITY[mode]


def _validate_priority(
    mode: QuestionnaireSourceMode,
    priority: int,
) -> None:
    expected = questionnaire_source_priority(mode)
    if priority != expected:
        raise ValueError(
            f"{mode.value} 的 priority 必须使用固定级别 {expected}"
        )


class QuestionnaireConflictResolution(str, Enum):
    """多来源字段冲突的处理状态。"""

    UNRESOLVED = "unresolved"
    ACCEPT_SUGGESTION = "accept_suggestion"
    USER_SELECTED = "user_selected"


class QuestionnaireSourceValue(ContractModel):
    """某个来源针对单一字段提供的候选值。"""

    source_id: str = Field(min_length=1)
    source_mode: QuestionnaireSourceMode
    priority: int = Field(ge=1)
    value: JsonValue
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_priority(self) -> "QuestionnaireSourceValue":
        _validate_priority(self.source_mode, self.priority)
        return self


class QuestionnaireSourceConflict(ContractModel):
    """不得静默覆盖的多来源字段冲突。"""

    conflict_id: str = Field(min_length=1)
    field_path: str = Field(min_length=1)
    candidates: list[QuestionnaireSourceValue] = Field(min_length=2)
    suggested_source_id: str = Field(min_length=1)
    suggested_value: JsonValue
    reason: str = Field(min_length=1)
    blocking: bool = False
    resolution: QuestionnaireConflictResolution = (
        QuestionnaireConflictResolution.UNRESOLVED
    )
    selected_source_id: str | None = Field(default=None, min_length=1)
    selected_value: JsonValue | None = None

    @model_validator(mode="after")
    def validate_resolution(self) -> "QuestionnaireSourceConflict":
        source_ids = [candidate.source_id for candidate in self.candidates]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("冲突 candidates 不能包含重复 source_id")
        if self.suggested_source_id not in source_ids:
            raise ValueError("suggested_source_id 必须来自 candidates")
        suggested = next(
            item for item in self.candidates
            if item.source_id == self.suggested_source_id
        )
        if suggested.value != self.suggested_value:
            raise ValueError("suggested_value 必须与建议来源的候选值一致")

        if self.resolution == QuestionnaireConflictResolution.UNRESOLVED:
            if self.selected_source_id is not None or self.selected_value is not None:
                raise ValueError("未解决冲突不能包含最终选择")
            return self

        if self.resolution == QuestionnaireConflictResolution.ACCEPT_SUGGESTION:
            if self.selected_source_id not in {None, self.suggested_source_id}:
                raise ValueError("接受建议时不能选择其他来源")
            if (
                self.selected_value is not None
                and self.selected_value != self.suggested_value
            ):
                raise ValueError("接受建议时不能选择其他值")
            self.selected_source_id = self.suggested_source_id
            self.selected_value = self.suggested_value
            return self

        if self.selected_source_id not in source_ids:
            raise ValueError("人工选择的 source_id 必须来自 candidates")
        selected = next(
            item for item in self.candidates
            if item.source_id == self.selected_source_id
        )
        if self.selected_value != selected.value:
            raise ValueError("selected_value 必须与人工选择来源的候选值一致")
        return self


class QuestionnaireSourceAttempt(ContractModel):
    """一次来源获取/解析尝试的安全摘要。"""

    source_id: str = Field(min_length=1)
    source_mode: QuestionnaireSourceMode
    priority: int = Field(ge=1)
    status: ProcessingStatus
    snapshot_id: str | None = Field(default=None, min_length=1)
    warnings: list[ImportWarning] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_priority(self) -> "QuestionnaireSourceAttempt":
        _validate_priority(self.source_mode, self.priority)
        return self


class QuestionnaireSourceResult(ContractModel):
    """第 2 批统一输出；Bundle 仍以两份领域对象原子保存。"""

    schema_version: Literal[1] = 1
    snapshot: QuestionnaireSnapshot
    collection: ResearchAssetCollection
    selected_source_ids: list[str] = Field(min_length=1)
    attempts: list[QuestionnaireSourceAttempt] = Field(min_length=1)
    conflicts: list[QuestionnaireSourceConflict] = Field(default_factory=list)
    partial_success: bool = False

    @model_validator(mode="after")
    def validate_sources(self) -> "QuestionnaireSourceResult":
        if len(self.selected_source_ids) != len(set(self.selected_source_ids)):
            raise ValueError("selected_source_ids 不能重复")
        attempt_ids = {attempt.source_id for attempt in self.attempts}
        if len(attempt_ids) != len(self.attempts):
            raise ValueError("attempts 不能包含重复 source_id")
        missing = set(self.selected_source_ids) - attempt_ids
        if missing:
            raise ValueError("selected_source_ids 必须来自 attempts")
        collection_source_ids = {
            source.source_id for source in self.collection.sources
        }
        if not set(self.selected_source_ids).issubset(collection_source_ids):
            raise ValueError("selected_source_ids 必须存在于素材集合来源中")
        if any(
            candidate.source_id not in attempt_ids
            for conflict in self.conflicts
            for candidate in conflict.candidates
        ):
            raise ValueError("冲突候选来源必须存在于 attempts")
        return self


class QuestionnaireMergeCandidate(ContractModel):
    """一个可参与来源优先级、冲突检测和降级选择的完整候选。"""

    source_id: str = Field(min_length=1)
    source_mode: QuestionnaireSourceMode
    priority: int = Field(ge=1)
    snapshot: QuestionnaireSnapshot | None = None
    collection: ResearchAssetCollection | None = None
    status: ProcessingStatus
    warnings: list[ImportWarning] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_payload(self) -> "QuestionnaireMergeCandidate":
        _validate_priority(self.source_mode, self.priority)
        if (self.snapshot is None) != (self.collection is None):
            raise ValueError("候选必须同时包含 snapshot 与 collection")
        if self.status == ProcessingStatus.COMPLETED and self.snapshot is None:
            raise ValueError("completed 候选必须包含完整 Bundle")
        if self.snapshot is not None:
            source_ids = {source.source_id for source in self.collection.sources}
            if self.source_id not in source_ids:
                raise ValueError("候选 source_id 必须存在于 collection")
            if self.snapshot.source_mode != self.source_mode:
                raise ValueError("候选 source_mode 必须与 snapshot 一致")
        return self
