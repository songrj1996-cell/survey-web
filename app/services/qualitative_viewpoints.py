"""定性问卷观点统计：把 AI 语义分类转换为按玩家去重的确定性人数。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import re
import time

from app.services import report_engine


_VIEWPOINT_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*观点：")
_MENTION_BLOCK_RE = re.compile(r"(?m)^[ \t]*(?:-[ \t]+)?\*\*提及情况：")
_INFERENCE_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*分析推断：")
_VAGUE_VIEWPOINT_TERMS = ("多数玩家", "多位玩家", "部分玩家", "少数玩家")
_CROSS_QUESTION_SYNTHESIS_STRATEGY = "single_pass_selective_grouping"
_CROSS_QUESTION_STAGE_MAX_SECONDS = 300


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
            quotes = list(theme.get("quotes") or theme.get("source_quotes") or [])[:3]
            if not quotes or not theme.get("count"):
                continue
            candidates.append({
                "name": str(theme.get("name") or "").strip(),
                "source_question_id": str(scope_key),
                "source_question": question,
                "description": str(theme.get("description") or "").strip(),
                "positive_summary": theme.get("positive_summary") or None,
                "negative_summary": theme.get("negative_summary") or None,
                "representative_quotes": quotes,
                "respondent_keys": sorted({
                    str(key).strip()
                    for key in theme.get("respondent_keys") or []
                    if str(key).strip()
                }),
            })
    return candidates


def _merge_call_diagnostic(
    stage: str,
    batch_index: int,
    candidates: list[dict],
    call_result: dict,
) -> dict:
    data = call_result.get("data") if isinstance(call_result, dict) else None
    themes = data.get("themes") if isinstance(data, dict) else None
    error = str(call_result.get("error") or "")[:300]
    return {
        "stage": stage,
        "batch_index": batch_index,
        "input_candidate_count": len(candidates),
        "output_theme_count": len(themes) if isinstance(themes, list) else 0,
        "status": "completed" if isinstance(data, dict) else "failed",
        "model": str(call_result.get("model") or ""),
        "repaired": bool(call_result.get("repaired")),
        "raw_len": int(call_result.get("raw_len") or 0),
        "error": error,
        "error_type": _diagnostic_error_type(error) if error else "",
        "finish_reason": "length" if "finish_reason=length" in error else "",
        "duration_seconds": float(call_result.get("duration_seconds") or 0),
    }


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


def _report_viewpoints_from_candidate_groups(
    themes: list[dict],
    candidates: list[dict],
    evidence: list[dict],
) -> list[dict]:
    """Hydrate cross-question counts and evidence from classified question themes."""
    candidate_lookup = {
        f"c{index:04d}": candidate
        for index, candidate in enumerate(candidates, 1)
    }
    respondents_by_scope: dict[str, set[str]] = {}
    for item in evidence:
        respondents_by_scope.setdefault(item["scope_key"], set()).add(
            item["respondent_key"]
        )

    result = []
    for theme in themes:
        source_candidates = [
            candidate_lookup[candidate_id]
            for candidate_id in theme.get("source_candidate_ids") or []
            if candidate_id in candidate_lookup
        ]
        members = {
            respondent_key
            for candidate in source_candidates
            for respondent_key in candidate.get("respondent_keys") or []
        }
        source_scope_keys = {
            candidate["source_question_id"] for candidate in source_candidates
        }
        denominator_members = set().union(*(
            respondents_by_scope.get(scope_key, set())
            for scope_key in source_scope_keys
        )) if source_scope_keys else set()
        if not members or not denominator_members:
            continue
        quotes = list(dict.fromkeys(
            quote
            for candidate in source_candidates
            for quote in candidate.get("representative_quotes") or []
            if isinstance(quote, str) and quote.strip()
        ))[:6]
        count = len(members)
        denominator = len(denominator_members)
        result.append({
            "id": f"RVIEW:{theme['id']}",
            "name": theme["name"],
            "description": theme.get("description", ""),
            "count": count,
            "denominator": denominator,
            "percentage": round(count / denominator * 100, 1),
            "source_questions": sorted({
                candidate["source_question"] for candidate in source_candidates
            }),
            "source_scope_keys": sorted(source_scope_keys),
            "quotes": quotes,
        })
    result.sort(key=lambda item: item["count"], reverse=True)
    return result


async def build_report_viewpoint_stats(
    clustered_themes: dict,
    open_text: dict,
    plan: dict,
    headers: list[str],
    *,
    on_attempt_event=None,
):
    """筛选跨题共同观点并复用逐题分类人数；yield progress/diagnostics/result。"""
    synthesis_started = time.monotonic()
    candidates = _cross_question_candidates(clustered_themes)
    evidence = _flatten_evidence(open_text, plan, headers)
    if len(clustered_themes) < 2 or not candidates or not evidence:
        yield ("result", [])
        return

    yield (
        "analysis_progress",
        {
            "phase": "synthesis",
            "phase_index": 2,
            "phase_total": 4,
            "status": "active",
            "step": "merging",
            "message": "正在合并不同题目中含义相近的玩家观点",
            "impact": "none",
        },
    )
    organization = _report_organization(plan)
    repair_events: asyncio.Queue = asyncio.Queue()
    stage_budget_seconds = min(
        _CROSS_QUESTION_STAGE_MAX_SECONDS,
        report_engine.LLM_QUALITATIVE_CALL_TIMEOUT_SECONDS,
    )

    async def _merge():
        call_started = time.monotonic()
        try:
            return await asyncio.wait_for(
                report_engine._direct_json_call(
                    report_engine._get_cross_question_merge_system_prompt(),
                    report_engine._build_cross_question_merge_query(
                        organization,
                        candidates,
                    ),
                    models=(
                        report_engine.LLM_THEME_MERGE_MODEL,
                        *report_engine.LLM_THEME_MERGE_FALLBACK_MODELS,
                    ),
                    max_tokens=report_engine.LLM_THEME_MERGE_MAX_TOKENS,
                    reasoning_effort=report_engine.LLM_THEME_MERGE_REASONING or None,
                    validator=lambda data: report_engine._validate_cross_question_themes(
                        data,
                        candidates,
                    ),
                    on_repair=lambda error: repair_events.put_nowait({
                        "stage": "selective_grouping",
                        "batch_index": 1,
                        "error": str(error)[:300],
                    }),
                    on_attempt_event=on_attempt_event,
                ),
                timeout=stage_budget_seconds,
            )
        except asyncio.TimeoutError:
            return {
                "data": None,
                "model": "",
                "raw_len": 0,
                "repaired": False,
                "error": "cross_question_synthesis_stage_timeout",
                "duration_seconds": round(time.monotonic() - call_started, 3),
            }

    merge_result = None
    async for event_type, payload in report_engine._run_bounded_calls(
        [_merge], 1, repair_events
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
                    "step": "merging",
                    "message": "跨题共同观点未通过校验，正在预算内自动修正",
                    "impact": "各题主题和原文仍完整保留",
                    **payload,
                },
            )
        else:
            _batch_index, merge_result = payload
    merge_result = merge_result or {}
    merge_calls = [
        _merge_call_diagnostic("selective_grouping", 1, candidates, merge_result)
    ]
    merged = merge_result.get("data") if isinstance(merge_result, dict) else None
    themes = merged.get("themes", []) if isinstance(merged, dict) else []
    result = _report_viewpoints_from_candidate_groups(themes, candidates, evidence)
    selected_candidate_ids = {
        candidate_id
        for theme in themes
        for candidate_id in theme.get("source_candidate_ids") or []
    }
    synthesis_status = (
        "completed"
        if isinstance(merged, dict) and (not themes or len(result) == len(themes))
        else "degraded"
        if isinstance(merged, dict) and result
        else "failed"
    )
    synthesis_diagnostics = {
        "status": synthesis_status,
        "strategy": _CROSS_QUESTION_SYNTHESIS_STRATEGY,
        "stage_budget_seconds": stage_budget_seconds,
        "input_candidate_count": len(candidates),
        "final_input_candidate_count": len(candidates),
        "selected_candidate_count": len(selected_candidate_ids),
        "excluded_candidate_count": len(candidates) - len(selected_candidate_ids),
        "reduction_levels": 0,
        "partial_failure_count": 0 if synthesis_status == "completed" else 1,
        "calls": merge_calls,
    }
    yield ("diagnostics", synthesis_diagnostics)
    if not isinstance(merged, dict):
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "degraded",
                "step": "completed",
                "message": "跨题观点归纳未完成，继续使用各题分析结果撰写报告",
                "impact": (
                    "各题主题和原文均保留，但跨题共同观点及其去重人数可能缺失"
                ),
                "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
            },
        )
        yield ("result", [])
        return
    if not themes:
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "completed",
                "step": "completed",
                "viewpoint_count": 0,
                "message": "跨题归纳完成，未发现由不同题目共同支持的同一玩家观点",
                "impact": "各题主题和原文完整保留",
                "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
            },
        )
        yield ("result", [])
        return
    if not result:
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "degraded",
                "step": "completed",
                "message": "跨题观点缺少可复用的逐题人数，继续使用各题分析结果撰写报告",
                "impact": "各题主题和原文均保留，但本次不生成跨题共同观点",
                "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
            },
        )
        yield ("result", [])
        return
    if merge_result.get("repaired"):
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "recovered",
                "step": "merging",
                "message": "跨题归纳自动修正成功，正在汇总逐题分类人数",
                "impact": "none",
            },
        )
    yield (
        "analysis_progress",
        {
            "phase": "synthesis",
            "phase_index": 2,
            "phase_total": 4,
            "status": "degraded" if synthesis_status == "degraded" else "completed",
            "step": "completed",
            "viewpoint_count": len(result),
            "message": f"跨题归纳完成，共形成 {len(result)} 个跨题观点",
            "impact": (
                "部分观点缺少可复用的逐题人数，已跳过；各题结果和原文不受影响"
                if synthesis_status == "degraded" else "none"
            ),
            "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
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
        "status": str(synthesis.get("status") or (
            "completed" if report_viewpoints else "not_run"
        )),
        "strategy": str(synthesis.get("strategy") or ""),
        "stage_budget_seconds": int(
            synthesis.get("stage_budget_seconds") or 0
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
        "calls": safe_synthesis_calls,
    }
    return {
        "schema_version": 2,
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
