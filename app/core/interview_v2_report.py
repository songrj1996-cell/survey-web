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
_REPORT_RE = re.compile(r"^report_[0-9a-f]{32}$")
_SECTION_RE = re.compile(r"^section_[0-9a-f]{32}$")
_CLAIM_RE = re.compile(r"^claim_[0-9a-f]{32}$")
_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])\d+(?:\.\d+)?%?(?![A-Za-z0-9_])"
)
_CHINESE_NUMBER_PATTERN = r"[零〇一二两三四五六七八九十百]+"
_PERSON_OR_SAMPLE = r"(?:玩家|受访者|用户|被访者|人|样本)"
_CHINESE_COUNT_RE = re.compile(
    rf"([零〇一二两三四五六七八九十百]+)\s*(?:名|位|个|份)?({_PERSON_OR_SAMPLE})"
)
_ARABIC_COUNT_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(\d+)\s*(?:名|位|个|份)?({_PERSON_OR_SAMPLE})"
)
_CHINESE_QUANTIFIED_RE = re.compile(
    r"([零〇一二两三四五六七八九十百]+)\s*(?:名|位|个|份|票)"
)
_RATIO_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(\d+)\s*[/／]\s*(\d+)(?![A-Za-z0-9_.])"
)
_WORD_RATIO_RE = re.compile(
    r"(?<![A-Za-z0-9_.])(\d+)\s*比\s*(\d+)(?![A-Za-z0-9_.])"
)
_QUANTITY_COMPARATOR_PATTERN = (
    r"超过|高于|大于|多于|不足|不到|低于|小于|少于|至少|不低于|不少于|"
    r"至多|不超过|不高于|最多|约|大约|接近"
)
_ARABIC_PERCENT_RE = re.compile(
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})?\s*"
    r"(?<![A-Za-z0-9_])(?P<value>\d+(?:\.\d+)?)(?![A-Za-z0-9_])\s*%"
)
_CHINESE_PERCENT_RE = re.compile(
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})?\s*"
    rf"百分之\s*(?P<value>{_CHINESE_NUMBER_PATTERN})"
)
_CHINESE_TENTHS_RE = re.compile(
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})?\s*"
    r"(?P<value>[零〇一二两三四五六七八九十])成"
)
_CHINESE_FRACTION_RE = re.compile(
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})?\s*"
    rf"(?P<denominator>{_CHINESE_NUMBER_PATTERN})分之"
    rf"(?P<numerator>{_CHINESE_NUMBER_PATTERN})"
)
_COMPARATIVE_CHINESE_COUNT_RE = re.compile(
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})\s*"
    rf"(?P<value>{_CHINESE_NUMBER_PATTERN})\s*(?:名|位|个|份)?"
    rf"(?P<subject>{_PERSON_OR_SAMPLE})"
)
_COMPARATIVE_ARABIC_COUNT_RE = re.compile(
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})\s*"
    rf"(?<![A-Za-z0-9_])(?P<value>\d+)(?![A-Za-z0-9_])\s*"
    rf"(?:名|位|个|份)?(?P<subject>{_PERSON_OR_SAMPLE})"
)
_LABELED_COUNT_RE = re.compile(
    rf"(?P<label>支持人数|提及人数|样本人数|分子|分母)\s*"
    rf"(?:为|是|共|:|：)?\s*"
    rf"(?P<comparator>{_QUANTITY_COMPARATOR_PATTERN})?\s*"
    rf"(?P<value>\d+|{_CHINESE_NUMBER_PATTERN})"
)
_VAGUE_QUANTITY_RE = re.compile(
    r"(?:大多数|大部分|多数|少数|过半|近半|将近一半|接近一半|约一半|"
    r"接近半数|约半数|一半|半数|至少半数|至多半数|超过半数|"
    r"半数以上|半数以下|不到半数|不足半数|"
    r"几名|数名|多名|若干名|几位|数位|多位|若干位|"
    r"[零〇一二两三四五六七八九十百]+分之[零〇一二两三四五六七八九十百]+)"
)
_UNIVERSAL_QUANTITY_RE = re.compile(
    r"(?:所有|全部|全体)\s*(?:玩家|受访者|用户|被访者)|"
    r"每\s*(?:一\s*)?(?:名|位|个)?\s*(?:玩家|受访者|用户|被访者)|"
    r"(?:玩家|受访者|用户|被访者)\s*(?:全部|全都|均|都)|"
    r"(?:全员|人人(?:都|均)?)"
)
_ZERO_QUANTITY_RE = re.compile(
    r"(?:无人|"
    r"没有\s*(?:(?:任何|一\s*(?:名|位|个)?)\s*)?(?:玩家|受访者|用户|被访者|人)|"
    r"无\s*一\s*(?:名|位|个)?\s*(?:玩家|受访者|用户|被访者|人)|"
    r"(?:并)?无\s*(?:任何)?\s*(?:玩家|受访者|用户|被访者|人)|"
    r"(?:一\s*(?:名|位|个)?|一个)\s*(?:玩家|受访者|用户|被访者|人)\s*"
    r"(?:也\s*)?(?:没有|未))"
)
_SELF_REPORT_RE = re.compile(
    r"(?:玩家|受访者|用户|被访者)\s*"
    r"(?:称|表示|认为|觉得|感觉|感到|提到|提及|反馈|说|指出|自述|坦言|"
    r"回答|所说|描述|希望|期望|偏好|喜欢|担心|相信|理解|认可|满意|不满)"
)
_OBSERVATION_ATTRIBUTION_RE = re.compile(
    r"(?:(?:研究员|访谈员|观察者)\s*"
    r"(?:(?:在)?访谈中|(?:在)?现场|行为)?\s*(?:观察到|观察显示|记录到|看到)|"
    r"(?:(?:在)?访谈中|(?:在)?现场|行为)\s*(?:观察到|观察显示|记录到|看到)|"
    r"(?:观察到|观察显示|记录到))"
)
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
_EVIDENCE_ROLE_ORDER = ("support", "counterexample", "observation")
_EVIDENCE_CASE_FIELDS = {
    "support": "supporting_cases",
    "counterexample": "counterexample_cases",
    "observation": "observation_cases",
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


def _chinese_integer(value: str) -> int | None:
    digits = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
        "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    if not value:
        return None
    if not any(character in {"十", "百"} for character in value):
        if any(character not in digits for character in value):
            return None
        return int("".join(str(digits[character]) for character in value))
    total = 0
    current = 0
    for character in value:
        if character in digits:
            current = digits[character]
        elif character == "十":
            total += (current or 1) * 10
            current = 0
        elif character == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return total + current


def _quantity_comparator(value: str | None) -> str:
    if value in {"超过", "高于", "大于", "多于"}:
        return "gt"
    if value in {"不足", "不到", "低于", "小于", "少于"}:
        return "lt"
    if value in {"至少", "不低于", "不少于"}:
        return "ge"
    if value in {"至多", "不超过", "不高于", "最多"}:
        return "le"
    if value in {"约", "大约", "接近"}:
        return "approx"
    return "eq"


def _quantitative_tokens(text: str) -> dict[str, Any]:
    tokens = set(_NUMBER_RE.findall(text))
    numerator_counts: set[str] = set()
    denominator_counts: set[str] = set()
    numerator_count_constraints: list[tuple[str, int]] = []
    denominator_count_constraints: list[tuple[str, int]] = []
    ratios: set[tuple[str, str]] = set()
    proportion_constraints: list[tuple[str, float]] = []
    threshold_tokens: set[str] = set()
    classified_positions: set[int] = set()

    def mark(match: re.Match[str]) -> None:
        classified_positions.update(range(match.start(), match.end()))

    def overlaps_classified(match: re.Match[str]) -> bool:
        return any(
            position in classified_positions
            for position in range(match.start(), match.end())
        )

    for match in _ZERO_QUANTITY_RE.finditer(text):
        for token in _NUMBER_RE.findall(match.group(0)):
            tokens.discard(token)
        mark(match)

    for match in _RATIO_RE.finditer(text):
        ratios.add((match.group(1), match.group(2)))
        mark(match)
    for match in _WORD_RATIO_RE.finditer(text):
        ratios.add((match.group(1), match.group(2)))
        mark(match)
    for match in _LABELED_COUNT_RE.finditer(text):
        raw_value = match.group("value")
        value = (
            int(raw_value)
            if raw_value.isascii() and raw_value.isdigit()
            else _chinese_integer(raw_value)
        )
        if value is not None:
            token = str(value)
            tokens.add(token)
            target = (
                denominator_counts
                if match.group("label") in {"样本人数", "分母"}
                else numerator_counts
            )
            comparator = match.group("comparator")
            if comparator:
                constraints = (
                    denominator_count_constraints
                    if target is denominator_counts
                    else numerator_count_constraints
                )
                constraints.append((_quantity_comparator(comparator), value))
                threshold_tokens.add(token)
            else:
                target.add(token)
            mark(match)

    def record_count(
        match: re.Match[str], value: str, subject: str, comparator: str | None = None
    ) -> None:
        before = text[max(0, match.start() - 8):match.start()]
        after = text[match.end():match.end() + 3]
        denominator_context = bool(
            subject == "样本"
            or re.match(r"\s*(?:中|内|里)", after)
            or re.search(
                r"(?:样本|共纳入)\s*(?:包含|包括|共|为|数为)?\s*$",
                before,
            )
        )
        if comparator:
            constraints = (
                denominator_count_constraints
                if denominator_context
                else numerator_count_constraints
            )
            constraints.append((_quantity_comparator(comparator), int(value)))
            threshold_tokens.add(value)
        else:
            (denominator_counts if denominator_context else numerator_counts).add(value)

    for match in _COMPARATIVE_CHINESE_COUNT_RE.finditer(text):
        if overlaps_classified(match):
            continue
        value = _chinese_integer(match.group("value"))
        if value is not None:
            token = str(value)
            tokens.add(token)
            record_count(
                match, token, match.group("subject"), match.group("comparator")
            )
            mark(match)
    for match in _COMPARATIVE_ARABIC_COUNT_RE.finditer(text):
        if overlaps_classified(match):
            continue
        token = str(int(match.group("value")))
        tokens.add(token)
        record_count(
            match, token, match.group("subject"), match.group("comparator")
        )
        mark(match)

    for match in _CHINESE_COUNT_RE.finditer(text):
        if overlaps_classified(match):
            continue
        value = _chinese_integer(match.group(1))
        if value is not None:
            token = str(value)
            tokens.add(token)
            record_count(match, token, match.group(2))
            mark(match)
    for match in _ARABIC_COUNT_RE.finditer(text):
        if overlaps_classified(match):
            continue
        token = str(int(match.group(1)))
        record_count(match, token, match.group(2))
        mark(match)
    for match in _ARABIC_PERCENT_RE.finditer(text):
        raw_value = match.group("value")
        value = float(raw_value)
        tokens.discard(raw_value)
        tokens.discard(f"{raw_value}%")
        token = f"{value:g}%"
        tokens.add(token)
        if match.group("comparator"):
            threshold_tokens.add(token)
        proportion_constraints.append((
            _quantity_comparator(match.group("comparator")), value / 100
        ))
        mark(match)
    for match in _CHINESE_PERCENT_RE.finditer(text):
        value = _chinese_integer(match.group("value"))
        if value is not None:
            token = f"{value}%"
            tokens.add(token)
            if match.group("comparator"):
                threshold_tokens.add(token)
            proportion_constraints.append((
                _quantity_comparator(match.group("comparator")), value / 100
            ))
            mark(match)
    for match in _CHINESE_TENTHS_RE.finditer(text):
        value = _chinese_integer(match.group("value"))
        if value is not None:
            token = f"{value * 10}%"
            tokens.add(token)
            if match.group("comparator"):
                threshold_tokens.add(token)
            proportion_constraints.append((
                _quantity_comparator(match.group("comparator")), value / 10
            ))
            mark(match)
    for match in _CHINESE_FRACTION_RE.finditer(text):
        denominator = _chinese_integer(match.group("denominator"))
        numerator = _chinese_integer(match.group("numerator"))
        if denominator and numerator is not None:
            token = f"{numerator / denominator * 100:.1f}%"
            tokens.add(token)
            if match.group("comparator"):
                threshold_tokens.add(token)
            proportion_constraints.append((
                _quantity_comparator(match.group("comparator")),
                numerator / denominator,
            ))
            mark(match)
    half_range = any(
        marker in text for marker in (
            "不到一半", "不足一半", "超过一半", "至少一半",
            "一半以上", "一半以下", "不到半数", "不足半数",
            "超过半数", "至少半数", "至多半数", "半数以上", "半数以下",
        )
    )
    if (
        "一半" in text
        and "近半" not in text
        and "将近一半" not in text
        and not half_range
    ):
        tokens.add("50%")
    residual = "".join(
        " " if index in classified_positions else character
        for index, character in enumerate(text)
    )
    unclassified_tokens = set(_NUMBER_RE.findall(residual))
    for raw in _CHINESE_QUANTIFIED_RE.findall(residual):
        value = _chinese_integer(raw)
        if value is not None:
            token = str(value)
            tokens.add(token)
            unclassified_tokens.add(token)
    return {
        "tokens": tokens,
        "has_quantitative_language": bool(
            tokens
            or _VAGUE_QUANTITY_RE.search(text)
            or _UNIVERSAL_QUANTITY_RE.search(text)
            or _ZERO_QUANTITY_RE.search(text)
            or half_range
        ),
        "numerator_counts": numerator_counts,
        "denominator_counts": denominator_counts,
        "numerator_count_constraints": numerator_count_constraints,
        "denominator_count_constraints": denominator_count_constraints,
        "ratios": ratios,
        "proportion_constraints": proportion_constraints,
        "threshold_tokens": threshold_tokens,
        "unclassified_tokens": unclassified_tokens,
    }


def _quantity_semantics_match(
    text: str, stat: dict[str, Any], quantitative: dict[str, Any]
) -> bool:
    def comparison_matches(
        actual: float, comparator: str, threshold: float, *, tolerance: float
    ) -> bool:
        if comparator == "gt":
            return actual > threshold
        if comparator == "lt":
            return actual < threshold
        if comparator == "ge":
            return actual >= threshold
        if comparator == "le":
            return actual <= threshold
        if comparator == "approx":
            return abs(actual - threshold) <= tolerance
        return abs(actual - threshold) <= 1e-9

    numerator = str(stat.get("numerator"))
    denominator_value = stat.get("denominator")
    denominator = str(denominator_value) if denominator_value is not None else None
    if any(item != numerator for item in quantitative["numerator_counts"]):
        return False
    if quantitative["denominator_counts"] and (
        denominator is None
        or any(item != denominator for item in quantitative["denominator_counts"])
    ):
        return False
    if quantitative["ratios"] and (
        denominator is None
        or any(
            left != numerator or right != denominator
            for left, right in quantitative["ratios"]
        )
    ):
        return False
    numerator_value = stat.get("numerator")
    if quantitative["numerator_count_constraints"] and (
        not isinstance(numerator_value, (int, float))
        or isinstance(numerator_value, bool)
        or any(
            not comparison_matches(
                float(numerator_value), comparator, float(threshold), tolerance=1.0
            )
            for comparator, threshold in quantitative["numerator_count_constraints"]
        )
    ):
        return False
    if quantitative["denominator_count_constraints"] and (
        not isinstance(denominator_value, (int, float))
        or isinstance(denominator_value, bool)
        or any(
            not comparison_matches(
                float(denominator_value), comparator, float(threshold), tolerance=1.0
            )
            for comparator, threshold in quantitative["denominator_count_constraints"]
        )
    ):
        return False
    if any(item != numerator for item in quantitative["unclassified_tokens"]):
        return False
    proportion = stat.get("proportion")
    is_proportion = isinstance(proportion, (int, float)) and not isinstance(
        proportion, bool
    )
    for comparator, threshold in quantitative["proportion_constraints"]:
        if not is_proportion:
            return False
        if not comparison_matches(
            float(proportion), comparator, threshold, tolerance=0.05
        ):
            return False
    if _ZERO_QUANTITY_RE.search(text):
        return bool(is_proportion and proportion == 0)
    if _UNIVERSAL_QUANTITY_RE.search(text):
        return bool(is_proportion and proportion == 1)
    if re.search(r"(?:近半|将近一半|接近一半|约一半|接近半数|约半数)", text):
        return bool(is_proportion and abs(proportion - 0.5) <= 0.1)
    if re.search(r"(?:不超过|不高于|至多)\s*(?:一半|半数)", text):
        return bool(is_proportion and proportion <= 0.5)
    if re.search(r"(?:不少于|不低于|至少)\s*(?:一半|半数)", text):
        return bool(is_proportion and proportion >= 0.5)
    if (
        "不到一半" in text or "不足一半" in text
        or "不到半数" in text or "不足半数" in text
    ):
        return bool(is_proportion and proportion < 0.5)
    if "超过一半" in text or "超过半数" in text:
        return bool(is_proportion and proportion > 0.5)
    if (
        "至少一半" in text or "至少半数" in text
        or "一半以上" in text or "半数以上" in text
    ):
        return bool(is_proportion and proportion >= 0.5)
    if "至多半数" in text or "一半以下" in text or "半数以下" in text:
        return bool(is_proportion and proportion <= 0.5)
    if "一半" in text or "半数" in text:
        return bool(is_proportion and abs(proportion - 0.5) <= 1e-9)
    if "少数" in text:
        return bool(is_proportion and proportion < 0.5)
    if re.search(r"(?:大多数|大部分|多数|过半)", text):
        return bool(is_proportion and proportion > 0.5)
    return True


def _derive_claim_links(
    finding_ids: list[str],
    finding_by_id: dict[str, dict[str, Any]],
    evidence_roles: list[str],
) -> tuple[list[str], list[str]]:
    participant_ids: set[str] = set()
    evidence_ids: set[str] = set()
    for finding_id in finding_ids:
        finding = finding_by_id[finding_id]
        for role in evidence_roles:
            for case in _complete_role_cases(finding, role):
                participant_ids.add(_text(case.get("participant_id")))
                evidence_ids.update(
                    _text(item) for item in case.get("evidence_ids") or []
                    if _text(item)
                )
    return sorted(participant_ids), sorted(evidence_ids)


def _complete_role_cases(
    finding: dict[str, Any], role: str
) -> list[dict[str, Any]]:
    cases = finding.get(_EVIDENCE_CASE_FIELDS[role])
    if not isinstance(cases, list):
        return []
    return [
        case for case in cases
        if isinstance(case, dict)
        and _text(case.get("participant_id"))
        and any(_text(item) for item in case.get("evidence_ids") or [])
    ]


def _claim_evidence_roles(
    raw_claim: dict[str, Any], finding_ids: list[str]
) -> list[str]:
    if "evidence_roles" not in raw_claim:
        return ["support"] if finding_ids else []
    raw_roles = raw_claim.get("evidence_roles")
    if not isinstance(raw_roles, list):
        raise InterviewV2ReportValidationError("report claim evidence roles must be a list")
    roles = [_text(item) for item in raw_roles]
    if (
        len(roles) != len(set(roles))
        or any(item not in _EVIDENCE_ROLE_ORDER for item in roles)
        or (finding_ids and not roles)
        or (not finding_ids and roles)
    ):
        raise InterviewV2ReportValidationError("report claim evidence roles are invalid")
    return [role for role in _EVIDENCE_ROLE_ORDER if role in roles]


def _evidence_type_allowlist(evidence_roles: list[str]) -> list[str]:
    result: list[str] = []
    if any(role in {"support", "counterexample"} for role in evidence_roles):
        result.append("participant_self_report")
    if "observation" in evidence_roles:
        result.append("researcher_observation")
    return result


def _report_indexes(
    report_input: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not isinstance(report_input, dict):
        raise InterviewV2ReportValidationError("report input must be an object")
    findings = report_input.get("findings")
    stat_facts = report_input.get("stat_facts")
    if not isinstance(findings, list) or not isinstance(stat_facts, list):
        raise InterviewV2ReportValidationError("report findings and stat facts must be lists")
    if any(not isinstance(item, dict) for item in [*findings, *stat_facts]):
        raise InterviewV2ReportValidationError("report findings and stat facts must be objects")
    finding_ids = [_text(item.get("finding_id")) for item in findings]
    stat_ids = [_text(item.get("stat_fact_id")) for item in stat_facts]
    if (
        len(finding_ids) != len(set(finding_ids))
        or any(not _FINDING_RE.fullmatch(item) for item in finding_ids)
        or len(stat_ids) != len(set(stat_ids))
        or any(not _STAT_RE.fullmatch(item) for item in stat_ids)
    ):
        raise InterviewV2ReportValidationError("report finding or stat identifiers are invalid")
    return (
        dict(zip(finding_ids, findings, strict=True)),
        dict(zip(stat_ids, stat_facts, strict=True)),
    )


def validate_report_section_output(
    raw: dict[str, Any],
    *,
    content: str,
    report_input: dict[str, Any],
    report_version_id: str,
    section_id: str,
    section_key: str,
    section_revision: int,
    locked: bool,
) -> dict[str, Any]:
    """Validate claims extracted from one server-owned section body.

    ``content`` is deliberately separate from ``raw``: callers must keep the
    persisted user-authored body authoritative and only accept claim spans and
    references from the model output.
    """

    section_specs = {
        key: {"title": title, "order": index + 1}
        for index, (key, title) in enumerate(REPORT_SECTION_SPECS)
    }
    expected = section_specs.get(_text(section_key))
    if expected is None:
        raise InterviewV2ReportValidationError("report section key is invalid")
    if (
        not isinstance(raw, dict)
        or (raw.get("section_key") is not None and _text(raw.get("section_key")) != section_key)
    ):
        raise InterviewV2ReportValidationError("report section output does not match its section")
    if not isinstance(content, str) or not content.strip() or len(content) > 30000:
        raise InterviewV2ReportValidationError("report section content is invalid")
    if (
        not _REPORT_RE.fullmatch(_text(report_version_id))
        or not _SECTION_RE.fullmatch(_text(section_id))
    ):
        raise InterviewV2ReportValidationError("report section ownership is invalid")
    if (
        isinstance(section_revision, bool)
        or not isinstance(section_revision, int)
        or section_revision < 1
        or not isinstance(locked, bool)
    ):
        raise InterviewV2ReportValidationError("report section revision or lock is invalid")

    finding_by_id, stat_by_id = _report_indexes(report_input)
    raw_claims = raw.get("claims")
    if not isinstance(raw_claims, list) or not raw_claims or len(raw_claims) > 200:
        raise InterviewV2ReportValidationError("report section claims must be a list")

    claims: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    covered_positions: set[int] = set()
    previous_start = -1
    for claim_index, raw_claim in enumerate(raw_claims):
        if not isinstance(raw_claim, dict):
            raise InterviewV2ReportValidationError("report claim must be an object")
        claim_type = _text(raw_claim.get("claim_type"))
        if claim_type not in {"scope", "finding", "difference", "logic", "suggestion", "limitation"}:
            raise InterviewV2ReportValidationError("report claim type is invalid")
        if claim_type not in _SECTION_CLAIM_TYPES[section_key]:
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
        if start < previous_start:
            raise InterviewV2ReportValidationError("report claim source span order is invalid")
        previous_start = start
        covered_positions.update(range(start, end))
        finding_ids = [_text(item) for item in raw_claim.get("finding_ids") or []]
        if len(finding_ids) != len(set(finding_ids)):
            raise InterviewV2ReportValidationError("report claim findings are duplicated")
        declared_evidence_roles = _claim_evidence_roles(raw_claim, finding_ids)
        claim_id = _stable_id("claim", report_version_id, section_key, claim_index, text)
        invalid_findings = [item for item in finding_ids if item not in finding_by_id]
        if invalid_findings:
            issues.append(_issue(
                code="REPORT_CLAIM_EVIDENCE_INVALID", severity="blocking",
                message="主张引用了当前分析版本之外的发现。",
                section_key=section_key, claim_id=claim_id,
            ))
            valid_finding_ids: list[str] = []
        else:
            valid_finding_ids = finding_ids
        evidence_roles = declared_evidence_roles if valid_finding_ids else []
        missing_evidence_roles = [
            role for role in evidence_roles
            if not any(
                _complete_role_cases(finding_by_id[finding_id], role)
                for finding_id in valid_finding_ids
            )
        ]
        uncovered_finding_ids = [
            finding_id for finding_id in valid_finding_ids
            if not any(
                _complete_role_cases(finding_by_id[finding_id], role)
                for role in evidence_roles
            )
        ]
        if missing_evidence_roles or uncovered_finding_ids:
            issues.append(_issue(
                code="REPORT_CLAIM_EVIDENCE_INVALID", severity="blocking",
                message="主张选择的证据身份未完整覆盖每个所引用发现的有效案例。",
                section_key=section_key, claim_id=claim_id,
            ))
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
                section_key=section_key, claim_id=claim_id,
            ))
            stat = None
        participants: list[str] = []
        evidence: list[str] = []
        if valid_finding_ids:
            participants, evidence = _derive_claim_links(
                valid_finding_ids, finding_by_id, evidence_roles
            )
        elif sample_stat_allowed:
            participants = sorted(stat.get("denominator_participant_ids") or [])
        has_counterexample = any(
            finding_by_id[finding_id].get("counterexample_cases") or []
            for finding_id in valid_finding_ids
        )
        if valid_finding_ids and (not participants or not evidence):
            issues.append(_issue(
                code="REPORT_CLAIM_EVIDENCE_INVALID", severity="blocking",
                message="主张选择的证据身份没有派生出完整的参与者与证据引用。",
                section_key=section_key, claim_id=claim_id,
            ))
        if (
            stat is not None
            and not sample_stat_allowed
            and evidence_roles != ["support"]
        ):
            issues.append(_issue(
                code="REPORT_STAT_FACT_MISMATCH", severity="blocking",
                message="StatFact 只能绑定到仅使用支持案例的主张。",
                section_key=section_key, claim_id=claim_id,
            ))
            stat = None
        if claim_type not in {"scope", "limitation"} and not valid_finding_ids:
            issues.append(_issue(
                code="REPORT_CLAIM_EVIDENCE_INVALID", severity="blocking",
                message="事实性主张没有引用当前分析发现。",
                section_key=section_key, claim_id=claim_id,
            ))
        quantitative = _quantitative_tokens(text)
        number_tokens = quantitative["tokens"]
        exact_number_tokens = number_tokens - quantitative["threshold_tokens"]
        if quantitative["has_quantitative_language"] and stat is None:
            issues.append(_issue(
                code="REPORT_STAT_FACT_MISMATCH", severity="blocking",
                message="主张包含人数、比例或其他数字，但没有引用确定性 StatFact。",
                section_key=section_key, claim_id=claim_id,
            ))
        elif stat is not None and (
            not exact_number_tokens <= _expected_number_tokens(stat)
            or not _quantity_semantics_match(text, stat, quantitative)
        ):
            issues.append(_issue(
                code="REPORT_STAT_FACT_MISMATCH", severity="blocking",
                message="主张中的数字与所引用 StatFact 不一致。",
                section_key=section_key, claim_id=claim_id,
            ))
        observation_attributed = bool(_OBSERVATION_ATTRIBUTION_RE.search(text))
        if "observation" in evidence_roles and (
            not observation_attributed
            or _SELF_REPORT_RE.search(text)
        ):
            issues.append(_issue(
                code="REPORT_OBSERVATION_MISATTRIBUTED", severity="blocking",
                message="主张引用研究员观察时，必须明确标记为观察结论，且不得表述为玩家自述。",
                section_key=section_key, claim_id=claim_id,
            ))
        elif observation_attributed and "observation" not in evidence_roles:
            issues.append(_issue(
                code="REPORT_OBSERVATION_MISATTRIBUTED", severity="blocking",
                message="主张使用观察性表述时，必须明确选择研究员观察证据。",
                section_key=section_key, claim_id=claim_id,
            ))
        if has_counterexample and not any(marker in text for marker in _COUNTER_MARKERS):
            issues.append(_issue(
                code="REPORT_COUNTEREVIDENCE_OMITTED", severity="warning",
                message="引用的发现包含反例，但主张未体现限定或反例。",
                section_key=section_key, claim_id=claim_id,
            ))
        if claim_type == "suggestion" and section_key != "recommendations":
            issues.append(_issue(
                code="REPORT_SUGGESTION_BOUNDARY_INVALID", severity="blocking",
                message="产品建议只能出现在轻量产品建议章节。",
                section_key=section_key, claim_id=claim_id,
            ))
        if section_key == "recommendations" and claim_type != "suggestion":
            issues.append(_issue(
                code="REPORT_SUGGESTION_BOUNDARY_INVALID", severity="blocking",
                message="轻量产品建议章节中的主张必须明确标记为 suggestion。",
                section_key=section_key, claim_id=claim_id,
            ))
        claim_audit_status = "audit_failed" if any(
            item["claim_id"] == claim_id and item["severity"] == "blocking"
            for item in issues
        ) else "audit_passed"
        claims.append({
            "claim_id": claim_id,
            "report_version_id": report_version_id,
            "section_id": section_id,
            "section_key": section_key,
            "section_revision": section_revision,
            "claim_type": claim_type,
            "text": text,
            "source_span": {"start": start, "end": end},
            "finding_ids": valid_finding_ids,
            "evidence_roles": evidence_roles,
            "evidence_type_allowlist": _evidence_type_allowlist(evidence_roles),
            "participant_ids": participants,
            "evidence_ids": evidence,
            "stat_fact_id": stat_fact_id if stat is not None else None,
            "evidence_policy_version": REPORT_CLAIM_POLICY_VERSION,
            "qualification_status": "failed" if claim_audit_status == "audit_failed" else "passed",
            "content_sha256": payload_sha256({"text": text, "finding_ids": valid_finding_ids}),
            "audit_status": claim_audit_status,
            "superseded_by": None,
        })

    uncovered = [
        position for position, character in enumerate(content)
        if not character.isspace() and position not in covered_positions
    ]
    if uncovered:
        raise InterviewV2ReportValidationError("report section contains unregistered prose")
    audit_status = "audit_failed" if any(
        item["severity"] == "blocking" for item in issues
    ) else "audit_passed"
    return {
        "section": {
            "section_id": section_id,
            "report_version_id": report_version_id,
            "section_key": section_key,
            "title": expected["title"],
            "order": expected["order"],
            "section_revision": section_revision,
            "content": content,
            "claim_ids": [item["claim_id"] for item in claims],
            "content_sha256": payload_sha256(content),
            "locked": locked,
            "audit_status": audit_status,
        },
        "claims": claims,
        "audit_issues": issues,
        "audit_status": audit_status,
    }


def validate_report_output(
    raw: dict[str, Any], *, report_input: dict[str, Any], report_version_id: str
) -> dict[str, Any]:
    """Validate fixed sections and derive evidence-bound claims plus audit issues."""

    if not isinstance(raw, dict) or not isinstance(raw.get("sections"), list):
        raise InterviewV2ReportValidationError("report sections must be a list")
    raw_sections = raw["sections"]
    if len(raw_sections) != len(REPORT_SECTION_SPECS):
        raise InterviewV2ReportValidationError("report must contain every fixed section exactly once")
    _report_indexes(report_input)
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
        total_chars += len(content)
        if expected_key == "recommendations":
            suggestion_chars += len(content)
        section_id = _stable_id("section", report_version_id, expected_key)
        validated = validate_report_section_output(
            raw_section,
            content=content,
            report_input=report_input,
            report_version_id=report_version_id,
            section_id=section_id,
            section_key=expected_key,
            section_revision=1,
            locked=False,
        )
        sections.append(validated["section"])
        claims.extend(validated["claims"])
        issues.extend(validated["audit_issues"])
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


def _approval_pending(message: str) -> None:
    raise InterviewV2ReportValidationError(f"report claims pending audit: {message}")


def _approval_blocked(message: str) -> None:
    raise InterviewV2ReportValidationError(f"report approval blocked: {message}")


def validate_report_approval(
    revision: dict[str, Any], *, report_input: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Rebuild the current claim audit before a report can be approved.

    Cached report, section and claim statuses are only gates. The persisted
    section bodies and claims are run through the deterministic validator again
    so a forged ``audit_passed`` flag cannot make altered content approvable.
    """

    if not isinstance(revision, dict):
        _approval_blocked("report revision is invalid")
    report_version_id = _text(revision.get("report_version_id"))
    if (
        not _REPORT_RE.fullmatch(report_version_id)
        or revision.get("report_schema_version") != REPORT_SCHEMA_VERSION
    ):
        _approval_blocked("report identity or schema is invalid")
    if _text(revision.get("status")) == "stale":
        _approval_blocked("report is stale")
    report_audit_status = _text(revision.get("audit_status"))
    if "pending" in report_audit_status:
        _approval_pending("report audit is pending")
    if report_audit_status not in {"audited", "audit_passed"}:
        _approval_blocked("report audit has not passed")

    effective_input = report_input
    if effective_input is None:
        effective_input = {
            "findings": revision.get("frozen_findings"),
            "stat_facts": revision.get("frozen_stat_facts"),
        }
    try:
        _report_indexes(effective_input)
    except InterviewV2ReportValidationError as exc:
        _approval_blocked(str(exc))

    sections = revision.get("sections")
    claims = revision.get("claims")
    audit_issues = revision.get("audit_issues")
    if (
        not isinstance(sections, list)
        or not isinstance(claims, list)
        or not isinstance(audit_issues, list)
    ):
        _approval_blocked("report sections, claims or audit issues are invalid")
    if len(sections) != len(REPORT_SECTION_SPECS):
        _approval_blocked("report must contain every fixed section exactly once")
    if any(not isinstance(item, dict) for item in [*sections, *claims, *audit_issues]):
        _approval_blocked("report section, claim or audit issue is invalid")

    claim_ids = [_text(item.get("claim_id")) for item in claims]
    if (
        not claim_ids
        or len(claim_ids) != len(set(claim_ids))
        or any(not _CLAIM_RE.fullmatch(item) for item in claim_ids)
    ):
        _approval_blocked("report claim identifiers are invalid")
    claim_by_id = dict(zip(claim_ids, claims, strict=True))
    section_keys = {item[0] for item in REPORT_SECTION_SPECS}
    for issue in audit_issues:
        severity = _text(issue.get("severity"))
        issue_claim_id = _text(issue.get("claim_id")) or None
        if (
            severity not in {"blocking", "warning", "info"}
            or not re.fullmatch(r"REPORT_[A-Z0-9_]{3,80}", _text(issue.get("code")))
            or not _text(issue.get("message"))
            or _text(issue.get("section_key")) not in section_keys
            or (issue_claim_id is not None and issue_claim_id not in claim_by_id)
        ):
            _approval_blocked("report contains an invalid audit issue")
        if severity == "blocking":
            _approval_blocked("report still contains blocking audit issues")
    consumed_claim_ids: list[str] = []

    for index, ((expected_key, expected_title), section) in enumerate(
        zip(REPORT_SECTION_SPECS, sections, strict=True)
    ):
        section_id = _text(section.get("section_id"))
        section_revision = section.get("section_revision")
        section_audit_status = _text(section.get("audit_status"))
        if "pending" in section_audit_status:
            _approval_pending(f"section {expected_key} is pending")
        if section_audit_status != "audit_passed":
            _approval_blocked(f"section {expected_key} audit has not passed")
        if (
            not _SECTION_RE.fullmatch(section_id)
            or _text(section.get("report_version_id")) != report_version_id
            or _text(section.get("section_key")) != expected_key
            or _text(section.get("title")) != expected_title
            or section.get("order") != index + 1
            or isinstance(section_revision, bool)
            or not isinstance(section_revision, int)
            or section_revision < 1
            or not isinstance(section.get("locked"), bool)
        ):
            _approval_blocked(f"section {expected_key} ownership or revision is invalid")
        content = section.get("content")
        if not isinstance(content, str) or not content.strip():
            _approval_blocked(f"section {expected_key} content is invalid")
        if section.get("content_sha256") != payload_sha256(content):
            _approval_blocked(f"section {expected_key} content hash does not match")
        current_claim_ids = section.get("claim_ids")
        if (
            not isinstance(current_claim_ids, list)
            or not current_claim_ids
            or len(current_claim_ids) != len(set(current_claim_ids))
            or any(_text(item) not in claim_by_id for item in current_claim_ids)
        ):
            _approval_blocked(f"section {expected_key} claim membership is invalid")
        current_claim_ids = [_text(item) for item in current_claim_ids]
        current_claims = [claim_by_id[item] for item in current_claim_ids]
        for claim in current_claims:
            claim_audit_status = _text(claim.get("audit_status"))
            if "pending" in claim_audit_status:
                _approval_pending(f"claim {_text(claim.get('claim_id'))} is pending")
            if claim_audit_status != "audit_passed":
                _approval_blocked(f"claim {_text(claim.get('claim_id'))} audit has not passed")
            if claim.get("superseded_by") is not None:
                _approval_blocked(f"claim {_text(claim.get('claim_id'))} is superseded")

        raw_claims: list[dict[str, Any]] = []
        for claim in current_claims:
            source_span = claim.get("source_span")
            if not isinstance(source_span, dict):
                _approval_blocked(f"claim {_text(claim.get('claim_id'))} span is invalid")
            raw_claims.append({
                "claim_type": claim.get("claim_type"),
                "text": claim.get("text"),
                "start": source_span.get("start"),
                "end": source_span.get("end"),
                "finding_ids": claim.get("finding_ids"),
                "evidence_roles": claim.get("evidence_roles"),
                "stat_fact_id": claim.get("stat_fact_id"),
            })
        try:
            rebuilt = validate_report_section_output(
                {"section_key": expected_key, "claims": raw_claims},
                content=content,
                report_input=effective_input,
                report_version_id=report_version_id,
                section_id=section_id,
                section_key=expected_key,
                section_revision=section_revision,
                locked=section["locked"],
            )
        except InterviewV2ReportValidationError as exc:
            _approval_blocked(f"section {expected_key} failed deterministic validation: {exc}")
        if rebuilt["audit_status"] != "audit_passed":
            _approval_blocked(f"section {expected_key} deterministic audit failed")
        for stored_claim, rebuilt_claim in zip(current_claims, rebuilt["claims"], strict=True):
            for field in (
                "claim_id", "report_version_id", "section_id", "section_key",
                "section_revision", "claim_type", "text", "source_span", "finding_ids",
                "evidence_roles", "evidence_type_allowlist", "participant_ids",
                "evidence_ids", "stat_fact_id", "evidence_policy_version",
                "qualification_status", "content_sha256", "audit_status", "superseded_by",
            ):
                if stored_claim.get(field) != rebuilt_claim.get(field):
                    _approval_blocked(
                        f"claim {rebuilt_claim['claim_id']} field {field} does not match deterministic audit"
                    )
        consumed_claim_ids.extend(current_claim_ids)

    if (
        set(consumed_claim_ids) != set(claim_ids)
        or len(consumed_claim_ids) != len(set(consumed_claim_ids))
    ):
        _approval_blocked("report contains claims outside current section revisions")
    return {
        "report_version_id": report_version_id,
        "audit_status": "audit_passed",
        "section_count": len(sections),
        "claim_count": len(claims),
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
    "validate_model_audit", "validate_report_approval", "validate_report_output",
    "validate_report_section_output",
]
