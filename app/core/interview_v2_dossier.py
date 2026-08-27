"""Deterministic participant dossier inputs and model-output validation."""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from typing import Any


DOSSIER_SCHEMA_VERSION = "interview-participant-dossier/1.0"
ATTRIBUTE_SCHEMA_VERSION = "interview-participant-attributes/1.0"
_FACT_SOURCES = {
    "explicit_self_report",
    "researcher_recorded_fact",
    "explicit_structured_field",
}
_CLAIM_TYPES = {
    "context", "behavior", "attitude", "reason", "impact",
    "expectation", "contradiction",
}
_SENSITIVE_KEYS = {"age", "gender", "region", "ethnicity", "religion"}
_FACT_STATUSES = {"active", "conflicting", "unknown"}


class InterviewV2DossierValidationError(ValueError):
    pass


def _stable_id(prefix: str, *parts: object) -> str:
    value = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{sha256(value.encode('utf-8')).hexdigest()[:32]}"


def _manifest(evidence_revision: dict[str, Any]) -> list[dict[str, str]]:
    payload = evidence_revision.get("evidence") or {}
    rows = evidence_revision.get("expected_participants")
    if rows is None and isinstance(payload, dict):
        rows = payload.get("expected_participants")
    if not isinstance(rows, list):
        raise InterviewV2DossierValidationError("玩家清单不存在。")
    return [dict(item) for item in rows if isinstance(item, dict)]


def _entries(evidence_revision: dict[str, Any]) -> list[dict[str, Any]]:
    payload = evidence_revision.get("evidence") or {}
    rows = evidence_revision.get("entries") or evidence_revision.get("evidence_entries")
    if rows is None and isinstance(payload, dict):
        rows = payload.get("entries") or payload.get("evidence_entries")
    if not isinstance(rows, list):
        raise InterviewV2DossierValidationError("证据清单不存在。")
    return [dict(item) for item in rows if isinstance(item, dict)]


def _scope_for(entry: dict[str, Any], rules: list[dict[str, Any]]) -> str:
    matches = []
    for rule in rules:
        if rule.get("decision_status") != "confirmed":
            continue
        if rule.get("sheet_id") != entry.get("sheet_id"):
            continue
        row = int(entry.get("row") or 0)
        if int(rule.get("start_row") or 0) <= row <= int(rule.get("end_row") or 0):
            matches.append(rule)
    if not matches:
        return "interview_body"
    matches.sort(key=lambda item: int(item.get("display_order") or 0))
    return str(matches[-1].get("scope_type") or "excluded")


def build_participant_input(
    *,
    participant_id: str,
    evidence_revision: dict[str, Any],
    analysis_boundary: dict[str, Any],
) -> dict[str, Any]:
    manifest = _manifest(evidence_revision)
    participant = next(
        (item for item in manifest if item.get("participant_id") == participant_id),
        None,
    )
    if participant is None:
        raise InterviewV2DossierValidationError("玩家不属于当前项目。")
    rules = list(analysis_boundary.get("source_scope_rules") or [])
    eligible = []
    for raw in _entries(evidence_revision):
        if raw.get("participant_id") != participant_id:
            continue
        if raw.get("inclusion_status") != "included":
            continue
        if raw.get("identity_decision_status") not in {
            "system_verified", "confirmed", "user_confirmed"
        }:
            continue
        scope_type = _scope_for(raw, rules)
        if scope_type == "excluded":
            continue
        item = deepcopy(raw)
        item["scope_type"] = scope_type
        eligible.append(item)
    eligible.sort(key=lambda item: str(item.get("evidence_id") or ""))
    return {
        "participant_id": participant_id,
        "group_id": participant.get("group_id"),
        "attribute_evidence": [
            item for item in eligible
            if item["scope_type"] == "participant_background"
        ],
        "dossier_evidence": [
            item for item in eligible if item["scope_type"] == "interview_body"
        ],
        "evidence_allowlist": [str(item["evidence_id"]) for item in eligible],
        "self_report_evidence_allowlist": [
            str(item["evidence_id"]) for item in eligible
            if item.get("evidence_type") == "participant_self_report"
        ],
    }


def _require_evidence_ids(
    ids: object, *, allowlist: set[str], label: str, required: bool = True
) -> list[str]:
    if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
        raise InterviewV2DossierValidationError(f"{label}证据格式不正确。")
    normalized = list(dict.fromkeys(ids))
    if required and not normalized:
        raise InterviewV2DossierValidationError(f"{label}缺少证据。")
    if any(item not in allowlist for item in normalized):
        raise InterviewV2DossierValidationError(f"{label}引用了越权证据。")
    return normalized


def validate_attribute_output(
    output: dict[str, Any], *, participant_input: dict[str, Any]
) -> dict[str, Any]:
    participant_id = participant_input["participant_id"]
    if output.get("participant_id") != participant_id:
        raise InterviewV2DossierValidationError("属性结果玩家不匹配。")
    background_ids = {
        str(item["evidence_id"]) for item in participant_input["attribute_evidence"]
    }
    facts = []
    fact_ids: set[str] = set()
    candidate_to_fact: dict[str, str] = {}
    for index, raw in enumerate(output.get("facts") or []):
        if not isinstance(raw, dict):
            raise InterviewV2DossierValidationError("属性事实格式不正确。")
        key = str(raw.get("attribute_key") or "").strip()
        raw_value = str(raw.get("raw_value") or "").strip()
        source = str(raw.get("fact_source") or "")
        evidence_ids = _require_evidence_ids(
            raw.get("evidence_ids"), allowlist=background_ids, label="属性事实"
        )
        if not key or not raw_value or source not in _FACT_SOURCES:
            raise InterviewV2DossierValidationError("属性事实字段不完整。")
        fact_status = str(raw.get("fact_status") or "active")
        if fact_status not in _FACT_STATUSES:
            raise InterviewV2DossierValidationError("属性事实状态不正确。")
        if key in _SENSITIVE_KEYS and source != "explicit_self_report":
            raise InterviewV2DossierValidationError("敏感属性只能来自明确自述。")
        evidence_by_id = {
            str(item["evidence_id"]): item
            for item in participant_input["attribute_evidence"]
        }
        source_entries = [evidence_by_id[item] for item in evidence_ids]
        if source == "explicit_self_report" and any(
            item.get("evidence_type") != "participant_self_report"
            for item in source_entries
        ):
            raise InterviewV2DossierValidationError("明确自述属性引用了非玩家证据。")
        if source == "researcher_recorded_fact" and any(
            item.get("evidence_type") != "researcher_observation"
            for item in source_entries
        ):
            raise InterviewV2DossierValidationError("研究员记录属性引用了非观察证据。")
        source_text = "\n".join(
            str(item.get("normalized_content") or item.get("display_content") or item.get("raw_content") or "")
            for item in source_entries
        )
        if raw_value not in source_text:
            raise InterviewV2DossierValidationError("属性原值无法在引用证据中定位。")
        fact_id = _stable_id("fact", participant_id, key, raw_value, *evidence_ids)
        fact_ids.add(fact_id)
        candidate_id = str(raw.get("candidate_id") or f"fact_candidate_{index + 1}")
        candidate_to_fact[candidate_id] = fact_id
        facts.append({
            "attribute_fact_id": fact_id,
            "attribute_key": key,
            "attribute_label": str(raw.get("attribute_label") or key).strip(),
            "raw_value": raw_value,
            "normalized_value": raw.get("normalized_value"),
            "fact_source": source,
            "fact_status": fact_status,
            "source_evidence_ids": evidence_ids,
            "confidence": float(raw.get("confidence") or 0),
            "review_status": "unreviewed",
        })
    labels = []
    for raw in output.get("analytical_labels") or []:
        if not isinstance(raw, dict):
            raise InterviewV2DossierValidationError("分析标签格式不正确。")
        requested_source_ids = list(dict.fromkeys(
            raw.get("source_fact_ids") or raw.get("source_fact_candidate_ids") or []
        ))
        source_ids = [candidate_to_fact.get(item, item) for item in requested_source_ids]
        if not source_ids or any(item not in fact_ids for item in source_ids):
            raise InterviewV2DossierValidationError("分析标签未绑定有效属性事实。")
        evidence_ids = _require_evidence_ids(
            raw.get("evidence_ids"), allowlist=background_ids, label="分析标签"
        )
        label_key = str(raw.get("label_key") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not label_key or not label:
            raise InterviewV2DossierValidationError("分析标签字段不完整。")
        labels.append({
            "analytical_label_id": _stable_id("label", participant_id, label_key, *source_ids),
            "label_key": label_key,
            "label": label,
            "source_fact_ids": source_ids,
            "source_evidence_ids": evidence_ids,
            "confidence": float(raw.get("confidence") or 0),
            "review_status": "unreviewed",
        })
    return {
        "attribute_schema_version": ATTRIBUTE_SCHEMA_VERSION,
        "participant_id": participant_id,
        "facts": facts,
        "analytical_labels": labels,
    }


def validate_dossier_output(
    output: dict[str, Any], *, participant_input: dict[str, Any]
) -> dict[str, Any]:
    participant_id = participant_input["participant_id"]
    if output.get("participant_id") != participant_id:
        raise InterviewV2DossierValidationError("档案结果玩家不匹配。")
    allowlist = set(participant_input["evidence_allowlist"])
    self_report_allowlist = set(participant_input["self_report_evidence_allowlist"])
    claims = []
    for raw in output.get("claims") or []:
        if not isinstance(raw, dict):
            raise InterviewV2DossierValidationError("档案主张格式不正确。")
        claim_type = str(raw.get("claim_type") or "")
        statement = str(raw.get("statement") or "").strip()
        if claim_type not in _CLAIM_TYPES or not statement:
            raise InterviewV2DossierValidationError("档案主张字段不完整。")
        supporting = _require_evidence_ids(
            raw.get("supporting_evidence_ids"), allowlist=self_report_allowlist, label="档案主张"
        )
        conflicting = _require_evidence_ids(
            raw.get("conflicting_evidence_ids") or [],
            allowlist=allowlist,
            label="冲突主张",
            required=False,
        )
        claims.append({
            "dossier_claim_id": _stable_id("claim", participant_id, claim_type, statement, *supporting),
            "claim_type": claim_type,
            "module_id": raw.get("module_id"),
            "evaluation_object_id": raw.get("evaluation_object_id"),
            "statement": statement,
            "supporting_evidence_ids": supporting,
            "conflicting_evidence_ids": conflicting,
            "confidence": float(raw.get("confidence") or 0),
        })
    return {
        "dossier_schema_version": DOSSIER_SCHEMA_VERSION,
        "participant_id": participant_id,
        "claims": claims,
        "contradictions": list(output.get("contradictions") or []),
        "missing_context": list(output.get("missing_context") or []),
    }


def payload_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()
