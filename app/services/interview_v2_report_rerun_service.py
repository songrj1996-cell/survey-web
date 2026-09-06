"""Batch 6B1 idempotent single-section report regeneration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any

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
    build_report_section_rerun_input,
    payload_sha256,
    validate_model_audit,
    validate_report_section_rerun_output,
)
from app.core.security import _owner_from_login
from app.integrations.llm_client import collect_chat_completion
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_report_review_service import (
    _apply_section_result,
    _retarget_revision,
)
from app.services.interview_v2_report_service import (
    _error,
    _is_report_current,
    _load_accessible_report,
    _parse_json,
    _public,
)
from app.storage import interview_v2_store as store
from app.storage.prompts import _load_prompts


_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _derived_id(prefix: str, *parts: object) -> str:
    encoded = json.dumps(
        parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _operation_owner(login: dict[str, Any] | None) -> str:
    return str(_owner_from_login(login).get("owner_key") or "local:anonymous")


def validate_report_rerun_idempotency_key(value: str) -> str:
    key = str(value or "").strip()
    if not _IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise _error(
            "RERUN_IDEMPOTENCY_KEY_INVALID",
            "请求缺少有效的 Idempotency-Key。",
            status=400,
        )
    return key


def _load_prompt_bundle() -> tuple[dict[str, str], dict[str, Any]]:
    catalog = _load_prompts()
    texts: dict[str, str] = {}
    snapshot: dict[str, Any] = {}
    for key in (
        "interview_v2_report_section_rerun_system",
        "interview_v2_report_audit_system",
    ):
        entry = catalog.get(key)
        if not isinstance(entry, dict):
            raise ValueError(f"report rerun prompt {key} is missing")
        content = str(entry.get("current") or "")
        if not content.strip():
            raise ValueError(f"report rerun prompt {key} is empty")
        texts[key] = content
        snapshot[key] = {
            "version": int(entry.get("version") or 1),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    return texts, snapshot


def _load_rerun_base(
    project_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    saved = _load_accessible_report(
        str(request.get("base_report_version_id") or ""), login
    )
    if saved.get("project_id") != project_id:
        raise _error(
            "INTERVIEW_REPORT_NOT_FOUND", "未找到该报告版本。", status=404
        )
    sections = [
        item
        for item in saved["revision"].get("sections") or []
        if isinstance(item, dict)
        and item.get("section_id") == request.get("section_id")
    ]
    if len(sections) != 1:
        raise _error(
            "REPORT_SECTION_NOT_FOUND", "未找到该报告章节。", status=404
        )
    section = sections[0]
    if int(section.get("section_revision") or 0) != int(
        request.get("base_section_revision") or 0
    ):
        raise _error(
            "REPORT_SECTION_REVISION_CONFLICT",
            "章节已被其他修改更新，请刷新后重试。",
        )
    if bool(section.get("locked")):
        raise _error(
            "REPORT_SECTION_LOCKED",
            "人工锁定章节不能由模型重生成，请先保留该人工版本。",
        )
    return saved, section


def validate_report_rerun_access(
    project_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> None:
    """Authorize the report and project before resolving the user's LLM key."""

    _load_rerun_base(project_id, request, login)


def _require_rerun_input_current(
    project_id: str, saved: dict[str, Any]
) -> None:
    try:
        if not _is_report_current(project_id, saved["revision"]):
            raise _error(
                "REPORT_INPUT_CHANGED",
                "报告引用的跨玩家分析已变化，请按最新分析重新生成。",
            )
    except InterviewV2ImportError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "REPORT_PERSISTENCE_FAILED",
            "报告上游状态读取失败。",
            status=500,
            retryable=True,
        ) from exc


def _rerun_public(
    saved: dict[str, Any], operation: dict[str, Any], *, reused: bool
) -> dict[str, Any]:
    return {
        **_public(saved),
        "rerun": {
            "rerun_id": operation.get("rerun_id"),
            "status": operation.get("status"),
            "reused": reused,
            "from_stage": "report_section",
            "base_report_version_id": operation.get(
                "base_report_version_id"
            ),
            "report_version_id": operation.get("report_version_id"),
            "section_id": operation.get("section_id"),
            "base_section_revision": operation.get("base_section_revision"),
            "input_fingerprint": operation.get("request_fingerprint"),
        },
    }


def _release_failed_claim(
    *,
    owner_key: str,
    project_id: str,
    idempotency_key: str,
    request_fingerprint: str,
    report_version_id: str,
) -> None:
    try:
        store.release_report_rerun_operation(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "REPORT_PERSISTENCE_FAILED",
            "章节重生成失败，且幂等占位清理未完成。",
            status=500,
            retryable=True,
        ) from exc


def _apply_audit(
    validated: dict[str, Any], model_issues: list[dict[str, Any]]
) -> dict[str, Any]:
    issues = list(validated.get("audit_issues") or []) + list(model_issues)
    blockers = [item for item in issues if item.get("severity") == "blocking"]
    section = validated["section"]
    section["audit_status"] = "audit_failed" if blockers else "audit_passed"
    for claim in validated.get("claims") or []:
        failed = any(
            issue.get("claim_id") in {None, claim.get("claim_id")}
            for issue in blockers
        )
        claim["audit_status"] = "audit_failed" if failed else "audit_passed"
        claim["qualification_status"] = "failed" if failed else "passed"
    return {
        "section": section,
        "claims": validated.get("claims") or [],
        "audit_issues": issues,
    }


async def create_report_section_rerun(
    project_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
    idempotency_key: str,
) -> dict[str, Any]:
    key = validate_report_rerun_idempotency_key(idempotency_key)
    saved, section = _load_rerun_base(project_id, request, login)
    current = saved["revision"]
    try:
        prompt_texts, prompt_snapshot = _load_prompt_bundle()
        rerun_input = build_report_section_rerun_input(
            report_revision=current,
            section_id=str(request.get("section_id") or ""),
            instruction=str(request.get("instruction") or ""),
            prompt_snapshot=prompt_snapshot,
        )
    except (InterviewV2ReportValidationError, KeyError, TypeError, ValueError) as exc:
        raise _error(
            "REPORT_RERUN_INPUT_INVALID", str(exc), status=422
        ) from exc

    owner_key = _operation_owner(login)
    request_fingerprint = str(rerun_input["input_fingerprint"])
    operation_seed = payload_sha256({
        "owner_key": owner_key,
        "project_id": project_id,
        "idempotency_key": key,
    })
    rerun_id = _derived_id("rerun", operation_seed)
    report_version_id = _derived_id("report", operation_seed)
    try:
        operation = store.claim_report_rerun_operation(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            rerun_id=rerun_id,
            report_version_id=report_version_id,
            base_report_version_id=str(current.get("report_version_id") or ""),
            section_id=str(section.get("section_id") or ""),
            base_section_revision=int(section.get("section_revision") or 0),
            created_at=_now(),
        )
    except store.ReportRerunIdempotencyConflictError as exc:
        raise _error(
            "RERUN_IDEMPOTENCY_CONFLICT",
            "该 Idempotency-Key 已用于不同的章节重生成输入。",
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "REPORT_PERSISTENCE_FAILED",
            "章节重生成幂等状态保存失败。",
            status=500,
            retryable=True,
        ) from exc

    if operation.get("status") == "completed":
        completed = _load_accessible_report(report_version_id, login)
        return _rerun_public(completed, operation, reused=True)
    if not operation.get("_claim_acquired"):
        raise _error(
            "RERUN_IN_PROGRESS",
            "相同的章节重生成请求正在处理中，请稍后使用同一 Idempotency-Key 重试。",
            retryable=True,
        )

    try:
        _require_rerun_input_current(project_id, saved)
    except InterviewV2ImportError:
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise

    try:
        head = store.load_current_report_version(project_id)
    except (OSError, TypeError, ValueError) as exc:
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise _error(
            "REPORT_PERSISTENCE_FAILED",
            "当前报告版本读取失败。",
            status=500,
            retryable=True,
        ) from exc
    if (
        head is None
        or head["revision"].get("report_version_id")
        != current.get("report_version_id")
    ):
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise _error(
            "REPORT_REVISION_CONFLICT",
            "当前报告版本已变化，请刷新后重试。",
        )

    writer_model = ""
    audit_model = ""
    try:
        writer_text, writer_model = await collect_chat_completion(
            [
                {
                    "role": "system",
                    "content": prompt_texts[
                        "interview_v2_report_section_rerun_system"
                    ],
                },
                {
                    "role": "user",
                    "content": (
                        "<untrusted_report_section_rerun_input>\n"
                        + json.dumps(rerun_input, ensure_ascii=False)
                        + "\n</untrusted_report_section_rerun_input>"
                    ),
                },
            ],
            models=(INTERVIEW_V2_REPORT_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
            max_tokens=INTERVIEW_V2_REPORT_MAX_TOKENS,
            reasoning_effort=INTERVIEW_V2_REPORT_REASONING,
        )
        validated = validate_report_section_rerun_output(
            _parse_json(writer_text, "report section rerun writer"),
            rerun_input=rerun_input,
            report_version_id=report_version_id,
        )
    except asyncio.CancelledError:
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise
    except InterviewV2ReportValidationError as exc:
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise _error(
            "REPORT_RERUN_MODEL_OUTPUT_INVALID",
            "章节重写模型输出未通过结构校验，未创建新版本。",
            status=502,
            retryable=True,
            context={"reason": str(exc)},
        ) from exc
    except Exception as exc:
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise _error(
            "REPORT_RERUN_FAILED",
            "章节重写请求未完成，未创建新版本。",
            status=502,
            retryable=True,
            context={"error_type": type(exc).__name__},
        ) from exc

    model_issues: list[dict[str, Any]] = []
    try:
        audit_payload = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "sections": [validated["section"]],
            "claims": validated["claims"],
            "findings": rerun_input["report_input"]["findings"],
            "stat_facts": rerun_input["report_input"]["stat_facts"],
            "deterministic_issues": validated["audit_issues"],
        }
        audit_text, audit_model = await collect_chat_completion(
            [
                {
                    "role": "system",
                    "content": prompt_texts["interview_v2_report_audit_system"],
                },
                {
                    "role": "user",
                    "content": (
                        "<untrusted_report_audit_input>\n"
                        + json.dumps(audit_payload, ensure_ascii=False)
                        + "\n</untrusted_report_audit_input>"
                    ),
                },
            ],
            models=(
                INTERVIEW_V2_REPORT_AUDIT_MODEL,
                *INTERVIEW_V2_MODEL_FALLBACKS,
            ),
            max_tokens=INTERVIEW_V2_REPORT_AUDIT_MAX_TOKENS,
            reasoning_effort=INTERVIEW_V2_REPORT_AUDIT_REASONING,
        )
        model_issues = validate_model_audit(
            _parse_json(audit_text, "report section rerun audit"),
            sections=[validated["section"]],
            claims=validated["claims"],
        )
    except asyncio.CancelledError:
        _release_failed_claim(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
        )
        raise
    except Exception as exc:
        model_issues = [{
            "audit_issue_id": _derived_id(
                "audit",
                rerun_id,
                "REPORT_AUDIT_INCOMPLETE",
                section.get("section_key"),
            ),
            "code": "REPORT_AUDIT_INCOMPLETE",
            "severity": "blocking",
            "message": "补充审校未完成；该草稿不得批准或正式导出。",
            "section_key": section.get("section_key"),
            "claim_id": None,
            "source": "service",
            "context": {"error_type": type(exc).__name__},
        }]

    result = _apply_audit(validated, model_issues)
    actor = str(_owner_from_login(login).get("owner_key") or "")
    result["section"].update({
        "locked": False,
        "reaudit_job_id": None,
        "rerun_id": rerun_id,
        "rerun_input_fingerprint": request_fingerprint,
        "rerun_by": actor,
        "rerun_at": _now(),
    })
    old_claim_ids = [
        str(item.get("claim_id") or "")
        for item in current.get("claims") or []
        if item.get("section_id") == section.get("section_id")
    ]
    next_revision = _retarget_revision(
        current,
        report_version_id=report_version_id,
        actor=actor,
        action="section_rerun",
    )
    _apply_section_result(
        next_revision,
        section_id=str(section.get("section_id") or ""),
        result=result,
    )
    next_revision["status"] = "draft"
    for field in (
        "approved_by",
        "approved_at",
        "approval_note",
        "approved_from_report_version_id",
    ):
        next_revision.pop(field, None)
    next_revision["superseded_claim_ids"] = sorted(set(
        [str(item) for item in next_revision.get("superseded_claim_ids") or []]
        + old_claim_ids
    ))
    usage = deepcopy(next_revision.get("model_usage") or {})
    section_reruns = list(usage.get("section_reruns") or [])
    section_reruns.append({
        "rerun_id": rerun_id,
        "section_id": section.get("section_id"),
        "base_section_revision": section.get("section_revision"),
        "section_revision": result["section"].get("section_revision"),
        "writer_model": writer_model,
        "audit_model": audit_model,
        "input_fingerprint": request_fingerprint,
        "instruction_sha256": payload_sha256(
            str(request.get("instruction") or "")
        ),
        "prompts": prompt_snapshot,
    })
    usage["section_reruns"] = section_reruns
    next_revision["model_usage"] = usage

    report_committed = False
    try:
        saved_next = store.save_report_version_cas(
            project_id=project_id,
            base_report_version_id=str(current.get("report_version_id") or ""),
            section_id=str(section.get("section_id") or ""),
            base_section_revision=int(section.get("section_revision") or 0),
            revision=next_revision,
        )
        report_committed = True
        completed = store.complete_report_rerun_operation(
            owner_key=owner_key,
            project_id=project_id,
            idempotency_key=key,
            request_fingerprint=request_fingerprint,
            report_version_id=report_version_id,
            completed_at=_now(),
        )
    except store.ReportInputConflictError as exc:
        raise _error(
            "REPORT_INPUT_CHANGED",
            "章节重生成期间跨玩家分析已变化，请刷新后重试。",
        ) from exc
    except store.ReportSectionRevisionConflictError as exc:
        raise _error(
            "REPORT_SECTION_REVISION_CONFLICT",
            "章节已被其他修改更新，请刷新后重试。",
        ) from exc
    except store.ReportHeadConflictError as exc:
        raise _error(
            "REPORT_REVISION_CONFLICT",
            "当前报告版本已变化，请刷新后重试。",
        ) from exc
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "REPORT_PERSISTENCE_FAILED",
            "章节重生成版本保存失败。",
            status=500,
            retryable=True,
        ) from exc
    finally:
        if not report_committed:
            _release_failed_claim(
                owner_key=owner_key,
                project_id=project_id,
                idempotency_key=key,
                request_fingerprint=request_fingerprint,
                report_version_id=report_version_id,
            )
    return _rerun_public(saved_next, completed, reused=False)


__all__ = [
    "create_report_section_rerun",
    "validate_report_rerun_access",
    "validate_report_rerun_idempotency_key",
]
