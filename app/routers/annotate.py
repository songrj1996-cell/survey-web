"""routers/annotate:数据标注接口（参数解析 + 权限检查 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在 services/annotate_workflow。
"""
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.core.config import DIFY_AI_DETECT_KEY, DIFY_QUALITY_KEY
from app.core.responses import _make_download_response
from app.schemas.requests import AnnotateConfirmAIRequest, AnnotateConfirmRequest
from app.services.annotate_workflow import (
    ai_detect_stream,
    annotate_set_column_config,
    annotate_set_confirmed_ai,
    build_and_save_annotate_download,
    get_annotate_history_file,
    handle_annotate_upload,
    validate_annotate_session_for_ai,
    validate_annotate_session_for_quality,
)
from app.services.audit import audit_log
from app.services.auth import _current_login

router = APIRouter()


@router.post("/api/annotate/upload")
async def annotate_upload(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    login = await _current_login(request)
    result = await handle_annotate_upload(file.filename or "upload.csv", content, login)
    await audit_log(
        request, "annotate", "上传标注数据",
        f"文件：{result['filename']}；样本行数：{result['total_rows']}",
        metadata={"session_id": result["session_id"], "rows": result["total_rows"]},
    )
    return result


@router.post("/api/annotate/{sid}/confirm-columns")
async def annotate_confirm_columns(sid: str, req: AnnotateConfirmRequest, request: Request):
    task_names = annotate_set_column_config(sid, req.id_col, req.open_text_cols, req.tasks, req.background)
    await audit_log(
        request, "annotate", "确认标注任务",
        f"会话：{sid}；主观题列数：{len(req.open_text_cols)}；任务：{', '.join(task_names) or '未选择'}",
        metadata={"session_id": sid, "open_text_cols": len(req.open_text_cols), "tasks": req.tasks},
    )
    return {"ok": True}


@router.get("/api/annotate/{sid}/run-ai-detect")
async def annotate_run_ai_detect(sid: str, request: Request):
    validate_annotate_session_for_ai(sid)
    if not DIFY_AI_DETECT_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_AI_DETECT_KEY")
    return StreamingResponse(ai_detect_stream(sid, request), media_type="text/event-stream")


@router.post("/api/annotate/{sid}/confirm-ai")
async def annotate_confirm_ai(sid: str, req: AnnotateConfirmAIRequest, request: Request):
    await annotate_set_confirmed_ai(sid, req.confirmed_ai_ids, request)
    await audit_log(
        request, "annotate", "确认 AI 作答结果",
        f"会话：{sid}；确认 AI 作答数：{len(req.confirmed_ai_ids)}",
        metadata={"session_id": sid, "confirmed_count": len(req.confirmed_ai_ids)},
    )
    return {"ok": True, "confirmed_count": len(req.confirmed_ai_ids)}


@router.get("/api/annotate/{sid}/run-quality")
async def annotate_run_quality(sid: str, request: Request):
    from app.services.annotate_workflow import quality_stream
    validate_annotate_session_for_quality(sid)
    if not DIFY_QUALITY_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_QUALITY_KEY")
    return StreamingResponse(quality_stream(sid, request), media_type="text/event-stream")


@router.get("/api/annotate/{sid}/download")
async def annotate_download(sid: str, request: Request):
    excel_bytes, download_name = await build_and_save_annotate_download(sid, request)
    await audit_log(
        request, "annotate", "下载标注结果",
        f"会话：{sid}", metadata={"session_id": sid},
    )
    return _make_download_response(
        excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        download_name,
    )


@router.get("/api/annotate-history/{history_id}/download")
async def annotate_history_download(history_id: str, request: Request):
    login = await _current_login(request)
    file_bytes, download_name = get_annotate_history_file(history_id, login)
    await audit_log(
        request, "annotate", "下载历史标注结果",
        f"历史记录：{history_id}；",
        metadata={"history_id": history_id},
    )
    return _make_download_response(
        file_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        download_name,
    )
