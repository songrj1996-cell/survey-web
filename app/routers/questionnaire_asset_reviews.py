"""Owner-scoped questionnaire asset review HTTP adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
import json
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.requests import ClientDisconnect

from app.core.security import _owner_from_login
from app.schemas.questionnaire_asset_review import (
    QuestionnaireAssetReviewDecisionRequest,
    QuestionnaireAssetReviewProjection,
    QuestionnaireAssetThumbnailResult,
)
from app.services.auth import _require_feature
from app.services.questionnaire_asset_review_api import (
    QuestionnaireAssetReviewApi,
    QuestionnaireAssetReviewConflictError,
    QuestionnaireAssetReviewInternalError,
    QuestionnaireAssetReviewInvalidError,
    QuestionnaireAssetReviewNotFoundError,
)


_MAX_CONCURRENT_ASSET_REVIEWS = 2
_MAX_CONCURRENT_ASSET_REVIEW_DECISIONS = 1
_ASSET_REVIEW_PROJECTION_TIMEOUT_SECONDS = 15.0
_ASSET_REVIEW_THUMBNAIL_TIMEOUT_SECONDS = 30.0
_ASSET_REVIEW_DECISION_BODY_TIMEOUT_SECONDS = 15.0
_ASSET_REVIEW_DECISION_TIMEOUT_SECONDS = 30.0
_ASSET_REVIEW_DECISION_BODY_MAX_BYTES = 4 * 1024

_NOT_FOUND_DETAIL = "问卷素材审阅内容不存在"
_INVALID_DETAIL = "问卷素材无法安全预览"
_INTERNAL_DETAIL = "问卷素材审阅暂时不可用"
_BUSY_DETAIL = "已有问卷素材正在处理，请稍后重试"
_TIMEOUT_DETAIL = "问卷素材处理超时，请稍后重试"
_DECISION_INVALID_DETAIL = "问卷素材审阅决定请求无效"
_DECISION_TOO_LARGE_DETAIL = "问卷素材审阅决定请求超过大小限制"
_DECISION_MEDIA_TYPE_DETAIL = "问卷素材审阅决定请求必须使用 JSON"
_DECISION_DISCONNECTED_DETAIL = "问卷素材审阅决定请求未完整发送"
_DECISION_BODY_TIMEOUT_DETAIL = "问卷素材审阅决定请求发送超时，请重试"
_DECISION_CONFLICT_DETAIL = "问卷素材审阅状态已变化，请刷新后重试"
_DECISION_BUSY_DETAIL = "已有问卷素材审阅决定正在处理，请稍后重试"
_DECISION_TIMEOUT_DETAIL = (
    "问卷素材审阅决定处理超时，结果可能已经生效；"
    "请使用相同幂等键重试"
)
_SAFE_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}

_ResultT = TypeVar("_ResultT")


class _DuplicateJsonKey(ValueError):
    """Reject requests whose meaning depends on duplicate-key ordering."""


class _InvalidJsonConstant(ValueError):
    """Reject non-standard NaN and infinity JSON constants."""


class _ReviewLease:
    """Ensure one admission slot is released exactly once."""

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
    lease: _ReviewLease,
) -> None:
    """Consume a background result and release only after real completion."""
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


def _safe_headers(
    existing: Mapping[str, str] | None = None,
) -> dict[str, str]:
    protected_names = {
        header_name.casefold() for header_name in _SAFE_RESPONSE_HEADERS
    }
    headers = {
        name: value
        for name, value in (existing or {}).items()
        if name.casefold() not in protected_names
    }
    headers.update(_SAFE_RESPONSE_HEADERS)
    return headers


def _reraise_http_exception(error: HTTPException) -> None:
    raise HTTPException(
        status_code=error.status_code,
        detail=error.detail,
        headers=_safe_headers(error.headers),
    ) from error


def _raise_http_error(error: Exception) -> None:
    if isinstance(error, QuestionnaireAssetReviewNotFoundError):
        raise HTTPException(
            status_code=404,
            detail=_NOT_FOUND_DETAIL,
            headers=_safe_headers(),
        ) from error
    if isinstance(error, QuestionnaireAssetReviewInvalidError):
        raise HTTPException(
            status_code=422,
            detail=_INVALID_DETAIL,
            headers=_safe_headers(),
        ) from error
    if isinstance(error, QuestionnaireAssetReviewConflictError):
        raise HTTPException(
            status_code=409,
            detail=_DECISION_CONFLICT_DETAIL,
            headers=_safe_headers(),
        ) from error
    if isinstance(error, QuestionnaireAssetReviewInternalError):
        raise HTTPException(
            status_code=500,
            detail=_INTERNAL_DETAIL,
            headers=_safe_headers(),
        ) from error
    raise HTTPException(
        status_code=500,
        detail=_INTERNAL_DETAIL,
        headers=_safe_headers(),
    ) from error


def _raise_decision_http_error(error: Exception) -> None:
    if isinstance(error, QuestionnaireAssetReviewInvalidError):
        raise HTTPException(
            status_code=422,
            detail=_DECISION_INVALID_DETAIL,
            headers=_safe_headers(),
        ) from error
    _raise_http_error(error)


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


def _reject_invalid_json_constant(_value: str) -> object:
    raise _InvalidJsonConstant()


async def _read_decision_request_body(request: Request) -> bytes:
    content_length_values = request.headers.getlist("content-length")
    if len(content_length_values) > 1:
        raise HTTPException(
            status_code=422,
            detail=_DECISION_INVALID_DETAIL,
        )
    content_length = (
        content_length_values[0] if content_length_values else None
    )
    declared_length: int | None = None
    if content_length is not None:
        if (
            not content_length
            or not content_length.isascii()
            or not content_length.isdecimal()
        ):
            raise HTTPException(
                status_code=422,
                detail=_DECISION_INVALID_DETAIL,
            )
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=_DECISION_INVALID_DETAIL,
            ) from error
        if declared_length > _ASSET_REVIEW_DECISION_BODY_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=_DECISION_TOO_LARGE_DETAIL,
            )

    content = bytearray()
    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > _ASSET_REVIEW_DECISION_BODY_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=_DECISION_TOO_LARGE_DETAIL,
                )
            content.extend(chunk)
    except HTTPException:
        raise
    except ClientDisconnect as error:
        raise HTTPException(
            status_code=400,
            detail=_DECISION_DISCONNECTED_DETAIL,
        ) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=_INTERNAL_DETAIL,
        ) from error
    if declared_length is not None and total != declared_length:
        raise HTTPException(
            status_code=400,
            detail=_DECISION_DISCONNECTED_DETAIL,
        )
    return bytes(content)


async def _parse_decision_request(
    request: Request,
) -> QuestionnaireAssetReviewDecisionRequest:
    content_type_values = request.headers.getlist("content-type")
    if len(content_type_values) != 1:
        raise HTTPException(
            status_code=415,
            detail=_DECISION_MEDIA_TYPE_DETAIL,
        )
    media_type = content_type_values[0].split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise HTTPException(
            status_code=415,
            detail=_DECISION_MEDIA_TYPE_DETAIL,
        )

    content = await _read_decision_request_body(request)
    if not content:
        raise HTTPException(
            status_code=422,
            detail=_DECISION_INVALID_DETAIL,
        )
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_invalid_json_constant,
        )
        return QuestionnaireAssetReviewDecisionRequest.model_validate(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        _InvalidJsonConstant,
        RecursionError,
        ValidationError,
    ) as error:
        raise HTTPException(
            status_code=422,
            detail=_DECISION_INVALID_DETAIL,
        ) from error


async def _run_admitted(
    semaphore: asyncio.Semaphore,
    operation_factory: Callable[[], Awaitable[_ResultT]],
    *,
    timeout_seconds: float,
) -> _ResultT:
    if semaphore.locked():
        raise HTTPException(status_code=429, detail=_BUSY_DETAIL)

    await semaphore.acquire()
    lease = _ReviewLease(semaphore)
    operation: Awaitable[_ResultT] | None = None
    try:
        operation = operation_factory()
        task = asyncio.create_task(operation)
    except BaseException:
        if operation is not None and hasattr(operation, "close"):
            operation.close()  # type: ignore[attr-defined]
        lease.release()
        raise
    release_deferred = False
    try:
        try:
            completed, _pending = await asyncio.wait(
                (task,),
                timeout=timeout_seconds,
            )
            if task in completed:
                return task.result()
            release_deferred = True
            if task.done():
                _release_after_task(task, lease)
            else:
                task.add_done_callback(
                    lambda completed: _release_after_task(completed, lease)
                )
            raise HTTPException(
                status_code=504,
                detail=_TIMEOUT_DETAIL,
            )
        except asyncio.CancelledError:
            release_deferred = True
            if task.done():
                _release_after_task(task, lease)
            else:
                task.add_done_callback(
                    lambda completed: _release_after_task(completed, lease)
                )
            raise
    finally:
        if not release_deferred:
            lease.release()


def create_questionnaire_asset_reviews_router(
    api: QuestionnaireAssetReviewApi,
) -> APIRouter:
    """Create review routes for one explicitly injected API."""
    if not isinstance(api, QuestionnaireAssetReviewApi):
        raise TypeError("api 必须是 QuestionnaireAssetReviewApi")

    router = APIRouter()
    review_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ASSET_REVIEWS)
    decision_semaphore = asyncio.Semaphore(
        _MAX_CONCURRENT_ASSET_REVIEW_DECISIONS
    )

    @router.get(
        "/api/questionnaire-sources/snapshots/{snapshot_id}/asset-review",
        response_model=QuestionnaireAssetReviewProjection,
    )
    async def get_questionnaire_asset_review(
        snapshot_id: str,
        request: Request,
        response: Response,
    ) -> QuestionnaireAssetReviewProjection:
        try:
            login = await _require_feature(request, "survey")
            owner_ref = _owner_key(login)
            projection = await _run_admitted(
                review_semaphore,
                lambda: api.get_projection(owner_ref, snapshot_id),
                timeout_seconds=_ASSET_REVIEW_PROJECTION_TIMEOUT_SECONDS,
            )
        except HTTPException as error:
            _reraise_http_exception(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _raise_http_error(error)
        response.headers.update(_SAFE_RESPONSE_HEADERS)
        return projection

    @router.get(
        "/api/questionnaire-sources/snapshots/{snapshot_id}"
        "/asset-review/thumbnails/{asset_token}.png",
    )
    async def get_questionnaire_asset_thumbnail(
        snapshot_id: str,
        asset_token: str,
        request: Request,
    ) -> Response:
        try:
            login = await _require_feature(request, "survey")
            owner_ref = _owner_key(login)
            thumbnail: QuestionnaireAssetThumbnailResult = await _run_admitted(
                review_semaphore,
                lambda: api.get_asset_thumbnail(
                    owner_ref,
                    snapshot_id,
                    asset_token,
                ),
                timeout_seconds=_ASSET_REVIEW_THUMBNAIL_TIMEOUT_SECONDS,
            )
        except HTTPException as error:
            _reraise_http_exception(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _raise_http_error(error)
        return Response(
            content=thumbnail.content,
            media_type=thumbnail.media_type,
            headers={
                **_SAFE_RESPONSE_HEADERS,
                "Content-Length": str(len(thumbnail.content)),
            },
        )

    @router.post(
        "/api/questionnaire-sources/snapshots/{snapshot_id}"
        "/asset-review/decisions",
        response_model=QuestionnaireAssetReviewProjection,
    )
    async def submit_questionnaire_asset_review_decision(
        snapshot_id: str,
        request: Request,
        response: Response,
    ) -> QuestionnaireAssetReviewProjection:
        lease: _ReviewLease | None = None
        release_deferred = False
        try:
            login = await _require_feature(request, "survey")
            owner_ref = _owner_key(login)
            if decision_semaphore.locked():
                raise HTTPException(
                    status_code=429,
                    detail=_DECISION_BUSY_DETAIL,
                )

            await decision_semaphore.acquire()
            lease = _ReviewLease(decision_semaphore)
            try:
                try:
                    payload = await asyncio.wait_for(
                        _parse_decision_request(request),
                        timeout=_ASSET_REVIEW_DECISION_BODY_TIMEOUT_SECONDS,
                    )
                except TimeoutError as error:
                    raise HTTPException(
                        status_code=408,
                        detail=_DECISION_BODY_TIMEOUT_DETAIL,
                    ) from error

                decision_task = asyncio.create_task(
                    api.submit_decision(owner_ref, snapshot_id, payload)
                )
                processing_timed_out = False
                try:
                    completed, _pending = await asyncio.wait(
                        (decision_task,),
                        timeout=_ASSET_REVIEW_DECISION_TIMEOUT_SECONDS,
                    )
                    if decision_task in completed:
                        projection = decision_task.result()
                    else:
                        release_deferred = True
                        if decision_task.done():
                            _release_after_task(decision_task, lease)
                        else:
                            decision_task.add_done_callback(
                                lambda completed: _release_after_task(
                                    completed,
                                    lease,
                                )
                            )
                        processing_timed_out = True
                except asyncio.CancelledError:
                    release_deferred = True
                    if decision_task.done():
                        _release_after_task(decision_task, lease)
                    else:
                        decision_task.add_done_callback(
                            lambda completed: _release_after_task(
                                completed,
                                lease,
                            )
                        )
                    raise
                except Exception as error:
                    _raise_decision_http_error(error)
                    raise AssertionError("unreachable")
                if processing_timed_out:
                    raise HTTPException(
                        status_code=504,
                        detail=_DECISION_TIMEOUT_DETAIL,
                    )
            finally:
                if lease is not None and not release_deferred:
                    lease.release()
        except HTTPException as error:
            _reraise_http_exception(error)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _raise_http_error(error)
        response.headers.update(_SAFE_RESPONSE_HEADERS)
        return projection

    return router
