"""问卷来源降级工作流的安全 HTTP 请求与响应契约。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StringConstraints, model_validator

from app.schemas.questionnaire import QuestionnaireSourceMode
from app.schemas.questionnaire_source_api import QuestionnaireSnapshotSummary
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireSourceFailureReason,
    QuestionnaireSourceNextAction,
    QuestionnaireSourceWorkflowStatus,
    questionnaire_source_priority,
    validate_acquisition_provenance,
)
from app.schemas.research_assets import (
    ContractModel,
    ImportErrorCode,
    ProcessingStatus,
)


_OpaqueIdentifier = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=512),
]
_OpaqueSelectionToken = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=512),
]
_SafeCode = Annotated[
    str,
    StringConstraints(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
_SafeFieldPath = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=1024),
]

_ACTIVE_ATTEMPT_STATUSES = {
    ProcessingStatus.PENDING,
    ProcessingStatus.ACQUIRING,
    ProcessingStatus.PARSING,
    ProcessingStatus.PROCESSING,
}
_USABLE_ATTEMPT_STATUSES = {
    ProcessingStatus.COMPLETED,
    ProcessingStatus.PARTIAL,
    ProcessingStatus.NEEDS_REVIEW,
}
_RESOLVED_WORKFLOW_STATUSES = {
    QuestionnaireSourceWorkflowStatus.RESOLVED,
    QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
}
_WAITING_WORKFLOW_STATUSES = {
    QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
    QuestionnaireSourceWorkflowStatus.CLOSED_PUBLIC_PAGE,
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
_ACQUISITION_ROUTE_ORDER = {
    QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT: 1,
    QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION: 2,
    QuestionnaireAcquisitionRoute.SNAPSHOT_UPLOAD: 3,
    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD: 4,
    QuestionnaireAcquisitionRoute.PUBLISHED_PAGE: 5,
}


class QuestionnaireSourceWorkflowRunRequest(ContractModel):
    """只允许用户确认选择或显式进入 response-only 的请求。"""

    schema_version: Literal[1] = 1
    selected_source_id: _OpaqueIdentifier | None = None
    selection_token: _OpaqueSelectionToken | None = None
    response_only: StrictBool = False

    @model_validator(mode="after")
    def validate_user_decision(self) -> "QuestionnaireSourceWorkflowRunRequest":
        if (self.selected_source_id is None) != (self.selection_token is None):
            raise ValueError(
                "selected_source_id 与 selection_token 必须同时提供"
            )
        if self.response_only and self.selected_source_id is not None:
            raise ValueError("response-only 不能同时选择问卷来源")
        return self


class QuestionnaireSourceAttemptSummary(ContractModel):
    """不包含原始文案、定位信息或提供方载荷的来源尝试摘要。"""

    acquisition_route: QuestionnaireAcquisitionRoute
    source_id: _OpaqueIdentifier
    source_mode: QuestionnaireSourceMode
    priority: int = Field(ge=1)
    status: ProcessingStatus
    snapshot_id: _OpaqueIdentifier | None = None
    failure_reason: QuestionnaireSourceFailureReason | None = None
    warning_codes: list[_SafeCode] = Field(default_factory=list)
    issue_codes: list[ImportErrorCode] = Field(default_factory=list)
    retryable: StrictBool = False

    @model_validator(mode="after")
    def validate_attempt(self) -> "QuestionnaireSourceAttemptSummary":
        if self.acquisition_route == QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
            raise ValueError("response_only 不能作为来源 attempt")
        validate_acquisition_provenance(
            self.acquisition_route,
            self.source_mode,
        )
        expected_priority = questionnaire_source_priority(self.source_mode)
        if self.priority != expected_priority:
            raise ValueError(
                f"{self.source_mode.value} 的 priority 必须使用固定级别 "
                f"{expected_priority}"
            )
        if len(self.warning_codes) != len(set(self.warning_codes)):
            raise ValueError("warning_codes 不能重复")
        if len(self.issue_codes) != len(set(self.issue_codes)):
            raise ValueError("issue_codes 不能重复")
        if self.status == ProcessingStatus.FAILED:
            if self.failure_reason is None or not self.issue_codes:
                raise ValueError("failed attempt 必须包含失败原因和 issue code")
        elif self.failure_reason is not None:
            raise ValueError("只有 failed attempt 可以包含 failure_reason")
        if self.retryable and not self.issue_codes:
            raise ValueError("retryable attempt 必须包含 issue code")
        if (
            self.status == ProcessingStatus.COMPLETED
            and self.snapshot_id is None
        ):
            raise ValueError("completed attempt 必须包含 snapshot_id")
        return self


class QuestionnaireSourceSelectionOption(ContractModel):
    """需要用户选择时可公开的候选身份，不包含候选内容。"""

    source_id: _OpaqueIdentifier
    source_mode: QuestionnaireSourceMode
    priority: int = Field(ge=1)
    status: ProcessingStatus
    snapshot_id: _OpaqueIdentifier

    @model_validator(mode="after")
    def validate_selectable(self) -> "QuestionnaireSourceSelectionOption":
        expected_priority = questionnaire_source_priority(self.source_mode)
        if self.priority != expected_priority:
            raise ValueError(
                f"{self.source_mode.value} 的 priority 必须使用固定级别 "
                f"{expected_priority}"
            )
        if self.status not in _USABLE_ATTEMPT_STATUSES:
            raise ValueError("待选来源必须是可用的问卷快照")
        return self


class QuestionnaireSourceConflictSummary(ContractModel):
    """仅公开冲突位置和候选身份，不公开候选值或解释文案。"""

    conflict_id: _OpaqueIdentifier
    field_path: _SafeFieldPath
    candidate_source_ids: list[_OpaqueIdentifier] = Field(min_length=2)
    suggested_source_id: _OpaqueIdentifier
    blocking: StrictBool = False

    @model_validator(mode="after")
    def validate_candidates(self) -> "QuestionnaireSourceConflictSummary":
        if len(self.candidate_source_ids) != len(
            set(self.candidate_source_ids)
        ):
            raise ValueError("candidate_source_ids 不能重复")
        if self.suggested_source_id not in self.candidate_source_ids:
            raise ValueError("suggested_source_id 必须来自候选来源")
        return self


class QuestionnaireSourceWorkflowApiResponse(ContractModel):
    """可直接返回给浏览器、且不泄露持久化或提供方细节的工作流状态。"""

    schema_version: Literal[1] = 1
    status: QuestionnaireSourceWorkflowStatus
    route: QuestionnaireAcquisitionRoute | None = None
    snapshot: QuestionnaireSnapshotSummary | None = None
    attempts: list[QuestionnaireSourceAttemptSummary] = Field(
        default_factory=list
    )
    next_actions: list[QuestionnaireSourceNextAction] = Field(
        default_factory=list
    )
    selection_options: list[QuestionnaireSourceSelectionOption] = Field(
        default_factory=list
    )
    selection_token: _OpaqueSelectionToken | None = None
    conflicts: list[QuestionnaireSourceConflictSummary] = Field(
        default_factory=list
    )
    failure_reason: QuestionnaireSourceFailureReason | None = None
    response_only_confirmed: StrictBool = False
    selected_source_ids: list[_OpaqueIdentifier] = Field(default_factory=list)
    partial_success: StrictBool = False

    @model_validator(mode="after")
    def validate_workflow_state(self) -> "QuestionnaireSourceWorkflowApiResponse":
        self._validate_unique_and_ordered_values()

        active_attempts = [
            attempt
            for attempt in self.attempts
            if attempt.status in _ACTIVE_ATTEMPT_STATUSES
        ]
        usable_attempts = [
            attempt
            for attempt in self.attempts
            if attempt.snapshot_id is not None
            and attempt.status in _USABLE_ATTEMPT_STATUSES
        ]
        if (
            self.status != QuestionnaireSourceWorkflowStatus.IN_PROGRESS
            and active_attempts
        ):
            raise ValueError("存在进行中 attempt 时只能返回 in_progress")
        if any(
            attempt.acquisition_route != self.route
            for attempt in usable_attempts
        ):
            raise ValueError("可用问卷快照必须来自当前工作流路径")

        if self.status in _RESOLVED_WORKFLOW_STATUSES:
            self._validate_resolved()
            return self
        if self.status == QuestionnaireSourceWorkflowStatus.IN_PROGRESS:
            self._validate_in_progress(active_attempts)
            return self
        if self.status == QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED:
            self._validate_selection_required()
            return self
        if self.status == QuestionnaireSourceWorkflowStatus.SKIPPED:
            self._validate_skipped()
            return self
        if self.status in _WAITING_WORKFLOW_STATUSES:
            self._validate_waiting(usable_attempts)
            return self
        if self.status == QuestionnaireSourceWorkflowStatus.FAILED:
            self._validate_failed(usable_attempts)
            return self
        raise ValueError("不支持的问卷来源工作流状态")

    def _validate_unique_and_ordered_values(self) -> None:
        if len(self.next_actions) != len(set(self.next_actions)):
            raise ValueError("next_actions 不能重复")
        if len(self.selected_source_ids) != len(
            set(self.selected_source_ids)
        ):
            raise ValueError("selected_source_ids 不能重复")

        attempt_keys = [
            (attempt.acquisition_route, attempt.source_id)
            for attempt in self.attempts
        ]
        if len(attempt_keys) != len(set(attempt_keys)):
            raise ValueError("attempts 在同一获取路径不能重复 source_id")
        route_order = [
            _ACQUISITION_ROUTE_ORDER[attempt.acquisition_route]
            for attempt in self.attempts
        ]
        if route_order != sorted(route_order):
            raise ValueError("attempts 必须按获取路径顺序记录")

        option_ids = [option.source_id for option in self.selection_options]
        if len(option_ids) != len(set(option_ids)):
            raise ValueError("selection_options 不能重复 source_id")
        conflict_ids = [conflict.conflict_id for conflict in self.conflicts]
        if len(conflict_ids) != len(set(conflict_ids)):
            raise ValueError("conflicts 不能重复 conflict_id")

    def _validate_resolved(self) -> None:
        if self.route in {None, QuestionnaireAcquisitionRoute.RESPONSE_ONLY}:
            raise ValueError("resolved 状态必须记录实际来源路径")
        if self.snapshot is None or not self.selected_source_ids:
            raise ValueError("resolved 状态必须包含快照摘要和选中来源")
        if not self.attempts or self.attempts[-1].acquisition_route != self.route:
            raise ValueError("resolved route 必须是最后执行路径")

        expected_partial = (
            self.status
            == QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL
        )
        if self.partial_success != expected_partial:
            raise ValueError("resolved 状态必须与 partial_success 一致")
        if not self.partial_success and any(
            attempt.status != ProcessingStatus.COMPLETED
            or bool(attempt.warning_codes)
            or bool(attempt.issue_codes)
            for attempt in self.attempts
        ):
            raise ValueError("来源降级或非完整 attempt 必须标记 partial_success")
        selected_attempts = [
            attempt
            for attempt in self.attempts
            if attempt.acquisition_route == self.route
            and attempt.source_id in self.selected_source_ids
            and attempt.snapshot_id == self.snapshot.snapshot_id
            and attempt.source_mode == self.snapshot.source_mode
            and attempt.status in _USABLE_ATTEMPT_STATUSES
        ]
        if {
            attempt.source_id for attempt in selected_attempts
        } != set(self.selected_source_ids):
            raise ValueError("选中来源必须与最终快照摘要一致")
        if (
            self.next_actions
            or self.selection_options
            or self.selection_token is not None
            or self.conflicts
            or self.failure_reason is not None
            or self.response_only_confirmed
        ):
            raise ValueError("resolved 状态不能同时要求后续动作或降级")

    def _validate_in_progress(
        self,
        active_attempts: list[QuestionnaireSourceAttemptSummary],
    ) -> None:
        if self.route in {None, QuestionnaireAcquisitionRoute.RESPONSE_ONLY}:
            raise ValueError("in_progress 状态必须记录当前来源路径")
        if (
            not active_attempts
            or any(
                attempt.acquisition_route != self.route
                for attempt in active_attempts
            )
            or not self.attempts
            or self.attempts[-1].acquisition_route != self.route
        ):
            raise ValueError("in_progress 状态必须包含当前路径的进行中 attempt")
        if (
            self.snapshot is not None
            or self.next_actions
            or self.selection_options
            or self.selection_token is not None
            or self.conflicts
            or self.failure_reason is not None
            or self.response_only_confirmed
            or self.selected_source_ids
            or self.partial_success
        ):
            raise ValueError("in_progress 状态不能包含最终结果或人工动作")

    def _validate_selection_required(self) -> None:
        if self.route in {None, QuestionnaireAcquisitionRoute.RESPONSE_ONLY}:
            raise ValueError("selection_required 必须记录候选来源路径")
        if len(self.selection_options) < 2 or self.selection_token is None:
            raise ValueError("selection_required 必须包含候选和选择令牌")
        if self.next_actions != [QuestionnaireSourceNextAction.SELECT_SOURCE]:
            raise ValueError("selection_required 只能要求 select_source")
        if not self.attempts or self.attempts[-1].acquisition_route != self.route:
            raise ValueError("selection_required route 必须是最后执行路径")

        selectable_attempts = {
            attempt.source_id: attempt
            for attempt in self.attempts
            if attempt.acquisition_route == self.route
            and attempt.snapshot_id is not None
            and attempt.status in _USABLE_ATTEMPT_STATUSES
        }
        options = {option.source_id: option for option in self.selection_options}
        if options.keys() != selectable_attempts.keys():
            raise ValueError("selection_options 必须等于当前路径的可用 attempts")
        for source_id, option in options.items():
            attempt = selectable_attempts[source_id]
            if (
                option.source_mode != attempt.source_mode
                or option.priority != attempt.priority
                or option.status != attempt.status
                or option.snapshot_id != attempt.snapshot_id
            ):
                raise ValueError("selection option 必须与来源 attempt 一致")
        if any(
            candidate_id not in options
            for conflict in self.conflicts
            for candidate_id in conflict.candidate_source_ids
        ):
            raise ValueError("冲突只能引用当前待选来源")
        if (
            self.snapshot is not None
            or self.failure_reason is not None
            or self.response_only_confirmed
            or self.selected_source_ids
            or self.partial_success
        ):
            raise ValueError("selection_required 不能包含最终结果或降级")

    def _validate_skipped(self) -> None:
        if self.route != QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
            raise ValueError("skipped 状态必须使用 response_only 路径")
        if not self.response_only_confirmed:
            raise ValueError("response-only 必须由用户明确确认")
        if (
            self.snapshot is not None
            or self.attempts
            or self.next_actions
            or self.selection_options
            or self.selection_token is not None
            or self.conflicts
            or self.failure_reason is not None
            or self.selected_source_ids
            or self.partial_success
        ):
            raise ValueError("skipped 状态不能包含快照来源或后续动作")

    def _validate_waiting(
        self,
        usable_attempts: list[QuestionnaireSourceAttemptSummary],
    ) -> None:
        self._validate_unresolved_common(usable_attempts)
        if not self.next_actions or self.failure_reason is None:
            raise ValueError("等待状态必须包含失败原因和下一步动作")
        if self.failure_reason != self._expected_failure_reason():
            raise ValueError("failure_reason 必须由最终路径 attempts 确定")
        if QuestionnaireSourceNextAction.SELECT_SOURCE in self.next_actions:
            raise ValueError("select_source 只能用于 selection_required")
        failure_reasons = [
            attempt.failure_reason
            or QuestionnaireSourceFailureReason.UNKNOWN
            for attempt in self.attempts
            if attempt.acquisition_route == self.route
            and attempt.status == ProcessingStatus.FAILED
        ] or [QuestionnaireSourceFailureReason.UNKNOWN]
        applicable_actions = {
            action
            for reason in failure_reasons
            for action in _NEXT_ACTIONS_BY_FAILURE_REASON[reason]
        }
        if any(action not in applicable_actions for action in self.next_actions):
            raise ValueError("next_actions 与最终路径的失败原因不一致")
        if self.status == QuestionnaireSourceWorkflowStatus.CLOSED_PUBLIC_PAGE:
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

    def _validate_failed(
        self,
        usable_attempts: list[QuestionnaireSourceAttemptSummary],
    ) -> None:
        self._validate_unresolved_common(usable_attempts)
        if self.next_actions:
            raise ValueError("failed 状态不能包含下一步动作")
        if self.failure_reason is None:
            raise ValueError("failed 状态必须包含失败原因")
        if self.failure_reason != self._expected_failure_reason():
            raise ValueError("failure_reason 必须由最终路径 attempts 确定")

    def _expected_failure_reason(self) -> QuestionnaireSourceFailureReason:
        reasons = [
            attempt.failure_reason
            or QuestionnaireSourceFailureReason.UNKNOWN
            for attempt in self.attempts
            if attempt.acquisition_route == self.route
            and attempt.status == ProcessingStatus.FAILED
        ]
        if not reasons:
            return QuestionnaireSourceFailureReason.UNKNOWN
        return min(reasons, key=_FAILURE_REASON_ORDER.__getitem__)

    def _validate_unresolved_common(
        self,
        usable_attempts: list[QuestionnaireSourceAttemptSummary],
    ) -> None:
        if usable_attempts:
            raise ValueError("存在可用快照时不能返回未解决状态")
        if self.route == QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
            raise ValueError("未解决状态不能使用 response_only 路径")
        if not self.attempts and self.route is not None:
            raise ValueError("无 attempts 时不能声明已执行来源路径")
        if self.attempts and self.route != self.attempts[-1].acquisition_route:
            raise ValueError("未解决 route 必须对应最后执行路径")
        if (
            self.snapshot is not None
            or self.selection_options
            or self.selection_token is not None
            or self.conflicts
            or self.response_only_confirmed
            or self.selected_source_ids
            or self.partial_success
        ):
            raise ValueError("未解决状态不能包含快照结果、选择或降级")
