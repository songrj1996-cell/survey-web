"""routers/comment_analysis:评论分析接口（参数解析 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在:
  services/comment_upload / comment_preprocess / comment_run
"""
import os

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.comment_preprocess import comment_preprocess_stream
from app.services.comment_run import comment_run_stream
from app.services.comment_upload import handle_comment_upload
from app.storage.sessions import get_session

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
    sess = get_session(session_id)
    if sess.get("kind") != "comment":
        raise HTTPException(status_code=400, detail="该会话不是评论分析任务")
    upload_path = sess.get("comment_upload_path")
    if not upload_path or not os.path.exists(upload_path):
        raise HTTPException(status_code=400, detail="评论文件不存在或已过期，请重新上传")
    return StreamingResponse(comment_preprocess_stream(session_id, request), media_type="text/event-stream")


@router.get("/api/comment-analysis/run/{session_id}")
async def comment_analysis_run(session_id: str, request: Request):
    sess = get_session(session_id)
    if sess.get("kind") != "comment" or not sess.get("comment_preprocess_done") or not sess.get("comment_sample"):
        raise HTTPException(status_code=400, detail="该会话尚未完成评论预处理，请重新上传并等待预处理完成")
    return StreamingResponse(comment_run_stream(session_id, request), media_type="text/event-stream")
