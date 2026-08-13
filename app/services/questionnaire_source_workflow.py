"""问卷来源的惰性降级编排，不执行授权或平台写操作。"""

from __future__ import annotations

import asyncio
import hmac
import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from itertools import groupby
from typing import TypeAlias

from app.core.research_assets import structured_sha256
from app.schemas.questionnaire import QuestionnaireSourceMode
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireConflictResolution,
    QuestionnaireMergeCandidate,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceFailureReason,
    QuestionnaireSourceNextAction,
    QuestionnaireSourceResult,
    QuestionnaireSourceWorkflowResult,
    QuestionnaireSourceWorkflowStatus,
    questionnaire_source_priority,
    validate_acquisition_provenance,
)
from app.schemas.research_assets import (
    ImportErrorCode,
    ImportIssue,
    ImportWarning,
    ProcessingStatus,
)
from app.services.questionnaire_source_service import (
    _conflicts as compare_questionnaire_source_conflicts,
    QuestionnaireSourceScopeError,
    QuestionnaireSourceUnavailableError,
    resolve_questionnaire_sources,
)


QuestionnaireSourceLoader: TypeAlias = Callable[
    [],
    QuestionnaireMergeCandidate
    | Awaitable[QuestionnaireMergeCandidate],
]


_ROUTE_ORDER = {
    QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT: 1,
    QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION: 2,
    QuestionnaireAcquisitionRoute.SNAPSHOT_UPLOAD: 3,
    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD: 4,
    QuestionnaireAcquisitionRoute.PUBLISHED_PAGE: 5,
}
_ACTION_ORDER = {
    action: index
    for index, action in enumerate(
        (
            QuestionnaireSourceNextAction.RETRY_SOURCE,
            QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
            QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
            QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
            QuestionnaireSourceNextAction.RETRY_PUBLISHED_PAGE,
            QuestionnaireSourceNextAction.TEMPORARILY_REOPEN_AND_RETRY,
            QuestionnaireSourceNextAction.SELECT_SOURCE,
            QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
        )
    )
}
_IN_PROGRESS_STATUSES = {
    ProcessingStatus.PENDING,
    ProcessingStatus.ACQUIRING,
    ProcessingStatus.PARSING,
    ProcessingStatus.PROCESSING,
}
_INVALID_INPUT_CODES = {
    ImportErrorCode.UNSUPPORTED_TYPE,
    ImportErrorCode.TOO_LARGE,
    ImportErrorCode.PARSE_FAILED,
    ImportErrorCode.MAPPING_CONFLICT,
    ImportErrorCode.INTEGRITY_ERROR,
    ImportErrorCode.INVALID_SOURCE,
}
_REASON_ORDER = {
    QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE: 0,
    QuestionnaireSourceFailureReason.LOGIN_REQUIRED: 1,
    QuestionnaireSourceFailureReason.PERMISSION_REQUIRED: 2,
    QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER: 3,
    QuestionnaireSourceFailureReason.INVALID_INPUT: 4,
    QuestionnaireSourceFailureReason.NOT_FOUND: 5,
    QuestionnaireSourceFailureReason.UNKNOWN: 6,
}
_ACTIONS_BY_REASON = {
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


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceStep:
    """一个带安全身份声明的惰性来源步骤。"""

    route: QuestionnaireAcquisitionRoute
    source_id: str
    source_mode: QuestionnaireSourceMode
    owner_ref: str
    load: QuestionnaireSourceLoader

    def __post_init__(self) -> None:
        if not isinstance(self.route, QuestionnaireAcquisitionRoute):
            raise TypeError("route 类型无效")
        if self.route == QuestionnaireAcquisitionRoute.RESPONSE_ONLY:
            raise ValueError("response_only 不是可执行的来源步骤")
        if not isinstance(self.source_mode, QuestionnaireSourceMode):
            raise TypeError("source_mode 类型无效")
        for value, label in (
            (self.source_id, "source_id"),
            (self.owner_ref, "owner_ref"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} 不能为空")
            if value != value.strip():
                raise ValueError(f"{label} 不能包含首尾空白")
        if not callable(self.load):
            raise TypeError("load 必须可调用")
        validate_acquisition_provenance(self.route, self.source_mode)


class QuestionnaireSourceAcquisitionError(RuntimeError):
    """来源适配器显式给出的安全失败，不包含底层异常文本。"""

    def __init__(
        self,
        issue: ImportIssue,
        *,
        reason: QuestionnaireSourceFailureReason | None = None,
    ) -> None:
        if not isinstance(issue, ImportIssue):
            raise TypeError("issue 必须是 ImportIssue")
        if reason is not None and not isinstance(
            reason,
            QuestionnaireSourceFailureReason,
        ):
            raise TypeError("reason 类型无效")
        super().__init__("问卷来源获取失败")
        self.issue = issue
        self.reason = reason


def _require_owner(owner_ref: str) -> str:
    if not isinstance(owner_ref, str) or not owner_ref.strip():
        raise ValueError("owner_ref 不能为空")
    if owner_ref != owner_ref.strip():
        raise ValueError("owner_ref 不能包含首尾空白")
    return owner_ref


def _reason_from_issue(
    issue: ImportIssue,
) -> QuestionnaireSourceFailureReason:
    if issue.code == ImportErrorCode.LOGIN_REQUIRED:
        return QuestionnaireSourceFailureReason.LOGIN_REQUIRED
    if issue.code == ImportErrorCode.PERMISSION_REQUIRED:
        return QuestionnaireSourceFailureReason.PERMISSION_REQUIRED
    if issue.code in {ImportErrorCode.NOT_FOUND, ImportErrorCode.DELETED}:
        return QuestionnaireSourceFailureReason.NOT_FOUND
    if issue.retryable:
        return QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER
    if issue.code in _INVALID_INPUT_CODES:
        return QuestionnaireSourceFailureReason.INVALID_INPUT
    return QuestionnaireSourceFailureReason.UNKNOWN


def _reason_from_issues(
    issues: list[ImportIssue],
) -> QuestionnaireSourceFailureReason:
    if not issues:
        return QuestionnaireSourceFailureReason.UNKNOWN
    reasons = [_reason_from_issue(issue) for issue in issues]
    return min(reasons, key=_REASON_ORDER.__getitem__)


def _generic_issue() -> ImportIssue:
    return ImportIssue(
        code=ImportErrorCode.PROVIDER_ERROR,
        message="问卷来源获取失败，未使用不完整结果",
        retryable=False,
        suggested_action=(
            "请重试其他可用来源、上传问卷，"
            "或明确仅分析回答数据"
        ),
    )


def _failed_candidate(
    step: QuestionnaireSourceStep,
    issue: ImportIssue,
) -> QuestionnaireMergeCandidate:
    return QuestionnaireMergeCandidate(
        source_id=step.source_id,
        source_mode=step.source_mode,
        priority=questionnaire_source_priority(step.source_mode),
        status=ProcessingStatus.FAILED,
        issues=[issue],
    )


async def _load_step(
    step: QuestionnaireSourceStep,
    owner_ref: str,
) -> tuple[QuestionnaireMergeCandidate, QuestionnaireSourceFailureReason | None]:
    try:
        if inspect.iscoroutinefunction(step.load):
            loaded = step.load()
        else:
            loaded = await asyncio.to_thread(step.load)
        candidate = await loaded if inspect.isawaitable(loaded) else loaded
    except QuestionnaireSourceScopeError:
        raise
    except QuestionnaireSourceAcquisitionError as error:
        derived_reason = _reason_from_issue(error.issue)
        if error.reason is None or error.reason == derived_reason:
            return _failed_candidate(step, error.issue), derived_reason
        if (
            error.reason == QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
            and step.route == QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
            and derived_reason == QuestionnaireSourceFailureReason.UNKNOWN
        ):
            return (
                _failed_candidate(step, error.issue),
                QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE,
            )
        issue = _generic_issue()
        return _failed_candidate(step, issue), QuestionnaireSourceFailureReason.UNKNOWN
    except Exception:
        issue = _generic_issue()
        return _failed_candidate(step, issue), QuestionnaireSourceFailureReason.UNKNOWN

    if not isinstance(candidate, QuestionnaireMergeCandidate):
        raise ValueError("来源步骤返回类型无效")
    if (
        candidate.source_id != step.source_id
        or candidate.source_mode != step.source_mode
    ):
        raise ValueError("来源步骤身份与候选身份不一致")
    if candidate.collection is not None:
        if candidate.collection.owner_ref != owner_ref or any(
            source.owner_ref != owner_ref
            for source in candidate.collection.sources
        ):
            raise QuestionnaireSourceScopeError()
    if candidate.status == ProcessingStatus.FAILED:
        if candidate.snapshot is not None or candidate.collection is not None:
            raise ValueError("failed 来源候选不能包含快照聚合")
        issues = candidate.issues or [_generic_issue()]
        if issues != candidate.issues:
            candidate = candidate.model_copy(update={"issues": issues})
        return candidate, _reason_from_issues(issues)
    return candidate, None


def _attempt_from_candidate(
    candidate: QuestionnaireMergeCandidate,
    route: QuestionnaireAcquisitionRoute,
    reason: QuestionnaireSourceFailureReason | None,
) -> QuestionnaireSourceAttempt:
    return QuestionnaireSourceAttempt(
        source_id=candidate.source_id,
        source_mode=candidate.source_mode,
        priority=candidate.priority,
        acquisition_route=route,
        status=candidate.status,
        snapshot_id=(
            candidate.snapshot.snapshot_id
            if candidate.snapshot is not None
            else None
        ),
        failure_reason=(
            reason if candidate.status == ProcessingStatus.FAILED else None
        ),
        warnings=candidate.warnings,
        issues=candidate.issues,
    )


def _routed_attempt(
    attempt: QuestionnaireSourceAttempt,
    route: QuestionnaireAcquisitionRoute,
    reason: QuestionnaireSourceFailureReason | None,
) -> QuestionnaireSourceAttempt:
    if attempt.status == ProcessingStatus.FAILED and reason is None:
        reason = _reason_from_issues(attempt.issues)
    value = attempt.model_dump(mode="python")
    value["acquisition_route"] = route
    value["failure_reason"] = (
        reason if attempt.status == ProcessingStatus.FAILED else None
    )
    return QuestionnaireSourceAttempt.model_validate(value)


def _skipped_attempt(
    attempt: QuestionnaireSourceAttempt,
) -> QuestionnaireSourceAttempt:
    value = attempt.model_dump(mode="python")
    value.update({
        "status": ProcessingStatus.SKIPPED,
        "failure_reason": None,
        "warnings": [
            *attempt.warnings,
            ImportWarning(
                code="source_not_selected",
                message="同一获取路径已由用户明确选择其他候选",
            ),
        ],
    })
    return QuestionnaireSourceAttempt.model_validate(value)


def _resolved_conflicts(
    conflicts,
    selected_source_id: str,
):
    resolved = []
    for conflict in conflicts:
        selected = next(
            (
                candidate
                for candidate in conflict.candidates
                if candidate.source_id == selected_source_id
            ),
            None,
        )
        if selected is None:
            raise ValueError("冲突未包含用户选择的来源")
        resolved.append(conflict.model_copy(update={
            "resolution": QuestionnaireConflictResolution.USER_SELECTED,
            "selected_source_id": selected_source_id,
            "selected_value": selected.value,
        }))
    return resolved


def _canonical_actions(
    actions: Iterable[QuestionnaireSourceNextAction],
) -> list[QuestionnaireSourceNextAction]:
    values = list(actions)
    if any(not isinstance(action, QuestionnaireSourceNextAction) for action in values):
        raise TypeError("available_actions 包含无效类型")
    if len(values) != len(set(values)):
        raise ValueError("available_actions 不能重复")
    return sorted(values, key=_ACTION_ORDER.__getitem__)


def _selection_token(
    route: QuestionnaireAcquisitionRoute,
    candidates: list[QuestionnaireMergeCandidate],
) -> str:
    def stable_candidate(
        candidate: QuestionnaireMergeCandidate,
    ) -> dict[str, object]:
        value = candidate.model_dump(mode="json")
        snapshot = value.get("snapshot")
        if isinstance(snapshot, dict):
            snapshot.pop("retrieved_at", None)
        collection = value.get("collection")
        if isinstance(collection, dict):
            for source in collection.get("sources", []):
                if isinstance(source, dict):
                    source.pop("created_at", None)
            for document in collection.get("documents", []):
                if isinstance(document, dict):
                    document.pop("retrieved_at", None)
            for derivative in collection.get("derivatives", []):
                if isinstance(derivative, dict):
                    derivative.pop("created_at", None)
        return value

    return structured_sha256({
        "route": route.value,
        "candidates": [
            stable_candidate(candidate)
            for candidate in sorted(candidates, key=lambda item: item.source_id)
        ],
    })


def _next_actions(
    reasons: Iterable[QuestionnaireSourceFailureReason],
    available_actions: list[QuestionnaireSourceNextAction],
) -> list[QuestionnaireSourceNextAction]:
    applicable = {
        action
        for reason in reasons
        for action in _ACTIONS_BY_REASON[reason]
    }
    return [action for action in available_actions if action in applicable]


async def run_questionnaire_source_workflow(
    *,
    owner_ref: str,
    steps: Iterable[QuestionnaireSourceStep],
    available_actions: Iterable[QuestionnaireSourceNextAction] = (),
    selected_source_id: str | None = None,
    selection_token: str | None = None,
    response_only: bool = False,
) -> QuestionnaireSourceWorkflowResult:
    """按固定路径惰性尝试来源，并返回可序列化的安全状态。

    本函数不会执行 OAuth、上传、发布页抓取或恢复问卷收集；这些能力只能
    由调用方注入为步骤或明确声明为可用的用户动作。
    """
    owner = _require_owner(owner_ref)
    declared_steps = list(steps)
    actions = _canonical_actions(available_actions)
    if (selected_source_id is None) != (selection_token is None):
        raise ValueError("selected_source_id 与 selection_token 必须同时提供")

    identities: set[tuple[QuestionnaireAcquisitionRoute, str]] = set()
    for step in declared_steps:
        if not isinstance(step, QuestionnaireSourceStep):
            raise TypeError("steps 必须包含 QuestionnaireSourceStep")
        if step.owner_ref != owner:
            raise QuestionnaireSourceScopeError()
        identity = (step.route, step.source_id)
        if identity in identities:
            raise ValueError("同一获取路径不能包含重复 source_id")
        identities.add(identity)

    if response_only:
        if selected_source_id is not None or selection_token is not None:
            raise ValueError("response-only 不能同时选择问卷来源")
        if QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY not in actions:
            raise ValueError("当前能力未允许 response-only 降级")
        return QuestionnaireSourceWorkflowResult(
            status=QuestionnaireSourceWorkflowStatus.SKIPPED,
            route=QuestionnaireAcquisitionRoute.RESPONSE_ONLY,
            response_only_confirmed=True,
        )

    indexed = list(enumerate(declared_steps))
    indexed.sort(key=lambda item: (_ROUTE_ORDER[item[1].route], item[0]))
    attempts: list[QuestionnaireSourceAttempt] = []
    last_route: QuestionnaireAcquisitionRoute | None = None

    for route, grouped in groupby(indexed, key=lambda item: item[1].route):
        loaded_candidates: list[
            tuple[
                QuestionnaireMergeCandidate,
                QuestionnaireSourceFailureReason | None,
            ]
        ] = []
        for _, step in grouped:
            candidate, reason = await _load_step(step, owner)
            loaded_candidates.append((candidate, reason))
            last_route = route

        if any(
            candidate.status in _IN_PROGRESS_STATUSES
            for candidate, _ in loaded_candidates
        ):
            if selected_source_id is not None:
                raise ValueError("来源仍在处理中，不能提前选择候选")
            in_progress_attempts = [
                *attempts,
                *(
                    _attempt_from_candidate(
                        candidate,
                        route,
                        reason,
                    )
                    for candidate, reason in loaded_candidates
                ),
            ]
            return QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.IN_PROGRESS,
                route=route,
                attempts=in_progress_attempts,
            )

        usable: list[
            tuple[
                QuestionnaireMergeCandidate,
                QuestionnaireSourceResult,
                QuestionnaireSourceAttempt,
            ]
        ] = []
        route_attempts: list[QuestionnaireSourceAttempt] = []
        for candidate, explicit_reason in loaded_candidates:
            try:
                individual = resolve_questionnaire_sources(
                    [candidate],
                    owner_ref=owner,
                )
            except QuestionnaireSourceUnavailableError as error:
                if len(error.attempts) != 1:
                    raise ValueError("单一来源校验返回了异常 attempt 数量")
                routed = _routed_attempt(
                    error.attempts[0],
                    route,
                    explicit_reason,
                )
                route_attempts.append(routed)
                continue

            if len(individual.attempts) != 1:
                raise ValueError("单一来源结果包含异常 attempt 数量")
            routed = _routed_attempt(
                individual.attempts[0],
                route,
                None,
            )
            route_attempts.append(routed)
            usable.append((candidate, individual, routed))

        if not usable:
            attempts.extend(route_attempts)
            continue

        route_conflicts = []
        if len(usable) > 1:
            if QuestionnaireSourceNextAction.SELECT_SOURCE not in actions:
                raise ValueError("当前能力未允许显式选择同路径来源")
            usable.sort(key=lambda item: (
                item[0].priority,
                item[0].source_id,
            ))
            usable_candidates = [item[0] for item in usable]
            current_selection_token = _selection_token(
                route,
                usable_candidates,
            )
            route_conflicts = compare_questionnaire_source_conflicts(
                usable_candidates[0],
                usable_candidates,
            )

            selectable_ids = [item[0].source_id for item in usable]
            if selected_source_id is None:
                return QuestionnaireSourceWorkflowResult(
                    status=(
                        QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED
                    ),
                    route=route,
                    attempts=[*attempts, *route_attempts],
                    next_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
                    selection_source_ids=selectable_ids,
                    selection_token=current_selection_token,
                    conflicts=route_conflicts,
                )
            if selection_token is None:
                raise ValueError("选择来源时必须提供 selection_token")
            if not hmac.compare_digest(selection_token, current_selection_token):
                raise ValueError("待选来源已变化，请重新确认")
            if selected_source_id not in selectable_ids:
                raise ValueError("selected_source_id 不属于当前待选择来源")
        elif selected_source_id is not None or selection_token is not None:
            raise ValueError("待选来源已变化，请重新确认")

        selected_source = (
            selected_source_id
            if len(usable) > 1
            else usable[0][0].source_id
        )
        selected = next(
            item for item in usable if item[0].source_id == selected_source
        )
        selected_result = selected[1]
        usable_source_ids = {item[0].source_id for item in usable}
        final_route_attempts = [
            (
                attempt
                if len(usable) == 1
                or attempt.source_id == selected_source
                or attempt.source_id not in usable_source_ids
                or attempt.status
                not in {
                    ProcessingStatus.COMPLETED,
                    ProcessingStatus.PARTIAL,
                    ProcessingStatus.NEEDS_REVIEW,
                }
                else _skipped_attempt(attempt)
            )
            for attempt in route_attempts
        ]
        all_attempts = [*attempts, *final_route_attempts]
        resolved_conflicts = (
            _resolved_conflicts(route_conflicts, selected_source)
            if len(usable) > 1
            else []
        )
        workflow_partial = (
            selected_result.partial_success
            or bool(resolved_conflicts)
            or any(
                attempt.status
                in {
                    ProcessingStatus.PARTIAL,
                    ProcessingStatus.NEEDS_REVIEW,
                    ProcessingStatus.FAILED,
                    ProcessingStatus.SKIPPED,
                }
                or bool(attempt.warnings)
                or bool(attempt.issues)
                for attempt in all_attempts
                if not (
                    attempt.acquisition_route == route
                    and attempt.source_id == selected_source
                    and attempt.snapshot_id is not None
                )
            )
        )
        result_value = selected_result.model_dump(mode="python")
        result_value.update({
            "attempts": all_attempts,
            "conflicts": resolved_conflicts,
            "partial_success": workflow_partial,
        })
        final_result = QuestionnaireSourceResult.model_validate(result_value)
        return QuestionnaireSourceWorkflowResult(
            status=(
                QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL
                if workflow_partial
                else QuestionnaireSourceWorkflowStatus.RESOLVED
            ),
            route=route,
            result=final_result,
            attempts=all_attempts,
        )

    if selected_source_id is not None or selection_token is not None:
        raise ValueError("selected_source_id 未对应待选择状态")

    route_attempts = [
        attempt
        for attempt in attempts
        if attempt.acquisition_route == last_route
    ]
    route_reasons = [
        attempt.failure_reason
        for attempt in route_attempts
        if attempt.failure_reason is not None
    ]
    failure_reason = (
        min(route_reasons, key=_REASON_ORDER.__getitem__)
        if route_reasons
        else QuestionnaireSourceFailureReason.UNKNOWN
    )
    next_actions = _next_actions(
        route_reasons or [QuestionnaireSourceFailureReason.UNKNOWN],
        actions,
    )
    if not next_actions:
        return QuestionnaireSourceWorkflowResult(
            status=QuestionnaireSourceWorkflowStatus.FAILED,
            route=last_route,
            attempts=attempts,
            failure_reason=failure_reason,
        )
    status = (
        QuestionnaireSourceWorkflowStatus.CLOSED_PUBLIC_PAGE
        if (
            failure_reason
            == QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
            and last_route == QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
        )
        else QuestionnaireSourceWorkflowStatus.AWAITING_ACTION
    )
    return QuestionnaireSourceWorkflowResult(
        status=status,
        route=last_route,
        attempts=attempts,
        next_actions=next_actions,
        failure_reason=failure_reason,
    )
