"""routers/export:报告导出接口（HTTP 壳）。

下载内容准备在 services/export_download + export_history；
飞书导出调用在 services/export_service。
"""
from fastapi import APIRouter, HTTPException, Request

from app.core.responses import _make_download_response
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.export_download import (
    get_session_export_data,
    prepare_markdown_download,
    prepare_pdf_download,
    prepare_word_download,
)
from app.services.export_history import (
    get_history_export_entry,
    prepare_pdf_history_download,
    prepare_word_history_download,
)
from app.services.export_service import _export_to_feishu, _feishu_export_error
from app.services.feishu_auth import require_feishu_configured

router = APIRouter()


@router.get("/api/export/word/{session_id}")
async def export_word(session_id: str, request: Request):
    docx_bytes, safe_title, title = await prepare_word_download(session_id)
    await audit_log(request, "report", "下载 Word", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe_title}.docx",
    )


@router.get("/api/export/markdown/{session_id}")
async def export_markdown(session_id: str, request: Request):
    md_bytes, safe_title, title = await prepare_markdown_download(session_id)
    await audit_log(request, "report", "下载 Markdown", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(md_bytes, "text/markdown; charset=utf-8", f"{safe_title}.md")


@router.get("/api/export/pdf/{session_id}")
async def export_pdf(session_id: str, request: Request):
    pdf_bytes, safe_title, title = await prepare_pdf_download(session_id)
    await audit_log(request, "report", "下载 PDF", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe_title}.pdf")


@router.get("/api/export/word-history/{history_id}")
async def export_word_history(history_id: str, request: Request):
    login = await _current_login(request)
    docx_bytes, safe_title, title = await prepare_word_history_download(history_id, login)
    await audit_log(request, "report", "下载历史 Word", f"报告：{title}", metadata={"history_id": history_id})
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe_title}.docx",
    )


@router.get("/api/export/pdf-history/{history_id}")
async def export_pdf_history(history_id: str, request: Request):
    login = await _current_login(request)
    pdf_bytes, safe_title, title = await prepare_pdf_history_download(history_id, login)
    await audit_log(request, "report", "下载历史 PDF", f"报告：{title}", metadata={"history_id": history_id})
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe_title}.pdf")


@router.post("/api/export/feishu/{session_id}")
async def export_feishu(session_id: str, request: Request):
    require_feishu_configured()
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    report_md, mode = get_session_export_data(session_id)
    try:
        url = await _export_to_feishu(report_md, login, mode=mode)
    except Exception as e:
        print(f"[feishu-export][ERROR] {e!r}")
        raise _feishu_export_error(e)
    await audit_log(request, "report", "导出飞书文档", f"会话：{session_id}", metadata={"session_id": session_id})
    return {"url": url, "type": "doc"}


@router.post("/api/export/feishu-history/{history_id}")
async def export_feishu_history(history_id: str, request: Request):
    require_feishu_configured()
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    entry = get_history_export_entry(history_id, login)
    entry_mode = entry.get("mode") or entry.get("plan", {}).get("mode", "")
    try:
        url = await _export_to_feishu(entry.get("report_md", ""), login, mode=entry_mode)
    except Exception as e:
        print(f"[feishu-export-history][ERROR] {e!r}")
        raise _feishu_export_error(e)
    await audit_log(request, "report", "导出历史飞书文档", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return {"url": url, "type": "doc"}
