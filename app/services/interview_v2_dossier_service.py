"""Batch 4A participant attributes and dossier orchestration."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import re
from typing import Any
from uuid import uuid4

from app.core.config import (
    INTERVIEW_V2_ATTRIBUTE_MAX_TOKENS,
    INTERVIEW_V2_ATTRIBUTE_MODEL,
    INTERVIEW_V2_ATTRIBUTE_REASONING,
    INTERVIEW_V2_DOSSIER_MAX_TOKENS,
    INTERVIEW_V2_DOSSIER_MODEL,
    INTERVIEW_V2_DOSSIER_REASONING,
    INTERVIEW_V2_MODEL_FALLBACKS,
)
from app.core.interview_v2_dossier import (
    InterviewV2DossierValidationError,
    build_participant_input,
    payload_sha256,
    validate_attribute_output,
    validate_dossier_output,
)
from app.core.security import _owner_from_login
from app.integrations.llm_client import collect_chat_completion
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.services.interview_v2_status_service import (
    get_interview_import_with_structure_status,
)
from app.storage import interview_v2_store as store
from app.storage.prompts import (
    _get_interview_v2_attribute_system_prompt,
    _get_interview_v2_dossier_system_prompt,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(code: str, message: str, *, status: int = 409, retryable: bool = False):
    return InterviewV2ImportError(
        status_code=status,
        code=code,
        message=message,
        retryable=retryable,
        suggested_action="refresh_dossier" if status == 409 else "retry_dossier",
    )


def _parse_json(text: str, label: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip())
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise _error("DOSSIER_MODEL_OUTPUT_INVALID", f"{label}没有返回有效 JSON。", status=502, retryable=True)
    try:
        value = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as exc:
        raise _error("DOSSIER_MODEL_OUTPUT_INVALID", f"{label}返回的 JSON 无法解析。", status=502, retryable=True) from exc
    if not isinstance(value, dict):
        raise _error("DOSSIER_MODEL_OUTPUT_INVALID", f"{label}返回格式不正确。", status=502, retryable=True)
    return value


def _ready_project(project_id: str, login: dict[str, Any] | None):
    try:
        state = store.load_analysis_boundary_state(project_id)
    except (OSError, TypeError, ValueError) as exc:
        raise _error("DOSSIER_PERSISTENCE_FAILED", "玩家档案状态读取失败。", status=500, retryable=True) from exc
    if state is None:
        raise _error("DOSSIER_INPUT_NOT_READY", "项目尚未确认分析边界。")
    import_id = str(state.get("import_id") or "")
    public = get_interview_import_with_structure_status(import_id, login)
    if public.get("project_id") != project_id:
        raise _error("INTERVIEW_IMPORT_NOT_FOUND", "未找到该访谈项目。", status=404)
    if public.get("status") != "READY_FOR_DOSSIERS" or bool(state.get("is_stale")):
        raise _error("DOSSIER_INPUT_NOT_READY", "分析边界未确认或已过期，暂不能生成玩家档案。")
    bundle = store.load_current_analysis_boundary_bundle(project_id, import_id)
    if bundle is None:
        raise _error("DOSSIER_INPUT_NOT_READY", "分析边界检查点不存在。")
    evidence_id = str(state.get("current_evidence_revision_id") or "")
    evidence = store.load_evidence_revision(project_id, evidence_id)
    if evidence is None:
        raise _error("DOSSIER_PERSISTENCE_FAILED", "当前证据版本不存在。", status=500)
    boundary_revision = bundle["boundary_revision"]
    coverage_revision = bundle["coverage_revision"]
    source = {
        "structure_revision_id": state.get("current_structure_revision_id"),
        "evidence_revision_id": evidence_id,
        "boundary_revision_id": state.get("current_boundary_revision_id"),
        "boundary_payload_sha256": state.get("current_boundary_payload_sha256"),
        "coverage_revision_id": state.get("current_coverage_revision_id"),
        "coverage_payload_sha256": state.get("current_coverage_payload_sha256"),
    }
    return public, evidence, boundary_revision["analysis_boundary"], coverage_revision, source


def _manifest(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    payload = evidence.get("evidence") or {}
    return list(evidence.get("expected_participants") or payload.get("expected_participants") or [])


def _public(project_id: str, import_id: str, participant_id: str, current: dict[str, Any] | None, source: dict[str, Any]):
    if current is None:
        return {
            "project_id": project_id, "import_id": import_id,
            "participant_id": participant_id, "status": "not_generated",
            "dossier_version_id": None, "dossier_version_number": 0,
            "attributes": {}, "dossier": {}, "source": source,
            "review": {}, "model_usage": {},
        }
    revision, state = current["revision"], current["state"]
    stale = revision.get("source") != source
    return {
        "project_id": project_id, "import_id": import_id,
        "participant_id": participant_id,
        "status": "stale" if stale else revision.get("status", "generated"),
        "dossier_version_id": revision.get("dossier_version_id"),
        "dossier_version_number": state.get("current_version_number", 0),
        "attributes": revision.get("attributes") or {},
        "dossier": revision.get("dossier") or {},
        "source": revision.get("source") or {},
        "review": revision.get("review") or {},
        "model_usage": revision.get("model_usage") or {},
    }


def list_participants(project_id: str, login: dict[str, Any] | None) -> dict[str, Any]:
    public, evidence, boundary, coverage, source = _ready_project(project_id, login)
    participants = []
    for item in _manifest(evidence):
        participant_id = str(item.get("participant_id") or "")
        current = store.load_current_participant_dossier(project_id, participant_id)
        participants.append({
            "participant_id": participant_id,
            "group_id": item.get("group_id"),
            "dossier_status": _public(project_id, public["import_id"], participant_id, current, source)["status"],
            "dossier_version_id": (current or {}).get("state", {}).get("current_dossier_version_id"),
        })
    return {"project_id": project_id, "import_id": public["import_id"], "status": "READY_FOR_DOSSIERS", "participants": participants}


def get_current_dossier(project_id: str, participant_id: str, login: dict[str, Any] | None) -> dict[str, Any]:
    public, evidence, boundary, coverage, source = _ready_project(project_id, login)
    build_participant_input(participant_id=participant_id, evidence_revision=evidence, analysis_boundary=boundary)
    current = store.load_current_participant_dossier(project_id, participant_id)
    return _public(project_id, public["import_id"], participant_id, current, source)


async def regenerate_dossier(project_id: str, participant_id: str, request: dict[str, Any], login: dict[str, Any] | None) -> dict[str, Any]:
    public, evidence, boundary, coverage, source = _ready_project(project_id, login)
    participant_input = build_participant_input(
        participant_id=participant_id,
        evidence_revision=evidence,
        analysis_boundary=boundary,
    )
    model_input = {
        "participant_id": participant_id,
        "attribute_evidence": participant_input["attribute_evidence"],
        "dossier_evidence": participant_input["dossier_evidence"],
        "evidence_allowlist": participant_input["evidence_allowlist"],
    }
    models = (INTERVIEW_V2_ATTRIBUTE_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS)
    attribute_text, attribute_model = await collect_chat_completion(
        [{"role": "system", "content": _get_interview_v2_attribute_system_prompt()},
         {"role": "user", "content": "<untrusted_interview_data>\n" + json.dumps(model_input, ensure_ascii=False) + "\n</untrusted_interview_data>"}],
        models=models,
        max_tokens=INTERVIEW_V2_ATTRIBUTE_MAX_TOKENS,
        reasoning_effort=INTERVIEW_V2_ATTRIBUTE_REASONING,
    )
    try:
        attributes = validate_attribute_output(_parse_json(attribute_text, "属性抽取"), participant_input=participant_input)
    except InterviewV2DossierValidationError as exc:
        raise _error("DOSSIER_ATTRIBUTE_VALIDATION_FAILED", str(exc), status=502, retryable=True) from exc
    dossier_payload = {**model_input, "validated_attributes": attributes}
    dossier_text, dossier_model = await collect_chat_completion(
        [{"role": "system", "content": _get_interview_v2_dossier_system_prompt()},
         {"role": "user", "content": "<untrusted_interview_data>\n" + json.dumps(dossier_payload, ensure_ascii=False) + "\n</untrusted_interview_data>"}],
        models=(INTERVIEW_V2_DOSSIER_MODEL, *INTERVIEW_V2_MODEL_FALLBACKS),
        max_tokens=INTERVIEW_V2_DOSSIER_MAX_TOKENS,
        reasoning_effort=INTERVIEW_V2_DOSSIER_REASONING,
    )
    try:
        dossier = validate_dossier_output(_parse_json(dossier_text, "玩家档案"), participant_input=participant_input)
    except InterviewV2DossierValidationError as exc:
        raise _error("DOSSIER_CLAIM_VALIDATION_FAILED", str(exc), status=502, retryable=True) from exc
    created_at = _now()
    revision = {
        "dossier_version_id": f"dossier_{uuid4().hex}",
        "import_id": public["import_id"],
        "source": source,
        "input_fingerprint": payload_sha256(dossier_payload),
        "attributes": attributes,
        "dossier": dossier,
        "status": "generated",
        "review": {},
        "model_usage": {"attribute_model": attribute_model, "dossier_model": dossier_model},
        "created_at": created_at,
        "created_by": _owner_from_login(login).get("owner_key", ""),
    }
    try:
        saved = store.save_participant_dossier_cas(
            project_id=project_id,
            participant_id=participant_id,
            base_dossier_version_id=request.get("base_dossier_version_id"),
            revision=revision,
        )
    except ValueError as exc:
        raise _error("DOSSIER_VERSION_CONFLICT", "玩家档案版本已变化，请刷新后重试。") from exc
    return _public(project_id, public["import_id"], participant_id, saved, source)


def review_dossier(project_id: str, participant_id: str, request: dict[str, Any], login: dict[str, Any] | None) -> dict[str, Any]:
    public, evidence, boundary, coverage, source = _ready_project(project_id, login)
    try:
        saved = store.review_participant_dossier_cas(
            project_id=project_id,
            participant_id=participant_id,
            base_dossier_version_id=request["base_dossier_version_id"],
            decision=request["decision"],
            note=request.get("note", ""),
            actor=_owner_from_login(login).get("owner_key", ""),
            reviewed_at=_now(),
        )
    except ValueError as exc:
        raise _error("DOSSIER_VERSION_CONFLICT", "玩家档案版本已变化，请刷新后重试。") from exc
    return _public(project_id, public["import_id"], participant_id, saved, source)
