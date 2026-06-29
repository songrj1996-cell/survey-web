"""services/annotate_workflow:数据标注的会话与编排辅助。

标注内存会话、AI 检测/质量打标的产物落盘与历史保存、结果 Excel 生成。
纯标注算法在 annotate 模块;AI 调用编排目前在 routers/annotate 的 SSE 流程内。
"""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import annotate
from fastapi import HTTPException, Request

from app.core.config import ANNOTATE_RESULT_DIR, DIFY_AI_DETECT_KEY
from app.core.security import _assign_session_owner
from app.services.auth import _current_login
from app.integrations.dify_client import sse_dify_stream
from app.services.report_history import save_annotate_to_history

# 标注会话用内存(生命周期短,不跨请求长期保活)
annotate_sessions: dict[str, dict] = {}


def _annotate_download_filename(filename: str) -> str:
    stem = re.sub(r"\.(csv|xlsx|xls)$", "", filename or "annotated", flags=re.IGNORECASE)
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem).strip() or "annotated"
    return f"{safe}_标注结果.xlsx"


def _annotate_result_path(sid: str) -> Path:
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
    return ANNOTATE_RESULT_DIR / f"{safe_sid}.xlsx"


def _annotate_incomplete_detail(sess: dict) -> str:
    missing_ai = sess.get("missing_ai_ids", []) or []
    missing_q = sess.get("missing_quality_ids", []) or []
    parts = []
    if missing_ai:
        ids_preview = ", ".join(missing_ai[:5]) + ("…" if len(missing_ai) > 5 else "")
        parts.append(f"AI 检测漏返 {len(missing_ai)} 行（ID：{ids_preview}）")
    if missing_q:
        ids_preview = ", ".join(missing_q[:5]) + ("…" if len(missing_q) > 5 else "")
        parts.append(f"质量打标漏返 {len(missing_q)} 行（ID：{ids_preview}）")
    return "；".join(parts)


def _build_annotate_excel_from_session(sess: dict) -> tuple[bytes, str]:
    rows = sess.get("rows")
    headers = sess.get("headers")
    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据")
    incomplete = _annotate_incomplete_detail(sess)
    if incomplete:
        raise HTTPException(status_code=400, detail=f"结果不完整，无法下载：{incomplete}。请返回重试对应任务。")
    filename = sess.get("filename", "annotated")
    excel_bytes = annotate.generate_annotated_excel(
        rows,
        headers,
        sess.get("ai_results", []),
        set(sess.get("confirmed_ai_ids", [])),
        sess.get("quality_results", []),
        sess.get("open_text_cols", []),
        sess.get("id_col", 1),
        sess.get("tasks", {}),
    )
    return excel_bytes, _annotate_download_filename(filename)


async def _save_annotate_result_history(sid: str, sess: dict, request: Request) -> None:
    if _annotate_incomplete_detail(sess):
        return
    login = await _current_login(request)
    _assign_session_owner(sess, login)
    loop = asyncio.get_event_loop()
    excel_bytes, download_name = await loop.run_in_executor(
        None,
        _build_annotate_excel_from_session,
        sess,
    )
    ANNOTATE_RESULT_DIR.mkdir(parents=True, exist_ok=True)
    result_path = _annotate_result_path(sid)
    result_path.write_bytes(excel_bytes)
    save_annotate_to_history(sid, sess, str(result_path), download_name)


def _annotate_ai_log(message: str, **fields) -> None:
    payload = " ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[annotate.ai_detect] {message}" + (f" {payload}" if payload else ""), flush=True)


def _get_annotate_session(sid: str) -> dict:
    sess = annotate_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="标注会话不存在或已过期，请重新上传文件")
    sess["ts"] = time.time()
    return sess


_ANNOTATE_SESSION_TTL = 7200  # 2 hours


def _clean_annotate_sessions() -> None:
    cutoff = time.time() - _ANNOTATE_SESSION_TTL
    expired = [k for k, v in annotate_sessions.items() if v.get("ts", 0) < cutoff]
    for k in expired:
        annotate_sessions.pop(k, None)


def _parse_string_array(text: str) -> list[str] | None:
    """从 LLM 输出中提取字符串数组，容忍值内部的裸双引号。"""
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        return None
    raw = m.group()
    # 先尝试直接解析
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(r) for r in result]
    except json.JSONDecodeError:
        pass
    # 兜底：逐行提取——取每行最外层引号之间的内容
    items = []
    for line in raw.splitlines():
        line = line.strip().rstrip(',')
        if line.startswith('"') and line.endswith('"') and len(line) >= 2:
            items.append(line[1:-1])
    return items if items else None


def _new_annotate_session() -> str:
    _clean_annotate_sessions()
    sid = str(uuid.uuid4())
    annotate_sessions[sid] = {"ts": time.time()}
    return sid


async def _translate_headers(headers: list) -> list:
    """将表头翻译为中文简体，失败时原样返回。"""
    if not DIFY_AI_DETECT_KEY:
        return headers
    query = (
        "将以下问卷列名按顺序翻译为中文简体，只输出 JSON 数组，不加其他任何内容：\n"
        + json.dumps(headers, ensure_ascii=False)
    )
    full_text = ""
    try:
        async for chunk, _ in sse_dify_stream(query, "hdr-translate", "", DIFY_AI_DETECT_KEY):
            full_text += chunk
        result = _parse_string_array(full_text)
        if result and len(result) == len(headers):
            return result
    except Exception:
        pass
    return headers
