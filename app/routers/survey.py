"""routers/survey:问卷分析主流程接口（参数解析 + 权限检查 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在 services/survey_service。
跑数表(crosstab)模式复用本组的 plan/stats/report/qa 流程，仅上传入口在 routers/crosstab。
"""
import survey_plan
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import (
    DIFY_ANALYST_KEY,
    DIFY_COLUMN_KEY,
    DIFY_CROSSTAB_PLANNER_KEY,
    DIFY_LARGE_ANALYST_KEY,
    DIFY_PLANNER_KEY,
    LARGE_SAMPLE_THRESHOLD,
)
from app.core.security import _find_history_for_login
from app.schemas.requests import (
    ColumnConfirmRequest,
    HistoryQARequest,
    PlanConfirmRequest,
    QARequest,
)
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.report_engine import _analyst_key_for_report
from app.services.survey_service import (
    columns_stream,
    compute_survey_stats,
    handle_survey_upload,
    history_qa_stream,
    plan_revision_stream,
    plan_stream,
    qa_stream,
    report_stream,
    set_survey_columns,
)
from app.storage.history import _load_history
from app.storage.sessions import get_session

router = APIRouter()


@router.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    login = await _current_login(request)
    result = await handle_survey_upload(file.filename or "upload.csv", content, login)
    await audit_log(
        request, "survey", "上传数据",
        f"文件：{result['filename']}；样本行数：{result['total_rows']}",
        metadata={"session_id": result["session_id"], "rows": result["total_rows"]},
    )
    return result


@router.get("/api/columns/{session_id}")
async def get_columns(session_id: str, request: Request):
    sess = get_session(session_id)
    if not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话中没有数据")
    if not DIFY_COLUMN_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_COLUMN_KEY（题型识别应用）")
    return StreamingResponse(columns_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/columns/{session_id}/confirm")
async def confirm_columns(session_id: str, req: ColumnConfirmRequest, request: Request):
    set_survey_columns(session_id, req.columns)
    await audit_log(
        request, "survey", "确认数据列",
        f"会话：{session_id}；确认列数：{len(req.columns)}",
        metadata={"session_id": session_id, "columns": len(req.columns)},
    )
    return {"ok": True}


@router.get("/api/plan/{session_id}")
async def get_plan(session_id: str, request: Request):
    sess = get_session(session_id)
    if not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话中没有数据，请先上传文件")
    is_crosstab = sess.get("mode") == "crosstab"
    if is_crosstab and not DIFY_CROSSTAB_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_CROSSTAB_PLANNER_KEY")
    elif not is_crosstab and not DIFY_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_PLANNER_KEY")
    return StreamingResponse(plan_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/plan/confirm")
async def confirm_plan(req: PlanConfirmRequest, request: Request):
    sess = get_session(req.session_id)
    if not sess.get("plan") or not sess.get("rows"):
        raise HTTPException(status_code=400, detail="会话状态丢失，请重新上传文件")
    if survey_plan.is_user_approval(req.user_text):
        await audit_log(
            request, "survey", "确认分析方案",
            f"会话：{req.session_id}", metadata={"session_id": req.session_id},
        )
        return JSONResponse({"approved": True})
    return StreamingResponse(
        plan_revision_stream(req.session_id, req.user_text, request),
        media_type="text/event-stream",
    )


@router.post("/api/stats/{session_id}")
async def compute_stats(session_id: str, request: Request):
    stats_md = await compute_survey_stats(session_id, request)
    return {"stats_md": stats_md}


@router.get("/api/report/{session_id}")
async def generate_report(session_id: str, request: Request):
    sess = get_session(session_id)
    if not all([sess.get("plan"), sess.get("rows"), sess.get("stats_md")]):
        raise HTTPException(status_code=400, detail="请先完成统计计算")
    use_large = sess.get("mode") == "crosstab" or any(
        len(v) > LARGE_SAMPLE_THRESHOLD for v in sess.get("open_text", {}).values()
    )
    if use_large and not DIFY_LARGE_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_LARGE_ANALYST_KEY")
    elif not use_large and not DIFY_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")
    return StreamingResponse(report_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/qa")
async def qa(req: QARequest, request: Request):
    sess = get_session(req.session_id)
    if not sess.get("analyst_conv_id"):
        raise HTTPException(status_code=400, detail="请先生成报告")
    analyst_key, analyst_key_name = _analyst_key_for_report(sess)
    if not analyst_key:
        raise HTTPException(status_code=500, detail=f"未配置 {analyst_key_name}")
    return StreamingResponse(qa_stream(req.session_id, req.question, request), media_type="text/event-stream")


@router.post("/api/history-qa")
async def history_qa(req: HistoryQARequest, request: Request):
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, req.history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    analyst_conv_id = entry.get("analyst_conv_id", "")
    if not analyst_conv_id:
        raise HTTPException(status_code=400, detail="该历史记录没有可续聊的对话")
    analyst_key, analyst_key_name = _analyst_key_for_report(entry)
    if not analyst_key:
        raise HTTPException(status_code=500, detail=f"未配置 {analyst_key_name}")
    return StreamingResponse(
        history_qa_stream(req.history_id, req.question, history, analyst_conv_id, analyst_key, analyst_key_name, request),
        media_type="text/event-stream",
    )
