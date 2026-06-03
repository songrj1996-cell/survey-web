"""问卷数据标注模块

提供两个独立于报告生成流程之外的标注功能：
1. AI 作答识别 (ai_detect)：判断受访者是否使用 AI 填写主观题
2. 回答质量打标 (quality)：为每道主观题和每位受访者整体打 无效/普通/优秀 标签

两个功能可独立使用，也可组合（先 AI 识别，用户确认后再打标）。
"""

import io
import json
import re
from typing import Optional

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

# 分批大小
AI_DETECT_BATCH = 10
QUALITY_BATCH = 150
AI_DETECT_MAX_CELL_CHARS = 420

# 标注列样式
_YELLOW_FILL  = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
_GRAY_FILL    = PatternFill(start_color="D9D9D9", end_color="D9D9D9", fill_type="solid")
_HEADER_FILL  = PatternFill(start_color="FFE699", end_color="FFE699", fill_type="solid")
_BOLD_FONT    = Font(bold=True)

# ============================================================
# 列检测
# ============================================================

def detect_id_column(headers: list[str], rows: list[list]) -> int:
    """检测玩家 ID 列。
    优先匹配常见 ID 关键词，找不到时默认第二列（index 1），与用户 prompt 约定一致。
    """
    for i, h in enumerate(headers):
        hl = h.lower().strip()
        if any(kw in hl for kw in [
            "discordid", "discord_id", "discord", "whatsapp",
            "mlbbid", "mlbb_id", "player_id", "playerid",
            "玩家id", "用户id", "respondent",
        ]):
            return i
    return min(1, len(headers) - 1)


def detect_open_text_columns(rows: list[list], headers: list[str]) -> list[int]:
    """检测主观题（开放题）列，逻辑与 server.py 的 _heuristic_type 保持一致。"""
    if len(rows) <= 1:
        return []
    body = rows[1:]
    result = []
    for i, header in enumerate(headers):
        h = header.lower().strip()
        # 跳过时间戳等
        if any(kw in h for kw in ["时间", "timestamp", "submit", "提交", "date", "日期"]):
            continue
        vals = [str(r[i]) if i < len(r) else "" for r in body]
        non_empty = [v.strip() for v in vals if v.strip()]
        if not non_empty:
            continue
        total = len(non_empty)
        # 纯数字列 → 量表
        nums = sum(1 for v in non_empty if _try_float(v))
        if nums / total > 0.85:
            continue
        # 多选题（分隔符）
        delim_count = sum(1 for v in non_empty if any(d in v for d in [",", "，", ";", "；", "、", "|"]))
        if delim_count / total > 0.25:
            continue
        # 唯一值少 → 选择题
        unique_vals = set(non_empty)
        if len(unique_vals) <= 8 or len(unique_vals) / total < 0.25:
            continue
        # 长文本 → 开放题
        avg_len = sum(len(v) for v in non_empty) / total
        if avg_len > 25:
            result.append(i)
    return result


def _try_float(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


# ============================================================
# 格式化工具
# ============================================================

def _rows_to_md_table(
    batch_rows: list[list],
    headers: list[str],
    col_indexes: list[int],
    max_cell_chars: int | None = None,
) -> str:
    """把选定列格式化为 Markdown 表格。"""
    sel_headers = [headers[i] if i < len(headers) else f"列{i}" for i in col_indexes]

    def esc(s: str) -> str:
        text = str(s).replace("|", "\\|").replace("\n", " ").strip()
        if max_cell_chars and len(text) > max_cell_chars:
            return text[:max_cell_chars].rstrip() + "…（已截断）"
        return text

    lines = ["| " + " | ".join(esc(h) for h in sel_headers) + " |"]
    lines.append("| " + " | ".join(["---"] * len(sel_headers)) + " |")
    for row in batch_rows:
        cells = [str(row[i]) if i < len(row) else "" for i in col_indexes]
        lines.append("| " + " | ".join(esc(c) for c in cells) + " |")
    return "\n".join(lines)


# ============================================================
# Query 构建
# ============================================================

_AI_DETECT_TMPL = """\
你是一名专业的用研分析师，负责帮 MLBB 游戏用户运营团队识别问卷中疑似由 AI 代为填写的玩家回答。

{background_block}以下是第 {batch_num} 批次（共 {total} 名玩家）的主观题回答数据。\
表格第一列为玩家唯一标识 ID，后续各列为主观题内容（列名后括号内为本系统的列编号）：

{table}

**分析任务**：
1. 以单个玩家为单位，综合其所有主观题回答判断 AI 作答概率（0–100 整数）。
2. AI 作答概率核心判断标准：
   - 观点是自己的、仅用 AI 润色结构 → ai_prob 低，is_polished = "high"
   - 内容有大量个人游戏细节、但结构明显被 AI 整理 → ai_prob 低，is_polished = "high"
   - 语法纠正软件 → ai_prob 低，is_polished = "high"
   - 观点和内容均由 AI 生成（无个人细节，泛泛而谈）→ ai_prob 高
3. 必须对全部 {total} 名玩家进行判断，不可跳过任何一行。
4. 将每位玩家的每道主观题回答翻译为中文（原文已是中文则原样返回，不加任何提示前缀）。

**输出要求**：只输出一段 ```json``` 围栏，数组顺序必须与输入数据行顺序完全一致：
```json
[
  {{
    "id": "玩家唯一标识（与输入第一列值完全一致）",
    "ai_prob": 85,
    "is_polished": "high",
    "reason": "判断理由（不超过 100 字）",
    "evidence": "最具代表性的原文摘录（不超过 150 字）",
    "translations": {{
      {col_keys_example}: "该列回答的中文译文"
    }}
  }}
]
```
`is_polished` 只取 `"high"` / `"medium"` / `"low"` 三个值。\
`translations` 的 key 格式为 `"col_列号"`（示例：`"col_3"`）。\
"""

_QUALITY_TMPL = """\
你是一名专业的用研分析师，负责帮 MLBB 游戏用户运营团队对问卷主观题回答进行质量打标。

以下是第 {batch_num} 批次（共 {total} 名玩家）的主观题回答数据。\
表格第一列为玩家唯一标识 ID，后续各列为主观题（{col_desc}）：

{table}

**质量打标标准**：
- **无效反馈**：仅提供观点没有原因；简短一句话喜欢/不喜欢；纯抱怨或纯夸奖。
- **优秀反馈**：提供了观点、得出观点的原因，并佐以具体实际案例。
- **普通反馈**：不属于以上两类，通常包含观点和原因但描述不详细，或没有具体案例。
- 某道题回答为空 → 标为 `"N/A"`。

**整体打标规则（overall）**：
- 无效反馈超过一半 → `"无效反馈"`
- 优秀反馈超过一半 → `"优秀反馈"`
- 其他 → `"普通反馈"`

**翻译要求**：将每位玩家每道主观题回答翻译为中文（原文已是中文则原样返回，不加任何提示前缀）。

**输出要求**：只输出一段 ```json``` 围栏，数组顺序必须与输入数据行顺序完全一致：
```json
[
  {{
    "id": "玩家唯一标识（与输入第一列值完全一致）",
    "q_labels": {{
      {col_keys_example}: "无效反馈"
    }},
    "overall": "普通反馈",
    "translations": {{
      {col_keys_example}: "该列回答的中文译文"
    }}
  }}
]
```
`q_labels` 和 `translations` 的 key 格式为 `"col_列号"`（示例：`"col_3"`）。\
`q_labels` 的值只取 `"无效反馈"` / `"普通反馈"` / `"优秀反馈"` / `"N/A"` 四种。\
"""


def build_ai_detect_query(
    batch_rows: list[list],
    headers: list[str],
    open_text_cols: list[int],
    id_col: int,
    batch_num: int | str = 1,
    background: str = "",
) -> str:
    """构建 AI 检测 Dify 查询。"""
    cols = [id_col] + [c for c in open_text_cols if c != id_col]
    table = _rows_to_md_table(batch_rows, headers, cols, max_cell_chars=AI_DETECT_MAX_CELL_CHARS)
    col_keys_example = ", ".join(f'"col_{c}": "..."' for c in open_text_cols[:3])
    if len(open_text_cols) > 3:
        col_keys_example += ", ..."
    bg_block = f"**调研背景参考**：{background.strip()}\n\n" if background.strip() else ""
    return _AI_DETECT_TMPL.format(
        background_block=bg_block,
        batch_num=batch_num,
        total=len(batch_rows),
        table=table,
        col_keys_example=col_keys_example,
    )


def build_quality_label_query(
    batch_rows: list[list],
    headers: list[str],
    open_text_cols: list[int],
    id_col: int,
    batch_num: int = 1,
) -> str:
    """构建质量打标 Dify 查询。"""
    cols = [id_col] + [c for c in open_text_cols if c != id_col]
    table = _rows_to_md_table(batch_rows, headers, cols)
    col_desc = "、".join(
        f"「{headers[c] if c < len(headers) else f'列{c}'}」(col_{c})"
        for c in open_text_cols
    )
    col_keys_example = ", ".join(f'"col_{c}": "..."' for c in open_text_cols[:3])
    if len(open_text_cols) > 3:
        col_keys_example += ", ..."
    return _QUALITY_TMPL.format(
        batch_num=batch_num,
        total=len(batch_rows),
        col_desc=col_desc,
        table=table,
        col_keys_example=col_keys_example,
    )


# ============================================================
# 结果解析
# ============================================================

def _repair_json_quotes(text: str) -> str:
    """将 JSON 字符串值内部的裸双引号转义为 \\\"。
    判断依据：当前 " 不是被 \\ 转义的，且下一个非空白字符不是 JSON 结构符（: , } ] \\n \\r），
    则认为它是值内部的裸引号而非字符串结束符。
    """
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_str:
            if c == '\\' and i + 1 < len(text):
                out.append(c)
                out.append(text[i + 1])
                i += 2
                continue
            elif c == '"':
                j = i + 1
                while j < len(text) and text[j] in ' \t':
                    j += 1
                nxt = text[j] if j < len(text) else ''
                if nxt in ':,}]\n\r':
                    out.append('"')
                    in_str = False
                else:
                    out.append('\\"')
            else:
                out.append(c)
        else:
            out.append(c)
            if c == '"':
                in_str = True
        i += 1
    return ''.join(out)


def _extract_json_array(text: str) -> Optional[list]:
    """从 LLM 输出中提取 JSON 数组，兼容 ```json 围栏，容忍字符串内裸双引号。"""
    def _try_parse(raw: str) -> Optional[list]:
        try:
            result = json.loads(raw)
            return result if isinstance(result, list) else None
        except json.JSONDecodeError:
            pass
        try:
            result = json.loads(_repair_json_quotes(raw))
            return result if isinstance(result, list) else None
        except json.JSONDecodeError:
            pass
        return None

    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        result = _try_parse(m.group(1))
        if result is not None:
            return result
    # 尝试裸数组（贪婪）
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        result = _try_parse(m.group(0))
        if result is not None:
            return result
    return None


def parse_ai_detect_result(llm_output: str) -> tuple[list[dict], str]:
    """解析 AI 检测结果。
    Returns: (results, error_msg) — results 为空列表表示解析失败。
    每条结果：{id, ai_prob, is_polished, reason, evidence, translations}
    """
    arr = _extract_json_array(llm_output)
    if arr is None:
        return [], "无法从输出中提取 JSON 数组"
    results = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        results.append({
            "id":           str(item.get("id", "")),
            "ai_prob":      _safe_int(item.get("ai_prob", 0), 0, 100),
            "is_polished":  str(item.get("is_polished", "low")),
            "reason":       str(item.get("reason", "")),
            "evidence":     str(item.get("evidence", "")),
            "translations": dict(item.get("translations") or {}),
        })
    return results, ""


def parse_quality_result(llm_output: str) -> tuple[list[dict], str]:
    """解析质量打标结果。
    Returns: (results, error_msg)
    每条结果：{id, q_labels, overall, translations}
    """
    arr = _extract_json_array(llm_output)
    if arr is None:
        return [], "无法从输出中提取 JSON 数组"
    results = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        results.append({
            "id":           str(item.get("id", "")),
            "q_labels":     dict(item.get("q_labels") or {}),
            "overall":      str(item.get("overall", "")),
            "translations": dict(item.get("translations") or {}),
        })
    return results, ""


def _safe_int(val, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(val)))
    except (TypeError, ValueError):
        return lo


# ============================================================
# Excel 生成
# ============================================================

def generate_annotated_excel(
    rows: list[list],
    headers: list[str],
    ai_results: list[dict],
    confirmed_ai_ids: set[str],
    quality_results: list[dict],
    open_text_cols: list[int],
    id_col: int,
    tasks: dict,
) -> bytes:
    """生成标注后的 Excel 文件。

    列顺序：
    - AI 检测时：[AI作答标注, AI作答概率, AI润色程度, 整体回答质量(可选), ...原始列]
    - 质量打标时：[整体回答质量, ...原始列（每道主观题前插入该题质量标注列）]
    - 组合：[AI作答标注, AI作答概率, AI润色程度, 整体回答质量, ...原始列（主观题前有质量标注）]

    所有单元格使用翻译后的中文内容（仅主观题列有翻译）。
    """
    do_ai = tasks.get("ai_detect", False)
    do_quality = tasks.get("quality", False)

    # 建立 ID → 标注结果 的快速查找
    id_to_ai:      dict[str, dict] = {r["id"]: r for r in ai_results}
    id_to_quality: dict[str, dict] = {r["id"]: r for r in quality_results}
    open_text_set = set(open_text_cols)

    # ── 构建表头 ──────────────────────────────────────────────
    # 前置标注列
    prefix_headers: list[str] = []
    if do_ai:
        prefix_headers += ["AI作答标注", "AI作答概率", "AI润色程度"]
    if do_quality:
        prefix_headers.append("整体回答质量")

    # 原始列规格（list of dict: {header, original_col_idx, is_label_col, label_for_col}）
    col_spec: list[dict] = []
    for i, h in enumerate(headers):
        if do_quality and i in open_text_set:
            # 主观题前插入质量标注列
            col_spec.append({
                "type": "quality_label",
                "header": f"[{h}]质量标注",
                "original_col_idx": i,
            })
        col_spec.append({
            "type": "original",
            "header": h,
            "original_col_idx": i,
        })

    full_headers = prefix_headers + [s["header"] for s in col_spec]

    # ── 创建工作簿 ─────────────────────────────────────────────
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "标注结果"

    # 写表头
    ws.append(full_headers)
    header_row = ws[1]
    for cell in header_row:
        val = cell.value or ""
        is_annotation = (
            val in ("AI作答标注", "AI作答概率", "AI润色程度", "整体回答质量")
            or val.endswith("质量标注")
        )
        if is_annotation:
            cell.fill = _HEADER_FILL
            cell.font = _BOLD_FONT
        else:
            cell.font = _BOLD_FONT

    # ── 写数据行 ───────────────────────────────────────────────
    body = rows[1:] if len(rows) > 1 else []
    for row_data in body:
        row_id = str(row_data[id_col]).strip() if id_col < len(row_data) else ""
        ai_info      = id_to_ai.get(row_id, {})
        quality_info = id_to_quality.get(row_id, {})
        is_ai        = row_id in confirmed_ai_ids

        # 合并翻译（优先 quality，回退 ai）
        translations: dict[str, str] = {}
        if ai_info.get("translations"):
            translations.update(ai_info["translations"])
        if quality_info.get("translations"):
            translations.update(quality_info["translations"])

        new_row: list = []

        # 前置标注列
        if do_ai:
            if is_ai:
                new_row.append("AI作答")
                new_row.append(ai_info.get("ai_prob", ""))
            else:
                prob = ai_info.get("ai_prob", "")
                new_row.append(f"{prob}%" if prob != "" else "")
                new_row.append(prob)
            new_row.append(ai_info.get("is_polished", "") if ai_info else "")

        if do_quality:
            if is_ai:
                new_row.append("")
            else:
                new_row.append(quality_info.get("overall", ""))

        # 原始列（含插入的质量标注列）
        for spec in col_spec:
            col_idx = spec["original_col_idx"]
            if spec["type"] == "quality_label":
                if is_ai:
                    new_row.append("")
                else:
                    new_row.append(quality_info.get("q_labels", {}).get(f"col_{col_idx}", ""))
            else:
                orig_val = str(row_data[col_idx]) if col_idx < len(row_data) else ""
                translated = translations.get(f"col_{col_idx}", "")
                new_row.append(translated if translated else orig_val)

        ws.append(new_row)
        row_num = ws.max_row

        # 标注列 → 黄色背景
        n_prefix = len(prefix_headers)
        for j in range(1, n_prefix + 1):
            ws.cell(row=row_num, column=j).fill = _YELLOW_FILL

        # 质量标注插入列 → 黄色背景
        col_cursor = n_prefix + 1
        for spec in col_spec:
            cell = ws.cell(row=row_num, column=col_cursor)
            if spec["type"] == "quality_label":
                cell.fill = _YELLOW_FILL
            col_cursor += 1

        # AI 作答行 → 整行灰色
        if is_ai:
            for j in range(1, len(full_headers) + 1):
                ws.cell(row=row_num, column=j).fill = _GRAY_FILL

    # ── 列宽 ──────────────────────────────────────────────────
    for col_num, h in enumerate(full_headers, 1):
        col_letter = openpyxl.utils.get_column_letter(col_num)
        if h in ("AI作答标注", "AI作答概率", "AI润色程度", "整体回答质量"):
            ws.column_dimensions[col_letter].width = 14
        elif h.endswith("质量标注"):
            ws.column_dimensions[col_letter].width = 14
        else:
            ws.column_dimensions[col_letter].width = 28

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
