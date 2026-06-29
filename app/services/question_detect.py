"""services/question_detect:题型识别(本地启发式 + Google Form 矩阵分组 + 题型识别问询构建)。

纯逻辑:给定 rows/headers,推断每列题型、构建发给 LLM 的题型识别 query。被 survey 与 annotate 共用。
"""
import re


ROLE_LABEL_MAP = {
    "id":            "用户ID",
    "mlbbid":        "MLBB ID",
    "profile_dim":   "画像维度",
    "single_choice": "单选题",
    "multi_choice":  "多选题",
    "scale":         "量表题",
    "matrix_scale":  "矩阵打分",
    "matrix_multi":  "矩阵多选",
    "open_text":     "开放题",
    "ignore":        "忽略此列",
}

LABEL_ROLE_MAP = {v: k for k, v in ROLE_LABEL_MAP.items()}


def _heuristic_type(header: str, values: list[str]) -> str:
    h = header.lower().strip()

    # ── ID / 忽略 ──
    if any(kw in h for kw in ["_id", "userid", "player", "玩家id", "用户id", "编号"]):
        return "id"
    if re.match(r"^(id|uid|uuid)$", h):
        return "id"
    if any(kw in h for kw in ["时间", "timestamp", "submit", "提交", "date", "日期"]):
        return "ignore"

    non_empty = [v.strip() for v in values if v.strip()]
    if not non_empty:
        return "open_text"
    total = len(non_empty)

    # ── 数值型 → 量表 ──
    nums = []
    for v in non_empty:
        try:
            nums.append(float(v))
        except ValueError:
            pass
    if len(nums) / total > 0.85:
        mn, mx = min(nums), max(nums)
        if mx - mn <= 15 and mn >= 0:
            return "scale"

    # ── 长文本优先判断（避免后续规则误伤）──
    avg_len = sum(len(v) for v in non_empty) / total
    if avg_len > 25:
        return "open_text"

    # ── 多选（分隔符检测：只认短片段列表，排除句子中的标点）──
    delimiters = [",", "，", ";", "；", "、", "|"]
    def _is_list(v: str) -> bool:
        for d in delimiters:
            if d in v:
                parts = [p.strip() for p in v.split(d) if p.strip()]
                if len(parts) >= 2 and all(len(p) < 30 for p in parts):
                    return True
        return False
    delim_count = sum(1 for v in non_empty if _is_list(v))
    if delim_count / total > 0.25:
        return "multi_choice"

    # ── 唯一值数量 ──
    unique_vals = set(non_empty)
    n_unique = len(unique_vals)
    ratio = n_unique / total

    if n_unique <= 2:
        return "single_choice"
    if n_unique <= 8:
        return "single_choice"
    if ratio < 0.25:
        return "single_choice"

    return "single_choice"


# ============================================================
# Google Form 矩阵题分组 + LLM 题型识别
# ============================================================

# `主问题 [子项]` 或 `主问题（子项）`：Google Form 矩阵题导出格式
_MATRIX_HEADER_RE = re.compile(r"^(.*?)\s*[\[\(（](.+?)[\]\)）]\s*$")


def _group_googleform_matrix(headers: list[str]) -> list[dict]:
    """识别 `主问题 [子项]` 多列 → 合并为逻辑题。

    返回逻辑题分组（保持原列顺序，矩阵组落在其首列位置）：
      {"type": "matrix"|"single", "title", "member_indexes": [...], "row_labels": [...]}
    """
    parsed = []  # (idx, prefix|None, sub|None)
    for i, h in enumerate(headers):
        hs = (h or "").strip()
        m = _MATRIX_HEADER_RE.match(hs)
        if m and m.group(1).strip():
            parsed.append((i, m.group(1).strip(), m.group(2).strip()))
        else:
            parsed.append((i, None, None))

    prefix_cols: dict[str, list[tuple[int, str]]] = {}
    for idx, pref, sub in parsed:
        if pref is not None:
            prefix_cols.setdefault(pref, []).append((idx, sub))
    matrix_prefixes = {p for p, cols in prefix_cols.items() if len(cols) >= 2}

    groups: list[dict] = []
    emitted: set[int] = set()
    for idx, pref, _sub in parsed:
        if idx in emitted:
            continue
        if pref in matrix_prefixes:
            cols = prefix_cols[pref]
            groups.append({
                "type": "matrix",
                "title": pref,
                "member_indexes": [c[0] for c in cols],
                "row_labels": [c[1] for c in cols],
            })
            emitted.update(c[0] for c in cols)
        else:
            groups.append({
                "type": "single",
                "title": (headers[idx] or "").strip() or f"列{idx}",
                "member_indexes": [idx],
                "row_labels": [],
            })
            emitted.add(idx)
    return groups


def _detect_open_text_cols(rows: list, headers: list) -> list[int]:
    """检测主观题列，复用 _heuristic_type + 矩阵题过滤，与分析流程保持一致。"""
    if len(rows) <= 1:
        return []
    body = rows[1:]

    # 矩阵题的所有子列均为单选，先排除
    matrix_idxs: set[int] = set()
    for g in _group_googleform_matrix(headers):
        if g["type"] == "matrix":
            matrix_idxs.update(g["member_indexes"])

    result = []
    for i, header in enumerate(headers):
        if i in matrix_idxs:
            continue
        vals = [str(r[i]) if i < len(r) else "" for r in body]
        if _heuristic_type(header, vals) == "open_text":
            result.append(i)
    return result


def _col_samples(body: list[list], idx: int, n: int = 20) -> list[str]:
    out: list[str] = []
    for r in body:
        v = str(r[idx]) if idx < len(r) else ""
        if v.strip():
            out.append(v.strip())
        if len(out) >= n:
            break
    return out


_COLUMN_DETECT_SCHEMA_HINT = """\
请只输出一段 ```json``` 围栏，schema 如下（不要附加任何解释文字）：
{
  "questions": [
    {
      "name_zh": "中文题名（把英文/原文题目翻译成简洁中文）",
      "role": "single_choice|multi_choice|scale|profile_dim|open_text|id|mlbbid|matrix_scale|matrix_multi|ignore",
      "column_indexes": [列号...],            // 普通题1个；矩阵题为该题所有子项列号
      "delimiter": "，",                       // 仅 multi_choice：选项分隔符（兜底）
      "options": ["选项A","选项B"],            // 选项题清单：优先用合并后的中文标准值
      "scale_min": 1, "scale_max": 5,          // scale / matrix_scale：量程
      "rows": ["子项1","子项2"],               // matrix_*：与 column_indexes 顺序一一对应的行标签
      "value_aliases": {"中文标准值": ["原始变体1","Mythic","Mítica"]},  // 见下「同义归并」
      "low_confidence": false  // 若对该题型判断不确定（样本稀少/题名模糊），设为 true
    }
  ]
}
角色判断要点：
- 玩家ID/编号 → id；明确是 MLBB 游戏ID → mlbbid；提交时间等 → ignore
- 年龄段/段位/地区等用于分群的 → profile_dim
- 数值评分（如 1–5、1–10）→ scale
- 含多个选项、可多选 → multi_choice，并尽量给出 options 清单；若多数回答为逗号分隔的短词（如 "Classic, Ranked"），即使少数回答带括号补充说明（如 "Arcade (Such as Tide Siege)"），括号内容视为该选项的描述、不影响题型判断，仍应判断为 multi_choice
- 长文本主观回答 → open_text
- 标注【疑似矩阵题】的，按子项判断 matrix_scale（每子项打分）或 matrix_multi（每子项可多选），rows 用给出的子项标签

同义归并（value_aliases，重要）：
- 仅对 single_choice / profile_dim / multi_choice / matrix_multi 这类「选项题」给出。
- 我已在每列附上「去重取值」。请把**语义相同但写法/语种不同**的取值（如 神话/Mythic/Mítica，中国/China/CN）归并到**同一个中文标准值**：key=中文标准值，value=该标准值对应的所有原始变体（含中文标准值本身可不必重复列出）。
- options 使用这些合并后的中文标准值；中文标准值可以不直接出现在原始数据里，但必须能由 value_aliases 中的真实取值支撑。
- 只有确属同义才合并；拿不准就不要合并。没有任何同义可并的列，可以省略 value_aliases 或给 {}。
- **程度不同的选项禁止合并**：如「有点长」与「太长了」、「还好」与「很好」，虽然方向相同但程度档位不同，不是同义，必须分开。同义归并仅限写法/语种不同、语义程度完全一致的取值。
- **同一道题内必须完整归并**：若已把某语种的某个表达归入了某标准值（如把「Lebih dari 3 tahun」归入「3年以上」），则该题内同语种所有语义相同的取值也必须全部归入，不得部分遗漏（如已识别印尼语「tahun」=年，则含相同数字区间的「1~3 tahun」也须归入「1~3年」）。
- 这样统计表里这些取值会被合并计数，避免同义被拆开。\
"""


def _col_distinct(body: list[list], idx: int, n: int = 60) -> tuple[list[tuple[str, int]], int]:
    """返回某列去重取值 [(值, 频次)]（按频次降序，上限 n）+ 去重总数。"""
    from collections import Counter
    cnt: Counter = Counter()
    for r in body:
        v = (str(r[idx]) if idx < len(r) else "").strip()
        if v:
            cnt[v] += 1
    total_distinct = len(cnt)
    return cnt.most_common(n), total_distinct


def _fmt_distinct(body: list[list], idx: int, n: int = 60) -> str:
    items, total_distinct = _col_distinct(body, idx, n)
    parts = [f"{v}（{c}）" for v, c in items]
    s = " | ".join(parts)
    if total_distinct > len(items):
        s += f" …（共 {total_distinct} 种不同取值，已截断前 {len(items)}）"
    return s or "（空）"


CHOICE_ROLES = {"single_choice", "profile_dim", "multi_choice", "matrix_multi"}


def _norm_option_key(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _split_option_cell(value: str, delimiter: str | None = None) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    delims = [delimiter] if delimiter else []
    delims += ["，", ",", ";", "；", "\n", "\r\n", "|"]
    for d in delims:
        if d and d in text:
            return [p.strip() for p in text.split(d) if p.strip()]
    return [text]


def _real_options_for_question(rows: list[list], q: dict) -> list[str]:
    body = rows[1:]
    role = q.get("role")
    cis = q.get("column_indexes") or []
    delimiter = q.get("delimiter")
    out: list[str] = []
    seen: set[str] = set()
    for idx in cis:
        for r in body:
            raw = str(r[idx]) if idx < len(r) else ""
            parts = _split_option_cell(raw, delimiter) if role in ("multi_choice", "matrix_multi") else [raw.strip()]
            for part in parts:
                key = _norm_option_key(part)
                if key and key not in seen:
                    seen.add(key)
                    out.append(part)
    return out


def _sanitize_choice_options(rows: list[list], questions: list[dict]) -> list[dict]:
    """Ground choice options in real cell values while preserving canonical aliases.

    The detector may translate and merge multilingual values into a canonical label
    that does not literally appear in the raw data. Keep those labels only when
    value_aliases proves they are backed by real cell values.
    """
    for q in questions:
        role = q.get("role")
        if role not in CHOICE_ROLES:
            continue
        real_options = _real_options_for_question(rows, q)
        real_by_key = {_norm_option_key(o): o for o in real_options}
        if not real_by_key:
            continue

        raw_aliases = q.get("value_aliases") if isinstance(q.get("value_aliases"), dict) else {}
        cleaned_options: list[str] = []
        cleaned_originals: list[str] = []
        cleaned_aliases: dict[str, list[str]] = {}
        seen_canonical: set[str] = set()
        covered_real: set[str] = set()

        def real_values_for(canonical: str, aliases: list | tuple | None = None) -> list[str]:
            values = [canonical]
            if aliases:
                values.extend(aliases)
            out: list[str] = []
            seen_real: set[str] = set()
            for value in values:
                key = _norm_option_key(str(value))
                real = real_by_key.get(key)
                if real is not None and key not in seen_real:
                    seen_real.add(key)
                    out.append(real)
            return out

        def add_canonical(canonical: str, aliases: list | tuple | None = None) -> None:
            canonical = str(canonical or "").strip()
            ckey = _norm_option_key(canonical)
            if not canonical or ckey in seen_canonical:
                return
            matched = [v for v in real_values_for(canonical, aliases) if _norm_option_key(v) not in covered_real]
            if not matched:
                return
            seen_canonical.add(ckey)
            cleaned_options.append(canonical)
            cleaned_originals.append(matched[0])
            for value in matched:
                covered_real.add(_norm_option_key(value))
            if len(matched) > 1 or _norm_option_key(canonical) != _norm_option_key(matched[0]):
                cleaned_aliases[canonical] = matched

        for opt in q.get("options") or []:
            opt_text = str(opt or "").strip()
            add_canonical(opt_text, raw_aliases.get(opt_text))

        for canonical, aliases in raw_aliases.items():
            add_canonical(str(canonical), aliases if isinstance(aliases, (list, tuple)) else [])

        for opt in real_options:
            add_canonical(opt, [])

        if cleaned_options:
            q["options"] = cleaned_options
            q["options_original"] = cleaned_originals
            if cleaned_aliases:
                q["value_aliases"] = cleaned_aliases
            else:
                q.pop("value_aliases", None)
    return questions


def _build_column_detect_query(rows: list[list], groups: list[dict]) -> str:
    body = rows[1:]
    blocks: list[str] = []
    for g in groups:
        if g["type"] == "matrix":
            lines = [
                f"【疑似矩阵题】主问题: {g['title']}"
                f"（{len(g['member_indexes'])} 个子项，column_indexes={g['member_indexes']}）"
            ]
            for k, idx in enumerate(g["member_indexes"]):
                sub = g["row_labels"][k]
                lines.append(f"  · 子项[{sub}] 列{idx} 去重取值: {_fmt_distinct(body, idx, 40)}")
            blocks.append("\n".join(lines))
        else:
            idx = g["member_indexes"][0]
            blocks.append(f"列{idx} 表头: {g['title']}\n  去重取值（值后括号是出现次数）: {_fmt_distinct(body, idx, 60)}")

    total = max(0, len(rows) - 1)
    return (
        f"<columns>\n总数据行数（不含表头）: {total}\n\n"
        + "\n\n".join(blocks)
        + "\n</columns>\n\n"
        + "选项边界（严格执行）：options 必须由该列「去重取值」里的真实单元格取值或多选拆分值支撑；不得从题干/表头中抽取选项。若 New Medal 等词只出现在题干里、没有出现在该列取值里，不得写入 options。若多语言取值语义相同，options 请写合并后的中文标准值，并在 value_aliases 中列出支撑它的真实取值。\n\n"
        + _COLUMN_DETECT_SCHEMA_HINT
    )


def _heuristic_questions(rows: list[list], groups: list[dict]) -> list[dict]:
    """LLM 解析失败时的本地兜底：用启发式 + 矩阵分组拼出 questions。"""
    headers = rows[0]
    body = rows[1:]
    out: list[dict] = []
    for g in groups:
        if g["type"] == "matrix":
            # 子项样本是否多为数值 → matrix_scale，否则 matrix_multi
            numeric = 0
            checked = 0
            for idx in g["member_indexes"]:
                for v in _col_samples(body, idx, 10):
                    checked += 1
                    try:
                        float(v)
                        numeric += 1
                    except ValueError:
                        pass
            role = "matrix_scale" if checked and numeric / checked > 0.7 else "matrix_multi"
            q = {
                "name_zh": g["title"],
                "role": role,
                "column_indexes": g["member_indexes"],
                "rows": g["row_labels"],
            }
            if role == "matrix_scale":
                q["scale_min"], q["scale_max"] = 1, 5
            else:
                q["delimiter"] = "，"
            out.append(q)
        else:
            idx = g["member_indexes"][0]
            vals = [str(r[idx]) if idx < len(r) else "" for r in body]
            role = _heuristic_type(headers[idx], vals)
            q = {"name_zh": g["title"], "role": role, "column_indexes": [idx]}
            if role == "multi_choice":
                q["delimiter"] = "，"
            if role == "scale":
                q["scale_min"], q["scale_max"] = 1, 5
            out.append(q)
    return out


def _enrich_questions(questions: list[dict], headers: list[str], groups: list[dict]) -> list[dict]:
    """补全前端展示需要的字段（name_zh 兜底、矩阵 rows 兜底）。"""
    matrix_rows_by_first: dict[int, list[str]] = {}
    for g in groups:
        if g["type"] == "matrix":
            matrix_rows_by_first[g["member_indexes"][0]] = g["row_labels"]
    for q in questions:
        cis = q.get("column_indexes") or []
        if not q.get("name_zh"):
            first = cis[0] if cis else None
            q["name_zh"] = (headers[first].strip() if first is not None and first < len(headers) else "") or "未命名题目"
        # 矩阵题缺 rows → 用本地分组补
        if q.get("role") in ("matrix_scale", "matrix_multi") and not q.get("rows"):
            if cis and cis[0] in matrix_rows_by_first:
                q["rows"] = matrix_rows_by_first[cis[0]]
    return questions
