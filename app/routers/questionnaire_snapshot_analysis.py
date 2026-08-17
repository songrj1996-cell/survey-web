"""已保存问卷快照创建标准分析会话的独立 HTTP 壳。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request
from multipart.exceptions import MultipartParseError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from app.core.security import _owner_from_login
from app.schemas.questionnaire_source_api import (
    QuestionnaireSnapshotAnalysisSessionResponse,
)
from app.services.auth import _require_feature
from app.services.questionnaire_snapshot_analysis_api import (
    MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES,
    QuestionnaireSnapshotAnalysisApi,
    QuestionnaireSnapshotAnalysisInternalError,
    QuestionnaireSnapshotAnalysisInvalidError,
    QuestionnaireSnapshotAnalysisNotFoundError,
)


_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_MAX_CONCURRENT_UPLOADS = 1
_MAX_CONCURRENT_SESSION_CREATIONS = 1
_UPLOAD_TIMEOUT_SECONDS = 120.0
_SESSION_CREATION_TIMEOUT_SECONDS = 180.0

_INVALID_REQUEST_DETAIL = "回答数据上传请求无效"
_INVALID_CONTENT_DETAIL = "回答数据无效或与问卷快照不匹配"
_NOT_FOUND_DETAIL = "问卷快照不存在"
_INTERNAL_DETAIL = "问卷快照分析会话暂时无法创建"
_TOO_LARGE_DETAIL = "回答数据超过上传大小限制"

_SUPPORTED_RESPONSE_MIME_TYPES = {
    ".csv": frozenset({
        "application/csv",
        "application/octet-stream",
        "application/vnd.ms-excel",
        "text/csv",
        "text/plain",
    }),
    ".xlsx": frozenset({
        "application/octet-stream",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }),
}


class _ResponseRequestTooLarge(MultiPartException):
    """multipart 请求在解析阶段超过总字节上限。"""


class _SemaphoreLease:
    """确保一个并发名额最多释放一次。"""

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
    lease: _SemaphoreLease,
) -> None:
    """消费后台任务异常，并在真实结束后释放创建名额。"""
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


def _raise_http_error(error: Exception) -> None:
    if isinstance(error, QuestionnaireSnapshotAnalysisNotFoundError):
        raise HTTPException(status_code=404, detail=_NOT_FOUND_DETAIL) from error
    if isinstance(error, QuestionnaireSnapshotAnalysisInvalidError):
        raise HTTPException(status_code=422, detail=_INVALID_CONTENT_DETAIL) from error
    if isinstance(error, QuestionnaireSnapshotAnalysisInternalError):
        raise HTTPException(status_code=500, detail=_INTERNAL_DETAIL) from error
    raise HTTPException(status_code=500, detail=_INTERNAL_DETAIL) from error


async def _bounded_request_stream(
    request: Request,
) -> AsyncIterator[bytes]:
    limit = MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES + _MULTIPART_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail=_INVALID_REQUEST_DETAIL,
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=422,
                detail=_INVALID_REQUEST_DETAIL,
            )
        if declared_length > limit:
            raise HTTPException(
                status_code=413,
                detail=_TOO_LARGE_DETAIL,
            )

    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise _ResponseRequestTooLarge("request too large")
            yield chunk
    except (_ResponseRequestTooLarge, HTTPException):
        raise
    except ClientDisconnect as error:
        raise HTTPException(
            status_code=400,
            detail="回答数据上传未完整发送",
        ) from error
    except asyncio.CancelledError:
        raise


def _close_parser_temporaries(
    parser: MultiPartParser,
    *,
    retained_file_ids: set[int] | None = None,
) -> None:
    retained = retained_file_ids or set()
    for temporary in tuple(
        getattr(parser, "_files_to_close_on_error", ())
    ):
        if id(temporary) in retained:
            continue
        try:
            temporary.close()
        except Exception:
            pass


async def _parse_response_upload(request: Request) -> FormData:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail="回答数据必须使用 multipart/form-data 上传",
        )

    parser = MultiPartParser(
        headers=request.headers,
        stream=_bounded_request_stream(request),
        max_files=1,
        max_fields=0,
    )
    succeeded = False
    try:
        form = await parser.parse()
        retained_file_ids = {
            id(value.file)
            for _, value in form.multi_items()
            if isinstance(value, UploadFile)
        }
        _close_parser_temporaries(
            parser,
            retained_file_ids=retained_file_ids,
        )
        succeeded = True
        return form
    except _ResponseRequestTooLarge as error:
        raise HTTPException(
            status_code=413,
            detail=_TOO_LARGE_DETAIL,
        ) from error
    except HTTPException:
        raise
    except (MultiPartException, MultipartParseError, KeyError) as error:
        raise HTTPException(
            status_code=422,
            detail=_INVALID_REQUEST_DETAIL,
        ) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=_INTERNAL_DETAIL,
        ) from error
    finally:
        if not succeeded:
            _close_parser_temporaries(parser)


def _safe_response_filename(file: UploadFile) -> tuple[str, str]:
    raw_filename = str(file.filename or "").strip()
    filename = PurePosixPath(raw_filename.replace("\\", "/")).name.strip()
    if (
        not filename
        or filename in {".", ".."}
        or len(filename) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in filename)
    ):
        raise HTTPException(
            status_code=422,
            detail="回答数据文件名无效",
        )
    suffix = PurePosixPath(filename).suffix.casefold()
    if suffix not in _SUPPORTED_RESPONSE_MIME_TYPES:
        raise HTTPException(
            status_code=422,
            detail="回答数据仅支持 .csv 或 .xlsx 文件",
        )
    media_type = str(file.content_type or "").split(";", 1)[0].strip().casefold()
    if media_type not in _SUPPORTED_RESPONSE_MIME_TYPES[suffix]:
        raise HTTPException(
            status_code=415,
            detail="回答数据文件类型与扩展名不匹配",
        )
    return filename, suffix


def create_questionnaire_snapshot_analysis_router(
    api: QuestionnaireSnapshotAnalysisApi,
) -> APIRouter:
    """创建从已保存快照进入旧问卷分析流程的独立路由。"""
    if not isinstance(api, QuestionnaireSnapshotAnalysisApi):
        raise TypeError("api 必须是 QuestionnaireSnapshotAnalysisApi")

    router = APIRouter()
    upload_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPLOADS)
    creation_semaphore = asyncio.Semaphore(
        _MAX_CONCURRENT_SESSION_CREATIONS
    )

    @router.post(
        "/api/questionnaire-sources/snapshots/{snapshot_id}/analysis-sessions",
        response_model=QuestionnaireSnapshotAnalysisSessionResponse,
    )
    async def create_snapshot_analysis_session(
        snapshot_id: str,
        request: Request,
    ) -> QuestionnaireSnapshotAnalysisSessionResponse:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        if upload_semaphore.locked() or creation_semaphore.locked():
            raise HTTPException(
                status_code=429,
                detail="已有回答数据正在创建分析会话，请稍后重试",
            )

        await upload_semaphore.acquire()
        upload_lease = _SemaphoreLease(upload_semaphore)
        creation_lease: _SemaphoreLease | None = None
        form: FormData | None = None
        release_deferred = False
        try:
            try:
                form = await asyncio.wait_for(
                    _parse_response_upload(request),
                    timeout=_UPLOAD_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise HTTPException(
                    status_code=408,
                    detail="回答数据上传超时，请重试",
                ) from error

            items = form.multi_items()
            if (
                len(items) != 1
                or items[0][0] != "file"
                or not isinstance(items[0][1], UploadFile)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="回答数据上传请求必须只包含 file 文件字段",
                )
            file = items[0][1]
            filename, _ = _safe_response_filename(file)
            try:
                content = await file.read(
                    MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES + 1
                )
            except Exception as error:
                raise HTTPException(
                    status_code=500,
                    detail=_INTERNAL_DETAIL,
                ) from error
            if not content:
                raise HTTPException(
                    status_code=422,
                    detail="回答数据不能为空",
                )
            if len(content) > MAX_QUESTIONNAIRE_RESPONSE_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=_TOO_LARGE_DETAIL,
                )

            await form.close()
            form = None
            await creation_semaphore.acquire()
            creation_lease = _SemaphoreLease(creation_semaphore)
            upload_lease.release()

            creation_task = asyncio.create_task(api.create_session(
                owner_ref,
                snapshot_id,
                filename,
                content,
                login,
            ))
            try:
                return await asyncio.wait_for(
                    asyncio.shield(creation_task),
                    timeout=_SESSION_CREATION_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                release_deferred = True
                assert creation_lease is not None
                if creation_task.done():
                    _release_after_task(creation_task, creation_lease)
                else:
                    creation_task.add_done_callback(
                        lambda task: _release_after_task(
                            task,
                            creation_lease,
                        )
                    )
                raise HTTPException(
                    status_code=504,
                    detail="创建问卷分析会话超时，请稍后重试",
                ) from error
            except asyncio.CancelledError:
                release_deferred = True
                assert creation_lease is not None
                if creation_task.done():
                    _release_after_task(creation_task, creation_lease)
                else:
                    creation_task.add_done_callback(
                        lambda task: _release_after_task(
                            task,
                            creation_lease,
                        )
                    )
                raise
            except Exception as error:
                _raise_http_error(error)
                raise AssertionError("unreachable")
        finally:
            try:
                if form is not None:
                    await form.close()
            finally:
                upload_lease.release()
                if creation_lease is not None and not release_deferred:
                    creation_lease.release()

    return router
