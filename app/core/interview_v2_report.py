"""Deterministic Batch 5B report contracts and claim audit."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any


REPORT_SCHEMA_VERSION = "interview-report/1.0"
REPORT_CLAIM_POLICY_VERSION = "interview-report-claim-policy/1.0"
REPORT_SECTION_SPECS = (
    ("scope_and_sample", "研究范围与样本说明"),
    ("core_findings", "核心研究发现"),
    ("module_findings", "按功能模块展开的详细发现"),
    ("participant_differences", "玩家属性与反馈差异"),
    ("participant_logics", "典型玩家逻辑"),
    ("recommendations", "轻量产品建议"),
    ("evidence_and_limitations", "证据范围与研究限制"),
)
_FINDING_RE = re.compile(r"^finding_[0-9a-f]{32}$")
_STAT_RE = re.compile(r"^stat_[0-9a-f]{32}$")
_NUMBER_RE = re.compile(r"(?<![A-Za-z_])\d+(?:\.\d+)?%?")
_SELF_REPORT_MARKERS = ("玩家表示", "玩家认为", "玩家提到", "玩家反馈", "玩家说")
_COUNTER_MARKERS = ("但", "不过", "同时", "反例", "例外", "并非所有", "也有玩家")
_SECTION_CLAIM_TYPES = {
    "scope_and_sample": {"scope"},
    "core_findings": {"finding"},
    "module_findings": {"finding"},
    "participant_differences": {"difference"},
    "participant_logics": {"logic"},
    "recommendations": {"suggestion"},
    "evidence_and_limitations": {"limitation"},
}


class InterviewV2ReportValidationError(ValueError):
    """A report writer output violated the immutable report contract."""


def _text(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "").strip())


def _stable_id(prefix: str, *parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


def payload_sha256(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_report_input(
    *, project_id: str, project: dict[str, Any], analysis_revision: dict[str, Any]
) -> dict[str, Any]:
    """Freeze the complete analysis, focus and writing contract for one report."""

    if analysis_revision.get("status") != "completed":
        raise InterviewV2ReportValidationError("report requires a completed analysis run")
    findings = analysis_revision.get("findings")
    stat_facts = analysis_revision.get("stat_facts")
    if not isinstance(findings, list) or not isinstance(stat_facts, list):
        raise InterviewV2ReportValidationError("analysis report inputs are invalid")
    finding_ids = [_text(item.get("finding_id")) for item in findings if isinstance(item, dict)]
    stat_ids = [_text(item.get("stat_fact_id")) for item in stat_facts if isinstance(item, dict)]
    if (
        len(finding_ids) != len(findings)
        or len(finding_ids) != len(set(finding_ids))
        or any(not _FINDING_RE.fullmatch(item) for item in finding_ids)
        or len(stat_ids) != len(stat_facts)
        or len(stat_ids) != len(set(stat_ids))
        or any(not _STAT_RE.fullmatch(item) for item in stat_ids)
    ):
        raise InterviewV2ReportValidationError("analysis report identifiers are invalid")
    frozen_stat_facts = list(stat_facts)
    dossier_versions = (analysis_revision.get("source") or {}).get("dossier_versions") or []
    participant_ids = sorted({
        _text(item.get("participant_id")) for item in dossier_versions
        if isinstance(item, dict) and _text(item.get("participant_id"))
    })
    if participant_ids:
        frozen_stat_facts.append({
            "stat_fact_id": _stable_id("stat", analysis_revision.get("analysis_run_id"), "sample_size", *participant_ids),
            "stat_fact_schema_version": "interview-stat-fact/1.0",
            "analysis_run_id": analysis_revision.get("analysis_run_id"),
            "finding_id": None,
            "metric_type": "sample_size",
            "numerator": len(participant_ids),
            "denominator": len(participant_ids),
            "denominator_definition": "当前分析版本冻结的独立玩家",
            "numerator_cases": [{"participant_id": item, "evidence_ids": []} for item in participant_ids],
            "denominator_participant_ids": participant_ids,
            "proportion": 1.0,
        })
    frozen = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "claim_policy_version": REPORT_CLAIM_POLICY_VERSION,
        "project_id": project_id,
        "analysis_run_id": analysis_revision.get("analysis_run_id"),
        "analysis_revision_payload_sha256": analysis_revision.get("revision_payload_sha256"),
        "analysis_source": analysis_revision.get("source") or {},
        "research_focus": _text(project.get("research_focus")),
        "section_specs": [
            {"section_key": key, "title": title, "order": index + 1}
            for index, (key, title) in enumerate(REPORT_SECTION_SPECS)
        ],
        "findings": findings,
        "stat_facts": frozen_stat_facts,
        "analysis_limitations": analysis_revision.get("limitations") or [],
    }
    frozen["input_fingerprint"] = payload_sha256(frozen)
    return frozen


def _issue(
    *, code: str, severity: str, message: str, section_key: str, claim_id: str | None
) -> dict[str, Any]:
    return {
        "audit_issue_id": _stable_id("audit", code, section_key, claim_id or "", message),
        "code": code,
        "severity": severity,
        "message": message,
        "section_key": section_key,
        "claim_id": claim_id,
        "source": "deterministic",
    }


def _expected_number_tokens(stat: dict[str, Any]) -> set[str]:
    result = {str(stat.get("numerator"))}
    denominator = stat.get("denominator")
    proportion = stat.get("proportion")
    if denominator is not None:
        result.add(str(denominator))
    if isinstance(proportion, (int, float)) and not isinstance(proportion, bool):
        result.add(str(round(proportion * 100)) + "%")
        result.add(f"{proportion * 100:.1f}%")
    return result


def _derive_claim_links(
    finding_ids: list[str], finding_by_id: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str], bool, bool]:
    participant_ids: set[str] = set()
    evidence_ids: set[str] = set()
    has_observation = False
    has_counterexample = False
    for finding_id in finding_ids:
        finding = finding_by_id[finding_id]
        counter = finding.get("counterexample_cases") or []
        observation = finding.get("observation_cases") or []
        has_counterexample = has_counterexample or bool(counter)
        has_observation = has_observation or bool(observation)
        for case in [
            *(finding.get("supporting_cases") or []),
            *counter,
            *observation,
        ]:
            participant_ids.add(_text(case.get("participant_id")))
            evidence_ids.update(_text(item) for item in case.get("evidence_ids") or [])
    return sorted(participant_ids), sorted(evidence_ids), has_observation, has_counterexample


def validate_report_output(
    raw: dict[str, Any], *, report_input: dict[str, Any], report_version_id: str
) -> dict[str, Any]:
    """Validate fixed sections and derive evidence-bound claims plus audit issues."""

    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        raise InterviewV2ReportValidationError("report sections must be a list")
    raw_sections = raw["sections"]
    if len(raw_sections) != len(REPORT_SECTION_SPECS):
        raise InterviewV2ReportValidationError("report must contain every fixed section exactly once")
    finding_by_id = {item["finding_id"]: item for item in report_input["findings"]}
    stat_by_id = {item["stat_fact_id"]: item for item in report_input["stat_facts"]}
    sections: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    total_chars = 0
    suggestion_chars = 0
    for index, ((expected_key, expected_title), raw_section) in enumerate(
        zip(REPORT_SECTION_SPECS, raw_sections, strict=True)
    ):
        if not isinstance(raw_section, dict) or _text(raw_section.get("section_key")) != expected_key:
            raise InterviewV2ReportValidationError("report section order or key is invalid")
        content = _text(raw_section.get("content"))
        if not content or len(content) > 30000:
            raise InterviewV2ReportValidationError("report section content is invalid")
        total_chars += len(content)
        if expected_key == "recommendations":
            suggestion_chars += len(content)
        section_id = _stable_id("section", report_version_id, expected_key)
        section_claim_ids: list[str] = []
        raw_claims = raw_section.get("claims")
        if not isinstance(raw_claims, list) or not raw_claims or len(raw_claims) > 200:
            raise InterviewV2ReportValidationError("report section claims must be a list")
        covered_positions: set[int] = set()
        for claim_index, raw_claim in enumerate(raw_claims):
            if not isinstance(raw_claim, dict):
                raise InterviewV2ReportValidationError("report claim must be an object")
            claim_type = _text(raw_claim.get("claim_type"))
            if claim_type not in {"scope", "finding", "difference", "logic", "suggestion", "limitation"}:
                raise InterviewV2ReportValidationError("report claim type is invalid")
            if claim_type not in _SECTION_CLAIM_TYPES[expected_key]:
                raise InterviewV2ReportValidationError("report claim type does not match its section")
            text = _text(raw_claim.get("text"))
            if not text or len(text) > 4000:
                raise InterviewV2ReportValidationError("report claim text is invalid")
            start, end = raw_claim.get("start"), raw_claim.get("end")
            if (
                isinstance(start, bool) or not isinstance(start, int)
                or isinstance(end, bool) or not isinstance(end, int)
                or start < 0 or end <= start or end > len(content)
                or content[start:end] != text
            ):
                raise InterviewV2ReportValidationError("report claim source span is invalid")
            covered_positions.update(range(start, end))
            finding_ids = [_text(item) for item in raw_claim.get("finding_ids") or []]
            if len(finding_ids) != len(set(finding_ids)):
                raise InterviewV2ReportValidationError("report claim findings are duplicated")
            claim_id = _stable_id("claim", report_version_id, expected_key, claim_index, text)
            invalid_findings = [item for item in finding_ids if item not in finding_by_id]
            if invalid_findings:
                issues.append(_issue(
                    code="REPORT_CLAIM_EVIDENCE_INVALID", severity="blocking",
                    message="主张引用了当前分析版本之外的发现。",
                    section_key=expected_key, claim_id=claim_id,
                ))
                valid_finding_ids: list[str] = []
            else:
                valid_finding_ids = finding_ids
            stat_fact_id = _text(raw_claim.get("stat_fact_id")) or None
            stat = stat_by_id.get(stat_fact_id) if stat_fact_id else None
            sample_stat_allowed = bool(
                stat is not None
                and claim_type == "scope"
                and stat.get("metric_type") == "sample_size"
                and not stat.get("finding_id")
            )
            if stat_fact_id and (
                stat is None
                or (
                    not sample_stat_allowed
                    and _text(stat.get("finding_id")) not in valid_finding_ids
                )
            ):
                issues.append(_issue(
                    code="REPORT_STAT_FACT_MISMATCH", severity="blocking",
                    message="人数事实不属于该主张引用的当前发现。",
                    section_key=expected_key, claim_id=claim_id,
                ))
                stat = None
            participants: list[str] = []
            evidence: list[str] = []
            has_observation = False
            has_counterexample = False
            if valid_finding_ids:
                participants, evidence, has_observation, has_counterexample = _derive_claim_links(
                    valid_finding_ids, finding_by_id
                )
            elif sample_stat_allowed:
                participants = sorted(stat.get("denominator_participant_ids") or [])
            if claim_type not in {"scope", "limitation"} and not valid_finding_ids:
                issues.append(_issue(
                    code="REPORT_CLAIM_EVIDENCE_INVALID", severity="blocking",
                    message="事实性主张没有引用当前分析发现。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            number_tokens = set(_NUMBER_RE.findall(text))
            if number_tokens and stat is None:
                issues.append(_issue(
                    code="REPORT_STAT_FACT_MISMATCH", severity="blocking",
                    message="主张包含人数、比例或其他数字，但没有引用确定性 StatFact。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            elif stat is not None and not number_tokens <= _expected_number_tokens(stat):
                issues.append(_issue(
                    code="REPORT_STAT_FACT_MISMATCH", severity="blocking",
                    message="主张中的数字与所引用 StatFact 不一致。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            if has_observation and any(marker in text for marker in _SELF_REPORT_MARKERS):
                issues.append(_issue(
                    code="REPORT_OBSERVATION_MISATTRIBUTED", severity="blocking",
                    message="主张引用研究员观察，却将其表述为玩家自述。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            if has_counterexample and not any(marker in text for marker in _COUNTER_MARKERS):
                issues.append(_issue(
                    code="REPORT_COUNTEREVIDENCE_OMITTED", severity="warning",
                    message="引用的发现包含反例，但主张未体现限定或反例。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            if claim_type == "suggestion" and expected_key != "recommendations":
                issues.append(_issue(
                    code="REPORT_SUGGESTION_BOUNDARY_INVALID", severity="blocking",
                    message="产品建议只能出现在轻量产品建议章节。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            if expected_key == "recommendations" and claim_type != "suggestion":
                issues.append(_issue(
                    code="REPORT_SUGGESTION_BOUNDARY_INVALID", severity="blocking",
                    message="轻量产品建议章节中的主张必须明确标记为 suggestion。",
                    section_key=expected_key, claim_id=claim_id,
                ))
            claim = {
                "claim_id": claim_id,
                "report_version_id": report_version_id,
                "section_id": section_id,
                "section_key": expected_key,
                "claim_type": claim_type,
                "text": text,
                "source_span": {"start": start, "end": end},
                "finding_ids": valid_finding_ids,
                "participant_ids": participants,
                "evidence_ids": evidence,
                "stat_fact_id": stat_fact_id if stat is not None else None,
                "evidence_policy_version": REPORT_CLAIM_POLICY_VERSION,
                "qualification_status": "failed" if any(
                    item["claim_id"] == claim_id and item["severity"] == "blocking"
                    for item in issues
                ) else "passed",
                "content_sha256": payload_sha256({"text": text, "finding_ids": valid_finding_ids}),
            }
            claims.append(claim)
            section_claim_ids.append(claim_id)
        sections.append({
            "section_id": section_id,
            "report_version_id": report_version_id,
            "section_key": expected_key,
            "title": expected_title,
            "order": index + 1,
            "content": content,
            "claim_ids": section_claim_ids,
            "content_sha256": payload_sha256(content),
            "locked": False,
        })
        uncovered = [
            position for position, character in enumerate(content)
            if not character.isspace() and position not in covered_positions
        ]
        if uncovered:
            raise InterviewV2ReportValidationError("report section contains unregistered prose")
    if total_chars and (suggestion_chars / total_chars < 0.05 or suggestion_chars / total_chars > 0.15):
        issues.append(_issue(
            code="REPORT_SUGGESTION_SHARE_OUT_OF_RANGE", severity="warning",
            message="轻量产品建议篇幅未落在报告正文约 5%–15% 的提示范围内。",
            section_key="recommendations", claim_id=None,
        ))
    return {
        "sections": sections,
        "claims": claims,
        "audit_issues": issues,
        "audit_status": "audit_failed" if any(item["severity"] == "blocking" for item in issues) else "audited",
    }


def validate_model_audit(
    raw: dict[str, Any], *, sections: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Accept bounded supplemental audit findings; it cannot clear deterministic issues."""

    raw_issues = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(raw_issues, list) or len(raw_issues) > 200:
        raise InterviewV2ReportValidationError("report audit issues must be a list")
    section_keys = {item["section_key"] for item in sections}
    claim_ids = {item["claim_id"] for item in claims}
    result: list[dict[str, Any]] = []
    for item in raw_issues:
        if not isinstance(item, dict):
            raise InterviewV2ReportValidationError("report audit issue must be an object")
        severity = _text(item.get("severity"))
        code = _text(item.get("code"))
        message = _text(item.get("message"))
        section_key = _text(item.get("section_key"))
        claim_id = _text(item.get("claim_id")) or None
        if (
            severity not in {"blocking", "warning", "info"}
            or not re.fullmatch(r"REPORT_[A-Z0-9_]{3,80}", code)
            or not message or len(message) > 1000
            or section_key not in section_keys
            or (claim_id is not None and claim_id not in claim_ids)
        ):
            raise InterviewV2ReportValidationError("report audit issue target is invalid")
        result.append({
            **_issue(code=code, severity=severity, message=message, section_key=section_key, claim_id=claim_id),
            "source": "model_audit",
        })
    return result


__all__ = [
    "REPORT_CLAIM_POLICY_VERSION", "REPORT_SCHEMA_VERSION", "REPORT_SECTION_SPECS",
    "InterviewV2ReportValidationError", "build_report_input", "payload_sha256",
    "validate_model_audit", "validate_report_output",
]
