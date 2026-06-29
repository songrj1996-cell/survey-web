"""routers/comment_analysis:评论分析接口（参数解析 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在:
  services/comment_upload / comment_preprocess / comment_run
"""
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.comment_preprocess import (
    comment_preprocess_stream,
    validate_comment_session_for_preprocess,
)
from app.services.comment_run import comment_run_stream, validate_comment_session_for_run
from app.services.comment_upload import handle_comment_upload

router = APIRouter()


@router.post("/api/comment-analysis/upload")
async def comment_analysis_upload(
    request: Request,
    file: UploadFile = File(...),
    post_title: str = Form(""),
    post_content: str = Form(""),
):
    content = await file.read()
    login = await _current_login(request)
    result = await handle_comment_upload(
        file.filename or "upload.csv", content, post_title, post_content, login
    )
    await audit_log(
        request, "comment", "上传评论文件",
        f"文件：{result['filename']}；大小：{result['size']} bytes",
        metadata={"session_id": result["session_id"], "size": result["size"]},
    )
    return result


@router.get("/api/comment-analysis/preprocess/{session_id}")
async def comment_analysis_preprocess(session_id: str, request: Request):
    validate_comment_session_for_preprocess(session_id)
    return StreamingResponse(comment_preprocess_stream(session_id, request), media_type="text/event-stream")


@router.get("/api/comment-analysis/run/{session_id}")
async def comment_analysis_run(session_id: str, request: Request):
    validate_comment_session_for_run(session_id)
    return StreamingResponse(comment_run_stream(session_id, request), media_type="text/event-stream")
