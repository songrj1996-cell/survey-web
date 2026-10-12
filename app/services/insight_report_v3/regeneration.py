"""Impact-gated regeneration planning for Insight Report V3."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import inspect
import json
import re

from app.core.config import (
    DEFAULT_INSIGHT_V3_FOCUS_REVISION_SYSTEM,
    DEFAULT_INSIGHT_V3_REGENERATION_CLASSIFIER_SYSTEM,
    INSIGHT_V3_REGENERATION_PROMPT_VERSION,
    LLM_PLANNER_FALLBACK_MODELS,
    LLM_PLANNER_MAX_TOKENS,
    LLM_PLANNER_MODEL,
    LLM_PLANNER_REASONING,
    LLM_STREAM_HEARTBEAT_SECONDS,
)
from app.services.report_engine import _direct_json_call


REGENERATION_SCHEMA_VERSION = "insight-v3-regeneration-v3"
DIMENSION_GUARD_VERSION = "profile-dimension-guard-v3-clause-negation"
_IMPACT_CATEGORIES = frozenset({
    "wording",
    "chapter_structure",
    "analysis_focus",
    "data_scope",
})
_CAPABILITIES = frozenset({
    "supported",
    "data_scope_change",
    "cross_chapter_multi_group",
    "ambiguous_or_other",
})
_CLASSIFICATION_FIELDS = frozenset({
    "categories",
    "supported",
    "capability",
    "reason",
    "normalized_request",
})
_FOCUS_STRING_FIELDS = (
    "core_question",
    "report_organization",
    "evidence_role",
)
_FOCUS_LIST_FIELDS = (
    "supporting_analyses",
    "expected_deliverables",
    "avoid_structures",
)
_FOCUS_FIELDS = frozenset((*_FOCUS_STRING_FIELDS, *_FOCUS_LIST_FIELDS))
_CROSS_CHAPTER_SCOPE_RE = re.compile(
    r"(?:每(?:个|一)?|各|所有)\s*(?:个)?\s*(?:(?:主题|分析|报告|正文)\s*)?"
    r"(?:part|章节|章|维度|section)|跨\s*(?:主题)?\s*章节",
    re.IGNORECASE,
)
_GROUP_COMPARISON_RE = re.compile(
    r"(?:区分|分别|对比|比较|横向|贯穿|compare|contrast|split)",
    re.IGNORECASE,
)
_PROFILE_SUBJECT_RE = re.compile(
    r"(?:玩家|人群|群体|母语|语言|性别|段位|地区|年龄|profile|group|cohort|native)",
    re.IGNORECASE,
)
_MULTI_GROUP_RE = re.compile(
    r"(?:玩家|人群|群体|group|cohort)[^，。;；\n]{0,30}"
    r"(?:和|与|及|以及|对比|比较|vs\.?|versus)[^，。;；\n]{0,30}"
    r"(?:玩家|人群|群体|group|cohort)",
    re.IGNORECASE,
)
_NATIVE_LANGUAGE_RE = re.compile(r"母语|mother\s*tongue|native\s*(?:language|speaker)", re.IGNORECASE)
_CLIENT_LANGUAGE_RE = re.compile(
    r"客户端语言|(?:客户端|游戏|程序|界面|client|game|interface).{0,8}(?:语言|language)",
    re.IGNORECASE,
)


def _clean(value) -> str:
    return str(value or "").strip()


def _safe_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def _forward_attempt_event(callback, event: dict) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def _call_with_heartbeats(coro):
    task = asyncio.create_task(coro)
    try:
        while not task.done():
            done, _ = await asyncio.wait(
                {task},
                timeout=LLM_STREAM_HEARTBEAT_SECONDS,
            )
            if task in done:
                break
            yield "heartbeat", ""
        yield "call_result", await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _validate_classification(data: dict | None) -> str | None:
    if not isinstance(data, dict) or set(data) != _CLASSIFICATION_FIELDS:
        return "根节点必须且只能包含 categories、supported、capability、reason、normalized_request"
    categories = data.get("categories")
    if (
        not isinstance(categories, list)
        or not categories
        or any(not isinstance(item, str) or item not in _IMPACT_CATEGORIES for item in categories)
        or len(categories) != len(set(categories))
    ):
        return "categories 必须是非空、不重复的允许值数组"
    if type(data.get("supported")) is not bool:
        return "supported 必须是布尔值"
    capability = data.get("capability")
    if capability not in _CAPABILITIES:
        return "capability 不在允许范围内"
    if not isinstance(data.get("reason"), str):
        return "reason 必须是字符串"
    if not _clean(data.get("normalized_request")):
        return "normalized_request 不能为空"
    if "data_scope" in categories and data["supported"]:
        return "data_scope 请求不得标记为 supported"
    if data["supported"] != (capability == "supported"):
        return "supported 与 capability 相互矛盾"
    return None


def _validate_focus_revision(data: dict | None) -> str | None:
    if not isinstance(data, dict) or set(data) != {"analysis_focus"}:
        return "根节点必须且只能包含 analysis_focus"
    focus = data.get("analysis_focus")
    if not isinstance(focus, dict) or set(focus) != _FOCUS_FIELDS:
        return "analysis_focus 必须包含且只能包含完整六字段"
    for field in _FOCUS_STRING_FIELDS:
        if not isinstance(focus.get(field), str) or not _clean(focus[field]):
            return f"analysis_focus.{field} 必须是非空字符串"
    for field in _FOCUS_LIST_FIELDS:
        values = focus.get(field)
        if not isinstance(values, list) or any(
            not isinstance(item, str) or not _clean(item) for item in values
        ):
            return f"analysis_focus.{field} 必须是非空字符串数组"
    if not focus["expected_deliverables"]:
        return "analysis_focus.expected_deliverables 不能为空"
    return None


def _matched_profile_values(text: str, options: list) -> list[str]:
    """Match longest options first so a combined option is not split in two."""
    normalized = re.sub(r"\s+", "", _clean(text).casefold())
    occupied: list[tuple[int, int]] = []
    ordered_options = [_clean(item) for item in options if _clean(item)]
    matched: set[str] = set()
    for raw in sorted(ordered_options, key=len, reverse=True):
        option = re.sub(r"\s+", "", raw.casefold())
        if not option:
            continue
        spans = [
            (match.start(), match.end())
            for match in re.finditer(re.escape(option), normalized)
            if not any(match.start() < end and match.end() > start for start, end in occupied)
        ]
        if not spans:
            continue
        matched.add(raw)
        occupied.extend(spans)
    return [item for item in ordered_options if item in matched]


def _requests_native_language(text: str) -> bool:
    # Only a local, explicit prohibition/absence can negate a mention. Never let
    # a negative clause (or an earlier mention) hide a later positive request.
    clauses = re.split(
        r"[，。；;,\n.!?？！]|但是|但|然而|不过|仍然|仍要|仍需|"
        r"并且要|并要|同时要|\b(?:but|however|yet)\b", text, flags=re.IGNORECASE,
    )
    negative_prefix = re.compile(
        r"(?:不涉及|不分析|不比较|不推断|不讨论|不包含|不研究|"
        r"(?:不要|不得|不能|不应|禁止)(?:擅自|自行|直接|再)?"
        r"(?:分析|比较|推断|讨论|研究)|不(?:做|进行|开展))"
        r"(?:任何|有关|关于|针对|玩家的?|受访者的?)*$|"
        r"(?:不要|不得|不能|不应|不会|不)(?:擅自|自行|直接)?(?:将|把)"
        r"[^，。；;,\n但而并却]{0,40}(?:当作|作为|视为|理解为|解释为|解读为|推断为|等同于)"
        r"(?:玩家的?|受访者的?)?$|"
        r"(?:未收集|未记录|没有|缺少|需要补充)(?:玩家的?|受访者的?|自报的?|真实的?)?$|"
        r"\b(?:do not|don't|does not|doesn't|must not|should not|cannot|never)\s+"
        r"(?:analyse|analyze|compare|infer|discuss|include|involve)\s+"
        r"(?:(?:the|players?'?|respondents?'?)\s+)*$|"
        r"\b(?:do not|don't|must not|should not)\s+(?:treat|interpret)\s+"
        r"(?:(?!\b(?:and|but|however|yet)\b)[^,;.\n]){0,60}\s+as\s+"
        r"(?:(?:the|players?'?)\s+)*$|"
        r"\b(?:no|missing|unrecorded)\s+(?:self[- ]reported\s+)?$|"
        r"\bno\s+(?:analysis|comparison|inference)\s+(?:of|about)\s+$",
        re.IGNORECASE,
    )
    double_negative = re.compile(
        r"不能不|不得不|不是不|并非不|没有理由不|不能没有|不是没有|并非没有|"
        r"(?:不要|不得|不能|不应)(?:忽略|排除|回避)|"
        r"\b(?:not|never)\s+(?:not|avoid|ignore|exclude)\b",
        re.IGNORECASE,
    )
    for clause in clauses:
        previous_end = 0
        for mention in _NATIVE_LANGUAGE_RE.finditer(clause):
            prefix = clause[previous_end:mention.start()]
            previous_end = mention.end()
            if double_negative.search(prefix) or not negative_prefix.search(prefix):
                return True
    return False


def _call_diagnostics(result: dict) -> dict:
    """Keep returned evidence, not an inferred cause based on elapsed time."""
    return {
        "model": _clean(result.get("model")),
        "repaired": bool(result.get("repaired")),
        "error": _clean(result.get("error"))[:300],
        "raw_len": result.get("raw_len"),
        "duration_seconds": float(result.get("duration_seconds") or 0.0),
        "initial_duration_seconds": result.get("initial_duration_seconds"),
        "repair_duration_seconds": result.get("repair_duration_seconds"),
    }


def _profile_dimension_error(instruction: str, plan: dict) -> str:
    """A matching option is not evidence of an equivalent profile dimension."""
    if not _requests_native_language(instruction):
        return ""
    names = [
        _clean(column.get("name_zh") or column.get("name"))
        for column in (plan or {}).get("columns") or []
        if isinstance(column, dict) and column.get("use_as_profile") is True
        and column.get("profile_scope", "analysis") == "analysis"
    ]
    if any(_NATIVE_LANGUAGE_RE.search(name) for name in names):
        return ""
    available = "、".join(names) or "无可用分析画像"
    return (
        f"当前数据没有母语画像字段（现有字段：{available}）。"
        "客户端语言、界面语言及回答使用的语言都不能作为玩家母语的依据。"
    )


def _resolve_cross_chapter_groups(
    instruction: str,
    plan: dict,
) -> tuple[bool, list[dict]]:
    requested = bool(
        _CROSS_CHAPTER_SCOPE_RE.search(instruction)
        and _GROUP_COMPARISON_RE.search(instruction)
        and _PROFILE_SUBJECT_RE.search(instruction)
    )
    if not requested:
        return False, []
    matches_by_profile = []
    for column in (plan or {}).get("columns") or []:
        if not isinstance(column, dict) or column.get("use_as_profile") is not True:
            continue
        if column.get("profile_scope", "analysis") != "analysis":
            continue
        profile_name = _clean(column.get("name_zh") or column.get("name"))
        if _requests_native_language(instruction) and not _NATIVE_LANGUAGE_RE.search(profile_name):
            continue
        if _CLIENT_LANGUAGE_RE.search(instruction) and not _CLIENT_LANGUAGE_RE.search(profile_name):
            continue
        values = _matched_profile_values(instruction, list(column.get("options") or []))
        if values:
            matches_by_profile.append((profile_name, values))
    if not matches_by_profile:
        return False, []
    if len(matches_by_profile) != 1 or len(matches_by_profile[0][1]) != 2:
        return True, []
    profile_name, values = matches_by_profile[0]
    return True, [
        {"profile_name": profile_name, "profile_value": value}
        for value in values
    ]


def _deterministic_unsupported(instruction: str, plan: dict | None = None) -> dict | None:
    """Reject cross-chapter requests that cannot resolve to exactly two groups."""
    dimension_error = _profile_dimension_error(instruction, plan or {})
    if dimension_error:
        return {
            "categories": ["analysis_focus"],
            "supported": False,
            "capability": "ambiguous_or_other",
            "reason": dimension_error,
            "normalized_request": instruction,
        }
    requested, groups = _resolve_cross_chapter_groups(instruction, plan or {})
    if requested and not groups:
        return {
            "categories": ["chapter_structure", "analysis_focus"],
            "supported": False,
            "capability": "cross_chapter_multi_group",
            "reason": "跨章节人群要求未能唯一映射到同一画像字段的两个现有取值",
            "normalized_request": instruction,
        }
    return None


def _classification_query(*, instruction: str, plan: dict, qualitative_context: dict | None) -> str:
    profiles = [
        {
            "index": column.get("index"),
            "name": column.get("name_zh") or column.get("name"),
            "options": column.get("options") or [],
        }
        for column in (plan or {}).get("columns") or []
        if isinstance(column, dict)
        and column.get("use_as_profile") is True
        and column.get("profile_scope", "analysis") == "analysis"
    ]
    contract = {
        "categories": ["wording"],
        "supported": True,
        "capability": "supported",
        "reason": "简短判断依据",
        "normalized_request": "不改变原意的规范化要求",
    }
    context = {
        "analysis_focus": deepcopy((plan or {}).get("analysis_focus") or {}),
        "parts": [
            {
                "name": item.get("name"),
                "column_indexes": item.get("column_indexes") or [],
                "has_filter": isinstance(item.get("filter"), dict),
            }
            for item in (plan or {}).get("parts") or []
            if isinstance(item, dict)
        ],
        "analysis_profiles": profiles,
        "business_context": deepcopy(qualitative_context or {}),
    }
    return (
        "<contract>" + _safe_json(contract) + "</contract>\n"
        "<current_snapshot>" + _safe_json(context) + "</current_snapshot>\n"
        "<regeneration_request>" + _safe_json({"instruction": instruction})
        + "</regeneration_request>\n"
        "按 system prompt 分类，只返回 contract 对应的严格 JSON。"
    )


def _focus_revision_query(
    *,
    instruction: str,
    classification: dict,
    plan: dict,
    qualitative_context: dict | None,
) -> str:
    contract = {
        "analysis_focus": {
            "core_question": "报告需要回答的核心问题",
            "report_organization": "本次新版本的章节组织方式",
            "supporting_analyses": ["支撑主线的分析"],
            "evidence_role": "统计、原话与案例各自承担的证据作用",
            "expected_deliverables": ["本次必须交付的内容"],
            "avoid_structures": ["本次必须避免的结构或写法"],
        }
    }
    plan_context = {
        "current_analysis_focus": deepcopy((plan or {}).get("analysis_focus") or {}),
        "requested_cross_chapter_groups": _resolve_cross_chapter_groups(instruction, plan)[1],
        "questionnaire_parts": [
            _clean(item.get("name"))
            for item in (plan or {}).get("parts") or []
            if isinstance(item, dict) and _clean(item.get("name"))
        ],
        "business_context": deepcopy(qualitative_context or {}),
        "analysis_profiles": [
            {"name": item.get("name_zh") or item.get("name"), "options": item.get("options") or []}
            for item in plan.get("columns") or []
            if isinstance(item, dict) and item.get("use_as_profile") is True
            and item.get("profile_scope", "analysis") == "analysis"
        ],
    }
    return (
        "<contract>" + _safe_json(contract) + "</contract>\n"
        "<current_plan_context>" + _safe_json(plan_context) + "</current_plan_context>\n"
        "<classified_request>" + _safe_json({
            "categories": classification["categories"],
            "normalized_request": classification["normalized_request"],
            "original_instruction": instruction,
        }) + "</classified_request>\n"
        "生成完整六字段 analysis_focus。原问卷 Parts 只是数据结构参考，不是必须沿用的报告章节。"
    )


def _unsupported_message(classification: dict) -> str:
    capability = classification.get("capability")
    if "当前数据没有母语画像字段" in _clean(classification.get("reason")):
        return classification["reason"] + "本次未生成新版本；请按实际画像字段提出要求。"
    if capability == "cross_chapter_multi_group":
        return (
            "跨章节人群视角只支持同一现有画像字段中可唯一识别的两个取值。"
            "本次要求未能满足该映射条件，因此未生成新版本；请明确写出画像字段和两个取值。"
        )
    if capability == "data_scope_change":
        return (
            "本次要求会改变样本、题目范围或统计口径，不能在报告重新生成阶段安全执行。"
            "本次未生成新版本；请回到数据与分析方案确认步骤调整后再生成。"
        )
    return (
        "当前无法可靠判断这条重新生成要求的影响范围，或要求超出 V3 已支持能力。"
        "为避免静默忽略，本次未生成新版本；请把要求拆成措辞、章节结构或分析主线中的一类后重试。"
    )


async def prepare_insight_regeneration(
    *,
    instruction: str,
    plan: dict,
    qualitative_context: dict | None,
    on_attempt_event=None,
):
    """Yield a gated run-specific plan before V3 chapter planning begins."""
    cleaned_instruction = _clean(instruction)
    if not cleaned_instruction:
        yield "result", {
            "supported": True,
            "effective_plan": deepcopy(plan),
            "report_instruction": "",
            "planning_instruction": "",
            "diagnostics": {
                "schema_version": REGENERATION_SCHEMA_VERSION,
                "prompt_version": INSIGHT_V3_REGENERATION_PROMPT_VERSION,
                "status": "not_requested",
                "logical_calls": 0,
                "models": [],
            },
        }
        return

    yield "progress", "正在判断本次重新生成要求会影响措辞、章节结构还是分析主线"
    cross_chapter_requested, cross_chapter_groups = _resolve_cross_chapter_groups(
        cleaned_instruction,
        plan,
    )
    classification = _deterministic_unsupported(cleaned_instruction, plan)
    classification_attempt: dict = {
        "source": "deterministic" if classification else "model",
        "model": "",
        "repaired": False,
        "error": "",
        "duration_seconds": 0.0,
    }
    models: list[str] = []
    logical_calls = 0

    if classification is None:
        async def _classification_attempt(event: dict):
            await _forward_attempt_event(on_attempt_event, {
                **event,
                "phase": "writing",
                "step": "regeneration_classification",
            })

        logical_calls += 1
        classification_result = None
        async for kind, payload in _call_with_heartbeats(_direct_json_call(
            DEFAULT_INSIGHT_V3_REGENERATION_CLASSIFIER_SYSTEM,
            _classification_query(
                instruction=cleaned_instruction,
                plan=plan,
                qualitative_context=qualitative_context,
            ),
            models=tuple(
                model for model in (LLM_PLANNER_MODEL, *LLM_PLANNER_FALLBACK_MODELS)
                if model
            ),
            max_tokens=min(2048, LLM_PLANNER_MAX_TOKENS),
            reasoning_effort=LLM_PLANNER_REASONING or None,
            validator=_validate_classification,
            on_attempt_event=_classification_attempt,
        )):
            if kind == "heartbeat":
                yield "heartbeat", ""
            else:
                classification_result = payload
        classification_attempt = {"source": "model", **_call_diagnostics(classification_result)}
        classification_attempt["output_valid"] = classification_result.get("data") is not None
        if classification_attempt["model"]:
            models.append(classification_attempt["model"])
        classification = classification_result.get("data")
        if classification is None:
            classification = {
                "categories": ["analysis_focus"],
                "supported": False,
                "capability": "ambiguous_or_other",
                "reason": "影响范围分类调用或输出校验未成功",
                "normalized_request": cleaned_instruction,
            }

    original_classification = (
        None if classification_attempt.get("output_valid") is False else deepcopy(classification)
    )
    classification = deepcopy(classification)
    if (
        cross_chapter_requested and cross_chapter_groups
        and classification.get("supported") is True
        and "data_scope" not in classification["categories"]
    ):
        classification = {
            **classification,
            "categories": list(dict.fromkeys([
                "chapter_structure",
                "analysis_focus",
                *(classification.get("categories") or []),
            ])),
            "supported": True,
            "capability": "supported",
            "reason": (
                "请求可唯一映射到同一画像字段的两个现有取值；"
                "按主题分配后逐章执行容量预检"
            ),
        }
    if "data_scope" in classification["categories"]:
        classification["supported"] = False
        classification["capability"] = "data_scope_change"

    diagnostics = {
        "schema_version": REGENERATION_SCHEMA_VERSION,
        "prompt_version": INSIGHT_V3_REGENERATION_PROMPT_VERSION,
        "dimension_guard_version": DIMENSION_GUARD_VERSION,
        "status": "classified",
        "classification": deepcopy(classification),
        "original_classification": original_classification,
        "classification_attempt": classification_attempt,
        "plan_revision_attempt": None,
        "cross_chapter_requested": cross_chapter_requested,
        "cross_chapter_groups": deepcopy(cross_chapter_groups),
        "logical_calls": logical_calls,
        "models": list(dict.fromkeys(models)),
    }
    if not classification["supported"]:
        diagnostics["status"] = "rejected"
        call_failed = classification_attempt.get("output_valid") is False
        diagnostics["rejection"] = {
            "stage": "classification",
            "code": "classification_failed" if call_failed else "unsupported_request",
        }
        yield "result", {
            "supported": False,
            "effective_plan": None,
            "report_instruction": cleaned_instruction,
            "planning_instruction": "",
            "error_message": (
                "本次重新生成的影响范围分类未成功，尚不能判断要求是否可执行。"
                "本次未生成新版本，旧报告保持不变。"
                if call_failed else _unsupported_message(classification)
            ),
            "diagnostics": diagnostics,
        }
        return

    requires_plan_revision = bool(
        {"chapter_structure", "analysis_focus"} & set(classification["categories"])
    )
    if not requires_plan_revision:
        diagnostics["status"] = "wording_only"
        yield "result", {
            "supported": True,
            "effective_plan": deepcopy(plan),
            "report_instruction": cleaned_instruction,
            "planning_instruction": "",
            "error_message": "",
            "diagnostics": diagnostics,
        }
        return

    yield "progress", "结构类要求已识别，正在生成本次运行专用的分析主线"

    async def _revision_attempt(event: dict):
        await _forward_attempt_event(on_attempt_event, {
            **event,
            "phase": "writing",
            "step": "regeneration_plan_revision",
        })

    logical_calls += 1
    revision_result = None
    async for kind, payload in _call_with_heartbeats(_direct_json_call(
        DEFAULT_INSIGHT_V3_FOCUS_REVISION_SYSTEM,
        _focus_revision_query(
            instruction=cleaned_instruction,
            classification=classification,
            plan=plan,
            qualitative_context=qualitative_context,
        ),
        models=tuple(
            model for model in (LLM_PLANNER_MODEL, *LLM_PLANNER_FALLBACK_MODELS)
            if model
        ),
        max_tokens=min(4096, LLM_PLANNER_MAX_TOKENS),
        reasoning_effort=LLM_PLANNER_REASONING or None,
        validator=_validate_focus_revision,
        on_attempt_event=_revision_attempt,
    )):
        if kind == "heartbeat":
            yield "heartbeat", ""
        else:
            revision_result = payload

    revision_attempt = _call_diagnostics(revision_result)
    if revision_attempt["model"]:
        models.append(revision_attempt["model"])
    diagnostics.update({
        "plan_revision_attempt": revision_attempt,
        "logical_calls": logical_calls,
        "models": list(dict.fromkeys(models)),
    })
    if revision_result.get("data") is None:
        diagnostics["status"] = "rejected"
        diagnostics["rejection"] = {"stage": "plan_revision", "code": "revision_failed"}
        yield "result", {
            "supported": False,
            "effective_plan": None,
            "report_instruction": cleaned_instruction,
            "planning_instruction": "",
            "error_message": (
                "已识别到章节结构或分析主线调整，但未能形成可验证的完整新方案。"
                "为避免沿用旧结构后静默忽略要求，本次未生成新版本。"
            ),
            "diagnostics": diagnostics,
        }
        return

    revised_plan = deepcopy(plan)
    revised_plan["analysis_focus"] = deepcopy(
        revision_result["data"]["analysis_focus"]
    )
    # Validate each positive field/list item independently; avoid_structures is
    # a prohibition field, not a requested analysis. Do not scan serialized JSON.
    dimension_issues = []
    for key, value in revised_plan["analysis_focus"].items():
        if key == "avoid_structures":
            continue
        values = list(enumerate(value)) if isinstance(value, list) else [(None, value)]
        for index, text in values:
            error = _profile_dimension_error(text, plan)
            if error:
                dimension_issues.append({
                    "field": key if index is None else f"{key}[{index}]",
                    "text": text, "error": error,
                })
    if dimension_issues:
        dimension_error = dimension_issues[0]["error"]
        diagnostics["status"] = "rejected"
        diagnostics["rejection"] = {"stage": "profile_dimension", "code": "dimension_mismatch"}
        diagnostics["dimension_issues"] = dimension_issues
        diagnostics["candidate_analysis_focus"] = deepcopy(revised_plan["analysis_focus"])
        diagnostics["candidate_applied"] = False
        yield "result", {
            "supported": False,
            "effective_plan": None,
            "report_instruction": cleaned_instruction,
            "planning_instruction": "",
            "error_message": dimension_error + "主线修订发生画像含义错配，本次未生成新版本。",
            "diagnostics": diagnostics,
        }
        return
    if cross_chapter_groups:
        values = [item["profile_value"] for item in cross_chapter_groups]
        profile_name = cross_chapter_groups[0]["profile_name"]
        focus = revised_plan["analysis_focus"]
        focus["report_organization"] = (
            f"{_clean(focus['report_organization'])}；每个主题章节分别呈现"
            f"{profile_name}为{values[0]}与{values[1]}玩家的定性材料，通过容量预检的章节采用章内分组，"
            "未通过的章节退回普通写法"
        )
        deliverable = (
            f"在每个主题章节分别呈现{profile_name}为{values[0]}与{values[1]}玩家的定性发现；"
            "只作分组归纳，不输出观点级比例或未经验证的强弱差异"
        )
        if deliverable not in focus["expected_deliverables"]:
            focus["expected_deliverables"].append(deliverable)
        avoid = "不得把分组归纳写成未经验证的人群比例差异或强弱排序"
        if avoid not in focus["avoid_structures"]:
            focus["avoid_structures"].append(avoid)
    diagnostics["status"] = "plan_revised"
    diagnostics["revised_analysis_focus"] = deepcopy(
        revised_plan["analysis_focus"]
    )
    yield "result", {
        "supported": True,
        "effective_plan": revised_plan,
        "report_instruction": cleaned_instruction,
        "planning_instruction": cleaned_instruction,
        "error_message": "",
        "diagnostics": diagnostics,
    }


__all__ = [
    "REGENERATION_SCHEMA_VERSION",
    "prepare_insight_regeneration",
]
