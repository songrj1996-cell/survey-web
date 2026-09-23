"""Deterministic frequency-tier rules for Insight Report v3."""

from __future__ import annotations

from math import ceil


FREQUENCY_TIER_RULES = {
    "绝大多数": ("ratio", 0.50, 5, 0),
    "普遍": ("ratio", 0.50, 5, 0),
    "大量": ("ratio_with_floor", 0.20, 5, 6),
    "很多": ("ratio_with_floor", 0.20, 5, 6),
    "不少": ("fixed", 5, 3, 0),
    "多名": ("fixed", 5, 3, 0),
    "部分": ("fixed", 3, 3, 0),
    "少数": ("fixed", 2, 2, 0),
    "个别": ("fixed", 2, 2, 0),
    "有玩家": ("fixed", 1, 1, 0),
    "有受访者": ("fixed", 1, 1, 0),
}

# Ordered from the strongest wording to the weakest. Aliases within one group
# intentionally share a contract; different groups must remain distinguishable.
FREQUENCY_TIER_GROUPS = (
    ("绝大多数 / 普遍", "绝大多数", "普遍"),
    ("大量 / 很多", "大量", "很多"),
    ("不少 / 多名", "不少", "多名"),
    ("部分", "部分"),
    ("少数 / 个别", "少数", "个别"),
    ("有玩家 / 有受访者", "有玩家", "有受访者"),
)

CROSS_QUESTION_FREQUENCY_LABEL = "多道题均有提及"
CROSS_QUESTION_FORBIDDEN_TERMS = frozenset(
    {*FREQUENCY_TIER_RULES, "人数", "占比", "百分比", "%"}
)


def minimum_support_count(tier: str, respondent_total: int) -> int:
    """Return the inclusive lower bound for a tier, counting unique players."""
    rule = FREQUENCY_TIER_RULES.get(str(tier or "").strip())
    if rule is None:
        raise ValueError("频次档位不在白名单内")
    total = max(0, int(respondent_total))
    kind, value, _minimum_representatives, support_floor = rule
    if kind == "ratio":
        return ceil(total * value)
    if kind == "ratio_with_floor":
        return max(int(support_floor), ceil(total * value))
    return int(value)


def minimum_representative_count(tier: str) -> int:
    rule = FREQUENCY_TIER_RULES.get(str(tier or "").strip())
    if rule is None:
        raise ValueError("频次档位不在白名单内")
    return int(rule[2])


def frequency_tier_availability(respondent_total: int) -> dict[str, bool]:
    """Return tiers usable without reversing or collapsing semantic order.

    Integer counts make every tier impossible to distinguish in very small
    samples. Preserve the weaker, more conservative wording and disable a
    colliding stronger tier instead of pretending that equal or inverted
    bounds differ.
    """
    total = max(0, int(respondent_total))
    availability: dict[str, bool] = {}
    previous_weaker_support: int | None = None
    for _label, canonical, *aliases in reversed(FREQUENCY_TIER_GROUPS):
        support = minimum_support_count(canonical, total)
        representatives = minimum_representative_count(canonical)
        available = support <= total and representatives <= total
        if (
            available
            and previous_weaker_support is not None
            and support <= previous_weaker_support
        ):
            available = False
        if available:
            previous_weaker_support = support
        for tier in (canonical, *aliases):
            availability[tier] = available
    return availability


def is_frequency_tier_available(tier: str, respondent_total: int) -> bool:
    normalized = str(tier or "").strip()
    if normalized not in FREQUENCY_TIER_RULES:
        raise ValueError("频次档位不在白名单内")
    return frequency_tier_availability(respondent_total)[normalized]


def validate_frequency(
    tier: str,
    support_count: int,
    representative_respondent_count: int,
    respondent_total: int,
) -> str | None:
    """Validate a model estimate without pretending it is an exact census."""
    normalized = str(tier or "").strip()
    if normalized not in FREQUENCY_TIER_RULES:
        return "frequency_tier 不在白名单内"
    if type(support_count) is not int or support_count < 1:
        return "support_count 必须是正整数"
    if type(representative_respondent_count) is not int or representative_respondent_count < 1:
        return "代表样本必须至少包含一名玩家"
    if type(respondent_total) is not int or respondent_total < 1:
        return "当前题缺少有效玩家"
    if not is_frequency_tier_available(normalized, respondent_total):
        return "frequency_tier 在当前题不可用"
    if support_count > respondent_total:
        return "support_count 超过当前题有效玩家数"
    if support_count < representative_respondent_count:
        return "support_count 小于代表样本玩家数"
    if support_count < minimum_support_count(normalized, respondent_total):
        return "support_count 未达到频次档位下界"
    if representative_respondent_count < minimum_representative_count(normalized):
        return "代表样本玩家数未达到频次档位要求"
    return None


def model_frequency_rules(respondent_total: int) -> list[str]:
    """Render the exact per-question thresholds the model must obey."""
    total = max(0, int(respondent_total))
    availability = frequency_tier_availability(total)
    lines = []
    for label, canonical, *_aliases in FREQUENCY_TIER_GROUPS:
        support = minimum_support_count(canonical, total)
        representatives = minimum_representative_count(canonical)
        if not availability[canonical]:
            lines.append(f"- {label}：本题不可用")
        else:
            lines.append(
                f"- {label}：support_count >= {support}，"
                f"response_ids 至少 {representatives} 个"
            )
    return lines
