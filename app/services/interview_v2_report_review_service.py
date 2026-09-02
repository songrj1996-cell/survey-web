"""Batch 5C immutable report editing, re-audit and approval workflow."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
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
    REPORT_CLAIM_POLICY_VERSION,
    REPORT_SCHEMA_VERSION,
    REPORT_SECTION_SPECS,
    InterviewV2ReportValidationError,
    payload_sha256,
    validate_model_audit,
    validate_report_approval,
    validate_report_section_output,
)
from app.core.security import _owner_from_login, _visible_to_owner
from app.integrations.llm_client import collect_chat_completion
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_report_service import (
    _error,
    _is_report_current,
    _load_accessible_report,
    _parse_json,
    _public,
)
from app.storage import interview_v2_store as store
from app.storage.prompts import _load_prompts


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _derived_id(prefix: str, *parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def _report_input(revision: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_schema_version": revision.get("report_schema_version") or REPORT_SCHEMA_VERSION,
        "claim_policy_version": REPORT_CLAIM_POLICY_VERSION,
        "project_id": revision.get("project_id"),
        "analysis_run_id": (revision.get("source") or {}).get("analysis_run_id"),
        "analysis_revision_payload_sha256": (revision.get("source") or {}).get(
            "analysis_revision_payload_sha256"
        ),
        "analysis_source": (revision.get("source") or {}).get("analysis_source") or {},
        "research_focus": (revision.get("frozen_config") or {}).get("research_focus", ""),
        "section_specs": [
            {"section_key": key, "title": title, "order": index + 1}
            for index, (key, title) in enumerate(REPORT_SECTION_SPECS)
        ],
        "findings": deepcopy(revision.get("frozen_findings") or []),
        "stat_facts": deepcopy(revision.get("frozen_stat_facts") or []),
        "analysis_limitations": deepcopy(revision.get("analysis_limitations") or []),
        "input_fingerprint": revision.get("input_fingerprint"),
    }


def _retarget_revision(
    revision: dict[str, Any], *, report_version_id: str, actor: str, action: str
) -> dict[str, Any]:
    durable = deepcopy(revision)
    previous_id = str(durable.get("report_version_id") or "")
    durable.pop("revision_payload_sha256", None)
    durable.pop("version_number", None)
    durable["report_version_id"] = report_version_id
    durable["previous_report_version_id"] = previous_id
    durable["created_at"] = _now()
    durable["created_by"] = actor
    durable["revision_action"] = action
    claims_by_id = {
        str(claim.get("claim_id") or ""): claim
        for claim in durable.get("claims") or []
    }
    claim_id_map: dict[str, str] = {}
    for section in durable.get("sections") or []:
        for index, claim_id in enumerate(section.get("claim_ids") or []):
            claim = claims_by_id.get(str(claim_id))
            if claim is None:
                continue
            claim_id_map[str(claim_id)] = _derived_id(
                "claim",
                report_version_id,
                section.get("section_key"),
                index,
                claim.get("text"),
            )
    for claim_id, claim in claims_by_id.items():
        claim_id_map.setdefault(
            claim_id,
            _derived_id(
                "claim", report_version_id, claim_id, claim.get("content_sha256")
            ),
        )
    for section in durable.get("sections") or []:
        section["report_version_id"] = report_version_id
        section["claim_ids"] = [
            claim_id_map.get(str(item), str(item)) for item in section.get("claim_ids") or []
        ]
    for claim in durable.get("claims") or []:
        old_id = str(claim.get("claim_id") or "")
        claim["claim_id"] = claim_id_map.get(old_id, old_id)
        claim["report_version_id"] = report_version_id
        if claim.get("superseded_by"):
            claim["superseded_by"] = claim_id_map.get(
                str(claim.get("superseded_by")), claim.get("superseded_by")
            )
    for issue in durable.get("audit_issues") or []:
        if issue.get("claim_id"):
            issue["claim_id"] = claim_id_map.get(
                str(issue.get("claim_id")), issue.get("claim_id")
            )
    return durable


def _load_current_section(
    section_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    try:
        saved = store.load_current_report_for_section(section_id)
    except ValueError as exc:
        raise _error("REPORT_SECTION_NOT_FOUND", "未找到该报告章节。", status=404) from exc
    except (OSError, TypeError) as exc:
        raise _error(
            "REPORT_PERSISTENCE_FAILED", "报告章节读取失败。", status=500, retryable=True
        ) from exc
    if saved is None:
        raise _error("REPORT_SECTION_NOT_FOUND", "未找到该报告章节。", status=404)
    project = store.load_project(saved["project_id"])
    if project is None or not _visible_to_owner(project, login):
        raise _error("REPORT_SECTION_NOT_FOUND", "未找到该报告章节。", status=404)
    try:
        if not _is_report_current(saved["project_id"], saved["revision"]):
            raise _error(
                "REPORT_INPUT_CHANGED", "报告引用的跨玩家分析已变化，当前版本只能查看。"
            )
    except InterviewV2ImportError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "REPORT_PERSISTENCE_FAILED", "报告上游状态读取失败。", status=500, retryable=True
        ) from exc
    if saved.get("section_locator_missing"):
        try:
            store.ensure_current_report_section_locator(
                project_id=saved["project_id"],
                report_version_id=str(
                    saved["revision"].get("report_version_id") or ""
                ),
                section_id=section_id,
            )
        except getattr(store, "ReportHeadConflictError", ()) as exc:
            raise _error(
                "REPORT_REVISION_CONFLICT", "当前报告版本已变化，请刷新后重试。"
            ) from exc
        except (OSError, TypeError, ValueError) as exc:
            raise _error(
                "REPORT_PERSISTENCE_FAILED", "报告章节索引恢复失败。",
                status=500, retryable=True,
            ) from exc
    return saved


def _section_public(saved: dict[str, Any], section_id: str) -> dict[str, Any]:
    section = next(
        item for item in saved["revision"].get("sections") or []
        if item.get("section_id") == section_id
    )
    return {
        "project_id": saved["project_id"],
        "report_version_id": saved["revision"].get("report_version_id"),
        "report_version_number": saved["revision"].get("version_number", 0),
        "section_id": section_id,
        "section_revision": section.get("section_revision", 1),
        "status": saved["revision"].get("status", "draft"),
        "audit_status": section.get("audit_status", "pending_reaudit"),
        "locked": bool(section.get("locked")),
        "reaudit_job_id": section.get("reaudit_job_id"),
        "content_sha256": section.get("content_sha256"),
    }


def _save_mutation(
    *, project_id: str, base_report_version_id: str, section_id: str | None,
    base_section_revision: int | None, revision: dict[str, Any]
) -> dict[str, Any]:
    try:
        return store.save_report_version_cas(
            project_id=project_id,
            base_report_version_id=base_report_version_id,
            section_id=section_id,
            base_section_revision=base_section_revision,
            revision=revision,
        )
    except getattr(store, "ReportInputConflictError", ()) as exc:
        raise _error("REPORT_INPUT_CHANGED", "报告引用的跨玩家分析已变化，请重新生成。") from exc
    except getattr(store, "ReportSectionRevisionConflictError", ()) as exc:
        raise _error(
            "REPORT_SECTION_REVISION_CONFLICT", "章节已被其他修改更新，请刷新后合并。"
        ) from exc
    except getattr(store, "ReportLockedSectionConflictError", ()) as exc:
        raise _error(
            "REPORT_LOCKED_SECTIONS_PRESENT",
            "当前报告包含人工锁定章节，不能覆盖其正文。",
            context={"section_ids": exc.section_ids},
        ) from exc
    except getattr(store, "ReportHeadConflictError", ()) as exc:
        raise _error("REPORT_REVISION_CONFLICT", "当前报告版本已变化，请刷新后重试。") from exc
    except ValueError as exc:
        message = str(exc)
        if "section" in message and "revision" in message:
            code, text = "REPORT_SECTION_REVISION_CONFLICT", "章节已被其他修改更新，请刷新后合并。"
        elif "input changed" in message:
            code, text = "REPORT_INPUT_CHANGED", "报告引用的跨玩家分析已变化，请重新生成。"
        else:
            code, text = "REPORT_REVISION_CONFLICT", "当前报告版本已变化，请刷新后重试。"
        raise _error(code, text) from exc
    except (OSError, TypeError) as exc:
        raise _error("REPORT_PERSISTENCE_FAILED", "报告版本保存失败。", status=500, retryable=True) from exc


def edit_report_section(
    section_id: str, request: dict[str, Any], login: dict[str, Any] | None
) -> dict[str, Any]:
    saved = _load_current_section(section_id, login)
    current = saved["revision"]
    section = saved["section"]
    base_revision = int(request.get("base_section_revision") or 0)
    if int(section.get("section_revision") or 1) != base_revision:
        raise _error(
            "REPORT_SECTION_REVISION_CONFLICT", "章节已被其他修改更新，请刷新后合并。"
        )
    content = str(request.get("content") or "")
    if not content.strip():
        raise _error("REPORT_REQUEST_INVALID", "章节正文不能为空。", status=400)
    actor = _owner_from_login(login).get("owner_key", "")
    next_id = f"report_{uuid4().hex}"
    old_claim_ids = [
        str(item.get("claim_id") or "")
        for item in current.get("claims") or []
        if item.get("section_id") == section_id
    ]
    next_revision = _retarget_revision(
        current, report_version_id=next_id, actor=actor, action="section_edit"
    )
    next_revision["claims"] = [
        item for item in next_revision.get("claims") or []
        if item.get("section_id") != section_id
    ]
    next_revision["audit_issues"] = [
        item for item in next_revision.get("audit_issues") or []
        if item.get("section_key") != section.get("section_key")
    ]
    job_id = f"job_{uuid4().hex}"
    updated_section = next(
        item for item in next_revision["sections"] if item.get("section_id") == section_id
    )
    updated_section.update({
        "section_revision": base_revision + 1,
        "content": content,
        "content_sha256": payload_sha256(content),
        "claim_ids": [],
        "locked": True,
        "audit_status": "pending_reaudit",
        "reaudit_job_id": job_id,
        "edited_by": actor,
        "edited_at": _now(),
        "edit_reason": str(request.get("edit_reason") or "").strip(),
    })
    pending_issue = {
        "audit_issue_id": f"audit_{uuid4().hex}",
        "code": "REPORT_CLAIMS_PENDING_AUDIT",
        "severity": "blocking",
        "message": "人工编辑后的正文尚未重新提取主张和完成审计。",
        "section_key": section.get("section_key"),
        "claim_id": None,
        "source": "service",
    }
    next_revision["audit_issues"].append(pending_issue)
    next_revision["superseded_claim_ids"] = sorted(set(
        [str(item) for item in next_revision.get("superseded_claim_ids") or []]
        + old_claim_ids
    ))
    next_revision["status"] = "draft"
    next_revision["audit_status"] = "pending_reaudit"
    next_revision.pop("approved_by", None)
    next_revision.pop("approved_at", None)
    saved_next = _save_mutation(
        project_id=saved["project_id"],
        base_report_version_id=str(current.get("report_version_id") or ""),
        section_id=section_id,
        base_section_revision=base_revision,
        revision=next_revision,
    )
    return _section_public({**saved_next, "project_id": saved["project_id"]}, section_id)


def _reaudit_prompt_snapshot() -> tuple[dict[str, str], dict[str, Any]]:
    catalog = _load_prompts()
    prompt_texts: dict[str, str] = {}
    snapshot: dict[str, Any] = {}
    for key in (
        "interview_v2_report_claim_extract_system",
        "interview_v2_report_audit_system",
    ):
        entry = catalog[key]
        content = str(entry.get("current") or "")
        if not content.strip():
            raise ValueError(f"report prompt {key} is empty")
        prompt_texts[key] = content
        snapshot[key] = {
            "version": int(entry.get("version") or 1),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    return prompt_texts, snapshot


def _apply_section_result(
    revision: dict[str, Any], *, section_id: str, result: dict[str, Any]
) -> None:
    section_key = result["section"]["section_key"]
    revision["sections"] = [
        result["section"] if item.get("section_id") == section_id else item
        for item in revision.get("sections") or []
    ]
    revision["claims"] = [
        item for item in revision.get("claims") or []
        if item.get("section_id") != section_id
    ] + list(result.get("claims") or [])
    revision["audit_issues"] = [
        item for item in revision.get("audit_issues") or []
        if item.get("section_key") != section_key
    ] + list(result.get("audit_issues") or [])
    blocking_by_section = {
        str(item.get("section_key") or "")
        for item in revision["audit_issues"] if item.get("severity") == "blocking"
    }
    for section in revision["sections"]:
        if (
            section.get("section_key") in blocking_by_section
            and section.get("audit_status") != "pending_reaudit"
        ):
            section["audit_status"] = "audit_failed"
    statuses = {str(item.get("audit_status") or "") for item in revision["sections"]}
    if "pending_reaudit" in statuses:
        revision["audit_status"] = "pending_reaudit"
    elif "audit_failed" in statuses or blocking_by_section:
        revision["audit_status"] = "audit_failed"
    else:
        revision["audit_status"] = "audited"


async def reaudit_report_section(
    section_id: str, request: dict[str, Any], login: dict[str, Any] | None
) -> dict[str, Any]:
    saved = _load_current_section(section_id, login)
    current, section = saved["revision"], saved["section"]
    if current.get("status") == "approved":
        raise _error("REPORT_ALREADY_APPROVED", "已批准报告不可重新审计。")
    base_revision = int(request.get("base_section_revision") or 0)
    if int(section.get("section_revision") or 1) != base_revision:
        raise _error(
            "REPORT_SECTION_REVISION_CONFLICT", "章节已被其他修改更新，请刷新后重试。"
        )
    if (
        section.get("audit_status") != "pending_reaudit"
        or section.get("reaudit_job_id") != request.get("reaudit_job_id")
    ):
        raise _error(
            "REPORT_SECTION_REVISION_CONFLICT", "重审任务已失效或不属于当前章节修订。"
        )
    actor = _owner_from_login(login).get("owner_key", "")
    next_id = f"report_{uuid4().hex}"
    next_revision = _retarget_revision(
        current, report_version_id=next_id, actor=actor, action="section_reaudit"
    )
    report_input = _report_input(current)
    target_revision = base_revision + 1
    result: dict[str, Any]
    extract_model = ""
    audit_model = ""
    prompt_snapshot: dict[str, Any] = {}
    try:
        prompt_texts, prompt_snapshot = _reaudit_prompt_snapshot()
        extract_payload = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "section_key": section.get("section_key"),
            "content": section.get("content"),
            "findings": report_input["findings"],
            "stat_facts": report_input["stat_facts"],
            "instruction": "只登记正文中的主张和引用，不得改写 content。",
        }
        extract_text, extract_model = await collect_chat_completion(
            [
                {
                    "role": "system",
                    "content": prompt_texts[
                        "interview_v2_report_claim_extract_system"
                    ],
                },
                {"role": "user", "content": "<untrusted_edited_section>\n" + json.dumps(extract_payload, ensure_ascii=False) + "\n</untrusted_edited_section>"},
            ],
            models=(INTERVIEW_V2_REPORT_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
            max_tokens=INTERVIEW_V2_REPORT_MAX_TOKENS,
            reasoning_effort=INTERVIEW_V2_REPORT_REASONING,
        )
        validated = validate_report_section_output(
            _parse_json(extract_text, "report claim extractor"),
            content=str(section.get("content") or ""),
            report_input=report_input,
            report_version_id=next_id,
            section_id=section_id,
            section_key=str(section.get("section_key") or ""),
            section_revision=target_revision,
            locked=True,
        )
        for field in (
            "edited_by", "edited_at", "edit_reason", "reaudit_retry_count",
        ):
            if field in section:
                validated["section"][field] = section[field]
        issues = list(validated["audit_issues"])
        audit_payload = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "sections": [validated["section"]],
            "claims": validated["claims"],
            "findings": report_input["findings"],
            "stat_facts": report_input["stat_facts"],
            "deterministic_issues": issues,
        }
        audit_text, audit_model = await collect_chat_completion(
            [
                {
                    "role": "system",
                    "content": prompt_texts[
                        "interview_v2_report_audit_system"
                    ],
                },
                {"role": "user", "content": "<untrusted_report_audit_input>\n" + json.dumps(audit_payload, ensure_ascii=False) + "\n</untrusted_report_audit_input>"},
            ],
            models=(INTERVIEW_V2_REPORT_AUDIT_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
            max_tokens=INTERVIEW_V2_REPORT_AUDIT_MAX_TOKENS,
            reasoning_effort=INTERVIEW_V2_REPORT_AUDIT_REASONING,
        )
        issues.extend(validate_model_audit(
            _parse_json(audit_text, "report section audit"),
            sections=[validated["section"]], claims=validated["claims"],
        ))
        blockers = [item for item in issues if item.get("severity") == "blocking"]
        validated["section"]["audit_status"] = "audit_failed" if blockers else "audit_passed"
        validated["section"]["reaudit_job_id"] = None
        validated["section"]["last_reaudit_job_id"] = request.get("reaudit_job_id")
        for claim in validated["claims"]:
            failed = any(
                item.get("claim_id") in {None, claim.get("claim_id")}
                for item in blockers
            )
            claim["audit_status"] = "audit_failed" if failed else "audit_passed"
            if failed:
                claim["qualification_status"] = "failed"
        result = {
            "section": validated["section"], "claims": validated["claims"],
            "audit_issues": issues,
        }
    except Exception as exc:
        failed_section = deepcopy(section)
        failed_section.update({
            "report_version_id": next_id,
            "section_revision": target_revision,
            "audit_status": "pending_reaudit",
            "claim_ids": [],
            "locked": True,
            "reaudit_job_id": request.get("reaudit_job_id"),
            "last_reaudit_error_type": type(exc).__name__,
            "last_reaudit_attempt_at": _now(),
            "reaudit_retry_count": int(section.get("reaudit_retry_count") or 0) + 1,
        })
        result = {
            "section": failed_section,
            "claims": [],
            "audit_issues": [{
                "audit_issue_id": f"audit_{uuid4().hex}",
                "code": "REPORT_AUDIT_INCOMPLETE",
                "severity": "blocking",
                "message": "章节主张重提取或补充审校未完成；该报告不得批准。",
                "section_key": section.get("section_key"),
                "claim_id": None,
                "source": "service",
                "context": {"error_type": type(exc).__name__},
            }],
        }
    result["section"]["audit_input_fingerprint"] = payload_sha256({
        "content_sha256": result["section"].get("content_sha256"),
        "section_revision": target_revision,
        "claim_hashes": [item.get("content_sha256") for item in result.get("claims") or []],
        "stat_facts_sha256": payload_sha256(report_input["stat_facts"]),
        "prompts": prompt_snapshot,
    })
    result["section"]["reaudited_by"] = actor
    result["section"]["reaudited_at"] = _now()
    result["section"]["claim_ids"] = [item.get("claim_id") for item in result.get("claims") or []]
    _apply_section_result(next_revision, section_id=section_id, result=result)
    next_revision["status"] = "draft"
    usage = deepcopy(next_revision.get("model_usage") or {})
    reaudits = list(usage.get("reaudits") or [])
    reaudits.append({
        "reaudit_job_id": request.get("reaudit_job_id"),
        "section_id": section_id,
        "section_revision": target_revision,
        "extract_model": extract_model,
        "audit_model": audit_model,
        "input_fingerprint": result["section"]["audit_input_fingerprint"],
    })
    usage["reaudits"] = reaudits
    next_revision["model_usage"] = usage
    saved_next = _save_mutation(
        project_id=saved["project_id"],
        base_report_version_id=str(current.get("report_version_id") or ""),
        section_id=section_id,
        base_section_revision=base_revision,
        revision=next_revision,
    )
    return _section_public({**saved_next, "project_id": saved["project_id"]}, section_id)


def approve_report(
    report_version_id: str, request: dict[str, Any], login: dict[str, Any] | None
) -> dict[str, Any]:
    if request.get("base_report_version_id") != report_version_id:
        raise _error("REPORT_REVISION_CONFLICT", "批准基准报告与路径版本不一致。")
    saved = _load_accessible_report(report_version_id, login)
    current_id = (saved.get("state") or {}).get("current_report_version_id")
    if current_id != report_version_id:
        raise _error("REPORT_REVISION_CONFLICT", "该报告已不是当前版本，请刷新后重试。")
    if saved["revision"].get("status") == "approved":
        raise _error("REPORT_ALREADY_APPROVED", "该报告已经批准。")
    if not _is_report_current(saved["project_id"], saved["revision"]):
        raise _error("REPORT_INPUT_CHANGED", "报告引用的跨玩家分析已变化，不能批准。")
    try:
        validate_report_approval(saved["revision"], report_input=_report_input(saved["revision"]))
    except InterviewV2ReportValidationError as exc:
        message = str(exc)
        code = (
            "REPORT_CLAIMS_PENDING_AUDIT"
            if "pending" in message.lower()
            else "REPORT_AUDIT_BLOCKED"
        )
        raise _error(code, "报告仍有未完成或未通过的审计，不能批准。") from exc
    actor = _owner_from_login(login).get("owner_key", "")
    next_id = f"report_{uuid4().hex}"
    next_revision = _retarget_revision(
        saved["revision"], report_version_id=next_id, actor=actor, action="approval"
    )
    next_revision["status"] = "approved"
    next_revision["audit_status"] = "audited"
    next_revision["approved_by"] = actor
    next_revision["approved_at"] = _now()
    next_revision["approval_note"] = str(request.get("note") or "").strip()
    next_revision["approved_from_report_version_id"] = report_version_id
    try:
        validate_report_approval(
            next_revision, report_input=_report_input(next_revision)
        )
    except InterviewV2ReportValidationError as exc:
        raise _error(
            "REPORT_AUDIT_BLOCKED",
            "批准版本转换后未通过确定性复核，未保存该版本。",
        ) from exc
    saved_next = _save_mutation(
        project_id=saved["project_id"],
        base_report_version_id=report_version_id,
        section_id=None,
        base_section_revision=None,
        revision=next_revision,
    )
    return _public(saved_next)


__all__ = ["approve_report", "edit_report_section", "reaudit_report_section"]
