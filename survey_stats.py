"""调研问卷的"确定性统计"——给定 rows + plan，纯 stdlib 算频数/占比/量表分布/交叉表。

设计要点：
- 输入：rows（rows[0]=表头，rows[1:]=数据）+ plan（survey_plan 模块定义）
- 输出：
  · stats_markdown：
      1. 顶部 <metadata>
      2. ## 画像维度概览（每个画像维度的样本分布，LLM 直接引用避免数字漂移）
      3. ## Part X 章节，每个客观题（single/multi/scale）下面同时含：
         - 总体频数/占比表
         - 该题 × 每个画像维度的交叉表（行=画像取值，列=各选项的频数+占比）
         - profile_dim 列在 part 里只标注"已在画像概览展示"
      4. open_text 题在 part 里只标"开放题，N 条非空回答 → 见 <open_text> 块"
  · open_text_by_col：{col_index: [{"ids":{...}, "profile":{...}, "text":...}, ...]}
      每条原文绑定该用户的所有 id 列值 + 画像维度值，让 LLM 能按观点聚合 + 引用原话时附 ID 和画像

为什么交叉表内嵌每道题：
  用户要求"每道选择题/打分题再额外给一组按画像分组的对比表"——是固定要求不是可选项。
  Python 自动给所有"客观题 × 画像维度"组合算交叉表，比 LLM 挑选更可靠。
  plan["cross_tabs"] 字段保留兼容但已不被使用。

为什么开放题原文带 ID + 画像：
  用户要求每个观点列出"持此观点 12 人中王者 8 人、星耀 3 人..."，引用原话时附玩家 ID + 画像。
  原文只有 text 时 LLM 没法追溯来自哪个用户什么画像，所以把每条原文连同该行所有 id 列值
  + 所有画像列值一起打包传给 LLM。

为什么支持 value_aliases：
  问卷可能多语言（中文/英文/西语等），同义选项（"王者" / "Mythic" / "Mítica"）原始字符串不同，
  直接频数统计会被算成 3 个独立选项。planner 在 plan JSON 里给出 value_aliases 映射，
  Python 统计时按 canonical 聚合，保证占比正确。

数字漂移防御：分母明确写 "N 份回答中" 而不是 "样本"，writer prompt 强调不重新算。
"""

from __future__ import annotations

import re as _re
import statistics
from collections import Counter
from typing import Any, Callable

_MATRIX_ROLES = ("matrix_scale", "matrix_single", "matrix_multi")
_OTHER_OPTION_LABEL = "Other / 其他"


def _norm_choice_key(value: str) -> str:
    return " ".join(str(value or "").split()).casefold()


def _is_other_option_label(value: str) -> bool:
    return _norm_choice_key(value) in {"other", "others", "其他", "其它", "other / 其他", "其他 / other"}


def _choice_other_meta(col: dict) -> dict | None:
    meta = col.get("other_text")
    if not isinstance(meta, dict):
        return None
    return meta


def _choice_other_label(col: dict) -> str | None:
    meta = _choice_other_meta(col)
    # 用户在题型确认页取消 Other 后，不能再把未知值归并为 Other；
    # 否则复选框只影响开放文本收集，客观题统计仍然会被错误改写。
    if not meta or meta.get("enabled") is False:
        return None
    option = str((meta or {}).get("option") or "").strip()
    if option:
        return option
    for opt in col.get("options") or []:
        if _is_other_option_label(opt):
            return str(opt).strip()
    return _OTHER_OPTION_LABEL


def _choice_other_values(
    col: dict,
    norm: Callable[[str], str],
) -> set[str] | None:
    """返回用户明确选为 Other 的原始值；None 表示兼容旧 session 的全量未知值。"""
    meta = _choice_other_meta(col)
    if not meta or "values" not in meta:
        return None
    values = meta.get("values")
    if not isinstance(values, list):
        return set()
    return {
        norm(str(value).strip())
        for value in values
        if str(value or "").strip()
    }


def _should_map_to_other(
    value: str,
    *,
    norm: Callable[[str], str],
    known_options: set[str],
    other_label: str | None,
    other_values: set[str] | None,
) -> bool:
    normalized = norm(value)
    return bool(
        other_label
        and normalized not in known_options
        and (other_values is None or normalized in other_values)
    )


def _choice_norm_options(
    options: list[str] | None,
    norm: Callable[[str], str],
    other_label: str | None = None,
) -> set[str]:
    known = {norm((o or "").strip()) for o in (options or []) if str(o or "").strip()}
    if other_label:
        known.add(norm(other_label))
    return {v for v in known if v}


# ============================================================================
# 公开入口
# ============================================================================


def compute(
    rows: list[list], plan: dict
) -> tuple[str, dict[int, list[dict]]]:
    """主入口：算出 stats markdown + 开放题原文池。

    rows[0] 是表头，rows[1:] 是数据。
    open_text 返回结构：{col_index: [{"ids":{...}, "profile":{...}, "text":...}, ...]}
    """
    if not rows:
        return "<metadata>总样本: 0</metadata>\n\n（表格为空）", {}

    headers = rows[0]
    body = rows[1:]
    total = len(body)

    cols_by_index: dict[int, dict] = {c["index"]: c for c in plan["columns"]}
    profile_cols = [c for c in plan["columns"] if c["role"] == "profile_dim"]
    mlbb_id_cols = [c for c in plan["columns"] if c["role"] == "mlbbid"]
    id_cols = [c for c in plan["columns"] if c["role"] == "id"]
    segment_indexes = {
        part.get("filter", {}).get("column_index")
        for part in plan.get("parts") or []
        if isinstance(part.get("filter"), dict)
    }
    segment_cols = [c for c in plan["columns"] if c["index"] in segment_indexes]

    md_parts: list[str] = []

    # 顶部 metadata
    blank_count = sum(1 for r in body if all(_is_blank(c) for c in r))
    valid = total - blank_count
    md_parts.append(
        f"<metadata>总样本: {total}, 有效样本: {valid}"
        + (f", 全空被排除: {blank_count} 条" if blank_count else "")
        + "</metadata>"
    )
    md_parts.append("")

    # 画像维度概览（让 LLM 在报告开头直接引用）
    if profile_cols:
        md_parts.append("## 画像维度概览")
        md_parts.append("")
        for p_col in profile_cols:
            section = _render_profile_overview(p_col, headers, body)
            md_parts.append(section)
            md_parts.append("")

    # 按 part 分组渲染
    for i, part in enumerate(plan["parts"], 1):
        md_parts.append(f"## Part {i} {part['name']}")
        md_parts.append("")
        part_body = _filter_rows_for_part(body, part, cols_by_index)
        if part.get("filter"):
            md_parts.append(_part_filter_note(part, cols_by_index, len(part_body)))
            md_parts.append("")
        rendered_matrix: set[str] = set()
        for col_idx in part["column_indexes"]:
            col = cols_by_index.get(col_idx)
            if not col:
                continue
            # 矩阵题：跨多列，按 matrix_group 合并渲染一次
            if col["role"] in _MATRIX_ROLES:
                grp = col.get("matrix_group") or col.get("name") or f"矩阵题{col_idx}"
                if grp in rendered_matrix:
                    continue
                members = [
                    cols_by_index[j]
                    for j in part["column_indexes"]
                    if cols_by_index.get(j)
                    and cols_by_index[j]["role"] == col["role"]
                    and (cols_by_index[j].get("matrix_group") or "") == (col.get("matrix_group") or "")
                ]
                section = _render_matrix(grp, col["role"], members, headers, part_body)
                rendered_matrix.add(grp)
                if section:
                    md_parts.append(section)
                    md_parts.append("")
                continue
            section = _render_column(col, headers, part_body, profile_cols)
            if section:
                md_parts.append(section)
                md_parts.append("")

    # 开放题数据池：每条原文带 ids + profile
    open_text: dict[int, list[dict]] = {}
    for c in plan["columns"]:
        if c["role"] == "open_text":
            open_text[c["index"]] = _collect_open_text(
                c["index"], body, headers, mlbb_id_cols + id_cols, profile_cols, segment_cols
            )
    for c in plan["columns"]:
        if c["role"] in ("single_choice", "multi_choice"):
            entries = _collect_choice_other_text(
                c, body, headers, mlbb_id_cols + id_cols, profile_cols, segment_cols
            )
            if entries:
                open_text.setdefault(c["index"], []).extend(entries)

    return "\n".join(md_parts).rstrip() + "\n", open_text


def collect_open_text(rows: list[list], plan: dict) -> dict[int, list[dict]]:
    """只收集开放题原文池（不做任何数值统计）。

    用于「跑数表模式」：数字来自外部跑数表，平台不再自算统计，
    只需把开放题原文按列收集好（结构与 compute() 返回的 open_text 完全一致），
    交给大样本聚类引擎处理。这样既复用已有收集逻辑，又避开易报错的数值计算。
    """
    if not rows:
        return {}
    headers = rows[0]
    body = rows[1:]
    profile_cols = [c for c in plan["columns"] if c["role"] == "profile_dim"]
    mlbb_id_cols = [c for c in plan["columns"] if c["role"] == "mlbbid"]
    id_cols = [c for c in plan["columns"] if c["role"] == "id"]
    segment_indexes = {
        part.get("filter", {}).get("column_index")
        for part in plan.get("parts") or []
        if isinstance(part.get("filter"), dict)
    }
    segment_cols = [c for c in plan["columns"] if c["index"] in segment_indexes]
    open_text: dict[int, list[dict]] = {}
    for c in plan["columns"]:
        if c["role"] == "open_text":
            open_text[c["index"]] = _collect_open_text(
                c["index"], body, headers, mlbb_id_cols + id_cols, profile_cols, segment_cols
            )
    return open_text


def _filter_rows_for_part(
    body: list[list], part: dict, cols_by_index: dict[int, dict]
) -> list[list]:
    part_filter = part.get("filter")
    if not isinstance(part_filter, dict):
        return body
    filter_idx = part_filter.get("column_index")
    filter_col = cols_by_index.get(filter_idx)
    if not filter_col:
        return []
    norm = _make_normalizer(filter_col.get("value_aliases"))
    allowed = {norm(str(option).strip()) for option in part_filter.get("allowed_options") or []}
    return [
        row for row in body
        if filter_idx < len(row) and norm(_format_cell(row[filter_idx]).strip()) in allowed
    ]


def _part_filter_note(part: dict, cols_by_index: dict[int, dict], count: int) -> str:
    part_filter = part.get("filter") or {}
    filter_col = cols_by_index.get(part_filter.get("column_index")) or {}
    parent_name = filter_col.get("name") or f"列{part_filter.get('column_index')}"
    options = " / ".join(f"「{option}」" for option in part_filter.get("allowed_options") or [])
    return f"适用范围：{parent_name}选择{options}；本 Part 有效人群 {count} 人。"


def _split_markdown_row(line: str) -> list[str]:
    """拆分 Markdown 表格行，保留转义的竖线字符。"""
    text = line.strip().strip("|")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in text:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "|":
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    cells.append("".join(current).strip())
    return cells


def _percent_number(value: str) -> float | None:
    match = _re.search(r"(-?\d+(?:\.\d+)?)\s*%", str(value or ""))
    if not match:
        return None
    return max(0.0, min(100.0, float(match.group(1))))


def structured_tables(stats_md: str) -> list[dict]:
    """把 Python 统计 Markdown 转为稳定的图表/附录数据。

    每道客观题只取第一张总体表；后续画像交叉表仍保留在 ``stats_md``
    供报告写作使用，但不重复塞入完整统计附录。
    """
    lines = str(stats_md or "").splitlines()
    blocks: list[dict] = []
    current_part = ""
    current_question = ""
    captured_question = ""
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped.startswith("## Part "):
            current_part = stripped.removeprefix("## ").strip()
            current_question = ""
            captured_question = ""
        elif stripped.startswith("### "):
            current_question = stripped.removeprefix("### ").strip()
            captured_question = ""
        elif stripped.startswith("## ") and not stripped.startswith("## 画像维度概览"):
            current_question = stripped.removeprefix("## ").strip()
            captured_question = ""

        if (
            current_question
            and current_question != captured_question
            and stripped.startswith("|")
            and index + 1 < len(lines)
            and _re.match(r"^\s*\|?\s*:?-{3,}", lines[index + 1])
        ):
            table_lines = [lines[index], lines[index + 1]]
            cursor = index + 2
            while cursor < len(lines) and lines[cursor].strip().startswith("|"):
                table_lines.append(lines[cursor])
                cursor += 1
            headers = _split_markdown_row(table_lines[0])
            rows = [_split_markdown_row(line) for line in table_lines[2:]]
            if rows and headers:
                chart: dict | None = None
                if headers[0] == "子项" and len(headers) > 2:
                    option_headers = headers[1:-1]
                    values = [
                        [_percent_number(cell) for cell in row[1:1 + len(option_headers)]]
                        for row in rows
                    ]
                    chart = {
                        "type": "heatmap",
                        "columns": option_headers,
                        "rows": [row[0] for row in rows],
                        "values": values,
                    }
                else:
                    percent_indexes = [
                        position for position, header in enumerate(headers)
                        if any(token in header for token in ("占比", "比例", "百分比"))
                    ]
                    if percent_indexes:
                        chart = {
                            "type": "bar",
                            "labels": [row[0] for row in rows],
                            "series": [
                                {
                                    "name": headers[position],
                                    "values": [
                                        _percent_number(row[position])
                                        if position < len(row) else None
                                        for row in rows
                                    ],
                                }
                                for position in percent_indexes[:6]
                            ],
                        }
                blocks.append({
                    "title": current_question,
                    "part": current_part,
                    "chart": chart,
                    "table_markdown": "\n".join(table_lines),
                })
                captured_question = current_question
            index = cursor
            continue
        index += 1
    return blocks


# ============================================================================
# 画像维度概览
# ============================================================================


def _render_profile_overview(p_col: dict, headers: list[str], body: list[list]) -> str:
    name = p_col.get("name") or _safe_header(headers, p_col["index"])
    aliases = p_col.get("value_aliases")
    norm = _make_normalizer(aliases)
    raw = _column_values(body, p_col["index"])
    nonblank = [norm(v) for v in raw if v.strip()]
    if not nonblank:
        return f"### {name}\n\n（该画像维度无有效数据）"
    counts = Counter(nonblank)
    total = sum(counts.values())
    lines = [f"### {name}"]
    lines.append("")
    lines.append("| 取值 | 频数 | 占比 |")
    lines.append("|---|---|---|")
    for v, n in counts.most_common():
        lines.append(f"| {_md_escape(v)} | {n} | {_pct(n, total)} |")
    lines.append(f"\n（共 {total} 份非空回答）")
    return "\n".join(lines)


# ============================================================================
# 单列渲染（part 内每道题）
# ============================================================================


def _render_column(
    col: dict, headers: list[str], body: list[list], profile_cols: list[dict]
) -> str:
    role = col["role"]
    idx = col["index"]
    name = col.get("name") or _safe_header(headers, idx)
    title = f"### {name}"

    if role in ("id", "mlbbid", "ignore"):
        label = {"id": "用户ID", "mlbbid": "MLBB ID", "ignore": "已忽略"}.get(role, "")
        return f"{title}\n\n（{label}列，不参与统计）"

    if role == "profile_dim":
        return f"{title}\n\n（画像维度，分布表见上方「画像维度概览」章节）"

    raw_values = _column_values(body, idx)
    nonblank_raw = [v for v in raw_values if v.strip()]
    if not nonblank_raw:
        return f"{title}\n\n（该列无有效数据）"

    aliases = col.get("value_aliases")
    norm = _make_normalizer(aliases)

    if role == "single_choice":
        other_label = _choice_other_label(col)
        options = col.get("options")
        known_options = _choice_norm_options(options, norm, other_label)
        other_values = _choice_other_values(col, norm)
        nonblank_norm = [
            other_label
            if options and _should_map_to_other(
                v,
                norm=norm,
                known_options=known_options,
                other_label=other_label,
                other_values=other_values,
            )
            else norm(v)
            for v in nonblank_raw
        ]
        body_md = _render_single_choice(nonblank_norm)
        body_md += _append_cross_tabs(
            col, raw_values, profile_cols, body, headers,
            single=True, options=options, other_label=other_label,
            other_values=other_values,
        )
    elif role == "multi_choice":
        delimiter = col.get("delimiter") or _guess_delimiter(nonblank_raw)
        options = col.get("options")
        other_label = _choice_other_label(col)
        other_values = _choice_other_values(col, norm)
        body_md = _render_multi_choice(
            nonblank_raw, delimiter, norm, options, other_label, other_values,
        )
        body_md += _append_cross_tabs(
            col, raw_values, profile_cols, body, headers,
            single=False, delimiter=delimiter, options=options, other_label=other_label,
            other_values=other_values,
        )
    elif role == "scale":
        lo = col.get("min")
        hi = col.get("max")
        body_md = _render_scale(nonblank_raw, lo, hi)
        body_md += _append_cross_tabs_scale(
            col, raw_values, profile_cols, body, headers,
        )
    elif role == "open_text":
        body_md = (
            f"（开放题，{len(nonblank_raw)} 条非空回答 → "
            f"见 `<open_text>` 块，每条带玩家 ID 和画像）"
        )
    elif role in _MATRIX_ROLES:
        # 矩阵题由 compute() 在组级合并渲染，单列不在此处理
        return ""
    else:
        body_md = "（未识别的题型）"

    return f"{title}\n\n{body_md}"


def _append_cross_tabs(
    col: dict,
    q_raw: list[str],
    profile_cols: list[dict],
    body: list[list],
    headers: list[str],
    *,
    single: bool,
    delimiter: str = ",",
    options: list[str] | None = None,
    other_label: str | None = None,
    other_values: set[str] | None = None,
) -> str:
    """单选/多选题：跟每个 profile_dim 配交叉表。"""
    if not profile_cols:
        return ""
    out = []
    q_norm_fn = _make_normalizer(col.get("value_aliases"))
    for p_col in profile_cols:
        if p_col["index"] == col["index"]:
            continue  # 跟自己交叉没意义
        p_raw = _column_values(body, p_col["index"])
        p_name = p_col.get("name") or _safe_header(headers, p_col["index"])
        p_norm_fn = _make_normalizer(p_col.get("value_aliases"))
        ct_md = _cross_tab_categorical(
            p_raw, q_raw, p_norm=p_norm_fn, q_norm=q_norm_fn,
            single=single, delimiter=delimiter, options=options, other_label=other_label,
            other_values=other_values,
        )
        if ct_md:
            out.append(f"\n\n**按「{p_name}」分组**\n\n{ct_md}")
    return "".join(out)


def _append_cross_tabs_scale(
    col: dict,
    q_raw: list[str],
    profile_cols: list[dict],
    body: list[list],
    headers: list[str],
) -> str:
    if not profile_cols:
        return ""
    out = []
    for p_col in profile_cols:
        p_raw = _column_values(body, p_col["index"])
        p_name = p_col.get("name") or _safe_header(headers, p_col["index"])
        p_norm = _make_normalizer(p_col.get("value_aliases"))
        ct_md = _cross_tab_scale(p_raw, q_raw, p_norm=p_norm)
        if ct_md:
            out.append(f"\n\n**按「{p_name}」分组（量表均值对比）**\n\n{ct_md}")
    return "".join(out)


def _render_single_choice(values: list[str]) -> str:
    counts = Counter(values)
    total = sum(counts.values())
    rows: list[str] = []
    rows.append("总体分布：")
    rows.append("")
    rows.append("| 选项 | 频数 | 占比 |")
    rows.append("|---|---|---|")
    for val, n in counts.most_common():
        rows.append(f"| {_md_escape(val)} | {n} | {_pct(n, total)} |")
    rows.append(f"\n（共 {total} 份非空回答）")
    return "\n".join(rows)


def _render_multi_choice(
    values: list[str],
    delimiter: str,
    norm: Callable[[str], str],
    options: list[str] | None = None,
    other_label: str | None = None,
    other_values: set[str] | None = None,
) -> str:
    counts: Counter = Counter()
    for v in values:
        opts = set(_split_by_vocab(
            v, options, delimiter, norm,
            other_label=other_label,
            other_values=other_values,
        ))
        for opt in opts:
            if opt:
                counts[opt] += 1
    total_responders = len(values)
    rows: list[str] = []
    how = "按选项词表匹配" if options else f"分隔符: `{delimiter}`"
    rows.append(
        f"总体分布（多选题，{how}，分母 = {total_responders} 份非空回答）："
    )
    rows.append("")
    rows.append("| 选项 | 选择人数 | 占比 |")
    rows.append("|---|---|---|")
    for val, n in counts.most_common():
        rows.append(f"| {_md_escape(val)} | {n} | {_pct(n, total_responders)} |")
    return "\n".join(rows)


def _render_scale(values: list[str], lo: Any, hi: Any) -> str:
    nums: list[float] = []
    invalid = 0
    for v in values:
        try:
            nums.append(float(v.strip()))
        except (ValueError, TypeError):
            invalid += 1

    if not nums:
        return "（量表题：所有回答均不能转为数字）"

    mean = statistics.mean(nums)
    median = statistics.median(nums)
    stdev = statistics.pstdev(nums) if len(nums) > 1 else 0.0

    rows: list[str] = []
    rows.append(
        f"- 均值: **{mean:.2f}**, 中位数: {median:g}, 标准差: {stdev:.2f}"
    )
    rows.append(f"- 有效数字回答: {len(nums)} 条")
    if invalid:
        rows.append(f"- 非数字回答（已剔除均值计算）: {invalid} 条")

    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        lo_i = int(lo)
        hi_i = int(hi)
        if hi_i - lo_i <= 12 and hi_i > lo_i:
            counts: Counter = Counter()
            for n in nums:
                counts[round(n)] += 1
            rows.append("")
            rows.append("分布：")
            rows.append("| 取值 | 频数 | 占比 |")
            rows.append("|---|---|---|")
            for v in range(lo_i, hi_i + 1):
                c = counts.get(v, 0)
                rows.append(f"| {v} | {c} | {_pct(c, len(nums))} |")
        else:
            rows.append(_histogram_bins(nums, lo_i, hi_i))
    return "\n".join(rows)


def _histogram_bins(nums: list[float], lo: int, hi: int, bins: int = 5) -> str:
    if hi <= lo:
        return ""
    width = (hi - lo) / bins
    counts = [0] * bins
    for n in nums:
        if n < lo:
            counts[0] += 1
            continue
        if n >= hi:
            counts[-1] += 1
            continue
        b = min(int((n - lo) / width), bins - 1)
        counts[b] += 1
    lines = ["", "分布：", "| 区间 | 频数 | 占比 |", "|---|---|---|"]
    for i, c in enumerate(counts):
        a = lo + i * width
        b = a + width
        lines.append(f"| {a:g}–{b:g} | {c} | {_pct(c, len(nums))} |")
    return "\n".join(lines)


# ============================================================================
# 交叉表
# ============================================================================


def _cross_tab_categorical(
    p_raw: list[str],
    q_raw: list[str],
    *,
    p_norm: Callable[[str], str],
    q_norm: Callable[[str], str],
    single: bool,
    delimiter: str = ",",
    options: list[str] | None = None,
    other_label: str | None = None,
    other_values: set[str] | None = None,
) -> str:
    """画像 × 类目题。每格 'n (xx%)'，每格 < 5 加 *。

    占比分母 = 该 profile 取值下的回答人数（按行）。
    多选题：行内集合化避免重复同选项；有选项词表时按词表匹配切分。
    """
    pairs = list(zip(p_raw, q_raw))
    pairs = [(p, q) for p, q in pairs if p.strip() and q.strip()]
    if not pairs:
        return "（无有效配对数据）"

    # 应用 normalizer
    pairs_norm = [(p_norm(p), q) for p, q in pairs]

    p_options = list(dict.fromkeys(p for p, _ in pairs_norm))
    known_options = _choice_norm_options(options, q_norm, other_label)

    def normalize_q(q: str) -> str:
        qn = q_norm(q)
        if options and _should_map_to_other(
            q,
            norm=q_norm,
            known_options=known_options,
            other_label=other_label,
            other_values=other_values,
        ):
            return other_label
        return qn

    if single:
        q_options = list(dict.fromkeys(normalize_q(q) for _, q in pairs_norm))
    else:
        q_set: list[str] = []
        seen: set[str] = set()
        for _, q in pairs_norm:
            for normo in _split_by_vocab(
                q, options, delimiter, q_norm,
                other_label=other_label,
                other_values=other_values,
            ):
                if normo and normo not in seen:
                    seen.add(normo)
                    q_set.append(normo)
        q_options = q_set

    p_totals: Counter = Counter()
    for p, _ in pairs_norm:
        p_totals[p] += 1

    grid: dict[tuple[str, str], int] = {}
    for p, q in pairs_norm:
        if single:
            qn = normalize_q(q)
            grid[(p, qn)] = grid.get((p, qn), 0) + 1
        else:
            opts = set(_split_by_vocab(
                q, options, delimiter, q_norm,
                other_label=other_label,
                other_values=other_values,
            ))
            for o in opts:
                if o:
                    grid[(p, o)] = grid.get((p, o), 0) + 1

    has_low = False
    lines: list[str] = []
    header_cells = [""] + [_md_escape(o) for o in q_options] + ["该画像总计"]
    lines.append("| " + " | ".join(header_cells) + " |")
    lines.append("|" + "|".join(["---"] * len(header_cells)) + "|")
    # 按 profile 总计降序
    for p in sorted(p_options, key=lambda x: -p_totals[x]):
        row_cells = [_md_escape(p)]
        denom = p_totals[p]
        for o in q_options:
            n = grid.get((p, o), 0)
            star = "*" if 0 < n < 5 else ""
            if 0 < n < 5:
                has_low = True
            row_cells.append(f"{n}{star} ({_pct(n, denom)})")
        row_cells.append(str(denom))
        lines.append("| " + " | ".join(row_cells) + " |")

    if has_low:
        lines.append("")
        lines.append("> `*` 该格样本量 < 5，谨慎解读")
    if not single:
        lines.append("")
        lines.append(
            f"> 多选题：分母 = 该画像取值下的**回答人数**，不是选项次数；分隔符 `{delimiter}`"
        )
    return "\n".join(lines)


def _cross_tab_scale(
    p_raw: list[str], q_raw: list[str], *, p_norm: Callable[[str], str]
) -> str:
    pairs = []
    invalid = 0
    for p, q in zip(p_raw, q_raw):
        if not p.strip() or not q.strip():
            continue
        try:
            pairs.append((p_norm(p.strip()), float(q.strip())))
        except (ValueError, TypeError):
            invalid += 1

    if not pairs:
        return "（无有效配对数据）"

    by_profile: dict[str, list[float]] = {}
    for p, q in pairs:
        by_profile.setdefault(p, []).append(q)

    lines: list[str] = []
    lines.append("| 画像取值 | 样本量 | 均值 | 中位数 | 标准差 |")
    lines.append("|---|---|---|---|---|")
    has_low = False
    for p, nums in sorted(by_profile.items(), key=lambda kv: -len(kv[1])):
        n = len(nums)
        star = "*" if n < 5 else ""
        if n < 5:
            has_low = True
        mean = statistics.mean(nums)
        median = statistics.median(nums)
        stdev = statistics.pstdev(nums) if n > 1 else 0.0
        lines.append(
            f"| {_md_escape(p)} | {n}{star} | {mean:.2f} | {median:g} | {stdev:.2f} |"
        )
    if has_low:
        lines.append("")
        lines.append("> `*` 该画像取值的样本量 < 5，均值不稳定，谨慎解读")
    if invalid:
        lines.append(f"> 另有 {invalid} 条非数字回答未参与计算")
    return "\n".join(lines)


# ============================================================================
# 开放题原文池：每条带 ids + profile
# ============================================================================


def _choice_parts_with_match(
    cell: str,
    options: list[str] | None,
    delimiter: str,
    norm: Callable[[str], str],
    *,
    other_values: set[str] | None = None,
) -> list[tuple[str | None, str]]:
    cell = (cell or "").strip()
    if not cell:
        return []
    if not options:
        return [(norm(x.strip()), x.strip()) for x in cell.split(delimiter) if x.strip()]

    norm_opts = _choice_norm_options(options, norm)
    frags = cell.split(delimiter)

    def match_at(start: int) -> tuple[str, str, int] | None:
        for end in range(len(frags), start, -1):
            raw = delimiter.join(frags[start:end]).strip()
            cand = norm(raw)
            if cand in norm_opts:
                return cand, raw, end
        return None

    result: list[tuple[str | None, str]] = []
    i = 0
    while i < len(frags):
        if not frags[i].strip():
            i += 1
            continue
        matched = match_at(i)
        if matched:
            cand, raw, i = matched
            result.append((cand, raw))
            continue

        if other_values is not None:
            raw = frags[i].strip()
            result.append((None, raw))
            i += 1
            continue

        end = i + 1
        while end < len(frags) and match_at(end) is None:
            end += 1
        raw = delimiter.join(frags[i:end]).strip()
        if raw:
            result.append((None, raw))
        i = end
    return result


def _collect_choice_other_text(
    col: dict,
    body: list[list],
    headers: list[str],
    id_cols: list[dict],
    profile_cols: list[dict],
    segment_cols: list[dict],
) -> list[dict]:
    other_label = _choice_other_label(col)
    meta = _choice_other_meta(col)
    options = col.get("options")
    if (meta and meta.get("enabled") is False) or not other_label or not options:
        return []

    idx = col["index"]
    role = col["role"]
    norm = _make_normalizer(col.get("value_aliases"))
    known_options = _choice_norm_options(options, norm, other_label)
    other_values = _choice_other_values(col, norm)
    delimiter = col.get("delimiter") or ","
    out: list[dict] = []
    seen_rows: set[tuple[int, str]] = set()

    for row_no, row in enumerate(body):
        raw = _format_cell(row[idx]) if idx < len(row) else ""
        if not raw.strip():
            continue

        unknown_texts: list[str] = []
        if role == "single_choice":
            if _should_map_to_other(
                raw.strip(),
                norm=norm,
                known_options=known_options,
                other_label=other_label,
                other_values=other_values,
            ):
                unknown_texts.append(raw.strip())
        elif role == "multi_choice":
            for matched, raw_part in _choice_parts_with_match(
                raw, options, delimiter, norm, other_values=other_values,
            ):
                if (
                    matched is None
                    and raw_part
                    and not _is_other_option_label(raw_part)
                    and _should_map_to_other(
                        raw_part,
                        norm=norm,
                        known_options=known_options,
                        other_label=other_label,
                        other_values=other_values,
                    )
                ):
                    unknown_texts.append(raw_part)

        for text in unknown_texts:
            key = (row_no, _norm_choice_key(text))
            if key in seen_rows:
                continue
            seen_rows.add(key)
            entry = _build_open_text_entry(
                row, text, headers, id_cols, profile_cols, segment_cols,
                row_number=row_no + 1,
            )
            entry["source"] = "choice_other_text"
            entry["parent_question"] = col.get("name") or _safe_header(headers, idx)
            entry["parent_index"] = idx
            entry["other_option"] = other_label
            out.append(entry)
    return out


def _build_open_text_entry(
    row: list,
    text: str,
    headers: list[str],
    id_cols: list[dict],
    profile_cols: list[dict],
    segment_cols: list[dict],
    *,
    row_number: int,
) -> dict:
    ids: dict[str, str] = {}
    for c in id_cols:
        i = c["index"]
        v = _format_cell(row[i]) if i < len(row) else ""
        if v.strip():
            key = "MLBB ID" if c.get("role") == "mlbbid" else (c.get("name") or _safe_header(headers, i))
            if c.get("role") == "mlbbid":
                v = _format_mlbb_id(v)
            ids[key] = v.strip()

    profile: dict[str, str] = {}
    for c in profile_cols:
        i = c["index"]
        v = _format_cell(row[i]) if i < len(row) else ""
        if v.strip():
            key = c.get("name") or _safe_header(headers, i)
            norm = _make_normalizer(c.get("value_aliases"))
            profile[key] = norm(v.strip())

    segments: dict[str, str] = {}
    for c in segment_cols:
        i = c["index"]
        v = _format_cell(row[i]) if i < len(row) else ""
        if v.strip():
            norm = _make_normalizer(c.get("value_aliases"))
            segments[str(i)] = norm(v.strip())

    respondent_key = "|".join(
        f"{key}={value}" for key, value in sorted(ids.items())
    ) or f"row:{row_number}"

    return {
        "respondent_key": respondent_key,
        "ids": ids,
        "profile": profile,
        "segments": segments,
        "text": text.strip(),
    }


def _collect_open_text(
    col_idx: int,
    body: list[list],
    headers: list[str],
    id_cols: list[dict],
    profile_cols: list[dict],
    segment_cols: list[dict],
) -> list[dict]:
    """收集某开放题的所有非空原文，每条附该行的所有 id 列值 + 画像列值。

    LLM 拿到这种结构后能直接做：
      - 按观点聚合时统计画像分布（"持此观点 12 人中王者 8 人..."）
      - 引用原话时附玩家 ID + 画像（"mlbbid:xxx (王者/中国): ..."）
    """
    out: list[dict] = []
    for row_no, row in enumerate(body, 1):
        text = _format_cell(row[col_idx]) if col_idx < len(row) else ""
        if not text.strip():
            continue
        out.append(
            _build_open_text_entry(
                row, text.strip(), headers, id_cols, profile_cols, segment_cols,
                row_number=row_no,
            )
        )
    return out


# ============================================================================
# 工具函数
# ============================================================================


def _make_normalizer(aliases: dict | None) -> Callable[[str], str]:
    """根据 value_aliases（{canonical: [aliases...]}）构造一个 value→canonical 的映射函数。

    比对用 strip + casefold（覆盖中英文混合大小写差异）。canonical 自身也算一个别名。
    没有 aliases 时返回 identity（去首尾空白）。
    """
    if not aliases or not isinstance(aliases, dict):
        return lambda v: v.strip() if isinstance(v, str) else v

    table: dict[str, str] = {}
    for canonical, alias_list in aliases.items():
        if not isinstance(canonical, str):
            continue
        table[canonical.strip().casefold()] = canonical
        if isinstance(alias_list, list):
            for a in alias_list:
                if isinstance(a, str):
                    table[a.strip().casefold()] = canonical

    def norm(v: str) -> str:
        if not isinstance(v, str):
            return v
        key = v.strip().casefold()
        return table.get(key, v.strip())

    return norm


def _column_values(body: list[list], idx: int) -> list[str]:
    out: list[str] = []
    for row in body:
        if idx < len(row):
            out.append(_format_cell(row[idx]))
        else:
            out.append("")
    return out


def _format_cell(cell: Any) -> str:
    if cell is None:
        return ""
    if isinstance(cell, list):
        parts = []
        for item in cell:
            if isinstance(item, dict):
                parts.append(str(item.get("text", "")))
            else:
                parts.append(str(item))
        return "".join(parts)
    return str(cell)


def _format_mlbb_id(value: str) -> str:
    """Normalize combined MLBB UID/server values to UID(server)."""
    s = " ".join(str(value or "").split()).strip()
    if not s:
        return ""

    # Already in the preferred form, possibly with extra spaces: 123456 (57001)
    m = _re.match(r"^(\d+)\s*\(\s*(\d+)\s*\)$", s)
    if m:
        return f"{m.group(1)}({m.group(2)})"

    # Common exports put UID and server in one cell separated by newline, spaces,
    # slash, comma, or punctuation. Keep single-number values untouched.
    nums = _re.findall(r"\d+", s)
    if len(nums) >= 2:
        return f"{nums[0]}({nums[1]})"
    return s


def _is_blank(cell: Any) -> bool:
    if cell is None or cell == "":
        return True
    if isinstance(cell, list) and not cell:
        return True
    if isinstance(cell, str) and not cell.strip():
        return True
    return False


def _md_escape(s: str) -> str:
    if s is None:
        return ""
    return str(s).replace("|", "\\|").replace("\n", " ")


def _pct(n: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{n * 100 / total:.1f}%"


_DELIMITER_CANDIDATES = [",", "，", ";", "；", "、", "/", "|"]


def _guess_delimiter(values: list[str]) -> str:
    sample = values[:20]
    # 倍市得拆列多选在标准化后使用换行拼接。换行的出现次数通常少于
    # 长文本选项内部的逗号数，因此必须优先识别，不能只按字符频次比较。
    if any("\n" in value for value in sample):
        return "\n"
    counts: dict[str, int] = {d: 0 for d in _DELIMITER_CANDIDATES}
    for v in sample:
        for d in _DELIMITER_CANDIDATES:
            counts[d] += v.count(d)
    best = max(counts.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return ","
    return best[0]


def _split_by_vocab(
    cell: str,
    options: list[str] | None,
    delimiter: str,
    norm: Callable[[str], str],
    *,
    other_label: str | None = None,
    other_values: set[str] | None = None,
) -> list[str]:
    """把一个多选单元格切成 normalized 选项列表。

    有 options 词表时：先按 delimiter 切片（不去空白），再对相邻片段做**最长连续
    重组**匹配已知选项——这样"选项本身含分隔符"（如 "Yes, definitely"）也能被还原；
    无法匹配的片段退回单片段。无 options 时退回普通 split。
    """
    cell = (cell or "").strip()
    if not cell:
        return []
    if not options:
        return [norm(x.strip()) for x in cell.split(delimiter) if x.strip()]

    # normalized 选项集合（canonical 经 norm 后的形式）
    norm_opts = set()
    for o in options:
        no = norm((o or "").strip())
        if no:
            norm_opts.add(no)

    frags = cell.split(delimiter)  # 不 strip：重组时用同一 delimiter join 可还原原串
    n = len(frags)

    def match_at(start: int) -> tuple[str, int] | None:
        for end in range(n, start, -1):
            cand = norm(delimiter.join(frags[start:end]).strip())
            if cand in norm_opts:
                return cand, end
        return None

    result: list[str] = []
    i = 0
    while i < n:
        if not frags[i].strip():
            i += 1
            continue
        matched = match_at(i)
        if matched:
            cand, i = matched
            result.append(cand)
            continue

        if other_values is not None:
            fallback = norm(frags[i].strip())
            map_to_other = bool(other_label and fallback in other_values)
            result.append(other_label if map_to_other else fallback)
            i += 1
            continue

        end = i + 1
        while end < n and match_at(end) is None:
            end += 1
        fallback = norm(delimiter.join(frags[i:end]).strip())
        result.append(other_label if other_label else fallback)
        i = end
    return result


# ============================================================================
# 矩阵题渲染（matrix_scale / matrix_single / matrix_multi）：组级合并成一张表
# ============================================================================


def _render_matrix(
    group_name: str,
    role: str,
    members: list[dict],
    headers: list[str],
    body: list[list],
) -> str:
    """把同一矩阵题的多列合并成一张表。members 顺序即子项行顺序。"""
    title = f"### {group_name}"
    if not members:
        return f"{title}\n\n（矩阵题无成员列）"

    if role == "matrix_scale":
        lo = members[0].get("min")
        hi = members[0].get("max")
        rng = ""
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            rng = f"，量程 {lo:g}–{hi:g}"
        lines = [
            f"{title}",
            "",
            f"矩阵打分（每个子项一行{rng}）：",
            "",
            "| 子项 | 样本量 | 均值 | 中位数 | 标准差 |",
            "|---|---|---|---|---|",
        ]
        has_low = False
        for m in members:
            row_label = m.get("matrix_row") or _safe_header(headers, m["index"])
            nums: list[float] = []
            for v in _column_values(body, m["index"]):
                if v.strip():
                    try:
                        nums.append(float(v.strip()))
                    except (ValueError, TypeError):
                        pass
            if not nums:
                lines.append(f"| {_md_escape(row_label)} | 0 | — | — | — |")
                continue
            mean = statistics.mean(nums)
            median = statistics.median(nums)
            stdev = statistics.pstdev(nums) if len(nums) > 1 else 0.0
            star = "*" if len(nums) < 5 else ""
            if len(nums) < 5:
                has_low = True
            lines.append(
                f"| {_md_escape(row_label)} | {len(nums)}{star} | {mean:.2f} | {median:g} | {stdev:.2f} |"
            )
        if has_low:
            lines.append("")
            lines.append("> `*` 该子项有效样本量 < 5，均值不稳定，谨慎解读")
        lines.append("")
        lines.append("> （矩阵题 × 画像维度的交叉分析本期暂未提供）")
        return "\n".join(lines)

    if role == "matrix_single":
        shared_options = members[0].get("options")
        norm = _make_normalizer(members[0].get("value_aliases"))
        opt_order: list[str] = []
        seen: set[str] = set()
        for option in shared_options or []:
            value = norm(str(option).strip())
            if value and value not in seen:
                seen.add(value)
                opt_order.append(value)
        if not opt_order:
            for member in members:
                for value in _column_values(body, member["index"]):
                    normalized = norm(value.strip())
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        opt_order.append(normalized)

        lines = [
            f"{title}",
            "",
            "矩阵单选（每个子项一行；单元格 = 选择人数(占比)，分母 = 该子项非空回答人数）：",
            "",
            "| 子项 | " + " | ".join(_md_escape(option) for option in opt_order) + " | 回答人数 |",
            "|" + "|".join(["---"] * (len(opt_order) + 2)) + "|",
        ]
        for member in members:
            row_label = member.get("matrix_row") or _safe_header(headers, member["index"])
            values = [
                norm(value.strip())
                for value in _column_values(body, member["index"])
                if value.strip()
            ]
            counts = Counter(values)
            denom = len(values)
            cells = [_md_escape(row_label)]
            for option in opt_order:
                count = counts.get(option, 0)
                cells.append(f"{count} ({_pct(count, denom)})" if denom else "0")
            cells.append(str(denom))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
        lines.append("> （矩阵题 × 画像维度的交叉分析本期暂未提供）")
        return "\n".join(lines)

    # matrix_multi：每个子项一行，列为各选项的选择人数/占比
    delimiter = members[0].get("delimiter") or "，"
    shared_options = members[0].get("options")
    norm = _make_normalizer(None)

    # 收集列选项全集（优先用共享词表，否则从数据里抽）
    if shared_options:
        opt_order = [norm(o.strip()) for o in shared_options if o.strip()]
    else:
        opt_order = []
        seen: set[str] = set()
        for m in members:
            for v in _column_values(body, m["index"]):
                if not v.strip():
                    continue
                for o in _split_by_vocab(v, shared_options, delimiter, norm):
                    if o and o not in seen:
                        seen.add(o)
                        opt_order.append(o)

    lines = [
        f"{title}",
        "",
        "矩阵多选（每个子项一行；单元格 = 选择人数(占比)，分母 = 该子项非空回答人数）：",
        "",
        "| 子项 | " + " | ".join(_md_escape(o) for o in opt_order) + " | 回答人数 |",
        "|" + "|".join(["---"] * (len(opt_order) + 2)) + "|",
    ]
    for m in members:
        row_label = m.get("matrix_row") or _safe_header(headers, m["index"])
        vals = [v for v in _column_values(body, m["index"]) if v.strip()]
        denom = len(vals)
        opt_counts: Counter = Counter()
        for v in vals:
            for o in set(_split_by_vocab(v, shared_options, delimiter, norm)):
                if o:
                    opt_counts[o] += 1
        cells = [_md_escape(row_label)]
        for o in opt_order:
            c = opt_counts.get(o, 0)
            cells.append(f"{c} ({_pct(c, denom)})" if denom else "0")
        cells.append(str(denom))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("> （矩阵题 × 画像维度的交叉分析本期暂未提供）")
    return "\n".join(lines)


def _safe_header(headers: list[str], idx: int) -> str:
    if idx < 0 or idx >= len(headers):
        return f"列{idx}"
    h = (headers[idx] or "").strip()
    return h or f"列{idx}"


# ============================================================================
# 数字漂移检查（writer 输出后置告警用）
# ============================================================================


_NUMBER_RE = _re.compile(r"\d+(?:\.\d+)?%?")


def find_numbers_not_in_stats(report_md: str, stats_md: str) -> list[str]:
    """从 report_md 抽出所有数字 token（含 %），返回不在 stats_md 里的那些。

    给 main.py 用作"writer 是否乱编数字"的后置告警，**不阻断流程**，只打日志。
    """
    in_stats = set(_NUMBER_RE.findall(stats_md))
    drifted: list[str] = []
    for tok in _NUMBER_RE.finditer(report_md):
        s = tok.group(0)
        if s not in in_stats:
            if len(s) <= 1:
                continue
            drifted.append(s)
    seen: set[str] = set()
    uniq: list[str] = []
    for x in drifted:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


# ============================================================================
# 报告比较关系一致性校验
# ============================================================================


_COMPARISON_ENTITY_RE = _re.compile(
    r"(?:概念|方案)\s*[0-9一二三四五六七八九十]+|concept\s*[0-9]+",
    _re.IGNORECASE,
)
_HIGH_WORDS = ("最高", "最大", "最多")
_LOW_WORDS = ("最低", "最小", "最少")
_HIGH_RELATIONS = ("高于", "超过", "大于")
_LOW_RELATIONS = ("低于", "不及", "小于")
_ORDER_WORDS = ("排序", "依次", "从高到低", "由高到低", "从低到高", "由低到高")
_TIE_WORDS = ("并列", "持平", "相同")
_STATISTICAL_CUES = ("均值", "平均", "满意度", "排序", "排名", "名次")
_CHINESE_RANKS = {"一": 1, "二": 2, "三": 3, "1": 1, "2": 2, "3": 3}


def _comparison_normalize(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _comparison_entity(name: str) -> tuple[str, list[str], str]:
    text = str(name or "").strip()
    match = _COMPARISON_ENTITY_RE.search(text)
    entity = match.group(0).replace(" ", "") if match else text
    metric = _COMPARISON_ENTITY_RE.sub("", text).strip(" -_｜|：:")
    aliases = [entity]
    if text and text not in aliases:
        aliases.append(text)
    return entity, aliases, metric


def _scale_mean(body: list[list], index: int) -> float | None:
    values: list[float] = []
    for row in body:
        if index >= len(row):
            continue
        try:
            values.append(float(str(row[index]).strip()))
        except (TypeError, ValueError):
            continue
    return statistics.mean(values) if values else None


def _comparison_rows_for_column(
    body: list[list],
    plan: dict,
    column_index: int,
) -> list[list] | None:
    """Use the same Part eligibility scope as the rendered deterministic stats."""
    matching_parts = [
        part
        for part in (plan.get("parts") or [])
        if column_index in (part.get("column_indexes") or [])
    ]
    if not matching_parts:
        return body
    if len(matching_parts) > 1:
        filters = [part.get("filter") for part in matching_parts]
        if any(item != filters[0] for item in filters[1:]):
            return None
    columns_by_index = {
        column["index"]: column
        for column in (plan.get("columns") or [])
        if isinstance(column, dict) and isinstance(column.get("index"), int)
    }
    return _filter_rows_for_part(body, matching_parts[0], columns_by_index)


def build_comparison_fact_catalog(rows: list[list], plan: dict) -> list[dict]:
    """从原始行与分析方案构造可确定比较的量表均值事实。"""
    if not rows or len(rows) <= 1 or not isinstance(plan, dict):
        return []
    headers = rows[0]
    body = rows[1:]
    scale_columns = [
        column
        for column in (plan.get("columns") or [])
        if isinstance(column, dict)
        and column.get("role") == "scale"
        and isinstance(column.get("index"), int)
        and 0 <= column["index"] < len(headers)
    ]
    header_counts: Counter = Counter(
        _comparison_normalize(headers[column["index"]]) for column in scale_columns
    )
    groups: dict[tuple, list[dict]] = {}
    for column in scale_columns:
        index = column["index"]
        name = str(column.get("name") or _safe_header(headers, index)).strip()
        entity, aliases, metric = _comparison_entity(name)
        raw_header = _comparison_normalize(headers[index])
        semantic_family = _comparison_normalize(_COMPARISON_ENTITY_RE.sub("#", name))
        family = (
            ("header", raw_header)
            if raw_header and header_counts[raw_header] > 1
            else ("semantic", semantic_family)
        )
        key = (family, column.get("min"), column.get("max"))
        scoped_body = _comparison_rows_for_column(body, plan, index)
        if scoped_body is None:
            continue
        mean = _scale_mean(scoped_body, index)
        if mean is None:
            continue
        groups.setdefault(key, []).append({
            "column_index": index,
            "entity": entity,
            "aliases": aliases,
            "name": name,
            "metric": metric or str(headers[index] or "量表均值").strip(),
            "value": mean,
            "sample_size": len(scoped_body),
        })

    catalog: list[dict] = []
    for group_index, members in enumerate(groups.values(), 1):
        entities = [member["entity"] for member in members]
        if len(members) < 2 or len(set(entities)) != len(entities):
            continue
        metrics = [member["metric"] for member in members if member["metric"]]
        metric = metrics[0] if metrics and len(set(metrics)) == 1 else "量表均值"
        descending = sorted(members, key=lambda item: (-item["value"], item["entity"]))
        ascending = sorted(members, key=lambda item: (item["value"], item["entity"]))
        rank_by_entity: dict[str, int] = {}
        previous_value: float | None = None
        current_rank = 0
        for position, member in enumerate(descending, 1):
            if previous_value is None or not _values_equal(member["value"], previous_value):
                current_rank = position
                previous_value = member["value"]
            rank_by_entity[member["entity"]] = current_rank
        catalog.append({
            "group_id": f"scale_mean_{group_index}",
            "metric": metric,
            "members": members,
            "descending": [member["entity"] for member in descending],
            "ascending": [member["entity"] for member in ascending],
            "rank_by_entity": rank_by_entity,
            "max_value": descending[0]["value"],
            "min_value": ascending[0]["value"],
        })
    return catalog


def _values_equal(left: float, right: float, tolerance: float = 0.005) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def _sentence_spans(text: str) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    start = 0
    for match in _re.finditer(r"[。！？!?](?:[\"”’）)])?|\n+", text):
        end = match.end()
        raw = text[start:end]
        leading = len(raw) - len(raw.lstrip())
        trailing = len(raw.rstrip())
        if raw.strip():
            spans.append((start + leading, start + trailing, raw.strip()))
        start = end
    raw = text[start:]
    if raw.strip():
        leading = len(raw) - len(raw.lstrip())
        spans.append((start + leading, len(text.rstrip()), raw.strip()))
    return spans


def _entity_mentions(sentence: str, group: dict) -> list[tuple[int, str]]:
    mentions: list[tuple[int, str]] = []
    for member in group["members"]:
        positions = []
        for alias in sorted(member["aliases"], key=len, reverse=True):
            match = _re.search(
                _alias_pattern(alias, member["entity"]),
                sentence,
                _re.IGNORECASE,
            )
            if match:
                positions.append(match.start())
        if positions:
            mentions.append((min(positions), member["entity"]))
    mentions.sort()
    return mentions


def _entity_pattern(member: dict) -> str:
    return "(?:" + "|".join(
        _alias_pattern(alias, member["entity"])
        for alias in sorted(member["aliases"], key=len, reverse=True)
    ) + ")"


def _alias_pattern(alias: str, entity: str) -> str:
    pattern = _re.escape(str(alias or ""))
    if _comparison_normalize(alias) == _comparison_normalize(entity):
        pattern += r"(?![0-9一二三四五六七八九十])"
    return pattern


def _claimed_extreme_entity(sentence: str, group: dict, words: tuple[str, ...]) -> str | None:
    word_pattern = "(?:" + "|".join(_re.escape(word) for word in words) + ")"
    word_match = _re.search(word_pattern, sentence, _re.IGNORECASE)
    if not word_match:
        return None
    mentions = _entity_mentions(sentence, group)
    after = [
        (position, entity)
        for position, entity in mentions
        if word_match.end() <= position <= word_match.end() + 16
    ]
    if after:
        return min(after)[1]
    before = [
        (position, entity)
        for position, entity in mentions
        if 0 <= word_match.start() - position <= 80
    ]
    if before:
        return max(before)[1]
    return mentions[0][1] if len(mentions) == 1 else None


def _comparison_context(text: str, start: int, end: int, width: int = 180) -> tuple[str, str]:
    return text[max(0, start - width):start].strip(), text[end:min(len(text), end + width)].strip()


def _has_comparison_relation(sentence: str) -> bool:
    return any(
        word in sentence
        for word in (
            *_HIGH_WORDS,
            *_LOW_WORDS,
            *_HIGH_RELATIONS,
            *_LOW_RELATIONS,
            *_ORDER_WORDS,
            *_TIE_WORDS,
        )
    ) or bool(_re.search(r"(?:排名|位列|排在)\s*第?[一二三123]", sentence))


def _comparison_group_score(sentence: str, group: dict) -> int:
    folded = sentence.casefold()
    score = 0
    metric = str(group.get("metric") or "").strip().casefold()
    if metric and metric != "量表均值" and metric in folded:
        score += 4
    for member in group.get("members") or []:
        entity = str(member.get("entity") or "").casefold()
        if any(
            str(alias or "").casefold() in folded
            for alias in member.get("aliases") or []
            if str(alias or "").casefold() != entity
        ):
            score += 2
            break
    return score


def analyze_comparison_claims(report_md: str, catalog: list[dict]) -> dict:
    """确定性检查量表均值的极值、两两关系、排序与名次陈述。"""
    text = str(report_md or "")
    checked_claim_count = 0
    issues: list[dict] = []
    for start, end, sentence in _sentence_spans(text):
        if sentence.lstrip().startswith(("#", "|")):
            continue
        if not any(cue in sentence for cue in _STATISTICAL_CUES):
            continue
        candidate_groups = [
            group for group in catalog
            if _entity_mentions(sentence, group)
        ]
        if len(candidate_groups) > 1:
            scores = [_comparison_group_score(sentence, group) for group in candidate_groups]
            best_score = max(scores)
            best_groups = [
                group for group, score in zip(candidate_groups, scores)
                if score == best_score
            ]
            if best_score > 0 and len(best_groups) == 1:
                candidate_groups = best_groups
            elif _has_comparison_relation(sentence):
                checked_claim_count += 1
                before, after = _comparison_context(text, start, end)
                issues.append({
                    "claim_id": "",
                    "group_id": "ambiguous_scale_group",
                    "metric": "量表均值",
                    "start": start,
                    "end": end,
                    "original_sentence": sentence,
                    "context_before": before,
                    "context_after": after,
                    "reasons": ["比较表述无法唯一绑定到一个确定性量表组"],
                    "expected_order": [],
                    "repairable": False,
                })
                continue
        for group in candidate_groups:
            mentions = _entity_mentions(sentence, group)
            has_scale_cue = any(cue in sentence for cue in ("均值", "平均", "满意度"))
            has_relation = _has_comparison_relation(sentence)
            if not has_relation:
                continue
            violations: list[str] = []
            repairable = has_scale_cue
            checked_claim_count += 1
            if not has_scale_cue:
                violations.append("比较表述未明确绑定到量表均值口径")
            else:
                highest = _claimed_extreme_entity(sentence, group, _HIGH_WORDS)
                if highest:
                    member = next(item for item in group["members"] if item["entity"] == highest)
                    if not _values_equal(member["value"], group["max_value"]):
                        violations.append(f"{highest}并非{group['metric']}最高项")
                    elif "唯一" in sentence:
                        tied = [
                            item for item in group["members"]
                            if _values_equal(item["value"], group["max_value"])
                        ]
                        if len(tied) > 1:
                            violations.append(f"{highest}并非唯一最高项")
                lowest = _claimed_extreme_entity(sentence, group, _LOW_WORDS)
                if lowest:
                    member = next(item for item in group["members"] if item["entity"] == lowest)
                    if not _values_equal(member["value"], group["min_value"]):
                        violations.append(f"{lowest}并非{group['metric']}最低项")
                    elif "唯一" in sentence:
                        tied = [
                            item for item in group["members"]
                            if _values_equal(item["value"], group["min_value"])
                        ]
                        if len(tied) > 1:
                            violations.append(f"{lowest}并非唯一最低项")

                for left in group["members"]:
                    for right in group["members"]:
                        if left is right:
                            continue
                        left_pattern = _entity_pattern(left)
                        right_pattern = _entity_pattern(right)
                        high_pattern = "(?:" + "|".join(_HIGH_RELATIONS) + ")"
                        low_pattern = "(?:" + "|".join(_LOW_RELATIONS) + ")"
                        if _re.search(
                            rf"{left_pattern}.{{0,80}}?{high_pattern}.{{0,40}}?{right_pattern}",
                            sentence,
                            _re.IGNORECASE,
                        ) and not left["value"] > right["value"] + 0.005:
                            violations.append(f"{left['entity']}并不高于{right['entity']}")
                        if _re.search(
                            rf"{left_pattern}.{{0,80}}?{low_pattern}.{{0,40}}?{right_pattern}",
                            sentence,
                            _re.IGNORECASE,
                        ) and not left["value"] < right["value"] - 0.005:
                            violations.append(f"{left['entity']}并不低于{right['entity']}")

                if any(word in sentence for word in _TIE_WORDS) and len(mentions) >= 2:
                    mentioned_members = [
                        next(item for item in group["members"] if item["entity"] == entity)
                        for _, entity in mentions
                    ]
                    tie_rank_match = _re.search(
                        r"并列(?:排名|位列|排在)?\s*第?([一二三123])",
                        sentence,
                    )
                    if tie_rank_match:
                        claimed_rank = _CHINESE_RANKS[tie_rank_match.group(1)]
                        if any(
                            group["rank_by_entity"][member["entity"]] != claimed_rank
                            for member in mentioned_members
                        ):
                            violations.append("正文中的并列名次与确定性统计排序不一致")
                    elif any(word in sentence for word in _HIGH_WORDS):
                        if any(
                            not _values_equal(member["value"], group["max_value"])
                            for member in mentioned_members
                        ):
                            violations.append("正文声称并列最高的项目并未共同处于最高值")
                    elif any(word in sentence for word in _LOW_WORDS):
                        if any(
                            not _values_equal(member["value"], group["min_value"])
                            for member in mentioned_members
                        ):
                            violations.append("正文声称并列最低的项目并未共同处于最低值")
                    elif any(
                        not _values_equal(mentioned_members[0]["value"], member["value"])
                        for member in mentioned_members[1:]
                    ):
                        violations.append("正文声称持平的项目均值并不相同")

                if any(word in sentence for word in _ORDER_WORDS) and len(mentions) >= 2:
                    claimed = [entity for _, entity in mentions]
                    expected_source = (
                        group["ascending"]
                        if any(word in sentence for word in ("从低到高", "由低到高"))
                        else group["descending"]
                    )
                    expected = [entity for entity in expected_source if entity in claimed]
                    if claimed != expected:
                        violations.append("正文中的顺序与确定性统计排序不一致")

                for member in group["members"]:
                    member_pattern = _entity_pattern(member)
                    rank_match = _re.search(
                        rf"{member_pattern}.{{0,28}}?(?:排名|位列|排在)\s*第?([一二三123])",
                        sentence,
                        _re.IGNORECASE,
                    )
                    if not rank_match:
                        continue
                    claimed_rank = _CHINESE_RANKS[rank_match.group(1)]
                    if group["rank_by_entity"][member["entity"]] != claimed_rank:
                        violations.append(
                            f"{member['entity']}的名次不是第{claimed_rank}"
                        )

            if not violations:
                continue
            before, after = _comparison_context(text, start, end)
            expected_order = [
                {
                    "entity": entity,
                    "value": next(
                        member["value"] for member in group["members"]
                        if member["entity"] == entity
                    ),
                }
                for entity in group["descending"]
            ]
            issues.append({
                "claim_id": "",
                "group_id": group["group_id"],
                "metric": group["metric"],
                "start": start,
                "end": end,
                "original_sentence": sentence,
                "context_before": before,
                "context_after": after,
                "reasons": list(dict.fromkeys(violations)),
                "expected_order": expected_order,
                "repairable": repairable,
            })

    deduplicated: list[dict] = []
    seen: set[tuple[int, int, str]] = set()
    for issue in issues:
        key = (issue["start"], issue["end"], issue["group_id"])
        if key in seen:
            continue
        seen.add(key)
        issue["claim_id"] = f"C{len(deduplicated) + 1:03d}"
        deduplicated.append(issue)
    return {
        "checked_claim_count": checked_claim_count,
        "issues": deduplicated,
    }
