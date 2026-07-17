"""根据问卷全量回答推断高置信度跳转关系。

Google Form 的 Responses 导出只保留题目和回答，不包含原表单的跳转配置。
本模块仅使用父题答案和后续题是否为空做本地推断，不读取或发送主观题原文。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.core.config import BRANCH_ANOMALY_MIN_ANSWERS, BRANCH_MAX_LEAKAGE_RATE


_PARENT_ROLES = {"single_choice", "profile_dim"}
_SKIP_TARGET_ROLES = {"id", "mlbbid", "ignore"}


def _clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def _norm_key(value: Any) -> str:
    return _clean_text(value).casefold()


def _question_indexes(question: dict) -> list[int]:
    indexes = question.get("column_indexes")
    if isinstance(indexes, list):
        return [idx for idx in indexes if isinstance(idx, int) and idx >= 0]
    idx = question.get("index")
    return [idx] if isinstance(idx, int) and idx >= 0 else []


def _question_name(question: dict, headers: list) -> str:
    name = _clean_text(question.get("name_zh") or question.get("name"))
    if name:
        return name
    indexes = _question_indexes(question)
    if indexes and indexes[0] < len(headers):
        return _clean_text(headers[indexes[0]]) or f"题目{indexes[0] + 1}"
    return "未命名题目"


def _choice_normalizer(question: dict):
    """返回把原始选项值归入用户已确认标准选项的函数。"""
    aliases: dict[str, str] = {}
    option_order: list[str] = []

    for option in question.get("options") or []:
        canonical = _clean_text(option)
        if canonical and _norm_key(canonical) not in aliases:
            aliases[_norm_key(canonical)] = canonical
            option_order.append(canonical)

    raw_aliases = question.get("value_aliases")
    if isinstance(raw_aliases, dict):
        for raw_canonical, values in raw_aliases.items():
            canonical = _clean_text(raw_canonical)
            if not canonical:
                continue
            aliases[_norm_key(canonical)] = canonical
            if canonical not in option_order:
                option_order.append(canonical)
            if isinstance(values, list):
                for value in values:
                    key = _norm_key(value)
                    if key:
                        aliases[key] = canonical

    def normalize(value: Any) -> str:
        text = _clean_text(value)
        if not text:
            return ""
        return aliases.get(_norm_key(text), text)

    return normalize, option_order


def _row_has_answer(row: list, indexes: list[int]) -> bool:
    return any(idx < len(row) and _clean_text(row[idx]) for idx in indexes)


def _parent_profile(body: list[list], question: dict) -> dict:
    indexes = _question_indexes(question)
    if not indexes:
        return {"counts": {}, "order": [], "masks": {}}
    parent_idx = indexes[0]
    normalize, option_order = _choice_normalizer(question)
    counts: dict[str, int] = defaultdict(int)
    masks: dict[str, int] = defaultdict(int)
    for row_idx, row in enumerate(body):
        value = normalize(row[parent_idx] if parent_idx < len(row) else "")
        if value:
            counts[value] += 1
            masks[value] |= 1 << row_idx
    ordered = [option for option in option_order if option in counts]
    ordered.extend(value for value in counts if value not in ordered)
    return {
        "counts": dict(counts),
        "order": ordered,
        "masks": dict(masks),
        "row_count": len(body),
    }


def _target_profile(body: list[list], question: dict) -> dict:
    indexes = _question_indexes(question)
    mask = 0
    for row_idx, row in enumerate(body):
        if _row_has_answer(row, indexes):
            mask |= 1 << row_idx
    return {"mask": mask, "answered_count": mask.bit_count()}


def _candidate_for_target(
    headers: list,
    parent: dict,
    parent_profile: dict,
    target: dict,
    target_profile: dict,
) -> dict | None:
    parent_indexes = _question_indexes(parent)
    target_indexes = _question_indexes(target)
    if not parent_indexes or not target_indexes or min(target_indexes) <= max(parent_indexes):
        return None

    option_counts = parent_profile["counts"]
    option_order = parent_profile["order"]
    if len(option_counts) < 2 or len(option_counts) > 12:
        return None

    target_mask = target_profile["mask"]
    answered_count = target_profile["answered_count"]
    if answered_count <= 0:
        return None

    answers_by_option = {
        option: (option_mask & target_mask).bit_count()
        for option, option_mask in parent_profile["masks"].items()
    }

    response_rates = {
        option: answers_by_option.get(option, 0) / option_counts[option]
        for option in option_order
    }
    ordered_by_rate = sorted(
        option_order,
        key=lambda option: (-response_rates[option], option_order.index(option)),
    )
    subset_candidates = []
    for cut in range(1, len(ordered_by_rate)):
        # 相邻选项的作答率相同时，没有证据把它们强行拆到不同分支。
        if abs(
            response_rates[ordered_by_rate[cut - 1]]
            - response_rates[ordered_by_rate[cut]]
        ) < 1e-12:
            continue
        active_set = set(ordered_by_rate[:cut])
        active_options = [option for option in option_order if option in active_set]
        eligible_count = sum(option_counts[option] for option in active_options)
        allowed_mask = 0
        for option in active_options:
            allowed_mask |= parent_profile["masks"][option]
        answers_in_branch = (allowed_mask & target_mask).bit_count()
        leakage_count = answered_count - answers_in_branch
        if answered_count < BRANCH_ANOMALY_MIN_ANSWERS:
            if leakage_count:
                continue
        elif leakage_count / answered_count > BRANCH_MAX_LEAKAGE_RATE:
            continue

        precision = answers_in_branch / answered_count
        response_rate = answers_in_branch / eligible_count
        inactive_count = max(1, parent_profile["row_count"] - eligible_count)
        inactive_response_rate = leakage_count / inactive_count
        separation = response_rate - inactive_response_rate
        if separation <= 0:
            continue
        subset_candidates.append(
            {
                "allowed_options": active_options,
                "eligible_count": eligible_count,
                "precision": precision,
                "response_rate": response_rate,
                "inactive_response_rate": inactive_response_rate,
                "separation": separation,
                "leakage_count": leakage_count,
                "exact_match": target_mask == allowed_mask,
            }
        )

    if not subset_candidates:
        return None
    best_subset = max(
        subset_candidates,
        key=lambda item: (
            round(item["separation"], 8),
            round(item["precision"], 8),
            round(item["response_rate"], 8),
            -len(item["allowed_options"]),
        ),
    )

    distance = min(target_indexes) - max(parent_indexes)
    return {
        "parent_index": parent_indexes[0],
        "parent_name": _question_name(parent, headers),
        "allowed_options": best_subset["allowed_options"],
        "eligible_count": best_subset["eligible_count"],
        "target": {
            "indexes": target_indexes,
            "name": _question_name(target, headers),
            "answered_count": answered_count,
            "_exact_match": best_subset["exact_match"],
            "_distance": distance,
        },
        "precision": best_subset["precision"],
        "response_rate": best_subset["response_rate"],
        "separation": best_subset["separation"],
        "leakage_count": best_subset["leakage_count"],
        "distance": distance,
    }


def _candidate_rank(candidate: dict) -> tuple[float, bool, float, float, int]:
    """优先选择分支内外差异更明显、结构更完整且距离更近的父题。"""
    return (
        round(candidate["separation"], 8),
        bool(candidate["target"]["_exact_match"]),
        round(candidate["precision"], 8),
        round(candidate["response_rate"], 8),
        -candidate["distance"],
    )


def infer_branch_rules(rows: list[list], confirmed_columns: list[dict]) -> list[dict]:
    """从全量回答中推断高置信度或疑似跳转关系。

    返回结果按“父题 + 适用选项”合并，同一分支下可包含多道目标题。
    样本量不作为硬门槛；小样本要求完全吻合，结构佐证不足时标为疑似关系。
    """
    if not rows or len(rows) <= 1 or not confirmed_columns:
        return []

    headers = list(rows[0])
    body = [list(row) for row in rows[1:]]
    parents = [q for q in confirmed_columns if q.get("role") in _PARENT_ROLES]
    targets = [q for q in confirmed_columns if q.get("role") not in _SKIP_TARGET_ROLES]
    parent_profiles = [(parent, _parent_profile(body, parent)) for parent in parents]
    target_profiles = [(target, _target_profile(body, target)) for target in targets]

    selected: list[dict] = []
    for target, target_profile in target_profiles:
        candidates = [
            candidate
            for parent, parent_profile in parent_profiles
            if parent is not target
            for candidate in [
                _candidate_for_target(
                    headers,
                    parent,
                    parent_profile,
                    target,
                    target_profile,
                )
            ]
            if candidate is not None
        ]
        if candidates:
            selected.append(max(candidates, key=_candidate_rank))

    grouped: dict[tuple[int, tuple[str, ...]], dict] = {}
    for candidate in selected:
        key = (candidate["parent_index"], tuple(candidate["allowed_options"]))
        rule = grouped.setdefault(
            key,
            {
                "parent_index": candidate["parent_index"],
                "parent_name": candidate["parent_name"],
                "allowed_options": list(candidate["allowed_options"]),
                "eligible_count": candidate["eligible_count"],
                "targets": [],
                "source": "inferred_from_responses",
                "precision": 1.0,
                "response_rate": 1.0,
                "separation": 1.0,
                "leakage_count": 0,
            },
        )
        rule["targets"].append(candidate["target"])
        rule["precision"] = min(rule["precision"], candidate["precision"])
        rule["response_rate"] = min(rule["response_rate"], candidate["response_rate"])
        rule["separation"] = min(rule["separation"], candidate["separation"])
        rule["leakage_count"] = max(rule["leakage_count"], candidate["leakage_count"])

    rules = list(grouped.values())
    rules_per_parent: dict[int, int] = defaultdict(int)
    for rule in rules:
        rules_per_parent[rule["parent_index"]] += 1
    for rule in rules:
        rule["targets"].sort(key=lambda target: target["indexes"][0])
        same_condition_multiple_targets = len(rule["targets"]) >= 2
        has_sibling_branches = rules_per_parent[rule["parent_index"]] >= 2
        exact_adjacent = any(
            target.get("_exact_match") and target.get("_distance") == 1
            for target in rule["targets"]
        )
        if same_condition_multiple_targets:
            rule["confidence"] = "high"
            rule["confidence_reason"] = "同一条件对应多道后续题"
        elif has_sibling_branches:
            rule["confidence"] = "high"
            rule["confidence_reason"] = "同一父题存在相互区分的兄弟分支"
        elif exact_adjacent:
            rule["confidence"] = "high"
            rule["confidence_reason"] = "紧邻后续题且作答人群完全吻合"
        else:
            rule["confidence"] = "medium"
            rule["confidence_reason"] = "回答分布存在条件关系，但缺少足够结构佐证"
        for target in rule["targets"]:
            target.pop("_exact_match", None)
            target.pop("_distance", None)
        rule["precision"] = round(rule["precision"], 4)
        rule["response_rate"] = round(rule["response_rate"], 4)
        rule["separation"] = round(rule["separation"], 4)
    rules.sort(key=lambda rule: (rule["parent_index"], rule["targets"][0]["indexes"][0]))
    return rules


def branch_rule_for_column(branch_rules: list[dict] | None, column_index: int) -> dict | None:
    """查找某个原始列所属的跳转规则。"""
    for rule in branch_rules or []:
        for target in rule.get("targets") or []:
            if column_index in (target.get("indexes") or []):
                return rule
    return None


def branch_rule_label(rule: dict, column_index: int | None = None) -> str:
    """生成供提示词和报告上下文使用的人类可读适用条件。"""
    options = " / ".join(str(option) for option in rule.get("allowed_options") or [])
    if rule.get("confidence") == "medium":
        label = f"当前回答分布主要对应「{rule.get('parent_name') or '前置题'}」选择「{options}」的玩家（疑似条件关系）"
    else:
        label = f"推定适用于「{rule.get('parent_name') or '前置题'}」选择「{options}」的玩家"
    eligible = rule.get("eligible_count")
    if isinstance(eligible, int):
        label += f"（进入该分支 {eligible} 人）"
    if column_index is not None:
        target = next(
            (
                item
                for item in rule.get("targets") or []
                if column_index in (item.get("indexes") or [])
            ),
            None,
        )
        answered = target.get("answered_count") if target else None
        if isinstance(answered, int):
            label += f"；本题 {answered} 条有效回答"
    return label
