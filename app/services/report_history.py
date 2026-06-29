"""services/report_history:报告如何进入历史的业务规则。

不是底层文件读写(那在 storage/history),而是:会话→历史条目的组装、报告改名、
评论查重、去重、历史展示字段计算。被 survey / comment / annotate / history 共同调用。
"""
import re
from datetime import datetime

from fastapi import HTTPException

from app.core.security import (
    _assign_session_owner,
    _find_history_for_login,
    _trim_history_for_owner,
    _visible_to_owner,
)
from app.storage.history import (
    _ensure_history_report_numbers,
    _load_history,
    _next_history_report_no,
    _save_history,
)
from app.storage.sessions import get_session, save_session


def _qa_user_count(entry: dict) -> int:
    return sum(1 for m in entry.get("qa_messages", []) if m.get("role") == "user")


def _sanitize_report_title(title: str) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", str(title or "")).strip()
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="报告名称不能为空")
    return cleaned[:120]


def _replace_report_h1(report_md: str, title: str) -> str:
    report_md = report_md or ""
    if re.search(r"^#\s+.+?$", report_md, re.MULTILINE):
        return re.sub(r"^#\s+.+?$", f"# {title}", report_md, count=1, flags=re.MULTILINE)
    return f"# {title}\n\n{report_md.lstrip()}"


def _comment_report_title(sess: dict) -> str:
    post_title = re.sub(r"\s+", " ", str(sess.get("comment_post_title") or "").strip())
    if post_title:
        suffix = "·舆情简报"
        max_post_len = max(1, 120 - len(suffix))
        if len(post_title) > max_post_len:
            post_title = post_title[: max_post_len - 1] + "…"
        return f"{post_title}{suffix}"
    return "评论分析·舆情简报"


def save_to_history(session_id: str, sess: dict) -> None:
    report_md = sess.get("report_md", "")
    if not report_md:
        return
    history = _load_history()
    _ensure_history_report_numbers(history, save=False)
    old_entry = next((h for h in history if h.get("id") == session_id), None)
    qa_messages = sess.get("qa_messages")
    if qa_messages is None and old_entry:
        qa_messages = old_entry.get("qa_messages", [])
    if sess.get("mode") == "comment":
        title = sess.get("comment_report_title") or _comment_report_title(sess)
    else:
        title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
        title = title_m.group(1).strip() if title_m else "未命名报告"
    owner = {
        "owner_key": sess.get("owner_key") or (old_entry or {}).get("owner_key", ""),
        "owner_email": sess.get("owner_email") or (old_entry or {}).get("owner_email", ""),
        "owner_open_id": sess.get("owner_open_id") or (old_entry or {}).get("owner_open_id", ""),
        "owner_name": sess.get("owner_name") or (old_entry or {}).get("owner_name", ""),
    }
    entry = {
        "id": session_id,
        "report_no": old_entry.get("report_no") if old_entry else _next_history_report_no(history),
        "filename": sess.get("filename", "unknown"),
        "title": title,
        "created_at": old_entry.get("created_at") if old_entry else datetime.now().isoformat(),
        "report_md": report_md,
        "plan": sess.get("plan"),
        "stats_md": sess.get("stats_md"),
        "analyst_conv_id": sess.get("analyst_conv_id", ""),
        "analyst_app": sess.get("analyst_app", ""),
        "qa_messages": qa_messages or [],
        "rows_fed": True,  # 历史 QA 跳过投喂 rows（对话已包含上下文）
        "mode": sess.get("mode", ""),  # 保存模式，导出时据此选择免责声明（定性/crosstab/comment）
        "row_count": max(0, len(sess.get("rows") or []) - 1) or (old_entry or {}).get("row_count", 0),
        **owner,
    }
    if sess.get("mode") == "comment":
        entry.update({
            "comment_file_hash": sess.get("comment_file_hash", ""),
            "comment_source_filename": sess.get("filename", "unknown"),
            "comment_post_title": sess.get("comment_post_title", ""),
            "comment_report_title": title,
            "comment_result": sess.get("comment_result"),
            "comment_sample_meta": sess.get("comment_sample_meta", {}),
            "comment_relevance_stats": sess.get("comment_relevance_stats", {}),
            "comment_selected_raw_comments": sess.get("comment_selected_raw_comments", []),
            "comment_valid_count": sess.get("comment_valid_count", 0),
            "comment_sample_count": sess.get("comment_sample_count", 0),
            "comment_scan_rows": sess.get("comment_scan_rows", 0),
            "comment_nonempty_count": sess.get("comment_nonempty_count", 0),
        })
    history = [h for h in history if h["id"] != session_id]
    history.insert(0, entry)
    history = _trim_history_for_owner(history, owner.get("owner_key", ""))
    _save_history(history)


def save_annotate_to_history(sid: str, sess: dict, result_path: str, download_name: str) -> None:
    history = _load_history()
    _ensure_history_report_numbers(history, save=False)
    old_entry = next((h for h in history if h.get("id") == sid), None)
    filename = sess.get("filename", "annotated")
    stem = re.sub(r"\.(csv|xlsx|xls)$", "", filename, flags=re.IGNORECASE).strip() or "标注结果"
    owner = {
        "owner_key": sess.get("owner_key") or (old_entry or {}).get("owner_key", ""),
        "owner_email": sess.get("owner_email") or (old_entry or {}).get("owner_email", ""),
        "owner_open_id": sess.get("owner_open_id") or (old_entry or {}).get("owner_open_id", ""),
        "owner_name": sess.get("owner_name") or (old_entry or {}).get("owner_name", ""),
    }
    tasks = sess.get("tasks", {}) or {}
    entry = {
        "id": sid,
        "report_no": old_entry.get("report_no") if old_entry else _next_history_report_no(history),
        "filename": filename,
        "title": (old_entry or {}).get("title") or f"数据标注结果 - {stem}",
        "created_at": old_entry.get("created_at") if old_entry else datetime.now().isoformat(),
        "report_md": "",
        "plan": None,
        "stats_md": "",
        "analyst_conv_id": "",
        "analyst_app": "",
        "qa_messages": [],
        "rows_fed": False,
        "mode": "annotate",
        "row_count": max(0, len(sess.get("rows") or []) - 1),
        "annotate_result_path": result_path,
        "annotate_download_name": download_name,
        "annotate_ai_count": len(sess.get("ai_results") or []),
        "annotate_confirmed_ai_count": len(sess.get("confirmed_ai_ids") or []),
        "annotate_quality_count": len(sess.get("quality_results") or []),
        "annotate_tasks": {
            "ai_detect": bool(tasks.get("ai_detect")),
            "quality": bool(tasks.get("quality")),
        },
        **owner,
    }
    history = [h for h in history if h.get("id") != sid]
    history.insert(0, entry)
    history = _trim_history_for_owner(history, owner.get("owner_key", ""))
    _save_history(history)


def _find_comment_duplicate_report(file_hash: str, login: dict | None) -> dict | None:
    if not file_hash:
        return None
    history = _ensure_history_report_numbers(_load_history())
    for entry in history:
        if (
            entry.get("mode") == "comment"
            and entry.get("comment_file_hash") == file_hash
            and _visible_to_owner(entry, login)
        ):
            return {
                "id": entry.get("id", ""),
                "report_no": entry.get("report_no", ""),
                "title": entry.get("title", "评论分析报告"),
                "filename": entry.get("filename", ""),
                "created_at": entry.get("created_at", ""),
                "valid_count": entry.get("comment_valid_count", 0),
                "sample_count": entry.get("comment_sample_count", 0),
            }
    return None


def _history_effective_row_count(entry: dict) -> int:
    try:
        row_count = int(entry.get("row_count") or 0)
    except (TypeError, ValueError):
        row_count = 0
    if row_count > 0:
        return row_count
    text = f"{entry.get('stats_md') or ''}\n{entry.get('report_md') or ''}"
    for pattern in (
        r"有效样本\(总计\):总体=(\d+)",
        r"有效样本（总计）:总体=(\d+)",
        r"有效样本[^\n]*总体=(\d+)",
        r"(\d+)\s*名受访者",
    ):
        m = re.search(pattern, text)
        if m:
            return int(m.group(1))
    return 0


def _update_history_title_by_id(hist_id: str, title: str, login: dict | None = None) -> dict:
    hist_id = str(hist_id or "").strip()
    new_title = _sanitize_report_title(title)
    history = _load_history()
    history = _ensure_history_report_numbers(history, save=False)
    entry = _find_history_for_login(history, hist_id, login)
    if not entry:
        try:
            sess = get_session(hist_id)
            if sess.get("report_md") and _visible_to_owner(sess, login):
                _assign_session_owner(sess, login)
                sess["report_md"] = _replace_report_h1(sess.get("report_md", ""), new_title)
                save_session(hist_id, sess)
                save_to_history(hist_id, sess)
                history = _load_history()
                history = _ensure_history_report_numbers(history, save=False)
                entry = _find_history_for_login(history, hist_id, login)
        except HTTPException:
            pass
    if not entry:
        print(f"[history-title] not found id={hist_id!r} existing={[h.get('id') for h in history]}")
        raise HTTPException(status_code=404, detail="未找到这份报告，请刷新历史记录后重试")

    entry["title"] = new_title
    entry["report_md"] = _replace_report_h1(entry.get("report_md", ""), new_title)

    try:
        sess = get_session(hist_id)
        if sess.get("report_md"):
            sess["report_md"] = _replace_report_h1(sess.get("report_md", ""), new_title)
            save_session(hist_id, sess)
    except HTTPException:
        pass  # session 已过期，只更新历史记录即可

    _save_history(history)
    return {
        "ok": True,
        "id": hist_id,
        "report_no": entry.get("report_no", ""),
        "title": new_title,
        "report_md": entry.get("report_md", ""),
    }
