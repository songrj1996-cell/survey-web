"""问卷分析平台 Web 后端 v2

新增：
- 本地题型推断 + 用户确认（Step 2）
- Prompt 管理（可编辑 + 版本历史）
- 历史记录（最近 5 条）
- Word 下载修复（RFC 5987）
"""

import asyncio
import csv
import io
import json
import os
import re
import time
import uuid
from datetime import datetime
from typing import AsyncGenerator
from urllib.parse import quote

import openpyxl
from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import secrets

import feishu_export
import survey_plan
import survey_stats
from dify import chat as dify_chat  # noqa: F401 (kept for compatibility)

DIFY_API_BASE    = os.getenv("DIFY_API_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_PLANNER_KEY = os.getenv("DIFY_PLANNER_KEY", "")
DIFY_ANALYST_KEY = os.getenv("DIFY_ANALYST_KEY", "")
DIFY_COLUMN_KEY  = os.getenv("DIFY_COLUMN_KEY", "")  # 题型识别（专用 Dify 应用）
DIFY_BASE_URL    = os.getenv("DIFY_API_BASE", "https://dify.web.moontontech.net/v1")
# 用于前端展示 Dify 后台入口（去掉 /v1 后缀）
DIFY_CONSOLE_URL = re.sub(r"/v1$", "", DIFY_BASE_URL)

# ── 数据目录 ──────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

PROMPTS_FILE = os.path.join(DATA_DIR, "prompts.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
MAX_HISTORY  = 5

# ============================================================
# 默认 Prompts
# ============================================================

DEFAULT_UPLOAD_GUIDE = """\
**【数据源】**

1. 支持直接传入 googleform 及倍市得平台导出的问卷回答。
   - 倍市得平台导出时请筛选成功完成的回复，**导出设置选择"excel-可读数据"，勾选多选题同一列**，其他默认即可。
   - google form 链接的 google sheet 可以直接下载对应格式并上传。
2. 支持 CSV 和 Excel (.xlsx/.xls) 格式，请确保表格的第一行为**题目名称**，从第二行开始为**答卷数据**。
3. 上传的 excel 文件中可以有多个 sheet，但**只会读取放置在第一位的 sheet 内容**进行分析。

**【题型及分析方案】**

1. 题型、题目及问卷分析逻辑会由 LLM 判断，请在"数据确认"及"方案确认"环节仔细审阅，保证产出报告的准确度。\
"""

DEFAULT_WRITER_REQUIREMENTS = """\
1. 报告开头用 `# 一级标题`；如 metadata 里有「被排除」样本，开头列出依据
2. 紧接标题之后，先写「核心结论」模块（**不是** `## Part`，直接用 `## 核心结论`），并用注释标记把整段包起来，格式严格如下：
   <!--CORE_START-->
   ## 核心结论
   - （3–6 条要点，突出**多数人/大盘最重要的发现**，每条尽量带关键数字）
   - 风险提示：仅当存在**个别但有风险**的观点时才写（如可能助长游戏黑产/代练/外挂、玩家极端负面体验、流失风险等），写清是少数人但需警惕；**没有风险就不要写这条**，也不要为凑数罗列普通的少数观点
   <!--CORE_END-->
   `<!--CORE_START-->` 必须在 `## 核心结论` 这一行的正上方、`<!--CORE_END-->` 在核心结论结束后另起一行，两个标记各自独占一行。
3. 之后严格按 plan 给的 parts 顺序划分章节，每个 part 用 `## Part X 章节名` 二级标题
4. 每个 part 章节内同时综合该 part 的客观题统计 + 主观题归纳，不要按题型割裂
5. `<stats>` 块里所有数字、百分比、表格已经算好——**严禁修改、重新计算、合并、四舍五入**。你写到报告里的所有数字必须能在 `<stats>` 里逐字找到
6. 主观题归纳：从 `<open_text>` 块里找该 part 内的开放题原文，归纳 3–5 个主题，每个主题给占比（自己数）和 1–2 条代表性原话
7. 关于「画像/人群结构」：仅当 `<stats>` 里有「画像维度概览」时才写人群结构相关内容，且要用**大白话**描述（例：「参与玩家以神话段位为主，约占四成」），**不要直接堆字段名/列名**；若 `<stats>` 里没有画像维度概览，则**整篇报告不要出现任何画像/人群结构章节或描述**
8. 不要复制 `<stats>` 整块，但可以原样引用其中的表格\
"""

# 报告免责声明（确定性插入到标题下方，不依赖 LLM）
REPORT_DISCLAIMER = "> 该报告使用智能调研分析工具产出，如有疑问，请联系开发者@宋润佳(Nancy)"
# 核心结论包裹标记（writer 按要求输出，飞书导出时据此定位转高亮块）
CORE_START = "<!--CORE_START-->"
CORE_END = "<!--CORE_END-->"


def _inject_disclaimer(md: str) -> str:
    """在第一行 `# 标题` 之后插入免责声明引用行；幂等；无 H1 则插到最前。"""
    if not md:
        return md
    if REPORT_DISCLAIMER in md:
        return md
    lines = md.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("# ") and not ln.startswith("## "):
            lines.insert(i + 1, "")
            lines.insert(i + 2, REPORT_DISCLAIMER)
            return "\n".join(lines)
    # 没有 H1：插到最前
    return REPORT_DISCLAIMER + "\n\n" + md


def _strip_core_markers(md: str) -> str:
    """移除核心结论包裹标记行（仅飞书导出用于定位高亮块，其它导出不应出现）。"""
    if not md:
        return md
    return "\n".join(ln for ln in md.split("\n") if ln.strip() not in (CORE_START, CORE_END))


def _prep_export_md(md: str) -> str:
    """通用导出前处理：补免责声明（幂等）+ 去掉核心结论标记。"""
    return _strip_core_markers(_inject_disclaimer(md))

DEFAULT_PLANNER_EXTRA = """\
请按 JSON schema 输出列分类、part 划分、交叉分析建议、open_questions。
open_questions 中的问题请以「我计划…，请确认是否这样做？」的格式提出，不要对已由用户确认的列类型再次提问。\
"""

DEFAULT_PROMPTS: dict = {
    "upload_guide": {
        "key": "upload_guide",
        "label": "上传说明文案",
        "description": (
            "显示在上传文件按钮上方的说明文本，支持 Markdown 格式。"
            "修改后刷新页面即可生效。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_UPLOAD_GUIDE,
        "history": [],
    },
    "writer_requirements": {
        "key": "writer_requirements",
        "label": "分析师写报告要求",
        "description": (
            "附加在发送给 Analyst（调研分析-分析师）的 query 末尾的写报告要求。"
            "修改后下一次分析立即生效，无需重启服务。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_WRITER_REQUIREMENTS,
        "history": [],
        "version": 2,  # 改了默认值就 +1：未被用户编辑过的会自动升级
    },
    "planner_extra": {
        "key": "planner_extra",
        "label": "Planner 分析指令",
        "description": (
            "附加在发送给 Planner（调研分析-规划器）的 query 末尾的补充指令。"
            "影响列分类、章节划分、交叉分析的规划方式。"
        ),
        "dify_app": None,
        "dify_url": None,
        "editable": True,
        "current": DEFAULT_PLANNER_EXTRA,
        "history": [],
    },
    "dify_planner_system": {
        "key": "dify_planner_system",
        "label": "规划器 System Prompt（Dify 管理）",
        "description": (
            "配置在 Dify「调研分析-规划器」应用中的 System Prompt。"
            "需在 Dify 后台「编排 → 提示词」中修改，此处仅供参考。"
        ),
        "dify_app": "调研分析-规划器",
        "dify_url": DIFY_CONSOLE_URL,
        "editable": False,
        "current": "（请前往 Dify 后台查看：调研分析-规划器 → 编排 → 提示词）",
        "history": [],
    },
    "dify_analyst_system": {
        "key": "dify_analyst_system",
        "label": "分析师 System Prompt（Dify 管理）",
        "description": (
            "配置在 Dify「调研分析-分析师」应用中的 System Prompt。"
            "需在 Dify 后台「编排 → 提示词」中修改，此处仅供参考。"
        ),
        "dify_app": "调研分析-分析师",
        "dify_url": DIFY_CONSOLE_URL,
        "editable": False,
        "current": "（请前往 Dify 后台查看：调研分析-分析师 → 编排 → 提示词）",
        "history": [],
    },
}


def _load_prompts() -> dict:
    if not os.path.exists(PROMPTS_FILE):
        _save_prompts(DEFAULT_PROMPTS)
        return DEFAULT_PROMPTS
    with open(PROMPTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    dirty = False
    for k, v in DEFAULT_PROMPTS.items():
        if k not in data:
            data[k] = v
            dirty = True
            continue
        # 默认值升级：版本落后且用户从未编辑过（history 为空）→ 用新默认覆盖 current
        default_ver = v.get("version", 1)
        if data[k].get("version", 1) < default_ver:
            if not data[k].get("history"):
                data[k]["current"] = v["current"]
            data[k]["version"] = default_ver
            dirty = True
    if dirty:
        _save_prompts(data)
    return data


def _save_prompts(prompts: dict) -> None:
    with open(PROMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(prompts, f, ensure_ascii=False, indent=2)


def _get_writer_requirements() -> str:
    return _load_prompts()["writer_requirements"]["current"]


def _get_planner_extra() -> str:
    return _load_prompts()["planner_extra"]["current"]


# ============================================================
# Session 管理
# ============================================================

sessions: dict[str, dict] = {}
SESSION_TTL = 7200


def _clean_sessions() -> None:
    now = time.time()
    for k in list(sessions.keys()):
        if sessions[k].get("ts", 0) + SESSION_TTL < now:
            del sessions[k]


def new_session() -> str:
    _clean_sessions()
    sid = str(uuid.uuid4())
    sessions[sid] = {"ts": time.time()}
    return sid


def get_session(sid: str) -> dict:
    sess = sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传文件")
    sess["ts"] = time.time()
    return sess


# ============================================================
# 历史记录
# ============================================================

def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_history(history: list) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def save_to_history(session_id: str, sess: dict) -> None:
    report_md = sess.get("report_md", "")
    if not report_md:
        return
    history = _load_history()
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "未命名报告"
    entry = {
        "id": session_id,
        "filename": sess.get("filename", "unknown"),
        "title": title,
        "created_at": datetime.now().isoformat(),
        "report_md": report_md,
        "plan": sess.get("plan"),
        "stats_md": sess.get("stats_md"),
        "analyst_conv_id": sess.get("analyst_conv_id", ""),
        "rows_fed": True,  # 历史 QA 跳过投喂 rows（对话已包含上下文）
    }
    history = [h for h in history if h["id"] != session_id]
    history.insert(0, entry)
    history = history[:MAX_HISTORY]
    _save_history(history)


# ============================================================
# 文件解析
# ============================================================

def _parse_csv(content: bytes) -> list[list]:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb2312"):
        try:
            text = content.decode(enc)
            reader = csv.reader(io.StringIO(text))
            rows = [list(row) for row in reader]
            while rows and all(not c.strip() for c in rows[-1]):
                rows.pop()
            return rows
        except (UnicodeDecodeError, csv.Error):
            continue
    raise ValueError("无法解析 CSV 文件，请确认文件编码为 UTF-8 或 GBK")


def _parse_excel(content: bytes) -> list[list]:
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([("" if c is None else str(c)) for c in row])
    wb.close()
    while rows and all(not c.strip() for c in rows[-1]):
        rows.pop()
    return rows


def _parse_file(filename: str, content: bytes) -> list[list]:
    name_lower = filename.lower()
    if name_lower.endswith(".csv"):
        return _parse_csv(content)
    elif name_lower.endswith((".xlsx", ".xls")):
        return _parse_excel(content)
    else:
        try:
            return _parse_csv(content)
        except ValueError:
            pass
        raise ValueError("不支持的文件格式，请上传 CSV 或 Excel 文件")


# ============================================================
# 本地题型推断
# ============================================================

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

    # ── 多选（分隔符检测）──
    delimiters = [",", "，", ";", "；", "、", "|"]
    delim_count = sum(1 for v in non_empty if any(d in v for d in delimiters))
    if delim_count / total > 0.25:
        return "multi_choice"

    # ── 唯一值数量 ──
    unique_vals = set(non_empty)
    n_unique = len(unique_vals)
    ratio = n_unique / total

    # 少量唯一值 → 单选 / 画像
    if n_unique <= 2:
        return "single_choice"
    if n_unique <= 8:
        return "single_choice"
    if ratio < 0.25:
        return "single_choice"

    # ── 长文本 → 开放题 ──
    avg_len = sum(len(v) for v in non_empty) / total
    if avg_len > 25:
        return "open_text"

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
      "options": ["选项A","选项B"],            // multi_choice / matrix_multi：完整选项清单，用中文标准值
      "scale_min": 1, "scale_max": 5,          // scale / matrix_scale：量程
      "rows": ["子项1","子项2"],               // matrix_*：与 column_indexes 顺序一一对应的行标签
      "value_aliases": {"中文标准值": ["原始变体1","Mythic","Mítica"]}  // 见下「同义归并」
    }
  ]
}
角色判断要点：
- 玩家ID/编号 → id；明确是 MLBB 游戏ID → mlbbid；提交时间等 → ignore
- 年龄段/段位/地区等用于分群的 → profile_dim
- 数值评分（如 1–5、1–10）→ scale
- 含多个选项、可多选 → multi_choice，并尽量给出 options 清单
- 长文本主观回答 → open_text
- 标注【疑似矩阵题】的，按子项判断 matrix_scale（每子项打分）或 matrix_multi（每子项可多选），rows 用给出的子项标签

同义归并（value_aliases，重要）：
- 仅对 single_choice / profile_dim / multi_choice / matrix_multi 这类「选项题」给出。
- 我已在每列附上「去重取值」。请把**语义相同但写法/语种不同**的取值（如 神话/Mythic/Mítica，中国/China/CN）归并到**同一个中文标准值**：key=中文标准值，value=该标准值对应的所有原始变体（含中文标准值本身可不必重复列出）。
- 只有确属同义才合并；拿不准就不要合并。没有任何同义可并的列，可以省略 value_aliases 或给 {}。
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


# ============================================================
# Planner / Writer 构建
# ============================================================

def _build_planner_sample(rows: list[list], sample_n: int = 5) -> str:
    if not rows:
        return ""
    headers = rows[0]
    sample = rows[1: 1 + sample_n]

    def esc(s):
        return ("" if s is None else str(s)).replace("|", "\\|").replace("\n", "<br>")

    md = "| " + " | ".join(esc(h) for h in headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"
    for r in sample:
        cells = [r[i] if i < len(r) else "" for i in range(len(headers))]
        md += "| " + " | ".join(esc(c) for c in cells) + " |\n"

    total_data_rows = max(0, len(rows) - 1)
    return (
        f"<sample>\n"
        f"总数据行数（不含表头）: {total_data_rows}\n"
        f"以下展示表头 + 前 {len(sample)} 行样本：\n\n"
        f"{md}\n"
        f"</sample>"
    )


def _build_planner_query_with_confirmed(rows: list[list], confirmed_columns: list[dict]) -> str:
    """构建给 Planner 的完整 query，含用户确认的题型（逻辑题，矩阵题跨多列）。"""
    sample_md = _build_planner_sample(rows)

    confirmed_lines = []
    for q in confirmed_columns:
        # 兼容旧结构（confirmed_type/index）与新结构（role/name_zh/column_indexes）
        role = q.get("role") or q.get("confirmed_type") or "single_choice"
        name = q.get("name_zh") or q.get("name") or "?"
        cis = q.get("column_indexes") or ([q["index"]] if "index" in q else [])
        label = ROLE_LABEL_MAP.get(role, role)
        extra = ""
        if role in ("multi_choice", "matrix_multi") and q.get("options"):
            opts = "、".join(str(o) for o in q["options"][:12])
            extra += f"，选项: {opts}"
        if role in ("multi_choice",) and q.get("delimiter"):
            extra += f"，分隔符: 「{q['delimiter']}」"
        if role in ("scale", "matrix_scale") and q.get("scale_min") is not None:
            extra += f"，量程: {q.get('scale_min')}–{q.get('scale_max')}"

        if role in ("matrix_scale", "matrix_multi"):
            rows_lbl = "、".join(str(r) for r in (q.get("rows") or []))
            confirmed_lines.append(
                f"- 矩阵题「{name}」({label})，子项行: {rows_lbl}；"
                f"对应列号 {cis}（这些列同属一道题，**必须整体归入同一个 part**）{extra}"
            )
        else:
            idx = cis[0] if cis else q.get("index", 0)
            confirmed_lines.append(f"- 列{idx}「{name}」: {label}{extra}")

    confirmed_block = "<confirmed_column_types>\n" + "\n".join(confirmed_lines) + "\n</confirmed_column_types>"
    extra_instructions = _get_planner_extra()

    return (
        f"{sample_md}\n\n"
        f"{confirmed_block}\n\n"
        f"注意：以上题型已由用户在界面中逐一确认，**不得**在 open_questions 中再次对题型进行发问。"
        f"矩阵题的多个列号务必整体归入同一个 part。\n\n"
        f"{extra_instructions}"
    )


def _build_writer_query(stats_md: str, open_text: dict, plan: dict, headers: list[str]) -> str:
    parts_lines = []
    for i, p in enumerate(plan["parts"], 1):
        col_names = []
        for idx in p["column_indexes"]:
            col = next((c for c in plan["columns"] if c["index"] == idx), None)
            name = (col and col.get("name")) or (headers[idx] if idx < len(headers) else f"列{idx}")
            role = col["role"] if col else "?"
            col_names.append(f"{name}({role})")
        parts_lines.append(f"  Part {i} {p['name']}: {'; '.join(col_names)}")
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"

    open_text_blocks = []
    for col_idx, texts in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        name = (col and col.get("name")) or (headers[col_idx] if col_idx < len(headers) else f"列{col_idx}")
        joined_lines = []
        for entry in texts:
            ids_str = " / ".join(f"{k}:{v}" for k, v in entry.get("ids", {}).items())
            profile_str = " / ".join(f"{k}:{v}" for k, v in entry.get("profile", {}).items())
            prefix = " | ".join(filter(None, [ids_str, profile_str]))
            text_val = entry.get("text", "")
            joined_lines.append(f"- {f'[{prefix}] ' if prefix else ''}{text_val}")
        joined = "\n".join(joined_lines)
        if len(joined) > 20000:
            joined = "\n".join(joined_lines[:200]) + f"\n（共 {len(texts)} 条，已截取前 200 条）"
        open_text_blocks.append(f"### {name}（列 {col_idx}, 共 {len(texts)} 条非空回答）\n{joined}")

    open_text_md = (
        "<open_text>\n" + "\n\n".join(open_text_blocks) + "\n</open_text>"
        if open_text_blocks else "<open_text>（本问卷没有开放题）</open_text>"
    )

    requirements = _get_writer_requirements()

    return (
        "**任务**：基于以下确定性统计数据撰写完整调研报告。\n\n"
        f"{plan_summary}\n\n"
        f"<stats>\n{stats_md}\n</stats>\n\n"
        f"{open_text_md}\n\n"
        f"**要求**：\n{requirements}"
    )


def _format_rows_for_qa(rows: list[list], plan: dict) -> str:
    QA_MAX = 60000
    if not rows or len(rows) <= 1:
        return "（无数据）"
    headers = rows[0]
    body = rows[1:]
    total = len(body)
    col_names = [(h or "").strip() or f"col_{i}" for i, h in enumerate(headers)]

    def row_obj(row):
        return {col_names[i]: (row[i] if i < len(row) else "") for i in range(len(col_names))}

    dump = "\n".join(json.dumps(row_obj(r), ensure_ascii=False) for r in body)
    if len(dump) > QA_MAX:
        pidxs = [c["index"] for c in plan.get("columns", []) if c.get("role") == "profile_dim"]
        sampled = _stratified_sample(body, pidxs, 100)
        note = (
            f"# 原始数据共 {total} 行，超出上下文上限，已按画像维度分层抽样到 {len(sampled)} 行。\n\n"
        )
        dump = note + "\n".join(json.dumps(row_obj(r), ensure_ascii=False) for r in sampled)
    return dump


def _stratified_sample(body: list[list], profile_indexes: list[int], target: int = 100):
    if not profile_indexes or len(body) <= target:
        return body[:target]

    def key(row):
        return tuple(row[i] if i < len(row) else "" for i in profile_indexes)

    buckets: dict = {}
    for r in body:
        buckets.setdefault(key(r), []).append(r)

    out: list = []
    total = len(body)
    for items in buckets.values():
        share = max(1, round(len(items) / total * target))
        out.extend(items[:share])
        if len(out) >= target:
            break
    return out[:target]


# ============================================================
# Markdown → Word
# ============================================================

def _add_formatted_run(paragraph, text: str):
    parts = re.split(r'(\*\*.*?\*\*|\*.*?\*|`[^`]+`)', text)
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            run = paragraph.add_run(part[2:-2])
            run.bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Courier New"
        elif part.startswith("*") and part.endswith("*"):
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        else:
            paragraph.add_run(part)


def _parse_md_table(lines: list[str]) -> list[list[str]]:
    result = []
    for line in lines:
        if re.match(r'^\|[\s\-|:]+\|$', line.strip()):
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        result.append(cells)
    return result


def markdown_to_docx(md_text: str) -> bytes:
    doc = Document()
    section = doc.sections[0]
    section.page_width = Inches(8.27)
    section.page_height = Inches(11.69)
    section.left_margin = Inches(1.18)
    section.right_margin = Inches(1.18)
    section.top_margin = Inches(0.98)
    section.bottom_margin = Inches(0.98)

    style = doc.styles['Normal']
    style.font.name = 'Calibri'
    style.font.size = Pt(11)
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    lines = md_text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith('# ') and not line.startswith('## '):
            h = doc.add_heading(level=1); h.clear()
            _add_formatted_run(h, line[2:].strip())
        elif line.startswith('## ') and not line.startswith('### '):
            h = doc.add_heading(level=2); h.clear()
            _add_formatted_run(h, line[3:].strip())
        elif line.startswith('### '):
            h = doc.add_heading(level=3); h.clear()
            _add_formatted_run(h, line[4:].strip())
        elif line.startswith('|'):
            table_lines = []
            while i < len(lines) and lines[i].startswith('|'):
                table_lines.append(lines[i]); i += 1
            table_data = _parse_md_table(table_lines)
            if table_data:
                num_cols = max(len(r) for r in table_data)
                tbl = doc.add_table(rows=len(table_data), cols=num_cols)
                tbl.style = 'Table Grid'
                for ri, row_data in enumerate(table_data):
                    for ci in range(num_cols):
                        ct = row_data[ci].replace('\\|', '|') if ci < len(row_data) else ""
                        cell = tbl.cell(ri, ci); cell.text = ""
                        run = cell.paragraphs[0].add_run(ct)
                        if ri == 0: run.bold = True
            continue
        elif line.startswith('- ') or line.startswith('* '):
            p = doc.add_paragraph(style='List Bullet'); p.clear()
            _add_formatted_run(p, line[2:].strip())
        elif re.match(r'^\d+\.\s', line):
            p = doc.add_paragraph(style='List Number'); p.clear()
            _add_formatted_run(p, re.sub(r'^\d+\.\s', '', line).strip())
        elif line.startswith('> '):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Inches(0.5)
            _add_formatted_run(p, line[2:].strip())
        elif line.strip() in ('---', '***', '___'):
            doc.add_paragraph()
        elif not line.strip():
            pass
        else:
            p = doc.add_paragraph(); _add_formatted_run(p, line.strip())

        i += 1

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


# ============================================================
# SSE 工具
# ============================================================

def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_dify_stream(
    query: str,
    user_id: str,
    conversation_id: str,
    api_key: str,
) -> AsyncGenerator[tuple[str, str], None]:
    import httpx

    payload = {
        "inputs": {},
        "query": query,
        "response_mode": "streaming",
        "user": user_id,
        "conversation_id": conversation_id,
    }
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    final_conv_id = conversation_id

    async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
        async with client.stream(
            "POST", f"{DIFY_API_BASE}/chat-messages",
            headers=req_headers, json=payload,
        ) as resp:
            if resp.status_code >= 400:
                err = await resp.aread()
                raise RuntimeError(f"Dify {resp.status_code}: {err.decode('utf-8', errors='replace')}")
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw:
                    continue
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event = data.get("event")
                if event in ("message", "agent_message"):
                    yield data.get("answer", ""), ""
                if data.get("conversation_id"):
                    final_conv_id = data["conversation_id"]
                if event == "error":
                    raise RuntimeError(f"Dify error: {data.get('code')} {data.get('message')}")
                if event in ("message_end", "workflow_finished", "agent_message_end"):
                    break

    yield "", final_conv_id


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(title="问卷分析平台")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


# ── 上传 ──────────────────────────────────────────────

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = _parse_file(file.filename or "upload.csv", content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not rows:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件只有表头没有数据行")

    sid = new_session()
    sessions[sid]["rows"] = rows
    sessions[sid]["filename"] = file.filename or "upload.csv"

    return {
        "session_id": sid,
        "filename": file.filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
    }


# ── 题型识别（Step 2，LLM，SSE）──────────────────────

@app.get("/api/columns/{session_id}")
async def get_columns(session_id: str):
    """LLM 识别列题型（含 Google Form 矩阵题分组、中文题名、多选选项清单）。

    流式返回；最终发 `columns_ready`，columns 为「逻辑题」列表（矩阵题跨多列）。
    LLM 解析失败时回退本地启发式。
    """
    sess = get_session(session_id)
    rows = sess.get("rows")
    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据")
    if not DIFY_COLUMN_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_COLUMN_KEY（题型识别应用）")

    async def generate():
        try:
            groups = _group_googleform_matrix(rows[0])
            query = _build_column_detect_query(rows, groups)
            header_count = len(rows[0])

            answer_chunks: list[str] = []
            final_conv = ""
            async for chunk, conv_id in sse_dify_stream(query, session_id, "", DIFY_COLUMN_KEY):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    final_conv = conv_id

            questions, err = survey_plan.parse_columns_from_llm("".join(answer_chunks), header_count)

            if not questions:
                retry_q = (
                    f"上次输出无法解析: {err}。请严格按 schema 用 ```json``` 围栏重新输出，"
                    "不要附加任何解释文字。"
                )
                retry_chunks: list[str] = []
                async for chunk, conv_id in sse_dify_stream(retry_q, session_id, final_conv, DIFY_COLUMN_KEY):
                    if chunk:
                        retry_chunks.append(chunk)
                questions, err = survey_plan.parse_columns_from_llm("".join(retry_chunks), header_count)

            if not questions:
                print(f"[columns] LLM 解析失败，回退本地启发式：{err}")
                questions = _heuristic_questions(rows, groups)
                yield sse_event({"type": "chunk", "content": "\n（题型识别解析失败，已回退本地推断，请仔细核对）\n"})

            questions = _enrich_questions(questions, rows[0], groups)
            sess["columns_detected"] = questions
            yield sse_event({"type": "columns_ready", "columns": questions})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


class ColumnConfirmRequest(BaseModel):
    columns: list[dict]


@app.post("/api/columns/{session_id}/confirm")
async def confirm_columns(session_id: str, req: ColumnConfirmRequest):
    """存储用户确认（或修改）后的列题型。"""
    sess = get_session(session_id)
    sess["confirmed_columns"] = req.columns
    return {"ok": True}


# ── Plan（SSE）────────────────────────────────────────

@app.get("/api/plan/{session_id}")
async def get_plan(session_id: str):
    sess = get_session(session_id)
    rows = sess.get("rows")
    confirmed_columns = sess.get("confirmed_columns")

    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据，请先上传文件")
    if not DIFY_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_PLANNER_KEY")

    async def generate():
        try:
            if confirmed_columns:
                planner_query = _build_planner_query_with_confirmed(rows, confirmed_columns)
            else:
                planner_query = (
                    _build_planner_sample(rows)
                    + "\n\n" + _get_planner_extra()
                )

            answer_chunks: list[str] = []
            final_conv_id = ""

            async for chunk, conv_id in sse_dify_stream(planner_query, session_id, "", DIFY_PLANNER_KEY):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    final_conv_id = conv_id

            full_answer = "".join(answer_chunks)
            headers = rows[0]
            plan, err = survey_plan.parse_plan_from_llm(full_answer, len(headers))

            if not plan:
                retry_q = (
                    f"上次输出无法解析: {err}。请严格按 JSON schema 重新输出，"
                    "用 ```json ``` 围栏，不要附加解释文字。"
                )
                retry_chunks: list[str] = []
                async for chunk, conv_id in sse_dify_stream(retry_q, session_id, final_conv_id, DIFY_PLANNER_KEY):
                    if chunk: retry_chunks.append(chunk)
                    if conv_id: final_conv_id = conv_id
                plan, err = survey_plan.parse_plan_from_llm("".join(retry_chunks), len(headers))

            if not plan:
                yield sse_event({"type": "error", "message": f"方案解析失败：{err}"}); return

            # 用用户确认的题型覆盖 planner 的列分类（权威），并修补 parts（矩阵题整体入同一 part）
            if confirmed_columns:
                plan = survey_plan.merge_confirmed_into_plan(plan, confirmed_columns)

            sess["plan"] = plan
            sess["planner_conv_id"] = final_conv_id
            card_text = survey_plan.render_plan_for_user(plan, headers)
            yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": headers})

        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── Plan 确认/修改 ──────────────────────────────────

class PlanConfirmRequest(BaseModel):
    session_id: str
    user_text: str


@app.post("/api/plan/confirm")
async def confirm_plan(req: PlanConfirmRequest):
    sess = get_session(req.session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    planner_conv_id = sess.get("planner_conv_id", "")

    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失，请重新上传文件")

    if survey_plan.is_user_approval(req.user_text):
        return JSONResponse({"approved": True})

    async def generate():
        try:
            new_conv_id = planner_conv_id
            answer_chunks: list[str] = []
            async for chunk, conv_id in sse_dify_stream(
                req.user_text, req.session_id, planner_conv_id, DIFY_PLANNER_KEY
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    new_conv_id = conv_id

            headers = rows[0]
            new_plan, err = survey_plan.parse_plan_from_llm(
                "".join(answer_chunks),
                survey_plan.header_count_from_plan(plan)
            )
            if not new_plan:
                yield sse_event({"type": "error", "message": f"修订方案解析失败：{err}"}); return

            if sess.get("confirmed_columns"):
                new_plan = survey_plan.merge_confirmed_into_plan(new_plan, sess["confirmed_columns"])

            sess["plan"] = new_plan
            sess["planner_conv_id"] = new_conv_id
            card_text = survey_plan.render_plan_for_user(new_plan, headers)
            yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": headers})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 统计 ────────────────────────────────────────────

@app.post("/api/stats/{session_id}")
async def compute_stats(session_id: str):
    sess = get_session(session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失")
    loop = asyncio.get_event_loop()
    stats_md, open_text = await loop.run_in_executor(None, survey_stats.compute, rows, plan)
    sess["stats_md"] = stats_md
    sess["open_text"] = open_text
    sess["rows_fed"] = False
    return {"stats_md": stats_md}


# ── 报告（SSE）──────────────────────────────────────

@app.get("/api/report/{session_id}")
async def generate_report(session_id: str):
    sess = get_session(session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    stats_md = sess.get("stats_md")
    open_text = sess.get("open_text", {})

    if not all([plan, rows, stats_md]):
        raise HTTPException(status_code=400, detail="请先完成统计计算")
    if not DIFY_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")

    async def generate():
        try:
            writer_query = _build_writer_query(stats_md, open_text, plan, rows[0])
            answer_chunks: list[str] = []
            final_conv_id = ""

            async for chunk, conv_id in sse_dify_stream(writer_query, session_id, "", DIFY_ANALYST_KEY):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    final_conv_id = conv_id

            full_report = "".join(answer_chunks)
            drifted = survey_stats.find_numbers_not_in_stats(full_report, stats_md)
            if drifted:
                print(f"[stats] WARN drifted numbers: {drifted[:20]}")

            full_report = _inject_disclaimer(full_report)
            sess["report_md"] = full_report
            sess["analyst_conv_id"] = final_conv_id
            sess["rows_fed"] = False

            # 自动保存历史
            save_to_history(session_id, sess)

            yield sse_event({"type": "report_done", "report_md": full_report})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── QA（SSE）────────────────────────────────────────

class QARequest(BaseModel):
    session_id: str
    question: str


@app.post("/api/qa")
async def qa(req: QARequest):
    sess = get_session(req.session_id)
    analyst_conv_id = sess.get("analyst_conv_id", "")
    rows = sess.get("rows", [])
    plan = sess.get("plan", {})
    rows_fed = sess.get("rows_fed", False)

    if not analyst_conv_id:
        raise HTTPException(status_code=400, detail="请先生成报告")
    if not DIFY_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")

    async def generate():
        try:
            if not rows_fed and rows:
                rows_block = _format_rows_for_qa(rows, plan)
                qa_query = f"<rows>\n{rows_block}\n</rows>\n\n用户问题: {req.question}"
            else:
                qa_query = req.question

            answer_chunks: list[str] = []
            new_conv_id = analyst_conv_id

            async for chunk, conv_id in sse_dify_stream(
                qa_query, req.session_id, analyst_conv_id, DIFY_ANALYST_KEY
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    new_conv_id = conv_id

            sess["analyst_conv_id"] = new_conv_id or analyst_conv_id
            sess["rows_fed"] = True
            yield sse_event({"type": "qa_done", "answer": "".join(answer_chunks)})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 历史 QA（从历史条目恢复会话）────────────────────

class HistoryQARequest(BaseModel):
    history_id: str
    question: str


@app.post("/api/history-qa")
async def history_qa(req: HistoryQARequest):
    """从历史记录中续聊 QA（无行数据，直接使用 analyst conv_id）。"""
    history = _load_history()
    entry = next((h for h in history if h["id"] == req.history_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")

    analyst_conv_id = entry.get("analyst_conv_id", "")
    if not analyst_conv_id:
        raise HTTPException(status_code=400, detail="该历史记录没有可续聊的对话")
    if not DIFY_ANALYST_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")

    async def generate():
        try:
            answer_chunks: list[str] = []
            new_conv_id = analyst_conv_id
            async for chunk, conv_id in sse_dify_stream(
                req.question, req.history_id, analyst_conv_id, DIFY_ANALYST_KEY
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    new_conv_id = conv_id

            # 更新历史中的 conv_id
            for h in history:
                if h["id"] == req.history_id:
                    h["analyst_conv_id"] = new_conv_id or analyst_conv_id
                    break
            _save_history(history)

            yield sse_event({"type": "qa_done", "answer": "".join(answer_chunks)})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 导出 ────────────────────────────────────────────

def _make_download_response(data: bytes, mime: str, filename: str) -> StreamingResponse:
    """构建带 RFC 5987 编码文件名的下载响应。"""
    encoded = quote(filename)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded}",
        "Content-Length": str(len(data)),
    }
    return StreamingResponse(io.BytesIO(data), media_type=mime, headers=headers)


@app.get("/api/export/word/{session_id}")
async def export_word(session_id: str):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    report_md = _prep_export_md(report_md)
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)

    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe}.docx",
    )


@app.get("/api/export/markdown/{session_id}")
async def export_markdown(session_id: str):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    report_md = _prep_export_md(report_md)
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    return _make_download_response(report_md.encode("utf-8"), "text/markdown; charset=utf-8", f"{safe}.md")


@app.get("/api/export/word-history/{history_id}")
async def export_word_history(history_id: str):
    history = _load_history()
    entry = next((h for h in history if h["id"] == history_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    report_md = _prep_export_md(entry.get("report_md", ""))
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe}.docx",
    )


# ── 飞书 OAuth 登录 + 文档导出 ────────────────────────

# Web 登录态（内存；重启失效，适合内部工具）
web_logins: dict[str, dict] = {}
oauth_states: dict[str, dict] = {}
COOKIE_NAME = "fs_sess"


def _extract_core_lines(md: str) -> list[str]:
    """取出 <!--CORE_START-->..<!--CORE_END--> 之间的内容行（供飞书高亮块用）。"""
    if not md or CORE_START not in md:
        return []
    try:
        seg = md.split(CORE_START, 1)[1].split(CORE_END, 1)[0]
    except IndexError:
        return []
    return [ln for ln in seg.split("\n") if ln.strip()]


async def _current_login(request: Request) -> dict | None:
    """从 cookie 取登录态；token 临过期则尝试刷新。返回 None 表示未登录。"""
    sid = request.cookies.get(COOKIE_NAME, "")
    login = web_logins.get(sid)
    if not login:
        return None
    if login.get("exp", 0) < time.time() + 120 and login.get("refresh"):
        try:
            fresh = await feishu_export.refresh_token(login["refresh"])
            login.update(fresh)
        except Exception:
            return None
    return login


@app.get("/api/feishu/login")
async def feishu_login(next: str = "/"):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）")
    state = secrets.token_urlsafe(16)
    oauth_states[state] = {"next": next, "ts": time.time()}
    # 清理过期 state
    now = time.time()
    for k in list(oauth_states.keys()):
        if oauth_states[k]["ts"] + 600 < now:
            del oauth_states[k]
    return RedirectResponse(feishu_export.build_authorize_url(state))


@app.get("/api/feishu/callback")
async def feishu_callback(code: str = "", state: str = ""):
    st = oauth_states.pop(state, None)
    if not st:
        raise HTTPException(status_code=400, detail="state 无效或已过期，请重新登录")
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权 code")
    try:
        login = await feishu_export.exchange_code(code)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"飞书授权失败：{e}")
    sid = secrets.token_urlsafe(24)
    web_logins[sid] = login
    resp = RedirectResponse(st.get("next") or "/")
    secure = feishu_export.FEISHU_REDIRECT_URI.startswith("https://")
    resp.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax", secure=secure, max_age=7 * 24 * 3600)
    return resp


@app.get("/api/feishu/me")
async def feishu_me(request: Request):
    login = await _current_login(request)
    return {
        "configured": feishu_export.is_configured(),
        "logged_in": bool(login),
        "name": (login or {}).get("name", ""),
    }


@app.post("/api/feishu/logout")
async def feishu_logout(request: Request):
    sid = request.cookies.get(COOKIE_NAME, "")
    web_logins.pop(sid, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


async def _export_to_feishu(report_md: str, login: dict) -> str:
    """把报告 markdown 导成飞书文档（归登录用户），尽力做核心结论高亮块。返回 url。"""
    full = _inject_disclaimer(report_md)
    title_m = re.search(r"^#\s+(.+?)$", full, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    core_lines = _extract_core_lines(full)
    import_md = _strip_core_markers(full)
    url, doc_token = await feishu_export.create_doc_as_user(title, import_md, login["token"])
    if core_lines:
        try:
            await feishu_export.apply_core_callout(doc_token, login["token"], core_lines)
        except Exception:
            pass
    return url


@app.post("/api/export/feishu/{session_id}")
async def export_feishu(session_id: str, request: Request):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用")
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")
    try:
        url = await _export_to_feishu(report_md, login)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生成飞书文档失败：{e}")
    return {"url": url}


@app.post("/api/export/feishu-history/{history_id}")
async def export_feishu_history(history_id: str, request: Request):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用")
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    history = _load_history()
    entry = next((h for h in history if h["id"] == history_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    try:
        url = await _export_to_feishu(entry.get("report_md", ""), login)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"生成飞书文档失败：{e}")
    return {"url": url}


# ── Prompts 管理 ─────────────────────────────────────

@app.get("/api/upload-guide")
async def get_upload_guide():
    prompts = _load_prompts()
    return {"content": prompts.get("upload_guide", {}).get("current", "")}


@app.get("/api/prompts")
async def get_prompts():
    return _load_prompts()


class PromptUpdateRequest(BaseModel):
    content: str
    note: str = ""


@app.put("/api/prompts/{key}")
async def update_prompt(key: str, req: PromptUpdateRequest):
    prompts = _load_prompts()
    if key not in prompts:
        raise HTTPException(status_code=404, detail=f"prompt '{key}' 不存在")
    p = prompts[key]
    if not p.get("editable", False):
        raise HTTPException(status_code=403, detail="该 Prompt 在 Dify 后台管理，不可在此修改")

    # 把当前版本存入历史
    p["history"].insert(0, {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content": p["current"],
        "note": req.note or "（未填写修改说明）",
    })
    p["history"] = p["history"][:20]  # 保留最近 20 条
    p["current"] = req.content
    _save_prompts(prompts)
    return {"ok": True, "key": key}


# ── 历史记录 ──────────────────────────────────────────

@app.get("/api/history")
async def get_history():
    history = _load_history()
    # 列表视图不返回完整 report_md（节省带宽）
    return [
        {
            "id": h["id"],
            "filename": h["filename"],
            "title": h["title"],
            "created_at": h["created_at"],
            "has_qa": bool(h.get("analyst_conv_id")),
        }
        for h in history
    ]


@app.get("/api/history/{hist_id}")
async def get_history_item(hist_id: str):
    history = _load_history()
    entry = next((h for h in history if h["id"] == hist_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    return entry


# ── 启动 ────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
