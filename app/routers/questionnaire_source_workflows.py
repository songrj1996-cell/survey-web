"""问卷来源降级、选择与原子保存工作流的独立 HTTP 壳。"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from app.core.security import _owner_from_login
from app.schemas.questionnaire_source_workflow_api import (
    QuestionnaireSourceWorkflowApiResponse,
    QuestionnaireSourceWorkflowRunRequest,
)
from app.services.auth import _require_feature
from app.services.questionnaire_source_workflow_api import (
    QuestionnaireSourceWorkflowApi,
    QuestionnaireSourceWorkflowConflictError,
    QuestionnaireSourceWorkflowInternalError,
    QuestionnaireSourceWorkflowInvalidError,
    QuestionnaireSourceWorkflowNotFoundError,
)


_WORKFLOW_REQUEST_MAX_BYTES = 4 * 1024
_WORKFLOW_REQUEST_TIMEOUT_SECONDS = 15.0
_WORKFLOW_PROCESSING_TIMEOUT_SECONDS = 180.0
_MAX_CONCURRENT_WORKFLOWS = 1


class _DuplicateJsonKey(ValueError):
    """JSON 请求不能依赖重复键的覆盖顺序。"""


class _WorkflowLease:
    """确保工作流并发名额最多释放一次。"""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()


def _release_after_task(
    task: asyncio.Task[object],
    lease: _WorkflowLease,
) -> None:
    """消费后台异常，并在实际工作流结束后释放并发名额。"""
    try:
        if not task.cancelled():
            task.exception()
    except BaseException:
        pass
    finally:
        lease.release()


def _owner_key(login: dict | None) -> str:
    owner = str(_owner_from_login(login)["owner_key"]).strip()
    if not owner:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    return owner


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


async def _read_workflow_request_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="问卷来源工作流请求无效",
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=422,
                detail="问卷来源工作流请求无效",
            )
        if declared_length > _WORKFLOW_REQUEST_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="问卷来源工作流请求超过大小限制",
            )

    content = bytearray()
    try:
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > _WORKFLOW_REQUEST_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="问卷来源工作流请求超过大小限制",
                )
    except HTTPException:
        raise
    except ClientDisconnect as error:
        raise HTTPException(
            status_code=400,
            detail="问卷来源工作流请求未完整发送",
        ) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="问卷来源工作流暂时不可用",
        ) from error
    return bytes(content)


async def _parse_workflow_request(
    request: Request,
) -> QuestionnaireSourceWorkflowRunRequest:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise HTTPException(
            status_code=415,
            detail="问卷来源工作流请求必须使用 JSON",
        )

    content = await _read_workflow_request_body(request)
    if not content:
        raise HTTPException(
            status_code=422,
            detail="问卷来源工作流请求不能为空",
        )
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        return QuestionnaireSourceWorkflowRunRequest.model_validate(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        ValidationError,
    ) as error:
        raise HTTPException(
            status_code=422,
            detail="问卷来源工作流请求无效",
        ) from error


def _raise_workflow_http_error(error: Exception) -> None:
    if isinstance(error, QuestionnaireSourceWorkflowInvalidError):
        raise HTTPException(
            status_code=422,
            detail="问卷来源工作流请求无效",
        ) from error
    if isinstance(error, QuestionnaireSourceWorkflowNotFoundError):
        raise HTTPException(
            status_code=404,
            detail="问卷来源计划不存在",
        ) from error
    if isinstance(error, QuestionnaireSourceWorkflowConflictError):
        raise HTTPException(
            status_code=409,
            detail="问卷来源状态已变化，请重新确认",
        ) from error
    if isinstance(error, QuestionnaireSourceWorkflowInternalError):
        raise HTTPException(
            status_code=500,
            detail="问卷来源工作流暂时不可用",
        ) from error
    raise HTTPException(
        status_code=500,
        detail="问卷来源工作流暂时不可用",
    ) from error


def create_questionnaire_source_workflows_router(
    api: QuestionnaireSourceWorkflowApi,
) -> APIRouter:
    """创建由服务端解析来源计划的独立工作流路由。"""
    if not isinstance(api, QuestionnaireSourceWorkflowApi):
        raise TypeError("api 必须是 QuestionnaireSourceWorkflowApi")

    router = APIRouter()
    workflow_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_WORKFLOWS)

    @router.post(
        "/api/questionnaire-sources/workflows/{workflow_ref}/resolve",
        response_model=QuestionnaireSourceWorkflowApiResponse,
    )
    async def resolve_questionnaire_source_workflow(
        workflow_ref: str,
        request: Request,
        response: Response,
    ) -> QuestionnaireSourceWorkflowApiResponse:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        if workflow_semaphore.locked():
            raise HTTPException(
                status_code=429,
                detail="已有问卷来源工作流正在处理，请稍后重试",
            )

        await workflow_semaphore.acquire()
        lease = _WorkflowLease(workflow_semaphore)
        release_deferred = False
        try:
            try:
                payload = await asyncio.wait_for(
                    _parse_workflow_request(request),
                    timeout=_WORKFLOW_REQUEST_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise HTTPException(
                    status_code=408,
                    detail="问卷来源工作流请求发送超时，请重试",
                ) from error

            workflow_task = asyncio.create_task(
                api.run(owner_ref, workflow_ref, payload)
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(workflow_task),
                    timeout=_WORKFLOW_PROCESSING_TIMEOUT_SECONDS,
                )
                response.headers["Cache-Control"] = "private, no-store"
                return result
            except TimeoutError as error:
                release_deferred = True
                if workflow_task.done():
                    _release_after_task(workflow_task, lease)
                else:
                    workflow_task.add_done_callback(
                        lambda task: _release_after_task(task, lease)
                    )
                raise HTTPException(
                    status_code=504,
                    detail="问卷来源工作流处理超时，请稍后重试",
                ) from error
            except asyncio.CancelledError:
                release_deferred = True
                if workflow_task.done():
                    _release_after_task(workflow_task, lease)
                else:
                    workflow_task.add_done_callback(
                        lambda task: _release_after_task(task, lease)
                    )
                raise
            except Exception as error:
                _raise_workflow_http_error(error)
                raise AssertionError("unreachable")
        finally:
            if not release_deferred:
                lease.release()

    return router
