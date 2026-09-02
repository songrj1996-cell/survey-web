"""Pure business rules for report snapshots stored on sessions or history entries."""

from copy import deepcopy
from datetime import datetime
import re

from app.core.config import MAX_REPORT_VERSIONS


_VERSION_KINDS = {"initial", "regenerate"}
_MIRROR_FIELDS = (
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
_TEXT_SNAPSHOT_FIELDS = tuple(
    field for field in _MIRROR_FIELDS
    if field not in {"qa_messages", "comparison_validation"}
)
_SUMMARY_FIELDS = (
    "version",
    "kind",
    "base_version",
    "instruction",
    "created_at",
    "title",
    "rerun_details",
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
        _snapshot_from(item, fallback={"created_at": source.get("created_at", "")})
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
            if field in snapshot
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
        state[field] = deepcopy(active_snapshot[field])
    return state, deepcopy(active_snapshot)


def _commit_state(source: dict, state: dict) -> None:
    for key, value in state.items():
        source[key] = value


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
    new_snapshot = _snapshot_from(
        snapshot,
        fallback=source,
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
