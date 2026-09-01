"""Deterministic Batch 5A cross-participant analysis contracts."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


ANALYSIS_SCHEMA_VERSION = "interview-cross-participant-analysis/1.0"
STAT_FACT_SCHEMA_VERSION = "interview-stat-fact/1.0"
_PARTICIPANT_RE = re.compile(r"^participant_[0-9a-f]{32}$")
_MODULE_RE = re.compile(r"^module_[0-9a-f]{32}$")
_EVALUATION_RE = re.compile(r"^evaluation_[0-9a-f]{32}$")
_QUESTION_RE = re.compile(r"^question_[0-9a-f]{32}$")
_EVIDENCE_RE = re.compile(r"^(?:ev|evidence)_[0-9a-f]{32}$")
_REPORTABLE_IDENTITY = {"confirmed", "system_verified"}


class InterviewV2AnalysisValidationError(ValueError):
    """A frozen input or model finding violated a server-side invariant."""


def _text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def payload_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _scope_for(entry: dict[str, Any], rules: list[dict[str, Any]]) -> str | None:
    sheet_id = _text(entry.get("sheet_id"))
    row = entry.get("row")
    if isinstance(row, bool) or not isinstance(row, int):
        return None
    for rule in rules:
        if (
            _text(rule.get("sheet_id")) == sheet_id
            and int(rule.get("start_row") or 0) <= row <= int(rule.get("end_row") or 0)
        ):
            return _text(rule.get("scope_type"))
    return None


def _scoped_attributes(
    attributes: dict[str, Any],
    *,
    boundary: dict[str, Any],
    module_id: str,
    evaluation_object_ids: set[str],
) -> dict[str, Any]:
    """Facts remain available; analytical labels obey confirmed 3B scope rules."""

    rules = {
        _text(item.get("label_key")): item
        for item in boundary.get("label_scope_rules") or []
        if item.get("decision_status") == "confirmed"
    }
    labels: list[dict[str, Any]] = []
    for label in attributes.get("analytical_labels") or []:
        rule = rules.get(_text(label.get("label_key")))
        if rule is None:
            continue
        mode = _text(rule.get("scope_mode"))
        allowed = (
            mode == "all_analysis"
            or (
                mode == "selected_modules"
                and module_id in set(rule.get("module_ids") or [])
            )
            or (
                mode == "selected_evaluation_objects"
                and bool(evaluation_object_ids & set(rule.get("evaluation_object_ids") or []))
            )
        )
        if allowed:
            labels.append(label)
    return {
        "facts": list(attributes.get("facts") or []),
        "analytical_labels": labels,
    }


def build_analysis_input(
    *,
    project_id: str,
    source: dict[str, Any],
    evidence_revision: dict[str, Any],
    analysis_boundary: dict[str, Any],
    coverage_revision: dict[str, Any],
    dossier_revisions: list[dict[str, Any]],
    unreviewed_participant_ids: list[str],
) -> dict[str, Any]:
    """Freeze current dossiers, legal evidence and coverage into module inputs."""

    manifest = evidence_revision.get("expected_participants") or (
        evidence_revision.get("evidence") or {}
    ).get("expected_participants") or []
    expected_ids = [_text(item.get("participant_id")) for item in manifest]
    if not expected_ids or any(not _PARTICIPANT_RE.fullmatch(item) for item in expected_ids):
        raise InterviewV2AnalysisValidationError("analysis participant manifest is invalid")
    if len(expected_ids) != len(set(expected_ids)):
        raise InterviewV2AnalysisValidationError("analysis participant manifest contains duplicates")

    dossier_by_participant: dict[str, dict[str, Any]] = {}
    for revision in dossier_revisions:
        participant_id = _text(revision.get("participant_id"))
        if participant_id in dossier_by_participant or participant_id not in expected_ids:
            raise InterviewV2AnalysisValidationError("analysis dossier participant set is invalid")
        dossier_by_participant[participant_id] = revision
    if set(dossier_by_participant) != set(expected_ids):
        raise InterviewV2AnalysisValidationError("every participant requires a current dossier")

    active_objects = [
        item
        for item in analysis_boundary.get("evaluation_objects") or []
        if item.get("decision_status") != "superseded"
    ]
    module_ids = sorted({_text(item.get("module_id")) for item in active_objects})
    if not module_ids or any(not _MODULE_RE.fullmatch(item) for item in module_ids):
        raise InterviewV2AnalysisValidationError("analysis boundary has no valid modules")

    rules = list(analysis_boundary.get("source_scope_rules") or [])
    entries = evidence_revision.get("entries") or (evidence_revision.get("evidence") or {}).get("entries") or []
    legal_entries: dict[str, dict[str, Any]] = {}
    for entry in entries:
        evidence_id = _text(entry.get("evidence_id"))
        participant_id = _text(entry.get("participant_id"))
        if (
            _EVIDENCE_RE.fullmatch(evidence_id)
            and participant_id in dossier_by_participant
            and entry.get("inclusion_status") == "included"
            and entry.get("identity_decision_status") in _REPORTABLE_IDENTITY
            and _scope_for(entry, rules) == "interview_body"
            and entry.get("evidence_type") in {"participant_self_report", "researcher_observation"}
        ):
            legal_entries[evidence_id] = {
                "evidence_id": evidence_id,
                "participant_id": participant_id,
                "module_id": _text(entry.get("module_id")) or None,
                "main_question_id": _text(entry.get("main_question_id")) or None,
                "evaluation_object_id": _text(entry.get("evaluation_object_id")) or None,
                "evidence_type": entry.get("evidence_type"),
                "normalized_content": _text(entry.get("normalized_content")),
            }

    coverage_rows = list(coverage_revision.get("rows") or [])
    coverage_summaries = list(coverage_revision.get("summaries") or [])
    modules: list[dict[str, Any]] = []
    for module_id in module_ids:
        module_objects = [item for item in active_objects if _text(item.get("module_id")) == module_id]
        module_object_ids = {
            _text(item.get("evaluation_object_id")) for item in module_objects
        }
        dossier_payloads: list[dict[str, Any]] = []
        referenced_ids: set[str] = set()
        for participant_id in expected_ids:
            revision = dossier_by_participant[participant_id]
            dossier = revision.get("dossier") or {}
            claims = [
                claim
                for claim in dossier.get("claims") or []
                if _text(claim.get("module_id")) in {"", module_id}
            ]
            for claim in claims:
                referenced_ids.update(_text(item) for item in claim.get("supporting_evidence_ids") or [])
                referenced_ids.update(_text(item) for item in claim.get("conflicting_evidence_ids") or [])
            dossier_payloads.append(
                {
                    "participant_id": participant_id,
                    "dossier_version_id": revision.get("dossier_version_id"),
                    "review_status": revision.get("status"),
                    "attributes": _scoped_attributes(
                        revision.get("attributes") or {},
                        boundary=analysis_boundary,
                        module_id=module_id,
                        evaluation_object_ids=module_object_ids,
                    ),
                    "claims": claims,
                    "contradictions": dossier.get("contradictions") or [],
                    "missing_context": dossier.get("missing_context") or [],
                }
            )
        module_evidence = [
            entry
            for evidence_id, entry in legal_entries.items()
            if entry.get("module_id") == module_id
            or (not entry.get("module_id") and evidence_id in referenced_ids)
        ]
        modules.append(
            {
                "module_id": module_id,
                "evidence_revision_id": source.get("evidence_revision_id"),
                "coverage_revision_id": source.get("coverage_revision_id"),
                "evaluation_objects": module_objects,
                "participant_dossiers": dossier_payloads,
                "coverage_rows": [row for row in coverage_rows if _text(row.get("module_id")) == module_id],
                "coverage_summaries": [
                    item for item in coverage_summaries if _text(item.get("module_id")) == module_id
                ],
                "evidence_allowlist": sorted(module_evidence, key=lambda item: item["evidence_id"]),
            }
        )
    frozen = {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "project_id": project_id,
        "source": source,
        "participant_ids": sorted(expected_ids),
        "unreviewed_participant_ids": sorted(set(unreviewed_participant_ids)),
        "modules": modules,
    }
    frozen["input_fingerprint"] = payload_sha256(frozen)
    return frozen


def _case_list(
    raw: object,
    *,
    role: str,
    participants: set[str],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise InterviewV2AnalysisValidationError(f"{role} cases must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    required_type = "researcher_observation" if role == "observation" else "participant_self_report"
    for item in raw:
        if not isinstance(item, dict):
            raise InterviewV2AnalysisValidationError(f"{role} case must be an object")
        participant_id = _text(item.get("participant_id"))
        evidence_ids = [_text(value) for value in item.get("evidence_ids") or []]
        if participant_id not in participants or participant_id in seen or not evidence_ids:
            raise InterviewV2AnalysisValidationError(f"{role} case participant is invalid")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise InterviewV2AnalysisValidationError(f"{role} case evidence is duplicated")
        for evidence_id in evidence_ids:
            evidence = evidence_by_id.get(evidence_id)
            if (
                evidence is None
                or evidence.get("participant_id") != participant_id
                or evidence.get("evidence_type") != required_type
            ):
                raise InterviewV2AnalysisValidationError(f"{role} case evidence is not qualified")
        seen.add(participant_id)
        result.append(
            {
                "finding_case_id": "",
                "role": role,
                "participant_id": participant_id,
                "evidence_ids": sorted(evidence_ids),
                "evidence_revision_id": "",
                "qualification_status": "passed",
            }
        )
    return sorted(result, key=lambda item: item["participant_id"])


def _stat_fact(
    *,
    analysis_run_id: str,
    finding_id: str,
    module_input: dict[str, Any],
    support_cases: list[dict[str, Any]],
    evaluation_object_id: str | None,
    main_question_id: str | None,
) -> dict[str, Any]:
    numerator_ids = sorted({item["participant_id"] for item in support_cases})
    denominator_ids: list[str] | None = None
    denominator_definition: str | None = None
    if evaluation_object_id and main_question_id:
        summaries = [
            item
            for item in module_input.get("coverage_summaries") or []
            if _text(item.get("evaluation_object_id")) == evaluation_object_id
            and _text(item.get("main_question_id")) == main_question_id
        ]
        if len(summaries) == 1 and bool(summaries[0].get("denominator_reliable")):
            denominator_ids = sorted(
                {
                    _text(row.get("participant_id"))
                    for row in module_input.get("coverage_rows") or []
                    if _text(row.get("evaluation_object_id")) == evaluation_object_id
                    and _text(row.get("main_question_id")) == main_question_id
                    and row.get("asked_status") == "asked"
                    and row.get("applicability") == "applicable"
                    and row.get("review_status") in {"system_verified", "confirmed"}
                }
            )
            if not set(numerator_ids) <= set(denominator_ids):
                raise InterviewV2AnalysisValidationError("supporting participant is outside the reliable denominator")
            denominator_definition = "被询问且适用该主问题的独立玩家"
    stat_fact_id = _stable_id("stat", analysis_run_id, finding_id, *numerator_ids)
    return {
        "stat_fact_id": stat_fact_id,
        "stat_fact_schema_version": STAT_FACT_SCHEMA_VERSION,
        "analysis_run_id": analysis_run_id,
        "finding_id": finding_id,
        "coverage_version_id": module_input.get("coverage_revision_id"),
        "evidence_revision_id": module_input.get("evidence_revision_id"),
        "metric_type": "participant_mentions",
        "numerator": len(numerator_ids),
        "denominator": len(denominator_ids) if denominator_ids is not None else None,
        "denominator_definition": denominator_definition,
        "numerator_cases": [
            {"participant_id": item["participant_id"], "evidence_ids": item["evidence_ids"]}
            for item in support_cases
        ],
        "denominator_participant_ids": denominator_ids,
        "proportion": (
            len(numerator_ids) / len(denominator_ids)
            if denominator_ids
            else (0.0 if denominator_ids == [] else None)
        ),
    }


def validate_module_findings(
    raw: dict[str, Any], *, module_input: dict[str, Any], analysis_run_id: str
) -> dict[str, Any]:
    """Validate model output and derive case IDs and immutable numeric facts."""

    if not isinstance(raw, dict) or _text(raw.get("module_id")) != module_input.get("module_id"):
        raise InterviewV2AnalysisValidationError("analysis module output does not match its input")
    participants = {
        _text(item.get("participant_id")) for item in module_input.get("participant_dossiers") or []
    }
    evidence_by_id = {
        _text(item.get("evidence_id")): item for item in module_input.get("evidence_allowlist") or []
    }
    object_ids = {
        _text(item.get("evaluation_object_id")) for item in module_input.get("evaluation_objects") or []
    }
    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list) or len(findings_raw) > 200:
        raise InterviewV2AnalysisValidationError("analysis findings must be a list")
    findings: list[dict[str, Any]] = []
    stat_facts: list[dict[str, Any]] = []
    for raw_finding in findings_raw:
        if not isinstance(raw_finding, dict):
            raise InterviewV2AnalysisValidationError("analysis finding must be an object")
        title = _text(raw_finding.get("title"))
        statement = _text(raw_finding.get("statement"))
        if not title or not statement or len(title) > 300 or len(statement) > 4000:
            raise InterviewV2AnalysisValidationError("analysis finding text is invalid")
        evaluation_object_id = _text(raw_finding.get("evaluation_object_id")) or None
        main_question_id = _text(raw_finding.get("main_question_id")) or None
        if bool(evaluation_object_id) != bool(main_question_id):
            raise InterviewV2AnalysisValidationError("coverage scope requires both object and question")
        if evaluation_object_id and (
            not _EVALUATION_RE.fullmatch(evaluation_object_id) or evaluation_object_id not in object_ids
        ):
            raise InterviewV2AnalysisValidationError("analysis finding object is invalid")
        if main_question_id and not _QUESTION_RE.fullmatch(main_question_id):
            raise InterviewV2AnalysisValidationError("analysis finding question is invalid")
        support = _case_list(
            raw_finding.get("supporting_cases", []), role="support",
            participants=participants, evidence_by_id=evidence_by_id,
        )
        counter = _case_list(
            raw_finding.get("counterexample_cases", []), role="counterexample",
            participants=participants, evidence_by_id=evidence_by_id,
        )
        observations = _case_list(
            raw_finding.get("observation_cases", []), role="observation",
            participants=participants, evidence_by_id=evidence_by_id,
        )
        if not support:
            raise InterviewV2AnalysisValidationError("analysis finding requires supporting cases")
        if {item["participant_id"] for item in support} & {item["participant_id"] for item in counter}:
            raise InterviewV2AnalysisValidationError("a participant cannot support and counter the same finding")
        finding_id = _stable_id("finding", module_input["module_id"], title, statement)
        for case in [*support, *counter, *observations]:
            case["finding_case_id"] = _stable_id(
                "case", finding_id, case["role"], case["participant_id"], *case["evidence_ids"]
            )
            case["finding_id"] = finding_id
            case["evidence_revision_id"] = module_input.get("evidence_revision_id")
        confidence = raw_finding.get("confidence", 0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            raise InterviewV2AnalysisValidationError("analysis finding confidence is invalid")
        raw_limitations = raw_finding.get("limitations") or []
        if not isinstance(raw_limitations, list) or len(raw_limitations) > 50:
            raise InterviewV2AnalysisValidationError("analysis finding limitations are invalid")
        limitations = [_text(item) for item in raw_limitations if _text(item)]
        if any(len(item) > 1000 for item in limitations):
            raise InterviewV2AnalysisValidationError("analysis finding limitation is too long")
        suggestion = _text(raw_finding.get("suggestion")) or None
        if suggestion and len(suggestion) > 1000:
            raise InterviewV2AnalysisValidationError("analysis finding suggestion is too long")
        finding = {
            "finding_id": finding_id,
            "module_id": module_input["module_id"],
            "title": title,
            "statement": statement,
            "evaluation_object_id": evaluation_object_id,
            "main_question_id": main_question_id,
            "supporting_cases": support,
            "counterexample_cases": counter,
            "observation_cases": observations,
            "limitations": limitations,
            "confidence": float(confidence),
            "suggestion": suggestion,
        }
        stat = _stat_fact(
            analysis_run_id=analysis_run_id,
            finding_id=finding_id,
            module_input=module_input,
            support_cases=support,
            evaluation_object_id=evaluation_object_id,
            main_question_id=main_question_id,
        )
        finding["stat_fact_id"] = stat["stat_fact_id"]
        findings.append(finding)
        stat_facts.append(stat)
    return {
        "module_id": module_input["module_id"],
        "findings": findings,
        "stat_facts": stat_facts,
    }


__all__ = [
    "ANALYSIS_SCHEMA_VERSION",
    "InterviewV2AnalysisValidationError",
    "build_analysis_input",
    "payload_sha256",
    "validate_module_findings",
]
