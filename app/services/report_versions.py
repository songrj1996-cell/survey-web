"""Pure business rules for report snapshots stored on sessions or history entries."""

from copy import deepcopy
from datetime import datetime
import re

from app.core.config import MAX_REPORT_VERSIONS
from app.services.report_modes import MODE_SNAPSHOT_FIELDS, MODE_OBJECT_FIELDS, resolve_report_mode


_VERSION_KINDS = {"initial", "regenerate"}
_MIRROR_FIELDS = (
    *MODE_SNAPSHOT_FIELDS,
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
    "report_llm_usage",
    "report_style",
    "quick_report_diagnostics",
)
_TEXT_SNAPSHOT_FIELDS = tuple(
    field for field in _MIRROR_FIELDS
    if field not in {"qa_messages", "comparison_validation", "report_llm_usage", "report_style", "quick_report_diagnostics", *MODE_SNAPSHOT_FIELDS}
)
_OPTIONAL_OBJECT_SNAPSHOT_FIELDS = ("report_llm_usage", "quick_report_diagnostics", *MODE_OBJECT_FIELDS)
_SUMMARY_FIELDS = (
    "quick_completion_revision",
    "report_mode", "report_status",
    "version",
    "kind",
    "base_version",
    "instruction",
    "created_at",
    "title",
    "rerun_details",
    "report_style",
)
_IMMUTABLE_UPDATE_FIELDS = {
    "version",
    "kind",
    "base_version",
    "created_at",
    "report_versions",
    "active_report_version",
    "next_report_version",
}


def _require_source(source: dict) -> None:
    if not isinstance(source, dict):
        raise ValueError("报告版本来源必须是字典")


def _version_number(value, *, field_name: str = "version") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是正整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{field_name} 必须是正整数")
    if isinstance(value, str):
        value = value.strip()
        if value[:1].lower() == "v":
            value = value[1:]
    try:
        version = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是正整数") from exc
    if version < 1:
        raise ValueError(f"{field_name} 必须是正整数")
    return version


def _optional_version_number(value) -> int | None:
    if value is None or value == "":
        return None
    return _version_number(value)


def _report_title(report_md: str) -> str:
    match = re.search(r"^#\s+(.+?)$", report_md or "", re.MULTILINE)
    return match.group(1).strip() if match else ""


def _snapshot_from(
    snapshot: dict,
    *,
    fallback: dict | None = None,
    version=None,
    kind=None,
    base_version=None,
    instruction=None,
    created_at=None,
) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("报告版本快照必须是字典")

    fallback = fallback or {}
    result = deepcopy(snapshot)
    result["version"] = _version_number(
        snapshot.get("version") if version is None else version
    )

    resolved_kind = str(snapshot.get("kind") if kind is None else kind).strip().lower()
    if not resolved_kind:
        resolved_kind = "initial" if result["version"] == 1 else "regenerate"
    if resolved_kind not in _VERSION_KINDS:
        raise ValueError("报告版本类型必须是 initial 或 regenerate")
    result["kind"] = resolved_kind

    raw_base_version = snapshot.get("base_version") if base_version is None else base_version
    result["base_version"] = (
        None if resolved_kind == "initial" else _optional_version_number(raw_base_version)
    )
    result["instruction"] = str(
        (snapshot.get("instruction", "") if instruction is None else instruction)
        or ""
    ).strip()
    result["created_at"] = str(
        snapshot.get("created_at")
        if created_at is None and snapshot.get("created_at") is not None
        else created_at or fallback.get("created_at") or ""
    ).strip()

    for field in _TEXT_SNAPSHOT_FIELDS:
        value = snapshot[field] if field in snapshot else fallback.get(field, "")
        result[field] = str(value or "")

    qa_messages = (
        snapshot["qa_messages"]
        if "qa_messages" in snapshot
        else fallback.get("qa_messages", [])
    )
    if qa_messages is None:
        qa_messages = []
    if not isinstance(qa_messages, list):
        raise ValueError("qa_messages 必须是列表")
    result["qa_messages"] = deepcopy(qa_messages)

    comparison_validation = (
        snapshot["comparison_validation"]
        if "comparison_validation" in snapshot
        else fallback.get("comparison_validation", {})
    )
    if comparison_validation is None:
        comparison_validation = {}
    if not isinstance(comparison_validation, dict):
        raise ValueError("comparison_validation 必须是对象")
    result["comparison_validation"] = deepcopy(comparison_validation)

    for field in _OPTIONAL_OBJECT_SNAPSHOT_FIELDS:
        if field in snapshot:
            value = snapshot[field]
        elif field in fallback:
            value = fallback[field]
        else:
            result.pop(field, None)
            continue
        if value is None:
            result.pop(field, None)
            continue
        if not isinstance(value, dict):
            raise ValueError(f"{field} 必须是对象")
        result[field] = deepcopy(value)

    result["report_style"] = "quick" if snapshot.get("report_style", fallback.get("report_style")) == "quick" else "full"
    result["report_mode"] = snapshot.get("report_mode") or (
        "quick" if result["report_style"] == "quick" else
        "statistics" if fallback.get("report_mode") == "statistics" or fallback.get("mode") in {"crosstab", "quantitative"} else "insight"
    )
    result["report_status"] = "partial" if snapshot.get("report_status", fallback.get("report_status")) == "partial" else "complete"
    if result["report_style"] != "quick":
        result.pop("quick_report_diagnostics", None)

    if not result["title"]:
        result["title"] = _report_title(result["report_md"])
    return result


def normalize_report_versions(source: dict) -> list[dict]:
    """Return normalized snapshot copies without changing ``source``.

    A legacy source with only ``report_md`` is projected as V1 in memory. The
    projection is intentionally not written back by this read helper.
    """
    _require_source(source)
    raw_versions = source.get("report_versions")
    if raw_versions in (None, []):
        if not source.get("report_md"):
            return []
        return [
            _snapshot_from(
                {
                    "version": 1,
                    "kind": "initial",
                    "base_version": None,
                    "instruction": "",
                    "created_at": source.get("created_at", ""),
                },
                fallback=source,
            )
        ]
    if not isinstance(raw_versions, list):
        raise ValueError("report_versions 必须是列表")

    versions = [
        _snapshot_from(item, fallback={"created_at": source.get("created_at", ""), "mode": source.get("mode")})
        for item in raw_versions
    ]
    versions.sort(key=lambda item: item["version"])
    version_numbers = [item["version"] for item in versions]
    if len(version_numbers) != len(set(version_numbers)):
        raise ValueError("报告版本号不能重复")

    previous_version = None
    for item in versions:
        if item["kind"] == "regenerate" and item["base_version"] is None:
            item["base_version"] = previous_version
        previous_version = item["version"]
    return versions


def _active_version_number(source: dict, versions: list[dict]) -> int:
    available = {item["version"] for item in versions}
    raw_active = source.get("active_report_version")
    try:
        active = _optional_version_number(raw_active)
    except ValueError:
        active = None
    return active if active in available else max(available)


def _next_version_number(
    source: dict,
    versions: list[dict],
    *,
    minimum: int | None = None,
) -> int:
    floor = max((item["version"] for item in versions), default=0) + 1
    if minimum is not None:
        floor = max(floor, minimum)
    try:
        configured = _optional_version_number(source.get("next_report_version"))
    except ValueError:
        configured = None
    return max(floor, configured or 1)


def resolve_report_version(source: dict, version=None) -> dict:
    """Resolve an explicit version, or the active version when omitted."""
    versions = normalize_report_versions(source)
    if not versions:
        raise ValueError("报告暂无可用版本")
    target = (
        _active_version_number(source, versions)
        if version is None
        else _version_number(version)
    )
    for item in versions:
        if item["version"] == target:
            return deepcopy(item)
    raise ValueError(f"报告版本 V{target} 不存在")


def report_version_summaries(source: dict) -> list[dict]:
    """Return metadata safe for list/SSE responses without report bodies."""
    return [
        {
            field: deepcopy(snapshot[field])
            for field in _SUMMARY_FIELDS
            if field in snapshot and (field != "report_style" or snapshot[field] == "quick")
            and (field not in {"report_mode", "report_status"} or "input_snapshot" in snapshot or "quick_summary" in snapshot)
        }
        for snapshot in normalize_report_versions(source)
    ]


def _synced_state(
    source: dict,
    versions: list[dict],
    *,
    active_version: int,
    minimum_next: int | None = None,
) -> tuple[dict, dict]:
    active_snapshot = next(
        item for item in versions if item["version"] == active_version
    )
    state = {
        "report_versions": deepcopy(versions),
        "active_report_version": active_version,
        "next_report_version": _next_version_number(
            source,
            versions,
            minimum=minimum_next,
        ),
    }
    for field in _MIRROR_FIELDS:
        if field in active_snapshot:
            state[field] = deepcopy(active_snapshot[field])
    return state, deepcopy(active_snapshot)


def _commit_state(source: dict, state: dict) -> None:
    for key, value in state.items():
        source[key] = value
    for field in _OPTIONAL_OBJECT_SNAPSHOT_FIELDS:
        if field not in state:
            source.pop(field, None)


def sync_active_report_version(source: dict) -> dict:
    """Materialize normalized versions and mirror the active snapshot on top."""
    versions = normalize_report_versions(source)
    if not versions:
        raise ValueError("报告暂无可用版本")
    active_version = _active_version_number(source, versions)
    state, active_snapshot = _synced_state(
        source,
        versions,
        active_version=active_version,
    )
    _commit_state(source, state)
    return active_snapshot


def append_report_version(
    source: dict,
    snapshot: dict,
    *,
    kind: str | None = None,
    base_version=None,
    instruction: str | None = None,
    created_at: str | None = None,
) -> dict:
    """Append and activate one successful snapshot without pruning old ones."""
    _require_source(source)
    if not isinstance(snapshot, dict):
        raise ValueError("报告版本快照必须是字典")
    versions = normalize_report_versions(source)
    if len(versions) >= MAX_REPORT_VERSIONS:
        raise ValueError(
            f"报告版本已达上限（{MAX_REPORT_VERSIONS} 个），无法继续追加"
        )

    new_version = _next_version_number(source, versions)
    resolved_kind = str(
        kind or snapshot.get("kind") or ("initial" if not versions else "regenerate")
    ).strip().lower()
    if resolved_kind not in _VERSION_KINDS:
        raise ValueError("报告版本类型必须是 initial 或 regenerate")
    if resolved_kind == "initial" and versions:
        raise ValueError("已有报告版本时不能追加 initial 版本")

    resolved_base = base_version
    if resolved_base is None:
        resolved_base = snapshot.get("base_version")
    if resolved_kind == "regenerate" and resolved_base is None:
        if not versions:
            raise ValueError("regenerate 版本必须指定基础版本")
        resolved_base = _active_version_number(source, versions)
    if resolved_kind == "regenerate":
        resolved_base = _version_number(resolved_base, field_name="base_version")
        if resolved_base not in {item["version"] for item in versions}:
            raise ValueError(f"基础报告版本 V{resolved_base} 不存在")
    else:
        resolved_base = None

    resolved_created_at = (
        created_at
        or snapshot.get("created_at")
        or datetime.now().isoformat(timespec="seconds")
    )
    snapshot_fallback = source
    if versions:
        snapshot_fallback = {
            key: value
            for key, value in source.items()
            if key not in {"report_llm_usage", "quick_report_diagnostics", "report_style", *MODE_SNAPSHOT_FIELDS}
        }
    new_snapshot = _snapshot_from(
        snapshot,
        fallback=snapshot_fallback,
        version=new_version,
        kind=resolved_kind,
        base_version=resolved_base,
        instruction=(
            snapshot.get("instruction", "") if instruction is None else instruction
        ),
        created_at=resolved_created_at,
    )
    if not new_snapshot["report_md"].strip():
        raise ValueError("报告版本正文不能为空")

    new_versions = [*versions, new_snapshot]
    state, committed_snapshot = _synced_state(
        source,
        new_versions,
        active_version=new_version,
        minimum_next=new_version + 1,
    )
    _commit_state(source, state)
    return committed_snapshot


def validate_quick_completion_base(base: dict) -> None:
    """Require every successful question and frozen answer/profile before retry."""
    frozen = base.get("input_snapshot") or {}
    questions = (base.get("quick_summary") or {}).get("questions") or []
    originals = frozen.get("source_questions") or []
    if resolve_report_mode(base) != "quick" or base.get("report_status") != "partial" or not questions:
        raise ValueError("所选版本没有可补全的失败题目")
    if [q.get("question_key") for q in questions] != [q.get("question_key") for q in originals]:
        raise ValueError("原版本题目资料不完整，无法安全补全")
    successful = {q["question_key"]: q for q in questions if q.get("status") == "complete"}
    cached = (base.get("quick_checkpoint") or {}).get("questions") or []
    if len(cached) != len(successful) or {q.get("question_key"): q for q in cached} != successful:
        raise ValueError("原版本成功题目缓存不完整，已停止补全，不会重跑成功题目")
    for question, original in zip(questions, originals):
        if question.get("sources") != original.get("sources"):
            raise ValueError("原版本回答与画像不匹配，无法安全补全")
        if any(not isinstance(row.get("profile"), dict) for row in original.get("sources", [])):
            raise ValueError("原版本缺少完整画像资料，已停止补全")


def _merge_completion_usage(previous: dict, current: dict) -> dict:
    result = deepcopy(current or previous or {})
    counts = ("input_tokens", "output_tokens", "total_tokens", "call_count",
              "usage_reported_call_count", "usage_missing_call_count")
    def merge(left, right):
        value = deepcopy(right)
        for key in counts:
            value[key] = int(left.get(key) or 0) + int(right.get(key) or 0)
        for key in ("models_used", "fallback_models_used"):
            value[key] = list(dict.fromkeys([*(left.get(key) or []), *(right.get(key) or [])]))
        value.update(active_calls=0, active_models={})
        return value
    if previous and current:
        result["totals"] = merge(previous.get("totals", {}), current.get("totals", {}))
        left, right = previous.get("phases", {}), current.get("phases", {})
        result["phases"] = {key: merge(left.get(key, {}), right.get(key, {})) for key in left.keys() | right.keys()}
    return result


def complete_quick_report_version(source: dict, snapshot: dict, *, expected_base: dict) -> dict:
    """Patch only unfinished questions; reject stale results before mutating source."""
    from app.services.report_quick_mode import render_quick_report, fill_question_evidence
    from app.services.report_modes import quick_qa_context

    current = resolve_report_version(source, expected_base["version"])
    validate_quick_completion_base(current)
    for field in ("input_snapshot", "quick_summary", "quick_checkpoint", "report_md", "quick_completion_revision"):
        if current.get(field) != expected_base.get(field):
            raise ValueError("该版本已更新，本次补全结果未覆盖新进度，请刷新报告")
    result = deepcopy(snapshot.get("quick_summary") or {})
    old_questions = current["quick_summary"]["questions"]
    new_questions = result.get("questions") or []
    if snapshot.get("input_snapshot") != current["input_snapshot"]:
        raise ValueError("补全结果的原始回答或画像发生变化，未保存")
    if [q.get("question_key") for q in new_questions] != [q.get("question_key") for q in old_questions]:
        raise ValueError("补全结果的题目范围发生变化，未保存")
    for old, new in zip(old_questions, new_questions):
        if old.get("status") == "complete" and old != new:
            raise ValueError("补全结果改动了成功题目，未保存")
        for field in ("question", "source_order", "sources"):
            if old.get(field) != new.get(field):
                raise ValueError("补全结果的回答或画像对应关系发生变化，未保存")
        for finding in new.get("findings") or []:
            try:
                expected = fill_question_evidence([finding], new["sources"])[0]["evidence"]
            except (KeyError, TypeError):
                raise ValueError("补全结果的引用不属于原题回答，未保存") from None
            if finding.get("evidence") != expected:
                raise ValueError("补全结果的引用原文或画像发生变化，未保存")
    if result.get("objective_stats") != current["quick_summary"].get("objective_stats"):
        raise ValueError("补全结果改动了原客观统计，未保存")
    cached = (snapshot.get("quick_checkpoint") or {}).get("questions") or []
    successful = {q["question_key"]: q for q in new_questions if q.get("status") == "complete"}
    if len(cached) != len(successful) or {q.get("question_key"): q for q in cached} != successful:
        raise ValueError("补全结果的成功题目缓存不完整，未保存")
    status = "complete" if all(q.get("status") == "complete" for q in new_questions) else "partial"
    markdown = render_quick_report(result, title=current["title"])
    if snapshot.get("report_md") != markdown or snapshot.get("report_status") != status:
        raise ValueError("补全正文或状态与题目结果不一致，未保存")
    attempts = deepcopy((current.get("quick_report_diagnostics") or {}).get("completion_attempts") or [])
    attempts.append({"completed_at": snapshot.get("report_completed_at"),
                     "duration_seconds": snapshot.get("report_duration_seconds"),
                     "report_status": status, "usage": deepcopy(snapshot.get("report_llm_usage") or {}),
                     "diagnostics": deepcopy(snapshot.get("quick_report_diagnostics") or {})})
    fields = {key: deepcopy(snapshot[key]) for key in ("quick_summary", "quick_checkpoint", "report_completed_at") if key in snapshot}
    fields.update(report_md=markdown, report_status=status,
                  qa_context_md=quick_qa_context(markdown, current["input_snapshot"]),
                  quick_completion_revision=int(current.get("quick_completion_revision") or 0) + 1,
                  report_duration_seconds=float(current.get("report_duration_seconds") or 0) + float(snapshot.get("report_duration_seconds") or 0),
                  report_llm_usage=_merge_completion_usage(current.get("report_llm_usage"), snapshot.get("report_llm_usage")),
                  quick_report_diagnostics={**deepcopy(snapshot.get("quick_report_diagnostics") or {}), "completion_attempts": attempts})
    return update_report_version(source, current["version"], **fields)


def update_report_version(source: dict, version, **fields) -> dict:
    """Update one snapshot, materializing a legacy V1 only on this write path."""
    _require_source(source)
    target = _version_number(version)
    immutable_fields = _IMMUTABLE_UPDATE_FIELDS.intersection(fields)
    if immutable_fields:
        names = ", ".join(sorted(immutable_fields))
        raise ValueError(f"报告版本不可修改这些字段：{names}")

    versions = normalize_report_versions(source)
    current = next(
        (item for item in versions if item["version"] == target),
        None,
    )
    if current is None:
        raise ValueError(f"报告版本 V{target} 不存在")

    candidate = deepcopy(current)
    candidate.update(deepcopy(fields))
    updated = _snapshot_from(candidate)
    if not updated["report_md"].strip():
        raise ValueError("报告版本正文不能为空")

    updated_versions = [
        updated if item["version"] == target else item
        for item in versions
    ]
    active_version = _active_version_number(source, versions)
    state, _ = _synced_state(
        source,
        updated_versions,
        active_version=active_version,
    )
    _commit_state(source, state)
    return deepcopy(updated)


def delete_report_version(source: dict, version) -> dict:
    """Delete one snapshot; the last remaining snapshot is protected."""
    _require_source(source)
    target = _version_number(version)
    versions = normalize_report_versions(source)
    deleted = next(
        (item for item in versions if item["version"] == target),
        None,
    )
    if deleted is None:
        raise ValueError(f"报告版本 V{target} 不存在")
    if len(versions) == 1:
        raise ValueError("不能删除最后一个报告版本")

    old_active = _active_version_number(source, versions)
    remaining = [item for item in versions if item["version"] != target]
    active_version = (
        max(item["version"] for item in remaining)
        if old_active == target
        else old_active
    )
    minimum_next = max(item["version"] for item in versions) + 1
    state, _ = _synced_state(
        source,
        remaining,
        active_version=active_version,
        minimum_next=minimum_next,
    )
    _commit_state(source, state)
    return deepcopy(deleted)
