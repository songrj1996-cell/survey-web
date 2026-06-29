"""services/export_history:历史报告导出查询与内容准备（Word / PDF / 飞书）。"""
import asyncio
import re

from fastapi import HTTPException

from app.core.security import _find_history_for_login
from app.services.report_render import _prep_export_md, markdown_to_docx, report_markdown_to_pdf
from app.storage.history import _load_history


def get_history_export_entry(history_id: str, login: dict | None) -> dict:
    """加载并校验历史记录，找不到则 raise 404。"""
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return entry


async def prepare_word_history_download(history_id: str, login: dict | None) -> tuple[bytes, str, str]:
    """返回 (docx_bytes, safe_title, title)。"""
    entry = get_history_export_entry(history_id, login)
    entry_mode = entry.get("mode") or entry.get("plan", {}).get("mode", "")
    report_md = _prep_export_md(entry.get("report_md", ""), mode=entry_mode)
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    return docx_bytes, safe, entry.get("title", history_id)


async def prepare_pdf_history_download(history_id: str, login: dict | None) -> tuple[bytes, str, str]:
    """返回 (pdf_bytes, safe_title, title)。"""
    entry = get_history_export_entry(history_id, login)
    report_md = entry.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="该历史记录没有报告内容")
    entry_mode = entry.get("mode") or entry.get("plan", {}).get("mode", "")
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(None, report_markdown_to_pdf, report_md, entry_mode)
    return pdf_bytes, safe, entry.get("title", history_id)
