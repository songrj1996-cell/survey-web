"""Batch 5A cross-participant analysis orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any
from uuid import uuid4

from app.core.config import (
    INTERVIEW_V2_ANALYSIS_MAX_TOKENS,
    INTERVIEW_V2_ANALYSIS_MODEL,
    INTERVIEW_V2_ANALYSIS_REASONING,
    INTERVIEW_V2_MODEL_FALLBACKS,
)
from app.core.interview_v2_analysis import (
    ANALYSIS_SCHEMA_VERSION,
    InterviewV2AnalysisValidationError,
    build_analysis_input,
    validate_module_findings,
)
from app.core.security import _owner_from_login
from app.integrations.llm_client import collect_chat_completion
from app.services.interview_v2_dossier_service import _manifest, _ready_project
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.storage import interview_v2_store as store
from app.storage.prompts import _get_interview_v2_analysis_system_prompt


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(
    code: str,
    message: str,
    *,
    status: int = 409,
    retryable: bool = False,
    context: dict[str, Any] | None = None,
) -> InterviewV2ImportError:
    return InterviewV2ImportError(
        status_code=status,
        code=code,
        message=message,
        retryable=retryable,
        suggested_action=(
            "refresh_analysis_inputs" if status == 409 else "retry_analysis"
        ),
        context=context,
    )


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise _error(
            "ANALYSIS_MODEL_OUTPUT_INVALID",
            "跨玩家分析没有返回有效 JSON。",
            status=502,
            retryable=True,
        )
    try:
        value = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise _error(
            "ANALYSIS_MODEL_OUTPUT_INVALID",
            "跨玩家分析返回的 JSON 无法解析。",
            status=502,
            retryable=True,
        ) from exc
    if not isinstance(value, dict):
        raise _error(
            "ANALYSIS_MODEL_OUTPUT_INVALID",
            "跨玩家分析返回格式不正确。",
            status=502,
            retryable=True,
        )
    return value


def _public(
    project_id: str,
    current: dict[str, Any] | None,
    *,
    status_override: str | None = None,
) -> dict[str, Any]:
    if current is None:
        return {
            "project_id": project_id,
            "analysis_run_id": None,
            "analysis_version_number": 0,
            "status": "not_generated",
            "source": {},
            "findings": [],
            "stat_facts": [],
            "limitations": [],
            "model_usage": {},
        }
    revision, state = current["revision"], current["state"]
    return {
        "project_id": project_id,
        "analysis_run_id": revision.get("analysis_run_id"),
        "analysis_version_number": state.get("current_version_number", 0),
        "status": status_override or revision.get("status", "completed"),
        "source": revision.get("source") or {},
        "findings": revision.get("findings") or [],
        "stat_facts": revision.get("stat_facts") or [],
        "limitations": revision.get("limitations") or [],
        "model_usage": revision.get("model_usage") or {},
    }


def _frozen_dossiers(
    project_id: str,
    manifest: list[dict[str, Any]],
    source: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[dict[str, str]]]:
    revisions: list[dict[str, Any]] = []
    unreviewed: list[str] = []
    blockers: list[dict[str, str]] = []
    for item in manifest:
        participant_id = str(item.get("participant_id") or "")
        try:
            current = store.load_current_participant_dossier(project_id, participant_id)
        except (OSError, TypeError, ValueError) as exc:
            raise _error(
                "ANALYSIS_PERSISTENCE_FAILED",
                "玩家档案状态读取失败。",
                status=500,
                retryable=True,
            ) from exc
        if current is None:
            blockers.append({"participant_id": participant_id, "reason": "not_generated"})
            continue
        revision = current["revision"]
        status = str(revision.get("status") or "generated")
        if revision.get("source") != source:
            blockers.append({"participant_id": participant_id, "reason": "stale"})
            continue
        if status == "needs_changes":
            blockers.append({"participant_id": participant_id, "reason": "needs_changes"})
            continue
        if status not in {"generated", "approved"}:
            blockers.append({"participant_id": participant_id, "reason": "invalid_status"})
            continue
        revisions.append(revision)
        if status != "approved":
            unreviewed.append(participant_id)
    if blockers:
        raise _error(
            "ANALYSIS_INPUT_NOT_READY",
            "部分玩家档案缺失、已过期或待修改，暂不能启动跨玩家分析。",
            context={"blocking_participants": blockers},
        )
    dossier_versions = sorted(
        [
            {
                "participant_id": str(item.get("participant_id") or ""),
                "dossier_version_id": str(item.get("dossier_version_id") or ""),
                "revision_payload_sha256": str(item.get("revision_payload_sha256") or ""),
            }
            for item in revisions
        ],
        key=lambda item: item["participant_id"],
    )
    return revisions, unreviewed, dossier_versions


def get_current_analysis(
    project_id: str, login: dict[str, Any] | None
) -> dict[str, Any]:
    _public_project, evidence, _boundary, _coverage, source = _ready_project(
        project_id, login
    )
    try:
        current = store.load_current_analysis_run(project_id)
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "ANALYSIS_PERSISTENCE_FAILED",
            "跨玩家分析状态读取失败。",
            status=500,
            retryable=True,
        ) from exc
    if current is None:
        return _public(project_id, None)
    stored_source = current["revision"].get("source") or {}
    base_fields = (
        "structure_revision_id",
        "evidence_revision_id",
        "boundary_revision_id",
        "boundary_payload_sha256",
        "coverage_revision_id",
        "coverage_payload_sha256",
    )
    is_current = all(stored_source.get(field) == source.get(field) for field in base_fields)
    expected_participants = {
        str(item.get("participant_id") or "") for item in _manifest(evidence)
    }
    stored_versions = stored_source.get("dossier_versions") or []
    if {
        str(item.get("participant_id") or "") for item in stored_versions
    } != expected_participants:
        is_current = False
    try:
        if is_current:
            for item in stored_versions:
                participant_id = str(item.get("participant_id") or "")
                dossier = store.load_current_participant_dossier(project_id, participant_id)
                if (
                    dossier is None
                    or dossier["state"].get("current_dossier_version_id")
                    != item.get("dossier_version_id")
                ):
                    is_current = False
                    break
    except (OSError, TypeError, ValueError) as exc:
        raise _error(
            "ANALYSIS_PERSISTENCE_FAILED",
            "跨玩家分析所引用的玩家档案读取失败。",
            status=500,
            retryable=True,
        ) from exc
    return _public(
        project_id,
        current,
        status_override=None if is_current else "stale",
    )


async def create_analysis_run(
    project_id: str,
    request: dict[str, Any],
    login: dict[str, Any] | None,
) -> dict[str, Any]:
    public, evidence, boundary, coverage_revision, source = _ready_project(
        project_id, login
    )
    manifest = _manifest(evidence)
    dossiers, unreviewed, dossier_versions = _frozen_dossiers(
        project_id, manifest, source
    )
    frozen_source = {**source, "dossier_versions": dossier_versions}
    coverage = coverage_revision.get("coverage_preview") or coverage_revision
    try:
        analysis_input = build_analysis_input(
            project_id=project_id,
            source=frozen_source,
            evidence_revision=evidence,
            analysis_boundary=boundary,
            coverage_revision=coverage,
            dossier_revisions=dossiers,
            unreviewed_participant_ids=unreviewed,
        )
    except InterviewV2AnalysisValidationError as exc:
        raise _error(
            "ANALYSIS_INPUT_INVALID",
            str(exc),
            status=422,
        ) from exc

    analysis_run_id = f"analysis_{uuid4().hex}"
    findings: list[dict[str, Any]] = []
    stat_facts: list[dict[str, Any]] = []
    model_usage: dict[str, Any] = {"modules": []}
    for module_input in analysis_input["modules"]:
        payload = {
            "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
            "source": frozen_source,
            "unreviewed_participant_ids": unreviewed,
            **module_input,
        }
        response_text, model = await collect_chat_completion(
            [
                {"role": "system", "content": _get_interview_v2_analysis_system_prompt()},
                {
                    "role": "user",
                    "content": "<untrusted_interview_data>\n"
                    + json.dumps(payload, ensure_ascii=False)
                    + "\n</untrusted_interview_data>",
                },
            ],
            models=(INTERVIEW_V2_ANALYSIS_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
            max_tokens=INTERVIEW_V2_ANALYSIS_MAX_TOKENS,
            reasoning_effort=INTERVIEW_V2_ANALYSIS_REASONING,
        )
        try:
            validated = validate_module_findings(
                _parse_json(response_text),
                module_input=module_input,
                analysis_run_id=analysis_run_id,
            )
        except InterviewV2AnalysisValidationError as exc:
            raise _error(
                "ANALYSIS_FINDING_VALIDATION_FAILED",
                str(exc),
                status=502,
                retryable=True,
                context={"module_id": module_input["module_id"]},
            ) from exc
        findings.extend(validated["findings"])
        stat_facts.extend(validated["stat_facts"])
        model_usage["modules"].append(
            {"module_id": module_input["module_id"], "model": model}
        )

    limitations = []
    if unreviewed:
        limitations.append(
            f"{len(unreviewed)} 名玩家档案由系统生成但尚未人工批准。"
        )
    revision = {
        "analysis_run_id": analysis_run_id,
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "import_id": public.get("import_id"),
        "source": frozen_source,
        "input_fingerprint": analysis_input["input_fingerprint"],
        "status": "completed",
        "findings": findings,
        "stat_facts": stat_facts,
        "limitations": limitations,
        "model_usage": model_usage,
        "created_at": _now(),
        "created_by": _owner_from_login(login).get("owner_key", ""),
    }
    try:
        saved = store.save_analysis_run_cas(
            project_id=project_id,
            base_analysis_run_id=request.get("base_analysis_run_id"),
            revision=revision,
        )
    except ValueError as exc:
        message = str(exc)
        code = (
            "ANALYSIS_INPUT_CHANGED"
            if "input changed" in message
            else "ANALYSIS_REVISION_CONFLICT"
        )
        raise _error(
            code,
            "分析期间上游版本已变化，请刷新后重试。"
            if code == "ANALYSIS_INPUT_CHANGED"
            else "跨玩家分析版本已变化，请刷新后重试。",
        ) from exc
    except (OSError, TypeError) as exc:
        raise _error(
            "ANALYSIS_PERSISTENCE_FAILED",
            "跨玩家分析保存失败。",
            status=500,
            retryable=True,
        ) from exc
    return _public(project_id, saved)


__all__ = ["create_analysis_run", "get_current_analysis"]
