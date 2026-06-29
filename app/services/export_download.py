"""services/export_download:会话报告下载内容准备（Word / Markdown / PDF）。"""
import asyncio
import re

from fastapi import HTTPException

from app.services.report_render import _prep_export_md, markdown_to_docx, report_markdown_to_pdf
from app.storage.sessions import get_session


def _extract_title(report_md: str) -> tuple[str, str]:
    """从 Markdown 提取标题，返回 (title, safe_title)。"""
    m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = m.group(1).strip() if m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    return title, safe


async def prepare_word_download(session_id: str) -> tuple[bytes, str, str]:
    """返回 (docx_bytes, safe_title, title)。"""
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")
    report_md = _prep_export_md(report_md, mode=sess.get("mode") or "")
    title, safe = _extract_title(report_md)
    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    return docx_bytes, safe, title


async def prepare_markdown_download(session_id: str) -> tuple[bytes, str, str]:
    """返回 (md_bytes, safe_title, title)。"""
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")
    report_md = _prep_export_md(report_md, mode=sess.get("mode") or "")
    title, safe = _extract_title(report_md)
    return report_md.encode("utf-8"), safe, title


async def prepare_pdf_download(session_id: str) -> tuple[bytes, str, str]:
    """返回 (pdf_bytes, safe_title, title)。"""
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")
    title, safe = _extract_title(report_md)
    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(
        None, report_markdown_to_pdf, report_md, sess.get("mode") or ""
    )
    return pdf_bytes, safe, title


def get_session_export_data(session_id: str) -> tuple[str, str]:
    """返回 (report_md, mode)，供飞书导出使用。"""
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")
    return report_md, sess.get("mode") or ""
