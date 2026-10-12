"""Deterministic profile presence and gated skew calculations for Insight v3."""

from __future__ import annotations

from app.services.insight_report_v3.research_context import STAT_REF_RE

from collections import Counter, defaultdict
from copy import deepcopy
import re

from app.core.column_roles import is_profile_grouping
from app.core.config import (
    INSIGHT_V3_SKEW_MIN_GAP,
    INSIGHT_V3_SKEW_MIN_LIFT,
    INSIGHT_V3_SKEW_MIN_PROFILE_COVERAGE,
    INSIGHT_V3_SKEW_MIN_SCOPE_KNOWN,
    INSIGHT_V3_SKEW_MIN_SUPPORTERS,
    INSIGHT_V3_SKEW_MIN_TARGET_SHARE,
    INSIGHT_V3_SKEW_MIN_TARGET_SUPPORTERS,
)


PROFILE_DIFFERENCE_TERMS = (
    "更突出",
    "更集中",
    "尤其明显",
    "更明显",
    "更常见",
    "倾向更强",
    "显著集中",
)
_PERCENT_RE = re.compile(r"(?:\d+(?:\.\d+)?\s*%|百分之[零〇一二两三四五六七八九十百点\d]+)")
_EXACT_PEOPLE_RE = re.compile(r"\d+\s*(?:名|位|个)?\s*(?:玩家|受访者|用户|人)")
# This is a narrow grammatical exception, not a product-keyword whitelist.
# Unclear subjects and mixed player/product sentences retain the original gate.
_PRODUCT_SUBJECT_RE = re.compile(
    r"(?:(?:而|但|同时)\s*)?"
    r"(?:(?:新版|旧版|新的|旧的|当前|改版后的|调整后的)\s*)?"
    r"(?:界面|页面|面板|菜单|按钮|图标|文字|字体|描边|边框|提示|入口|布局|导航|配色)"
    r"(?:的(?:信息|布局|文字|图标|按钮|描边|边框|配色|入口|提示))?"
    r"\s*(?:变得|显得)?\s*"
)
_HUMAN_OR_REFERENCE_RE = re.compile(
    r"玩家|受访者|用户|人群|群体|男性|女性|男生|女生|反馈|评价|偏好|感受|"
    r"他们|她们|它们|他|她|它|其|这类|这方面|这些|那些|该类"
)


def _is_clear_product_comparison(sentence: str, start: int, end: int, profile_tokens: set[str]) -> bool:
    """Accept only explicit product-subject predicates, never inferred people."""
    subject_context = sentence
    for term in PROFILE_DIFFERENCE_TERMS:
        subject_context = subject_context.replace(term, "")
    if _HUMAN_OR_REFERENCE_RE.search(subject_context) or any(token in sentence for token in profile_tokens):
        return False
    # Commas delimit clauses, but the guard above covers the entire sentence.
    prefix = re.split(r"[，,：:]", sentence[:start])[-1].strip()
    suffix = re.split(r"[，,：:]", sentence[end:])[0].strip()
    return bool(_PRODUCT_SUBJECT_RE.fullmatch(prefix)) and suffix in {"", "了"}


def _clean(value) -> str:
    return str(value or "").strip()


def analysis_profile_fields(plan: dict) -> list[dict]:
    """Return profile dimensions allowed to participate in analysis.

    ``source_keys`` mirrors names that may already be present in open-text
    source records. The first name is the stable display name used downstream.
    """
    fields: list[dict] = []
    seen_names: set[str] = set()
    for column in (plan or {}).get("columns") or []:
        if not isinstance(column, dict) or not is_profile_grouping(column):
            continue
        names = list(dict.fromkeys(
            name
            for name in (
                _clean(column.get("name_zh")),
                _clean(column.get("name")),
            )
            if name
        ))
        if not names:
            names = [f"列{column.get('index', '')}"]
        display_name = names[0]
        if display_name in seen_names:
            continue
        seen_names.add(display_name)
        fields.append({
            "profile_name": display_name,
            "source_keys": names,
            "column_index": column.get("index"),
        })
    return fields


def _record_profile_value(record: dict, field: dict) -> str:
    profile = record.get("profile") if isinstance(record, dict) else None
    if not isinstance(profile, dict):
        return ""
    for key in field["source_keys"]:
        value = _clean(profile.get(key))
        if value:
            return value
    return ""


def profile_catalog(source_registry: dict[str, dict], plan: dict) -> list[dict]:
    """List available analysis-profile values without exposing proportions."""
    catalog: list[dict] = []
    for field in analysis_profile_fields(plan):
        by_respondent: dict[str, str] = {}
        conflicts: set[str] = set()
        for record in (source_registry or {}).values():
            respondent_ref = _clean(record.get("respondent_ref"))
            value = _record_profile_value(record, field)
            if not respondent_ref or not value:
                continue
            previous = by_respondent.get(respondent_ref)
            if previous and previous != value:
                conflicts.add(respondent_ref)
            else:
                by_respondent[respondent_ref] = value
        counts = Counter(
            value
            for respondent_ref, value in by_respondent.items()
            if respondent_ref not in conflicts
        )
        if counts:
            catalog.append({
                "profile_name": field["profile_name"],
                "values": [value for value, _count in counts.most_common()],
            })
    return catalog


def profile_option_exists(
    catalog: list[dict],
    profile_name: str,
    profile_value: str,
) -> bool:
    return any(
        item.get("profile_name") == profile_name
        and profile_value in (item.get("values") or [])
        for item in catalog
    )


def finding_profile_presence(
    finding: dict,
    *,
    source_registry: dict[str, dict],
    plan: dict,
) -> list[dict]:
    """Presence among representative evidence only; never a denominator."""
    records_by_respondent: dict[str, list[dict]] = defaultdict(list)
    for response_id in finding.get("response_ids") or []:
        record = (source_registry or {}).get(response_id)
        if not isinstance(record, dict):
            continue
        respondent_ref = _clean(record.get("respondent_ref"))
        if respondent_ref:
            records_by_respondent[respondent_ref].append(record)

    presence: list[dict] = []
    for field in analysis_profile_fields(plan):
        values: set[str] = set()
        for records in records_by_respondent.values():
            respondent_values = {
                value
                for value in (_record_profile_value(record, field) for record in records)
                if value
            }
            values.update(respondent_values)
        if values:
            presence.append({
                "profile_name": field["profile_name"],
                "values": sorted(values),
                "basis": "representative_response_ids_only",
            })
    return presence


def attach_profile_presence(
    artifact: dict,
    *,
    plan: dict,
) -> dict:
    result = deepcopy(artifact)
    registry = result.get("source_registry") or {}
    for question in result.get("questions") or []:
        for finding in question.get("findings") or []:
            finding["profile_presence"] = finding_profile_presence(
                finding,
                source_registry=registry,
                plan=plan,
            )
            finding.setdefault("skewed_toward", [])
    return result


def filter_group_sources(
    source_registry: dict[str, dict],
    *,
    plan: dict,
    profile_name: str,
    profile_value: str,
) -> list[dict]:
    """Return every open-text source for respondents in one approved group."""
    field = next(
        (
            item for item in analysis_profile_fields(plan)
            if item["profile_name"] == profile_name
        ),
        None,
    )
    if field is None:
        return []
    matching_respondents = {
        _clean(record.get("respondent_ref"))
        for record in (source_registry or {}).values()
        if _record_profile_value(record, field) == profile_value
        and _clean(record.get("respondent_ref"))
    }
    return sorted(
        (
            deepcopy(record)
            for record in (source_registry or {}).values()
            if _clean(record.get("respondent_ref")) in matching_respondents
        ),
        key=lambda item: (
            int(item.get("column_index") or 0),
            int(item.get("source_index") or 0),
            _clean(item.get("response_id")),
        ),
    )


def _respondent_values(
    response_ids: list[str],
    *,
    source_registry: dict[str, dict],
    field: dict,
) -> tuple[set[str], dict[str, str], set[str]]:
    respondents: set[str] = set()
    values: dict[str, str] = {}
    conflicts: set[str] = set()
    for response_id in response_ids:
        record = source_registry.get(response_id)
        if not isinstance(record, dict):
            continue
        respondent_ref = _clean(record.get("respondent_ref"))
        if not respondent_ref:
            continue
        respondents.add(respondent_ref)
        value = _record_profile_value(record, field)
        if not value:
            continue
        previous = values.get(respondent_ref)
        if previous and previous != value:
            conflicts.add(respondent_ref)
        else:
            values[respondent_ref] = value
    for respondent_ref in conflicts:
        values.pop(respondent_ref, None)
    return respondents, values, conflicts


def preflight_skew_candidate(
    *,
    scope_response_ids: list[str],
    source_registry: dict[str, dict],
    plan: dict,
    profile_name: str,
    profile_value: str,
) -> dict:
    """Reject fixed-scope candidates that cannot pass the current skew gates.

    This check deliberately knows nothing about who supports the finding. It
    only asks whether *any* supporter subset drawn from the current question
    could satisfy every configured threshold. Eligible candidates still need
    the existing model-backed supporter enumeration and ``evaluate_skew``.
    """
    field = next(
        (
            item for item in analysis_profile_fields(plan)
            if item["profile_name"] == profile_name
        ),
        None,
    )
    if field is None:
        return {
            "status": "unavailable",
            "reason": "profile_not_analysis_scoped",
            "diagnostics": {},
        }

    scope_respondents, scope_values, scope_conflicts = _respondent_values(
        scope_response_ids,
        source_registry=source_registry,
        field=field,
    )
    scope_total = len(scope_respondents)
    scope_known = len(scope_values)
    target_scope = sum(value == profile_value for value in scope_values.values())
    coverage = scope_known / scope_total if scope_total else 0.0
    baseline_share = target_scope / scope_known if scope_known else 0.0

    # To maximize the target share while still passing the minimum known
    # supporter gate, choose the smallest allowed supporter set and fill it
    # with as many target-group respondents as the fixed scope contains.
    minimum_supporter_set = max(
        INSIGHT_V3_SKEW_MIN_SUPPORTERS,
        INSIGHT_V3_SKEW_MIN_TARGET_SUPPORTERS,
    )
    maximum_target_share = (
        min(1.0, target_scope / minimum_supporter_set)
        if minimum_supporter_set > 0 and scope_known >= minimum_supporter_set
        else 0.0
    )
    maximum_gap = maximum_target_share - baseline_share
    maximum_lift = (
        maximum_target_share / baseline_share
        if baseline_share > 0 else 0.0
    )
    diagnostics = {
        "scope_respondent_count": scope_total,
        "scope_known_profile_count": scope_known,
        "profile_coverage": round(coverage, 6),
        "target_scope_count": target_scope,
        "baseline_share": round(baseline_share, 6),
        "theoretical_max_target_share": round(maximum_target_share, 6),
        "theoretical_max_gap": round(maximum_gap, 6),
        "theoretical_max_lift": round(maximum_lift, 6),
        "scope_conflict_count": len(scope_conflicts),
    }

    fixed_checks = (
        (scope_known >= INSIGHT_V3_SKEW_MIN_SCOPE_KNOWN, "scope_known_too_small"),
        (coverage >= INSIGHT_V3_SKEW_MIN_PROFILE_COVERAGE, "profile_coverage_too_low"),
        (scope_known >= INSIGHT_V3_SKEW_MIN_SUPPORTERS, "scope_too_small_for_supporter_floor"),
        (
            target_scope >= INSIGHT_V3_SKEW_MIN_TARGET_SUPPORTERS,
            "target_scope_too_small_for_threshold",
        ),
    )
    failed = next((reason for passed, reason in fixed_checks if not passed), "")
    if failed:
        return {
            "status": "unavailable",
            "reason": failed,
            "diagnostics": diagnostics,
        }

    mathematically_reachable = all((
        maximum_target_share >= INSIGHT_V3_SKEW_MIN_TARGET_SHARE,
        maximum_gap >= INSIGHT_V3_SKEW_MIN_GAP,
        maximum_lift >= INSIGHT_V3_SKEW_MIN_LIFT,
    ))
    if not mathematically_reachable:
        return {
            "status": "unavailable",
            "reason": "skew_threshold_mathematically_unreachable",
            "diagnostics": diagnostics,
        }
    return {
        "status": "eligible",
        "reason": "preflight_passed",
        "diagnostics": diagnostics,
    }


def evaluate_skew(
    *,
    scope_response_ids: list[str],
    supporter_response_ids: list[str],
    source_registry: dict[str, dict],
    plan: dict,
    profile_name: str,
    profile_value: str,
) -> dict:
    """Compute a conservative skew; ratios remain diagnostics-only."""
    field = next(
        (
            item for item in analysis_profile_fields(plan)
            if item["profile_name"] == profile_name
        ),
        None,
    )
    if field is None:
        return {
            "status": "unavailable",
            "reason": "profile_not_analysis_scoped",
            "skewed_toward": None,
            "diagnostics": {},
        }

    scope_respondents, scope_values, scope_conflicts = _respondent_values(
        scope_response_ids,
        source_registry=source_registry,
        field=field,
    )
    supporter_respondents, supporter_values, supporter_conflicts = _respondent_values(
        supporter_response_ids,
        source_registry=source_registry,
        field=field,
    )
    supporter_respondents &= scope_respondents
    supporter_values = {
        respondent_ref: value
        for respondent_ref, value in supporter_values.items()
        if respondent_ref in supporter_respondents
    }

    scope_total = len(scope_respondents)
    scope_known = len(scope_values)
    supporter_known = len(supporter_values)
    target_scope = sum(value == profile_value for value in scope_values.values())
    target_supporters = sum(
        value == profile_value for value in supporter_values.values()
    )
    coverage = scope_known / scope_total if scope_total else 0.0
    baseline_share = target_scope / scope_known if scope_known else 0.0
    supporter_share = target_supporters / supporter_known if supporter_known else 0.0
    gap = supporter_share - baseline_share
    lift = supporter_share / baseline_share if baseline_share > 0 else 0.0
    diagnostics = {
        "scope_respondent_count": scope_total,
        "scope_known_profile_count": scope_known,
        "profile_coverage": round(coverage, 6),
        "supporter_count": len(supporter_respondents),
        "supporter_known_profile_count": supporter_known,
        "target_scope_count": target_scope,
        "target_supporter_count": target_supporters,
        "baseline_share": round(baseline_share, 6),
        "supporter_share": round(supporter_share, 6),
        "share_gap": round(gap, 6),
        "lift": round(lift, 6),
        "scope_conflict_count": len(scope_conflicts),
        "supporter_conflict_count": len(supporter_conflicts),
    }

    checks = (
        (scope_known >= INSIGHT_V3_SKEW_MIN_SCOPE_KNOWN, "scope_known_too_small"),
        (coverage >= INSIGHT_V3_SKEW_MIN_PROFILE_COVERAGE, "profile_coverage_too_low"),
        (supporter_known >= INSIGHT_V3_SKEW_MIN_SUPPORTERS, "supporters_too_few"),
        (target_supporters >= INSIGHT_V3_SKEW_MIN_TARGET_SUPPORTERS, "target_supporters_too_few"),
        (supporter_share >= INSIGHT_V3_SKEW_MIN_TARGET_SHARE, "target_share_too_low"),
        (gap >= INSIGHT_V3_SKEW_MIN_GAP, "share_gap_too_small"),
        (lift >= INSIGHT_V3_SKEW_MIN_LIFT, "lift_too_small"),
    )
    failed = next((reason for passed, reason in checks if not passed), "")
    if failed:
        return {
            "status": "not_skewed",
            "reason": failed,
            "skewed_toward": None,
            "diagnostics": diagnostics,
        }
    return {
        "status": "verified",
        "reason": "thresholds_passed",
        "skewed_toward": {
            "profile_name": profile_name,
            "profile_value": profile_value,
            "supporter_count": target_supporters,
        },
        "diagnostics": diagnostics,
    }


def validate_profile_language(
    text: str,
    *,
    findings: list[dict],
    catalog: list[dict],
) -> str | None:
    """Reject unverified comparison language and viewpoint-level precision."""
    # Stats tokens contain code-owned objective facts, never estimated viewpoint counts.
    # Callers must first validate token membership against their selected tables.
    content = STAT_REF_RE.sub("确定性客观统计", _clean(text))
    profile_tokens = {
        _clean(item.get("profile_name"))
        for item in catalog
        if _clean(item.get("profile_name"))
    }
    profile_tokens.update(
        _clean(value)
        for item in catalog
        for value in item.get("values") or []
        if _clean(value)
    )
    mentions_profile = any(token in content for token in profile_tokens)
    if mentions_profile and _PERCENT_RE.search(content):
        return "观点级画像表述不得包含百分比"
    profile_before_count = any(
        re.search(
            rf"{re.escape(token)}[^。！？；;\n]{{0,16}}"
            r"\d+\s*(?:(?:名|位)(?:玩家|受访者|用户)?|"
            r"个(?:玩家|受访者|用户|人)|玩家|受访者|用户|人|人次)",
            content,
        )
        for token in profile_tokens
    )
    count_before_profile = any(
        re.search(
            rf"\d+\s*(?:名|位|个)?\s*{re.escape(token)}"
            r"\s*(?:玩家|受访者|用户|人)?",
            content,
        )
        for token in profile_tokens
    )
    if _EXACT_PEOPLE_RE.search(content) or profile_before_count or count_before_profile:
        return "观点级画像表述不得包含精确人数"

    used_difference = next((
        term
        for sentence in re.split(r"[。！？!?；;\n]", content)
        for term in PROFILE_DIFFERENCE_TERMS
        for match in re.finditer(re.escape(term), sentence)
        if not _is_clear_product_comparison(sentence, match.start(), match.end(), profile_tokens)
    ), "")
    if not used_difference:
        return None
    verified = [
        skew
        for finding in findings
        for skew in finding.get("skewed_toward") or []
        if isinstance(skew, dict)
    ]
    if not verified:
        return f"没有 skewed_toward 时不得使用“{used_difference}”"
    if not any(
        _clean(skew.get("profile_name")) in content
        or _clean(skew.get("profile_value")) in content
        for skew in verified
    ):
        return "人群差异表述未指向已验证的画像维度或取值"
    return None


def writer_profile_payload(finding: dict) -> dict:
    """Expose only qualitative presence and approved absolute skew counts."""
    return {
        "profile_presence": deepcopy(finding.get("profile_presence") or []),
        "skewed_toward": [
            {
                "profile_name": _clean(item.get("profile_name")),
                "profile_value": _clean(item.get("profile_value")),
                "supporter_count": int(item.get("supporter_count") or 0),
            }
            for item in finding.get("skewed_toward") or []
            if isinstance(item, dict)
            and _clean(item.get("profile_name"))
            and _clean(item.get("profile_value"))
        ],
    }
