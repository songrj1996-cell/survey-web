"""Owner-scoped, read-only questionnaire asset review HTTP adapter."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Request, Response

from app.core.security import _owner_from_login
from app.schemas.questionnaire_asset_review import (
    QuestionnaireAssetReviewProjection,
    QuestionnaireAssetThumbnailResult,
)
from app.services.auth import _require_feature
from app.services.questionnaire_asset_review_api import (
    QuestionnaireAssetReviewApi,
    QuestionnaireAssetReviewInternalError,
    QuestionnaireAssetReviewInvalidError,
    QuestionnaireAssetReviewNotFoundError,
)


_MAX_CONCURRENT_ASSET_REVIEWS = 2
_ASSET_REVIEW_PROJECTION_TIMEOUT_SECONDS = 15.0
_ASSET_REVIEW_THUMBNAIL_TIMEOUT_SECONDS = 30.0

_NOT_FOUND_DETAIL = "问卷素材审阅内容不存在"
_INVALID_DETAIL = "问卷素材无法安全预览"
_INTERNAL_DETAIL = "问卷素材审阅暂时不可用"
_BUSY_DETAIL = "已有问卷素材正在处理，请稍后重试"
_TIMEOUT_DETAIL = "问卷素材处理超时，请稍后重试"
_SAFE_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
}

_ResultT = TypeVar("_ResultT")


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
    """Create read-only review routes for one explicitly injected API."""
    if not isinstance(api, QuestionnaireAssetReviewApi):
        raise TypeError("api 必须是 QuestionnaireAssetReviewApi")

    router = APIRouter()
    review_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_ASSET_REVIEWS)

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

    return router
