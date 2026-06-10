"""倍市得"跑数表"(banner 交叉表)解析器。

输入:倍市得后台导出的跑数结果 .xlsx(WPS 生成),内容是一张按画像分段的
交叉统计表 —— 纵向逐题排列(题目 + 选项),横向按分组维度分段(总体 / 段位 /
分路 …),每格是占比(当前模板)或计数+占比(旧模板)。

输出:结构化 dict + render_to_markdown() 渲染成 Writer 用的 <stats> markdown。

设计要点:
  * 不依赖 openpyxl 的 dimension/max_row(WPS 导出常把 dimension 写成 A1:A1,
    导致 openpyxl/pandas 读成空表)。直接按 zip + XML 逐格还原单元格,最可靠。
  * 分组维度 / 分段列数完全动态,不写死。
  * 自动识别两种模板:
      - 当前模板(手感版):表头 2 行(分组名 / 分段名),每段 1 列,值即占比;
      - 旧模板(卡蒂塔版):表头 3 行(多一行"计数/百分比"),每段 2 列,取占比列。
  * 打分题:总计行下紧跟一行"空选项标签 + 单个数字" = 均分,单独识别。
  * 矩阵题:题目文本含":" → 冒号前为矩阵题组、冒号后为子项。
"""

from __future__ import annotations

import io
import re
import zipfile
import xml.etree.ElementTree as ET

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


# ──────────────────────────────────────────────────────────────────────────────
# 公开入口
# ──────────────────────────────────────────────────────────────────────────────

def parse(xlsx_bytes: bytes) -> dict:
    """解析跑数表字节流,返回结构化 dict。

    返回:
      {
        "segments": [{"group": str, "name": str, "label": str}, ...],  # 横向分段(有序)
        "questions": [
          {
            "name": str,                  # 题目文本(矩阵题含子项)
            "matrix_group": str | None,   # 冒号前(矩阵题组),非矩阵为 None
            "sub_item": str | None,       # 冒号后(矩阵子项)
            "base": {seg_label: int},     # 总计:各分段有效样本 N
            "mean": {seg_label: float} | None,  # 打分题均分(无则 None)
            "options": [{"label": str, "values": {seg_label: float}}],  # 占比(0~1)
          }, ...
        ],
      }
    """
    grid = _read_grid(xlsx_bytes)
    if not grid:
        raise ValueError("跑数表为空或无法解析(未读到任何单元格)")
    return _parse_banner(grid)


def render_to_markdown(parsed: dict) -> str:
    """把解析结果渲染成 Writer <stats> 槽用的 markdown 字符串。"""
    segs = parsed["segments"]
    seg_labels = [s["label"] for s in segs]

    lines: list[str] = []
    lines.append("> 数据来源:倍市得跑数表(交叉统计)。占比已按各分段有效样本计算。")
    lines.append(f"> 分段维度:{', '.join(seg_labels)}")
    lines.append("")

    for q in parsed["questions"]:
        title = q["name"]
        lines.append(f"## {title}")

        base = q.get("base") or {}
        if base:
            base_str = ", ".join(
                f"{lbl}={base[lbl]}" for lbl in seg_labels if lbl in base
            )
            lines.append(f"- 有效样本(总计):{base_str}")

        mean = q.get("mean")
        if mean:
            mean_str = ", ".join(
                f"{lbl}={_fmt_num(mean[lbl])}" for lbl in seg_labels if lbl in mean
            )
            lines.append(f"- 均分:{mean_str}")

        opts = q.get("options") or []
        if opts:
            header = "| 选项 | " + " | ".join(seg_labels) + " |"
            sep = "| --- | " + " | ".join("---" for _ in seg_labels) + " |"
            lines.append(header)
            lines.append(sep)
            for o in opts:
                vals = o["values"]
                cells = [_fmt_pct(vals.get(lbl)) for lbl in seg_labels]
                lines.append(f"| {o['label']} | " + " | ".join(cells) + " |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# 读取单元格网格(zip + XML,绕开 openpyxl dimension 问题)
# ──────────────────────────────────────────────────────────────────────────────

def _read_grid(xlsx_bytes: bytes) -> dict[int, dict[int, str]]:
    """返回 {row_num: {col_num: cell_text}}(1-based 行列号)。"""
    z = zipfile.ZipFile(io.BytesIO(xlsx_bytes))

    # 共享字符串
    sst: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(f"{_NS}si"):
            sst.append("".join(t.text or "" for t in si.iter(f"{_NS}t")))

    # 第一个工作表(按 workbook 顺序的第一个 sheet 对应 sheet1.xml)
    sheet_path = _first_sheet_path(z)
    ws = ET.fromstring(z.read(sheet_path))

    grid: dict[int, dict[int, str]] = {}
    for row in ws.iter(f"{_NS}row"):
        for c in row.findall(f"{_NS}c"):
            ref = c.get("r")
            if not ref:
                continue
            col, rn = _ref_to_colrow(ref)
            t = c.get("t")
            val = ""
            if t == "s":  # 共享字符串
                v = c.find(f"{_NS}v")
                if v is not None and v.text is not None:
                    idx = int(v.text)
                    val = sst[idx] if 0 <= idx < len(sst) else ""
            elif t == "inlineStr":
                is_el = c.find(f"{_NS}is")
                if is_el is not None:
                    val = "".join(tt.text or "" for tt in is_el.iter(f"{_NS}t"))
            else:  # 数字 / 其他
                v = c.find(f"{_NS}v")
                if v is not None and v.text is not None:
                    val = v.text
            val = (val or "").strip()
            if val != "":
                grid.setdefault(rn, {})[col] = val
    return grid


def _first_sheet_path(z: zipfile.ZipFile) -> str:
    """返回 workbook 中第一个 sheet 的 xml 路径。"""
    names = z.namelist()
    # 优先 sheet1.xml;否则取任意一个 worksheets/*.xml
    if "xl/worksheets/sheet1.xml" in names:
        return "xl/worksheets/sheet1.xml"
    for n in names:
        if n.startswith("xl/worksheets/") and n.endswith(".xml"):
            return n
    raise ValueError("跑数表中找不到工作表 xml")


_REF_RE = re.compile(r"([A-Z]+)(\d+)")


def _ref_to_colrow(ref: str) -> tuple[int, int]:
    """'C4' -> (col=3, row=4)。"""
    m = _REF_RE.match(ref)
    letters, num = m.group(1), int(m.group(2))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return col, num


# ──────────────────────────────────────────────────────────────────────────────
# banner 表结构解析
# ──────────────────────────────────────────────────────────────────────────────

_Q_COL = 1   # A 列:题目文本
_OPT_COL = 2  # B 列:选项 / "总计"
_FIRST_SEG_COL = 3  # C 列起:分段数据


def _parse_banner(grid: dict[int, dict[int, str]]) -> dict:
    max_row = max(grid.keys())
    max_col = max((max(cols) for cols in grid.values()), default=0)

    # 1) 定位首个"总计"行(数据起点);其上为表头带
    anchor = None
    for rn in sorted(grid.keys()):
        if grid[rn].get(_OPT_COL, "") == "总计":
            anchor = rn
            break
    if anchor is None:
        raise ValueError("跑数表中找不到「总计」行,无法定位数据起点")

    header_rows = [rn for rn in range(1, anchor) if rn in grid]

    # 2) 判定模板变体:表头带里是否出现"计数/百分比"行
    count_pct_row = None
    for rn in header_rows:
        vals = list(grid[rn].values())
        if any(v in ("计数", "百分比") for v in vals):
            count_pct_row = rn
            break
    paired = count_pct_row is not None

    # 3) 构建分段列表
    segments, col_to_seg = _build_segments(
        grid, header_rows, count_pct_row, max_col, paired
    )

    # 4) 逐题解析
    questions: list[dict] = []
    cur: dict | None = None
    for rn in range(anchor, max_row + 1):
        if rn not in grid:
            continue
        row = grid[rn]
        a = row.get(_Q_COL, "")
        b = row.get(_OPT_COL, "")

        if b == "总计":
            # 新题块起点
            if cur:
                questions.append(cur)
            name = a.strip()
            matrix_group, sub_item = _split_matrix(name)
            cur = {
                "name": name,
                "matrix_group": matrix_group,
                "sub_item": sub_item,
                "base": _row_seg_values(row, col_to_seg, as_int=True),
                "mean": None,
                "options": [],
            }
            continue

        if cur is None:
            continue

        # 均分行:选项标签为空,但分段列有数值(打分题)
        if not a and not b:
            seg_vals = _row_seg_values(row, col_to_seg, as_int=False)
            if seg_vals:
                cur["mean"] = seg_vals
            continue

        # 普通选项行
        if b:
            cur["options"].append({
                "label": b.strip(),
                "values": _row_seg_values(row, col_to_seg, as_int=False),
            })

    if cur:
        questions.append(cur)

    return {"segments": segments, "questions": questions}


def _build_segments(grid, header_rows, count_pct_row, max_col, paired):
    """构建有序分段列表 + 列号→分段label 的映射。

    返回 (segments, col_to_seg):
      segments: [{"group","name","label"}]
      col_to_seg: {data_col: seg_label}
    """
    # 表头:第一行=分组名,第二行=分段名
    grp_row = header_rows[0] if header_rows else None
    seg_row = header_rows[1] if len(header_rows) > 1 else None

    grp_map = grid.get(grp_row, {}) if grp_row else {}
    seg_map = grid.get(seg_row, {}) if seg_row else {}

    # 分组名按列从左到右前向填充(合并单元格只在起始列有值)
    groups_ff: dict[int, str] = {}
    last_grp = ""
    for col in range(_FIRST_SEG_COL, max_col + 1):
        if grp_map.get(col):
            last_grp = grp_map[col]
        groups_ff[col] = last_grp

    segments: list[dict] = []
    col_to_seg: dict[int, str] = {}

    if not paired:
        # 当前模板:每段 1 列
        last_seg = ""
        for col in range(_FIRST_SEG_COL, max_col + 1):
            seg_name = seg_map.get(col, "")
            grp = groups_ff.get(col, "")
            if not seg_name:
                # "总体"这种:分段名留空 → 用分组名
                seg_name = grp or last_seg
            last_seg = seg_name
            label = _seg_label(grp, seg_name)
            segments.append({"group": grp, "name": seg_name, "label": label})
            col_to_seg[col] = label
    else:
        # 旧模板:每段 2 列(计数/百分比),取百分比列;分段名前向填充
        cp_map = grid.get(count_pct_row, {})
        last_seg = ""
        for col in range(_FIRST_SEG_COL, max_col + 1):
            if seg_map.get(col):
                last_seg = seg_map[col]
            kind = cp_map.get(col, "")
            if kind == "百分比":
                grp = groups_ff.get(col, "")
                seg_name = last_seg or grp
                label = _seg_label(grp, seg_name)
                # 同一分段两列只登记一次(百分比列)
                if label not in col_to_seg.values():
                    segments.append({"group": grp, "name": seg_name, "label": label})
                col_to_seg[col] = label

    return segments, col_to_seg


def _seg_label(group: str, name: str) -> str:
    """分段展示标签:总体 → '总体';其余 → '分组:分段'。"""
    if not group or group == name:
        return name
    return f"{group}:{name}"


def _row_seg_values(row: dict[int, str], col_to_seg: dict[int, str], as_int: bool):
    """从一行里按 col_to_seg 取各分段数值。空值跳过。"""
    out: dict[str, float] = {}
    for col, label in col_to_seg.items():
        raw = row.get(col, "")
        if raw == "":
            continue
        num = _to_number(raw)
        if num is None:
            continue
        out[label] = int(round(num)) if as_int else num
    return out


def _split_matrix(name: str) -> tuple[str | None, str | None]:
    """题目文本含中文/英文冒号 → 拆分为 (矩阵题组, 子项)。"""
    for sep in ("：", ":"):
        if sep in name:
            grp, sub = name.split(sep, 1)
            grp, sub = grp.strip(), sub.strip()
            if grp and sub:
                return grp, sub
    return None, None


def _to_number(s: str):
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.1f}%"


def _fmt_num(v) -> str:
    if v is None:
        return "-"
    return f"{v:.2f}"
