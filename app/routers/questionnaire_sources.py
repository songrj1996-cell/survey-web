"""问卷快照上传与查询接口的 HTTP 壳。"""

import asyncio
from collections.abc import AsyncIterator
import json

from fastapi import APIRouter, HTTPException, Request
from multipart.exceptions import MultipartParseError
from pydantic import ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from app.core.security import _owner_from_login
from app.schemas.questionnaire_source_api import (
    GoogleFormsSnapshotImportRequest,
    QuestionnaireMaterialUploadSummary,
    QuestionnaireSnapshotSummary,
)
from app.services.auth import _require_feature
from app.services.bested_questionnaire_snapshot_api import (
    MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES,
    BestedQuestionnaireConflictError,
    BestedQuestionnaireInternalError,
    BestedQuestionnaireInvalidError,
    BestedQuestionnaireSnapshotApi,
)
from app.services.google_forms_snapshot_api import (
    GoogleFormsQuestionnaireAuthRequiredError,
    GoogleFormsQuestionnaireConflictError,
    GoogleFormsQuestionnaireInternalError,
    GoogleFormsQuestionnaireInvalidError,
    GoogleFormsQuestionnaireNotFoundError,
    GoogleFormsQuestionnairePermissionError,
    GoogleFormsQuestionnaireProviderError,
    GoogleFormsQuestionnaireRetryableError,
    GoogleFormsQuestionnaireSnapshotApi,
)
from app.services.questionnaire_material_snapshot_api import (
    MAX_MATERIAL_SCREENSHOT_BYTES,
    MAX_MATERIAL_SCREENSHOT_FILES,
    MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES,
    SUPPORTED_MATERIAL_SCREENSHOT_MIME_TYPES,
    QuestionnaireMaterialConflictError,
    QuestionnaireMaterialInternalError,
    QuestionnaireMaterialInvalidError,
    QuestionnaireMaterialScreenshot,
    QuestionnaireMaterialSnapshotApi,
)
from app.services.questionnaire_snapshot_api import (
    MAX_SNAPSHOT_UPLOAD_BYTES,
    QuestionnaireSnapshotApi,
    QuestionnaireSnapshotConflictError,
    QuestionnaireSnapshotInvalidError,
    QuestionnaireSnapshotNotFoundError,
)


_INVALID_DETAIL = "问卷快照包无效或已损坏"
_CONFLICT_DETAIL = "同一快照 ID 已存在不同内容"
_INTERNAL_DETAIL = "问卷快照服务暂时不可用"
_MAX_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_DOWNLOAD_FILENAME = "questionnaire-snapshot.zip"
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_CONCURRENT_DOWNLOADS = 1
_MAX_CONCURRENT_BESTED_IMPORTS = 1
_MAX_CONCURRENT_BESTED_UPLOADS = 1
_BESTED_UPLOAD_TIMEOUT_SECONDS = 120.0
_MAX_CONCURRENT_GOOGLE_IMPORTS = 1
_GOOGLE_REQUEST_MAX_BYTES = 4 * 1024
_GOOGLE_REQUEST_TIMEOUT_SECONDS = 15.0
_GOOGLE_IMPORT_TIMEOUT_SECONDS = 180.0
_MAX_CONCURRENT_MATERIAL_IMPORTS = 1
_MAX_CONCURRENT_MATERIAL_UPLOADS = 1
_MATERIAL_MULTIPART_OVERHEAD_BYTES = 256 * 1024
_MATERIAL_UPLOAD_TIMEOUT_SECONDS = 120.0
_MATERIAL_IMPORT_TIMEOUT_SECONDS = 180.0


class _SnapshotRequestTooLarge(MultiPartException):
    """multipart 请求在解析阶段超过总字节上限。"""


class _DuplicateJsonKey(ValueError):
    """JSON 请求不能依赖重复键的解析器覆盖顺序。"""


class _DownloadLease:
    """确保一个下载并发名额最多释放一次。"""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._semaphore.release()


class _LeasedBytesIterator(AsyncIterator[bytes]):
    """分块读取内存内容，并允许在首次迭代前安全关闭。"""

    def __init__(self, content: bytes, lease: _DownloadLease) -> None:
        self._content = content
        self._lease = lease
        self._offset = 0

    def __aiter__(self) -> "_LeasedBytesIterator":
        return self

    async def __anext__(self) -> bytes:
        if self._offset >= len(self._content):
            self._lease.release()
            raise StopAsyncIteration
        next_offset = min(
            self._offset + _DOWNLOAD_CHUNK_BYTES,
            len(self._content),
        )
        chunk = self._content[self._offset:next_offset]
        self._offset = next_offset
        return chunk

    async def aclose(self) -> None:
        self._lease.release()


class _SnapshotDownloadResponse(StreamingResponse):
    """无论发送完成、断连还是取消，都归还下载名额。"""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        lease: _DownloadLease,
        content_length: int,
    ) -> None:
        super().__init__(
            content,
            media_type="application/zip",
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": (
                    f'attachment; filename="{_DOWNLOAD_FILENAME}"'
                ),
                "Content-Length": str(content_length),
            },
        )
        self._lease = lease

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._lease.release()


def _release_after_task(
    task: asyncio.Task[object],
    lease: _DownloadLease,
) -> None:
    """消费后台导出异常，并在真实导出结束后释放名额。"""
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
    if isinstance(error, QuestionnaireSnapshotInvalidError):
        raise HTTPException(status_code=422, detail=_INVALID_DETAIL) from error
    if isinstance(error, QuestionnaireSnapshotConflictError):
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL) from error
    if isinstance(error, QuestionnaireSnapshotNotFoundError):
        raise HTTPException(status_code=404, detail="问卷快照不存在") from error
    raise HTTPException(status_code=500, detail=_INTERNAL_DETAIL) from error


def _raise_bested_http_error(error: Exception) -> None:
    if isinstance(error, BestedQuestionnaireInvalidError):
        raise HTTPException(
            status_code=422,
            detail="倍市得原问卷无效或不受支持",
        ) from error
    if isinstance(error, BestedQuestionnaireConflictError):
        raise HTTPException(
            status_code=409,
            detail="同一问卷快照 ID 已存在不同内容",
        ) from error
    if isinstance(error, BestedQuestionnaireInternalError):
        raise HTTPException(
            status_code=500,
            detail="倍市得问卷导入暂时不可用",
        ) from error
    raise HTTPException(
        status_code=500,
        detail="倍市得问卷导入暂时不可用",
    ) from error


def _raise_google_http_error(error: Exception) -> None:
    if isinstance(error, GoogleFormsQuestionnaireInvalidError):
        raise HTTPException(
            status_code=422,
            detail="Google Forms 问卷 ID 无效",
        ) from error
    if isinstance(error, GoogleFormsQuestionnaireAuthRequiredError):
        raise HTTPException(
            status_code=401,
            detail="请连接或重新授权 Google Forms",
        ) from error
    if isinstance(error, GoogleFormsQuestionnairePermissionError):
        raise HTTPException(
            status_code=403,
            detail="当前 Google 账号无权读取该问卷",
        ) from error
    if isinstance(error, GoogleFormsQuestionnaireNotFoundError):
        raise HTTPException(
            status_code=404,
            detail="Google Forms 问卷不存在",
        ) from error
    if isinstance(error, GoogleFormsQuestionnaireRetryableError):
        raise HTTPException(
            status_code=503,
            detail="Google Forms 暂时不可用，请稍后重试",
        ) from error
    if isinstance(error, GoogleFormsQuestionnaireProviderError):
        raise HTTPException(
            status_code=502,
            detail="Google Forms 返回了无法处理的问卷数据",
        ) from error
    if isinstance(error, GoogleFormsQuestionnaireConflictError):
        raise HTTPException(
            status_code=409,
            detail="同一 Google Forms 快照 ID 已存在不同内容",
        ) from error
    if isinstance(error, GoogleFormsQuestionnaireInternalError):
        raise HTTPException(
            status_code=500,
            detail="Google Forms 问卷导入暂时不可用",
        ) from error
    raise HTTPException(
        status_code=500,
        detail="Google Forms 问卷导入暂时不可用",
    ) from error


def _raise_material_http_error(error: Exception) -> None:
    if isinstance(error, QuestionnaireMaterialInvalidError):
        raise HTTPException(
            status_code=422,
            detail="问卷截图材料无效或不受支持",
        ) from error
    if isinstance(error, QuestionnaireMaterialConflictError):
        raise HTTPException(
            status_code=409,
            detail="同一截图材料快照 ID 已存在不同内容",
        ) from error
    if isinstance(error, QuestionnaireMaterialInternalError):
        raise HTTPException(
            status_code=500,
            detail="问卷截图材料导入暂时不可用",
        ) from error
    raise HTTPException(
        status_code=500,
        detail="问卷截图材料导入暂时不可用",
    ) from error


def _reject_duplicate_json_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey()
        result[key] = value
    return result


async def _read_google_request_body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="Google Forms 导入请求无效",
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=422,
                detail="Google Forms 导入请求无效",
            )
        if declared_length > _GOOGLE_REQUEST_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail="Google Forms 导入请求超过大小限制",
            )

    content = bytearray()
    try:
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > _GOOGLE_REQUEST_MAX_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Google Forms 导入请求超过大小限制",
                )
    except HTTPException:
        raise
    except ClientDisconnect as error:
        raise HTTPException(
            status_code=400,
            detail="Google Forms 导入请求未完整发送",
        ) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="Google Forms 问卷导入暂时不可用",
        ) from error
    return bytes(content)


async def _parse_google_request(
    request: Request,
) -> GoogleFormsSnapshotImportRequest:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "application/json":
        raise HTTPException(
            status_code=415,
            detail="Google Forms 导入请求必须使用 JSON",
        )
    content = await _read_google_request_body(request)
    if not content:
        raise HTTPException(
            status_code=422,
            detail="Google Forms 导入请求不能为空",
        )
    try:
        value = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
        return GoogleFormsSnapshotImportRequest.model_validate(value)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        _DuplicateJsonKey,
        ValidationError,
    ) as error:
        raise HTTPException(
            status_code=422,
            detail="Google Forms 导入请求无效",
        ) from error


async def _bounded_request_stream(
    request: Request,
    max_file_bytes: int,
) -> AsyncIterator[bytes]:
    limit = max_file_bytes + _MAX_MULTIPART_OVERHEAD_BYTES
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _SnapshotRequestTooLarge("request too large")
        yield chunk


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


async def _parse_file_upload(
    request: Request,
    *,
    max_file_bytes: int,
    too_large_detail: str,
    invalid_detail: str,
    internal_detail: str,
) -> FormData:
    parser = MultiPartParser(
        headers=request.headers,
        stream=_bounded_request_stream(request, max_file_bytes),
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
    except _SnapshotRequestTooLarge as error:
        raise HTTPException(
            status_code=413,
            detail=too_large_detail,
        ) from error
    except (MultiPartException, MultipartParseError, KeyError) as error:
        raise HTTPException(
            status_code=422,
            detail=invalid_detail,
        ) from error
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail=internal_detail,
        ) from error
    finally:
        if not succeeded:
            _close_parser_temporaries(parser)


async def _parse_snapshot_upload(request: Request) -> FormData:
    return await _parse_file_upload(
        request,
        max_file_bytes=MAX_SNAPSHOT_UPLOAD_BYTES,
        too_large_detail="问卷快照包超过上传大小限制",
        invalid_detail="问卷快照上传请求无效",
        internal_detail=_INTERNAL_DETAIL,
    )


async def _parse_bested_upload(request: Request) -> FormData:
    return await _parse_file_upload(
        request,
        max_file_bytes=MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES,
        too_large_detail="倍市得原问卷超过上传大小限制",
        invalid_detail="倍市得原问卷上传请求无效",
        internal_detail="倍市得问卷导入暂时不可用",
    )


async def _bounded_material_request_stream(
    request: Request,
) -> AsyncIterator[bytes]:
    limit = (
        MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES
        + _MATERIAL_MULTIPART_OVERHEAD_BYTES
    )
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="问卷截图材料上传请求无效",
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=422,
                detail="问卷截图材料上传请求无效",
            )
        if declared_length > limit:
            raise HTTPException(
                status_code=413,
                detail="问卷截图材料超过上传大小限制",
            )

    total = 0
    try:
        async for chunk in request.stream():
            total += len(chunk)
            if total > limit:
                raise _SnapshotRequestTooLarge("request too large")
            yield chunk
    except (_SnapshotRequestTooLarge, HTTPException):
        raise
    except ClientDisconnect as error:
        raise HTTPException(
            status_code=400,
            detail="问卷截图材料上传未完整发送",
        ) from error


async def _parse_material_upload(request: Request) -> FormData:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail="问卷截图材料必须使用 multipart/form-data 上传",
        )
    parser = MultiPartParser(
        headers=request.headers,
        stream=_bounded_material_request_stream(request),
        max_files=MAX_MATERIAL_SCREENSHOT_FILES,
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
    except _SnapshotRequestTooLarge as error:
        raise HTTPException(
            status_code=413,
            detail="问卷截图材料超过上传大小限制",
        ) from error
    except HTTPException:
        raise
    except (MultiPartException, MultipartParseError, KeyError) as error:
        raise HTTPException(
            status_code=422,
            detail="问卷截图材料上传请求无效",
        ) from error
    except asyncio.CancelledError:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=500,
            detail="问卷截图材料导入暂时不可用",
        ) from error
    finally:
        if not succeeded:
            _close_parser_temporaries(parser)


def create_questionnaire_sources_router(
    api: QuestionnaireSnapshotApi,
) -> APIRouter:
    """创建只依赖显式注入业务门面的快照路由。"""
    if not isinstance(api, QuestionnaireSnapshotApi):
        raise TypeError("api 必须是 QuestionnaireSnapshotApi")

    router = APIRouter()
    package_read_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_DOWNLOADS)

    @router.post(
        "/api/questionnaire-sources/snapshots",
        response_model=QuestionnaireSnapshotSummary,
    )
    async def upload_questionnaire_snapshot(
        request: Request,
    ) -> QuestionnaireSnapshotSummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        form = await _parse_snapshot_upload(request)
        try:
            items = form.multi_items()
            if (
                len(items) != 1
                or items[0][0] != "file"
                or not isinstance(items[0][1], UploadFile)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="问卷快照上传请求必须只包含 file 文件字段",
                )
            file = items[0][1]
            filename = str(file.filename or "").strip().casefold()
            if not filename.endswith(".zip"):
                raise HTTPException(
                    status_code=422,
                    detail="请上传 .zip 格式的问卷快照包",
                )
            try:
                content = await file.read(MAX_SNAPSHOT_UPLOAD_BYTES + 1)
            except Exception as error:
                raise HTTPException(
                    status_code=500,
                    detail=_INTERNAL_DETAIL,
                ) from error
            if not content:
                raise HTTPException(
                    status_code=422,
                    detail="问卷快照包不能为空",
                )
            if len(content) > MAX_SNAPSHOT_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="问卷快照包超过上传大小限制",
                )

            try:
                return await api.import_snapshot(owner_ref, content)
            except Exception as error:
                _raise_http_error(error)
                raise AssertionError("unreachable")
        finally:
            await form.close()

    @router.get(
        "/api/questionnaire-sources/snapshots/{snapshot_id}",
        response_model=QuestionnaireSnapshotSummary,
    )
    async def get_questionnaire_snapshot(
        snapshot_id: str,
        request: Request,
    ) -> QuestionnaireSnapshotSummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        await package_read_semaphore.acquire()
        lease = _DownloadLease(package_read_semaphore)
        load_task = asyncio.create_task(
            api.get_snapshot(owner_ref, snapshot_id)
        )
        try:
            summary = await asyncio.shield(load_task)
        except asyncio.CancelledError:
            if load_task.done():
                _release_after_task(load_task, lease)
            else:
                load_task.add_done_callback(
                    lambda task: _release_after_task(task, lease)
                )
            raise
        except Exception as error:
            lease.release()
            _raise_http_error(error)
            raise AssertionError("unreachable")
        except BaseException:
            lease.release()
            raise
        lease.release()
        return summary

    @router.get(
        "/api/questionnaire-sources/snapshots/{snapshot_id}/download",
    )
    async def download_questionnaire_snapshot(
        snapshot_id: str,
        request: Request,
    ) -> StreamingResponse:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        await package_read_semaphore.acquire()
        lease = _DownloadLease(package_read_semaphore)
        export_task = asyncio.create_task(
            api.export_snapshot(owner_ref, snapshot_id)
        )
        try:
            content = await asyncio.shield(export_task)
        except asyncio.CancelledError:
            if export_task.done():
                _release_after_task(export_task, lease)
            else:
                export_task.add_done_callback(
                    lambda task: _release_after_task(task, lease)
                )
            raise
        except Exception as error:
            lease.release()
            _raise_http_error(error)
            raise AssertionError("unreachable")
        except BaseException:
            lease.release()
            raise

        return _SnapshotDownloadResponse(
            _LeasedBytesIterator(content, lease),
            lease=lease,
            content_length=len(content),
        )

    return router


def create_bested_questionnaire_sources_router(
    api: BestedQuestionnaireSnapshotApi,
) -> APIRouter:
    """创建倍市得原问卷上传到完整快照的独立路由。"""
    if not isinstance(api, BestedQuestionnaireSnapshotApi):
        raise TypeError("api 必须是 BestedQuestionnaireSnapshotApi")

    router = APIRouter()
    import_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BESTED_IMPORTS)
    upload_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_BESTED_UPLOADS)

    @router.post(
        "/api/questionnaire-sources/bested/snapshots",
        response_model=QuestionnaireSnapshotSummary,
    )
    async def upload_bested_questionnaire(
        request: Request,
    ) -> QuestionnaireSnapshotSummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        if import_semaphore.locked() or upload_semaphore.locked():
            raise HTTPException(
                status_code=429,
                detail="已有倍市得问卷正在导入，请稍后重试",
            )
        await upload_semaphore.acquire()
        upload_lease = _DownloadLease(upload_semaphore)
        import_lease: _DownloadLease | None = None
        form: FormData | None = None
        release_deferred = False
        try:
            try:
                form = await asyncio.wait_for(
                    _parse_bested_upload(request),
                    timeout=_BESTED_UPLOAD_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise HTTPException(
                    status_code=408,
                    detail="倍市得原问卷上传超时，请重试",
                ) from error
            items = form.multi_items()
            if (
                len(items) != 1
                or items[0][0] != "file"
                or not isinstance(items[0][1], UploadFile)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="倍市得原问卷上传请求必须只包含 file 文件字段",
                )
            file = items[0][1]
            filename = str(file.filename or "").strip()
            normalized_filename = filename.replace("\\", "/")
            if not normalized_filename.casefold().endswith(".xlsx"):
                raise HTTPException(
                    status_code=422,
                    detail="请上传 .xlsx 格式的倍市得原问卷",
                )
            try:
                content = await file.read(
                    MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES + 1
                )
            except Exception as error:
                raise HTTPException(
                    status_code=500,
                    detail="倍市得问卷导入暂时不可用",
                ) from error
            if not content:
                raise HTTPException(
                    status_code=422,
                    detail="倍市得原问卷不能为空",
                )
            if len(content) > MAX_BESTED_QUESTIONNAIRE_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="倍市得原问卷超过上传大小限制",
                )
            await form.close()
            form = None
            await import_semaphore.acquire()
            import_lease = _DownloadLease(import_semaphore)
            upload_lease.release()

            import_task = asyncio.create_task(
                api.import_questionnaire(owner_ref, filename, content)
            )
            try:
                summary = await asyncio.shield(import_task)
            except asyncio.CancelledError:
                release_deferred = True
                assert import_lease is not None
                if import_task.done():
                    _release_after_task(import_task, import_lease)
                else:
                    import_task.add_done_callback(
                        lambda task: _release_after_task(task, import_lease)
                    )
                raise
            except Exception as error:
                _raise_bested_http_error(error)
                raise AssertionError("unreachable")
            return summary
        finally:
            try:
                if form is not None:
                    await form.close()
            finally:
                upload_lease.release()
                if import_lease is not None and not release_deferred:
                    import_lease.release()

    return router


def create_google_forms_questionnaire_sources_router(
    api: GoogleFormsQuestionnaireSnapshotApi,
) -> APIRouter:
    """创建 Google Forms 授权读取并保存完整快照的独立路由。"""
    if not isinstance(api, GoogleFormsQuestionnaireSnapshotApi):
        raise TypeError("api 必须是 GoogleFormsQuestionnaireSnapshotApi")

    router = APIRouter()
    import_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_GOOGLE_IMPORTS)

    @router.post(
        "/api/questionnaire-sources/google-forms/snapshots",
        response_model=QuestionnaireSnapshotSummary,
    )
    async def import_google_forms_questionnaire(
        request: Request,
    ) -> QuestionnaireSnapshotSummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        if import_semaphore.locked():
            raise HTTPException(
                status_code=429,
                detail="已有 Google Forms 问卷正在导入，请稍后重试",
            )
        await import_semaphore.acquire()
        lease = _DownloadLease(import_semaphore)
        release_deferred = False
        try:
            try:
                payload = await asyncio.wait_for(
                    _parse_google_request(request),
                    timeout=_GOOGLE_REQUEST_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise HTTPException(
                    status_code=408,
                    detail="Google Forms 导入请求发送超时，请重试",
                ) from error

            import_task = asyncio.create_task(
                api.import_questionnaire(owner_ref, payload.form_id)
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(import_task),
                    timeout=_GOOGLE_IMPORT_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                release_deferred = True
                import_task.cancel()
                if import_task.done():
                    _release_after_task(import_task, lease)
                else:
                    import_task.add_done_callback(
                        lambda task: _release_after_task(task, lease)
                    )
                raise HTTPException(
                    status_code=504,
                    detail="Google Forms 问卷导入超时，请稍后重试",
                ) from error
            except asyncio.CancelledError:
                release_deferred = True
                if import_task.done():
                    _release_after_task(import_task, lease)
                else:
                    import_task.add_done_callback(
                        lambda task: _release_after_task(task, lease)
                    )
                raise
            except Exception as error:
                _raise_google_http_error(error)
                raise AssertionError("unreachable")
        finally:
            if not release_deferred:
                lease.release()

    return router


def create_questionnaire_material_sources_router(
    api: QuestionnaireMaterialSnapshotApi,
) -> APIRouter:
    """创建连续截图材料到低可信快照的独立路由。"""
    if not isinstance(api, QuestionnaireMaterialSnapshotApi):
        raise TypeError("api 必须是 QuestionnaireMaterialSnapshotApi")

    router = APIRouter()
    import_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MATERIAL_IMPORTS)
    upload_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_MATERIAL_UPLOADS)

    @router.post(
        "/api/questionnaire-sources/materials/snapshots",
        response_model=QuestionnaireMaterialUploadSummary,
    )
    async def upload_questionnaire_material_screenshots(
        request: Request,
    ) -> QuestionnaireMaterialUploadSummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        if import_semaphore.locked() or upload_semaphore.locked():
            raise HTTPException(
                status_code=429,
                detail="已有问卷截图材料正在导入，请稍后重试",
            )

        await upload_semaphore.acquire()
        upload_lease = _DownloadLease(upload_semaphore)
        import_lease: _DownloadLease | None = None
        form: FormData | None = None
        release_deferred = False
        try:
            try:
                form = await asyncio.wait_for(
                    _parse_material_upload(request),
                    timeout=_MATERIAL_UPLOAD_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise HTTPException(
                    status_code=408,
                    detail="问卷截图材料上传超时，请重试",
                ) from error

            items = form.multi_items()
            if (
                not items
                or len(items) > MAX_MATERIAL_SCREENSHOT_FILES
                or any(
                    key != "files" or not isinstance(value, UploadFile)
                    for key, value in items
                )
            ):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "问卷截图材料上传请求只能包含 1 至 "
                        f"{MAX_MATERIAL_SCREENSHOT_FILES} 个 files 文件字段"
                    ),
                )

            screenshots: list[QuestionnaireMaterialScreenshot] = []
            total_size = 0
            for _, value in items:
                assert isinstance(value, UploadFile)
                mime_type = str(value.content_type or "").split(";", 1)[0]
                mime_type = mime_type.strip().casefold()
                if mime_type not in SUPPORTED_MATERIAL_SCREENSHOT_MIME_TYPES:
                    raise HTTPException(
                        status_code=415,
                        detail="仅支持 PNG、JPEG 或 WebP 问卷截图",
                    )
                try:
                    content = await value.read(
                        MAX_MATERIAL_SCREENSHOT_BYTES + 1
                    )
                except Exception as error:
                    raise HTTPException(
                        status_code=500,
                        detail="问卷截图材料导入暂时不可用",
                    ) from error
                if not content:
                    raise HTTPException(
                        status_code=422,
                        detail="问卷截图文件不能为空",
                    )
                if len(content) > MAX_MATERIAL_SCREENSHOT_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="问卷截图材料超过上传大小限制",
                    )
                total_size += len(content)
                if total_size > MAX_MATERIAL_SCREENSHOTS_TOTAL_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="问卷截图材料超过上传大小限制",
                    )
                screenshots.append(QuestionnaireMaterialScreenshot(
                    filename=str(value.filename or ""),
                    mime_type=mime_type,
                    content=content,
                ))

            await form.close()
            form = None
            await import_semaphore.acquire()
            import_lease = _DownloadLease(import_semaphore)
            upload_lease.release()

            import_task = asyncio.create_task(
                api.import_screenshots(owner_ref, tuple(screenshots))
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(import_task),
                    timeout=_MATERIAL_IMPORT_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                release_deferred = True
                import_task.cancel()
                assert import_lease is not None
                if import_task.done():
                    _release_after_task(import_task, import_lease)
                else:
                    import_task.add_done_callback(
                        lambda task: _release_after_task(task, import_lease)
                    )
                raise HTTPException(
                    status_code=504,
                    detail="问卷截图材料处理超时，请稍后重试",
                ) from error
            except asyncio.CancelledError:
                release_deferred = True
                assert import_lease is not None
                if import_task.done():
                    _release_after_task(import_task, import_lease)
                else:
                    import_task.add_done_callback(
                        lambda task: _release_after_task(task, import_lease)
                    )
                raise
            except Exception as error:
                _raise_material_http_error(error)
                raise AssertionError("unreachable")
        finally:
            try:
                if form is not None:
                    await form.close()
            finally:
                upload_lease.release()
                if import_lease is not None and not release_deferred:
                    import_lease.release()

    return router
