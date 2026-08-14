"""访谈报告 V2 批次 2：Sheet 分组与玩家绑定编排。"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.core.interview_v2_mapping import (
    InterviewV2MappingError,
    build_group_proposals,
    normalize_and_validate_mapping,
)
from app.core.security import _owner_from_login, _visible_to_owner
from app.services.interview_v2_import_service import (
    InterviewV2ImportError,
    get_interview_import,
)
from app.storage import interview_v2_store as store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _resource_error() -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=404,
        code="INTERVIEW_IMPORT_NOT_FOUND",
        message="导入记录不存在。",
        suggested_action="refresh_import_list",
    )


def _load_owned_import(
    import_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        item = store.load_interview_import(import_id)
    except ValueError as exc:
        raise InterviewV2ImportError(
            status_code=400,
            code="RESOURCE_ID_INVALID",
            message="导入记录 ID 格式无效。",
            suggested_action="check_resource_id",
        ) from exc
    except OSError as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_INPUT_UNAVAILABLE",
            message="分组所需的导入记录不可用，请稍后重试。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    if item is None or not _visible_to_owner(item, login):
        raise _resource_error()
    return item


def _load_owned_inputs(
    import_id: str, login: dict[str, Any] | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    item = _load_owned_import(import_id, login)

    try:
        bundle = store.load_mapping_input_bundle(import_id)
    except OSError as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_INPUT_UNAVAILABLE",
            message="分组所需的工作簿快照不可用，请重新上传。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    except ValueError as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_INPUT_UNAVAILABLE",
            message="分组所需的工作簿快照不可用，请重新上传。",
            suggested_action="restart_upload",
        ) from exc
    bundled_item = bundle.get("interview_import") if bundle else None
    if bundled_item is None:
        raise _resource_error()
    if bundled_item != item:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_INPUT_UNAVAILABLE",
            message="分组所需的导入记录校验失败，请重新上传。",
            suggested_action="restart_upload",
        )
    return item, deepcopy(bundle["physical_snapshot"])


def _persistence_error() -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=500,
        code="MAPPING_PERSISTENCE_FAILED",
        message="分组版本状态校验失败，请稍后重试。",
        retryable=True,
        suggested_action="retry_mapping_request",
    )


def _state(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        state = store.load_mapping_state(str(item.get("project_id") or ""))
    except (ValueError, OSError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_PERSISTENCE_FAILED",
            message="分组状态读取失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    if state is None:
        return None
    try:
        state_payload_sha256 = str(state.get("state_payload_sha256") or "")
        if (
            not re.fullmatch(r"[0-9a-f]{64}", state_payload_sha256)
            or store.mapping_state_payload_sha256(state) != state_payload_sha256
        ):
            raise ValueError("mapping state payload digest mismatch")
    except (TypeError, ValueError) as exc:
        raise _persistence_error() from exc
    try:
        raw_revision_number = state.get("current_revision_number")
        if isinstance(raw_revision_number, bool):
            raise ValueError("boolean revision number")
        revision_number = int(raw_revision_number or 0)
    except (TypeError, ValueError) as exc:
        raise _persistence_error() from exc
    history = state.get("revision_history")
    confirmations = state.get("confirmation_events")
    status = state.get("effective_status")
    if (
        state.get("project_id") != item.get("project_id")
        or state.get("import_id") != item.get("import_id")
        or revision_number < 0
        or status
        not in {"GROUP_CONFIRMATION_REQUIRED", "GROUP_MAPPING_CONFIRMED"}
        or not isinstance(history, list)
        or not isinstance(confirmations, list)
        or len(history) != revision_number
    ):
        raise _persistence_error()

    history_by_id: dict[str, dict[str, Any]] = {}
    for expected_number, entry in enumerate(history, start=1):
        if not isinstance(entry, dict):
            raise _persistence_error()
        entry_id = str(entry.get("mapping_revision_id") or "")
        entry_mapping_sha = str(entry.get("mapping_sha256") or "")
        entry_payload_sha = str(entry.get("revision_payload_sha256") or "")
        change_kind = str(entry.get("change_kind") or "")
        restored_from_id = entry.get("restored_from_mapping_revision_id")
        restored_from_number = entry.get("restored_from_revision_number")
        is_restore = change_kind in {"undo", "redo", "restore"}
        if (
            isinstance(entry.get("revision_number"), bool)
            or entry.get("revision_number") != expected_number
            or not re.fullmatch(r"mapping_[0-9a-f]{32}", entry_id)
            or not re.fullmatch(r"[0-9a-f]{64}", entry_mapping_sha)
            or not re.fullmatch(r"[0-9a-f]{64}", entry_payload_sha)
            or entry_id in history_by_id
            or change_kind
            not in {"manual_edit", "undo", "redo", "restore"}
            or (
                is_restore
                and (
                    not isinstance(restored_from_id, str)
                    or not re.fullmatch(
                        r"mapping_[0-9a-f]{32}", restored_from_id
                    )
                    or isinstance(restored_from_number, bool)
                    or not isinstance(restored_from_number, int)
                    or restored_from_number < 1
                    or restored_from_number >= expected_number
                    or history_by_id.get(restored_from_id, {}).get(
                        "revision_number"
                    )
                    != restored_from_number
                )
            )
            or (
                not is_restore
                and (
                    restored_from_id is not None
                    or restored_from_number is not None
                )
            )
        ):
            raise _persistence_error()
        history_by_id[entry_id] = entry

    current_revision_id = str(state.get("current_mapping_revision_id") or "")
    current_mapping_sha = str(state.get("current_mapping_sha256") or "")
    current_payload_sha = str(
        state.get("current_revision_payload_sha256") or ""
    )
    if revision_number == 0:
        if current_revision_id or current_mapping_sha or current_payload_sha:
            raise _persistence_error()
    else:
        head = history[-1]
        if (
            current_revision_id != head.get("mapping_revision_id")
            or current_mapping_sha != head.get("mapping_sha256")
            or current_payload_sha != head.get("revision_payload_sha256")
        ):
            raise _persistence_error()

    confirmed_current = False
    seen_confirmations: set[str] = set()
    for event in confirmations:
        if not isinstance(event, dict):
            raise _persistence_error()
        event_id = str(event.get("mapping_revision_id") or "")
        referenced = history_by_id.get(event_id)
        if (
            referenced is None
            or event_id in seen_confirmations
            or isinstance(event.get("revision_number"), bool)
            or event.get("revision_number") != referenced.get("revision_number")
            or event.get("mapping_sha256") != referenced.get("mapping_sha256")
        ):
            raise _persistence_error()
        seen_confirmations.add(event_id)
        if event_id == current_revision_id:
            confirmed_current = True

    status_is_confirmed = status == "GROUP_MAPPING_CONFIRMED"
    if confirmed_current != status_is_confirmed:
        raise _persistence_error()
    if status_is_confirmed and (
        state.get("confirmed_mapping_revision_id") != current_revision_id
        or state.get("confirmed_mapping_sha256") != current_mapping_sha
        or state.get("confirmed_mapping_revision_number") != revision_number
    ):
        raise _persistence_error()
    if not status_is_confirmed and any(
        state.get(key) is not None
        for key in (
            "confirmed_mapping_revision_id",
            "confirmed_mapping_sha256",
            "confirmed_mapping_revision_number",
        )
    ):
        raise _persistence_error()
    return state


def _current_metadata(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    return {
        "current_revision_number": int(state.get("current_revision_number") or 0),
        "current_mapping_revision_id": state.get("current_mapping_revision_id"),
        "current_mapping_sha256": state.get("current_mapping_sha256"),
    }


def _conflict(state: dict[str, Any] | None) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=409,
        code="REVISION_CONFLICT",
        message="分组映射已被更新，请刷新后合并更改。",
        retryable=False,
        suggested_action="refresh_group_mapping",
        context=_current_metadata(state),
    )


def _canonical_sha(mapping: dict[str, Any]) -> str:
    encoded = json.dumps(
        mapping,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _revision_id(
    project_id: str,
    import_id: str,
    revision_number: int,
    mapping_sha256: str,
) -> str:
    identity = (
        f"{project_id}\0{import_id}\0{revision_number}\0{mapping_sha256}"
    ).encode("utf-8")
    return f"mapping_{hashlib.sha256(identity).hexdigest()[:32]}"


def _public_value(value: Any) -> Any:
    """Recursively remove storage and evidence payload fields from responses."""

    blocked = {
        "cells",
        "source_content",
        "storage_path",
        "path",
        "owner_key",
        "owner_email",
        "owner_open_id",
        "owner_name",
        "created_by",
        "confirmed_by",
        "raw_value",
        "normalized_text",
        "display_value",
        "formula_text",
        "cached_value",
        "value_sha256",
        "filename",
        "original_filename",
        "source_filename",
        "source_file",
        "source_path",
        "source_name",
        "file",
    }
    if isinstance(value, dict):
        return {
            str(key): _public_value(item)
            for key, item in value.items()
            if str(key) not in blocked
        }
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    return deepcopy(value)


def _history(state: dict[str, Any] | None) -> list[dict[str, Any]]:
    state = state or {}
    confirmations = {
        str(event.get("mapping_revision_id") or ""): event
        for event in state.get("confirmation_events") or []
        if isinstance(event, dict)
    }
    result: list[dict[str, Any]] = []
    for entry in state.get("revision_history") or []:
        if not isinstance(entry, dict):
            continue
        public = {
            "revision_number": int(entry.get("revision_number") or 0),
            "mapping_revision_id": entry.get("mapping_revision_id"),
            "mapping_sha256": entry.get("mapping_sha256"),
            "change_kind": str(entry.get("change_kind") or "manual_edit"),
            "restored_from_mapping_revision_id": entry.get(
                "restored_from_mapping_revision_id"
            ),
            "restored_from_revision_number": entry.get(
                "restored_from_revision_number"
            ),
            "created_at": entry.get("created_at"),
            "confirmed": False,
            "confirmed_at": None,
        }
        event = confirmations.get(str(entry.get("mapping_revision_id") or ""))
        if event is not None:
            public["confirmed"] = True
            public["confirmed_at"] = event.get("confirmed_at")
        result.append(public)
    return result


def _load_revision_from_history(
    item: dict[str, Any], history_entry: dict[str, Any]
) -> dict[str, Any]:
    revision_id = str(history_entry.get("mapping_revision_id") or "")
    try:
        revision = store.load_mapping_revision(
            str(item.get("project_id") or ""), revision_id
        )
    except (ValueError, OSError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_PERSISTENCE_FAILED",
            message="当前分组版本读取失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    if revision is None:
        raise _persistence_error()
    try:
        revision_number = int(revision.get("revision_number") or 0)
        expected_revision_number = int(history_entry.get("revision_number") or 0)
        mapping = revision.get("mapping") or {}
        mapping_sha256 = _canonical_sha(mapping)
        revision_payload_sha256 = store.mapping_revision_payload_sha256(
            revision
        )
    except (TypeError, ValueError) as exc:
        raise _persistence_error() from exc
    if (
        revision.get("mapping_revision_id") != revision_id
        or revision.get("project_id") != item.get("project_id")
        or revision.get("import_id") != item.get("import_id")
        or revision.get("workbook_revision_id") != item.get("workbook_revision_id")
        or revision_number != expected_revision_number
        or revision.get("mapping_sha256") != mapping_sha256
        or history_entry.get("mapping_sha256") != mapping_sha256
        or revision.get("revision_payload_sha256") != revision_payload_sha256
        or history_entry.get("revision_payload_sha256") != revision_payload_sha256
        or history_entry.get("change_kind") != revision.get("change_kind")
        or history_entry.get("change_reason") != revision.get("change_reason")
        or history_entry.get("restored_from_mapping_revision_id")
        != revision.get("restored_from_mapping_revision_id")
        or history_entry.get("restored_from_revision_number")
        != revision.get("restored_from_revision_number")
        or history_entry.get("created_at") != revision.get("created_at")
    ):
        raise _persistence_error()
    return revision


def _load_current_revision(
    item: dict[str, Any], state: dict[str, Any] | None
) -> dict[str, Any] | None:
    revision_number = int((state or {}).get("current_revision_number") or 0)
    if revision_number == 0:
        return None
    history = list((state or {}).get("revision_history") or [])
    if len(history) != revision_number:
        raise _persistence_error()
    return _load_revision_from_history(item, history[-1])


def _load_base_mapping(
    item: dict[str, Any],
    state: dict[str, Any] | None,
    base_mapping_revision: int,
) -> dict[str, Any] | None:
    if base_mapping_revision == 0:
        return None
    history = list((state or {}).get("revision_history") or [])
    if base_mapping_revision > len(history):
        raise _conflict(state)
    revision = _load_revision_from_history(
        item,
        history[base_mapping_revision - 1],
    )
    return deepcopy(revision.get("mapping") or {})


def _history_entry_by_id(
    state: dict[str, Any] | None,
    mapping_revision_id: str,
) -> dict[str, Any] | None:
    for entry in (state or {}).get("revision_history") or []:
        if (
            isinstance(entry, dict)
            and entry.get("mapping_revision_id") == mapping_revision_id
        ):
            return entry
    return None


def _response(
    *,
    item: dict[str, Any],
    proposals: dict[str, Any] | None,
    state: dict[str, Any] | None,
    revision: dict[str, Any] | None,
) -> dict[str, Any]:
    state = state or {}
    revision = revision or {}
    return {
        "import_id": item.get("import_id"),
        "project_id": item.get("project_id"),
        "status": state.get("effective_status")
        or item.get("status")
        or "GROUP_CONFIRMATION_REQUIRED",
        "proposals": _public_value(proposals or {}),
        "revision_number": int(state.get("current_revision_number") or 0),
        "mapping_revision_id": state.get("current_mapping_revision_id"),
        "mapping_sha256": state.get("current_mapping_sha256"),
        "mapping": _public_value(revision.get("mapping") or {}),
        "issues": _public_value(revision.get("issues") or []),
        "confirmation_ready": bool(revision.get("confirmation_ready", False)),
        "final_participant_preview": _public_value(
            revision.get("final_participant_preview") or {}
        ),
        "history": _history(state),
    }


def get_group_proposals(
    import_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    item, snapshot = _load_owned_inputs(import_id, login)
    try:
        proposals = build_group_proposals(
            snapshot,
            project_id=str(item.get("project_id") or ""),
            import_id=import_id,
            workbook_revision_id=str(item.get("workbook_revision_id") or ""),
        )
    except InterviewV2MappingError as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_INPUT_INVALID",
            message="分组所需的工作簿快照无法安全解析，请重新上传。",
            suggested_action="restart_upload",
        ) from exc
    state = _state(item)
    revision = _load_current_revision(item, state)
    return _response(
        item=item,
        proposals=proposals,
        state=state,
        revision=revision,
    )


def save_group_mapping(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    item, snapshot = _load_owned_inputs(import_id, login)
    state = _state(item)
    base = request.get("base_mapping_revision")
    if isinstance(base, bool) or not isinstance(base, int) or base < 0:
        raise InterviewV2ImportError(
            status_code=422,
            code="MAPPING_REQUEST_INVALID",
            message="分组映射请求格式无效。",
            suggested_action="review_group_mapping",
        )
    change_reason = str(request.get("change_reason") or "")
    change_kind = request.get("change_kind", "manual_edit")
    if not isinstance(change_kind, str) or change_kind != "manual_edit":
        raise InterviewV2ImportError(
            status_code=422,
            code="MAPPING_REQUEST_INVALID",
            message="分组映射请求格式无效。",
            suggested_action="review_group_mapping",
        )
    if len(change_reason) > 500:
        raise InterviewV2ImportError(
            status_code=422,
            code="MAPPING_REQUEST_INVALID",
            message="分组修改原因不能超过 500 个字符。",
            suggested_action="review_group_mapping",
            context={"limit_chars": 500},
        )
    current_number = int((state or {}).get("current_revision_number") or 0)
    current_sha = str((state or {}).get("current_mapping_sha256") or "")
    if current_number not in {base, base + 1}:
        raise _conflict(state)
    base_mapping = _load_base_mapping(item, state, base)
    try:
        result = normalize_and_validate_mapping(
            snapshot,
            request,
            project_id=str(item.get("project_id") or ""),
            import_id=import_id,
            workbook_revision_id=str(item.get("workbook_revision_id") or ""),
            base_mapping=base_mapping,
            target_mapping_revision=base + 1,
        )
    except InterviewV2MappingError as exc:
        raise InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            suggested_action="review_group_mapping",
            context=exc.context,
        ) from exc
    mapping = _public_value(result.get("mapping") or {})
    mapping_sha256 = _canonical_sha(mapping)
    if current_number == base + 1 and current_sha == mapping_sha256:
        revision = _load_current_revision(item, state)
        if (
            str((revision or {}).get("change_kind") or "manual_edit")
            != "manual_edit"
            or str((revision or {}).get("change_reason") or "") != change_reason
        ):
            raise _conflict(state)
        return _response(
            item=item,
            proposals={},
            state=state,
            revision=revision,
        )
    if current_number != base:
        raise _conflict(state)

    revision_number = base + 1
    project_id = str(item.get("project_id") or "")
    now = _now()
    revision = {
        "mapping_revision_id": _revision_id(
            project_id, import_id, revision_number, mapping_sha256
        ),
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": item.get("workbook_revision_id"),
        "revision_number": revision_number,
        "mapping_sha256": mapping_sha256,
        "mapping": mapping,
        "issues": _public_value(result.get("issues") or []),
        "confirmation_ready": bool(result.get("confirmation_ready", False)),
        "final_participant_preview": _public_value(
            result.get("final_participant_preview")
            or result.get("preview")
            or {}
        ),
        "change_kind": "manual_edit",
        "change_reason": change_reason,
        "created_at": now,
        "created_by": _owner_from_login(login).get("owner_key", ""),
    }
    revision["revision_payload_sha256"] = (
        store.mapping_revision_payload_sha256(revision)
    )
    try:
        revision, state = store.save_mapping_revision_cas(
            project_id=project_id,
            import_id=import_id,
            base_mapping_revision=base,
            revision=revision,
            updated_at=now,
        )
    except FileExistsError as exc:
        latest_state = _state(item)
        latest_number = int(
            (latest_state or {}).get("current_revision_number") or 0
        )
        latest_sha = str(
            (latest_state or {}).get("current_mapping_sha256") or ""
        )
        if latest_number == base + 1 and latest_sha == mapping_sha256:
            latest_revision = _load_current_revision(item, latest_state)
            if (
                str((latest_revision or {}).get("change_kind") or "manual_edit")
                == "manual_edit"
                and str((latest_revision or {}).get("change_reason") or "")
                == change_reason
                and str((latest_revision or {}).get("created_by") or "")
                == _owner_from_login(login).get("owner_key", "")
            ):
                return _response(
                    item=item,
                    proposals={},
                    state=latest_state,
                    revision=latest_revision,
                )
        raise _conflict(latest_state) from exc
    except (OSError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_PERSISTENCE_FAILED",
            message="分组映射保存失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    return _response(
        item=item,
        proposals={},
        state=state,
        revision=revision,
    )


def restore_group_mapping(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    """Append a new revision copied from a verified historical mapping."""

    item = _load_owned_import(import_id, login)
    state = _state(item)
    base = request.get("base_mapping_revision")
    target_id = str(request.get("target_mapping_revision_id") or "")
    target_sha = str(request.get("target_mapping_sha256") or "")
    change_kind = str(request.get("change_kind") or "")
    change_reason = str(request.get("change_reason") or "").strip()
    if (
        isinstance(base, bool)
        or not isinstance(base, int)
        or base < 1
        or not re.fullmatch(r"mapping_[0-9a-f]{32}", target_id)
        or not re.fullmatch(r"[0-9a-f]{64}", target_sha)
        or change_kind not in {"undo", "redo", "restore"}
        or not change_reason
        or len(change_reason) > 500
    ):
        raise InterviewV2ImportError(
            status_code=422,
            code="MAPPING_REQUEST_INVALID",
            message="分组版本恢复请求格式无效。",
            suggested_action="review_mapping_history",
        )

    current_number = int((state or {}).get("current_revision_number") or 0)
    if current_number == base + 1:
        target_entry = _history_entry_by_id(state, target_id)
        current_revision = _load_current_revision(item, state)
        if (
            target_entry is not None
            and target_entry.get("mapping_sha256") == target_sha
            and isinstance(target_entry.get("revision_number"), int)
            and not isinstance(target_entry.get("revision_number"), bool)
            and int(target_entry["revision_number"]) < base
            and current_revision is not None
            and current_revision.get("restored_from_mapping_revision_id")
            == target_id
            and current_revision.get("restored_from_revision_number")
            == target_entry.get("revision_number")
            and current_revision.get("mapping_sha256") == target_sha
            and current_revision.get("change_kind") == change_kind
            and current_revision.get("change_reason") == change_reason
            and current_revision.get("created_by")
            == _owner_from_login(login).get("owner_key", "")
        ):
            return _response(
                item=item,
                proposals={},
                state=state,
                revision=current_revision,
            )
        raise _conflict(state)
    if current_number != base:
        raise _conflict(state)

    target_entry = _history_entry_by_id(state, target_id)
    if (
        target_entry is None
        or target_entry.get("mapping_sha256") != target_sha
        or isinstance(target_entry.get("revision_number"), bool)
        or not isinstance(target_entry.get("revision_number"), int)
        or int(target_entry["revision_number"]) >= base
    ):
        raise InterviewV2ImportError(
            status_code=422,
            code="MAPPING_RESTORE_TARGET_INVALID",
            message="目标分组版本不属于当前可恢复历史，请刷新版本记录。",
            suggested_action="refresh_group_mapping",
        )
    target_revision = _load_revision_from_history(item, target_entry)

    revision_number = base + 1
    project_id = str(item.get("project_id") or "")
    now = _now()
    revision = {
        "mapping_revision_id": _revision_id(
            project_id, import_id, revision_number, target_sha
        ),
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": item.get("workbook_revision_id"),
        "revision_number": revision_number,
        "mapping_sha256": target_sha,
        "mapping": deepcopy(target_revision.get("mapping") or {}),
        "issues": deepcopy(target_revision.get("issues") or []),
        "confirmation_ready": bool(
            target_revision.get("confirmation_ready", False)
        ),
        "final_participant_preview": deepcopy(
            target_revision.get("final_participant_preview") or {}
        ),
        "change_kind": change_kind,
        "change_reason": change_reason,
        "restored_from_mapping_revision_id": target_id,
        "restored_from_revision_number": int(target_entry["revision_number"]),
        "created_at": now,
        "created_by": _owner_from_login(login).get("owner_key", ""),
    }
    revision["revision_payload_sha256"] = (
        store.mapping_revision_payload_sha256(revision)
    )
    try:
        revision, state = store.save_mapping_revision_cas(
            project_id=project_id,
            import_id=import_id,
            base_mapping_revision=base,
            revision=revision,
            updated_at=now,
        )
    except FileExistsError as exc:
        latest_state = _state(item)
        latest_number = int(
            (latest_state or {}).get("current_revision_number") or 0
        )
        if latest_number == base + 1:
            latest_target_entry = _history_entry_by_id(latest_state, target_id)
            latest_revision = _load_current_revision(item, latest_state)
            if (
                latest_target_entry is not None
                and latest_target_entry.get("mapping_sha256") == target_sha
                and latest_target_entry.get("revision_number")
                == target_entry.get("revision_number")
                and latest_revision is not None
                and latest_revision.get("restored_from_mapping_revision_id")
                == target_id
                and latest_revision.get("restored_from_revision_number")
                == target_entry.get("revision_number")
                and latest_revision.get("mapping_sha256") == target_sha
                and latest_revision.get("change_kind") == change_kind
                and latest_revision.get("change_reason") == change_reason
                and latest_revision.get("created_by")
                == _owner_from_login(login).get("owner_key", "")
            ):
                return _response(
                    item=item,
                    proposals={},
                    state=latest_state,
                    revision=latest_revision,
                )
        raise _conflict(latest_state) from exc
    except (OSError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_PERSISTENCE_FAILED",
            message="分组版本恢复失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    return _response(
        item=item,
        proposals={},
        state=state,
        revision=revision,
    )


def confirm_group_mapping(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    item = _load_owned_import(import_id, login)
    state = _state(item)
    base = request.get("base_mapping_revision")
    mapping_sha256 = str(request.get("mapping_sha256") or "")
    if (
        isinstance(base, bool)
        or not isinstance(base, int)
        or base < 0
        or len(mapping_sha256) != 64
        or any(char not in "0123456789abcdef" for char in mapping_sha256)
    ):
        raise InterviewV2ImportError(
            status_code=422,
            code="MAPPING_REQUEST_INVALID",
            message="分组确认请求格式无效。",
            suggested_action="refresh_group_mapping",
        )
    current = _current_metadata(state)
    if (
        current["current_revision_number"] != base
        or current["current_mapping_sha256"] != mapping_sha256
    ):
        raise _conflict(state)
    revision = _load_current_revision(item, state)
    if revision is None or not revision.get("confirmation_ready"):
        issues = list((revision or {}).get("issues") or [])
        raise InterviewV2ImportError(
            status_code=422,
            code="GROUP_MAPPING_CONFIRMATION_REQUIRED",
            message="分组和玩家绑定仍有待处理项，暂不能确认。",
            suggested_action="review_group_mapping",
            context={"issue_count": len(issues)},
        )
    try:
        state = store.confirm_mapping_revision_cas(
            project_id=str(item.get("project_id") or ""),
            import_id=import_id,
            base_mapping_revision=base,
            mapping_sha256=mapping_sha256,
            confirmed_by=_owner_from_login(login).get("owner_key", ""),
            confirmed_at=_now(),
        )
    except FileExistsError as exc:
        raise _conflict(_state(item)) from exc
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="MAPPING_PERSISTENCE_FAILED",
            message="分组确认保存失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_mapping_request",
        ) from exc
    return _response(
        item=item,
        proposals={},
        state=state,
        revision=revision,
    )


def get_interview_import_with_mapping_status(
    import_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    """Overlay the authoritative mapping checkpoint on the Batch 1 response."""

    public = get_interview_import(import_id, login)
    state = _state(public)
    if state is not None:
        _load_current_revision(public, state)
        public["status"] = state.get("effective_status") or public.get("status")
    return public
