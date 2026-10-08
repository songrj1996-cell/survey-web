"""Pure-code assembly for fixed Insight v3 sections and verified evidence."""

from __future__ import annotations

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
    # Single-dimensional profile distributions are the primary sample overview.
    for block in profile_tables:
        if len(selected) >= maximum:
            break
        selected.append(block)
    # Only objective tables with an actual analysis-focus match may join them.
    for score, _index, block in focus_ranked:
        if len(selected) >= maximum or score <= 0:
            break
        if block not in selected:
            selected.append(block)
    return selected


def render_sample_overview(
    stats_md: str,
    structured_tables: list[dict],
    *,
    analysis_focus: dict | None,
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

    explanation = (
        "样本概况只用于说明本次定性材料来自哪些玩家，不把样本结构外推为总体人群结构。"
        "所有数字和表格均直接取自确定性统计结果，未交给模型重算、改写或补齐。"
        "为避免概况章节挤占洞察正文，本章仅保留单维画像分布，以及与已确认分析重点直接相关的少量表格；"
        "逐题客观题明细统一放在报告末尾的完整统计附录。"
        "样本分布只帮助理解后续观点的材料背景；样本较少的人群观察应视为探索性线索，"
        "不得据此比较群体高低、推断因果或形成总体结论。"
    )
    if selected:
        titles = "、".join(f"“{_clean(block.get('title'))}”" for block in selected)
        selection_note = (
            f"本章展示 {len(selected)} 张表，覆盖{titles}。阅读这些表时应关注样本覆盖与构成，"
            "不能据此推断开放题观点在总体玩家中的精确比例，也不能把代表性引用数量当作观点人数。"
        )
    else:
        selection_note = (
            "当前统计中没有符合本章筛选规则的单维画像或分析重点相关表格，因此不为凑数量复制其它统计表。"
            "完整逐题数字仍在附录保留。"
        )

    lines = ["## 二、样本概况", "", opening, "", explanation, "", selection_note]
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
        f"> 代表反馈：{lines[0]}",
        *(f"> {line}" if line else ">" for line in lines[1:]),
    ]


def render_written_section(
    *,
    section_title: str,
    blocks: list[dict],
    findings_by_id: dict[str, dict],
    section_index: int,
) -> str:
    lines = [f"## {_section_number(section_index)}、{_clean(section_title)}"]
    for block in blocks or []:
        finding_ids = list(dict.fromkeys(block.get("finding_ids") or []))
        findings = [findings_by_id[item] for item in finding_ids if item in findings_by_id]
        heading = _clean(block.get("heading")) or "关键发现"
        body = _clean(block.get("body"))
        frequency = finding_frequency_label(findings)
        lines.extend(["", f"**{heading}**"])
        if frequency:
            lines.extend(["", f"**提及情况：** {frequency}。"])
        if body:
            lines.extend(["", body])
        for evidence in block.get("evidence") or []:
            translation = _clean(evidence.get("translation_zh"))
            if translation:
                lines.append("")
                lines.extend(_evidence_quote_lines(translation))
            else:
                lines.extend(["", "> 代表反馈的中文翻译暂不可用；来源编号已保留供人工核对。"])
    return "\n".join(lines).strip()


def assemble_report(
    *,
    report_title: str,
    background_md: str,
    sample_md: str,
    core_blocks: list[dict],
    chapters: list[dict],
    findings_by_id: dict[str, dict],
) -> str:
    title = _clean(report_title) or "观点洞察报告"
    core = render_written_section(
        section_title="核心发现",
        blocks=core_blocks,
        findings_by_id=findings_by_id,
        section_index=3,
    )
    rendered = [f"# {title}", _clean(background_md), _clean(sample_md), core]
    for offset, chapter in enumerate(chapters, 4):
        rendered.append(render_written_section(
            section_title=chapter.get("title") or f"分析主题 {offset - 3}",
            blocks=chapter.get("blocks") or [],
            findings_by_id=findings_by_id,
            section_index=offset,
        ))
    return "\n\n".join(section for section in rendered if section).strip()
