"""Batch 5B evidence-bound report generation and retrieval."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from app.core.config import (
    INTERVIEW_V2_MODEL_FALLBACKS,
    INTERVIEW_V2_REPORT_AUDIT_MAX_TOKENS,
    INTERVIEW_V2_REPORT_AUDIT_MODEL,
    INTERVIEW_V2_REPORT_AUDIT_REASONING,
    INTERVIEW_V2_REPORT_MAX_TOKENS,
    INTERVIEW_V2_REPORT_MODEL,
    INTERVIEW_V2_REPORT_REASONING,
)
from app.core.interview_v2_report import (
    REPORT_SCHEMA_VERSION,
    InterviewV2ReportValidationError,
    build_report_input,
    validate_model_audit,
    validate_report_output,
)
from app.core.security import _owner_from_login, _visible_to_owner
from app.integrations.llm_client import collect_chat_completion
from app.services.interview_v2_analysis_service import get_current_analysis
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.storage import interview_v2_store as store
from app.storage.prompts import (
    _get_interview_v2_report_audit_system_prompt,
    _get_interview_v2_report_system_prompt,
    _load_prompts,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(
    code: str, message: str, *, status: int = 409, retryable: bool = False,
    context: dict[str, Any] | None = None,
) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=status,
        code=code,
        message=message,
        retryable=retryable,
        suggested_action="refresh_report_inputs" if status == 409 else "retry_report",
        context=context,
    )


def _parse_json(text: str, label: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise InterviewV2ReportValidationError(f"{label} did not return JSON")
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise InterviewV2ReportValidationError(f"{label} returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise InterviewV2ReportValidationError(f"{label} returned an invalid object")
    return value


def _prompt_snapshot() -> dict[str, Any]:
    catalog = _load_prompts()
    result: dict[str, Any] = {}
    for key in ("interview_v2_report_system", "interview_v2_report_audit_system"):
        entry = catalog[key]
        text = str(entry.get("current") or "")
        result[key] = {
            "version": int(entry.get("version") or 1),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    return result


def _frozen_config(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "research_focus": str(project.get("research_focus") or "").strip(),
        "writer": {
            "model": INTERVIEW_V2_REPORT_MODEL,
            "fallbacks": list(INTERVIEW_V2_MODEL_FALLBACKS),
            "reasoning_effort": INTERVIEW_V2_REPORT_REASONING,
            "max_tokens": INTERVIEW_V2_REPORT_MAX_TOKENS,
        },
        "audit": {
            "model": INTERVIEW_V2_REPORT_AUDIT_MODEL,
            "fallbacks": list(INTERVIEW_V2_MODEL_FALLBACKS),
            "reasoning_effort": INTERVIEW_V2_REPORT_AUDIT_REASONING,
            "max_tokens": INTERVIEW_V2_REPORT_AUDIT_MAX_TOKENS,
        },
        "prompts": _prompt_snapshot(),
    }


def _is_report_current(project_id: str, revision: dict[str, Any]) -> bool:
    current = store.load_current_analysis_run(project_id)
    source = revision.get("source") or {}
    return bool(
        current
        and current["revision"].get("analysis_run_id") == source.get("analysis_run_id")
        and current["revision"].get("revision_payload_sha256")
        == source.get("analysis_revision_payload_sha256")
        and current["revision"].get("status") == "completed"
    )


def _public(saved: dict[str, Any], *, stale: bool = False) -> dict[str, Any]:
    revision, state = saved["revision"], saved.get("state") or {}
    return {
        "project_id": revision.get("project_id"),
        "report_version_id": revision.get("report_version_id"),
        "report_version_number": revision.get("version_number", 0),
        "status": "stale" if stale else revision.get("status", "draft"),
        "audit_status": revision.get("audit_status", "audit_failed"),
        "source": revision.get("source") or {},
        "frozen_config": revision.get("frozen_config") or {},
        "sections": revision.get("sections") or [],
        "claims": revision.get("claims") or [],
        "audit_issues": revision.get("audit_issues") or [],
        "model_usage": revision.get("model_usage") or {},
        "is_current_version": state.get("current_report_version_id") == revision.get("report_version_id"),
    }


def _load_accessible_report(
    report_version_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        saved = store.load_report_version(report_version_id)
    except ValueError as exc:
        raise _error("REPORT_REQUEST_INVALID", "报告版本 ID 格式无效。", status=400) from exc
    except (OSError, TypeError) as exc:
        raise _error("REPORT_PERSISTENCE_FAILED", "报告版本读取失败。", status=500, retryable=True) from exc
    if saved is None:
        raise _error("INTERVIEW_REPORT_NOT_FOUND", "未找到该报告版本。", status=404)
    project = store.load_project(saved["project_id"])
    if project is None or not _visible_to_owner(project, login):
        raise _error("INTERVIEW_REPORT_NOT_FOUND", "未找到该报告版本。", status=404)
    return saved


def get_report(report_version_id: str, login: dict[str, Any] | None) -> dict[str, Any]:
    saved = _load_accessible_report(report_version_id, login)
    try:
        stale = not _is_report_current(saved["project_id"], saved["revision"])
    except (OSError, TypeError, ValueError) as exc:
        raise _error("REPORT_PERSISTENCE_FAILED", "报告上游状态读取失败。", status=500, retryable=True) from exc
    return _public(saved, stale=stale)


def get_report_claim(
    report_version_id: str, claim_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    saved = _load_accessible_report(report_version_id, login)
    claim = next(
        (item for item in saved["revision"].get("claims") or [] if item.get("claim_id") == claim_id),
        None,
    )
    if claim is None:
        raise _error("INTERVIEW_REPORT_CLAIM_NOT_FOUND", "未找到该报告主张。", status=404)
    findings_by_id = {
        item.get("finding_id"): item
        for item in saved["revision"].get("frozen_findings") or []
    }
    stats_by_id = {
        item.get("stat_fact_id"): item
        for item in saved["revision"].get("frozen_stat_facts") or []
    }
    stale = not _is_report_current(saved["project_id"], saved["revision"])
    return {
        "project_id": saved["project_id"],
        "report_version_id": report_version_id,
        "status": "stale" if stale else saved["revision"].get("status", "draft"),
        "claim": claim,
        "findings": [findings_by_id[item] for item in claim.get("finding_ids") or [] if item in findings_by_id],
        "stat_fact": stats_by_id.get(claim.get("stat_fact_id")),
        "audit_issues": [
            item for item in saved["revision"].get("audit_issues") or []
            if item.get("claim_id") == claim_id
        ],
    }


async def create_report(
    project_id: str, request: dict[str, Any], login: dict[str, Any] | None
) -> dict[str, Any]:
    analysis_public = get_current_analysis(project_id, login)
    if analysis_public.get("status") != "completed":
        raise _error("REPORT_INPUT_NOT_READY", "当前跨玩家分析不存在、未完成或已过期。")
    try:
        current_analysis = store.load_current_analysis_run(project_id)
        project = store.load_project(project_id)
    except (OSError, TypeError, ValueError) as exc:
        raise _error("REPORT_PERSISTENCE_FAILED", "报告输入读取失败。", status=500, retryable=True) from exc
    if current_analysis is None or project is None:
        raise _error("REPORT_INPUT_NOT_READY", "当前报告输入不完整。")
    try:
        report_input = build_report_input(
            project_id=project_id, project=project,
            analysis_revision=current_analysis["revision"],
        )
        frozen_config = _frozen_config(project)
    except (InterviewV2ReportValidationError, KeyError, TypeError, ValueError) as exc:
        raise _error("REPORT_INPUT_INVALID", str(exc), status=422) from exc

    report_version_id = f"report_{uuid4().hex}"
    writer_payload = {**report_input, "frozen_config": frozen_config}
    writer_text, writer_model = await collect_chat_completion(
        [
            {"role": "system", "content": _get_interview_v2_report_system_prompt()},
            {"role": "user", "content": "<untrusted_report_input>\n" + json.dumps(writer_payload, ensure_ascii=False) + "\n</untrusted_report_input>"},
        ],
        models=(INTERVIEW_V2_REPORT_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
        max_tokens=INTERVIEW_V2_REPORT_MAX_TOKENS,
        reasoning_effort=INTERVIEW_V2_REPORT_REASONING,
    )
    try:
        validated = validate_report_output(
            _parse_json(writer_text, "report writer"),
            report_input=report_input,
            report_version_id=report_version_id,
        )
    except InterviewV2ReportValidationError as exc:
        raise _error(
            "REPORT_MODEL_OUTPUT_INVALID", "报告写作模型输出未通过结构校验。",
            status=502, retryable=True, context={"reason": str(exc)},
        ) from exc

    audit_issues = list(validated["audit_issues"])
    audit_status = validated["audit_status"]
    audit_model = ""
    try:
        audit_payload = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "sections": validated["sections"],
            "claims": validated["claims"],
            "findings": report_input["findings"],
            "stat_facts": report_input["stat_facts"],
            "deterministic_issues": audit_issues,
        }
        audit_text, audit_model = await collect_chat_completion(
            [
                {"role": "system", "content": _get_interview_v2_report_audit_system_prompt()},
                {"role": "user", "content": "<untrusted_report_audit_input>\n" + json.dumps(audit_payload, ensure_ascii=False) + "\n</untrusted_report_audit_input>"},
            ],
            models=(INTERVIEW_V2_REPORT_AUDIT_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
            max_tokens=INTERVIEW_V2_REPORT_AUDIT_MAX_TOKENS,
            reasoning_effort=INTERVIEW_V2_REPORT_AUDIT_REASONING,
        )
        audit_issues.extend(validate_model_audit(
            _parse_json(audit_text, "report audit"),
            sections=validated["sections"], claims=validated["claims"],
        ))
        if any(item["severity"] == "blocking" for item in audit_issues):
            audit_status = "audit_failed"
    except Exception as exc:
        audit_status = "audit_failed"
        audit_issues.append({
            "audit_issue_id": f"audit_{uuid4().hex}",
            "code": "REPORT_AUDIT_INCOMPLETE",
            "severity": "blocking",
            "message": "补充审校未完成；该草稿不得批准或正式导出。",
            "section_key": "evidence_and_limitations",
            "claim_id": None,
            "source": "service",
            "context": {"error_type": type(exc).__name__},
        })

    source = {
        "analysis_run_id": current_analysis["revision"].get("analysis_run_id"),
        "analysis_revision_payload_sha256": current_analysis["revision"].get("revision_payload_sha256"),
        "analysis_input_fingerprint": current_analysis["revision"].get("input_fingerprint"),
        "analysis_source": current_analysis["revision"].get("source") or {},
    }
    revision = {
        "report_version_id": report_version_id,
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "source": source,
        "input_fingerprint": report_input["input_fingerprint"],
        "frozen_config": frozen_config,
        "status": "draft",
        "audit_status": audit_status,
        "sections": validated["sections"],
        "claims": validated["claims"],
        "audit_issues": audit_issues,
        "frozen_findings": report_input["findings"],
        "frozen_stat_facts": report_input["stat_facts"],
        "analysis_limitations": report_input["analysis_limitations"],
        "model_usage": {"writer_model": writer_model, "audit_model": audit_model},
        "created_at": _now(),
        "created_by": _owner_from_login(login).get("owner_key", ""),
    }
    try:
        saved = store.save_report_version_cas(
            project_id=project_id,
            base_report_version_id=request.get("base_report_version_id"),
            revision=revision,
        )
    except ValueError as exc:
        code = "REPORT_INPUT_CHANGED" if "input changed" in str(exc) else "REPORT_REVISION_CONFLICT"
        raise _error(
            code,
            "报告生成期间跨玩家分析已变化，请刷新后重试。"
            if code == "REPORT_INPUT_CHANGED" else "当前报告版本已变化，请刷新后重试。",
        ) from exc
    except (OSError, TypeError) as exc:
        raise _error("REPORT_PERSISTENCE_FAILED", "报告保存失败。", status=500, retryable=True) from exc
    return _public(saved)


__all__ = ["create_report", "get_report", "get_report_claim"]
