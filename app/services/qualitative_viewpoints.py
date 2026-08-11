"""定性问卷观点统计：把 AI 语义分类转换为按玩家去重的确定性人数。"""

from __future__ import annotations

from app.services import report_engine


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
    """跨题合并观点并回跑全部原文；yield progress/heartbeat/result。"""
    candidates = _cross_question_candidates(clustered_themes)
    evidence = _flatten_evidence(open_text, plan, headers)
    if len(clustered_themes) < 2 or not candidates or not evidence:
        yield ("result", [])
        return

    yield ("progress", "正在按报告分析结构合并跨题观点")
    organization = _report_organization(plan)
    merge_result = await report_engine._direct_json_call(
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
    )
    merged = merge_result.get("data") if isinstance(merge_result, dict) else None
    themes = merged.get("themes", []) if isinstance(merged, dict) else []
    if not themes:
        yield ("result", [])
        return

    batches = [
        evidence[index:index + report_engine.BATCH_SIZE]
        for index in range(0, len(evidence), report_engine.BATCH_SIZE)
    ]
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
