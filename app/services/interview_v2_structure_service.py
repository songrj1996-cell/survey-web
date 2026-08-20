"""访谈报告 V2：确定性结构、证据和人工复核编排。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.core.interview_v2_evidence import (
    InterviewV2StructureError,
    apply_review_resolutions,
    build_structure_and_evidence,
)
from app.core.security import _owner_from_login
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_mapping_service import (
    get_interview_import_with_mapping_status,
)
from app.storage import interview_v2_store as store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_sha(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _deterministic_id(prefix: str, value: Any) -> str:
    return f"{prefix}_{_canonical_sha(value)[:32]}"


def _persistence_error(message: str = "访谈结构保存失败，请稍后重试。"):
    return InterviewV2ImportError(
        status_code=500,
        code="STRUCTURE_PERSISTENCE_FAILED",
        message=message,
        retryable=True,
        suggested_action="retry_structure_request",
    )


def _not_ready_error(status: str) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=409,
        code="STRUCTURE_INPUT_NOT_READY",
        message="请先确认当前 Sheet 分组和玩家绑定。",
        suggested_action="review_group_mapping",
        context={"status": status},
    )


def _head_context(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    return {
        "current_structure_revision_id": state.get(
            "current_structure_revision_id"
        ),
        "current_evidence_revision_id": state.get(
            "current_evidence_revision_id"
        ),
    }


def _revision_conflict(state: dict[str, Any] | None) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=409,
        code="STRUCTURE_REVISION_CONFLICT",
        message="访谈结构或证据已更新，请刷新后合并更改。",
        suggested_action="refresh_structure_review",
        context=_head_context(state),
    )


def _mapping_conflict(
    mapping_revision_id: str,
    mapping_sha256: str,
    mapping_status: str | None = None,
) -> InterviewV2ImportError:
    context = {
        "current_mapping_revision_id": mapping_revision_id,
        "current_mapping_sha256": mapping_sha256,
    }
    if mapping_status:
        context["current_mapping_status"] = mapping_status
    return InterviewV2ImportError(
        status_code=409,
        code="STRUCTURE_INPUT_CONFLICT",
        message="分组映射已更新，请刷新后重新开始结构化。",
        suggested_action="refresh_group_mapping",
        context=context,
    )


def _load_mapping_checkpoint(
    import_id: str,
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    public = get_interview_import_with_mapping_status(import_id, login)
    status = str(public.get("status") or "")
    if status != "GROUP_MAPPING_CONFIRMED":
        raise _not_ready_error(status)
    return public


def _load_confirmed_input(
    import_id: str,
    login: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    public = _load_mapping_checkpoint(import_id, login)
    try:
        bundle = store.load_confirmed_structure_input_bundle(import_id)
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error(
            "访谈结构所需的确认输入不可用，请稍后重试。"
        ) from exc
    if bundle is None:
        raise _not_ready_error(str(public.get("status") or ""))
    bundled_import = bundle.get("interview_import") or {}
    if (
        bundled_import.get("import_id") != public.get("import_id")
        or bundled_import.get("project_id") != public.get("project_id")
        or bundled_import.get("workbook_revision_id")
        != public.get("workbook_revision_id")
    ):
        raise _persistence_error(
            "访谈结构所需的确认输入校验失败，请重新上传。"
        )
    return public, bundle


def _load_current_bundle_after_owner_check(
    public: dict[str, Any],
) -> dict[str, Any]:
    project_id = str(public.get("project_id") or "")
    import_id = str(public.get("import_id") or "")
    try:
        bundle = store.load_current_structure_bundle(project_id, import_id)
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error(
            "访谈结构状态校验失败，请稍后重试。"
        ) from exc
    if bundle is None:
        raise InterviewV2ImportError(
            status_code=409,
            code="STRUCTURE_NOT_BUILT",
            message="当前导入尚未生成访谈结构。",
            suggested_action="build_structure",
        )
    state = bundle.get("state") or {}
    if bool(state.get("is_stale")) or state.get("artifact_status") == "STALE":
        raise InterviewV2ImportError(
            status_code=409,
            code="STRUCTURE_INPUT_STALE",
            message="分组映射已更新，请重新生成访谈结构。",
            suggested_action="build_structure",
            context=_head_context(state),
        )
    return bundle


def _load_current_bundle(
    import_id: str,
    login: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    public = _load_mapping_checkpoint(import_id, login)
    return public, _load_current_bundle_after_owner_check(public)


def _issue_counts(issues: list[dict[str, Any]]) -> tuple[int, int]:
    open_issues = [
        item
        for item in issues
        if str(item.get("status") or "open") not in {"resolved", "dismissed"}
    ]
    blocking = [
        item
        for item in open_issues
        if str(item.get("severity") or "") == "blocking"
    ]
    return len(open_issues), len(blocking)


def _select(value: dict[str, Any], fields: set[str]) -> dict[str, Any]:
    return {
        key: deepcopy(item)
        for key, item in value.items()
        if key in fields
    }


def _public_structure_payload(value: dict[str, Any]) -> dict[str, Any]:
    source_fields = {
        "project_id",
        "import_id",
        "workbook_revision_id",
        "base_snapshot_sha256",
        "mapping_revision_id",
        "mapping_sha256",
        "rules_version",
    }
    module_fields = {
        "module_id",
        "canonical_name",
        "normalized_key",
        "raw_titles",
        "occurrence_ids",
        "mapping_method",
        "decision_status",
        "decision_source",
        "confidence",
        "confirmed_at",
    }
    question_fields = {
        "main_question_id",
        "module_id",
        "canonical_text",
        "normalized_key",
        "raw_prompts",
        "occurrence_ids",
        "alignment_method",
        "decision_status",
        "decision_source",
        "confidence",
        "confirmed_at",
    }
    occurrence_fields = {
        "occurrence_id",
        "group_id",
        "sheet_id",
        "sheet_name",
        "recorder_label",
        "row",
        "row_role",
        "raw_module_text",
        "raw_type_text",
        "raw_prompt_text",
        "canonical_module_id",
        "canonical_main_question_id",
        "parent_main_occurrence_id",
        "mapping_method",
        "confidence",
        "decision_status",
        "decision_source",
        "confirmed_at",
        "has_participant_content",
    }
    return {
        "structure_schema_version": value.get("structure_schema_version"),
        "source": _select(value.get("source") or {}, source_fields),
        "modules": [
            _select(item, module_fields)
            for item in value.get("modules") or []
            if isinstance(item, dict)
        ],
        "main_questions": [
            _select(item, question_fields)
            for item in value.get("main_questions") or []
            if isinstance(item, dict)
        ],
        "occurrences": [
            _select(item, occurrence_fields)
            for item in value.get("occurrences") or []
            if isinstance(item, dict)
        ],
    }


def _public_review_issue(value: dict[str, Any]) -> dict[str, Any]:
    affected_fields = {
        "group_ids",
        "participant_ids",
        "sheet_ids",
        "occurrence_ids",
        "evidence_ids",
        "module_ids",
        "main_question_ids",
    }
    suggestion_fields = {
        "resolution",
        "target_id",
        "row_role",
        "evidence_type",
    }
    resolution_fields = {
        "action",
        "target_id",
        "row_role",
        "evidence_type",
        "comment",
        "resolved_at",
    }
    result = _select(
        value,
        {
            "issue_id",
            "code",
            "severity",
            "status",
            "message",
            "suggested_action",
            "allowed_resolutions",
            "reason",
            "report_impact",
        },
    )
    result["affected_ids"] = _select(
        value.get("affected_ids") or {}, affected_fields
    )
    result["source_context"] = _select(
        value.get("source_context") or {}, {"group_id", "sheet_id", "row"}
    )
    result["suggested_resolution"] = _select(
        value.get("suggested_resolution") or {}, suggestion_fields
    )
    resolution = value.get("resolution")
    result["resolution"] = (
        _select(resolution, resolution_fields)
        if isinstance(resolution, dict)
        else None
    )
    return result


def _structure_response(bundle: dict[str, Any]) -> dict[str, Any]:
    state = bundle.get("state") or {}
    structure_revision = bundle.get("structure_revision") or {}
    evidence_revision = bundle.get("evidence_revision") or {}
    issues = list(bundle.get("review_issues") or [])
    entries = list(
        (evidence_revision.get("evidence") or evidence_revision).get(
            "entries", []
        )
    )
    included_entries = [
        item for item in entries if item.get("inclusion_status") == "included"
    ]
    open_count, blocking_count = _issue_counts(issues)
    recommended_count = sum(
        1
        for item in issues
        if str(item.get("status") or "open") not in {"resolved", "dismissed"}
        and str(item.get("severity") or "") == "recommended"
    )
    return {
        "import_id": state.get("import_id"),
        "project_id": state.get("project_id"),
        "status": state.get("effective_status"),
        "structure_revision_id": state.get("current_structure_revision_id"),
        "evidence_revision_id": state.get("current_evidence_revision_id"),
        "structure": _public_structure_payload(
            structure_revision.get("structure") or structure_revision
        ),
        "evidence_summary": {
            "evidence_count": len(included_entries),
            "self_report_count": sum(
                1
                for item in included_entries
                if item.get("evidence_type") == "participant_self_report"
            ),
            "observation_count": sum(
                1
                for item in included_entries
                if item.get("evidence_type") == "researcher_observation"
            ),
            "needs_review_count": sum(
                1
                for item in included_entries
                if item.get("identity_decision_status") == "needs_review"
            ),
        },
        "review_summary": {
            "open_issue_count": open_count,
            "blocking_issue_count": blocking_count,
            "recommended_issue_count": recommended_count,
        },
    }


def _review_issues_response(bundle: dict[str, Any]) -> dict[str, Any]:
    state = bundle.get("state") or {}
    raw_issues = list(bundle.get("review_issues") or [])
    issues = [_public_review_issue(item) for item in raw_issues]
    open_count, blocking_count = _issue_counts(issues)
    return {
        "import_id": state.get("import_id"),
        "project_id": state.get("project_id"),
        "status": state.get("effective_status"),
        "structure_revision_id": state.get("current_structure_revision_id"),
        "evidence_revision_id": state.get("current_evidence_revision_id"),
        "issues": issues,
        "open_issue_count": open_count,
        "blocking_issue_count": blocking_count,
    }


def _resolution_response(
    bundle: dict[str, Any],
    *,
    resolved_issue_ids: list[str],
    manual_override_ids: list[str],
) -> dict[str, Any]:
    state = bundle.get("state") or {}
    issues = list(bundle.get("review_issues") or [])
    open_count, blocking_count = _issue_counts(issues)
    return {
        "import_id": state.get("import_id"),
        "project_id": state.get("project_id"),
        "status": state.get("effective_status"),
        "structure_revision_id": state.get("current_structure_revision_id"),
        "evidence_revision_id": state.get("current_evidence_revision_id"),
        "resolved_issue_ids": list(resolved_issue_ids),
        "manual_override_ids": list(manual_override_ids),
        "open_issue_count": open_count,
        "blocking_issue_count": blocking_count,
    }


def _resolved_issue_ids(
    bundle: dict[str, Any], requested_issue_ids: list[str]
) -> list[str]:
    requested = set(requested_issue_ids)
    return [
        str(item.get("issue_id") or "")
        for item in bundle.get("review_issues") or []
        if item.get("issue_id") in requested
        and str(item.get("status") or "") in {"resolved", "dismissed"}
    ]


def _revision_content(revision: dict[str, Any], key: str) -> dict[str, Any]:
    content = revision.get(key)
    return deepcopy(content if isinstance(content, dict) else revision)


def _make_revisions(
    *,
    public: dict[str, Any],
    mapping_revision_id: str,
    mapping_sha256: str,
    snapshot_sha256: str,
    structure_number: int,
    evidence_number: int,
    result: dict[str, Any],
    request_fingerprint: str,
    base_structure_revision_id: str | None,
    base_evidence_revision_id: str | None,
    actor: str,
    created_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project_id = str(public.get("project_id") or "")
    import_id = str(public.get("import_id") or "")
    workbook_revision_id = str(public.get("workbook_revision_id") or "")
    structure = deepcopy(result.get("structure") or {})
    evidence = deepcopy(result.get("evidence") or {})
    structure_id = _deterministic_id(
        "structure",
        {
            "project_id": project_id,
            "import_id": import_id,
            "base_structure_revision_id": base_structure_revision_id,
            "request_fingerprint": request_fingerprint,
        },
    )
    evidence_id = _deterministic_id(
        "evidence",
        {
            "project_id": project_id,
            "import_id": import_id,
            "base_evidence_revision_id": base_evidence_revision_id,
            "request_fingerprint": request_fingerprint,
        },
    )
    common = {
        "project_id": project_id,
        "import_id": import_id,
        "workbook_revision_id": workbook_revision_id,
        "snapshot_sha256": snapshot_sha256,
        "mapping_revision_id": mapping_revision_id,
        "mapping_sha256": mapping_sha256,
        "request_fingerprint": request_fingerprint,
        "created_at": created_at,
        "created_by": actor,
    }
    structure_revision = {
        **common,
        "structure_revision_id": structure_id,
        "revision_number": structure_number,
        "structure": structure,
    }
    structure_revision["revision_payload_sha256"] = (
        store.structure_revision_payload_sha256(structure_revision)
    )
    evidence_revision = {
        **common,
        "evidence_revision_id": evidence_id,
        "structure_revision_id": structure_id,
        "revision_number": evidence_number,
        "evidence": evidence,
    }
    evidence_revision["revision_payload_sha256"] = (
        store.evidence_revision_payload_sha256(evidence_revision)
    )
    return structure_revision, evidence_revision


def build_structure(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    public, input_bundle = _load_confirmed_input(import_id, login)
    mapping_state = input_bundle.get("mapping_state") or {}
    mapping_revision = input_bundle.get("mapping_revision") or {}
    mapping_revision_id = str(
        mapping_state.get("confirmed_mapping_revision_id")
        or mapping_revision.get("mapping_revision_id")
        or ""
    )
    mapping_sha256 = str(
        mapping_state.get("confirmed_mapping_sha256")
        or mapping_revision.get("mapping_sha256")
        or ""
    )
    if (
        request.get("base_mapping_revision_id") != mapping_revision_id
        or request.get("base_mapping_sha256") != mapping_sha256
    ):
        raise _mapping_conflict(mapping_revision_id, mapping_sha256)

    fingerprint = _canonical_sha(
        {
            "operation": "build_structure",
            "project_id": public.get("project_id"),
            "import_id": import_id,
            "workbook_revision_id": public.get("workbook_revision_id"),
            "mapping_revision_id": mapping_revision_id,
            "mapping_sha256": mapping_sha256,
            "actor": _owner_from_login(login).get("owner_key", ""),
        }
    )
    try:
        current_state = store.load_structure_state(
            str(public.get("project_id") or "")
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error(
            "访谈结构状态读取失败，请稍后重试。"
        ) from exc
    if current_state is not None and not bool(current_state.get("is_stale")):
        current_mapping_id = str(
            current_state.get("current_mapping_revision_id") or ""
        )
        current_mapping_sha = str(
            current_state.get("current_mapping_sha256") or ""
        )
        if (
            current_mapping_id == mapping_revision_id
            and current_mapping_sha == mapping_sha256
        ):
            return _structure_response(
                _load_current_bundle_after_owner_check(public)
            )

    snapshot = input_bundle.get("physical_snapshot") or {}
    mapping = mapping_revision.get("mapping") or {}
    try:
        result = build_structure_and_evidence(
            snapshot,
            mapping,
            project_id=str(public.get("project_id") or ""),
            import_id=import_id,
            workbook_revision_id=str(public.get("workbook_revision_id") or ""),
            mapping_revision_id=mapping_revision_id,
            mapping_sha256=mapping_sha256,
        )
    except InterviewV2StructureError as exc:
        raise InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            suggested_action="review_structure_input",
            context=exc.context,
        ) from exc

    state = current_state or {}
    structure_number = int(
        state.get("current_structure_revision_number") or 0
    ) + 1
    evidence_number = int(
        state.get("current_evidence_revision_number") or 0
    ) + 1
    actor = str(_owner_from_login(login).get("owner_key") or "")
    created_at = _now()
    snapshot_sha256 = str(
        snapshot.get("snapshot_sha256")
        or (input_bundle.get("workbook_revision") or {}).get(
            "snapshot_sha256"
        )
        or ""
    )
    structure_revision, evidence_revision = _make_revisions(
        public=public,
        mapping_revision_id=mapping_revision_id,
        mapping_sha256=mapping_sha256,
        snapshot_sha256=snapshot_sha256,
        structure_number=structure_number,
        evidence_number=evidence_number,
        result=result,
        request_fingerprint=fingerprint,
        base_structure_revision_id=state.get("current_structure_revision_id"),
        base_evidence_revision_id=state.get("current_evidence_revision_id"),
        actor=actor,
        created_at=created_at,
    )
    try:
        structure_revision, evidence_revision, saved_state = (
            store.save_structure_bundle_cas(
                project_id=str(public.get("project_id") or ""),
                import_id=import_id,
                base_structure_revision_id=state.get(
                    "current_structure_revision_id"
                ),
                base_evidence_revision_id=state.get(
                    "current_evidence_revision_id"
                ),
                structure_revision=structure_revision,
                evidence_revision=evidence_revision,
                review_issues=list(result.get("review_issues") or []),
                manual_overrides=[],
                request_fingerprint=fingerprint,
                effective_status=str(result.get("status") or ""),
                updated_at=created_at,
            )
        )
    except store.StructureInputConflictError as exc:
        raise _mapping_conflict(
            exc.current_mapping_revision_id,
            exc.current_mapping_sha256,
            exc.current_mapping_status,
        ) from exc
    except FileExistsError as exc:
        latest = _load_current_bundle_after_owner_check(public)
        latest_state = (latest or {}).get("state") or {}
        if (
            latest is not None
            and latest_state.get("current_request_fingerprint") == fingerprint
            and latest_state.get("current_mapping_revision_id")
            == mapping_revision_id
            and latest_state.get("current_mapping_sha256") == mapping_sha256
        ):
            return _structure_response(latest)
        raise _revision_conflict(latest_state) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error() from exc
    durable = _load_current_bundle_after_owner_check(public)
    return _structure_response(durable)


def get_structure(
    import_id: str,
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    _public, bundle = _load_current_bundle(import_id, login)
    return _structure_response(bundle)


def get_review_issues(
    import_id: str,
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    _public, bundle = _load_current_bundle(import_id, login)
    return _review_issues_response(bundle)


def _normalize_resolution(
    issue_id: str,
    resolution: dict[str, Any],
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "resolution": resolution.get("resolution"),
        "target_id": resolution.get("target_id"),
        "row_role": resolution.get("row_role"),
        "evidence_type": resolution.get("evidence_type"),
        "comment": resolution.get("comment"),
    }


def _apply_resolutions(
    *,
    public: dict[str, Any],
    bundle: dict[str, Any],
    base_structure_revision_id: str,
    base_evidence_revision_id: str,
    resolutions: list[dict[str, Any]],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    state = bundle.get("state") or {}
    fingerprint = _canonical_sha(
        {
            "operation": "resolve_structure_issues",
            "project_id": public.get("project_id"),
            "import_id": public.get("import_id"),
            "base_structure_revision_id": base_structure_revision_id,
            "base_evidence_revision_id": base_evidence_revision_id,
            "resolutions": resolutions,
            "actor": _owner_from_login(login).get("owner_key", ""),
        }
    )
    requested_issue_ids = [str(item.get("issue_id") or "") for item in resolutions]
    if state.get("current_request_fingerprint") == fingerprint:
        overrides = [
            item
            for item in bundle.get("manual_overrides") or []
            if item.get("request_fingerprint") == fingerprint
        ]
        return _resolution_response(
            bundle,
            resolved_issue_ids=_resolved_issue_ids(bundle, requested_issue_ids),
            manual_override_ids=[
                str(item.get("manual_override_id") or "")
                for item in overrides
                if item.get("manual_override_id")
            ],
        )
    if (
        state.get("current_structure_revision_id")
        != base_structure_revision_id
        or state.get("current_evidence_revision_id")
        != base_evidence_revision_id
    ):
        raise _revision_conflict(state)

    structure_revision = bundle.get("structure_revision") or {}
    evidence_revision = bundle.get("evidence_revision") or {}
    issues = list(bundle.get("review_issues") or [])
    actor = str(_owner_from_login(login).get("owner_key") or "")
    resolved_at = _now()
    try:
        result = apply_review_resolutions(
            _revision_content(structure_revision, "structure"),
            _revision_content(evidence_revision, "evidence"),
            issues,
            resolutions,
            actor=actor,
            resolved_at=resolved_at,
            operation_fingerprint=fingerprint,
        )
    except InterviewV2StructureError as exc:
        raise InterviewV2ImportError(
            status_code=422,
            code=exc.code,
            message=exc.message,
            suggested_action="review_structure_issue",
            context=exc.context,
        ) from exc

    new_structure, new_evidence = _make_revisions(
        public=public,
        mapping_revision_id=str(structure_revision.get("mapping_revision_id") or ""),
        mapping_sha256=str(structure_revision.get("mapping_sha256") or ""),
        snapshot_sha256=str(structure_revision.get("snapshot_sha256") or ""),
        structure_number=int(state.get("current_structure_revision_number") or 0)
        + 1,
        evidence_number=int(state.get("current_evidence_revision_number") or 0)
        + 1,
        result=result,
        request_fingerprint=fingerprint,
        base_structure_revision_id=base_structure_revision_id,
        base_evidence_revision_id=base_evidence_revision_id,
        actor=actor,
        created_at=resolved_at,
    )
    manual_overrides = list(result.get("manual_overrides") or [])
    for override in manual_overrides:
        override["request_fingerprint"] = fingerprint
    try:
        new_structure, new_evidence, saved_state = store.save_structure_bundle_cas(
            project_id=str(public.get("project_id") or ""),
            import_id=str(public.get("import_id") or ""),
            base_structure_revision_id=base_structure_revision_id,
            base_evidence_revision_id=base_evidence_revision_id,
            structure_revision=new_structure,
            evidence_revision=new_evidence,
            review_issues=list(result.get("review_issues") or []),
            manual_overrides=manual_overrides,
            request_fingerprint=fingerprint,
            effective_status=str(result.get("status") or ""),
            updated_at=resolved_at,
        )
    except store.StructureInputConflictError as exc:
        raise _mapping_conflict(
            exc.current_mapping_revision_id,
            exc.current_mapping_sha256,
            exc.current_mapping_status,
        ) from exc
    except FileExistsError as exc:
        latest = _load_current_bundle_after_owner_check(public)
        latest_state = (latest or {}).get("state") or {}
        if (
            latest is not None
            and latest_state.get("current_request_fingerprint") == fingerprint
        ):
            overrides = [
                item
                for item in latest.get("manual_overrides") or []
                if item.get("request_fingerprint") == fingerprint
            ]
            return _resolution_response(
                latest,
                resolved_issue_ids=_resolved_issue_ids(
                    latest, requested_issue_ids
                ),
                manual_override_ids=[
                    str(item.get("manual_override_id") or "")
                    for item in overrides
                    if item.get("manual_override_id")
                ],
            )
        raise _revision_conflict(latest_state) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error() from exc

    saved_bundle = _load_current_bundle_after_owner_check(public)
    durable_overrides = [
        item
        for item in saved_bundle.get("manual_overrides") or []
        if item.get("request_fingerprint") == fingerprint
    ]
    return _resolution_response(
        saved_bundle,
        resolved_issue_ids=_resolved_issue_ids(
            saved_bundle, requested_issue_ids
        ),
        manual_override_ids=[
            str(item.get("manual_override_id") or "")
            for item in durable_overrides
            if item.get("manual_override_id")
        ],
    )


def resolve_review_issue(
    issue_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        locator = store.locate_review_issue(issue_id)
    except (OSError, TypeError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=404,
            code="REVIEW_ISSUE_NOT_FOUND",
            message="待复核项不存在。",
            suggested_action="refresh_structure_review",
        ) from exc
    if locator is None:
        raise InterviewV2ImportError(
            status_code=404,
            code="REVIEW_ISSUE_NOT_FOUND",
            message="待复核项不存在。",
            suggested_action="refresh_structure_review",
        )
    public, bundle = _load_current_bundle(str(locator.get("import_id") or ""), login)
    if (
        locator.get("project_id") != public.get("project_id")
        or not any(
            item.get("issue_id") == issue_id
            for item in bundle.get("review_issues") or []
        )
    ):
        raise InterviewV2ImportError(
            status_code=404,
            code="REVIEW_ISSUE_NOT_FOUND",
            message="待复核项不存在。",
            suggested_action="refresh_structure_review",
        )
    return _apply_resolutions(
        public=public,
        bundle=bundle,
        base_structure_revision_id=str(
            request.get("base_structure_revision_id") or ""
        ),
        base_evidence_revision_id=str(
            request.get("base_evidence_revision_id") or ""
        ),
        resolutions=[_normalize_resolution(issue_id, request)],
        login=login,
    )


def resolve_review_issues_batch(
    import_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    public, bundle = _load_current_bundle(import_id, login)
    resolutions = [
        _normalize_resolution(str(item.get("issue_id") or ""), item)
        for item in request.get("resolutions") or []
    ]
    return _apply_resolutions(
        public=public,
        bundle=bundle,
        base_structure_revision_id=str(
            request.get("base_structure_revision_id") or ""
        ),
        base_evidence_revision_id=str(
            request.get("base_evidence_revision_id") or ""
        ),
        resolutions=resolutions,
        login=login,
    )


def get_evidence_context(
    evidence_id: str,
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    try:
        locator = store.locate_evidence(evidence_id)
    except (OSError, TypeError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=404,
            code="EVIDENCE_NOT_FOUND",
            message="证据不存在。",
            suggested_action="refresh_structure_review",
        ) from exc
    if locator is None:
        raise InterviewV2ImportError(
            status_code=404,
            code="EVIDENCE_NOT_FOUND",
            message="证据不存在。",
            suggested_action="refresh_structure_review",
        )
    public, bundle = _load_current_bundle(str(locator.get("import_id") or ""), login)
    state = bundle.get("state") or {}
    if (
        locator.get("project_id") != public.get("project_id")
        or not any(
            item.get("evidence_id") == evidence_id
            for item in (
                (bundle.get("evidence_revision") or {}).get("evidence")
                or bundle.get("evidence_revision")
                or {}
            ).get("entries", [])
        )
    ):
        raise InterviewV2ImportError(
            status_code=404,
            code="EVIDENCE_NOT_FOUND",
            message="证据不存在。",
            suggested_action="refresh_structure_review",
        )
    try:
        stored = store.load_evidence_with_context(
            str(locator.get("project_id") or ""),
            str(locator.get("import_id") or ""),
            str(state.get("current_evidence_revision_id") or ""),
            evidence_id,
        )
    except FileExistsError as exc:
        try:
            latest_state = store.load_structure_state(
                str(public.get("project_id") or "")
            )
        except (OSError, TypeError, ValueError) as reload_exc:
            raise _persistence_error(
                "证据上下文读取失败，请稍后重试。"
            ) from reload_exc
        raise _revision_conflict(latest_state) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _persistence_error(
            "证据上下文读取失败，请稍后重试。"
        ) from exc
    if stored is None:
        raise InterviewV2ImportError(
            status_code=404,
            code="EVIDENCE_NOT_FOUND",
            message="证据不存在。",
            suggested_action="refresh_structure_review",
        )
    return _public_evidence_context(stored, evidence_id, state)


def _public_evidence_context(
    stored: dict[str, Any],
    evidence_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Whitelist one participant's evidence and structural source context."""

    evidence_allowed = {
        "evidence_id",
        "participant_id",
        "participant_label",
        "group_id",
        "recorder_label",
        "module_id",
        "main_question_id",
        "occurrence_id",
        "sheet_id",
        "sheet_name",
        "row",
        "column",
        "cell_address",
        "evidence_type",
        "capture_context",
        "prompt_text",
        "raw_content",
        "display_content",
        "normalized_content",
        "source_cell_id",
        "source_value_sha256",
        "formula_cache_status",
        "fragment_text_field",
        "fragment_start",
        "fragment_end",
        "inclusion_status",
        "identity_decision_status",
        "decision_source",
        "confidence",
        "confirmed_at",
    }
    occurrence_allowed = {
        "occurrence_id",
        "group_id",
        "sheet_id",
        "sheet_name",
        "recorder_label",
        "row",
        "row_role",
        "raw_module_text",
        "raw_type_text",
        "raw_prompt_text",
        "canonical_module_id",
        "canonical_main_question_id",
        "parent_main_occurrence_id",
        "mapping_method",
        "confidence",
        "decision_status",
        "decision_source",
        "confirmed_at",
        "has_participant_content",
    }
    raw_evidence = stored.get("evidence") or {}
    raw_occurrence = stored.get("occurrence") or {}
    if not raw_occurrence:
        structure_revision = stored.get("structure_revision") or {}
        structure = structure_revision.get("structure") or structure_revision
        raw_occurrence = next(
            (
                item
                for item in structure.get("occurrences") or []
                if isinstance(item, dict)
                and item.get("occurrence_id") == raw_evidence.get("occurrence_id")
            ),
            {},
        )
    evidence = {
        key: deepcopy(value)
        for key, value in raw_evidence.items()
        if key in evidence_allowed
    }
    occurrence = {
        key: deepcopy(value)
        for key, value in raw_occurrence.items()
        if key in occurrence_allowed
    }
    raw_context = stored.get("source_context") or _assemble_source_context(
        stored, raw_evidence
    )
    context_scalar_fields = {
        "source_cell_id",
        "sheet_id",
        "sheet_name",
        "row",
        "column",
        "cell_address",
    }
    source_context = {
        key: deepcopy(value)
        for key, value in raw_context.items()
        if key in context_scalar_fields
    }
    neighboring = raw_context.get("neighboring_occurrences")
    if isinstance(neighboring, list):
        source_context["neighboring_occurrences"] = [
            {
                key: deepcopy(value)
                for key, value in item.items()
                if key in occurrence_allowed
            }
            for item in neighboring
            if isinstance(item, dict)
        ]
    return {
        "evidence_id": evidence_id,
        "structure_revision_id": state.get("current_structure_revision_id"),
        "evidence_revision_id": state.get("current_evidence_revision_id"),
        "evidence": evidence,
        "occurrence": occurrence,
        "source_context": source_context,
    }


def _assemble_source_context(
    stored: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    """Select structural neighbors without exposing other participant cells."""

    sheet_id = str(evidence.get("sheet_id") or "")
    try:
        target_row = int(evidence.get("row"))
        target_column = int(evidence.get("column"))
    except (TypeError, ValueError):
        target_row = 0
        target_column = 0
    structure_revision = stored.get("structure_revision") or {}
    structure = structure_revision.get("structure") or structure_revision
    neighboring = [
        item
        for item in structure.get("occurrences") or []
        if isinstance(item, dict)
        and str(item.get("sheet_id") or "") == sheet_id
        and item.get("occurrence_id") != evidence.get("occurrence_id")
        and abs(int(item.get("row") or 0) - target_row) <= 2
    ]
    neighboring.sort(key=lambda item: int(item.get("row") or 0))
    return {
        "source_cell_id": evidence.get("source_cell_id"),
        "sheet_id": sheet_id,
        "sheet_name": evidence.get("sheet_name"),
        "row": target_row or evidence.get("row"),
        "column": target_column or evidence.get("column"),
        "cell_address": evidence.get("cell_address"),
        "neighboring_occurrences": neighboring,
    }
