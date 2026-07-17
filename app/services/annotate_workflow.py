"""services/annotate_workflow:数据标注的全部业务编排。

包含:内存会话管理、上传处理、列确认、AI 检测 SSE 流程、质量打标 SSE 流程、
结果 Excel 生成与落盘、历史保存、历史文件下载。
纯标注算法在 annotate 模块;HTTP 参数解析与响应包装在 routers/annotate。
"""
import asyncio
import json
import re
import time
import uuid
from pathlib import Path

import annotate
from fastapi import HTTPException, Request

from app.core.config import (
    ANNOTATE_AI_BATCH_SIZE,
    ANNOTATE_AI_CONCURRENCY,
    ANNOTATE_AI_HIGH_THRESHOLD,
    ANNOTATE_AI_MAX_QUERY_CHARS,
    ANNOTATE_AI_REVIEW_THRESHOLD,
    ANNOTATE_QUALITY_BATCH_SIZE,
    ANNOTATE_QUALITY_CONCURRENCY,
    ANNOTATE_QUALITY_MAX_QUERY_CHARS,
    ANNOTATE_RESULT_DIR,
    DIFY_AI_DETECT_KEY,
    DIFY_QUALITY_KEY,
)
from app.core.parsing import _parse_file
from app.core.responses import sse_event
from app.core.security import _assign_session_owner, _find_history_for_login
from app.integrations.dify_client import workflow_run
from app.services.audit import audit_log
from app.services.auth import _current_login
from app.services.question_detect import _detect_open_text_cols, _group_googleform_matrix
from app.services.report_history import save_annotate_to_history
from app.storage.history import _ensure_history_report_numbers, _load_history

# 标注会话用内存(生命周期短,不跨请求长期保活)
annotate_sessions: dict[str, dict] = {}
_ANNOTATE_SSE_HEARTBEAT_SECONDS = 15


# ── 会话辅助 ────────────────────────────────────────────────────


def _annotate_download_filename(filename: str) -> str:
    stem = re.sub(r"\.(csv|xlsx)$", "", filename or "annotated", flags=re.IGNORECASE)
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem).strip() or "annotated"
    return f"{safe}_标注结果.xlsx"


def _annotate_result_path(sid: str) -> Path:
    safe_sid = re.sub(r"[^A-Za-z0-9_-]", "_", sid)
    return ANNOTATE_RESULT_DIR / f"{safe_sid}.xlsx"


def _annotate_incomplete_detail(sess: dict) -> str:
    rows = (sess.get("rows") or [])[1:]
    id_col = sess.get("id_col", 1)
    tasks = sess.get("tasks") or {}
    expected_ids = {_row_id(row, id_col) for row in rows if _row_id(row, id_col)}
    confirmed_ai_ids = set(sess.get("confirmed_ai_ids") or [])
    ai_result_ids = {
        str(result.get("id", "")).strip()
        for result in sess.get("ai_results", [])
        if str(result.get("id", "")).strip()
    }
    quality_result_ids = {
        str(result.get("id", "")).strip()
        for result in sess.get("quality_results", [])
        if str(result.get("id", "")).strip()
    }
    missing_ai = set(sess.get("missing_ai_ids", []) or [])
    missing_q = set(sess.get("missing_quality_ids", []) or [])
    missing_translations = sess.get("missing_translation_ids", []) or []
    parts = []
    if tasks.get("ai_detect"):
        missing_ai.update(expected_ids - ai_result_ids)
        if sess.get("ai_status") != "complete":
            parts.append("AI 检测尚未完成")
        if sess.get("ai_status") == "complete" and not sess.get("ai_confirmation_complete"):
            parts.append("AI 作答结果尚未人工确认")
    if tasks.get("quality"):
        missing_q.update((expected_ids - confirmed_ai_ids) - quality_result_ids)
        if sess.get("quality_status") != "complete":
            parts.append("质量打标尚未完成")
    if missing_ai:
        ordered = sorted(missing_ai)
        ids_preview = ", ".join(ordered[:5]) + ("…" if len(ordered) > 5 else "")
        parts.append(f"AI 检测漏返 {len(missing_ai)} 行（ID：{ids_preview}）")
    if missing_q:
        ordered = sorted(missing_q)
        ids_preview = ", ".join(ordered[:5]) + ("…" if len(ordered) > 5 else "")
        parts.append(f"质量打标漏返 {len(missing_q)} 行（ID：{ids_preview}）")
    if missing_translations:
        ids_preview = ", ".join(missing_translations[:5]) + (
            "…" if len(missing_translations) > 5 else ""
        )
        parts.append(f"中文翻译缺失 {len(missing_translations)} 行（ID：{ids_preview}）")
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


def get_annotate_session(sid: str) -> dict:
    sess = annotate_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="标注会话不存在或已过期，请重新上传文件")
    sess["ts"] = time.time()
    return sess


def validate_annotate_session_for_ai(sid: str) -> None:
    """校验 session 是否具备运行 AI 检测的前置条件，不满足则 raise HTTPException。"""
    sess = get_annotate_session(sid)
    if not sess.get("rows") or not sess.get("open_text_cols"):
        raise HTTPException(status_code=400, detail="会话状态不完整，请重新上传")
    if not (sess.get("tasks") or {}).get("ai_detect"):
        raise HTTPException(status_code=400, detail="当前任务未启用 AI 作答识别")
    if sess.get("ai_status") == "running":
        raise HTTPException(status_code=409, detail="AI 作答识别正在运行，请勿重复启动")
    if (
        sess.get("ai_status") == "complete"
        and not sess.get("missing_translation_ids")
        and not sess.get("missing_ai_ids")
    ):
        raise HTTPException(status_code=409, detail="AI 作答识别已经完成，无需重复运行")
    sess["ai_status"] = "running"


def validate_annotate_session_for_quality(sid: str) -> None:
    """校验 session 是否具备运行质量打标的前置条件，不满足则 raise HTTPException。"""
    sess = get_annotate_session(sid)
    if not sess.get("rows") or not sess.get("open_text_cols"):
        raise HTTPException(status_code=400, detail="会话状态不完整，请重新上传")
    tasks = sess.get("tasks") or {}
    if not tasks.get("quality"):
        raise HTTPException(status_code=400, detail="当前任务未启用质量打标")
    if tasks.get("ai_detect"):
        if sess.get("ai_status") != "complete":
            raise HTTPException(status_code=400, detail="请先完成 AI 作答识别")
        if not sess.get("ai_confirmation_complete"):
            raise HTTPException(status_code=400, detail="请先确认 AI 作答结果")
    if sess.get("quality_status") == "running":
        raise HTTPException(status_code=409, detail="质量打标正在运行，请勿重复启动")
    if (
        sess.get("quality_status") == "complete"
        and not sess.get("missing_translation_ids")
        and not sess.get("missing_quality_ids")
    ):
        raise HTTPException(status_code=409, detail="质量打标已经完成，无需重复运行")
    sess["quality_status"] = "running"


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
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(r) for r in result]
    except json.JSONDecodeError:
        pass
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


_UPLOAD_HEADER_RE = re.compile(
    r"(?:\bupload\b|\battach(?:ment)?\b|\bscreenshot\b|上传|截图|附件|图片|照片)",
    re.IGNORECASE,
)
_FILE_VALUE_RE = re.compile(
    r"^(?:https?://|www\.|data:image/)|\.(?:png|jpe?g|gif|webp|bmp|pdf)(?:\?.*)?$",
    re.IGNORECASE,
)


def _empty_annotate_column_indexes(rows: list[list], headers: list) -> list[int]:
    """返回表头和全部数据行均为空的列，供标注确认页隐藏。"""
    return [
        index
        for index in range(len(headers))
        if all(not (str(row[index]) if index < len(row) else "").strip() for row in rows)
    ]


def _filter_annotate_open_text_cols(
    rows: list[list],
    headers: list,
    candidates: list[int],
) -> list[int]:
    """排除空表头、全空列和文件上传列，避免它们被默认当作主观题。"""
    body = rows[1:]
    filtered: list[int] = []
    for index in candidates:
        header = str(headers[index]).strip() if index < len(headers) else ""
        values = [
            (str(row[index]) if index < len(row) else "").strip()
            for row in body
        ]
        non_empty = [value for value in values if value]
        if not header or not non_empty or _UPLOAD_HEADER_RE.search(header):
            continue
        file_like = sum(bool(_FILE_VALUE_RE.search(value)) for value in non_empty)
        if file_like / len(non_empty) >= 0.8:
            continue
        filtered.append(index)
    return filtered


async def _translate_headers(headers: list) -> tuple[list, str]:
    """将表头翻译为中文简体；只补发缺失项，最终失败时返回明确警告。"""
    if not DIFY_AI_DETECT_KEY:
        return list(headers), "未配置 AI 识别应用，列名未翻译"
    translated = list(headers)
    pending: dict[str, dict] = {}
    for index, header in enumerate(headers):
        original = str(header).strip()
        if (
            original
            and not _is_likely_chinese(original)
            and re.search(r"[A-Za-z\u3040-\u30ff\uac00-\ud7af]", original)
        ):
            key = f"col_{index}"
            pending[key] = {"id": "__headers__", "key": key, "text": original}
    if not pending:
        return translated, ""

    for attempt in range(1, 3):
        repair_items = list(pending.values())
        try:
            output = await workflow_run(
                inputs={
                    "mode": "translation_repair",
                    "query": annotate.build_translation_repair_query(repair_items),
                },
                api_key=DIFY_AI_DETECT_KEY,
                user=f"hdr-translate-{attempt}",
                max_retries=3,
                log_prefix=f"annotate.header.translate.{attempt}",
            )
            repaired, _ = annotate.parse_translation_repair_result(output)
        except Exception as exc:
            _annotate_ai_log("header translation failed", attempt=attempt, error=str(exc)[:500])
            repaired = []
        for item in repaired:
            key = str(item.get("key", ""))
            source = pending.get(key)
            translation = str(item.get("translation", "")).strip()
            if (
                item.get("id") == "__headers__"
                and source
                and _translation_is_usable(source["text"], translation)
            ):
                translated[int(key.removeprefix("col_"))] = translation
                pending.pop(key, None)
        if not pending:
            break

    warning = f"{len(pending)} 个列名翻译失败，已保留原文" if pending else ""
    return translated, warning


# ── 上传 ────────────────────────────────────────────────────────


async def handle_annotate_upload(filename: str, content: bytes, login: dict | None) -> dict:
    """解析上传文件、检测列、翻译表头、创建会话，返回前端所需的 result dict。"""
    try:
        rows = _parse_file(filename, content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not rows or len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件为空或只有表头")

    headers = rows[0]
    body = rows[1:]

    id_col = annotate.detect_id_column(headers, rows)
    detected_open_text_cols = _detect_open_text_cols(rows, headers)
    open_text_cols = _filter_annotate_open_text_cols(
        rows, headers, detected_open_text_cols,
    )
    empty_col_idxs = _empty_annotate_column_indexes(rows, headers)

    matrix_col_idxs: list[int] = []
    for g in _group_googleform_matrix(headers):
        if g["type"] == "matrix":
            matrix_col_idxs.extend(g["member_indexes"])

    headers_zh, header_translation_warning = await _translate_headers(headers)

    sid = _new_annotate_session()
    sess = {
        "rows": rows,
        "headers": headers,
        "headers_zh": headers_zh,
        "filename": filename,
        "id_col": id_col,
        "open_text_cols": open_text_cols,
    }
    _assign_session_owner(sess, login)
    annotate_sessions[sid].update(sess)

    return {
        "session_id": sid,
        "filename": filename,
        "total_rows": len(body),
        "headers": headers,
        "headers_zh": headers_zh,
        "id_col": id_col,
        "open_text_cols": open_text_cols,
        "matrix_col_idxs": matrix_col_idxs,
        "empty_col_idxs": empty_col_idxs,
        "header_translation_warning": header_translation_warning,
        "preview": rows[1: min(4, len(rows))],
    }


# ── 列确认 ──────────────────────────────────────────────────────


def annotate_set_column_config(
    sid: str,
    id_col: int,
    open_text_cols: list[int],
    tasks: dict,
    background: str,
) -> list[str]:
    """更新会话的列配置，返回任务名称列表（用于审计）。"""
    sess = get_annotate_session(sid)
    headers = sess.get("headers") or []
    body = (sess.get("rows") or [])[1:]
    if not isinstance(id_col, int) or not 0 <= id_col < len(headers):
        raise HTTPException(status_code=400, detail="玩家 ID 列无效，请重新选择")
    normalized_open_cols = list(dict.fromkeys(open_text_cols))
    if not normalized_open_cols or any(
        not isinstance(col, int) or not 0 <= col < len(headers) or col == id_col
        for col in normalized_open_cols
    ):
        raise HTTPException(status_code=400, detail="主观题列无效，请重新选择")
    if not any(bool(tasks.get(key)) for key in ("ai_detect", "quality")):
        raise HTTPException(status_code=400, detail="请至少选择一项标注任务")

    ids = [_row_id(row, id_col) for row in body]
    empty_rows = [index + 2 for index, row_id in enumerate(ids) if not row_id]
    if empty_rows:
        preview = "、".join(str(index) for index in empty_rows[:8])
        suffix = "等" if len(empty_rows) > 8 else ""
        raise HTTPException(status_code=400, detail=f"玩家 ID 不能为空：Excel 第 {preview}{suffix} 行缺少 ID")
    seen: set[str] = set()
    duplicate_ids: list[str] = []
    for row_id in ids:
        if row_id in seen and row_id not in duplicate_ids:
            duplicate_ids.append(row_id)
        seen.add(row_id)
    if duplicate_ids:
        preview = "、".join(duplicate_ids[:8])
        suffix = "等" if len(duplicate_ids) > 8 else ""
        raise HTTPException(status_code=400, detail=f"玩家 ID 必须唯一，以下 ID 重复：{preview}{suffix}")

    sess["id_col"] = id_col
    sess["open_text_cols"] = normalized_open_cols
    sess["tasks"] = {
        "ai_detect": bool(tasks.get("ai_detect")),
        "quality": bool(tasks.get("quality")),
    }
    sess["background"] = background
    sess["ai_results"] = []
    sess["confirmed_ai_ids"] = []
    sess["quality_results"] = []
    sess["ai_status"] = "pending" if tasks.get("ai_detect") else "skipped"
    sess["ai_confirmation_complete"] = not bool(tasks.get("ai_detect"))
    sess["quality_status"] = "pending" if tasks.get("quality") else "skipped"
    sess.pop("missing_ai_ids", None)
    sess.pop("missing_quality_ids", None)
    sess.pop("missing_translation_ids", None)
    task_names = []
    if tasks.get("ai_detect"):
        task_names.append("AI 作答识别")
    if tasks.get("quality"):
        task_names.append("回答质量打标")
    return task_names


# ── AI 检测 SSE ─────────────────────────────────────────────────


_EMPTY_ANSWER_RE = re.compile(
    r"^(?:n/?a|none|null|nil|no\.?|nothing|no\s+(?:feedback|comment)|"
    r"not\s+applicable|tidak|无|没有|暂无|无意见|没了|なし|없음)[.!。！]?$",
    re.IGNORECASE,
)


def _is_effectively_empty_answer(value: object) -> bool:
    """Treat explicit no-answer placeholders as empty while preserving source text."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return not text or bool(_EMPTY_ANSWER_RE.fullmatch(text))


def _has_open_text(row: list, open_text_cols: list[int]) -> bool:
    return any(
        not _is_effectively_empty_answer(row[c] if c < len(row) else "")
        for c in open_text_cols
    )


def _row_id(row: list, id_col: int) -> str:
    return str(row[id_col]).strip() if id_col < len(row) else ""


def _chunks(items: list, size: int) -> list[list]:
    return [items[i: i + size] for i in range(0, len(items), size)]


def _chunk_rows_by_query_budget(
    rows: list[list],
    max_rows: int,
    max_chars: int,
    build_query,
) -> list[list[list]]:
    """Keep normal batch limits while splitting early when source text is large."""
    batches: list[list[list]] = []
    current: list[list] = []
    for row in rows:
        candidate = current + [row]
        if current and (
            len(candidate) > max_rows
            or len(build_query(candidate)) > max_chars
        ):
            batches.append(current)
            current = [row]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def _fit_rows_to_query_budget(
    rows: list[list],
    text_cols: list[int],
    max_chars: int,
    build_query,
) -> tuple[list[list], str]:
    """Trim only the model copy of oversized cells; originals stay untouched in session."""
    query = build_query(rows)
    if len(query) <= max_chars or not rows or not text_cols:
        return rows, query

    cell_count = max(1, len(rows) * len(text_cols))
    cap = max(0, (max_chars - 500) // cell_count)
    model_rows = [list(row) for row in rows]
    while True:
        for model_row, source_row in zip(model_rows, rows):
            for col in text_cols:
                if col >= len(model_row) or col >= len(source_row):
                    continue
                text = str(source_row[col])
                model_row[col] = text if len(text) <= cap else text[:cap]
        query = build_query(model_rows)
        if len(query) <= max_chars:
            return model_rows, query
        if cap == 0:
            raise ValueError("查询固定内容超过字符预算，请缩短列名或调研背景")
        cap = max(0, int(cap * 0.75) - 1)


def _effective_batch_size(configured_size: int, open_text_cols: list[int], cell_budget: int) -> int:
    """按每批主观题单元格数量限制输出规模，配置值仍作为上限。"""
    question_count = max(1, len(open_text_cols))
    return max(1, min(configured_size, max(3, cell_budget // question_count)))


def _contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u3400-\u9fff]", str(text or "")))


def _is_likely_chinese(text: str) -> bool:
    value = str(text or "")
    return _contains_cjk(value) and not re.search(r"[\u3040-\u30ff]", value)


def _translation_is_usable(original: str, translation: str) -> bool:
    original = str(original or "").strip()
    translation = str(translation or "").strip()
    if _is_effectively_empty_answer(original):
        return True
    if not translation:
        return False
    if _is_likely_chinese(original) or _contains_cjk(translation):
        return True
    if not re.search(r"[A-Za-z\u3040-\u30ff\uac00-\ud7af]", original):
        return True
    if translation != original:
        return False
    if re.fullmatch(r"(?:https?://|www\.)\S+", original, re.IGNORECASE):
        return True
    if original.upper() in {"N/A", "NA", "NONE", "NULL"}:
        return True
    if re.fullmatch(r"[A-Z0-9][A-Z0-9._+/#-]{1,19}", original):
        return True
    return bool(
        len(original) <= 40
        and re.fullmatch(r"(?:[A-Z][A-Za-z0-9'._-]*)(?: [A-Z][A-Za-z0-9'._-]*){0,3}", original)
    )


def _public_dify_error(error: str) -> str:
    lowered = str(error or "").lower()
    if any(token in lowered for token in (
        "modelunavailable", "model unavailable", "apiconnectionerror",
        "temporarily unavailable", "bedrockexception",
    )):
        return "模型服务暂时不可用，自动重试后仍未恢复"
    if "timeout" in lowered or "timed out" in lowered:
        return "模型服务响应超时"
    if error:
        return "模型返回内容无法完成校验"
    return ""


def _validation_error_summary(errors: list[str]) -> str:
    categories: list[str] = []
    joined = " ".join(str(error) for error in errors)
    for token, label in (
        ("中文翻译", "中文翻译缺失"),
        ("非空回答不能为 N/A", "非空回答被错误标为 N/A"),
        ("标签非法", "标签格式错误"),
        ("缺少原因", "判断原因缺失"),
        ("原文证据", "AI 原文证据无效"),
        ("Dify", "模型调用失败"),
        ("模型服务", "模型服务暂时不可用"),
    ):
        if token in joined and label not in categories:
            categories.append(label)
    return "、".join(categories[:3]) or ("模型结果仍不完整" if errors else "")


def _open_text_originals(row: list, open_text_cols: list[int]) -> dict[str, str]:
    return {
        f"col_{c}": str(row[c]).strip()
        for c in open_text_cols
        if c < len(row) and str(row[c]).strip()
    }


def _empty_ai_result(row: list, id_col: int, open_text_cols: list[int], reason: str) -> dict:
    return {
        "id": _row_id(row, id_col),
        "ai_prob": 0,
        "polish_prob": 0,
        "reason": reason,
        "evidence": "",
        "counter_evidence": "",
        "originals": _open_text_originals(row, open_text_cols),
        "translations": {},
    }


def _attach_originals(
    results: list[dict],
    batch_rows: list[list],
    id_col: int,
    open_text_cols: list[int],
) -> list[dict]:
    rows_by_id = {_row_id(row, id_col): row for row in batch_rows if _row_id(row, id_col)}
    for result in results:
        row = rows_by_id.get(str(result.get("id", "")).strip())
        if row is not None:
            result["originals"] = _open_text_originals(row, open_text_cols)
    return results


def _canonical_text_with_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize prompt-only escaping/whitespace and retain source spans."""
    source = str(text or "")
    chars: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(source):
        if source[index] == "\\" and index + 1 < len(source) and source[index + 1] == "|":
            chars.append("|")
            spans.append((index, index + 2))
            index += 2
            continue
        if source[index].isspace():
            start = index
            while index < len(source) and source[index].isspace():
                index += 1
            if chars and chars[-1] != " ":
                chars.append(" ")
                spans.append((start, index))
            continue
        chars.append(source[index])
        spans.append((index, index + 1))
        index += 1
    while chars and chars[-1] == " ":
        chars.pop()
        spans.pop()
    return "".join(chars), spans


def _exact_original_evidence(evidence: str, originals: dict[str, str]) -> str | None:
    """Return the exact source slice represented by evidence, or None when rewritten."""
    evidence = str(evidence or "").strip()
    if not evidence:
        return ""
    for original in originals.values():
        if evidence in original:
            return evidence
        normalized_original, spans = _canonical_text_with_spans(original)
        normalized_evidence, _ = _canonical_text_with_spans(evidence)
        start = normalized_original.find(normalized_evidence)
        if start >= 0 and normalized_evidence:
            end = start + len(normalized_evidence)
            return original[spans[start][0]:spans[end - 1][1]].strip()
    return None


def _evidence_in_originals(evidence: str, originals: dict[str, str]) -> bool:
    return _exact_original_evidence(evidence, originals) is not None


def _validated_ai_results(
    results: list[dict],
    batch_rows: list[list],
    id_col: int,
    open_text_cols: list[int],
) -> tuple[list[dict], set[str], list[str]]:
    rows_by_id = {_row_id(row, id_col): row for row in batch_rows}
    valid: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    for result in _attach_originals(results, batch_rows, id_col, open_text_cols):
        row_id = str(result.get("id", "")).strip()
        if row_id not in rows_by_id or row_id in seen:
            errors.append(f"ID {row_id or '(空)'} 无法唯一匹配输入")
            continue
        originals = result.get("originals") or {}
        review_risk = result.get("ai_prob", 0) >= ANNOTATE_AI_REVIEW_THRESHOLD
        if review_risk and not str(
            result.get("evidence", "")
        ).strip():
            errors.append(f"ID {row_id} 的中高风险 AI 判断缺少原文证据")
            continue
        exact_evidence = _exact_original_evidence(result.get("evidence", ""), originals)
        if exact_evidence is None:
            if review_risk:
                errors.append(f"ID {row_id} 的 AI 原文证据不是连续原文")
                continue
            result["evidence"] = ""
        else:
            result["evidence"] = exact_evidence
        exact_counter = _exact_original_evidence(
            result.get("counter_evidence", ""), originals,
        )
        if exact_counter is None:
            result["counter_evidence"] = ""
        else:
            result["counter_evidence"] = exact_counter
        seen.add(row_id)
        valid.append(result)
    missing = set(rows_by_id) - seen
    return valid, missing, errors


def _empty_quality_result(row: list, id_col: int, open_text_cols: list[int]) -> dict:
    q_labels = {f"col_{col}": "N/A" for col in open_text_cols}
    q_reasons = {f"col_{col}": "回答为空，按 N/A 处理" for col in open_text_cols}
    q_evidence = {f"col_{col}": "" for col in open_text_cols}
    overall, overall_reason = annotate.calculate_overall_quality(q_labels, open_text_cols)
    return {
        "id": _row_id(row, id_col),
        "q_labels": q_labels,
        "q_reasons": q_reasons,
        "q_evidence": q_evidence,
        "translations": {},
        "originals": {},
        "overall": overall,
        "overall_reason": overall_reason,
    }


def _validated_quality_results(
    results: list[dict],
    batch_rows: list[list],
    id_col: int,
    open_text_cols: list[int],
    include_translations: bool,
) -> tuple[list[dict], set[str], list[str]]:
    rows_by_id = {_row_id(row, id_col): row for row in batch_rows}
    valid: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    expected_keys = {f"col_{col}" for col in open_text_cols}
    for result in results:
        row_id = str(result.get("id", "")).strip()
        row = rows_by_id.get(row_id)
        if row is None or row_id in seen:
            errors.append(f"ID {row_id or '(空)'} 无法唯一匹配输入")
            continue
        labels = result.get("q_labels") or {}
        reasons = result.get("q_reasons") or {}
        evidence_map = result.get("q_evidence") or {}
        translations = result.get("translations") or {}
        result["q_evidence"] = evidence_map
        result["translations"] = translations
        row_errors: list[str] = []
        for col in open_text_cols:
            key = f"col_{col}"
            original = str(row[col]).strip() if col < len(row) else ""
            label = str(labels.get(key, ""))
            reason = str(reasons.get(key, "")).strip()
            evidence = str(evidence_map.get(key, "")).strip()
            if _is_effectively_empty_answer(original):
                labels[key] = "N/A"
                reasons[key] = "回答为无内容占位表达，按 N/A 处理"
                evidence_map[key] = ""
                continue
            if label not in annotate.QUALITY_LABELS:
                row_errors.append(f"{key} 标签非法")
            elif not original and label != "N/A":
                row_errors.append(f"{key} 空回答必须为 N/A")
            elif original and label == "N/A":
                row_errors.append(f"{key} 非空回答不能为 N/A")
            if not reason:
                row_errors.append(f"{key} 缺少原因")
            if label != "N/A" and (not evidence or evidence not in original):
                evidence_map[key] = original
            elif label == "N/A":
                evidence_map[key] = ""
        if not expected_keys.issubset(labels):
            row_errors.append("逐题标签列不完整")
        if row_errors:
            errors.append(f"ID {row_id}：{'；'.join(row_errors[:4])}")
            continue
        result["originals"] = _open_text_originals(row, open_text_cols)
        result["overall"], result["overall_reason"] = annotate.calculate_overall_quality(
            labels, open_text_cols,
        )
        seen.add(row_id)
        valid.append(result)
    return valid, set(rows_by_id) - seen, errors


def _quality_invalid_cols(
    result: dict | None,
    row: list,
    open_text_cols: list[int],
) -> set[int]:
    """返回需要模型重新判断的题目列；证据和翻译由独立流程修复。"""
    if result is None:
        return set(open_text_cols)
    labels = result.get("q_labels") or {}
    reasons = result.get("q_reasons") or {}
    invalid: set[int] = set()
    for col in open_text_cols:
        key = f"col_{col}"
        original = str(row[col]).strip() if col < len(row) else ""
        if _is_effectively_empty_answer(original):
            continue
        label = str(labels.get(key, ""))
        reason = str(reasons.get(key, "")).strip()
        if (
            label not in annotate.QUALITY_LABELS
            or (not original and label != "N/A")
            or (original and label == "N/A")
            or not reason
        ):
            invalid.add(col)
    return invalid


async def _run_ai_direct_batch(
    sid: str,
    batch_rows: list[list],
    headers: list,
    open_text_cols: list[int],
    id_col: int,
    background: str,
    api_key: str,
    label: str,
) -> tuple[list[dict], str]:
    """单个子批次的 Dify 调用 + 解析 + 一次重试。"""
    try:
        _, query = _fit_rows_to_query_budget(
            batch_rows,
            open_text_cols,
            ANNOTATE_AI_MAX_QUERY_CHARS - 500,
            lambda model_rows: annotate.build_ai_detect_query(
                model_rows, headers, open_text_cols, id_col, label, background,
            ),
        )
        _annotate_ai_log(
            "subbatch start", sid=sid, batch=label,
            rows=len(batch_rows), query_len=len(query),
        )
        text = await workflow_run(
            inputs={"mode": "ai_detect", "query": query},
            api_key=api_key,
            user=f"{sid}-split-{label}",
            max_retries=3,
            log_prefix=f"annotate.ai_detect.{label}",
        )
        _annotate_ai_log(
            "subbatch dify done", sid=sid, batch=label, mode="workflow",
            answer_len=len(text or ""), fallback=False,
        )
        if not (text or "").strip():
            return [], "Dify 返回空内容"
        results, err = annotate.parse_ai_detect_result(text)
        if results:
            return _attach_originals(results, batch_rows, id_col, open_text_cols), ""
        retry_q = (
            f"上次输出无法解析（{err}）。请重新处理下面这批数据，严格返回系统提示词指定的"
            "顶层 JSON 数组，不要附加解释文字。\n\n"
            f"{query}"
        )
        retry_text = await workflow_run(
            inputs={"mode": "ai_detect", "query": retry_q},
            api_key=api_key,
            user=f"{sid}-split-retry-{label}",
            max_retries=3,
            log_prefix=f"annotate.ai_detect.{label}.retry",
        )
        _annotate_ai_log(
            "subbatch retry done", sid=sid, batch=label, mode="workflow",
            answer_len=len(retry_text or ""), fallback=False,
        )
        results, retry_err = annotate.parse_ai_detect_result(retry_text)
        if results:
            return _attach_originals(results, batch_rows, id_col, open_text_cols), ""
        return results, retry_err
    except Exception as exc:
        _annotate_ai_log("subbatch failed", sid=sid, batch=label, error=str(exc)[:1000])
        return [], _public_dify_error(str(exc))


async def _repair_missing_translations(
    sid: str,
    results: list[dict],
    batch_rows: list[list],
    id_col: int,
    open_text_cols: list[int],
    api_key: str,
    stage: str,
    fallback_api_key: str = "",
) -> tuple[set[str], str]:
    """只翻译缺失单元格；定向重试后可切换另一工作流兜底。"""
    rows_by_id = {_row_id(row, id_col): row for row in batch_rows}
    results_by_id = {str(result.get("id", "")): result for result in results}

    for row_id, result in results_by_id.items():
        row = rows_by_id.get(row_id)
        if row is None:
            continue
        translations = result.setdefault("translations", {})
        for col in open_text_cols:
            key = f"col_{col}"
            original = str(row[col]).strip() if col < len(row) else ""
            existing = str(translations.get(key, "")).strip()
            if not original or _translation_is_usable(original, existing):
                continue
            if _is_likely_chinese(original) or not re.search(
                r"[A-Za-z\u3040-\u30ff\uac00-\ud7af]", original
            ):
                translations[key] = original

    def pending_items() -> list[dict]:
        pending: list[dict] = []
        for row_id, result in results_by_id.items():
            row = rows_by_id.get(row_id)
            if row is None:
                continue
            translations = result.setdefault("translations", {})
            for col in open_text_cols:
                key = f"col_{col}"
                original = str(row[col]).strip() if col < len(row) else ""
                if original and not _translation_is_usable(original, translations.get(key, "")):
                    pending.append({"id": row_id, "key": key, "text": original})
        return pending

    async def run_repair_pass(
        repair_api_key: str,
        items: list[dict],
        chunk_size: int,
        pass_name: str,
    ) -> None:
        if not repair_api_key:
            return
        single_text_limit = max(1000, ANNOTATE_QUALITY_MAX_QUERY_CHARS - 1500)
        long_items = [
            item for item in items if len(str(item.get("text", ""))) > single_text_limit
        ]
        normal_items = [item for item in items if item not in long_items]

        for long_index, item in enumerate(long_items, 1):
            source = str(item["text"])
            segments = [
                source[start:start + single_text_limit]
                for start in range(0, len(source), single_text_limit)
            ]
            translated_segments: list[str] = []
            for part_index, segment in enumerate(segments, 1):
                query = annotate.build_translation_repair_query([{
                    "id": item["id"], "key": item["key"], "text": segment,
                }])
                try:
                    text = await workflow_run(
                        inputs={"mode": "translation_repair", "query": query},
                        api_key=repair_api_key,
                        user=f"{sid}-{stage}-translate-{pass_name}-long-{long_index}-{part_index}",
                        max_retries=3,
                        log_prefix=(
                            f"annotate.{stage}.translate.{pass_name}."
                            f"long-{long_index}-{part_index}"
                        ),
                    )
                    repaired, _ = annotate.parse_translation_repair_result(text)
                except Exception as exc:
                    _annotate_ai_log(
                        "translation repair failed", sid=sid, stage=stage,
                        pass_name=pass_name, chunk=f"long-{long_index}-{part_index}",
                        error=str(exc)[:1000],
                    )
                    repaired = []
                translation = next((
                    str(repaired_item.get("translation", "")).strip()
                    for repaired_item in repaired
                    if repaired_item.get("id") == item["id"]
                    and repaired_item.get("key") == item["key"]
                ), "")
                if not _translation_is_usable(segment, translation):
                    translated_segments = []
                    break
                translated_segments.append(translation)
            if len(translated_segments) == len(segments):
                results_by_id[item["id"]].setdefault("translations", {})[
                    item["key"]
                ] = "\n".join(translated_segments)

        repair_chunks: list[list[dict]] = []
        current: list[dict] = []
        for item in normal_items:
            candidate = current + [item]
            candidate_query = annotate.build_translation_repair_query(candidate)
            if current and (
                len(candidate) > chunk_size
                or len(candidate_query) > ANNOTATE_QUALITY_MAX_QUERY_CHARS
            ):
                repair_chunks.append(current)
                current = [item]
            else:
                current = candidate
        if current:
            repair_chunks.append(current)

        for index, repair_items in enumerate(repair_chunks, 1):
            query = annotate.build_translation_repair_query(repair_items)
            try:
                text = await workflow_run(
                    inputs={"mode": "translation_repair", "query": query},
                    api_key=repair_api_key,
                    user=f"{sid}-{stage}-translate-{pass_name}-{index}",
                    max_retries=3,
                    log_prefix=f"annotate.{stage}.translate.{pass_name}.{index}",
                )
                repaired, _ = annotate.parse_translation_repair_result(text)
            except Exception as exc:
                _annotate_ai_log(
                    "translation repair failed", sid=sid, stage=stage,
                    pass_name=pass_name, chunk=index, error=str(exc)[:1000],
                )
                repaired = []
            expected = {
                (item["id"], item["key"]): item["text"] for item in repair_items
            }
            for item in repaired:
                pair = (item["id"], item["key"])
                translation = str(item.get("translation", "")).strip()
                if pair in expected and _translation_is_usable(expected[pair], translation):
                    results_by_id[item["id"]].setdefault("translations", {})[
                        item["key"]
                    ] = translation

    pending = pending_items()
    await run_repair_pass(api_key, pending, 20, "primary")
    pending = pending_items()
    if pending:
        await run_repair_pass(api_key, pending, 5, "retry")
    pending = pending_items()
    if pending and fallback_api_key and fallback_api_key != api_key:
        await run_repair_pass(fallback_api_key, pending, 5, "fallback")

    missing_ids: set[str] = set()
    for row_id, result in results_by_id.items():
        row = rows_by_id.get(row_id)
        if row is None:
            continue
        translations = result.get("translations") or {}
        if any(
            str(row[col]).strip() and not _translation_is_usable(
                str(row[col]).strip(), translations.get(f"col_{col}", "")
            )
            for col in open_text_cols if col < len(row)
        ):
            missing_ids.add(row_id)
    return missing_ids, ("中文翻译缺失" if missing_ids else "")



# ── confirm-ai ──────────────────────────────────────────────────


async def annotate_set_confirmed_ai(sid: str, confirmed_ai_ids: list[str], request: Request) -> None:
    """存储用户确认的 AI 作答 ID，必要时自动保存历史。"""
    sess = get_annotate_session(sid)
    if sess.get("ai_status") != "complete" or sess.get("missing_ai_ids"):
        raise HTTPException(status_code=400, detail="AI 作答识别尚未完整完成")
    reviewable_ids = {
        str(result.get("id", ""))
        for result in sess.get("ai_results", [])
        if result.get("ai_prob", 0) >= ANNOTATE_AI_REVIEW_THRESHOLD
    }
    normalized = list(dict.fromkeys(str(row_id).strip() for row_id in confirmed_ai_ids if str(row_id).strip()))
    invalid = set(normalized) - reviewable_ids
    if invalid:
        raise HTTPException(status_code=400, detail="只能确认已进入人工复核范围的玩家")
    sess["confirmed_ai_ids"] = normalized
    sess["ai_confirmation_complete"] = True
    if not (sess.get("tasks") or {}).get("quality"):
        await _save_annotate_result_history(sid, sess, request)


# ── 质量打标 SSE ────────────────────────────────────────────────



# ── 下载 ────────────────────────────────────────────────────────


async def build_and_save_annotate_download(sid: str, request: Request) -> tuple[bytes, str]:
    """生成标注 Excel、落盘历史、返回 (bytes, download_name)。"""
    sess = get_annotate_session(sid)
    loop = asyncio.get_event_loop()
    excel_bytes, download_name = await loop.run_in_executor(
        None, _build_annotate_excel_from_session, sess
    )
    await _save_annotate_result_history(sid, sess, request)
    return excel_bytes, download_name


def get_annotate_history_file(history_id: str, login: dict | None) -> tuple[bytes, str]:
    """从历史记录获取标注文件内容，返回 (bytes, download_name)，找不到则抛 HTTPException。"""
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    entry = _find_history_for_login(history, history_id, login)
    if not entry or entry.get("mode") != "annotate":
        raise HTTPException(status_code=404, detail="标注历史记录不存在")

    raw_path = str(entry.get("annotate_result_path") or "")
    if not raw_path:
        raise HTTPException(status_code=404, detail="这条历史记录没有可下载的标注文件")
    result_path = Path(raw_path)
    if not result_path.is_absolute():
        result_path = ANNOTATE_RESULT_DIR / result_path.name
    try:
        result_resolved = result_path.resolve(strict=True)
        root_resolved = ANNOTATE_RESULT_DIR.resolve()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="标注结果文件已不存在")
    if root_resolved not in result_resolved.parents and result_resolved != root_resolved:
        raise HTTPException(status_code=400, detail="标注结果路径无效")

    download_name = (
        entry.get("annotate_download_name")
        or _annotate_download_filename(entry.get("filename", "annotated"))
    )
    return result_resolved.read_bytes(), download_name


# ── 严格标注流程 ───────────────────────────────────────────────


async def _run_ai_batch_checked(
    sid: str,
    batch_num: int,
    batch: list,
    headers: list,
    open_text_cols: list[int],
    id_col: int,
    background: str,
) -> tuple[int, list[dict], set[str], set[str], str]:
    results, err = await _run_ai_direct_batch(
        sid, batch, headers, open_text_cols, id_col, background,
        DIFY_AI_DETECT_KEY, str(batch_num),
    )
    valid, missing, errors = _validated_ai_results(results, batch, id_col, open_text_cols)
    if missing:
        missing_rows = [row for row in batch if _row_id(row, id_col) in missing]
        retry_results, retry_err = await _run_ai_direct_batch(
            sid, missing_rows, headers, open_text_cols, id_col, background,
            DIFY_AI_DETECT_KEY, f"{batch_num}.miss",
        )
        retry_valid, retry_missing, retry_errors = _validated_ai_results(
            retry_results, missing_rows, id_col, open_text_cols,
        )
        valid.extend(retry_valid)
        missing = retry_missing
        errors = retry_errors
        if retry_err and missing:
            errors.append(retry_err)
    else:
        errors = []

    translation_missing, translation_error = await _repair_missing_translations(
        sid, valid, batch, id_col, open_text_cols, DIFY_AI_DETECT_KEY, f"ai-{batch_num}",
        fallback_api_key=DIFY_QUALITY_KEY,
    )
    if err and missing and not errors:
        errors.append(err)
    detail = _validation_error_summary(errors)
    if translation_missing and not detail:
        detail = translation_error
    return batch_num, valid, missing, translation_missing, detail


async def ai_detect_stream(sid: str, request: Request):
    """Run only missing AI rows, retain prior trusted results, and repair translations."""
    sess = get_annotate_session(sid)
    if sess.get("ai_status") != "running":
        sess["ai_status"] = "running"
    rows = sess.get("rows", [])
    headers = sess.get("headers", [])
    id_col = sess.get("id_col", 1)
    open_text_cols = sess.get("open_text_cols", [])
    background = sess.get("background", "")
    body = rows[1:]
    expected_ids = {_row_id(row, id_col) for row in body}
    order = {_row_id(row, id_col): index for index, row in enumerate(body)}
    results_by_id = {
        str(result.get("id", "")).strip(): result
        for result in sess.get("ai_results", [])
        if str(result.get("id", "")).strip() in expected_ids
    }
    target_ids = expected_ids - set(results_by_id)
    target_ids.update(sess.get("missing_ai_ids") or [])
    for row_id in target_ids:
        results_by_id.pop(row_id, None)

    empty_rows = [
        row for row in body
        if _row_id(row, id_col) in target_ids
        and not _has_open_text(row, open_text_cols)
    ]
    for row in empty_rows:
        result = _empty_ai_result(
            row, id_col, open_text_cols,
            "主观题均为空，无法构成 AI 内容生成证据",
        )
        results_by_id[result["id"]] = result
    active_rows = [
        row for row in body
        if _row_id(row, id_col) in target_ids
        and _has_open_text(row, open_text_cols)
    ]
    max_rows = _effective_batch_size(ANNOTATE_AI_BATCH_SIZE, open_text_cols, 48)
    batches = _chunk_rows_by_query_budget(
        active_rows,
        max_rows,
        ANNOTATE_AI_MAX_QUERY_CHARS,
        lambda batch: annotate.build_ai_detect_query(
            batch, headers, open_text_cols, id_col, "budget", background,
        ),
    )
    pending: set[asyncio.Task] = set()
    try:
        yield sse_event({
            "type": "started",
            "rows": len(body),
            "target_rows": len(target_ids),
            "total_batches": len(batches),
            "batch_size": max_rows,
            "msg": (
                f"已连接，本次仅处理 {len(target_ids)} 行待补结果，分 {len(batches)} 批；"
                f"已保留 {len(results_by_id)} 行可信结果"
            ),
        })
        sem = asyncio.Semaphore(ANNOTATE_AI_CONCURRENCY)

        async def run_with_sem(batch_num: int, batch: list):
            async with sem:
                return await _run_ai_batch_checked(
                    sid, batch_num, batch, headers, open_text_cols, id_col, background,
                )

        pending = {
            asyncio.create_task(run_with_sem(index, batch))
            for index, batch in enumerate(batches, 1)
        }
        done_count = 0
        while pending:
            finished, pending = await asyncio.wait(
                pending,
                timeout=_ANNOTATE_SSE_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not finished:
                yield sse_event({"type": "heartbeat"})
                continue
            for task in finished:
                batch_num, batch_results, missing, translation_missing, err = await task
                done_count += 1
                for result in batch_results:
                    results_by_id[str(result.get("id", "")).strip()] = result
                if missing:
                    yield sse_event({
                        "type": "warn",
                        "msg": (
                            f"第 {batch_num} 批有 {len(missing)} 行未通过完整性校验"
                            + (f"：{err}" if err else "")
                        ),
                    })
                if translation_missing:
                    yield sse_event({
                        "type": "warn",
                        "msg": (
                            f"第 {batch_num} 批的 AI 判断已保留，但仍有 "
                            f"{len(translation_missing)} 行中文翻译待补齐"
                        ),
                    })
                yield sse_event({
                    "type": "progress", "done": done_count, "total": len(batches),
                    "msg": f"第 {batch_num} 批完成，补回 {len(batch_results)} 条可信结果（{done_count}/{len(batches)}）",
                })

        all_missing_ids = expected_ids - set(results_by_id)
        all_results = sorted(
            results_by_id.values(),
            key=lambda result: order.get(str(result.get("id", "")), len(order)),
        )
        all_missing_translation_ids, _ = await _repair_missing_translations(
            sid,
            all_results,
            body,
            id_col,
            open_text_cols,
            DIFY_AI_DETECT_KEY,
            "ai-final",
            fallback_api_key=DIFY_QUALITY_KEY,
        )
        sess["ai_results"] = all_results
        sess["ai_status"] = "complete" if not all_missing_ids else "incomplete"
        sess.pop("missing_ai_ids", None)
        if all_missing_ids:
            sess["missing_ai_ids"] = sorted(all_missing_ids)
        sess.pop("missing_translation_ids", None)
        if all_missing_translation_ids:
            sess["missing_translation_ids"] = sorted(all_missing_translation_ids)
        high_prob = [
            result for result in all_results
            if result.get("ai_prob", 0) >= ANNOTATE_AI_HIGH_THRESHOLD
        ]
        review_results = [
            result for result in all_results
            if result.get("ai_prob", 0) >= ANNOTATE_AI_REVIEW_THRESHOLD
        ]
        if not all_missing_ids:
            if not review_results:
                sess["ai_confirmation_complete"] = True
                sess["confirmed_ai_ids"] = []
            elif not sess.get("ai_confirmation_complete"):
                sess["confirmed_ai_ids"] = []
        if (
            not _annotate_incomplete_detail(sess)
            and not (sess.get("tasks") or {}).get("quality")
        ):
            await _save_annotate_result_history(sid, sess, request)
        await audit_log(
            request, "annotate", "完成 AI 作答识别",
            f"会话：{sid}；结果数：{len(all_results)}；高风险数：{len(high_prob)}；待复核数：{len(review_results)}",
            metadata={
                "session_id": sid, "results": len(all_results),
                "high_prob": len(high_prob), "review": len(review_results),
                "missing": len(all_missing_ids),
                "missing_translations": len(all_missing_translation_ids),
            },
        )
        yield sse_event({
            "type": "ai_detect_done",
            "results": all_results,
            "high_prob": high_prob,
            "review_results": review_results,
            "review_threshold": ANNOTATE_AI_REVIEW_THRESHOLD,
            "high_threshold": ANNOTATE_AI_HIGH_THRESHOLD,
            "confirmation_complete": bool(sess.get("ai_confirmation_complete")),
            "missing_ids": sorted(all_missing_ids),
            "missing_translation_ids": sorted(all_missing_translation_ids),
        })
    except asyncio.CancelledError:
        sess["ai_status"] = "incomplete"
        raise
    except Exception as exc:
        sess["ai_status"] = "incomplete"
        yield sse_event({"type": "error", "message": str(exc)})
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _run_one_quality_batch_strict(
    sid: str,
    batch_num: int,
    batch: list,
    headers: list,
    open_text_cols: list[int],
    id_col: int,
    include_translations: bool,
) -> tuple[int, list[dict], set[str], str]:
    async def call(current_query: str) -> tuple[list[dict], str]:
        output = await workflow_run(
            inputs={"mode": "quality_label", "query": current_query},
            api_key=DIFY_QUALITY_KEY,
            user=f"{sid}-quality-{batch_num}",
            max_retries=3,
            log_prefix=f"annotate.quality.{batch_num}",
        )
        if not output.strip():
            return [], "Dify Workflow 返回空内容"
        return annotate.parse_quality_result(output)

    try:
        _, query = _fit_rows_to_query_budget(
            batch,
            open_text_cols,
            ANNOTATE_QUALITY_MAX_QUERY_CHARS - 500,
            lambda model_rows: annotate.build_quality_label_query(
                model_rows, headers, open_text_cols, id_col, batch_num,
                include_translations=include_translations,
            ),
        )
        parsed, conv_or_error = await call(query)
        if not parsed:
            retry_query = (
                f"上次输出无法解析（{conv_or_error}）。请重新处理并严格返回指定 JSON。\n\n{query}"
            )
            parsed, conv_or_error = await call(retry_query)
        parsed_by_id = {
            str(result.get("id", "")).strip(): result
            for result in parsed if str(result.get("id", "")).strip()
        }
        invalid_cols_by_id: dict[str, set[int]] = {}
        rows_by_id = {_row_id(row, id_col): row for row in batch}
        for row_id, row in rows_by_id.items():
            invalid_cols = _quality_invalid_cols(
                parsed_by_id.get(row_id), row, open_text_cols,
            )
            if invalid_cols:
                invalid_cols_by_id[row_id] = invalid_cols

        repair_error = ""
        if invalid_cols_by_id:
            repair_rows = [
                row for row in batch if _row_id(row, id_col) in invalid_cols_by_id
            ]
            repair_cols = sorted({
                col for cols in invalid_cols_by_id.values() for col in cols
            })
            _, repair_query = _fit_rows_to_query_budget(
                repair_rows,
                repair_cols,
                ANNOTATE_QUALITY_MAX_QUERY_CHARS,
                lambda model_rows: annotate.build_quality_label_query(
                    model_rows, headers, repair_cols, id_col, f"{batch_num}.miss",
                    include_translations=include_translations,
                ),
            )
            try:
                repair_parsed, repair_context = await call(repair_query)
                repair_error = "" if repair_parsed else repair_context
            except Exception as exc:
                repair_parsed = []
                repair_error = _public_dify_error(str(exc))

            for repaired in repair_parsed:
                row_id = str(repaired.get("id", "")).strip()
                invalid_cols = invalid_cols_by_id.get(row_id)
                if not invalid_cols:
                    continue
                base = parsed_by_id.setdefault(row_id, {
                    "id": row_id,
                    "q_labels": {},
                    "q_reasons": {},
                    "q_evidence": {},
                    "translations": {},
                })
                for field in ("q_labels", "q_reasons", "q_evidence", "translations"):
                    source = repaired.get(field) or {}
                    target = base.setdefault(field, {})
                    for col in invalid_cols:
                        key = f"col_{col}"
                        if key in source:
                            target[key] = source[key]

        valid, missing, errors = _validated_quality_results(
            list(parsed_by_id.values()), batch, id_col, open_text_cols, include_translations,
        )
        if missing and repair_error:
            errors.append(repair_error)

        return batch_num, valid, missing, _validation_error_summary(errors)
    except Exception as exc:
        return (
            batch_num,
            [],
            {_row_id(row, id_col) for row in batch},
            _public_dify_error(str(exc)),
        )


async def quality_stream(sid: str, request: Request):
    """Run only missing quality rows and retain prior trusted labels."""
    sess = get_annotate_session(sid)
    if sess.get("quality_status") != "running":
        sess["quality_status"] = "running"
    rows = sess.get("rows", [])
    headers = sess.get("headers", [])
    id_col = sess.get("id_col", 1)
    open_text_cols = sess.get("open_text_cols", [])
    confirmed_ai_ids = set(sess.get("confirmed_ai_ids", []))
    body = [row for row in rows[1:] if _row_id(row, id_col) not in confirmed_ai_ids]
    expected_ids = {_row_id(row, id_col) for row in body}
    results_by_id = {
        str(result.get("id", "")).strip(): result
        for result in sess.get("quality_results", [])
        if str(result.get("id", "")).strip() in expected_ids
    }
    target_ids = expected_ids - set(results_by_id)
    target_ids.update(sess.get("missing_quality_ids") or [])
    for row_id in target_ids:
        results_by_id.pop(row_id, None)

    empty_rows = [
        row for row in body
        if _row_id(row, id_col) in target_ids
        and not _has_open_text(row, open_text_cols)
    ]
    for row in empty_rows:
        result = _empty_quality_result(row, id_col, open_text_cols)
        results_by_id[result["id"]] = result
    active_rows = [
        row for row in body
        if _row_id(row, id_col) in target_ids
        and _has_open_text(row, open_text_cols)
    ]
    max_rows = _effective_batch_size(ANNOTATE_QUALITY_BATCH_SIZE, open_text_cols, 36)
    include_translations = not bool((sess.get("tasks") or {}).get("ai_detect"))
    batches = _chunk_rows_by_query_budget(
        active_rows,
        max_rows,
        ANNOTATE_QUALITY_MAX_QUERY_CHARS,
        lambda batch: annotate.build_quality_label_query(
            batch, headers, open_text_cols, id_col, "budget",
            include_translations=include_translations,
        ),
    )
    pending: set[asyncio.Task] = set()
    try:
        yield sse_event({
            "type": "progress", "done": 0, "total": len(batches),
            "msg": (
                f"本次仅处理 {len(target_ids)} 行待补质量结果，分 {len(batches)} 批；"
                f"已保留 {len(results_by_id)} 行可信结果"
            ),
        })
        sem = asyncio.Semaphore(ANNOTATE_QUALITY_CONCURRENCY)

        async def run_with_sem(batch_num: int, batch: list):
            async with sem:
                return await _run_one_quality_batch_strict(
                    sid, batch_num, batch, headers, open_text_cols, id_col, include_translations,
                )

        pending = {
            asyncio.create_task(run_with_sem(index, batch))
            for index, batch in enumerate(batches, 1)
        }
        done_count = 0
        while pending:
            finished, pending = await asyncio.wait(
                pending,
                timeout=_ANNOTATE_SSE_HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not finished:
                yield sse_event({"type": "heartbeat"})
                continue
            for task in finished:
                batch_num, batch_results, missing, err = await task
                done_count += 1
                for result in batch_results:
                    results_by_id[str(result.get("id", "")).strip()] = result
                if missing or err:
                    yield sse_event({
                        "type": "warn",
                        "msg": f"第 {batch_num} 批有 {len(missing)} 行未通过完整性校验" + (f"：{err}" if err else ""),
                    })
                yield sse_event({
                    "type": "progress", "done": done_count, "total": len(batches),
                    "msg": f"第 {batch_num} 批完成，补回 {len(batch_results)} 条可信结果（{done_count}/{len(batches)}）",
                })

        all_missing_ids = expected_ids - set(results_by_id)
        ai_by_id = {
            str(result.get("id", "")): result
            for result in sess.get("ai_results", [])
        }
        all_results = list(results_by_id.values())
        for result in all_results:
            ai_translations = (
                ai_by_id.get(str(result.get("id", "")), {}).get("translations") or {}
            )
            merged = dict(ai_translations)
            merged.update(result.get("translations") or {})
            result["translations"] = merged

        translation_targets = dict(ai_by_id)
        translation_targets.update({
            str(result.get("id", "")): result
            for result in all_results if str(result.get("id", ""))
        })
        missing_translation_ids, _ = await _repair_missing_translations(
            sid,
            list(translation_targets.values()),
            rows[1:],
            id_col,
            open_text_cols,
            DIFY_QUALITY_KEY,
            "quality-final",
            fallback_api_key=DIFY_AI_DETECT_KEY,
        )
        order = {_row_id(row, id_col): index for index, row in enumerate(rows[1:])}
        all_results.sort(key=lambda result: order.get(str(result.get("id", "")), len(order)))
        sess["quality_results"] = all_results
        sess["quality_status"] = "complete" if not all_missing_ids else "incomplete"
        sess.pop("missing_quality_ids", None)
        if all_missing_ids:
            sess["missing_quality_ids"] = sorted(all_missing_ids)
        sess.pop("missing_translation_ids", None)
        if missing_translation_ids:
            sess["missing_translation_ids"] = sorted(missing_translation_ids)
        if not _annotate_incomplete_detail(sess):
            await _save_annotate_result_history(sid, sess, request)
        await audit_log(
            request, "annotate", "完成回答质量打标",
            f"会话：{sid}；结果数：{len(all_results)}；未回填：{len(all_missing_ids)}",
            metadata={
                "session_id": sid,
                "results": len(all_results),
                "missing": len(all_missing_ids),
                "missing_translations": len(missing_translation_ids),
            },
        )
        yield sse_event({
            "type": "quality_done", "count": len(all_results),
            "results": all_results, "missing_ids": sorted(all_missing_ids),
            "missing_translation_ids": sorted(missing_translation_ids),
        })
    except asyncio.CancelledError:
        sess["quality_status"] = "incomplete"
        raise
    except Exception as exc:
        sess["quality_status"] = "incomplete"
        yield sse_event({"type": "error", "message": str(exc)})
    finally:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
