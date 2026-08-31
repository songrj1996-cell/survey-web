"""services/history_service:历史记录列表、详情查询。"""
from copy import deepcopy

from app.core.config import MAX_REPORT_VERSIONS
from app.core.security import _find_history_for_login, _visible_to_owner
from app.services.report_history import (
    _history_effective_row_count,
    _qa_user_count,
    _supports_report_versions,
)
from app.services.report_versions import (
    normalize_report_versions,
    report_version_summaries,
    resolve_report_version,
)
from app.storage.history import _load_history_with_report_numbers
from app.storage.sessions import get_session  # kept as a stable patch seam for integrations


_SELECTED_VERSION_FIELDS = (
    "report_md",
    "title",
    "qa_context_md",
    "qa_messages",
    "qa_provider",
    "qa_model",
    "report_writer_provider",
    "report_writer_model",
    "analyst_conv_id",
    "analyst_app",
    "comparison_validation",
)


def _can_generate_report_version(entry: dict, login: dict | None) -> bool:
    # 报告页只负责查看/切换/追问/导出；重跑必须从新上传的数据确认页发起。
    return False


def _next_report_version_number(entry: dict, versions: list[dict]) -> int | None:
    if not versions:
        return None
    minimum = max(item["version"] for item in versions) + 1
    try:
        configured = int(entry.get("next_report_version") or minimum)
    except (TypeError, ValueError):
        configured = minimum
    return max(minimum, configured)


def _history_version_metadata(entry: dict, login: dict | None) -> dict:
    if not _supports_report_versions(entry):
        return {
            "report_versions": [],
            "versions": [],
            "active_report_version": None,
            "active_version": None,
            "next_version": None,
            "version_count": 0,
            "max_versions": MAX_REPORT_VERSIONS,
            "can_generate_version": False,
        }
    versions = normalize_report_versions(entry)
    if not versions:
        summaries = []
        active_version = None
    else:
        summaries = report_version_summaries(entry)
        active_version = resolve_report_version(entry)["version"]
    return {
        "report_versions": summaries,
        "versions": deepcopy(summaries),
        "active_report_version": active_version,
        "active_version": active_version,
        "next_version": _next_report_version_number(entry, versions),
        "version_count": len(versions),
        "max_versions": MAX_REPORT_VERSIONS,
        "can_generate_version": _can_generate_report_version(entry, login),
    }


def get_history_list(login: dict | None, mode: str = "") -> list[dict]:
    """返回对当前用户可见的历史列表（列表视图格式，不含完整 report_md）。"""
    history = _load_history_with_report_numbers()
    visible = [h for h in history if _visible_to_owner(h, login)]
    if mode:
        visible = [h for h in visible if (h.get("mode") or "") == mode]
    result = []
    for h in visible:
        active_snapshot = (
            resolve_report_version(h)
            if _supports_report_versions(h)
            else h
        )
        result.append({
            "id": h["id"],
            "report_no": h.get("report_no", ""),
            "filename": h["filename"],
            "title": h["title"],
            "created_at": h["created_at"],
            "has_qa": bool(h.get("qa_messages") or h.get("analyst_conv_id")),
            "qa_count": _qa_user_count(h),
            "mode": h.get("mode", ""),
            "row_count": _history_effective_row_count(h),
            "comment_valid_count": h.get("comment_valid_count", 0),
            "comment_sample_count": h.get("comment_sample_count", 0),
            "interview_sheet_count": h.get("interview_sheet_count", 0),
            "interview_player_count": h.get("interview_player_count", 0),
            "interview_module_count": h.get("interview_module_count", 0),
            "annotate_ai_count": h.get("annotate_ai_count", 0),
            "annotate_confirmed_ai_count": h.get("annotate_confirmed_ai_count", 0),
            "annotate_quality_count": h.get("annotate_quality_count", 0),
            "annotate_has_download": bool(h.get("annotate_result_path")),
            "annotate_quality_duration_seconds": h.get(
                "annotate_quality_duration_seconds"
            ),
            "report_duration_seconds": active_snapshot.get(
                "report_duration_seconds"
            ),
            **_history_version_metadata(h, login),
        })
    return result


def get_history_entry(
    hist_id: str,
    login: dict | None,
    version=None,
) -> dict | None:
    """返回指定历史记录（含完整内容），找不到或无权限返回 None。"""
    history = _load_history_with_report_numbers()
    entry = _find_history_for_login(history, hist_id, login)
    if not entry:
        return None

    result = deepcopy(entry)
    metadata = _history_version_metadata(entry, login)
    result.update(metadata)
    if metadata["version_count"]:
        selected = resolve_report_version(
            entry,
            None if version in (None, "") else version,
        )
        for field in _SELECTED_VERSION_FIELDS:
            result[field] = deepcopy(selected[field])
        result.update({
            "version": selected["version"],
            "selected_version": selected["version"],
            "kind": selected["kind"],
            "base_version": selected["base_version"],
            "instruction": selected["instruction"],
            "version_created_at": selected["created_at"],
            "plan_approved_at": selected.get("plan_approved_at", ""),
            "report_completed_at": selected.get("report_completed_at", ""),
            "report_duration_seconds": selected.get("report_duration_seconds"),
        })
    return result
