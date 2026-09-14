"""routers/survey:问卷分析主流程接口（参数解析 + 权限检查 + HTTP 响应）。

业务编排、SSE 流程、session 推进、历史落库全部在 services/survey_service。
跑数表(crosstab)模式复用本组的 plan/stats/report/qa 流程，仅上传入口在 routers/crosstab。
"""
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, Query
from fastapi.responses import JSONResponse, StreamingResponse
from app.core.llm_context import bind_llm_api_key

from app.core.config import (
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
    ReportSourceTranslationRequest,
    SurveyAnalysisSettingsRequest,
)
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.llm_credentials import (
    require_request_llm_api_key,
    stream_with_llm_api_key,
)
from app.services.session_access import require_session_request_access
from app.services.survey_service import (
    apply_analysis_preset_to_session,
    columns_stream,
    columns_require_llm,
    cancel_report_run,
    get_report_sources,
    translate_report_sources,
    prepare_quick_history_session,
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
    report_style_options,
    save_qualitative_context,
    set_survey_columns,
    set_survey_analysis_settings,
    validate_columns_ready,
    validate_plan_confirm_ready,
    validate_plan_ready,
    validate_qa_ready,
    validate_report_ready,
)

router = APIRouter()


@router.post("/api/analysis-settings/{session_id}")
async def update_analysis_settings(
    session_id: str, req: SurveyAnalysisSettingsRequest, request: Request,
):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    return set_survey_analysis_settings(session_id, req.report_focus, report_mode=req.report_mode)


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
    if columns_require_llm(session_id) and not LLM_COLUMN_MODEL:
        raise HTTPException(status_code=500, detail="未配置题型识别 LLM 分发服务")
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            columns_stream(session_id, request),
            api_key,
            request=request,
            category="survey",
            action="题型识别",
            reference_id=session_id,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/columns/{session_id}/confirm")
async def confirm_columns(session_id: str, req: ColumnConfirmRequest, request: Request):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    set_survey_columns(session_id, req.columns, req.selected_question_keys)
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
    if not LLM_PLANNER_MODEL:
        raise HTTPException(status_code=500, detail="未配置方案规划 LLM 分发服务")
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            plan_stream(session_id, request),
            api_key,
            request=request,
            category="survey",
            action="方案规划",
            reference_id=session_id,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/plan/confirm")
async def confirm_plan(req: PlanConfirmRequest, request: Request):
    login = await require_session_request_access(
        request, req.session_id, login_resolver=_current_login,
    )
    validate_plan_confirm_ready(req.session_id)
    if is_survey_plan_approval(req.user_text):
        if login is None:
            login = await _current_login(request)
        result = confirm_survey_plan(req.session_id, login, report_style=req.report_style, plan=req.plan)
        await audit_log(
            request, "survey", "确认分析方案",
            f"会话：{req.session_id}", metadata={"session_id": req.session_id},
        )
        return JSONResponse(result)
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            plan_revision_stream(req.session_id, req.user_text, request),
            api_key,
            request=request,
            category="survey",
            action="方案调整",
            reference_id=req.session_id,
        ),
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


@router.get("/api/report/{session_id}/options")
async def get_report_options(session_id: str, request: Request):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    return report_style_options(session_id)


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
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            report_stream(session_id, request, generation_kind="initial"),
            api_key,
            request=request,
            category="survey",
            action="报告生成",
            reference_id=session_id,
            history_id=session_id,
        ),
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
    return await _report_rerun_response(session_id, req, request, retry_failed=False)


async def _report_rerun_response(session_id, req, request, *, retry_failed):
    if req.history_id:
        login = await _current_login(request)
        session_id = prepare_quick_history_session(req.history_id, login, req.base_version)
    else:
        await require_session_request_access(request, session_id, login_resolver=_current_login)
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            report_stream(session_id, request, instruction=req.instruction,
                          base_version=req.base_version, generation_kind="regenerate", retry_failed=retry_failed),
            api_key, request=request, category="survey", action="失败题目重试" if retry_failed else "重新生成报告",
            reference_id=session_id, history_id=req.history_id or session_id,
        ), media_type="text/event-stream",
    )


@router.post("/api/report/{session_id}/retry-failed")
async def retry_failed_questions(session_id: str, req: ReportVersionRequest, request: Request):
    return await _report_rerun_response(session_id, req, request, retry_failed=True)


@router.post("/api/report/{session_id}/cancel")
async def cancel_report(session_id: str, request: Request):
    await require_session_request_access(request, session_id, login_resolver=_current_login)
    return cancel_report_run(session_id)


@router.get("/api/report/{session_id}/sources")
async def report_sources(session_id: str, request: Request, version: int | None = Query(None, ge=1),
                         history_id: str = "", question_key: str = "", offset: int = Query(0, ge=0),
                         limit: int = Query(50, ge=1, le=200), q: str = Query("", max_length=500)):
    login = await _current_login(request)
    return get_report_sources(session_id, version=version, history_id=history_id, login=login,
                              question_key=question_key, offset=offset, limit=limit, q=q)


@router.post("/api/report/{session_id}/sources/translate")
async def translate_report_sources_route(session_id: str, req: ReportSourceTranslationRequest, request: Request):
    login = await _current_login(request)
    api_key = await require_request_llm_api_key(request)
    with bind_llm_api_key(api_key):
        return await translate_report_sources(session_id, version=req.version,
            response_ids=req.response_ids, history_id=req.history_id or "", login=login)


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
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            qa_stream(req.session_id, req.question, request, req.version),
            api_key,
            request=request,
            category="survey",
            action="报告追问",
            reference_id=req.session_id,
            history_id=req.session_id,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/history-qa")
async def history_qa(req: HistoryQARequest, request: Request):
    login = await _current_login(request)
    history = prepare_history_qa_context(req.history_id, login, req.version)
    api_key = await require_request_llm_api_key(request)
    return StreamingResponse(
        stream_with_llm_api_key(
            history_qa_stream(
                req.history_id,
                req.question,
                history,
                request,
                req.version,
            ),
            api_key,
            request=request,
            category="survey",
            action="历史报告追问",
            reference_id=req.history_id,
            title=str(history.get("title") or ""),
            history_id=req.history_id,
        ),
        media_type="text/event-stream",
    )
