"""routers/survey:问卷分析主流程接口（参数解析 + 权限检查 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在 services/survey_service。
跑数表(crosstab)模式复用本组的 plan/stats/report/qa 流程，仅上传入口在 routers/crosstab。
"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import (
    LLM_API_KEY,
    LLM_COLUMN_MODEL,
    LLM_PLANNER_MODEL,
)
from app.schemas.requests import (
    AnalysisPresetApplyRequest,
    ColumnConfirmRequest,
    HistoryQARequest,
    PlanConfirmRequest,
    PrepareReportRerunRequest,
    QARequest,
    QualitativeContextRequest,
    ReportVersionRequest,
)
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.session_access import require_session_request_access
from app.services.survey_service import (
    apply_analysis_preset_to_session,
    columns_stream,
    columns_require_llm,
    confirm_survey_plan,
    compute_survey_stats,
    delete_session_report_version,
    get_analysis_preset_offer_for_session,
    get_session_report_version,
    get_session_report_versions,
    handle_survey_upload,
    history_qa_stream,
    is_survey_plan_approval,
    plan_revision_stream,
    plan_stream,
    prepare_duplicate_report_rerun,
    prepare_history_qa_context,
    qa_stream,
    report_stream,
    save_qualitative_context,
    set_survey_columns,
    validate_columns_ready,
    validate_plan_confirm_ready,
    validate_plan_ready,
    validate_qa_ready,
    validate_report_ready,
)

router = APIRouter()


@router.post("/api/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    source_type: str = Form("google"),
    questionnaire_file: UploadFile | None = File(None),
):
    content = await file.read()
    questionnaire_content = (
        await questionnaire_file.read() if questionnaire_file is not None else None
    )
    login = await _current_login(request)
    result = await handle_survey_upload(
        file.filename or "upload.csv",
        content,
        login,
        source_type=source_type,
        questionnaire_filename=(
            questionnaire_file.filename if questionnaire_file is not None else None
        ),
        questionnaire_content=questionnaire_content,
    )
    await audit_log(
        request, "survey", "上传数据",
        f"文件：{result['filename']}；样本行数：{result['total_rows']}",
        metadata={
            "session_id": result["session_id"],
            "rows": result["total_rows"],
            "source_type": result["source_type"],
            "questionnaire_used": result["questionnaire_used"],
        },
    )
    return result


@router.get("/api/columns/{session_id}")
async def get_columns(session_id: str, request: Request):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    validate_columns_ready(session_id)
    if columns_require_llm(session_id) and (not LLM_API_KEY or not LLM_COLUMN_MODEL):
        raise HTTPException(status_code=500, detail="未配置题型识别 LLM 分发服务")
    return StreamingResponse(columns_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/columns/{session_id}/confirm")
async def confirm_columns(session_id: str, req: ColumnConfirmRequest, request: Request):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    set_survey_columns(session_id, req.columns)
    if login is None:
        login = await _current_login(request)
    preset_offer = get_analysis_preset_offer_for_session(session_id, login)
    await audit_log(
        request, "survey", "确认数据列",
        f"会话：{session_id}；确认列数：{len(req.columns)}",
        metadata={"session_id": session_id, "columns": len(req.columns)},
    )
    return {"ok": True, "analysis_preset_offer": preset_offer}


@router.post("/api/analysis-presets/{session_id}/apply")
async def apply_analysis_preset_route(
    session_id: str,
    req: AnalysisPresetApplyRequest,
    request: Request,
):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    if login is None:
        login = await _current_login(request)
    preset = apply_analysis_preset_to_session(session_id, login, req.preset_id)
    await audit_log(
        request,
        "survey",
        "复用分析思路",
        f"会话：{session_id}",
        metadata={"session_id": session_id, "preset_id": req.preset_id},
    )
    return {"ok": True, "preset": preset}


@router.post("/api/survey-context/{session_id}")
async def submit_survey_context(session_id: str, req: QualitativeContextRequest, request: Request):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    if login is None:
        login = await _current_login(request)
    duplicate_report = save_qualitative_context(session_id, req, login)
    await audit_log(
        request, "survey", "提交业务上下文",
        f"会话：{session_id}",
        metadata={"session_id": session_id},
    )
    return {"ok": True, "duplicate_report": duplicate_report}


@router.get("/api/plan/{session_id}")
async def get_plan(session_id: str, request: Request):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    validate_plan_ready(session_id)
    if not LLM_API_KEY or not LLM_PLANNER_MODEL:
        raise HTTPException(status_code=500, detail="未配置方案规划 LLM 分发服务")
    return StreamingResponse(plan_stream(session_id, request), media_type="text/event-stream")


@router.post("/api/plan/confirm")
async def confirm_plan(req: PlanConfirmRequest, request: Request):
    login = await require_session_request_access(
        request, req.session_id, login_resolver=_current_login,
    )
    validate_plan_confirm_ready(req.session_id)
    if is_survey_plan_approval(req.user_text):
        if login is None:
            login = await _current_login(request)
        result = confirm_survey_plan(req.session_id, login)
        await audit_log(
            request, "survey", "确认分析方案",
            f"会话：{req.session_id}", metadata={"session_id": req.session_id},
        )
        return JSONResponse(result)
    return StreamingResponse(
        plan_revision_stream(req.session_id, req.user_text, request),
        media_type="text/event-stream",
    )


@router.post("/api/stats/{session_id}")
async def compute_stats(session_id: str, request: Request):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    stats_md = await compute_survey_stats(session_id, request)
    return {"stats_md": stats_md}


@router.post("/api/report/{session_id}/prepare-rerun")
async def prepare_report_rerun(
    session_id: str,
    req: PrepareReportRerunRequest,
    request: Request,
):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    if login is None:
        login = await _current_login(request)
    result = prepare_duplicate_report_rerun(
        session_id,
        login,
        history_id=req.history_id,
        instruction=req.instruction,
        base_version=req.base_version,
    )
    await audit_log(
        request,
        "survey",
        "准备重新生成报告",
        f"会话：{session_id}；原报告：{req.history_id}",
        metadata={
            "session_id": session_id,
            "history_id": req.history_id,
            "base_version": result["base_version"],
            "target_version": result["target_version"],
        },
    )
    return result


@router.get("/api/report/{session_id}")
async def generate_report(
    session_id: str,
    request: Request,
    version: int | None = None,
):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    if version is not None:
        return get_session_report_version(session_id, version)
    validate_report_ready(session_id)
    return StreamingResponse(
        report_stream(session_id, request, generation_kind="initial"),
        media_type="text/event-stream",
    )


@router.get("/api/report/{session_id}/versions")
async def list_report_versions(session_id: str, request: Request):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    return get_session_report_versions(session_id)


@router.post("/api/report/{session_id}/versions")
async def generate_report_version(
    session_id: str,
    req: ReportVersionRequest,
    request: Request,
):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    raise HTTPException(
        status_code=405,
        detail="报告页不再支持直接生成新版本，请重新上传数据并从数据确认页发起。",
    )


@router.delete("/api/report/{session_id}/versions/{version}")
async def delete_report_version_route(session_id: str, version: int, request: Request):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    if login is None:
        login = await _current_login(request)
    return delete_session_report_version(session_id, version, login)


@router.post("/api/qa")
async def qa(req: QARequest, request: Request):
    await require_session_request_access(
        request, req.session_id, login_resolver=_current_login,
    )
    validate_qa_ready(req.session_id, req.version)
    return StreamingResponse(
        qa_stream(req.session_id, req.question, request, req.version),
        media_type="text/event-stream",
    )


@router.post("/api/history-qa")
async def history_qa(req: HistoryQARequest, request: Request):
    login = await _current_login(request)
    history = prepare_history_qa_context(req.history_id, login, req.version)
    return StreamingResponse(
        history_qa_stream(
            req.history_id,
            req.question,
            history,
            request,
            req.version,
        ),
        media_type="text/event-stream",
    )
