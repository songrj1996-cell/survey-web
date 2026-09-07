"""定性问卷观点统计：把 AI 语义分类转换为按玩家去重的确定性人数。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import re
import time

from app.core.config import (
    LLM_CROSS_QUESTION_MAX_VIEWPOINTS,
    LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS,
)
from app.services import report_engine
from app.storage.prompts import _get_cross_question_viewpoint_system_prompt


_VIEWPOINT_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*观点：")
_MENTION_BLOCK_RE = re.compile(r"(?m)^[ \t]*(?:-[ \t]+)?\*\*提及情况：")
_INFERENCE_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*分析推断：")
_VAGUE_VIEWPOINT_TERMS = ("多数玩家", "多位玩家", "部分玩家", "少数玩家")
_CROSS_QUESTION_CONTRACT_VERSION = 1
_CROSS_QUESTION_SYNTHESIS_STRATEGY = "single_pass_selective_grouping"
_CROSS_QUESTION_RUNTIME_CONTRACT = """\
<protected_cross_question_viewpoint_contract>
以下运行时契约优先于上文任何旧主题合并规则或输出示例：
1. 当前任务是筛选跨题共同观点，不是完整保留所有逐题主题。
2. 每个 viewpoint 必须由至少两个不同 source_scope_key 的候选共同支持。
3. viewpoint 数量不得超过 <max_viewpoints>；单题独有候选必须进入 excluded_candidate_ids。
4. 每个 candidate_id 必须且只能出现在一个 viewpoint 或 excluded_candidate_ids 中。
5. viewpoints 为空时，status 必须是 no_shared_viewpoints，且全部候选必须被排除。
6. 只允许输出 viewpoints，禁止输出旧主题合并协议的 themes。
</protected_cross_question_viewpoint_contract>"""


def _question_label(data: dict) -> str:
    return str(data.get("col_name") or f"列{data.get('column_index', '')}").strip()


def _report_organization(plan: dict) -> str:
    focus = plan.get("analysis_focus") if isinstance(plan, dict) else None
    if isinstance(focus, dict) and str(focus.get("report_organization") or "").strip():
        return str(focus["report_organization"]).strip()
    return "；".join(
        f"Part {index} {part.get('name', '')}"
        for index, part in enumerate(plan.get("parts") or [], 1)
    )


def _cross_question_candidates(clustered_themes: dict) -> list[dict]:
    candidates: list[dict] = []
    for scope_key, data in clustered_themes.items():
        question = _question_label(data)
        for theme in data.get("all_themes") or data.get("themes") or []:
            quotes = list(theme.get("source_quotes") or theme.get("quotes") or [])[:3]
            if not quotes or not theme.get("count"):
                continue
            candidates.append({
                "name": str(theme.get("name") or "").strip(),
                "source_scope_key": str(scope_key),
                "source_question": question,
                "description": (
                    f"来源问题：{question}。{str(theme.get('description') or '').strip()}"
                ),
                "positive_summary": theme.get("positive_summary") or None,
                "negative_summary": theme.get("negative_summary") or None,
                "representative_quotes": quotes,
            })
    return candidates


def _get_cross_question_prompt() -> str:
    return (
        _get_cross_question_viewpoint_system_prompt().rstrip()
        + "\n\n"
        + _CROSS_QUESTION_RUNTIME_CONTRACT
    )


def _build_cross_question_query(
    organization: str,
    candidates: list[dict],
) -> str:
    indexed = []
    for index, candidate in enumerate(candidates, 1):
        item = deepcopy(candidate)
        item["candidate_id"] = f"c{index:04d}"
        indexed.append(item)
    return (
        f"<report_organization>{organization}</report_organization>\n"
        f"<max_viewpoints>{LLM_CROSS_QUESTION_MAX_VIEWPOINTS}</max_viewpoints>\n"
        "<viewpoint_candidates_json>\n"
        f"{json.dumps(indexed, ensure_ascii=False)}\n"
        "</viewpoint_candidates_json>"
    )


def _validate_cross_question_viewpoints(
    data: dict | None,
    candidates: list[dict],
) -> str | None:
    if not isinstance(data, dict):
        return "JSON 根节点必须是对象"
    if "themes" in data:
        return "跨题筛选禁止返回旧协议字段 themes"
    viewpoints = data.get("viewpoints")
    excluded_ids = data.get("excluded_candidate_ids")
    status = str(data.get("status") or "").strip()
    if not isinstance(viewpoints, list):
        return "viewpoints 必须是数组"
    if not isinstance(excluded_ids, list) or not all(
        isinstance(candidate_id, str) for candidate_id in excluded_ids
    ):
        return "excluded_candidate_ids 必须是字符串数组"
    if len(viewpoints) > LLM_CROSS_QUESTION_MAX_VIEWPOINTS:
        return f"跨题观点不得超过 {LLM_CROSS_QUESTION_MAX_VIEWPOINTS} 条"
    if len(set(excluded_ids)) != len(excluded_ids):
        return "excluded_candidate_ids 不得重复"

    candidate_lookup = {
        f"c{index:04d}": candidate
        for index, candidate in enumerate(candidates, 1)
    }
    expected_ids = set(candidate_lookup)
    assigned_ids: set[str] = set()
    seen_names: set[str] = set()
    expected_viewpoint_ids = [
        f"v{index:02d}" for index in range(1, len(viewpoints) + 1)
    ]
    actual_viewpoint_ids = [
        viewpoint.get("id") if isinstance(viewpoint, dict) else None
        for viewpoint in viewpoints
    ]
    if actual_viewpoint_ids != expected_viewpoint_ids:
        return f"跨题观点 ID 必须从 v01 连续编号，期望 {expected_viewpoint_ids}"

    for viewpoint in viewpoints:
        name = str(viewpoint.get("name") or "").strip()
        description = str(viewpoint.get("description") or "").strip()
        if not name or not description:
            return f"跨题观点 {viewpoint.get('id')} 缺少 name 或 description"
        name_key = name.casefold()
        if name_key in seen_names:
            return f"跨题观点名称重复：{name}"
        seen_names.add(name_key)
        source_ids = viewpoint.get("source_candidate_ids")
        if (
            not isinstance(source_ids, list)
            or len(source_ids) < 2
            or not all(isinstance(candidate_id, str) for candidate_id in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            return f"跨题观点「{name}」必须包含至少两个不重复的候选 ID"
        invalid_ids = [
            candidate_id for candidate_id in source_ids
            if candidate_id not in expected_ids
        ]
        if invalid_ids:
            return f"跨题观点「{name}」引用了不存在的候选 ID：{invalid_ids[0]}"
        repeated_ids = [
            candidate_id for candidate_id in source_ids
            if candidate_id in assigned_ids
        ]
        if repeated_ids:
            return f"候选 ID 被重复分配：{repeated_ids[0]}"
        source_scope_keys = {
            str(candidate_lookup[candidate_id].get("source_scope_key") or "")
            for candidate_id in source_ids
        }
        if len(source_scope_keys) < 2:
            return f"跨题观点「{name}」没有跨越至少两道题"
        assigned_ids.update(source_ids)

    invalid_excluded = [
        candidate_id for candidate_id in excluded_ids
        if candidate_id not in expected_ids
    ]
    if invalid_excluded:
        return f"排除列表引用了不存在的候选 ID：{invalid_excluded[0]}"
    overlap = assigned_ids & set(excluded_ids)
    if overlap:
        return f"候选 ID 同时被选中和排除：{sorted(overlap)[0]}"
    missing = expected_ids - assigned_ids - set(excluded_ids)
    if missing:
        return f"候选 ID 未被选中或排除：{sorted(missing)[0]}"
    if viewpoints and status != "completed":
        return "存在跨题观点时 status 必须是 completed"
    if not viewpoints and status != "no_shared_viewpoints":
        return "无跨题观点时 status 必须是 no_shared_viewpoints"
    return None


def _selection_call_diagnostic(
    candidates: list[dict],
    call_result: dict,
) -> dict:
    data = call_result.get("data") if isinstance(call_result, dict) else None
    viewpoints = data.get("viewpoints") if isinstance(data, dict) else None
    excluded = data.get("excluded_candidate_ids") if isinstance(data, dict) else None
    error = str(call_result.get("error") or "")[:300]
    output_count = len(viewpoints) if isinstance(viewpoints, list) else 0
    return {
        "stage": "selection",
        "batch_index": 1,
        "input_candidate_count": len(candidates),
        "output_theme_count": output_count,
        "excluded_candidate_count": len(excluded) if isinstance(excluded, list) else 0,
        "retention_ratio": round(output_count / len(candidates), 4) if candidates else 0,
        "status": "completed" if isinstance(viewpoints, list) and not error else "failed",
        "model": str(call_result.get("model") or ""),
        "repaired": bool(call_result.get("repaired")),
        "raw_len": int(call_result.get("raw_len") or 0),
        "error": error,
        "error_type": _diagnostic_error_type(error) if error else "",
        "finish_reason": "length" if "finish_reason=length" in error else "",
        "duration_seconds": float(call_result.get("duration_seconds") or 0),
    }


def _validated_viewpoints_as_themes(data: dict, candidates: list[dict]) -> list[dict]:
    candidate_lookup = {
        f"c{index:04d}": candidate
        for index, candidate in enumerate(candidates, 1)
    }
    themes = []
    for viewpoint in data.get("viewpoints") or []:
        source_ids = viewpoint["source_candidate_ids"]
        source_candidates = [
            candidate_lookup[candidate_id] for candidate_id in source_ids
        ]
        themes.append({
            "id": viewpoint["id"],
            "name": viewpoint["name"],
            "description": viewpoint["description"],
            "source_candidate_ids": list(source_ids),
            "source_scope_keys": sorted({
                str(candidate.get("source_scope_key") or "")
                for candidate in source_candidates
            }),
            "representative_quotes": list(dict.fromkeys(
                quote
                for candidate in source_candidates
                for quote in candidate.get("representative_quotes") or []
                if isinstance(quote, str) and quote.strip()
            ))[:3],
        })
    return themes


def _flatten_evidence(open_text: dict, plan: dict, headers: list[str]) -> list[dict]:
    evidence: list[dict] = []
    for scope_key, col_idx, part_index, part, entries in report_engine._open_text_scopes(
        open_text, plan
    ):
        col = next(
            (item for item in plan.get("columns") or [] if item.get("index") == col_idx),
            None,
        )
        question = (col and col.get("name")) or (
            headers[col_idx] if isinstance(col_idx, int) and col_idx < len(headers)
            else f"列{col_idx}"
        )
        question = report_engine._question_name_with_branch(question, plan, col_idx)
        filter_desc = report_engine._part_filter_desc(part, plan)
        if filter_desc:
            question = f"Part {part_index} {part.get('name', '')} / {question}【{filter_desc}】"
        for entry_index, entry in enumerate(entries):
            text = str(entry.get("text") or "").strip()
            if not text:
                continue
            evidence.append({
                **entry,
                "text": f"【问题：{question}】{text}",
                "raw_text": text,
                "scope_key": str(scope_key),
                "question": question,
                "respondent_key": str(
                    entry.get("respondent_key")
                    or f"scope:{scope_key}:entry:{entry_index}"
                ),
            })
    return evidence


async def build_report_viewpoint_stats(
    clustered_themes: dict,
    open_text: dict,
    plan: dict,
    headers: list[str],
    *,
    on_attempt_event=None,
):
    """筛选跨题共同观点并回查原文；整个阶段受同一个 deadline 约束。"""
    synthesis_started = time.monotonic()
    candidates = _cross_question_candidates(clustered_themes)
    evidence = _flatten_evidence(open_text, plan, headers)
    if len(clustered_themes) < 2 or not candidates or not evidence:
        yield ("result", [])
        return

    stage_deadline = synthesis_started + LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS
    batches = [
        evidence[index:index + report_engine.BATCH_SIZE]
        for index in range(0, len(evidence), report_engine.BATCH_SIZE)
    ]
    planned_logical_call_count = 1 + len(batches)
    yield (
        "analysis_progress",
        {
            "phase": "synthesis",
            "phase_index": 2,
            "phase_total": 4,
            "status": "active",
            "step": "selecting",
            "message": f"正在一次筛选 {len(candidates)} 个逐题候选中的跨题共同观点",
            "planned_logical_call_count": planned_logical_call_count,
            "stage_budget_seconds": LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS,
            "impact": "none",
        },
    )
    organization = _report_organization(plan)
    repair_events: asyncio.Queue = asyncio.Queue()
    selection_result: dict = {}

    async def _select():
        return await report_engine._direct_json_call(
            _get_cross_question_prompt(),
            _build_cross_question_query(organization, candidates),
            models=(
                report_engine.LLM_THEME_MERGE_MODEL,
                *report_engine.LLM_THEME_MERGE_FALLBACK_MODELS,
            ),
            max_tokens=report_engine.LLM_THEME_MERGE_MAX_TOKENS,
            reasoning_effort=report_engine.LLM_THEME_MERGE_REASONING or None,
            validator=lambda data: _validate_cross_question_viewpoints(data, candidates),
            on_repair=lambda error: repair_events.put_nowait({
                "stage": "selection",
                "batch_index": 1,
                "error": str(error)[:300],
            }),
            on_attempt_event=on_attempt_event,
        )

    stage_timed_out = False
    try:
        async for event_type, payload in report_engine._run_bounded_calls(
            [_select],
            1,
            repair_events,
            deadline=stage_deadline,
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
            elif event_type == "call_progress":
                yield (
                    "analysis_progress",
                    {
                        "phase": "synthesis",
                        "phase_index": 2,
                        "phase_total": 4,
                        "status": "retrying",
                        "step": "selecting",
                        "message": "跨题筛选未通过独立契约，正在预算内修正一次",
                        "impact": "逐题主题和原文仍完整保留",
                        **payload,
                    },
                )
            else:
                _batch_index, selection_result = payload
    except asyncio.TimeoutError:
        stage_timed_out = True

    completed_logical_call_count = 0 if stage_timed_out else 1
    if not selection_result:
        selection_result = {
            "data": None,
            "model": "",
            "raw_len": 0,
            "repaired": False,
            "error": "stage_timeout" if stage_timed_out else "selection_failed",
            "duration_seconds": round(time.monotonic() - synthesis_started, 3),
        }

    selection_diagnostic = _selection_call_diagnostic(candidates, selection_result)
    selection_data = selection_result.get("data")
    themes = (
        _validated_viewpoints_as_themes(selection_data, candidates)
        if isinstance(selection_data, dict)
        else []
    )
    stop_reason = ""
    if stage_timed_out:
        stop_reason = "stage_timeout"
    elif not isinstance(selection_data, dict):
        error = str(selection_result.get("error") or "selection_failed")
        stop_reason = (
            "no_progress"
            if any(marker in error for marker in (
                "不得超过",
                "没有跨越",
                "旧协议字段 themes",
                "未被选中或排除",
            ))
            else "selection_failed"
        )

    synthesis_diagnostics = {
        "contract_version": _CROSS_QUESTION_CONTRACT_VERSION,
        "strategy": _CROSS_QUESTION_SYNTHESIS_STRATEGY,
        "status": "failed" if stop_reason else "completed",
        "stop_reason": stop_reason,
        "stage_budget_seconds": LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS,
        "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
        "planned_logical_call_count": planned_logical_call_count,
        "completed_logical_call_count": completed_logical_call_count,
        "input_candidate_count": len(candidates),
        "final_input_candidate_count": len(themes),
        "selected_candidate_count": sum(
            len(theme.get("source_candidate_ids") or []) for theme in themes
        ),
        "excluded_candidate_count": len(
            selection_data.get("excluded_candidate_ids") or []
        ) if isinstance(selection_data, dict) else 0,
        "reduction_levels": 0,
        "partial_failure_count": 0,
        "classification_batch_count": len(batches),
        "classification_fallback_count": 0,
        "final_viewpoint_count": 0,
        "calls": [selection_diagnostic],
    }

    if stop_reason:
        yield ("diagnostics", synthesis_diagnostics)
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "degraded",
                "step": "completed",
                "message": "跨题观点筛选在整体预算内停止，继续使用逐题结果撰写报告",
                "impact": "逐题主题和原文均保留，本次不生成跨题共同观点",
                "stop_reason": stop_reason,
                "elapsed_seconds": synthesis_diagnostics["elapsed_seconds"],
            },
        )
        yield ("result", [])
        return

    if not themes:
        synthesis_diagnostics["status"] = "completed_empty"
        synthesis_diagnostics["stop_reason"] = "no_shared_viewpoints"
        yield ("diagnostics", synthesis_diagnostics)
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "completed",
                "step": "completed",
                "message": "已检查全部逐题候选，本次没有满足跨题契约的共同观点",
                "impact": "none",
                "elapsed_seconds": synthesis_diagnostics["elapsed_seconds"],
            },
        )
        yield ("result", [])
        return

    if selection_result.get("repaired"):
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "recovered",
                "step": "selecting",
                "message": "跨题筛选自动修正成功，正在回查全部原文",
                "impact": "none",
            },
        )

    yield (
        "analysis_progress",
        {
            "phase": "synthesis",
            "phase_index": 2,
            "phase_total": 4,
            "status": "active",
            "step": "classifying",
            "message": f"正在回查全部原文并统计跨题观点（共 {len(batches)} 批）",
            "impact": "none",
        },
    )
    factories = []
    for batch in batches:
        async def _classify(batch=batch):
            return await report_engine._classify_batch_direct(
                f"跨题报告观点；报告组织方式：{organization}",
                themes,
                batch,
                on_attempt_event=on_attempt_event,
            )
        factories.append(_classify)

    classified: dict[int, dict] = {}
    try:
        async for event_type, payload in report_engine._run_bounded_calls(
            factories,
            report_engine.LLM_CLASSIFY_CONCURRENCY,
            deadline=stage_deadline,
        ):
            if event_type == "heartbeat":
                yield ("heartbeat", "")
            else:
                batch_index, result = payload
                classified[batch_index] = result
    except asyncio.TimeoutError:
        synthesis_diagnostics.update({
            "status": "failed",
            "stop_reason": "stage_timeout",
            "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
            "completed_logical_call_count": 1 + len(classified),
        })
        yield ("diagnostics", synthesis_diagnostics)
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "degraded",
                "step": "completed",
                "message": "跨题原文回查达到整体预算，继续使用逐题结果撰写报告",
                "impact": "逐题主题和原文均保留，本次不使用不完整的跨题统计",
                "stop_reason": "stage_timeout",
                "elapsed_seconds": synthesis_diagnostics["elapsed_seconds"],
            },
        )
        yield ("result", [])
        return

    members = {theme["id"]: set() for theme in themes}
    sources = {theme["id"]: set() for theme in themes}
    quotes = {theme["id"]: [] for theme in themes}
    respondents_by_scope: dict[str, set[str]] = {}
    for item in evidence:
        respondents_by_scope.setdefault(item["scope_key"], set()).add(
            item["respondent_key"]
        )

    for batch_index, classification in sorted(classified.items()):
        batch = batches[batch_index]
        for classified_item in classification.get("classifications") or []:
            response_index = int(classified_item["response_id"])
            if not 0 <= response_index < len(batch):
                continue
            source = batch[response_index]
            for assignment in classified_item.get("assignments") or []:
                theme_id = assignment.get("theme_id")
                if theme_id not in members:
                    continue
                members[theme_id].add(source["respondent_key"])
                sources[theme_id].add(source["scope_key"])
                if source["raw_text"] not in quotes[theme_id] and len(quotes[theme_id]) < 6:
                    quotes[theme_id].append(source["raw_text"])

    result = []
    for theme in themes:
        theme_id = theme["id"]
        count = len(members[theme_id])
        source_scopes = sources[theme_id]
        denominator_members = set().union(
            *(respondents_by_scope[scope] for scope in source_scopes)
        ) if source_scopes else set()
        denominator = len(denominator_members)
        if not count or not denominator or len(source_scopes) < 2:
            continue
        result.append({
            "id": f"RVIEW:{theme_id}",
            "name": theme["name"],
            "description": theme.get("description", ""),
            "count": count,
            "denominator": denominator,
            "percentage": round(count / denominator * 100, 1),
            "source_questions": sorted({
                item["question"] for item in evidence
                if item["scope_key"] in source_scopes
            }),
            "source_scope_keys": sorted(source_scopes),
            "quotes": quotes[theme_id],
        })
    result.sort(key=lambda item: item["count"], reverse=True)
    fallback_count = sum(
        item.get("fallback_count", 0) for item in classified.values()
    )
    synthesis_diagnostics.update({
        "status": (
            "degraded"
            if fallback_count or not result
            else "recovered"
            if selection_result.get("repaired")
            else "completed"
        ),
        "stop_reason": "no_final_viewpoints" if not result else "",
        "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
        "completed_logical_call_count": 1 + len(classified),
        "classification_fallback_count": fallback_count,
        "final_viewpoint_count": len(result),
    })
    yield ("diagnostics", synthesis_diagnostics)
    yield (
        "analysis_progress",
        {
            "phase": "synthesis",
            "phase_index": 2,
            "phase_total": 4,
            "status": "degraded" if fallback_count or not result else "completed",
            "step": "completed",
            "viewpoint_count": len(result),
            "message": f"跨题归纳完成，共形成 {len(result)} 个跨题观点",
            "impact": (
                f"有 {fallback_count} 条回答未能归入跨题观点；逐题结果和原文不受影响"
                if fallback_count
                else "筛选出的共同观点未在至少两道题原文中形成有效统计"
                if not result
                else "none"
            ),
            "elapsed_seconds": synthesis_diagnostics["elapsed_seconds"],
        },
    )
    yield ("result", result)

def render_viewpoint_stats(
    clustered_themes: dict,
    report_viewpoints: list[dict],
    *,
    part_index: int | None = None,
) -> str:
    """渲染给 Writer 的只读观点目录；可严格裁到单个 Part。"""
    selected_scope_keys = {
        str(scope_key)
        for scope_key, data in clustered_themes.items()
        if part_index is None or int(data.get("part_index") or 0) == part_index
    }
    lines = [
        "<subjective_viewpoint_stats>",
        "口径：人数均按玩家去重；同一玩家可提及多个观点，所以占比之和可能超过100%。",
        "只有本目录中的观点才可写“X名玩家提及”；目录外的综合判断必须标为“分析推断”。",
        "",
        "## 单题观点",
    ]
    for scope_key, data in clustered_themes.items():
        if part_index is not None and str(scope_key) not in selected_scope_keys:
            continue
        question = _question_label(data)
        denominator = int(data.get("total") or 0)
        for theme in data.get("all_themes") or data.get("themes") or []:
            count = int(theme.get("count") or 0)
            if not count or not denominator:
                continue
            lines.append(
                f"- [QVIEW:{scope_key}:{theme['id']}] {question}｜{theme['name']}："
                f"{count}名玩家提及，占本题{denominator}名有效回答玩家的{theme['percentage']}%。"
            )

    selected_report_viewpoints = []
    for item in report_viewpoints:
        source_scope_keys = {
            str(scope_key) for scope_key in item.get("source_scope_keys") or []
        }
        if (
            part_index is None
            or not source_scope_keys
            or source_scope_keys & selected_scope_keys
        ):
            selected_report_viewpoints.append(item)
    if selected_report_viewpoints:
        lines.extend(["", "## 跨题重组观点"])
        for item in selected_report_viewpoints:
            sources = "；".join(item.get("source_questions") or [])
            lines.append(
                f"- [{item['id']}] {item['name']}：{item['count']}名玩家提及，"
                f"占相关题目{item['denominator']}名有效回答玩家的{item['percentage']}%；"
                f"来源题目：{sources}。"
            )
    lines.append("</subjective_viewpoint_stats>")
    return "\n".join(lines)


def _diagnostic_number(value, default=0):
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _diagnostic_error_type(value) -> str:
    text = str(value or "").lower()
    for error_type, markers in (
        ("timeout", ("timeout", "timed out", "超时")),
        ("rate_limit", ("rate limit", "ratelimit", "429", "限流")),
        ("authentication", ("authentication", "unauthorized", "401", "鉴权")),
        ("connection", ("connection", "connecterror", "network", "网络")),
        ("json_validation", ("json", "validation", "schema", "校验")),
        ("empty_output", ("empty", "为空", "无有效")),
    ):
        if any(marker in text for marker in markers):
            return error_type
    return "other"


def _diagnostic_error_counts(diagnostics: dict) -> tuple[dict, dict]:
    type_counts: dict[str, int] = {}
    stage_counts: dict[str, int] = {}

    def collect(value, stage: str = "unknown") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                next_stage = str(key) if str(key).startswith("phase_") else stage
                if str(key) == "error" and item:
                    error_type = _diagnostic_error_type(item)
                    type_counts[error_type] = type_counts.get(error_type, 0) + 1
                    stage_counts[next_stage] = stage_counts.get(next_stage, 0) + 1
                else:
                    collect(item, next_stage)
        elif isinstance(value, list):
            for item in value:
                collect(item, stage)

    collect(diagnostics)
    return type_counts, stage_counts


def build_viewpoint_diagnostics(
    clustered_themes: dict,
    report_viewpoints: list[dict],
    viewpoint_stats_md: str,
    *,
    cluster_diagnostics: dict | None = None,
    cluster_metrics: dict | None = None,
    synthesis_diagnostics: dict | None = None,
) -> dict:
    """Build a per-report, privacy-safe snapshot of the viewpoint pipeline."""
    catalog_entries: list[dict] = []
    question_viewpoint_count = 0
    for scope_key, data in (clustered_themes or {}).items():
        question = _question_label(data)
        denominator = int(_diagnostic_number(data.get("total"), 0))
        for theme in data.get("all_themes") or data.get("themes") or []:
            count = int(_diagnostic_number(theme.get("count"), 0))
            if not count or not denominator:
                continue
            question_viewpoint_count += 1
            catalog_entries.append({
                "id": f"QVIEW:{scope_key}:{theme.get('id', '')}",
                "kind": "question",
                "name": str(theme.get("name") or "").strip(),
                "count": count,
                "denominator": denominator,
                "percentage": _diagnostic_number(theme.get("percentage"), 0),
                "source_questions": [question],
            })

    report_viewpoint_count = 0
    for item in report_viewpoints or []:
        count = int(_diagnostic_number(item.get("count"), 0))
        denominator = int(_diagnostic_number(item.get("denominator"), 0))
        if not count or not denominator:
            continue
        report_viewpoint_count += 1
        catalog_entries.append({
            "id": str(item.get("id") or "").strip(),
            "kind": "report",
            "name": str(item.get("name") or "").strip(),
            "count": count,
            "denominator": denominator,
            "percentage": _diagnostic_number(item.get("percentage"), 0),
            "source_questions": [
                str(question).strip()
                for question in item.get("source_questions") or []
                if str(question).strip()
            ],
        })

    diagnostics = cluster_diagnostics or {}
    failed_scope_count = sum(
        1 for item in diagnostics.values()
        if isinstance(item, dict) and item.get("status") == "failed"
    )
    degraded_scope_count = sum(
        1 for item in diagnostics.values()
        if isinstance(item, dict) and item.get("quality_status") == "degraded"
    )
    error_type_counts, error_stage_counts = _diagnostic_error_counts(diagnostics)
    if failed_scope_count and failed_scope_count == len(diagnostics):
        cluster_status = "failed"
    elif failed_scope_count or degraded_scope_count:
        cluster_status = "degraded"
    elif clustered_themes:
        cluster_status = "completed"
    else:
        cluster_status = "empty"

    safe_metrics = {}
    for key in ("scope_concurrency", "elapsed_seconds"):
        value = (cluster_metrics or {}).get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            safe_metrics[key] = value

    rendered = str(viewpoint_stats_md or "")
    synthesis = synthesis_diagnostics or {}
    safe_synthesis_calls = []
    for call in synthesis.get("calls") or []:
        if not isinstance(call, dict):
            continue
        safe_synthesis_calls.append({
            key: call.get(key)
            for key in (
                "stage",
                "batch_index",
                "input_candidate_count",
                "output_theme_count",
                "excluded_candidate_count",
                "retention_ratio",
                "status",
                "model",
                "repaired",
                "raw_len",
                "error",
                "error_type",
                "finish_reason",
                "duration_seconds",
            )
        })
    safe_synthesis = {
        "contract_version": int(synthesis.get("contract_version") or 0),
        "strategy": str(synthesis.get("strategy") or ""),
        "status": str(synthesis.get("status") or (
            "completed" if report_viewpoints else "not_run"
        )),
        "stop_reason": str(synthesis.get("stop_reason") or ""),
        "stage_budget_seconds": int(synthesis.get("stage_budget_seconds") or 0),
        "elapsed_seconds": _diagnostic_number(synthesis.get("elapsed_seconds"), 0),
        "planned_logical_call_count": int(
            synthesis.get("planned_logical_call_count") or 0
        ),
        "completed_logical_call_count": int(
            synthesis.get("completed_logical_call_count") or 0
        ),
        "input_candidate_count": int(synthesis.get("input_candidate_count") or 0),
        "final_input_candidate_count": int(
            synthesis.get("final_input_candidate_count") or 0
        ),
        "selected_candidate_count": int(
            synthesis.get("selected_candidate_count") or 0
        ),
        "excluded_candidate_count": int(
            synthesis.get("excluded_candidate_count") or 0
        ),
        "reduction_levels": int(synthesis.get("reduction_levels") or 0),
        "partial_failure_count": int(
            synthesis.get("partial_failure_count") or 0
        ),
        "classification_batch_count": int(
            synthesis.get("classification_batch_count") or 0
        ),
        "classification_fallback_count": int(
            synthesis.get("classification_fallback_count") or 0
        ),
        "final_viewpoint_count": int(
            synthesis.get("final_viewpoint_count") or 0
        ),
        "calls": safe_synthesis_calls,
    }
    return {
        "schema_version": 3,
        "cluster": {
            "status": cluster_status,
            "scope_count": len(clustered_themes or {}),
            "failed_scope_count": failed_scope_count,
            "degraded_scope_count": degraded_scope_count,
            "error_type_counts": error_type_counts,
            "error_stage_counts": error_stage_counts,
            "metrics": safe_metrics,
        },
        "catalog": {
            "question_viewpoint_count": question_viewpoint_count,
            "report_viewpoint_count": report_viewpoint_count,
            "entry_count": len(catalog_entries),
            "rendered": bool(rendered.strip()),
            "rendered_char_count": len(rendered),
            "rendered_sha256": (
                hashlib.sha256(rendered.encode("utf-8")).hexdigest()
                if rendered else ""
            ),
            "entries": catalog_entries,
        },
        "synthesis": safe_synthesis,
        "writer_context": {
            "included": False,
        },
        "writer_output": {
            "status": "not_checked",
        },
    }


def finalize_viewpoint_diagnostics(
    diagnostics: dict,
    report_md: str,
    *,
    writer_context_included: bool,
) -> dict:
    """Add Writer propagation/compliance facts without changing report output."""
    result = deepcopy(diagnostics)
    catalog_count = int(
        _diagnostic_number(result.get("catalog", {}).get("entry_count"), 0)
    )
    viewpoint_block_count = len(_VIEWPOINT_BLOCK_RE.findall(report_md or ""))
    mention_block_count = len(_MENTION_BLOCK_RE.findall(report_md or ""))
    inference_block_count = len(_INFERENCE_BLOCK_RE.findall(report_md or ""))
    missing_mention_count = max(0, viewpoint_block_count - mention_block_count)

    if not catalog_count and viewpoint_block_count:
        status = "catalog_unavailable"
    elif catalog_count and not writer_context_included:
        status = "context_missing"
    elif catalog_count and not viewpoint_block_count:
        status = "writer_no_viewpoints"
    elif missing_mention_count:
        status = "writer_omission"
    elif not catalog_count and not viewpoint_block_count:
        status = "not_applicable"
    else:
        status = "complete"

    result["writer_context"] = {
        "included": bool(writer_context_included),
    }
    result["writer_output"] = {
        "status": status,
        "viewpoint_block_count": viewpoint_block_count,
        "mention_block_count": mention_block_count,
        "missing_mention_count": missing_mention_count,
        "analysis_inference_block_count": inference_block_count,
        "vague_reference_count": sum(
            str(report_md or "").count(term) for term in _VAGUE_VIEWPOINT_TERMS
        ),
    }
    return result
