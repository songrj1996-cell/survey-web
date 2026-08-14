"""问卷 PDF 中可信材料上传接口的独立 HTTP 壳。"""

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
    QuestionnairePdfMaterialUploadSummary,
)
from app.services.auth import _require_feature
from app.services.questionnaire_pdf_material_snapshot_api import (
    MAX_QUESTIONNAIRE_PDF_BYTES,
    SUPPORTED_QUESTIONNAIRE_PDF_MIME_TYPES,
    QuestionnairePdfMaterial,
    QuestionnairePdfMaterialConflictError,
    QuestionnairePdfMaterialInternalError,
    QuestionnairePdfMaterialInvalidError,
    QuestionnairePdfMaterialSnapshotApi,
)


_PDF_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_MAX_CONCURRENT_PDF_UPLOADS = 1
_MAX_CONCURRENT_PDF_IMPORTS = 1
_PDF_UPLOAD_TIMEOUT_SECONDS = 120.0
_PDF_IMPORT_TIMEOUT_SECONDS = 180.0

_INVALID_DETAIL = "问卷 PDF 材料无效或不受支持"
_CONFLICT_DETAIL = "同一 PDF 材料快照 ID 已存在不同内容"
_INTERNAL_DETAIL = "问卷 PDF 材料导入暂时不可用"
_TOO_LARGE_DETAIL = "问卷 PDF 材料超过上传大小限制"


class _PdfRequestTooLarge(MultiPartException):
    """multipart 请求在解析阶段超过 PDF 总字节上限。"""


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
    """消费后台任务异常，并在任务真实结束后释放导入名额。"""
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


def _raise_pdf_http_error(error: Exception) -> None:
    if isinstance(error, QuestionnairePdfMaterialInvalidError):
        raise HTTPException(status_code=422, detail=_INVALID_DETAIL) from error
    if isinstance(error, QuestionnairePdfMaterialConflictError):
        raise HTTPException(status_code=409, detail=_CONFLICT_DETAIL) from error
    if isinstance(error, QuestionnairePdfMaterialInternalError):
        raise HTTPException(status_code=500, detail=_INTERNAL_DETAIL) from error
    raise HTTPException(status_code=500, detail=_INTERNAL_DETAIL) from error


async def _bounded_pdf_request_stream(
    request: Request,
) -> AsyncIterator[bytes]:
    limit = MAX_QUESTIONNAIRE_PDF_BYTES + _PDF_MULTIPART_OVERHEAD_BYTES
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_length = int(declared)
        except ValueError as error:
            raise HTTPException(
                status_code=422,
                detail="问卷 PDF 材料上传请求无效",
            ) from error
        if declared_length < 0:
            raise HTTPException(
                status_code=422,
                detail="问卷 PDF 材料上传请求无效",
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
                raise _PdfRequestTooLarge("request too large")
            yield chunk
    except (_PdfRequestTooLarge, HTTPException):
        raise
    except ClientDisconnect as error:
        raise HTTPException(
            status_code=400,
            detail="问卷 PDF 材料上传未完整发送",
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


async def _parse_pdf_upload(request: Request) -> FormData:
    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().casefold() != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail="问卷 PDF 材料必须使用 multipart/form-data 上传",
        )

    parser = MultiPartParser(
        headers=request.headers,
        stream=_bounded_pdf_request_stream(request),
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
    except _PdfRequestTooLarge as error:
        raise HTTPException(
            status_code=413,
            detail=_TOO_LARGE_DETAIL,
        ) from error
    except HTTPException:
        raise
    except (MultiPartException, MultipartParseError, KeyError) as error:
        raise HTTPException(
            status_code=422,
            detail="问卷 PDF 材料上传请求无效",
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


def create_questionnaire_pdf_material_sources_router(
    api: QuestionnairePdfMaterialSnapshotApi,
) -> APIRouter:
    """创建单 PDF 中可信问卷材料的独立上传路由。"""
    if not isinstance(api, QuestionnairePdfMaterialSnapshotApi):
        raise TypeError("api 必须是 QuestionnairePdfMaterialSnapshotApi")

    router = APIRouter()
    upload_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PDF_UPLOADS)
    import_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_PDF_IMPORTS)

    @router.post(
        "/api/questionnaire-sources/materials/pdf/snapshots",
        response_model=QuestionnairePdfMaterialUploadSummary,
    )
    async def upload_questionnaire_pdf_material(
        request: Request,
    ) -> QuestionnairePdfMaterialUploadSummary:
        login = await _require_feature(request, "survey")
        owner_ref = _owner_key(login)
        if import_semaphore.locked() or upload_semaphore.locked():
            raise HTTPException(
                status_code=429,
                detail="已有问卷 PDF 材料正在导入，请稍后重试",
            )

        await upload_semaphore.acquire()
        upload_lease = _SemaphoreLease(upload_semaphore)
        import_lease: _SemaphoreLease | None = None
        form: FormData | None = None
        release_deferred = False
        try:
            try:
                form = await asyncio.wait_for(
                    _parse_pdf_upload(request),
                    timeout=_PDF_UPLOAD_TIMEOUT_SECONDS,
                )
            except TimeoutError as error:
                raise HTTPException(
                    status_code=408,
                    detail="问卷 PDF 材料上传超时，请重试",
                ) from error

            items = form.multi_items()
            if (
                len(items) != 1
                or items[0][0] != "file"
                or not isinstance(items[0][1], UploadFile)
            ):
                raise HTTPException(
                    status_code=422,
                    detail="问卷 PDF 材料上传请求必须只包含 file 文件字段",
                )

            file = items[0][1]
            filename = str(file.filename or "").strip()
            normalized_filename = filename.replace("\\", "/")
            if PurePosixPath(normalized_filename).suffix.casefold() != ".pdf":
                raise HTTPException(
                    status_code=422,
                    detail="请上传 .pdf 格式的问卷材料",
                )

            mime_type = str(file.content_type or "").split(";", 1)[0]
            mime_type = mime_type.strip().casefold()
            if mime_type not in SUPPORTED_QUESTIONNAIRE_PDF_MIME_TYPES:
                raise HTTPException(
                    status_code=415,
                    detail="仅支持 application/pdf 问卷材料",
                )

            try:
                content = await file.read(MAX_QUESTIONNAIRE_PDF_BYTES + 1)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise HTTPException(
                    status_code=500,
                    detail=_INTERNAL_DETAIL,
                ) from error
            if not content:
                raise HTTPException(
                    status_code=422,
                    detail="问卷 PDF 文件不能为空",
                )
            if len(content) > MAX_QUESTIONNAIRE_PDF_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=_TOO_LARGE_DETAIL,
                )

            material = QuestionnairePdfMaterial(
                filename=filename,
                mime_type=mime_type,
                content=content,
            )

            await form.close()
            form = None
            await import_semaphore.acquire()
            import_lease = _SemaphoreLease(import_semaphore)
            upload_lease.release()

            import_task = asyncio.create_task(
                api.import_pdf(owner_ref, material)
            )
            try:
                return await asyncio.wait_for(
                    asyncio.shield(import_task),
                    timeout=_PDF_IMPORT_TIMEOUT_SECONDS,
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
                    detail="问卷 PDF 材料处理超时，请稍后重试",
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
                _raise_pdf_http_error(error)
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
