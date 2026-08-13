"""问卷来源编排、冲突和统一导入结果的数据契约。"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import Field, JsonValue, model_validator

from app.schemas.questionnaire import QuestionnaireSnapshot, QuestionnaireSourceMode
from app.schemas.research_assets import (
    ContractModel,
    ImportErrorCode,
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


class QuestionnaireAcquisitionRoute(str, Enum):
    """本次工作流如何取得候选；与快照原始 ``source_mode`` 分离。"""

    SAVED_SNAPSHOT = "saved_snapshot"
    AUTHORIZED_CONNECTION = "authorized_connection"
    SNAPSHOT_UPLOAD = "snapshot_upload"
    ORIGINAL_QUESTIONNAIRE_UPLOAD = "original_questionnaire_upload"
    PUBLISHED_PAGE = "published_page"
    RESPONSE_ONLY = "response_only"


class QuestionnaireSourceWorkflowStatus(str, Enum):
    """问卷来源降级工作流的稳定外部状态。"""

    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    RESOLVED_PARTIAL = "resolved_partial"
    AWAITING_ACTION = "awaiting_action"
    CLOSED_PUBLIC_PAGE = "closed_public_page"
    SELECTION_REQUIRED = "selection_required"
    SKIPPED = "skipped"
    FAILED = "failed"


class QuestionnaireSourceFailureReason(str, Enum):
    """不依赖错误文案推断的稳定失败分类。"""

    LOGIN_REQUIRED = "login_required"
    PERMISSION_REQUIRED = "permission_required"
    CLOSED_PUBLIC_PAGE = "closed_public_page"
    NOT_FOUND = "not_found"
    RETRYABLE_PROVIDER = "retryable_provider"
    INVALID_INPUT = "invalid_input"
    UNKNOWN = "unknown"


class QuestionnaireSourceNextAction(str, Enum):
    """上层明确声明可用后，工作流才会返回的下一步动作。"""

    RETRY_SOURCE = "retry_source"
    AUTHORIZE_CONNECTION = "authorize_connection"
    UPLOAD_SNAPSHOT = "upload_snapshot"
    UPLOAD_ORIGINAL_QUESTIONNAIRE = "upload_original_questionnaire"
    RETRY_PUBLISHED_PAGE = "retry_published_page"
    TEMPORARILY_REOPEN_AND_RETRY = "temporarily_reopen_and_retry"
    SELECT_SOURCE = "select_source"
    CONTINUE_RESPONSE_ONLY = "continue_response_only"


_NEXT_ACTIONS_BY_FAILURE_REASON = {
    QuestionnaireSourceFailureReason.LOGIN_REQUIRED: {
        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
    QuestionnaireSourceFailureReason.PERMISSION_REQUIRED: {
        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
    QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE: {
        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.RETRY_PUBLISHED_PAGE,
        QuestionnaireSourceNextAction.TEMPORARILY_REOPEN_AND_RETRY,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
    QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER: {
        QuestionnaireSourceNextAction.RETRY_SOURCE,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
    QuestionnaireSourceFailureReason.INVALID_INPUT: {
        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
    QuestionnaireSourceFailureReason.NOT_FOUND: {
        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
    QuestionnaireSourceFailureReason.UNKNOWN: {
        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
        QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
    },
}

_FAILURE_REASON_ORDER = {
    QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE: 0,
    QuestionnaireSourceFailureReason.LOGIN_REQUIRED: 1,
    QuestionnaireSourceFailureReason.PERMISSION_REQUIRED: 2,
    QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER: 3,
    QuestionnaireSourceFailureReason.INVALID_INPUT: 4,
    QuestionnaireSourceFailureReason.NOT_FOUND: 5,
    QuestionnaireSourceFailureReason.UNKNOWN: 6,
}

_SOURCE_MODES_BY_ACQUISITION_ROUTE = {
    QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION: {
        QuestionnaireSourceMode.OFFICIAL_API,
        QuestionnaireSourceMode.AUTHORIZED_EDIT,
    },
    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD: {
        QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD,
        QuestionnaireSourceMode.MATERIAL_UPLOAD,
    },
    QuestionnaireAcquisitionRoute.PUBLISHED_PAGE: {
        QuestionnaireSourceMode.PUBLISHED_PAGE,
    },
}

_ACQUISITION_ROUTE_ORDER = {
    QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT: 1,
    QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION: 2,
    QuestionnaireAcquisitionRoute.SNAPSHOT_UPLOAD: 3,
    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD: 4,
    QuestionnaireAcquisitionRoute.PUBLISHED_PAGE: 5,
}


def validate_acquisition_provenance(
    route: QuestionnaireAcquisitionRoute,
    source_mode: QuestionnaireSourceMode,
) -> None:
    """校验实时获取路径的来源真实性。

    已存快照和快照包会保留原始 provenance，因此可以承载任意
    ``source_mode``；实时授权、原问卷上传和发布页则必须与实际
    获取方式一致。
    """
    if route == QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
        raise ValueError("response_only 不能作为来源 attempt")
    allowed = _SOURCE_MODES_BY_ACQUISITION_ROUTE.get(route)
    if allowed is not None and source_mode not in allowed:
        raise ValueError("acquisition route 与 source_mode 不兼容")


def _failure_reason_from_issues(
    issues: list[ImportIssue],
) -> QuestionnaireSourceFailureReason:
    reasons: list[QuestionnaireSourceFailureReason] = []
    invalid_input_codes = {
        ImportErrorCode.INVALID_SOURCE,
        ImportErrorCode.UNSUPPORTED_TYPE,
        ImportErrorCode.TOO_LARGE,
        ImportErrorCode.PARSE_FAILED,
        ImportErrorCode.MAPPING_CONFLICT,
        ImportErrorCode.INTEGRITY_ERROR,
    }
    for issue in issues:
        if issue.code == ImportErrorCode.LOGIN_REQUIRED:
            reasons.append(QuestionnaireSourceFailureReason.LOGIN_REQUIRED)
        elif issue.code == ImportErrorCode.PERMISSION_REQUIRED:
            reasons.append(QuestionnaireSourceFailureReason.PERMISSION_REQUIRED)
        elif issue.code in {ImportErrorCode.NOT_FOUND, ImportErrorCode.DELETED}:
            reasons.append(QuestionnaireSourceFailureReason.NOT_FOUND)
        elif issue.retryable:
            reasons.append(QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER)
        elif issue.code in invalid_input_codes:
            reasons.append(QuestionnaireSourceFailureReason.INVALID_INPUT)
        else:
            reasons.append(QuestionnaireSourceFailureReason.UNKNOWN)
    if not reasons:
        return QuestionnaireSourceFailureReason.UNKNOWN
    return min(reasons, key=_FAILURE_REASON_ORDER.__getitem__)


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
    acquisition_route: QuestionnaireAcquisitionRoute | None = None
    status: ProcessingStatus
    snapshot_id: str | None = Field(default=None, min_length=1)
    failure_reason: QuestionnaireSourceFailureReason | None = None
    warnings: list[ImportWarning] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_priority(self) -> "QuestionnaireSourceAttempt":
        _validate_priority(self.source_mode, self.priority)
        if self.acquisition_route is not None:
            validate_acquisition_provenance(
                self.acquisition_route,
                self.source_mode,
            )
        if (
            self.failure_reason is not None
            and self.status != ProcessingStatus.FAILED
        ):
            raise ValueError("只有 failed 来源尝试可以包含 failure_reason")
        if self.status == ProcessingStatus.FAILED and self.acquisition_route is not None:
            if self.failure_reason is None or not self.issues:
                raise ValueError("工作流 failed attempt 必须包含原因和 issue")
            expected_reason = _failure_reason_from_issues(self.issues)
            if self.failure_reason == QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE:
                if (
                    self.acquisition_route
                    != QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
                    or expected_reason != QuestionnaireSourceFailureReason.UNKNOWN
                    or any(issue.retryable for issue in self.issues)
                ):
                    raise ValueError("closed_public_page 与结构化 issues 不一致")
            elif self.failure_reason != expected_reason:
                raise ValueError("failure_reason 必须由结构化 issues 确定")
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
        attempt_keys = [
            (attempt.acquisition_route, attempt.source_id)
            for attempt in self.attempts
        ]
        if len(attempt_keys) != len(set(attempt_keys)):
            raise ValueError("attempts 在同一获取路径不能重复 source_id")
        attempt_ids = {attempt.source_id for attempt in self.attempts}
        missing = set(self.selected_source_ids) - attempt_ids
        if missing:
            raise ValueError("selected_source_ids 必须来自 attempts")
        selected_evidence_ids = {
            attempt.source_id
            for attempt in self.attempts
            if attempt.source_id in self.selected_source_ids
            and attempt.source_mode == self.snapshot.source_mode
            and attempt.snapshot_id == self.snapshot.snapshot_id
            and attempt.status
            in {
                ProcessingStatus.COMPLETED,
                ProcessingStatus.PARTIAL,
                ProcessingStatus.NEEDS_REVIEW,
            }
        }
        if selected_evidence_ids != set(self.selected_source_ids):
            raise ValueError("选中 attempt 必须与最终快照身份及 provenance 一致")
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
        selected_attempt_keys = {
            (attempt.acquisition_route, attempt.source_id)
            for attempt in self.attempts
            if attempt.source_id in self.selected_source_ids
            and attempt.source_mode == self.snapshot.source_mode
            and attempt.snapshot_id == self.snapshot.snapshot_id
            and attempt.status
            in {
                ProcessingStatus.COMPLETED,
                ProcessingStatus.PARTIAL,
                ProcessingStatus.NEEDS_REVIEW,
            }
        }
        requires_partial = bool(self.conflicts) or any(
            attempt.status
            in {
                ProcessingStatus.PARTIAL,
                ProcessingStatus.NEEDS_REVIEW,
                ProcessingStatus.FAILED,
                ProcessingStatus.SKIPPED,
            }
            or bool(attempt.warnings)
            or bool(attempt.issues)
            for attempt in self.attempts
            if (attempt.acquisition_route, attempt.source_id)
            not in selected_attempt_keys
        ) or any(
            attempt.status != ProcessingStatus.COMPLETED
            or bool(attempt.warnings)
            or bool(attempt.issues)
            for attempt in self.attempts
            if (attempt.acquisition_route, attempt.source_id)
            in selected_attempt_keys
        )
        if requires_partial and not self.partial_success:
            raise ValueError(
                "来源降级、冲突或非完整 attempt 必须标记 partial_success"
            )
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


class QuestionnaireSourceWorkflowResult(ContractModel):
    """可在没有快照时诚实表达等待、选择、跳过或失败。"""

    schema_version: Literal[1] = 1
    status: QuestionnaireSourceWorkflowStatus
    route: QuestionnaireAcquisitionRoute | None = None
    result: QuestionnaireSourceResult | None = None
    attempts: list[QuestionnaireSourceAttempt] = Field(default_factory=list)
    next_actions: list[QuestionnaireSourceNextAction] = Field(
        default_factory=list
    )
    selection_source_ids: list[str] = Field(default_factory=list)
    selection_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    conflicts: list[QuestionnaireSourceConflict] = Field(default_factory=list)
    failure_reason: QuestionnaireSourceFailureReason | None = None
    response_only_confirmed: bool = False

    @model_validator(mode="after")
    def validate_workflow_state(self) -> "QuestionnaireSourceWorkflowResult":
        if len(self.next_actions) != len(set(self.next_actions)):
            raise ValueError("next_actions 不能重复")
        if len(self.selection_source_ids) != len(set(self.selection_source_ids)):
            raise ValueError("selection_source_ids 不能重复")
        if any(attempt.acquisition_route is None for attempt in self.attempts):
            raise ValueError("工作流 attempts 必须记录 acquisition_route")
        attempt_keys = [
            (attempt.acquisition_route, attempt.source_id)
            for attempt in self.attempts
        ]
        if len(attempt_keys) != len(set(attempt_keys)):
            raise ValueError("工作流 attempts 在同一获取路径不能重复")
        route_order = [
            _ACQUISITION_ROUTE_ORDER[attempt.acquisition_route]
            for attempt in self.attempts
        ]
        if route_order != sorted(route_order):
            raise ValueError("工作流 attempts 必须按获取路径顺序记录")
        active_statuses = {
            ProcessingStatus.PENDING,
            ProcessingStatus.ACQUIRING,
            ProcessingStatus.PARSING,
            ProcessingStatus.PROCESSING,
        }
        active_attempts = [
            attempt for attempt in self.attempts
            if attempt.status in active_statuses
        ]
        usable_attempts = [
            attempt for attempt in self.attempts
            if attempt.snapshot_id is not None
            and attempt.status
            in {
                ProcessingStatus.COMPLETED,
                ProcessingStatus.PARTIAL,
                ProcessingStatus.NEEDS_REVIEW,
            }
        ]
        if any(
            attempt.acquisition_route != self.route
            for attempt in usable_attempts
        ):
            raise ValueError("可用问卷快照必须来自当前最终获取路径")
        if (
            self.status != QuestionnaireSourceWorkflowStatus.IN_PROGRESS
            and active_attempts
        ):
            raise ValueError("存在进行中 attempt 时只能返回 in_progress")

        terminal_resolved = {
            QuestionnaireSourceWorkflowStatus.RESOLVED,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        }
        if self.status in terminal_resolved:
            if self.result is None:
                raise ValueError("resolved 状态必须包含问卷来源结果")
            expected = (
                QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL
                if self.result.partial_success
                else QuestionnaireSourceWorkflowStatus.RESOLVED
            )
            if self.status != expected:
                raise ValueError("resolved 状态必须与 result.partial_success 一致")
            if self.route in {None, QuestionnaireAcquisitionRoute.RESPONSE_ONLY}:
                raise ValueError("resolved 状态必须记录实际来源路径")
            if self.attempts != self.result.attempts:
                raise ValueError("resolved 状态的 attempts 必须与 result 一致")
            if not self.attempts or self.attempts[-1].acquisition_route != self.route:
                raise ValueError("resolved route 必须是最后执行路径")
            selected_attempts = [
                attempt
                for attempt in self.attempts
                if attempt.acquisition_route == self.route
                and attempt.source_id in self.result.selected_source_ids
                and attempt.snapshot_id == self.result.snapshot.snapshot_id
                and attempt.source_mode == self.result.snapshot.source_mode
                and attempt.status
                in {
                    ProcessingStatus.COMPLETED,
                    ProcessingStatus.PARTIAL,
                    ProcessingStatus.NEEDS_REVIEW,
                }
            ]
            if (
                len(selected_attempts) != len(self.result.selected_source_ids)
                or {
                    attempt.source_id for attempt in selected_attempts
                }
                != set(self.result.selected_source_ids)
            ):
                raise ValueError("resolved route 必须来自最终选中来源")
            if (
                self.next_actions
                or self.selection_source_ids
                or self.selection_token is not None
                or self.conflicts
                or self.response_only_confirmed
                or self.failure_reason is not None
            ):
                raise ValueError("resolved 状态不能同时要求后续动作或降级")
            return self

        if self.result is not None:
            raise ValueError("非 resolved 状态不能包含问卷来源结果")

        if self.status == QuestionnaireSourceWorkflowStatus.IN_PROGRESS:
            if self.route in {None, QuestionnaireAcquisitionRoute.RESPONSE_ONLY}:
                raise ValueError("in_progress 状态必须记录当前来源路径")
            if (
                not active_attempts
                or any(
                    attempt.acquisition_route != self.route
                    for attempt in active_attempts
                )
                or self.attempts[-1].acquisition_route != self.route
            ):
                raise ValueError("in_progress 状态必须包含进行中的 attempt")
            if (
                self.next_actions
                or self.selection_source_ids
                or self.selection_token is not None
                or self.conflicts
                or self.failure_reason is not None
                or self.response_only_confirmed
            ):
                raise ValueError("in_progress 状态不能同时要求人工动作")
            return self

        if self.status == QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED:
            if self.route in {None, QuestionnaireAcquisitionRoute.RESPONSE_ONLY}:
                raise ValueError("selection_required 必须记录候选来源路径")
            if len(self.selection_source_ids) < 2:
                raise ValueError("selection_required 至少需要两个来源")
            if self.selection_token is None:
                raise ValueError("selection_required 必须绑定候选内容令牌")
            if not self.attempts or self.attempts[-1].acquisition_route != self.route:
                raise ValueError("selection_required route 必须是最后执行路径")
            attempt_ids = {
                attempt.source_id
                for attempt in self.attempts
                if attempt.acquisition_route == self.route
                and attempt.snapshot_id is not None
                and attempt.status
                in {
                    ProcessingStatus.COMPLETED,
                    ProcessingStatus.PARTIAL,
                    ProcessingStatus.NEEDS_REVIEW,
                }
            }
            if set(self.selection_source_ids) != attempt_ids:
                raise ValueError("待选来源必须等于当前路径的可用 attempts")
            if any(
                candidate.source_id not in attempt_ids
                for conflict in self.conflicts
                for candidate in conflict.candidates
            ):
                raise ValueError("选择冲突只能引用当前待选来源")
            if self.next_actions != [QuestionnaireSourceNextAction.SELECT_SOURCE]:
                raise ValueError("selection_required 只能要求 select_source")
            if self.failure_reason is not None or self.response_only_confirmed:
                raise ValueError("selection_required 不能同时标记失败或跳过")
            return self

        if self.status == QuestionnaireSourceWorkflowStatus.SKIPPED:
            if self.route != QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
                raise ValueError("skipped 状态必须使用 response_only 路径")
            if not self.response_only_confirmed:
                raise ValueError("response-only 必须由用户明确确认")
            if (
                self.next_actions
                or self.selection_source_ids
                or self.selection_token is not None
                or self.conflicts
                or self.attempts
                or self.failure_reason is not None
            ):
                raise ValueError("skipped 状态不能包含快照选择或后续动作")
            return self

        if self.response_only_confirmed:
            raise ValueError("只有 skipped 状态可以确认 response-only")
        if (
            self.selection_source_ids
            or self.selection_token is not None
            or self.conflicts
        ):
            raise ValueError("只有 selection_required 可以包含待选来源")

        route_failure_reasons = [
            attempt.failure_reason
            or QuestionnaireSourceFailureReason.UNKNOWN
            for attempt in self.attempts
            if attempt.acquisition_route == self.route
            and attempt.status == ProcessingStatus.FAILED
        ]
        if not route_failure_reasons:
            route_failure_reasons = [QuestionnaireSourceFailureReason.UNKNOWN]
        expected_failure_reason = min(
            route_failure_reasons,
            key=_FAILURE_REASON_ORDER.__getitem__,
        )

        if self.status in {
            QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
            QuestionnaireSourceWorkflowStatus.CLOSED_PUBLIC_PAGE,
        }:
            if usable_attempts:
                raise ValueError("存在可用快照时不能进入等待失败状态")
            if not self.next_actions:
                raise ValueError("等待用户处理时必须至少提供一个下一步动作")
            if self.failure_reason is None:
                raise ValueError("等待用户处理时必须包含稳定 failure_reason")
            if self.failure_reason != expected_failure_reason:
                raise ValueError("failure_reason 必须由最终路径 attempts 确定")
            if self.route == QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
                raise ValueError("等待状态不能使用 response_only 路径")
            if not self.attempts and self.route is not None:
                raise ValueError("无 attempts 时不能声明已执行来源路径")
            if self.attempts and self.route != self.attempts[-1].acquisition_route:
                raise ValueError("等待状态 route 必须对应最后执行的来源路径")
            if QuestionnaireSourceNextAction.SELECT_SOURCE in self.next_actions:
                raise ValueError("select_source 只能用于 selection_required")
            applicable = {
                action
                for reason in route_failure_reasons
                for action in _NEXT_ACTIONS_BY_FAILURE_REASON[reason]
            }
            if any(action not in applicable for action in self.next_actions):
                raise ValueError("next_actions 与 failure_reason 的语义不一致")
            if (
                self.status
                == QuestionnaireSourceWorkflowStatus.CLOSED_PUBLIC_PAGE
            ):
                if (
                    self.route != QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
                    or self.failure_reason
                    != QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
                ):
                    raise ValueError("closed_public_page 状态与失败原因不一致")
            elif (
                self.route == QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
                and self.failure_reason
                == QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
            ):
                raise ValueError("关闭发布页必须使用 closed_public_page 状态")
            return self

        if self.status == QuestionnaireSourceWorkflowStatus.FAILED:
            if usable_attempts:
                raise ValueError("存在可用快照时不能返回 failed")
            if self.next_actions:
                raise ValueError("failed 状态不能包含可用的下一步动作")
            if self.failure_reason is None:
                raise ValueError("failed 状态必须包含稳定 failure_reason")
            if self.failure_reason != expected_failure_reason:
                raise ValueError("failure_reason 必须由最终路径 attempts 确定")
            if self.route == QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
                raise ValueError("failed 状态不能使用 response_only 路径")
            if not self.attempts and self.route is not None:
                raise ValueError("无 attempts 时不能声明已执行来源路径")
            if self.attempts and self.route != self.attempts[-1].acquisition_route:
                raise ValueError("failed 状态 route 必须对应最后执行的来源路径")
            return self

        raise ValueError("不支持的问卷来源工作流状态")
