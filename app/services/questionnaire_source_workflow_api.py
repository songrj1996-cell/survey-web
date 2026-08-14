"""问卷来源工作流的 owner-scoped 安全业务门面。"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import inspect
import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, runtime_checkable

from pydantic import ValidationError

from app.schemas.questionnaire_source_api import QuestionnaireSnapshotSummary
from app.schemas.questionnaire_source_workflow_api import (
    QuestionnaireSourceAttemptSummary,
    QuestionnaireSourceConflictSummary,
    QuestionnaireSourceSelectionOption,
    QuestionnaireSourceWorkflowApiResponse,
    QuestionnaireSourceWorkflowRunRequest,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireSourceAttempt,
    QuestionnaireSourceNextAction,
    QuestionnaireSourceWorkflowResult,
    QuestionnaireSourceWorkflowStatus,
)
from app.services.questionnaire_snapshot_api import _validated_summary
from app.services.questionnaire_source_materialization import (
    QuestionnaireSourceMaterializedStep,
    run_and_persist_questionnaire_source_workflow,
)
from app.services.questionnaire_source_service import (
    QuestionnaireSourceScopeError,
    load_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
)


_INTERNAL_SELECTION_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TOKEN_VERSION = "v1"
_TOKEN_DOMAIN = b"questionnaire-source-selection:v1"
_MIN_HMAC_SECRET_BYTES = 32
_MAX_PUBLIC_IDENTIFIER_BYTES = 512
_MAX_OWNER_REF_BYTES = 4096


class QuestionnaireSourceWorkflowApiError(RuntimeError):
    """HTTP 层可按类型安全映射、且不携带底层细节的错误。"""

    _safe_message = "问卷来源工作流处理失败"

    def __init__(self) -> None:
        super().__init__(self._safe_message)


class QuestionnaireSourceWorkflowInvalidError(
    QuestionnaireSourceWorkflowApiError
):
    """请求身份或请求模型不满足工作流接口约束。"""

    _safe_message = "问卷来源工作流请求无效"


class QuestionnaireSourceWorkflowNotFoundError(
    QuestionnaireSourceWorkflowApiError
):
    """当前 owner 范围内不存在目标服务端计划。"""

    _safe_message = "问卷来源计划不存在"


class QuestionnaireSourceWorkflowConflictError(
    QuestionnaireSourceWorkflowApiError
):
    """用户确认与当前服务端来源状态不再一致。"""

    _safe_message = "问卷来源工作流状态已变化"


class QuestionnaireSourceWorkflowInternalError(
    QuestionnaireSourceWorkflowApiError
):
    """不得向 HTTP 响应暴露细节的内部执行失败。"""


class QuestionnaireSourceSelectionTokenError(ValueError):
    """外部选择令牌无效；错误文本不包含令牌或绑定身份。"""

    def __init__(self) -> None:
        super().__init__("问卷来源选择令牌无效")


def _require_exact_identifier(
    value: object,
    *,
    max_bytes: int,
) -> str:
    if not isinstance(value, str) or not value or not value.strip():
        raise ValueError("标识不能为空")
    if value != value.strip():
        raise ValueError("标识不能包含首尾空白")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("标识包含无效字符") from error
    if len(encoded) > max_bytes:
        raise ValueError("标识超过长度限制")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("标识不能包含控制字符")
    return value


def _require_owner(owner_ref: object) -> str:
    return _require_exact_identifier(
        owner_ref,
        max_bytes=_MAX_OWNER_REF_BYTES,
    )


def _require_workflow_ref(workflow_ref: object) -> str:
    return _require_exact_identifier(
        workflow_ref,
        max_bytes=_MAX_PUBLIC_IDENTIFIER_BYTES,
    )


def _require_internal_selection_token(value: object) -> str:
    if not isinstance(value, str) or not _INTERNAL_SELECTION_TOKEN_RE.fullmatch(
        value
    ):
        raise QuestionnaireSourceSelectionTokenError()
    return value


def _require_external_selection_token(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise QuestionnaireSourceSelectionTokenError()
    return value


def _length_prefixed(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def _token_binding_message(
    owner_ref: str,
    workflow_ref: str,
    internal_token: bytes,
) -> bytes:
    return b"".join((
        _length_prefixed(_TOKEN_DOMAIN),
        _length_prefixed(owner_ref.encode("utf-8")),
        _length_prefixed(workflow_ref.encode("utf-8")),
        _length_prefixed(internal_token),
    ))


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    if not value or not _TOKEN_PART_RE.fullmatch(value):
        raise QuestionnaireSourceSelectionTokenError()
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise QuestionnaireSourceSelectionTokenError() from error
    if _base64url_encode(decoded) != value:
        raise QuestionnaireSourceSelectionTokenError()
    return decoded


@runtime_checkable
class QuestionnaireSourceSelectionTokenCodec(Protocol):
    """把领域选择令牌绑定到 owner 与服务端 workflow_ref。"""

    def encode(
        self,
        owner_ref: str,
        workflow_ref: str,
        internal_token: str,
    ) -> str:
        ...

    def decode(
        self,
        owner_ref: str,
        workflow_ref: str,
        external_token: str,
    ) -> str:
        ...


@dataclass(frozen=True, slots=True)
class HmacQuestionnaireSourceSelectionTokenCodec:
    """使用调用方注入密钥签名、但不暴露 owner 的无状态选择令牌。"""

    secret: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.secret, bytes):
            raise TypeError("secret 必须是 bytes")
        if len(self.secret) < _MIN_HMAC_SECRET_BYTES:
            raise ValueError("secret 至少需要 32 字节")
        object.__setattr__(self, "secret", bytes(self.secret))

    def encode(
        self,
        owner_ref: str,
        workflow_ref: str,
        internal_token: str,
    ) -> str:
        try:
            owner = _require_owner(owner_ref)
            workflow = _require_workflow_ref(workflow_ref)
            normalized_token = _require_internal_selection_token(
                internal_token
            )
        except (TypeError, ValueError) as error:
            raise QuestionnaireSourceSelectionTokenError() from error
        token_bytes = bytes.fromhex(normalized_token)
        signature = hmac.digest(
            self.secret,
            _token_binding_message(owner, workflow, token_bytes),
            hashlib.sha256,
        )
        return ".".join((
            _TOKEN_VERSION,
            _base64url_encode(token_bytes),
            _base64url_encode(signature),
        ))

    def decode(
        self,
        owner_ref: str,
        workflow_ref: str,
        external_token: str,
    ) -> str:
        try:
            owner = _require_owner(owner_ref)
            workflow = _require_workflow_ref(workflow_ref)
            token = _require_external_selection_token(external_token)
            version, encoded_token, encoded_signature = token.split(".")
            if version != _TOKEN_VERSION:
                raise QuestionnaireSourceSelectionTokenError()
            token_bytes = _base64url_decode(encoded_token)
            signature = _base64url_decode(encoded_signature)
            if (
                len(token_bytes) != 32
                or len(signature) != hashlib.sha256().digest_size
            ):
                raise QuestionnaireSourceSelectionTokenError()
            expected_signature = hmac.digest(
                self.secret,
                _token_binding_message(owner, workflow, token_bytes),
                hashlib.sha256,
            )
            if not hmac.compare_digest(signature, expected_signature):
                raise QuestionnaireSourceSelectionTokenError()
            return _require_internal_selection_token(token_bytes.hex())
        except QuestionnaireSourceSelectionTokenError:
            raise
        except (TypeError, ValueError) as error:
            raise QuestionnaireSourceSelectionTokenError() from error


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceWorkflowPlan:
    """只由服务端构造的 owner-bound 来源步骤与能力声明。"""

    workflow_ref: str
    owner_ref: str
    steps: tuple[QuestionnaireSourceMaterializedStep, ...]
    available_actions: tuple[QuestionnaireSourceNextAction, ...] = ()

    def __post_init__(self) -> None:
        workflow = _require_workflow_ref(self.workflow_ref)
        owner = _require_owner(self.owner_ref)
        try:
            steps = tuple(self.steps)
            actions = tuple(self.available_actions)
        except TypeError as error:
            raise TypeError("steps 与 available_actions 必须可迭代") from error

        identities: set[tuple[object, str]] = set()
        for step in steps:
            if not isinstance(step, QuestionnaireSourceMaterializedStep):
                raise TypeError(
                    "steps 必须包含 QuestionnaireSourceMaterializedStep"
                )
            if step.owner_ref != owner:
                raise QuestionnaireSourceScopeError()
            _require_exact_identifier(
                step.source_id,
                max_bytes=_MAX_PUBLIC_IDENTIFIER_BYTES,
            )
            identity = (step.route, step.source_id)
            if identity in identities:
                raise ValueError("同一获取路径不能包含重复 source_id")
            identities.add(identity)

        if any(
            not isinstance(action, QuestionnaireSourceNextAction)
            for action in actions
        ):
            raise TypeError("available_actions 包含无效类型")
        if len(actions) != len(set(actions)):
            raise ValueError("available_actions 不能重复")

        object.__setattr__(self, "workflow_ref", workflow)
        object.__setattr__(self, "owner_ref", owner)
        object.__setattr__(self, "steps", steps)
        object.__setattr__(self, "available_actions", actions)


QuestionnaireSourceWorkflowPlanValue: TypeAlias = (
    QuestionnaireSourceWorkflowPlan | None
)
QuestionnaireSourceWorkflowPlanResult: TypeAlias = (
    QuestionnaireSourceWorkflowPlanValue
    | Awaitable[QuestionnaireSourceWorkflowPlanValue]
)


@runtime_checkable
class QuestionnaireSourceWorkflowPlanProvider(Protocol):
    """按已认证 owner 和公开引用解析服务端计划的最小端口。"""

    def __call__(
        self,
        owner_ref: str,
        workflow_ref: str,
    ) -> QuestionnaireSourceWorkflowPlanResult:
        ...


def _attempt_summary(
    attempt: QuestionnaireSourceAttempt,
) -> QuestionnaireSourceAttemptSummary:
    if attempt.acquisition_route is None:
        raise ValueError("工作流 attempt 缺少 acquisition_route")
    return QuestionnaireSourceAttemptSummary(
        acquisition_route=attempt.acquisition_route,
        source_id=attempt.source_id,
        source_mode=attempt.source_mode,
        priority=attempt.priority,
        status=attempt.status,
        snapshot_id=attempt.snapshot_id,
        failure_reason=attempt.failure_reason,
        warning_codes=list(dict.fromkeys(
            warning.code for warning in attempt.warnings
        )),
        issue_codes=list(dict.fromkeys(
            issue.code for issue in attempt.issues
        )),
        retryable=any(issue.retryable for issue in attempt.issues),
    )


def _selection_options(
    workflow: QuestionnaireSourceWorkflowResult,
) -> list[QuestionnaireSourceSelectionOption]:
    if workflow.status != QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED:
        return []
    attempts = {
        attempt.source_id: attempt
        for attempt in workflow.attempts
        if attempt.acquisition_route == workflow.route
        and attempt.snapshot_id is not None
    }
    options: list[QuestionnaireSourceSelectionOption] = []
    for source_id in workflow.selection_source_ids:
        attempt = attempts.get(source_id)
        if attempt is None or attempt.snapshot_id is None:
            raise ValueError("待选来源缺少安全 attempt 摘要")
        options.append(QuestionnaireSourceSelectionOption(
            source_id=attempt.source_id,
            source_mode=attempt.source_mode,
            priority=attempt.priority,
            status=attempt.status,
            snapshot_id=attempt.snapshot_id,
        ))
    return options


def _selection_conflicts(
    workflow: QuestionnaireSourceWorkflowResult,
) -> list[QuestionnaireSourceConflictSummary]:
    if workflow.status != QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED:
        return []
    return [
        QuestionnaireSourceConflictSummary(
            conflict_id=conflict.conflict_id,
            field_path=conflict.field_path,
            candidate_source_ids=[
                candidate.source_id for candidate in conflict.candidates
            ],
            suggested_source_id=conflict.suggested_source_id,
            blocking=conflict.blocking,
        )
        for conflict in workflow.conflicts
    ]


def _load_persisted_summary(
    owner_ref: str,
    workflow: QuestionnaireSourceWorkflowResult,
    storage: ResearchSnapshotStorage,
) -> QuestionnaireSnapshotSummary:
    if workflow.result is None:
        raise QuestionnaireSourceWorkflowInternalError()
    expected_bundle = ResearchAssetBundle(
        workflow.result.snapshot,
        workflow.result.collection,
    )
    snapshot_id = workflow.result.snapshot.snapshot_id
    package = load_questionnaire_source_snapshot(
        owner_ref,
        snapshot_id,
        storage,
    )
    summary = _validated_summary(package, owner_ref, snapshot_id)
    if not isinstance(package, SnapshotPackage) or package.bundle != expected_bundle:
        raise QuestionnaireSourceWorkflowInternalError()
    return summary


async def _await_uncancelled(
    task: asyncio.Task[QuestionnaireSourceWorkflowApiResponse],
) -> None:
    """外层取消后等待计划、获取和持久化真实结束，再传播取消。"""
    current = asyncio.current_task()
    while not task.done():
        if current is not None and hasattr(current, "uncancel"):
            current.uncancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    try:
        if not task.cancelled():
            task.exception()
    except BaseException:
        pass


@dataclass(frozen=True, slots=True)
class QuestionnaireSourceWorkflowApi:
    """解析服务端计划、执行来源降级并返回最小安全投影。"""

    storage: ResearchSnapshotStorage
    plan_provider: QuestionnaireSourceWorkflowPlanProvider
    selection_token_codec: QuestionnaireSourceSelectionTokenCodec

    def __post_init__(self) -> None:
        if not isinstance(self.storage, ResearchSnapshotStorage):
            raise TypeError("storage 必须实现 ResearchSnapshotStorage")
        if not callable(self.plan_provider):
            raise TypeError("plan_provider 必须可调用")
        if not isinstance(
            self.selection_token_codec,
            QuestionnaireSourceSelectionTokenCodec,
        ):
            raise TypeError(
                "selection_token_codec 必须实现选择令牌编解码端口"
            )

    async def _resolve_plan(
        self,
        owner_ref: str,
        workflow_ref: str,
    ) -> QuestionnaireSourceWorkflowPlan:
        try:
            if inspect.iscoroutinefunction(self.plan_provider):
                planned = self.plan_provider(owner_ref, workflow_ref)
            else:
                planned = await asyncio.to_thread(
                    self.plan_provider,
                    owner_ref,
                    workflow_ref,
                )
            if inspect.isawaitable(planned):
                planned = await planned
        except QuestionnaireSourceWorkflowNotFoundError as error:
            raise QuestionnaireSourceWorkflowNotFoundError() from error
        except QuestionnaireSourceScopeError as error:
            raise QuestionnaireSourceWorkflowNotFoundError() from error
        except QuestionnaireSourceWorkflowApiError as error:
            raise QuestionnaireSourceWorkflowInternalError() from error
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise QuestionnaireSourceWorkflowInternalError() from error

        if planned is None:
            raise QuestionnaireSourceWorkflowNotFoundError()
        if not isinstance(planned, QuestionnaireSourceWorkflowPlan):
            raise QuestionnaireSourceWorkflowInternalError()
        if planned.owner_ref != owner_ref or planned.workflow_ref != workflow_ref:
            raise QuestionnaireSourceWorkflowNotFoundError()
        return planned

    def _decode_selection_token(
        self,
        owner_ref: str,
        workflow_ref: str,
        external_token: str | None,
    ) -> str | None:
        if external_token is None:
            return None
        try:
            internal_token = self.selection_token_codec.decode(
                owner_ref,
                workflow_ref,
                external_token,
            )
            return _require_internal_selection_token(internal_token)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise QuestionnaireSourceWorkflowConflictError() from error

    def _encode_selection_token(
        self,
        owner_ref: str,
        workflow_ref: str,
        internal_token: str,
    ) -> str:
        try:
            normalized = _require_internal_selection_token(internal_token)
            encoded = self.selection_token_codec.encode(
                owner_ref,
                workflow_ref,
                normalized,
            )
            return _require_external_selection_token(encoded)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise QuestionnaireSourceWorkflowInternalError() from error

    async def _run_operation(
        self,
        owner_ref: str,
        workflow_ref: str,
        request: QuestionnaireSourceWorkflowRunRequest,
    ) -> QuestionnaireSourceWorkflowApiResponse:
        plan = await self._resolve_plan(owner_ref, workflow_ref)
        selection_token = self._decode_selection_token(
            owner_ref,
            workflow_ref,
            request.selection_token,
        )
        try:
            workflow = await run_and_persist_questionnaire_source_workflow(
                owner_ref=owner_ref,
                steps=plan.steps,
                storage=self.storage,
                available_actions=plan.available_actions,
                selected_source_id=request.selected_source_id,
                selection_token=selection_token,
                response_only=request.response_only,
            )
        except QuestionnaireSourceScopeError as error:
            raise QuestionnaireSourceWorkflowNotFoundError() from error
        except SnapshotConflictError as error:
            raise QuestionnaireSourceWorkflowConflictError() from error
        except QuestionnaireSourceWorkflowApiError:
            raise
        except asyncio.CancelledError:
            raise
        except ValueError as error:
            if (
                request.selected_source_id is not None
                or request.selection_token is not None
                or request.response_only
            ):
                raise QuestionnaireSourceWorkflowConflictError() from error
            raise QuestionnaireSourceWorkflowInternalError() from error
        except TypeError as error:
            raise QuestionnaireSourceWorkflowInternalError() from error
        except Exception as error:
            raise QuestionnaireSourceWorkflowInternalError() from error

        if not isinstance(workflow, QuestionnaireSourceWorkflowResult):
            raise QuestionnaireSourceWorkflowInternalError()

        persisted_summary: QuestionnaireSnapshotSummary | None = None
        if workflow.status in {
            QuestionnaireSourceWorkflowStatus.RESOLVED,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        }:
            try:
                persisted_summary = await asyncio.to_thread(
                    _load_persisted_summary,
                    owner_ref,
                    workflow,
                    self.storage,
                )
            except QuestionnaireSourceWorkflowApiError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise QuestionnaireSourceWorkflowInternalError() from error

        external_selection_token: str | None = None
        if workflow.selection_token is not None:
            external_selection_token = self._encode_selection_token(
                owner_ref,
                workflow_ref,
                workflow.selection_token,
            )

        try:
            result = workflow.result
            return QuestionnaireSourceWorkflowApiResponse(
                status=workflow.status,
                route=workflow.route,
                snapshot=persisted_summary,
                attempts=[
                    _attempt_summary(attempt)
                    for attempt in workflow.attempts
                ],
                next_actions=list(workflow.next_actions),
                selection_options=_selection_options(workflow),
                selection_token=external_selection_token,
                conflicts=_selection_conflicts(workflow),
                failure_reason=workflow.failure_reason,
                response_only_confirmed=workflow.response_only_confirmed,
                selected_source_ids=(
                    list(result.selected_source_ids)
                    if result is not None
                    else []
                ),
                partial_success=(
                    result.partial_success if result is not None else False
                ),
            )
        except QuestionnaireSourceWorkflowApiError:
            raise
        except Exception as error:
            raise QuestionnaireSourceWorkflowInternalError() from error

    async def run(
        self,
        owner_ref: str,
        workflow_ref: str,
        request: QuestionnaireSourceWorkflowRunRequest,
    ) -> QuestionnaireSourceWorkflowApiResponse:
        """执行服务端计划；取消时仍等待所有副作用真实结束。"""
        try:
            owner = _require_owner(owner_ref)
            workflow = _require_workflow_ref(workflow_ref)
            if not isinstance(request, QuestionnaireSourceWorkflowRunRequest):
                raise QuestionnaireSourceWorkflowInvalidError()
            payload = QuestionnaireSourceWorkflowRunRequest.model_validate(
                request.model_dump(mode="python")
            )
        except QuestionnaireSourceWorkflowInvalidError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise QuestionnaireSourceWorkflowInvalidError() from error

        operation = asyncio.create_task(self._run_operation(
            owner,
            workflow,
            payload,
        ))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await _await_uncancelled(operation)
            raise
        except QuestionnaireSourceWorkflowApiError:
            raise
        except Exception as error:
            raise QuestionnaireSourceWorkflowInternalError() from error
