"""访谈报告接口：上传 Excel 并启动 Markdown 报告生成。"""
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import StreamingResponse

from app.services.audit import audit_log
from app.services.auth import _require_feature
from app.services.interview_service import (
    handle_interview_upload,
    get_interview_status,
    interview_report_stream,
    revise_interview_audit_issue_stream,
    validate_interview_session,
)
from app.services.report_history import confirm_interview_audit_issue

router = APIRouter()


@router.post("/api/interview/upload")
async def interview_upload(
    request: Request,
    file: UploadFile = File(...),
    research_focus: str = Form(""),
):
    content = await file.read()
    login = await _require_feature(request, "interview")
    result = await handle_interview_upload(
        file.filename or "interview.xlsx",
        content,
        login,
        research_focus,
    )
    await audit_log(
        request,
        "interview",
        "上传访谈记录",
        f"文件：{result['filename']}；Sheet 数：{len(result['sheets'])}",
        metadata={
            "session_id": result["session_id"],
            "sheets": len(result["sheets"]),
            "cells": result["total_cells"],
        },
    )
    return result


@router.get("/api/interview/run/{session_id}")
async def interview_run(session_id: str, request: Request):
    login = await _require_feature(request, "interview")
    validate_interview_session(session_id, login)
    return StreamingResponse(
        interview_report_stream(session_id, request, login),
        media_type="text/event-stream",
    )


@router.get("/api/interview/status/{session_id}")
async def interview_status(session_id: str, request: Request):
    login = await _require_feature(request, "interview")
    return get_interview_status(session_id, login)


@router.patch("/api/interview/review/{session_id}/issues/{issue_index}/confirm")
async def interview_confirm_review_issue(
    session_id: str,
    issue_index: int,
    request: Request,
):
    login = await _require_feature(request, "interview")
    result = confirm_interview_audit_issue(session_id, issue_index, login)
    await audit_log(
        request,
        "interview",
        "确认访谈报告审校提醒",
        f"报告：{result.get('report_no') or session_id}；提醒序号：{issue_index + 1}",
        metadata={"session_id": session_id, "issue_index": issue_index},
    )
    return result


@router.post("/api/interview/review/{session_id}/issues/{issue_index}/revise")
async def interview_revise_review_issue(
    session_id: str,
    issue_index: int,
    request: Request,
):
    login = await _require_feature(request, "interview")
    validate_interview_session(session_id, login)
    await audit_log(
        request,
        "interview",
        "按审校建议修订访谈报告",
        f"会话：{session_id}；提醒序号：{issue_index + 1}",
        metadata={"session_id": session_id, "issue_index": issue_index},
    )
    return StreamingResponse(
        revise_interview_audit_issue_stream(
            session_id,
            issue_index,
            request,
            login,
        ),
        media_type="text/event-stream",
    )
