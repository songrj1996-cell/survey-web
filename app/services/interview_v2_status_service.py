"""访谈报告 V2 导入检查点状态聚合。"""

from __future__ import annotations

from typing import Any

from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_mapping_service import (
    get_interview_import_with_mapping_status,
)
from app.storage import interview_v2_store as store


_STRUCTURE_STATUSES = {
    "STRUCTURE_REVIEW_REQUIRED",
    "READY_FOR_DOSSIERS",
}
_ANALYSIS_BOUNDARY_STATUSES = {
    "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
    "READY_FOR_DOSSIERS",
}


def get_interview_import_with_structure_status(
    import_id: str,
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the authoritative reachable checkpoint for an import.

    Mapping remains the upstream authority.  A structure checkpoint is only
    exposed while the current mapping is still confirmed and the persisted
    structure state is current for that mapping.
    """

    public = get_interview_import_with_mapping_status(import_id, login)
    if public.get("status") != "GROUP_MAPPING_CONFIRMED":
        return public

    try:
        state = store.load_structure_state(str(public.get("project_id") or ""))
    except (OSError, TypeError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="STRUCTURE_PERSISTENCE_FAILED",
            message="访谈结构状态读取失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_structure_request",
        ) from exc

    if state is None or bool(state.get("is_stale")):
        return public

    status = str(
        state.get("derived_status")
        or state.get("effective_status")
        or ""
    )
    if status == "STALE":
        return public
    if status not in _STRUCTURE_STATUSES:
        raise InterviewV2ImportError(
            status_code=500,
            code="STRUCTURE_PERSISTENCE_FAILED",
            message="访谈结构状态校验失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_structure_request",
        )

    if status != "READY_FOR_DOSSIERS":
        public["status"] = status
        return public

    try:
        boundary_state = store.load_analysis_boundary_state(
            str(public.get("project_id") or "")
        )
    except (OSError, TypeError, ValueError) as exc:
        raise InterviewV2ImportError(
            status_code=500,
            code="ANALYSIS_BOUNDARY_PERSISTENCE_FAILED",
            message="分析边界状态读取失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_analysis_boundary_request",
        ) from exc

    if boundary_state is None or bool(boundary_state.get("is_stale")):
        public["status"] = "ANALYSIS_BOUNDARY_REQUIRED"
        return public

    boundary_status = str(
        boundary_state.get("derived_status")
        or boundary_state.get("effective_status")
        or ""
    )
    if boundary_status == "ANALYSIS_BOUNDARY_REQUIRED":
        public["status"] = boundary_status
        return public
    if boundary_status not in _ANALYSIS_BOUNDARY_STATUSES:
        raise InterviewV2ImportError(
            status_code=500,
            code="ANALYSIS_BOUNDARY_PERSISTENCE_FAILED",
            message="分析边界状态校验失败，请稍后重试。",
            retryable=True,
            suggested_action="retry_analysis_boundary_request",
        )

    public["status"] = boundary_status
    if boundary_status == "READY_FOR_DOSSIERS":
        evidence_revision_id = str(
            boundary_state.get("current_evidence_revision_id") or ""
        )
        if not evidence_revision_id:
            return public
        evidence_revision = store.load_evidence_revision(
            str(public.get("project_id") or ""),
            evidence_revision_id,
        )
        payload = (evidence_revision or {}).get("evidence") or {}
        participants = (
            (evidence_revision or {}).get("expected_participants")
            or payload.get("expected_participants")
            or []
        )
        summary = {
            "participant_count": len(participants),
            "not_generated_count": 0,
            "generated_count": 0,
            "approved_count": 0,
            "needs_changes_count": 0,
        }
        for participant in participants:
            current = store.load_current_participant_dossier(
                str(public.get("project_id") or ""),
                str(participant.get("participant_id") or ""),
            )
            status = str((current or {}).get("revision", {}).get("status") or "not_generated")
            key = f"{status}_count"
            if key not in summary:
                key = "generated_count"
            summary[key] += 1
        public["dossier_summary"] = summary
    return public
