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
            "stale_count": 0,
            "analysis_ready": False,
            "blocking_participant_ids": [],
        }
        dossier_source = {
            "structure_revision_id": boundary_state.get("current_structure_revision_id"),
            "evidence_revision_id": evidence_revision_id,
            "boundary_revision_id": boundary_state.get("current_boundary_revision_id"),
            "boundary_payload_sha256": boundary_state.get("current_boundary_payload_sha256"),
            "coverage_revision_id": boundary_state.get("current_coverage_revision_id"),
            "coverage_payload_sha256": boundary_state.get("current_coverage_payload_sha256"),
        }
        current_dossier_versions: dict[str, str] = {}
        for participant in participants:
            participant_id = str(participant.get("participant_id") or "")
            current = store.load_current_participant_dossier(
                str(public.get("project_id") or ""),
                participant_id,
            )
            revision = (current or {}).get("revision") or {}
            if current is not None:
                current_dossier_versions[participant_id] = str(
                    (current.get("state") or {}).get("current_dossier_version_id")
                    or revision.get("dossier_version_id")
                    or ""
                )
            status = str(revision.get("status") or "not_generated")
            if current is not None and revision.get("source") != dossier_source:
                status = "stale"
            elif status not in {"not_generated", "generated", "approved", "needs_changes"}:
                status = "stale"
            key = f"{status}_count"
            if key not in summary:
                key = "generated_count"
            summary[key] += 1
            if status in {"not_generated", "needs_changes", "stale"}:
                summary["blocking_participant_ids"].append(participant_id)
        summary["blocking_participant_ids"].sort()
        summary["analysis_ready"] = bool(participants) and not summary[
            "blocking_participant_ids"
        ]
        public["dossier_summary"] = summary
        try:
            analysis = store.load_current_analysis_run(
                str(public.get("project_id") or "")
            )
            report = store.load_current_report_version(
                str(public.get("project_id") or "")
            )
        except (OSError, TypeError, ValueError) as exc:
            raise InterviewV2ImportError(
                status_code=500,
                code="REPORT_PERSISTENCE_FAILED",
                message="分析或报告状态读取失败，请稍后重试。",
                retryable=True,
                suggested_action="retry_report_request",
            ) from exc
        analysis_revision = (analysis or {}).get("revision") or {}
        analysis_status = str(analysis_revision.get("status") or "not_generated")
        if analysis and analysis_revision.get("source"):
            source = analysis_revision["source"]
            if any(
                source.get(key) != dossier_source.get(key)
                for key in dossier_source
            ):
                analysis_status = "stale"
            frozen_dossiers = source.get("dossier_versions") or []
            if (
                {str(item.get("participant_id") or "") for item in frozen_dossiers}
                != set(current_dossier_versions)
                or any(
                    current_dossier_versions.get(str(item.get("participant_id") or ""))
                    != str(item.get("dossier_version_id") or "")
                    for item in frozen_dossiers
                )
            ):
                analysis_status = "stale"
        report_revision = (report or {}).get("revision") or {}
        report_status = str(report_revision.get("status") or "not_generated")
        if report and (
            analysis_status != "completed"
            or (report_revision.get("source") or {}).get("analysis_run_id")
            != analysis_revision.get("analysis_run_id")
            or (report_revision.get("source") or {}).get("analysis_revision_payload_sha256")
            != analysis_revision.get("revision_payload_sha256")
        ):
            report_status = "stale"
        report_sections = list(report_revision.get("sections") or [])
        pending_reaudit_count = sum(
            1 for item in report_sections
            if item.get("audit_status") == "pending_reaudit"
        )
        audit_failed_count = sum(
            1 for item in report_sections
            if item.get("audit_status") == "audit_failed"
        )
        blocking_issue_count = sum(
            1 for item in report_revision.get("audit_issues") or []
            if item.get("severity") == "blocking"
        )
        public["analysis_summary"] = {
            "analysis_run_id": analysis_revision.get("analysis_run_id"),
            "status": analysis_status,
            "finding_count": len(analysis_revision.get("findings") or []),
            "report_ready": analysis_status == "completed",
        }
        public["report_summary"] = {
            "report_version_id": report_revision.get("report_version_id"),
            "status": report_status,
            "audit_status": report_revision.get("audit_status") or "not_generated",
            "pending_reaudit_count": pending_reaudit_count,
            "audit_failed_count": audit_failed_count,
            "blocking_issue_count": blocking_issue_count,
            "approval_ready": bool(report_sections)
            and report_status == "draft"
            and pending_reaudit_count == 0
            and audit_failed_count == 0
            and blocking_issue_count == 0
            and all(
                item.get("audit_status") == "audit_passed"
                for item in report_sections
            ),
        }
    return public
