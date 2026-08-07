"""同问卷分析预设的业务规则。

预设只复用业务背景、最终分析主线和完整的方案修订文本。答卷、统计、
报告正文和方案 parts 均不进入持久化记录。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
import uuid
from typing import Any

from app.storage.analysis_presets import (
    load_analysis_presets,
    mutate_analysis_presets,
)


ANALYSIS_PRESET_FINGERPRINT_VERSION = 1
_FINGERPRINT_PREFIX = f"v{ANALYSIS_PRESET_FINGERPRINT_VERSION}:"
_BUSINESS_CONTEXT_FIELDS = (
    "problem",
    "background",
    "target_users",
    "key_concerns",
    "report_usage",
    "analysis_approach",
)
_ANALYSIS_FOCUS_STRING_FIELDS = (
    "core_question",
    "report_organization",
    "evidence_role",
)
_ANALYSIS_FOCUS_LIST_FIELDS = (
    "supporting_analyses",
    "expected_deliverables",
    "avoid_structures",
)
_ANALYSIS_FOCUS_FIELDS = frozenset(
    (*_ANALYSIS_FOCUS_STRING_FIELDS, *_ANALYSIS_FOCUS_LIST_FIELDS)
)
_MATRIX_ROLES = frozenset({"matrix_scale", "matrix_single", "matrix_multi"})
_SCALE_ROLES = frozenset({"scale", "matrix_scale"})
_EXCLUDED_ROLE_TOKENS = frozenset({"id", "mlbbid", "ignore"})
_STANDARD_MODES = frozenset({"", "qualitative", "standard", "survey"})


def _normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", normalized).strip()


def _normalized_role(value: Any) -> str:
    return _normalize_text(value).casefold()


def _is_excluded_role(role: str) -> bool:
    token = re.sub(r"[\s_-]+", "", role)
    return token in _EXCLUDED_ROLE_TOKENS


def _column_indexes(column: dict[str, Any]) -> list[int]:
    raw_indexes = column.get("column_indexes")
    if not isinstance(raw_indexes, list) or not raw_indexes:
        raw_indexes = [column.get("index")]
    return [index for index in raw_indexes if type(index) is int and index >= 0]


def _normalized_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_normalize_text(item) for item in value]


def _fingerprint_questions(sess: dict[str, Any]) -> list[dict[str, Any]]:
    confirmed_columns = sess.get("confirmed_columns")
    rows = sess.get("rows")
    if not isinstance(confirmed_columns, list) or not confirmed_columns:
        return []
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], (list, tuple)):
        return []

    headers = rows[0]
    questionnaire_authoritative = sess.get("column_provider") == "questionnaire"
    questions: list[dict[str, Any]] = []
    for column in confirmed_columns:
        if not isinstance(column, dict):
            continue
        role = _normalized_role(column.get("role") or column.get("confirmed_type"))
        if _is_excluded_role(role):
            continue

        original_headers = [
            _normalize_text(headers[index])
            for index in _column_indexes(column)
            if index < len(headers)
        ]
        if not original_headers:
            continue

        question: dict[str, Any] = {
            "role": role,
            "original_headers": original_headers,
        }
        if role in _MATRIX_ROLES:
            matrix_rows = column.get("rows_original")
            if not isinstance(matrix_rows, list):
                matrix_rows = column.get("rows")
            question["matrix_rows"] = _normalized_text_list(matrix_rows)
        if role in _SCALE_ROLES:
            question["scale_range"] = [
                _normalize_text(column.get("scale_min")),
                _normalize_text(column.get("scale_max")),
            ]
        if questionnaire_authoritative:
            options = column.get("options_original")
            if not isinstance(options, list):
                options = column.get("options")
            if isinstance(options, list):
                question["authoritative_options"] = _normalized_text_list(options)
        questions.append(question)
    return questions


def build_analysis_preset_fingerprint(sess: dict[str, Any]) -> str | None:
    """构造只由已确认问卷结构决定的版本化 SHA-256 指纹。"""
    if not isinstance(sess, dict):
        return None
    questions = _fingerprint_questions(sess)
    if not questions:
        return None
    canonical = {
        "fingerprint_version": ANALYSIS_PRESET_FINGERPRINT_VERSION,
        "questions": questions,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _FINGERPRINT_PREFIX + hashlib.sha256(encoded).hexdigest()


def _owner_key_from_login(login: dict[str, Any] | None) -> str:
    if not isinstance(login, dict):
        return ""
    email = str(login.get("email") or "").strip().lower()
    if email:
        return f"email:{email}"
    open_id = str(login.get("open_id") or "").strip()
    if open_id:
        return f"open_id:{open_id}"
    return ""


def _is_eligible_standard_session(sess: dict[str, Any], eligible: bool) -> bool:
    if not eligible or not isinstance(sess, dict):
        return False
    mode = _normalized_role(sess.get("mode"))
    analysis_mode = _normalized_role(sess.get("analysis_mode"))
    return mode in _STANDARD_MODES and analysis_mode != "quantitative"


def _authorized_owner_key(
    sess: dict[str, Any],
    login: dict[str, Any] | None,
    eligible: bool,
) -> str:
    if not _is_eligible_standard_session(sess, eligible):
        return ""
    owner_key = _owner_key_from_login(login)
    if not owner_key:
        return ""
    session_owner_key = str(sess.get("owner_key") or "").strip()
    return owner_key if session_owner_key == owner_key else ""


def _business_context(value: Any, *, strict: bool) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None if strict else {field: "" for field in _BUSINESS_CONTEXT_FIELDS}
    context: dict[str, str] = {}
    for field in _BUSINESS_CONTEXT_FIELDS:
        item = value.get(field, "")
        if strict and not isinstance(item, str):
            return None
        context[field] = item.strip() if isinstance(item, str) else ""
    return context


def _analysis_focus(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != _ANALYSIS_FOCUS_FIELDS:
        return None
    focus: dict[str, Any] = {}
    for field in _ANALYSIS_FOCUS_STRING_FIELDS:
        item = value.get(field)
        if not isinstance(item, str) or not item.strip():
            return None
        focus[field] = item.strip()
    for field in _ANALYSIS_FOCUS_LIST_FIELDS:
        items = value.get(field)
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            return None
        if field == "expected_deliverables" and not items:
            return None
        focus[field] = [item.strip() for item in items]
    return focus


def _revision_texts(value: Any, *, missing_ok: bool) -> list[str] | None:
    if value is None and missing_ok:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def _preset_for_use(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    preset_id = str(value.get("id") or "").strip()
    owner_key = str(value.get("owner_key") or "").strip()
    fingerprint = str(value.get("fingerprint") or "").strip()
    fingerprint_version = value.get("fingerprint_version")
    context = _business_context(value.get("context"), strict=True)
    raw_focus = value.get("analysis_focus")
    focus = None if raw_focus is None else _analysis_focus(raw_focus)
    revisions = _revision_texts(value.get("plan_revision_texts"), missing_ok=False)
    if not preset_id or not owner_key or not fingerprint or context is None:
        return None
    if (
        type(fingerprint_version) is not int
        or fingerprint_version != ANALYSIS_PRESET_FINGERPRINT_VERSION
        or re.fullmatch(rf"{re.escape(_FINGERPRINT_PREFIX)}[0-9a-f]{{64}}", fingerprint)
        is None
    ):
        return None
    if (raw_focus is not None and focus is None) or revisions is None:
        return None
    return {
        "id": preset_id,
        "owner_key": owner_key,
        "fingerprint": fingerprint,
        "fingerprint_version": ANALYSIS_PRESET_FINGERPRINT_VERSION,
        "context": context,
        "analysis_focus": focus,
        "plan_revision_texts": revisions,
        "created_at": str(value.get("created_at") or "").strip(),
        "updated_at": str(value.get("updated_at") or "").strip(),
    }


def _matching_preset(
    presets: Any,
    *,
    owner_key: str,
    fingerprint: str,
    preset_id: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(presets, list):
        return None
    for value in reversed(presets):
        preset = _preset_for_use(value)
        if preset is None:
            continue
        if preset["owner_key"] != owner_key or preset["fingerprint"] != fingerprint:
            continue
        if preset_id is not None and preset["id"] != preset_id:
            continue
        return preset
    return None


def get_analysis_preset_offer(
    sess: dict[str, Any],
    login: dict[str, Any] | None,
    eligible: bool = True,
) -> dict[str, Any] | None:
    """返回当前登录人同问卷的预设；匿名或不合资格时不读取存储。"""
    owner_key = _authorized_owner_key(sess, login, eligible)
    if not owner_key:
        return None
    fingerprint = build_analysis_preset_fingerprint(sess)
    if not fingerprint:
        return None
    document = load_analysis_presets()
    preset = _matching_preset(
        document["presets"],
        owner_key=owner_key,
        fingerprint=fingerprint,
    )
    return deepcopy(preset) if preset is not None else None


def apply_analysis_preset(
    sess: dict[str, Any],
    login: dict[str, Any] | None,
    preset_id: str,
    eligible: bool = True,
) -> dict[str, Any] | None:
    """重新校验 owner 与当前问卷指纹后，将预设安全合并进 session。

    当前业务背景中的非空值优先；历史修订排在本次任务已有修订之前并按
    原文去重。调用方负责把已修改的 session 持久化，并可使用返回副本填充 UI。
    """
    owner_key = _authorized_owner_key(sess, login, eligible)
    selected_id = str(preset_id or "").strip()
    if not owner_key or not selected_id:
        return None
    fingerprint = build_analysis_preset_fingerprint(sess)
    if not fingerprint:
        return None
    document = load_analysis_presets()
    preset = _matching_preset(
        document["presets"],
        owner_key=owner_key,
        fingerprint=fingerprint,
        preset_id=selected_id,
    )
    if preset is None:
        return None

    current_revision_source = (
        sess.get("current_plan_revision_texts")
        if "current_plan_revision_texts" in sess
        else sess.get("plan_revision_texts")
    )
    current_revisions = _revision_texts(current_revision_source, missing_ok=True)
    if current_revisions is None:
        return None
    merged_revisions: list[str] = []
    seen_revisions: set[str] = set()
    for text in [*preset["plan_revision_texts"], *current_revisions]:
        if text in seen_revisions:
            continue
        seen_revisions.add(text)
        merged_revisions.append(text)

    current_context = sess.get("qualitative_context")
    merged_context = dict(current_context) if isinstance(current_context, dict) else {}
    for field in _BUSINESS_CONTEXT_FIELDS:
        current_value = merged_context.get(field)
        if not isinstance(current_value, str) or not current_value.strip():
            merged_context[field] = preset["context"][field]

    sess["qualitative_context"] = merged_context
    sess["applied_analysis_preset_id"] = preset["id"]
    sess["applied_analysis_preset_fingerprint"] = preset["fingerprint"]
    sess["analysis_preference_fingerprint"] = preset["fingerprint"]
    if preset["analysis_focus"] is None:
        sess.pop("preset_analysis_focus", None)
    else:
        sess["preset_analysis_focus"] = deepcopy(preset["analysis_focus"])
    sess["preset_plan_revision_texts"] = list(preset["plan_revision_texts"])
    sess["current_plan_revision_texts"] = list(current_revisions)
    sess["plan_revision_texts"] = merged_revisions
    return deepcopy(preset)


def save_analysis_preset(
    sess: dict[str, Any],
    login: dict[str, Any] | None,
    eligible: bool = True,
) -> dict[str, Any] | None:
    """按 owner + 问卷指纹 upsert 当前 session 的最终分析偏好。"""
    owner_key = _authorized_owner_key(sess, login, eligible)
    if not owner_key:
        return None
    fingerprint = build_analysis_preset_fingerprint(sess)
    if not fingerprint:
        return None

    context = _business_context(sess.get("qualitative_context"), strict=False)
    plan = sess.get("plan")
    raw_focus = plan.get("analysis_focus") if isinstance(plan, dict) else None
    focus = None if raw_focus is None else _analysis_focus(raw_focus)
    revisions = _revision_texts(sess.get("plan_revision_texts"), missing_ok=True)
    if (
        context is None
        or (raw_focus is not None and focus is None)
        or revisions is None
    ):
        return None
    if not any(context.values()) and focus is None and not revisions:
        return None

    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    saved: dict[str, Any] = {}

    def upsert(presets: list[dict[str, Any]]) -> None:
        nonlocal saved
        matching_indexes = [
            index
            for index, item in enumerate(presets)
            if str(item.get("owner_key") or "").strip() == owner_key
            and str(item.get("fingerprint") or "").strip() == fingerprint
        ]
        existing = presets[matching_indexes[0]] if matching_indexes else {}
        preset_id = str(existing.get("id") or "").strip() or str(uuid.uuid4())
        created_at = str(existing.get("created_at") or "").strip() or now
        saved = {
            "id": preset_id,
            "owner_key": owner_key,
            "fingerprint": fingerprint,
            "fingerprint_version": ANALYSIS_PRESET_FINGERPRINT_VERSION,
            "context": deepcopy(context),
            "analysis_focus": deepcopy(focus),
            "plan_revision_texts": list(revisions),
            "created_at": created_at,
            "updated_at": now,
        }
        if matching_indexes:
            presets[matching_indexes[0]] = deepcopy(saved)
            for index in reversed(matching_indexes[1:]):
                del presets[index]
        else:
            presets.append(deepcopy(saved))

    mutate_analysis_presets(upsert)
    return deepcopy(saved)
