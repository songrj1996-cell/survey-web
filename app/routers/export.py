"""routers/export:报告导出接口（HTTP 壳）。

下载内容准备在 services/export_download + export_history；
飞书导出调用在 services/export_service。
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.responses import _make_download_response
from app.core.runtime_logging import runtime_log
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.export_download import (
    get_session_feishu_export_data,
    prepare_markdown_download,
    prepare_pdf_download,
    prepare_word_download,
)
from app.services.export_history import (
    get_history_feishu_export_data,
    prepare_markdown_history_download,
    prepare_pdf_history_download,
    prepare_word_history_download,
)
from app.services.export_service import _export_to_feishu, _feishu_export_error
from app.services.feishu_auth import require_feishu_configured
from app.services.session_access import require_session_request_access

from app.services.feature_access import legacy_route_access

router = APIRouter(dependencies=[Depends(legacy_route_access)])


@router.get("/api/export/word/{session_id}")
async def export_word(
    session_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    docx_bytes, safe_title, title = await prepare_word_download(session_id, version, scope=scope)
    await audit_log(
        request,
        "report",
        "下载 Word",
        f"报告：{title}",
        metadata={"session_id": session_id, "version": version},
    )
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe_title}.docx",
    )


@router.get("/api/export/markdown/{session_id}")
async def export_markdown(
    session_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    md_bytes, safe_title, title = await prepare_markdown_download(session_id, version, scope=scope)
    await audit_log(
        request,
        "report",
        "下载 Markdown",
        f"报告：{title}",
        metadata={"session_id": session_id, "version": version},
    )
    return _make_download_response(md_bytes, "text/markdown; charset=utf-8", f"{safe_title}.md")


@router.get("/api/export/pdf/{session_id}")
async def export_pdf(
    session_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    pdf_bytes, safe_title, title = await prepare_pdf_download(session_id, version, scope=scope)
    await audit_log(
        request,
        "report",
        "下载 PDF",
        f"报告：{title}",
        metadata={"session_id": session_id, "version": version},
    )
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe_title}.pdf")


@router.get("/api/export/word-history/{history_id}")
async def export_word_history(
    history_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    login = await _current_login(request)
    docx_bytes, safe_title, title = await prepare_word_history_download(
        history_id,
        login,
        version,
        scope=scope,
    )
    await audit_log(
        request,
        "report",
        "下载历史 Word",
        f"报告：{title}",
        metadata={"history_id": history_id, "version": version},
    )
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe_title}.docx",
    )


@router.get("/api/export/markdown-history/{history_id}")
async def export_markdown_history(
    history_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    login = await _current_login(request)
    md_bytes, safe_title, title = await prepare_markdown_history_download(
        history_id,
        login,
        version,
        scope=scope,
    )
    await audit_log(
        request,
        "report",
        "下载历史 Markdown",
        f"报告：{title}",
        metadata={"history_id": history_id, "version": version},
    )
    return _make_download_response(
        md_bytes,
        "text/markdown; charset=utf-8",
        f"{safe_title}.md",
    )


@router.get("/api/export/pdf-history/{history_id}")
async def export_pdf_history(
    history_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    login = await _current_login(request)
    pdf_bytes, safe_title, title = await prepare_pdf_history_download(
        history_id,
        login,
        version,
        scope=scope,
    )
    await audit_log(
        request,
        "report",
        "下载历史 PDF",
        f"报告：{title}",
        metadata={"history_id": history_id, "version": version},
    )
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe_title}.pdf")


@router.post("/api/export/feishu/{session_id}")
async def export_feishu(
    session_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    login = await require_session_request_access(
        request, session_id, login_resolver=_current_login,
    )
    if login is None:
        login = await _current_login(request)
    require_feishu_configured()
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    payload = get_session_feishu_export_data(session_id, version, scope=scope)
    try:
        url = await _export_to_feishu(login=login, **payload)
    except Exception as e:
        runtime_log("飞书导出", "error", "导出当前报告失败", error=e)
        raise _feishu_export_error(e)
    await audit_log(
        request,
        "report",
        "导出飞书文档",
        f"会话：{session_id}",
        metadata={"session_id": session_id, "version": version},
    )
    return {"url": url, "type": "doc"}


@router.post("/api/export/feishu-history/{history_id}")
async def export_feishu_history(
    history_id: str,
    request: Request,
    version: str | None = None,
    scope: Literal["body", "evidence"] = "body",
):
    require_feishu_configured()
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    payload = get_history_feishu_export_data(history_id, login, version, scope=scope)
    try:
        url = await _export_to_feishu(login=login, **payload)
    except Exception as e:
        runtime_log("飞书导出", "error", "导出历史报告失败", error=e)
        raise _feishu_export_error(e)
    await audit_log(
        request,
        "report",
        "导出历史飞书文档",
        f"报告：{payload.get('title') or history_id}",
        metadata={"history_id": history_id, "version": version},
    )
    return {"url": url, "type": "doc"}
