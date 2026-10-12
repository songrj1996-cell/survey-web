"""Pure-code assembly for fixed Insight v3 sections and verified evidence."""

from __future__ import annotations

from app.services.insight_report_v3.research_context import expand_stat_references

import json
import re

from app.services.insight_report_v3.frequency import (
    CROSS_QUESTION_FREQUENCY_LABEL,
)


_CONTEXT_LABELS = (
    ("problem", "研究问题"),
    ("background", "业务背景"),
    ("target_users", "目标用户"),
    ("key_concerns", "重点关注"),
    ("report_usage", "报告用途"),
    ("analysis_approach", "分析方式"),
)
_METADATA_RE = re.compile(
    r"<metadata>总样本:\s*(\d+),\s*有效样本:\s*(\d+)(?:,\s*全空被排除:\s*(\d+)\s*条)?</metadata>"
)
_NUMBER_WORDS = (
    "零", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二",
    "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
)
_VALIDATION_SEMANTICS_RE = re.compile(
    r"(?:验证|核验|校验|检验|评估|衡量|复核|核对|确认|对比|比较|监测|监控|观察|"
    r"跟踪|追踪|回归测试|A\s*/\s*B\s*测试)",
    re.IGNORECASE,
)


def _clean(value) -> str:
    return str(value or "").strip()


def _section_number(index: int) -> str:
    if 0 <= index < len(_NUMBER_WORDS):
        return _NUMBER_WORDS[index]
    return str(index)


def render_research_background(qualitative_context: dict | None) -> str:
    context = qualitative_context if isinstance(qualitative_context, dict) else {}
    lines = ["## 一、调研背景"]
    populated = [
        (label, _clean(context.get(field)))
        for field, label in _CONTEXT_LABELS
        if _clean(context.get(field))
    ]
    if not populated:
        lines.extend([
            "",
            "本次报告基于已确认的问卷范围与玩家反馈生成；未提供额外业务背景，",
            "因此只呈现材料能够支持的判断，不补充研究材料之外的目标、假设或因果解释。",
        ])
        return "\n".join(lines)
    lines.append("")
    lines.extend(f"- **{label}：** {value}" for label, value in populated)
    return "\n".join(lines)


def _focus_text(analysis_focus: dict | None) -> str:
    if not isinstance(analysis_focus, dict):
        return ""
    return json.dumps(analysis_focus, ensure_ascii=False, sort_keys=True).casefold()


def _title_relevance(title: str, focus_text: str) -> int:
    normalized = _clean(title).casefold()
    if not normalized or not focus_text:
        return 0
    score = 0
    if normalized in focus_text:
        score += 100
    tokens = set(re.findall(r"[\u3400-\u9fff]{2,}|[A-Za-z0-9_]{3,}", normalized))
    score += sum(len(token) for token in tokens if token in focus_text)
    return score


def _sample_distribution_key(block: dict) -> str | None:
    """Compare only plain single-dimensional distributions, never titles alone."""
    lines = [line.strip() for line in _clean(block.get("table_markdown")).splitlines() if line.strip()]
    if len(lines) < 3 or any(not line.startswith("|") or not line.endswith("|") for line in lines):
        return None
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    if rows[0] not in (["取值", "频数", "占比"], ["选项", "频数", "占比"]):
        return None
    if len(rows[1]) != 3 or any(not re.fullmatch(r":?-{3,}:?", cell) for cell in rows[1]):
        return None
    if any(len(row) != 3 for row in rows[2:]):
        return None
    metadata = {key: value for key, value in block.items() if key not in {
        "part", "chart", "table_markdown", "id", "table_key", "headers", "rows",
    }}
    return json.dumps([metadata, rows[2:]], ensure_ascii=False, sort_keys=True)


def select_sample_tables(
    structured_tables: list[dict],
    *,
    analysis_focus: dict | None,
    limit: int = 3,
) -> list[dict]:
    """Choose at most three one-dimensional/focus-relevant tables."""
    maximum = max(0, min(3, int(limit)))
    if not maximum:
        return []
    valid = [
        block for block in structured_tables or []
        if isinstance(block, dict) and _clean(block.get("table_markdown"))
    ]
    profile_tables = [block for block in valid if not _clean(block.get("part"))]
    focus_text = _focus_text(analysis_focus)
    focus_ranked = sorted(
        (
            (_title_relevance(block.get("title", ""), focus_text), index, block)
            for index, block in enumerate(valid)
            if block not in profile_tables
        ),
        key=lambda item: (-item[0], item[1]),
    )

    selected: list[dict] = []
    profile_keys = {
        key for block in profile_tables if (key := _sample_distribution_key(block)) is not None
    }
    seen_profile_keys: set[str] = set()

    def append_unique(block: dict) -> None:
        key = _sample_distribution_key(block)
        if block in selected or (key in profile_keys and key in seen_profile_keys):
            return
        selected.append(block)
        if key in profile_keys:
            seen_profile_keys.add(key)

    # Single-dimensional profile distributions are the primary sample overview.
    for block in profile_tables:
        if len(selected) >= maximum:
            break
        append_unique(block)
    # Only objective tables with an actual analysis-focus match may join them.
    for score, _index, block in focus_ranked:
        if len(selected) >= maximum or score <= 0:
            break
        append_unique(block)
    return selected


def render_sample_overview(
    stats_md: str,
    structured_tables: list[dict],
    *,
    analysis_focus: dict | None,
    interpretation: dict | None = None,
) -> tuple[str, list[dict]]:
    selected = select_sample_tables(
        structured_tables,
        analysis_focus=analysis_focus,
        limit=3,
    )
    match = _METADATA_RE.search(_clean(stats_md))
    if match:
        total, valid, blank = (int(value or 0) for value in match.groups())
        opening = (
            f"本次数据共记录 {total} 份回答，其中 {valid} 份进入有效样本口径。"
            + (f"另有 {blank} 份全空记录已由统计程序排除。" if blank else "")
        )
    else:
        opening = "本章沿用已确认统计中的样本口径；原始统计未提供可单独解析的总样本元数据。"

    interpretation = interpretation or {}
    explanation = expand_stat_references(
        _clean(interpretation.get("body")), interpretation.get("objective_facts") or {},
    )
    lines = ["## 二、样本概况", "", opening]
    if explanation:
        lines.extend(["", explanation])
    else:
        lines.extend(["", "以下分布描述本次受访样本，不代表总体玩家结构。"])
    for block in selected:
        lines.extend([
            "",
            f"**{_clean(block.get('title')) or '样本结构'}**",
            "",
            _clean(block.get("table_markdown")),
        ])
    return "\n".join(lines).strip(), selected


def finding_frequency_label(findings: list[dict]) -> str:
    valid = [finding for finding in findings if isinstance(finding, dict)]
    if not valid:
        return ""
    question_keys = {
        _clean(finding.get("question_key"))
        for finding in valid
        if _clean(finding.get("question_key"))
    }
    if len(question_keys) > 1 or any(
        finding.get("frequency_tier") == CROSS_QUESTION_FREQUENCY_LABEL
        for finding in valid
    ):
        return CROSS_QUESTION_FREQUENCY_LABEL
    tiers = {
        _clean(finding.get("frequency_tier"))
        for finding in valid
        if _clean(finding.get("frequency_tier"))
    }
    return next(iter(tiers)) if len(tiers) == 1 else ""


def _evidence_quote_lines(translation: str) -> list[str]:
    lines = translation.splitlines() or [translation]
    return [
        f"> {lines[0]}",
        *(f"> {line}" if line else ">" for line in lines[1:]),
    ]


def build_evidence_index(
    *,
    core_blocks: list[dict],
    chapters: list[dict],
) -> list[dict]:
    """Deduplicate full translations while retaining every body reference."""
    ordered_sections = [
        ("三、核心发现", core_blocks or []),
        *(
            (
                f"{_section_number(index)}、{_clean(chapter.get('title'))}",
                chapter.get("blocks") or [],
            )
            for index, chapter in enumerate(chapters or [], 4)
        ),
    ]
    by_response: dict[str, dict] = {}
    ordered: list[dict] = []
    for section_label, blocks in ordered_sections:
        for block in blocks:
            heading = _clean(block.get("heading")) or "关键发现"
            reference = f"{section_label} / {heading}"
            for evidence in block.get("evidence") or []:
                response_id = _clean(evidence.get("response_id"))
                if not response_id:
                    continue
                entry = by_response.get(response_id)
                if entry is None:
                    entry = {
                        "evidence_id": f"E-{len(ordered) + 1:03d}",
                        "response_id": response_id,
                        "translation_zh": _clean(evidence.get("translation_zh")),
                        "translation_status": evidence.get("translation_status") or "failed",
                        "references": [],
                    }
                    by_response[response_id] = entry
                    ordered.append(entry)
                elif not entry["translation_zh"] and _clean(evidence.get("translation_zh")):
                    entry["translation_zh"] = _clean(evidence.get("translation_zh"))
                    entry["translation_status"] = evidence.get("translation_status") or "failed"
                if reference not in entry["references"]:
                    entry["references"].append(reference)
    return ordered


def render_written_section(
    *,
    section_title: str,
    blocks: list[dict],
    findings_by_id: dict[str, dict],
    section_index: int,
    evidence_ids: dict[str, str],
) -> str:
    lines = [f"## {_section_number(section_index)}、{_clean(section_title)}"]
    for block in blocks or []:
        finding_ids = list(dict.fromkeys(block.get("finding_ids") or []))
        findings = [findings_by_id[item] for item in finding_ids if item in findings_by_id]
        heading = expand_stat_references(_clean(block.get("heading")), block.get("objective_facts") or {}) or "关键发现"
        body = expand_stat_references(_clean(block.get("body")), block.get("objective_facts") or {})
        lines.extend(["", f"**{heading}**"])
        if body:
            lines.extend(["", body])
    return "\n".join(lines).strip()


def render_actions_section(
    *,
    actions: list[dict],
    chapter_note: str,
    section_index: int,
) -> str:
    lines = [f"## {_section_number(section_index)}、行动建议"]
    if not actions:
        lines.extend(["", _clean(chapter_note) or "现有证据不足以形成可绑定、可验证的行动建议。"])
        return "\n".join(lines).strip()

    for index, action in enumerate(actions, 1):
        title = _clean(action.get("title")) or "待验证的产品方向"
        lines.extend(["", f"**建议{_section_number(index)}：{title}**"])
        target_group = action.get("target_group")
        if isinstance(target_group, dict):
            profile_name = _clean(target_group.get("profile_name"))
            profile_value = _clean(target_group.get("profile_value"))
            if profile_value:
                group_label = f"“{profile_name}”为“{profile_value}”的群体" if profile_name else f"{profile_value}群体"
                lines.extend(["", f"这是一项面向{group_label}的专项建议，依据该人群的定性材料提出。"])
            elif profile_name:
                lines.extend(["", f"这是一项面向{profile_name}相关群体的专项建议。"])
        problem = _clean(action.get("problem"))
        objective = _clean(action.get("objective"))
        if problem and objective:
            lines.extend(["", f"{problem.rstrip('。.!！?？；;')}。建议目标是{objective.rstrip('。.!！?？；;')}。"])
        elif problem or objective:
            lines.extend(["", problem or objective])

        directions = [
            _clean(item) for item in action.get("design_directions") or [] if _clean(item)
        ]
        if directions:
            lines.extend(["", "可考虑的设计方向："])
            lines.extend(f"{direction_index}. {direction}" for direction_index, direction in enumerate(directions, 1))
        validation_plan = _clean(action.get("validation_plan"))
        if validation_plan:
            validation_detail = re.sub(r"^(?:可)?通过\s*", "", validation_plan).rstrip("。.!！?？；;")
            if _VALIDATION_SEMANTICS_RE.search(validation_detail):
                validation_sentence = f"实施前后可通过{validation_detail}。"
            else:
                validation_sentence = f"实施前后可通过{validation_detail}来验证这一方向是否有效。"
            lines.extend(["", validation_sentence])
        evidence_gap = _clean(action.get("evidence_gap"))
        required_data = _clean(action.get("required_data"))
        if evidence_gap and required_data:
            gap_detail = evidence_gap.rstrip("。.!！?？；;")
            required_detail = re.sub(r"^需(?:要)?补充\s*", "", required_data).rstrip("。.!！?？；;")
            lines.extend(["", f"当前证据边界是{gap_detail}；在进一步决策前仍需补充{required_detail}。"])
    return "\n".join(lines).strip()


def render_evidence_appendix(evidence_index: list[dict]) -> str:
    if not evidence_index:
        return ""
    lines = [
        "## 附录 A：完整证据索引",
        "",
        "本附录保存完整中文译文，并按章节与段落列出全部引用位置。原始反馈不等于已核实事实。",
    ]
    for entry in evidence_index:
        lines.extend([
            "",
            f"### {entry['evidence_id']}",
            "",
            f"- **引用位置：** {'；'.join(entry.get('references') or [])}",
            f"- **来源编号：** {_clean(entry.get('response_id'))}",
            "",
            "**完整中文译文：**",
            "",
        ])
        translation = _clean(entry.get("translation_zh"))
        if translation:
            lines.extend(_evidence_quote_lines(translation))
        else:
            lines.append("> 中文翻译暂不可用；来源编号已保留供人工核对。")
    return "\n".join(lines).strip()


def render_unresolved_audit_notice(unresolved: list[dict] | None) -> str:
    """Render every unresolved audit item without presenting it as a confirmed error."""
    items = [item for item in (unresolved or []) if isinstance(item, dict)]
    if not items:
        return ""
    lines = [
        "> **语义审校提示：** 本报告有 "
        f"{len(items)} 项未能由自动审校完整确认，报告已保留并需要人工复核。"
        "下列项目不代表一定错误。",
    ]
    for item in items:
        section = _clean(item.get("section")) or "未定位章节"
        heading = _clean(item.get("heading")) or _clean(item.get("target")) or "未命名内容"
        target = _clean(item.get("target")) or "待审校内容"
        problem = _clean(item.get("problem")) or "未完成证据审校"
        lines.append(f"> - **{section} / {heading}（{target}）：** {problem}")
    return "\n".join(lines)


def assemble_report(
    *,
    report_title: str,
    background_md: str,
    sample_md: str,
    core_blocks: list[dict],
    chapters: list[dict],
    findings_by_id: dict[str, dict],
    actions: list[dict] | None = None,
    action_note: str = "",
    evidence_index: list[dict] | None = None,
    audit_unresolved: list[dict] | None = None,
    research_gaps: list[dict] | None = None,
) -> str:
    title = _clean(report_title) or "观点洞察报告"
    index = evidence_index if evidence_index is not None else build_evidence_index(
        core_blocks=core_blocks,
        chapters=chapters,
    )
    evidence_ids = {
        _clean(item.get("response_id")): _clean(item.get("evidence_id"))
        for item in index
        if _clean(item.get("response_id")) and _clean(item.get("evidence_id"))
    }
    core = render_written_section(
        section_title="核心发现",
        blocks=core_blocks,
        findings_by_id=findings_by_id,
        section_index=3,
        evidence_ids=evidence_ids,
    )
    rendered = [
        f"# {title}",
        render_unresolved_audit_notice(audit_unresolved),
        "\n".join(
            "> **研究任务缺口：** " + _clean(item.get("expected_deliverable") or item.get("title") or "已确认研究要求")
            + "——" + _clean(item.get("gap") or item.get("reason") or "尚未得到可靠覆盖")
            for item in research_gaps or []
        ),
        _clean(background_md),
        _clean(sample_md),
        core,
    ]
    for offset, chapter in enumerate(chapters, 4):
        rendered.append(render_written_section(
            section_title=chapter.get("title") or f"分析主题 {offset - 3}",
            blocks=chapter.get("blocks") or [],
            findings_by_id=findings_by_id,
            section_index=offset,
            evidence_ids=evidence_ids,
        ))
    rendered.append(render_actions_section(
        actions=actions or [],
        chapter_note=action_note,
        section_index=4 + len(chapters),
    ))
    rendered.append(render_evidence_appendix(index))
    return "\n\n".join(section for section in rendered if section).strip()
