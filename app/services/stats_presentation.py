"""把确定性统计组织为报告附录与网页图表标记。"""

from __future__ import annotations

import json
import re


CHART_FENCE = "stats-chart"


_PART_HEADING_RE = re.compile(r"^##\s+(Part\s+\d+\b.*)$")
_QUESTION_HEADING_RE = re.compile(r"^###\s+(.+)$")
_PERCENT_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_at(lines: list[str], index: int) -> tuple[dict | None, int]:
    if (
        index + 1 >= len(lines)
        or not lines[index].strip().startswith("|")
        or not re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
    ):
        return None, index + 1
    table_lines = [lines[index], lines[index + 1]]
    cursor = index + 2
    while cursor < len(lines) and lines[cursor].strip().startswith("|"):
        table_lines.append(lines[cursor])
        cursor += 1
    return {
        "headers": _split_markdown_row(table_lines[0]),
        "rows": [_split_markdown_row(line) for line in table_lines[2:]],
        "markdown": "\n".join(table_lines),
    }, cursor


def _parse_qualitative_stats(stats_md: str) -> tuple[list[str], list[dict]]:
    """提取画像概览及各 Part 的客观题统计，保留同题的全部统计表。"""
    lines = str(stats_md or "").splitlines()
    part_order: list[str] = []
    sections: list[dict] = []
    current_part = ""
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        part_match = _PART_HEADING_RE.match(stripped)
        if part_match:
            current_part = part_match.group(1).strip()
            if current_part not in part_order:
                part_order.append(current_part)
            index += 1
            continue
        if stripped == "## 画像维度概览":
            current_part = "__profile__"
            index += 1
            continue
        question_match = _QUESTION_HEADING_RE.match(stripped)
        if not question_match:
            index += 1
            continue

        title = question_match.group(1).strip()
        cursor = index + 1
        body: list[str] = []
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if candidate.startswith("## ") or candidate.startswith("### "):
                break
            body.append(lines[cursor])
            cursor += 1

        tables: list[dict] = []
        notes: list[str] = []
        table_index = 0
        while table_index < len(body):
            table, next_index = _table_at(body, table_index)
            if table:
                label_lines: list[str] = []
                back = table_index - 1
                while back >= 0 and not body[back].strip():
                    back -= 1
                if back >= 0 and body[back].strip().startswith("**"):
                    label_lines.append(body[back].strip())
                table["label"] = "\n".join(label_lines)
                tables.append(table)
                table_index = next_index
                continue
            text = body[table_index].strip()
            if (
                text.startswith("- 均值:")
                or text.startswith("- 有效数字回答:")
                or text.startswith("- 非数字回答")
                or text.startswith("（共 ")
                or text.startswith("> `*`")
                or text.startswith("> （矩阵题")
            ):
                notes.append(text)
            table_index += 1

        if tables:
            sections.append({
                "part": current_part,
                "title": title,
                "tables": tables,
                "notes": notes,
            })
        index = cursor
    return part_order, sections


def _cell_percent(cell: str) -> float | None:
    match = _PERCENT_RE.search(str(cell or ""))
    return float(match.group(1)) if match else None


def _cell_number(cell: str) -> float | None:
    match = _NUMBER_RE.search(str(cell or "").replace("*", ""))
    return float(match.group(0)) if match else None


def _sample_size(table: dict, row: list[str]) -> float | None:
    headers = table.get("headers") or []
    for token in ("样本量", "该画像总计", "回答人数"):
        for position, header in enumerate(headers):
            if token in header and position < len(row):
                return _cell_number(row[position])
    return None


def _cross_table_is_meaningful(table: dict) -> bool:
    """只展示样本量尚可且描述性差异达到 15 个百分点的交叉表。"""
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    if "均值" in headers:
        mean_index = headers.index("均值")
        means = [
            _cell_number(row[mean_index])
            for row in rows
            if mean_index < len(row) and (_sample_size(table, row) or 0) >= 5
        ]
        means = [value for value in means if value is not None]
        return len(means) >= 2 and max(means) - min(means) >= 0.5

    eligible = [row for row in rows if (_sample_size(table, row) or 0) >= 5]
    if len(eligible) < 2:
        return False
    for position in range(1, len(headers)):
        values = [
            _cell_percent(row[position])
            for row in eligible
            if position < len(row)
        ]
        values = [value for value in values if value is not None]
        if len(values) >= 2 and max(values) - min(values) >= 15:
            return True
    return False


def _basic_interpretation(section: dict, include_cross: bool) -> str:
    table = section["tables"][0]
    headers = table.get("headers") or []
    rows = table.get("rows") or []
    notes = section.get("notes") or []
    metric = next((note for note in notes if note.startswith("- 均值:")), "")
    if metric:
        text = metric.removeprefix("- ").replace("**", "")
        result = f"本题的{text}。"
    else:
        percent_indexes = [
            index for index, header in enumerate(headers)
            if any(token in header for token in ("占比", "比例", "百分比"))
        ]
        ranked: list[tuple[float, str, str]] = []
        if percent_indexes:
            position = percent_indexes[0]
            for row in rows:
                if not row or position >= len(row):
                    continue
                value = _cell_percent(row[position])
                if value is not None:
                    ranked.append((value, row[0], row[position]))
        if ranked:
            ranked.sort(reverse=True)
            highest = ranked[0]
            result = f"总体分布中，“{highest[1]}”占比最高（{highest[2]}）"
            if len(ranked) > 1:
                second = ranked[1]
                result += f"，其次是“{second[1]}”（{second[2]}）"
            result += "。"
        elif headers and rows and "均值" in headers:
            mean_index = headers.index("均值")
            means = [
                (_cell_number(row[mean_index]), row[0])
                for row in rows if mean_index < len(row)
            ]
            means = [(value, label) for value, label in means if value is not None]
            if means:
                highest = max(means)
                lowest = min(means)
                result = (
                    f"从均值看，“{highest[1]}”最高（{highest[0]:g}），"
                    f"“{lowest[1]}”最低（{lowest[0]:g}）。"
                )
            else:
                result = "该表用于描述本题各项的样本分布。"
        else:
            matrix_values: list[tuple[float, str, str]] = []
            for row in rows:
                for position in range(1, min(len(row), len(headers))):
                    value = _cell_percent(row[position])
                    if value is not None:
                        matrix_values.append((value, row[0], headers[position]))
            if matrix_values:
                highest = max(matrix_values)
                result = (
                    f"矩阵各子项中，“{highest[1]}”选择“{highest[2]}”的占比最高"
                    f"（{highest[0]:g}%）。"
                )
            else:
                result = "该表用于描述本题各项的样本分布。"
    if include_cross:
        result += " 分组表中存在较明显的描述性差异，但仍需结合各组样本量谨慎理解。"
    return result


def render_qualitative_stats_by_part(
    stats_md: str,
    plan: dict | None = None,
) -> dict[str, str]:
    """为定性报告生成轻量的 Part 内辅助统计；不生成图表。"""
    part_order, sections = _parse_qualitative_stats(stats_md)
    if not part_order:
        return {}
    first_part = part_order[0]
    grouped: dict[str, list[dict]] = {part: [] for part in part_order}
    seen: set[tuple[str, str]] = set()
    for section in sections:
        part = first_part if section["part"] == "__profile__" else section["part"]
        if part not in grouped:
            continue
        key = (part, section["title"])
        if key in seen:
            continue
        seen.add(key)
        grouped[part].append(section)

    rendered: dict[str, str] = {}
    explicit_cross_tabs = json.dumps(
        (plan or {}).get("cross_tabs") or [], ensure_ascii=False,
    )
    for part, part_sections in grouped.items():
        if not part_sections:
            continue
        lines = [
            "### 辅助统计",
            "",
            "> 以下统计仅用于理解本次定性样本，不作总体推断。",
        ]
        for section in part_sections:
            lines.extend(["", f"**{section['title']}**", ""])
            notes = section.get("notes") or []
            for note in notes:
                if note.startswith("- 均值:") or note.startswith("- 有效数字回答:"):
                    lines.append(note)
            if any(note.startswith("- 均值:") for note in notes):
                lines.append("")
            lines.append(section["tables"][0]["markdown"])
            cross_tables = [
                table for table in section["tables"][1:]
                if (
                    section["title"] in explicit_cross_tabs
                    or _cross_table_is_meaningful(table)
                )
            ]
            for table in cross_tables:
                if table.get("label"):
                    lines.extend(["", table["label"]])
                lines.extend(["", table["markdown"]])
            low_sample_notes = [note for note in notes if note.startswith("> `*`")]
            if cross_tables and low_sample_notes:
                lines.extend(["", low_sample_notes[-1]])
            lines.extend([
                "",
                f"**基础解读：** {_basic_interpretation(section, bool(cross_tables))}",
            ])
        rendered[part] = "\n".join(lines).rstrip()
    return rendered


def inject_qualitative_stats(
    report_md: str,
    stats_md: str,
    plan: dict | None = None,
) -> str:
    """将辅助统计插入对应 Part 的本节总结之后，缺少总结时紧接 Part 标题。"""
    blocks = render_qualitative_stats_by_part(stats_md, plan)
    if not blocks:
        return report_md
    lines = str(report_md or "").splitlines()
    for part, block in blocks.items():
        heading = f"## {part}"
        try:
            start = next(index for index, line in enumerate(lines) if line.strip() == heading)
        except StopIteration:
            continue
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].startswith("## ")),
            len(lines),
        )
        insert_at = start + 1
        summary_index = next(
            (
                index for index in range(start + 1, end)
                if lines[index].strip().startswith("**本节总结")
            ),
            None,
        )
        if summary_index is not None:
            insert_at = next(
                (
                    index for index in range(summary_index + 1, end)
                    if (
                        lines[index].strip().startswith("### ")
                        or (
                            lines[index].strip().startswith("**")
                            and not lines[index].strip().startswith("**本节总结")
                        )
                    )
                ),
                end,
            )
        insertion = ["", block, ""]
        lines[insert_at:insert_at] = insertion
    return "\n".join(lines).strip()


def render_stats_appendix(blocks: list[dict], stats_source: str) -> str:
    """每道客观题固定输出图表标记和完整表格，不交给 AI 决定是否展示。"""
    valid = [block for block in blocks if block.get("table_markdown")]
    if not valid:
        return ""
    source_text = (
        "专业跑数表（正式统计口径）"
        if stats_source == "external_crosstab"
        else "平台 Python 自动计算"
    )
    lines = [
        "## 完整统计附录",
        "",
        f"> 统计来源：{source_text}。每道客观题均保留完整统计表；网页端同时显示统计图。",
    ]
    current_part = None
    for block in valid:
        part = str(block.get("part") or "").strip()
        if part and part != current_part:
            lines.extend(["", f"### {part}"])
            current_part = part
        heading = "####" if part else "###"
        lines.extend(["", f"{heading} {block.get('title') or '未命名题目'}", ""])
        chart = block.get("chart")
        if chart:
            payload = {
                "title": block.get("title") or "未命名题目",
                **chart,
            }
            lines.extend([
                f"```{CHART_FENCE}",
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                "```",
                "",
            ])
        lines.append(str(block["table_markdown"]).strip())
    return "\n".join(lines).rstrip()
