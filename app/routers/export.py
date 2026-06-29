"""routers/export:报告导出(Word / Markdown / PDF / 飞书,会话 + 历史)。"""
import asyncio
import re

from fastapi import APIRouter, HTTPException, Request

from app.core.responses import _make_download_response
from app.core.security import _find_history_for_login
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.integrations import feishu_client as feishu_export
from app.services.export_service import _export_to_feishu, _feishu_export_error
from app.services.report_render import _prep_export_md, markdown_to_docx, report_markdown_to_pdf
from app.storage.history import _load_history
from app.storage.sessions import get_session

router = APIRouter()


@router.get("/api/export/word/{session_id}")
async def export_word(session_id: str, request: Request):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    report_md = _prep_export_md(report_md, mode=sess.get("mode") or "")
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)

    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    await audit_log(request, "report", "下载 Word", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe}.docx",
    )


@router.get("/api/export/markdown/{session_id}")
async def export_markdown(session_id: str, request: Request):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    report_md = _prep_export_md(report_md, mode=sess.get("mode") or "")
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    await audit_log(request, "report", "下载 Markdown", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(report_md.encode("utf-8"), "text/markdown; charset=utf-8", f"{safe}.md")


@router.get("/api/export/pdf/{session_id}")
async def export_pdf(session_id: str, request: Request):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)

    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(None, report_markdown_to_pdf, report_md, sess.get("mode") or "")
    await audit_log(request, "report", "下载 PDF", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe}.pdf")


@router.get("/api/export/word-history/{history_id}")
async def export_word_history(history_id: str, request: Request):
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    entry_mode = entry.get("mode") or entry.get("plan", {}).get("mode", "")
    report_md = _prep_export_md(entry.get("report_md", ""), mode=entry_mode)
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    await audit_log(request, "report", "下载历史 Word", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe}.docx",
    )


@router.get("/api/export/pdf-history/{history_id}")
async def export_pdf_history(history_id: str, request: Request):
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    report_md = entry.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="该历史记录没有报告内容")
    entry_mode = entry.get("mode") or entry.get("plan", {}).get("mode", "")
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(
        None, report_markdown_to_pdf, report_md, entry_mode
    )
    await audit_log(request, "report", "下载历史 PDF", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe}.pdf")


@router.post("/api/export/feishu/{session_id}")
async def export_feishu(session_id: str, request: Request):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用")
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")
    try:
        url = await _export_to_feishu(report_md, login, mode=sess.get("mode") or "")
    except Exception as e:
        print(f"[feishu-export][ERROR] {e!r}")
        raise _feishu_export_error(e)
    await audit_log(request, "report", "导出飞书文档", f"会话：{session_id}", metadata={"session_id": session_id})
    return {"url": url, "type": "doc"}


@router.post("/api/export/feishu-history/{history_id}")
async def export_feishu_history(history_id: str, request: Request):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用")
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    try:
        entry_mode = entry.get("mode") or entry.get("plan", {}).get("mode", "")
        url = await _export_to_feishu(
            entry.get("report_md", ""), login,
            mode=entry_mode,
        )
    except Exception as e:
        print(f"[feishu-export-history][ERROR] {e!r}")
        raise _feishu_export_error(e)
    await audit_log(request, "report", "导出历史飞书文档", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return {"url": url, "type": "doc"}
