"""services/report_history:报告如何进入历史的业务规则。

不是底层文件读写(那在 storage/history),而是:会话→历史条目的组装、报告改名、
评论查重、去重、历史展示字段计算。被 survey / comment / annotate / history 共同调用。
"""
from copy import deepcopy
from difflib import SequenceMatcher
import re
import unicodedata
from datetime import datetime

from fastapi import HTTPException

from app.core.config import MAX_REPORT_VERSIONS
from app.core.security import (
    _assign_session_owner,
    _find_history_for_login,
    _history_owner_key,
    _owner_from_login,
    _trim_history_for_owner,
    _visible_to_owner,
)
from app.services.report_versions import (
    append_report_version,
    delete_report_version,
    normalize_report_versions,
    report_version_summaries,
    resolve_report_version,
    sync_active_report_version,
    update_report_version,
)
from app.services.report_partial_rerun import (
    build_partial_rerun_source,
    build_plan_fingerprint,
    verify_partial_rerun_source,
)
from app.storage.history import (
    _ensure_history_report_numbers,
    _load_history,
    _load_history_with_report_numbers,
    _next_history_report_no,
    mutate_history,
)
from app.storage.sessions import get_session, save_session


_NON_VERSIONED_REPORT_MODES = {"comment", "interview", "annotate"}
_SURVEY_DUPLICATE_CONTEXT_FIELDS = (
    "problem",
    "key_concerns",
    "target_users",
    "analysis_approach",
)
_SURVEY_DUPLICATE_CONTEXT_SIMILARITY = 0.80
DEFAULT_RERUN_VERSION_INSTRUCTION = "未填写补充要求，本次为重新生成"
_VERSION_MIRROR_FIELDS = (
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


def _supports_report_versions(source: dict) -> bool:
    return bool(source.get("report_md")) and (
        source.get("mode") or ""
    ) not in _NON_VERSIONED_REPORT_MODES


def _history_version_source(
    sess: dict,
    old_entry: dict | None,
    *,
    title: str,
    created_at: str,
    replace_report_versions: bool = False,
) -> dict | None:
    """Build a compact, synchronized version source for one survey entry.

    Ordinary saves merge the session snapshots into the latest history entry.
    This prevents an older live session from erasing versions appended by a
    separate exact-match rerun. Explicit deletion is the sole caller allowed
    to replace the persisted list wholesale.
    """
    if not _supports_report_versions(sess):
        return None

    session_source: dict = {}
    for field in (
        "report_versions",
        "active_report_version",
        "next_report_version",
    ):
        if field in sess:
            session_source[field] = deepcopy(sess[field])
    for field in _VERSION_MIRROR_FIELDS:
        if field in sess:
            session_source[field] = deepcopy(sess[field])
    session_source["report_md"] = str(sess.get("report_md") or "")
    session_source["title"] = title
    session_source["created_at"] = created_at

    versions = normalize_report_versions(session_source)
    if not versions:
        return None
    if session_source.get("report_versions"):
        active_version = resolve_report_version(session_source)["version"]
        active_updates = {
            field: deepcopy(session_source[field])
            for field in _VERSION_MIRROR_FIELDS
            if field in session_source
        }
        active_updates["report_md"] = session_source["report_md"]
        active_updates["title"] = title
        update_report_version(session_source, active_version, **active_updates)
    else:
        sync_active_report_version(session_source)

    if replace_report_versions or not _supports_report_versions(old_entry or {}):
        return session_source

    history_source = deepcopy(old_entry)
    history_versions = normalize_report_versions(history_source)
    session_versions = normalize_report_versions(session_source)
    history_numbers = {item["version"] for item in history_versions}
    session_numbers = {item["version"] for item in session_versions}
    history_next_version = max(history_numbers) + 1
    try:
        history_next_version = max(
            history_next_version,
            int(old_entry.get("next_report_version") or 0),
        )
    except (TypeError, ValueError):
        pass
    merged_by_version = {
        item["version"]: deepcopy(item)
        for item in history_versions
    }
    for item in session_versions:
        version = item["version"]
        if version in history_numbers or version >= history_next_version:
            merged_by_version[version] = deepcopy(item)
    history_source["report_versions"] = [
        merged_by_version[version]
        for version in sorted(merged_by_version)
    ]

    # A strict-subset session is stale, so it must not reactivate its old V1.
    # When both sides know the same versions, the live session remains the
    # source of truth for active-version state.
    if history_numbers - session_numbers:
        history_source["active_report_version"] = resolve_report_version(
            old_entry,
        )["version"]
    else:
        history_source["active_report_version"] = resolve_report_version(
            session_source,
        )["version"]

    configured_next = []
    for candidate in (old_entry, session_source):
        try:
            configured_next.append(int(candidate.get("next_report_version") or 0))
        except (TypeError, ValueError):
            continue
    history_source["next_report_version"] = max(
        [max(merged_by_version) + 1, *configured_next]
    )
    sync_active_report_version(history_source)
    return history_source


def _rename_report_source(source: dict, title: str) -> None:
    """Rename the top-level report and every survey snapshot in place."""
    if _supports_report_versions(source):
        versions = normalize_report_versions(source)
        for snapshot in versions:
            snapshot["title"] = title
            snapshot["report_md"] = _replace_report_h1(
                snapshot.get("report_md", ""),
                title,
            )
        source["report_versions"] = versions
        sync_active_report_version(source)
        return
    source["title"] = title
    source["report_md"] = _replace_report_h1(source.get("report_md", ""), title)


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


def _normalize_duplicate_context_text(value) -> str:
    """Normalize user context for deterministic Chinese/English text comparison."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(char for char in normalized if char.isalnum())


def _duplicate_context_similarity(left: dict, right: dict) -> float:
    """Return a length-weighted similarity across the four confirmation fields."""
    weighted_score = 0.0
    total_weight = 0
    for field in _SURVEY_DUPLICATE_CONTEXT_FIELDS:
        left_text = _normalize_duplicate_context_text(left.get(field, ""))
        right_text = _normalize_duplicate_context_text(right.get(field, ""))
        weight = max(len(left_text), len(right_text))
        if not weight:
            continue
        score = SequenceMatcher(
            None,
            left_text,
            right_text,
            autojunk=False,
        ).ratio()
        weighted_score += score * weight
        total_weight += weight
    return 1.0 if not total_weight else weighted_score / total_weight


def _has_complete_survey_duplicate_fingerprint(source: dict) -> bool:
    required = (
        "source_type",
        "file_sha256",
        "questionnaire_sha256",
        "questionnaire_used",
        "qualitative_context",
    )
    if any(field not in source for field in required):
        return False
    if source.get("source_type") not in {"google", "bested"}:
        return False
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(source.get("file_sha256") or "").strip().lower(),
    ):
        return False
    context = source.get("qualitative_context")
    if not isinstance(context, dict) or any(
        field not in context for field in _SURVEY_DUPLICATE_CONTEXT_FIELDS
    ):
        return False
    questionnaire_sha256 = str(source.get("questionnaire_sha256") or "").strip().lower()
    return not bool(source.get("questionnaire_used")) or bool(
        re.fullmatch(r"[0-9a-f]{64}", questionnaire_sha256)
    )


def _is_exact_survey_duplicate(entry: dict, sess: dict, login: dict | None) -> bool:
    """Match one owner's identical upload when its submitted context is >=80% similar."""
    if not _supports_report_versions(entry) or not isinstance(entry.get("plan"), dict):
        return False
    if not _has_complete_survey_duplicate_fingerprint(entry):
        return False
    if not _has_complete_survey_duplicate_fingerprint(sess):
        return False
    if not _visible_to_owner(entry, login):
        return False
    if _history_owner_key(entry) != _history_owner_key(sess):
        return False

    required_scalar_fields = (
        "source_type",
        "file_sha256",
        "questionnaire_sha256",
        "questionnaire_used",
    )
    if any(field not in entry or field not in sess for field in required_scalar_fields):
        return False
    if entry.get("source_type") != sess.get("source_type"):
        return False

    file_sha256 = str(sess.get("file_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", file_sha256):
        return False
    if str(entry.get("file_sha256") or "").strip().lower() != file_sha256:
        return False

    questionnaire_used = bool(sess.get("questionnaire_used"))
    if bool(entry.get("questionnaire_used")) != questionnaire_used:
        return False
    questionnaire_sha256 = str(sess.get("questionnaire_sha256") or "").strip().lower()
    if questionnaire_used and not re.fullmatch(r"[0-9a-f]{64}", questionnaire_sha256):
        return False
    if (
        str(entry.get("questionnaire_sha256") or "").strip().lower()
        != questionnaire_sha256
    ):
        return False

    entry_context = entry.get("qualitative_context")
    session_context = sess.get("qualitative_context")
    if not isinstance(entry_context, dict) or not isinstance(session_context, dict):
        return False
    if any(
        field not in entry_context or field not in session_context
        for field in _SURVEY_DUPLICATE_CONTEXT_FIELDS
    ):
        return False
    return (
        _duplicate_context_similarity(entry_context, session_context)
        >= _SURVEY_DUPLICATE_CONTEXT_SIMILARITY
    )


def _duplicate_report_summary(entry: dict) -> dict:
    versions = normalize_report_versions(entry)
    active_version = resolve_report_version(entry)["version"]
    return {
        "id": entry.get("id", ""),
        "history_id": entry.get("id", ""),
        "report_no": entry.get("report_no", ""),
        "title": entry.get("title", "未命名报告"),
        "filename": entry.get("filename", ""),
        "created_at": entry.get("created_at", ""),
        "version_count": len(versions),
        "active_version": active_version,
    }


def find_exact_survey_duplicate_entry(
    sess: dict,
    login: dict | None,
    history_id: str | None = None,
) -> dict | None:
    """Return a detached exact-match history entry, optionally restricted by id."""
    wanted_id = str(history_id or "").strip()
    if not _has_complete_survey_duplicate_fingerprint(sess):
        return None
    history = _load_history()
    for entry in history:
        if wanted_id and str(entry.get("id") or "") != wanted_id:
            continue
        try:
            if _is_exact_survey_duplicate(entry, sess, login):
                return deepcopy(entry)
        except (TypeError, ValueError):
            # A malformed/legacy history entry is never promoted to an exact match.
            continue
    return None


def find_exact_survey_duplicate_report(
    sess: dict,
    login: dict | None,
) -> dict | None:
    """Return duplicate-card metadata without exposing stored report bodies."""
    entry = find_exact_survey_duplicate_entry(sess, login)
    return _duplicate_report_summary(entry) if entry else None


def save_to_history(
    session_id: str,
    sess: dict,
    *,
    replace_report_versions: bool = False,
) -> dict | None:
    report_md = sess.get("report_md", "")
    if not report_md:
        return None

    def persist(history: list) -> dict:
        _ensure_history_report_numbers(history, save=False)
        old_entry = next(
            (h for h in history if h.get("id") == session_id),
            None,
        )
        if sess.get("mode") == "comment":
            title = sess.get("comment_report_title") or _comment_report_title(sess)
        else:
            title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
            title = title_m.group(1).strip() if title_m else "未命名报告"
        created_at = (
            old_entry.get("created_at")
            if old_entry
            else datetime.now().isoformat()
        )
        version_source = _history_version_source(
            sess,
            old_entry,
            title=title,
            created_at=created_at,
            replace_report_versions=replace_report_versions,
        )
        active_source = version_source or sess
        timing_source = (
            resolve_report_version(version_source)
            if version_source
            else sess
        )
        qa_messages = active_source.get("qa_messages")
        if qa_messages is None and old_entry:
            qa_messages = old_entry.get("qa_messages", [])
        owner = {
            "owner_key": sess.get("owner_key") or (old_entry or {}).get("owner_key", ""),
            "owner_email": sess.get("owner_email") or (old_entry or {}).get("owner_email", ""),
            "owner_open_id": sess.get("owner_open_id") or (old_entry or {}).get("owner_open_id", ""),
            "owner_name": sess.get("owner_name") or (old_entry or {}).get("owner_name", ""),
        }
        partial_rerun_source = build_partial_rerun_source(sess)
        if partial_rerun_source is None and old_entry:
            partial_rerun_source = deepcopy(old_entry.get("partial_rerun_source"))
        entry = {
            "id": session_id,
            "report_no": old_entry.get("report_no") if old_entry else _next_history_report_no(history),
            "filename": sess.get("filename", "unknown"),
            "title": active_source.get("title") or title,
            "created_at": created_at,
            "plan_approved_at": timing_source.get("plan_approved_at", ""),
            "report_completed_at": timing_source.get("report_completed_at", ""),
            "report_duration_seconds": timing_source.get(
                "report_duration_seconds"
            ),
            "report_md": active_source.get("report_md") or report_md,
            "plan": sess.get("plan"),
            "stats_md": sess.get("stats_md"),
            "qualitative_context": sess.get("qualitative_context", {}),
            "confirmed_columns": deepcopy(
                sess.get("confirmed_columns")
                if "confirmed_columns" in sess
                else (old_entry or {}).get("confirmed_columns")
            ),
            "source_type": sess.get("source_type")
            if "source_type" in sess
            else (old_entry or {}).get("source_type", ""),
            "file_sha256": sess.get("file_sha256")
            if "file_sha256" in sess
            else (old_entry or {}).get("file_sha256", ""),
            "questionnaire_sha256": sess.get("questionnaire_sha256")
            if "questionnaire_sha256" in sess
            else (old_entry or {}).get("questionnaire_sha256", ""),
            "questionnaire_used": bool(
                sess.get("questionnaire_used")
                if "questionnaire_used" in sess
                else (old_entry or {}).get("questionnaire_used", False)
            ),
            "qa_context_md": active_source.get("qa_context_md", ""),
            "analyst_conv_id": active_source.get("analyst_conv_id", ""),
            "analyst_app": active_source.get("analyst_app", ""),
            "report_writer_provider": active_source.get("report_writer_provider", ""),
            "report_writer_model": active_source.get("report_writer_model", ""),
            "comparison_validation": deepcopy(
                active_source.get("comparison_validation") or {}
            ),
            "qa_provider": active_source.get("qa_provider", "")
            if version_source
            else active_source.get("qa_provider")
            or (old_entry or {}).get("qa_provider", ""),
            "qa_model": active_source.get("qa_model", "")
            if version_source
            else active_source.get("qa_model")
            or (old_entry or {}).get("qa_model", ""),
            "qa_messages": qa_messages or [],
            "rows_fed": bool(sess.get("rows_fed", False)),
            "mode": sess.get("mode", ""),
            "row_count": max(0, len(sess.get("rows") or []) - 1)
            or (old_entry or {}).get("row_count", 0),
            **owner,
        }
        if partial_rerun_source:
            entry["partial_rerun_source"] = partial_rerun_source
        if version_source:
            entry.update({
                "report_versions": deepcopy(version_source["report_versions"]),
                "active_report_version": version_source["active_report_version"],
                "next_report_version": version_source["next_report_version"],
            })
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
        elif sess.get("mode") == "interview":
            entry.update({
                "interview_sheet_count": len(
                    (sess.get("interview_workbook") or {}).get("sheets") or []
                ),
                "interview_player_count": sess.get("interview_player_count", 0),
                "interview_module_count": sess.get("interview_module_count", 0),
                "interview_research_focus": sess.get("interview_research_focus", ""),
                "interview_models_used": sess.get("interview_models_used", {}),
                "interview_audit": sess.get("interview_audit", {}),
            })
        retained = [h for h in history if h.get("id") != session_id]
        retained.insert(0, entry)
        history[:] = _trim_history_for_owner(
            retained,
            owner.get("owner_key", ""),
        )
        return entry

    return mutate_history(persist)


def _copy_report_version_state(target: dict, source: dict) -> None:
    for field in (
        "report_versions",
        "active_report_version",
        "next_report_version",
        *_VERSION_MIRROR_FIELDS,
    ):
        if field in source:
            target[field] = deepcopy(source[field])


def append_exact_rerun_to_history(
    history_id: str,
    sess: dict,
    snapshot: dict,
    *,
    base_version: int,
    instruction: str,
    login: dict | None,
) -> tuple[dict, dict]:
    """Atomically append a rerun snapshot to its exact-match history card."""
    target_id = str(history_id or "").strip()

    def persist(history: list) -> tuple[dict, dict]:
        _ensure_history_report_numbers(history, save=False)
        entry = _find_history_for_login(history, target_id, login)
        if not entry:
            raise HTTPException(status_code=404, detail="原报告不存在或无权访问")
        if not _is_exact_survey_duplicate(entry, sess, login):
            raise HTTPException(
                status_code=409,
                detail="当前上传数据或确认信息已变化，请重新确认后再生成。",
            )
        if not isinstance(sess.get("plan"), dict) or sess.get("plan") != entry.get("plan"):
            raise HTTPException(
                status_code=409,
                detail="原报告的分析方案已变化，请重新确认后再生成。",
            )
        try:
            resolve_report_version(entry, base_version)
            committed = append_report_version(
                entry,
                snapshot,
                kind="regenerate",
                base_version=base_version,
                instruction=instruction,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        # These top-level fields describe the data used by the active snapshot.
        # The history id/report number/created_at stay untouched, so no new card
        # is created and concurrent QA updates already present on old versions survive.
        entry.update({
            "stats_md": sess.get("stats_md"),
            "qualitative_context": deepcopy(sess.get("qualitative_context", {})),
            "confirmed_columns": deepcopy(sess.get("confirmed_columns")),
            "source_type": sess.get("source_type", ""),
            "file_sha256": sess.get("file_sha256", ""),
            "questionnaire_sha256": sess.get("questionnaire_sha256", ""),
            "questionnaire_used": bool(sess.get("questionnaire_used")),
            "row_count": max(0, len(sess.get("rows") or []) - 1),
            "rows_fed": False,
        })
        partial_rerun_source = build_partial_rerun_source(sess)
        if partial_rerun_source:
            entry["partial_rerun_source"] = partial_rerun_source
        return deepcopy(entry), deepcopy(committed)

    return mutate_history(persist)


def append_partial_rerun_to_history(
    history_id: str,
    snapshot: dict,
    *,
    base_version: int,
    expected_plan_fingerprint: str,
    expected_source_fingerprint: str,
    instruction: str,
    login: dict | None,
) -> tuple[dict, dict]:
    """Atomically append a validated partial-rerun snapshot to one history card."""
    target_id = str(history_id or "").strip()

    def persist(history: list) -> tuple[dict, dict]:
        entry = _find_history_for_login(history, target_id, login)
        if not entry:
            raise HTTPException(status_code=404, detail="历史报告不存在或无权访问")
        source = entry.get("partial_rerun_source")
        if not isinstance(source, dict):
            raise HTTPException(status_code=409, detail="该报告缺少局部重做来源数据")
        if (
            not verify_partial_rerun_source(source)
            or build_plan_fingerprint(entry.get("plan") or {})
            != expected_plan_fingerprint
            or source.get("plan_fingerprint") != expected_plan_fingerprint
            or source.get("source_fingerprint") != expected_source_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="报告的数据或分析方案已变化，本次结果未保存。",
            )
        try:
            base = resolve_report_version(entry, base_version)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        artifacts = base.get("analysis_artifacts")
        if (
            not isinstance(artifacts, dict)
            or artifacts.get("plan_fingerprint") != expected_plan_fingerprint
            or artifacts.get("source_fingerprint") != expected_source_fingerprint
        ):
            raise HTTPException(
                status_code=409,
                detail="基础版本的局部分析产物已变化，本次结果未保存。",
            )
        try:
            committed = append_report_version(
                entry,
                snapshot,
                kind="regenerate",
                base_version=base_version,
                instruction=instruction,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return deepcopy(entry), deepcopy(committed)

    return mutate_history(persist)


def sync_exact_rerun_qa_to_history(
    history_id: str,
    sess: dict,
    version: int,
    *,
    login: dict | None,
) -> dict:
    """Persist one rerun-session QA snapshot without creating another history card."""
    target_id = str(history_id or "").strip()
    session_snapshot = resolve_report_version(sess, version)

    def persist(history: list) -> dict:
        entry = _find_history_for_login(history, target_id, login)
        if not entry:
            raise HTTPException(status_code=404, detail="原报告不存在或无权访问")
        if not _is_exact_survey_duplicate(entry, sess, login):
            raise HTTPException(status_code=409, detail="原报告与当前任务不再匹配")
        try:
            update_report_version(
                entry,
                version,
                qa_context_md=session_snapshot.get("qa_context_md", ""),
                qa_provider=session_snapshot.get("qa_provider", ""),
                qa_model=session_snapshot.get("qa_model", ""),
                qa_messages=deepcopy(session_snapshot.get("qa_messages") or []),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        entry["rows_fed"] = True
        return deepcopy(entry)

    return mutate_history(persist)


def delete_history_report_version(
    history_id: str,
    version: int,
    login: dict | None,
) -> dict:
    """Delete one owned history version atomically while preserving at least one."""
    target_id = str(history_id or "").strip()

    def persist(history: list) -> tuple[dict, dict]:
        _ensure_history_report_numbers(history, save=False)
        entry = _find_history_for_login(history, target_id, login)
        if not entry:
            raise HTTPException(status_code=404, detail="历史记录不存在")
        viewer_key = _owner_from_login(login).get("owner_key", "")
        if viewer_key and viewer_key != _history_owner_key(entry):
            raise HTTPException(status_code=404, detail="历史记录不存在")
        if not _supports_report_versions(entry):
            raise HTTPException(status_code=400, detail="该报告类型不支持版本管理")
        try:
            deleted = delete_report_version(entry, version)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not any(
            isinstance(snapshot.get("analysis_artifacts"), dict)
            for snapshot in normalize_report_versions(entry)
        ):
            entry.pop("partial_rerun_source", None)
        return deepcopy(entry), deepcopy(deleted)

    entry, deleted = mutate_history(persist)

    # Keep the original live session from reintroducing a deleted version later.
    try:
        live_session = get_session(target_id)
        if _history_owner_key(live_session) == _history_owner_key(entry):
            _copy_report_version_state(live_session, entry)
            save_session(target_id, live_session)
    except HTTPException:
        pass
    except OSError as exc:
        print(
            "[report-version-delete] WARN history committed but live session sync failed: "
            f"{type(exc).__name__}"
        )

    active = resolve_report_version(entry)
    summaries = report_version_summaries(entry)
    return {
        "ok": True,
        "id": target_id,
        "history_id": target_id,
        "deleted_version": deleted["version"],
        "report_md": active["report_md"],
        "title": active["title"],
        "report_versions": summaries,
        "versions": deepcopy(summaries),
        "active_report_version": active["version"],
        "active_version": active["version"],
        "version": active["version"],
        "selected_version": active["version"],
        "next_version": entry.get("next_report_version"),
        "version_count": len(summaries),
        "max_versions": MAX_REPORT_VERSIONS,
        "can_generate_version": False,
    }


def confirm_interview_audit_issue(
    hist_id: str,
    issue_index: int,
    login: dict | None,
) -> dict:
    """确认一条访谈审校提醒，并同步仍然存在的临时会话。"""
    def confirm(history: list) -> tuple[dict, dict]:
        _ensure_history_report_numbers(history, save=False)
        entry = _find_history_for_login(history, hist_id, login)
        if not entry or entry.get("mode") != "interview":
            raise HTTPException(status_code=404, detail="未找到这份访谈报告")

        audit = dict(entry.get("interview_audit") or {})
        issues = [
            dict(item) if isinstance(item, dict) else {}
            for item in (audit.get("issues") or [])
        ]
        if issue_index < 0 or issue_index >= len(issues):
            raise HTTPException(status_code=404, detail="未找到这条审校提醒")

        reviewer = (
            str((login or {}).get("email") or "").strip().lower()
            or str((login or {}).get("open_id") or "").strip()
            or str((login or {}).get("name") or "").strip()
        )
        issues[issue_index].update({
            "review_status": "confirmed",
            "reviewed_at": datetime.now().isoformat(timespec="seconds"),
            "reviewed_by": reviewer,
        })
        audit["issues"] = issues
        entry["interview_audit"] = audit
        return entry, audit

    entry, audit = mutate_history(confirm)

    try:
        sess = get_session(hist_id)
        if sess.get("mode") == "interview" and _visible_to_owner(sess, login):
            sess["interview_audit"] = audit
            save_session(hist_id, sess)
    except HTTPException:
        pass
    return entry


def save_annotate_to_history(sid: str, sess: dict, result_path: str, download_name: str) -> None:
    def persist(history: list) -> None:
        _ensure_history_report_numbers(history, save=False)
        old_entry = next((h for h in history if h.get("id") == sid), None)
        filename = sess.get("filename", "annotated")
        stem = (
            re.sub(r"\.(csv|xlsx|xls)$", "", filename, flags=re.IGNORECASE).strip()
            or "标注结果"
        )
        owner = {
            "owner_key": sess.get("owner_key") or (old_entry or {}).get("owner_key", ""),
            "owner_email": sess.get("owner_email") or (old_entry or {}).get("owner_email", ""),
            "owner_open_id": sess.get("owner_open_id") or (old_entry or {}).get("owner_open_id", ""),
            "owner_name": sess.get("owner_name") or (old_entry or {}).get("owner_name", ""),
        }
        tasks = sess.get("tasks", {}) or {}
        annotate_timing = {}
        for session_key, history_key in (
            ("quality_started_at", "annotate_quality_started_at"),
            ("quality_completed_at", "annotate_quality_completed_at"),
            ("quality_duration_seconds", "annotate_quality_duration_seconds"),
        ):
            if session_key in sess:
                annotate_timing[history_key] = sess.get(session_key)
            elif old_entry and history_key in old_entry:
                annotate_timing[history_key] = old_entry.get(history_key)
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
            **annotate_timing,
            **owner,
        }
        retained = [h for h in history if h.get("id") != sid]
        retained.insert(0, entry)
        history[:] = _trim_history_for_owner(
            retained,
            owner.get("owner_key", ""),
        )

    mutate_history(persist)


def _find_comment_duplicate_report(file_hash: str, login: dict | None) -> dict | None:
    if not file_hash:
        return None
    history = _load_history_with_report_numbers()
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

    class HistoryEntryNotFound(Exception):
        pass

    def rename_history(history: list) -> dict:
        _ensure_history_report_numbers(history, save=False)
        entry = _find_history_for_login(history, hist_id, login)
        if not entry:
            raise HistoryEntryNotFound
        _rename_report_source(entry, new_title)
        return entry

    try:
        entry = mutate_history(rename_history)
    except HistoryEntryNotFound:
        entry = None

    if entry:
        try:
            sess = get_session(hist_id)
            if sess.get("report_md") and _visible_to_owner(sess, login):
                _rename_report_source(sess, new_title)
                save_session(hist_id, sess)
        except HTTPException:
            pass  # session 已过期，只更新历史记录即可
    else:
        try:
            sess = get_session(hist_id)
            if sess.get("report_md") and _visible_to_owner(sess, login):
                rerun_history_id = str(
                    sess.get("rerun_target_history_id") or ""
                ).strip()
                if rerun_history_id and rerun_history_id != hist_id:
                    # A rerun session is only a temporary workspace. Rename the
                    # original card and mirror it locally; never archive this sid.
                    result = _update_history_title_by_id(
                        rerun_history_id,
                        new_title,
                        login,
                    )
                    _rename_report_source(sess, new_title)
                    save_session(hist_id, sess)
                    return result
                _assign_session_owner(sess, login)
                _rename_report_source(sess, new_title)
                save_session(hist_id, sess)
                entry = save_to_history(hist_id, sess)
        except HTTPException:
            entry = None
    if not entry:
        raise HTTPException(status_code=404, detail="未找到这份报告，请刷新历史记录后重试")

    return {
        "ok": True,
        "id": hist_id,
        "report_no": entry.get("report_no", ""),
        "title": new_title,
        "report_md": entry.get("report_md", ""),
    }
