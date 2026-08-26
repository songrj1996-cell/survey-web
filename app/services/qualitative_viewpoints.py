"""定性问卷观点统计：把 AI 语义分类转换为按玩家去重的确定性人数。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import re
import time

from app.services import report_engine


_VIEWPOINT_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*观点：")
_MENTION_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*提及情况：")
_INFERENCE_BLOCK_RE = re.compile(r"(?m)^[ \t]*\*\*分析推断：")
_VAGUE_VIEWPOINT_TERMS = ("多数玩家", "多位玩家", "部分玩家", "少数玩家")


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
    for data in clustered_themes.values():
        question = _question_label(data)
        for theme in data.get("all_themes") or data.get("themes") or []:
            quotes = list(theme.get("source_quotes") or theme.get("quotes") or [])[:3]
            if not quotes or not theme.get("count"):
                continue
            candidates.append({
                "name": str(theme.get("name") or "").strip(),
                "description": (
                    f"来源问题：{question}。{str(theme.get('description') or '').strip()}"
                ),
                "positive_summary": theme.get("positive_summary") or None,
                "negative_summary": theme.get("negative_summary") or None,
                "representative_quotes": quotes,
            })
    return candidates


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
):
    """跨题合并观点并回跑全部原文；yield analysis_progress/heartbeat/result。"""
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

    async def _merge():
        return await report_engine._direct_json_call(
            report_engine._get_theme_merge_system_prompt()
            + (
                "\n\n跨题观点额外规则：最终主题必须是玩家能够在一条回答中直接表达的具体观点。"
                "可以合并不同题目里意思相同的玩家说法，但禁止创造‘A影响B’‘A导致B’‘A与B有关’"
                "等需要比较多题、客观统计或多类证据才能得出的关系、标准、框架或产品判断；"
                "这类内容属于分析推断，不进入玩家观点目录。"
            ),
            report_engine._build_theme_merge_query(
                f"跨题报告观点；报告组织方式：{organization}",
                candidates,
                len(evidence),
            ),
            models=(
                report_engine.LLM_THEME_MERGE_MODEL,
                *report_engine.LLM_THEME_MERGE_FALLBACK_MODELS,
            ),
            max_tokens=report_engine.LLM_THEME_MERGE_MAX_TOKENS,
            reasoning_effort=report_engine.LLM_THEME_MERGE_REASONING or None,
            validator=lambda data: report_engine._validate_merged_themes(data, candidates),
            on_repair=lambda error: repair_events.put_nowait({"error": error}),
        )

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
                    "retry_index": 1,
                    "retry_total": 1,
                    "message": "跨题归纳未通过校验，正在自动修正并重新调用（1/1）",
                    "impact": "各题主题和原文仍完整保留",
                },
            )
        else:
            _batch_index, merge_result = payload
    merge_result = merge_result or {}
    merged = merge_result.get("data") if isinstance(merge_result, dict) else None
    themes = merged.get("themes", []) if isinstance(merged, dict) else []
    if not themes:
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
    if merge_result.get("repaired"):
        yield (
            "analysis_progress",
            {
                "phase": "synthesis",
                "phase_index": 2,
                "phase_total": 4,
                "status": "recovered",
                "step": "merging",
                "message": "跨题归纳自动修正成功，正在回查全部原文",
                "impact": "none",
            },
        )

    batches = [
        evidence[index:index + report_engine.BATCH_SIZE]
        for index in range(0, len(evidence), report_engine.BATCH_SIZE)
    ]
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
                f"跨题报告观点；报告组织方式：{organization}", themes, batch
            )
        factories.append(_classify)

    classified: dict[int, dict] = {}
    async for event_type, payload in report_engine._run_bounded_calls(
        factories, report_engine.LLM_CLASSIFY_CONCURRENCY
    ):
        if event_type == "heartbeat":
            yield ("heartbeat", "")
        else:
            batch_index, result = payload
            classified[batch_index] = result

    members = {theme["id"]: set() for theme in themes}
    sources = {theme["id"]: set() for theme in themes}
    quotes = {theme["id"]: [] for theme in themes}
    respondents_by_scope: dict[str, set[str]] = {}
    for item in evidence:
        respondents_by_scope.setdefault(item["scope_key"], set()).add(
            item["respondent_key"]
        )

    for batch_index, result in sorted(classified.items()):
        batch = batches[batch_index]
        for classified_item in result.get("classifications") or []:
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
        if not count or not denominator:
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
            "quotes": quotes[theme_id],
        })
    result.sort(key=lambda item: item["count"], reverse=True)
    fallback_count = sum(
        item.get("fallback_count", 0) for item in classified.values()
    )
    yield (
        "analysis_progress",
        {
            "phase": "synthesis",
            "phase_index": 2,
            "phase_total": 4,
            "status": "degraded" if fallback_count else "completed",
            "step": "completed",
            "viewpoint_count": len(result),
            "message": f"跨题归纳完成，共形成 {len(result)} 个跨题观点",
            "impact": (
                f"有 {fallback_count} 条回答未能归入跨题观点；单题结果和原文不受影响"
                if fallback_count else "none"
            ),
            "elapsed_seconds": round(time.monotonic() - synthesis_started, 3),
        },
    )
    yield ("result", result)


def render_viewpoint_stats(clustered_themes: dict, report_viewpoints: list[dict]) -> str:
    """渲染给 Writer 的只读观点统计目录。"""
    lines = [
        "<subjective_viewpoint_stats>",
        "口径：人数均按玩家去重；同一玩家可提及多个观点，所以占比之和可能超过100%。",
        "只有本目录中的观点才可写“X名玩家提及”；目录外的综合判断必须标为“分析推断”。",
        "",
        "## 单题观点",
    ]
    for scope_key, data in clustered_themes.items():
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

    if report_viewpoints:
        lines.extend(["", "## 跨题重组观点"])
        for item in report_viewpoints:
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
    return {
        "schema_version": 1,
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
