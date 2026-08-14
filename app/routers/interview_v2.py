"""访谈报告 V2：上传预检与 Sheet/玩家映射 HTTP 边界。"""
import json
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from app.core.config import (
    INTERVIEW_V2_ENABLED,
    INTERVIEW_V2_MAX_FILE_BYTES,
)
from app.schemas.interview_v2 import (
    InterviewV2ErrorResponse,
    InterviewV2ImportResponse,
    InterviewV2UploadAttemptResponse,
)
from app.schemas.interview_v2_mapping import (
    InterviewV2GroupMappingConfirmRequest,
    InterviewV2GroupMappingDraftRequest,
    InterviewV2GroupMappingRestoreRequest,
    InterviewV2GroupMappingResponse,
    InterviewV2GroupProposalResponse,
)
from app.services.auth import _require_feature
from app.services.interview_v2_import_service import (
    InterviewV2ImportError,
    create_upload_attempt,
    get_upload_attempt,
    run_upload_precheck,
    upload_attempt_needs_precheck,
)
from app.services.interview_v2_mapping_service import (
    confirm_group_mapping,
    get_group_proposals,
    get_interview_import_with_mapping_status as get_interview_import,
    restore_group_mapping,
    save_group_mapping,
)

router = APIRouter()
_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 64 * 1024
_MULTIPART_FIELD_MAX_BYTES = 20 * 1024
_MULTIPART_LIMIT_MESSAGE = "INTERVIEW_V2_MULTIPART_LIMIT_EXCEEDED"
_MAPPING_JSON_MAX_BYTES = 1024 * 1024


def _trace_id(request: Request) -> str:
    headers = getattr(request, "headers", None)
    supplied = headers.get("x-request-id", "") if headers is not None else ""
    return supplied.strip() or f"trace_{uuid4().hex}"


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    suggested_action: str = "",
    context: dict | None = None,
    trace_id: str = "",
) -> JSONResponse:
    body = InterviewV2ErrorResponse(
        error={
            "code": code,
            "message": message,
            "retryable": retryable,
            "suggested_action": suggested_action,
            "context": context or {},
            "trace_id": trace_id or _trace_id(request),
        }
    )
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


def _service_error_response(request: Request, exc: InterviewV2ImportError) -> JSONResponse:
    return _error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
        suggested_action=exc.suggested_action or "",
        context=exc.context,
        trace_id=exc.trace_id or "",
    )


async def _bounded_request_stream(request: Request):
    total = 0
    limit = INTERVIEW_V2_MAX_FILE_BYTES + _MULTIPART_OVERHEAD_BYTES
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise MultiPartException(_MULTIPART_LIMIT_MESSAGE)
        yield chunk


async def _parse_upload_form(request: Request):
    content_type = request.headers.get("content-type", "")
    if not content_type.lower().startswith("multipart/form-data"):
        raise MultiPartException("Content-Type must be multipart/form-data.")
    parser = MultiPartParser(
        headers=request.headers,
        stream=_bounded_request_stream(request),
        max_files=1,
        max_fields=3,
        max_part_size=_MULTIPART_FIELD_MAX_BYTES,
    )
    return await parser.parse()


async def _read_upload_limited(file: UploadFile) -> bytes:
    content = bytearray()
    while len(content) <= INTERVIEW_V2_MAX_FILE_BYTES:
        remaining = INTERVIEW_V2_MAX_FILE_BYTES + 1 - len(content)
        chunk = await file.read(min(_UPLOAD_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        content.extend(chunk)
    return bytes(content)


async def _read_mapping_json(request: Request) -> dict:
    content_type = (
        request.headers.get("content-type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if content_type != "application/json" and not (
        content_type.startswith("application/") and content_type.endswith("+json")
    ):
        raise ValueError("mapping request must use application/json")
    content_length = request.headers.get("content-length", "").strip()
    if content_length.isdigit() and int(content_length) > _MAPPING_JSON_MAX_BYTES:
        raise OverflowError("mapping request exceeds size limit")
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > _MAPPING_JSON_MAX_BYTES:
            raise OverflowError("mapping request exceeds size limit")
        content.extend(chunk)
    value = json.loads(bytes(content).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("mapping request must be a JSON object")
    return value


def _mapping_request_error(
    request: Request, *, too_large: bool = False
) -> JSONResponse:
    return _error_response(
        request,
        status_code=413 if too_large else 422,
        code="MAPPING_REQUEST_INVALID",
        message=(
            "分组映射请求超过 1MB 上限。"
            if too_large
            else "分组映射请求格式无效。"
        ),
        suggested_action="review_group_mapping",
        context={"limit_bytes": _MAPPING_JSON_MAX_BYTES} if too_large else {},
    )


def _disabled_response(request: Request) -> JSONResponse:
    return _error_response(
        request,
        status_code=503,
        code="INTERVIEW_V2_DISABLED",
        message="访谈报告 V2 当前未启用。",
        suggested_action="contact_administrator",
    )


@router.post(
    "/api/v1/interview-upload-attempts",
    response_model=InterviewV2UploadAttemptResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        409: {"model": InterviewV2ErrorResponse},
        413: {"model": InterviewV2ErrorResponse},
        422: {"model": InterviewV2ErrorResponse},
        500: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def create_interview_upload_attempt(
    request: Request,
    background_tasks: BackgroundTasks,
):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)

    idempotency_key = request.headers.get("idempotency-key", "").strip()
    if not idempotency_key:
        return _error_response(
            request,
            status_code=422,
            code="IDEMPOTENCY_KEY_INVALID",
            message="请求缺少有效的 Idempotency-Key。",
            suggested_action="generate_idempotency_key",
        )

    content_length = request.headers.get("content-length", "").strip()
    if content_length.isdigit() and int(content_length) > (
        INTERVIEW_V2_MAX_FILE_BYTES + _MULTIPART_OVERHEAD_BYTES
    ):
        return _error_response(
            request,
            status_code=413,
            code="WORKBOOK_LIMIT_EXCEEDED",
            message="上传文件超过系统上限，请拆分或精简后重试。",
            suggested_action="reduce_workbook_size",
            context={"limit_bytes": INTERVIEW_V2_MAX_FILE_BYTES},
        )

    try:
        form = await _parse_upload_form(request)
    except (KeyError, MultiPartException, ValueError) as exc:
        if str(exc) == _MULTIPART_LIMIT_MESSAGE:
            return _error_response(
                request,
                status_code=413,
                code="WORKBOOK_LIMIT_EXCEEDED",
                message="上传文件超过系统上限，请拆分或精简后重试。",
                suggested_action="reduce_workbook_size",
                context={"limit_bytes": INTERVIEW_V2_MAX_FILE_BYTES},
            )
        return _error_response(
            request,
            status_code=422,
            code="UPLOAD_REQUEST_INVALID",
            message="上传请求格式无效，请重新选择文件后重试。",
            suggested_action="retry_upload",
        )

    try:
        file = form.get("file")
        research_focus = form.get("research_focus", "")
        file_contract_version = form.get("file_contract_version", "")
        acknowledged_raw = form.get("contract_acknowledged", "")
        if not isinstance(file, UploadFile):
            return _error_response(
                request,
                status_code=422,
                code="UPLOAD_EMPTY",
                message="请选择一个包含内容的 .xlsx 文件。",
                suggested_action="select_workbook",
            )
        if not isinstance(research_focus, str):
            return _error_response(
                request,
                status_code=422,
                code="UPLOAD_REQUEST_INVALID",
                message="报告重点格式无效。",
                suggested_action="retry_upload",
            )
        if len(research_focus) > 4000:
            return _error_response(
                request,
                status_code=422,
                code="RESEARCH_FOCUS_TOO_LONG",
                message="报告重点不能超过 4000 个字符。",
                suggested_action="shorten_research_focus",
                context={"limit_chars": 4000},
            )
        if not isinstance(file_contract_version, str):
            file_contract_version = ""
        contract_acknowledged = (
            isinstance(acknowledged_raw, str)
            and acknowledged_raw.strip().lower() in {"1", "true", "yes", "on"}
        )

        content = await _read_upload_limited(file)
        if len(content) > INTERVIEW_V2_MAX_FILE_BYTES:
            return _error_response(
                request,
                status_code=413,
                code="WORKBOOK_LIMIT_EXCEEDED",
                message="上传文件超过系统上限，请拆分或精简后重试。",
                suggested_action="reduce_workbook_size",
                context={"limit_bytes": INTERVIEW_V2_MAX_FILE_BYTES},
            )

        try:
            result, should_schedule = create_upload_attempt(
                filename=file.filename or "interview.xlsx",
                content=content,
                login=login,
                research_focus=research_focus,
                file_contract_version=file_contract_version,
                contract_acknowledged=contract_acknowledged,
                idempotency_key=idempotency_key,
            )
        except InterviewV2ImportError as exc:
            return _service_error_response(request, exc)
    finally:
        await form.close()

    if should_schedule:
        background_tasks.add_task(
            run_upload_precheck,
            result["upload_attempt_id"],
        )
    return result


@router.get(
    "/api/v1/interview-upload-attempts/{upload_attempt_id}",
    response_model=InterviewV2UploadAttemptResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        404: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def get_interview_upload_attempt(
    upload_attempt_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)
    try:
        result = get_upload_attempt(upload_attempt_id, login)
        if upload_attempt_needs_precheck(upload_attempt_id, login):
            background_tasks.add_task(run_upload_precheck, upload_attempt_id)
        return result
    except InterviewV2ImportError as exc:
        return _service_error_response(request, exc)


@router.get(
    "/api/v1/interview-imports/{import_id}",
    response_model=InterviewV2ImportResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        404: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def get_interview_import_status(import_id: str, request: Request):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)
    try:
        return get_interview_import(import_id, login)
    except InterviewV2ImportError as exc:
        return _service_error_response(request, exc)


@router.get(
    "/api/v1/interview-imports/{import_id}/group-proposals",
    response_model=InterviewV2GroupProposalResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        404: {"model": InterviewV2ErrorResponse},
        500: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def get_interview_group_proposals(import_id: str, request: Request):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)
    try:
        return get_group_proposals(import_id, login)
    except InterviewV2ImportError as exc:
        return _service_error_response(request, exc)


@router.put(
    "/api/v1/interview-imports/{import_id}/group-mapping",
    response_model=InterviewV2GroupMappingResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        404: {"model": InterviewV2ErrorResponse},
        409: {"model": InterviewV2ErrorResponse},
        413: {"model": InterviewV2ErrorResponse},
        422: {"model": InterviewV2ErrorResponse},
        500: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def put_interview_group_mapping(import_id: str, request: Request):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)
    try:
        raw = await _read_mapping_json(request)
        payload = InterviewV2GroupMappingDraftRequest.model_validate(raw).model_dump(
            mode="json"
        )
    except OverflowError:
        return _mapping_request_error(request, too_large=True)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ):
        return _mapping_request_error(request)
    try:
        return save_group_mapping(import_id, payload, login)
    except InterviewV2ImportError as exc:
        return _service_error_response(request, exc)


@router.post(
    "/api/v1/interview-imports/{import_id}/group-mapping:restore",
    response_model=InterviewV2GroupMappingResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        404: {"model": InterviewV2ErrorResponse},
        409: {"model": InterviewV2ErrorResponse},
        413: {"model": InterviewV2ErrorResponse},
        422: {"model": InterviewV2ErrorResponse},
        500: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def restore_interview_group_mapping(import_id: str, request: Request):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)
    try:
        raw = await _read_mapping_json(request)
        payload = InterviewV2GroupMappingRestoreRequest.model_validate(raw).model_dump(
            mode="json"
        )
    except OverflowError:
        return _mapping_request_error(request, too_large=True)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ):
        return _mapping_request_error(request)
    try:
        return restore_group_mapping(import_id, payload, login)
    except InterviewV2ImportError as exc:
        return _service_error_response(request, exc)


@router.post(
    "/api/v1/interview-imports/{import_id}/group-mapping:confirm",
    response_model=InterviewV2GroupMappingResponse,
    responses={
        400: {"model": InterviewV2ErrorResponse},
        403: {"description": "沿用平台现有功能权限错误响应"},
        404: {"model": InterviewV2ErrorResponse},
        409: {"model": InterviewV2ErrorResponse},
        413: {"model": InterviewV2ErrorResponse},
        422: {"model": InterviewV2ErrorResponse},
        500: {"model": InterviewV2ErrorResponse},
        503: {"model": InterviewV2ErrorResponse},
    },
)
async def confirm_interview_group_mapping(import_id: str, request: Request):
    login = await _require_feature(request, "interview")
    if not INTERVIEW_V2_ENABLED:
        return _disabled_response(request)
    try:
        raw = await _read_mapping_json(request)
        payload = InterviewV2GroupMappingConfirmRequest.model_validate(raw).model_dump(
            mode="json"
        )
    except OverflowError:
        return _mapping_request_error(request, too_large=True)
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ):
        return _mapping_request_error(request)
    try:
        return confirm_group_mapping(import_id, payload, login)
    except InterviewV2ImportError as exc:
        return _service_error_response(request, exc)
