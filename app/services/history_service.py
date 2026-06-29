"""services/history_service:历史记录列表、详情查询。"""
from app.core.security import _find_history_for_login, _visible_to_owner
from app.services.report_history import _history_effective_row_count, _qa_user_count
from app.storage.history import _ensure_history_report_numbers, _load_history


def get_history_list(login: dict | None, mode: str = "") -> list[dict]:
    """返回对当前用户可见的历史列表（列表视图格式，不含完整 report_md）。"""
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    visible = [h for h in history if _visible_to_owner(h, login)]
    if mode:
        visible = [h for h in visible if (h.get("mode") or "") == mode]
    return [
        {
            "id": h["id"],
            "report_no": h.get("report_no", ""),
            "filename": h["filename"],
            "title": h["title"],
            "created_at": h["created_at"],
            "has_qa": bool(h.get("analyst_conv_id")),
            "qa_count": _qa_user_count(h),
            "mode": h.get("mode", ""),
            "row_count": _history_effective_row_count(h),
            "comment_valid_count": h.get("comment_valid_count", 0),
            "comment_sample_count": h.get("comment_sample_count", 0),
            "annotate_ai_count": h.get("annotate_ai_count", 0),
            "annotate_confirmed_ai_count": h.get("annotate_confirmed_ai_count", 0),
            "annotate_quality_count": h.get("annotate_quality_count", 0),
            "annotate_has_download": bool(h.get("annotate_result_path")),
        }
        for h in visible
    ]


def get_history_entry(hist_id: str, login: dict | None) -> dict | None:
    """返回指定历史记录（含完整内容），找不到或无权限返回 None。"""
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    return _find_history_for_login(history, hist_id, login)
