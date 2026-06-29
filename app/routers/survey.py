"""routers/survey:问卷分析主流程接口（参数解析 + 权限检查 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在 services/survey_service。
跑数表(crosstab)模式复用本组的 plan/stats/report/qa 流程，仅上传入口在 routers/crosstab。
"""
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import (
    DIFY_ANALYST_KEY,
    DIFY_COLUMN_KEY,
    DIFY_CROSSTAB_PLANNER_KEY,
    DIFY_LARGE_ANALYST_KEY,
    DIFY_PLANNER_KEY,
)
from app.schemas.requests import (
    ColumnConfirmRequest,
    HistoryQARequest,
    PlanConfirmRequest,
    QARequest,
)
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.survey_service import (
    columns_stream,
    compute_survey_stats,
    handle_survey_upload,
    history_qa_stream,
    is_survey_plan_approval,
    plan_revision_stream,
    plan_stream,
    prepare_history_qa_context,
    qa_stream,
    report_stream,
    set_survey_columns,
    validate_columns_ready,
    validate_plan_confirm_ready,
    validate_plan_ready,
    validate_qa_ready,
    validate_report_ready,
)

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
    validate_columns_ready(session_id)
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
    mode = validate_plan_ready(session_id)
    if mode == "crosstab" and not DIFY_CROSSTAB_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_CROSSTAB_PLANNER_KEY")
    elif mode != "crosstab" and not DIFY_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_PLANNER_KEY")
    return StreamingResponse(plan_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/plan/confirm")
async def confirm_plan(req: PlanConfirmRequest, request: Request):
    validate_plan_confirm_ready(req.session_id)
    if is_survey_plan_approval(req.user_text):
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
    use_large = validate_report_ready(session_id)
    if use_large and not DIFY_LARGE_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_LARGE_ANALYST_KEY")
    elif not use_large and not DIFY_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")
    return StreamingResponse(report_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/qa")
async def qa(req: QARequest, request: Request):
    validate_qa_ready(req.session_id)
    return StreamingResponse(qa_stream(req.session_id, req.question, request), media_type="text/event-stream")


@router.post("/api/history-qa")
async def history_qa(req: HistoryQARequest, request: Request):
    login = await _current_login(request)
    history, analyst_conv_id, analyst_key, analyst_key_name = prepare_history_qa_context(
        req.history_id, login
    )
    return StreamingResponse(
        history_qa_stream(req.history_id, req.question, history, analyst_conv_id, analyst_key, analyst_key_name, request),
        media_type="text/event-stream",
    )
