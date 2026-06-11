"""问卷分析平台 Web 后端 v2

新增：
- 本地题型推断 + 用户确认（Step 2）
- Prompt 管理（可编辑 + 版本历史）
- 历史记录（最近 5 条）
- Word 下载修复（RFC 5987）
"""

import asyncio
import base64
import csv
import html
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import quote
from urllib.request import urlopen

import openpyxl
import markdown as markdown_lib
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

import annotate
import crosstab_parser
import feishu_export
import survey_plan
import survey_stats
from dify import chat as dify_chat  # noqa: F401 (kept for compatibility)

DIFY_API_BASE      = os.getenv("DIFY_API_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_PLANNER_KEY   = os.getenv("DIFY_PLANNER_KEY", "")
DIFY_ANALYST_KEY   = os.getenv("DIFY_ANALYST_KEY", "")
DIFY_COLUMN_KEY    = os.getenv("DIFY_COLUMN_KEY", "")      # 题型识别
DIFY_AI_DETECT_KEY       = os.getenv("DIFY_AI_DETECT_KEY", "")        # AI 作答识别
DIFY_QUALITY_KEY         = os.getenv("DIFY_QUALITY_KEY", "")          # 回答质量打标
DIFY_THEME_EXTRACT_KEY   = os.getenv("DIFY_THEME_EXTRACT_KEY", "")    # 大样本-主题提取
DIFY_THEME_MERGE_KEY     = os.getenv("DIFY_THEME_MERGE_KEY", "")      # 大样本-主题合并
DIFY_CLASSIFY_KEY        = os.getenv("DIFY_CLASSIFY_KEY", "")         # 大样本-回复分类
DIFY_LARGE_ANALYST_KEY   = os.getenv("DIFY_LARGE_ANALYST_KEY", "")    # 报告撰写助手（大样本版）
DIFY_CROSSTAB_PLANNER_KEY = os.getenv("DIFY_CROSSTAB_PLANNER_KEY", "")  # 跑数表模式-章节大纲策划

# 大样本分析阈值：开放题总回复数超过此值时自动启用批处理模式
LARGE_SAMPLE_THRESHOLD = 500
BATCH_SIZE = 300  # 每批发给 Dify 的回复数量
OTHER_THEME_PCT = 5.0  # 占比低于此值的主题合并入「其他声音」
DIFY_BASE_URL      = os.getenv("DIFY_API_BASE", "https://dify.web.moontontech.net/v1")
# 用于前端展示 Dify 后台入口（去掉 /v1 后缀）
DIFY_CONSOLE_URL = re.sub(r"/v1$", "", DIFY_BASE_URL)

def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        return default


def _env_csv_set(name: str, *, lower: bool = False) -> set[str]:
    vals = []
    for item in os.getenv(name, "").replace(";", ",").split(","):
        item = item.strip()
        if item:
            vals.append(item.lower() if lower else item)
    return set(vals)


FEISHU_LOGIN_REQUIRED = _env_bool("FEISHU_LOGIN_REQUIRED", False)
FEISHU_ALLOWED_EMAILS = _env_csv_set("FEISHU_ALLOWED_EMAILS", lower=True)
FEISHU_ADMIN_EMAILS   = _env_csv_set("FEISHU_ADMIN_EMAILS",   lower=True)
FEISHU_SESSION_DAYS = max(1, _env_int("FEISHU_SESSION_DAYS", 7))
FEISHU_SESSION_SECONDS = FEISHU_SESSION_DAYS * 24 * 3600
COOKIE_NAME = "fs_sess"

# ── 数据目录 ──────────────────────────────────────────────────
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(DATA_DIR, exist_ok=True)

PROMPTS_FILE   = os.path.join(DATA_DIR, "prompts.json")
WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
WEB_LOGINS_FILE = os.path.join(DATA_DIR, "web_logins.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
AUDIT_LOG_FILE = os.path.join(DATA_DIR, "audit_logs.json")
MAX_HISTORY  = 5
MAX_AUDIT_LOGS = max(200, _env_int("AUDIT_LOG_MAX", 5000))

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
   本次调研共收集 N 份有效回复。（**第一行必须写明样本总数，从 `<stats>` 里的总行数取值**）
   ### 总体判断
   （用 1 段话概括本次调研最重要的总体方向、满意/不满的核心矛盾、最需要产品关注的决策点）
   ### Part X 章节名：关键发现
   - **短标题**：结论 + 原因/证据 + 必要案例。
   ### 高信号少数观点与风险
   - **短标题**：仅写高业务风险、强烈情绪、明确案例、功能异常、流失风险或设计决策价值的少数观点；没有则省略本小节。
   ### 待确认问题概述
   - 仅当正文有 `## Bug 或待确认问题` 模块时写；没有则省略本小节。
   <!--CORE_END-->
   `<!--CORE_START-->` 必须在 `## 核心结论` 这一行的正上方、`<!--CORE_END-->` 在核心结论结束后另起一行，两个标记各自独占一行。
   核心结论各条要点的写作规则：
   - **不使用百分比，也不使用精确人数**，改用笼统的量级描述（例：「38 名受访者中，绝大多数人认为…」「少数玩家提到…」「近半数受访者…」），总样本数可在首行已说明的基础上引用
   - 采用「混合结构」：先写 `### 总体判断`，再按报告 Part 逐组写 `### Part X 章节名：关键发现`，最后按需写 `### 高信号少数观点与风险` 与 `### 待确认问题概述`；不要把所有内容堆成一整段或一个超长列表。
   - Part 小节标题必须引用正文/plan 里的真实 Part 标题，例如 `### Part 1 子播报区体验反馈：关键发现`、`### Part 2 新勋章设计评价：关键发现`；严禁只写 `Part 1 关键发现`、`Part 2 关键发现` 这种无法判断内容的泛化标题。
   - 每条要点必须使用「**短标题**：结论 + 原因/证据 + 必要案例」的格式；短标题要直接点明主题，后文再说明为什么重要。
   - 每条要点须完整呈现该观点的**核心内容、主要原因和关键逻辑**（从 `<open_text>` 充分归纳），要求读完核心结论后无需再查阅正文详情也能全面了解玩家想法；**信息完整、严谨、置信是最高优先级**，可读性通过分组、短标题和加粗重点解决，而不是删减关键信息。
   - 涉及主观题的结论要点，须归纳玩家的**多元观点和核心理由**，不只说「支持」或「反对」，要说清楚支持/反对的具体原因和逻辑
   - 少数玩家反馈只要具备高业务风险、强烈情绪、明确案例、功能异常、流失风险或设计决策价值，就必须进入核心结论，不能因人数少而省略；但普通偏好、泛泛建议、无具体依据的情绪抱怨不用强行写入。
   - 玩家提供了明确案例时，核心结论必须适当概述案例，不需要逐字复述，但要保留关键信息（例：「某玩家反馈 Lolita 98% 坦克成就只进入副播报，被击杀播报挤占主播报位置，并表达流失风险」）。
   - 每个 Part 至少覆盖 1 条关键发现；如果某 Part 内有多个决策价值很高的分歧或风险，可写 2–3 条，不要为了控制条数遗漏重点。
   - 若报告末尾包含 `## Bug 或待确认问题` 模块，则核心结论最后必须追加 `### 待确认问题概述`，只概述有哪些问题类型需要确认，不展开玩家原文；若正文没有该模块，则核心结论不要写任何待确认问题相关小节。
3. 之后严格按 plan 给的 parts 顺序划分章节，每个 part 用 `## Part X 章节名` 二级标题
4. 每个 part 章节**紧接标题之后**，先写一段「本节总结」：用连贯的段落文字（不用列表）综合该 part 所有题目的客观统计结果与主观观点，要求读完这一段即可完整了解该 part 的全部发现——包括关键数据（绝对数值）、玩家态度分布、主要正面/负面/中立观点及其核心逻辑；**文字详尽，长度不限**。总结段落之后再按题目逐一展开详细内容。
5. `<stats>` 块里所有数字、百分比、表格已经算好——**严禁修改、重新计算、合并、四舍五入**。你写到报告里的所有数字必须能在 `<stats>` 里逐字找到（核心结论绝对数值也必须与 `<stats>` 一致）
6. 主观题归纳：从 `<open_text>` 块里找该 part 内的开放题原文，先按具体题目展开，**每个具体题目必须使用 `### 题目名` 三级标题**；该题目下再按玩家态度倾向分组，分组标题必须使用 `#### 正面观点` / `#### 负面观点` / `#### 中立 / 建议` 四级标题（无相关内容的类别可省略）。严禁把「正面观点 / 负面观点 / 中立 / 建议」写成与题目平级的 `###`。在每个分组下，必须按「观点」逐条展开，**禁止**把多个观点合并成一个「观点主题 / 代表性原话」的大表。每个观点使用固定结构：先写 `**观点：观点短标题**`，下一行写 `提及情况：...`（可用「多名玩家」「少数玩家」「个别玩家」这类定性量级，避免编造精确人数），再写 `代表性原话：`，其下用一个小表格列出该观点对应的 1–5 条玩家信息与原话/中文翻译。ID 展示规则固定如下：如果 `<open_text>` 前缀里只有 `MLBBID=...`，表格表头写 `MLBBID` 且单元格只放 ID 值；如果只有 `玩家ID=...`，表头写 `玩家ID` 且单元格只放 ID 值；如果两者都有，表格必须同时有 `玩家ID` 和 `MLBBID` 两列。原文/翻译列可写 `玩家原文` 或 `中文翻译`，但必须与该观点逐条对应。严禁在单元格里写 `MLBB ID:xxx`、`玩家ID:xxx` 这类前缀。画像信息如需展示，必须单独放在 `玩家信息` 或 `画像信息` 列，不得混入 ID 列。
7. 关于「画像/人群结构」：仅当 `<stats>` 里有「画像维度概览」时才写人群结构相关内容，且要用**大白话**描述（例：「参与玩家以神话段位为主，约占四成」），**不要直接堆字段名/列名**；若 `<stats>` 里没有画像维度概览，则**整篇报告不要出现任何画像/人群结构章节或描述**
8. 在所有 Part 内容结束后，通览 `<open_text>` 全部开放反馈，判断是否需要追加 `## Bug 或待确认问题` 模块：
   - 仅当确实发现疑似功能 bug、体验异常、规则不明确、玩家无法判断是否设计如此的问题时才写该模块；如果没有相关线索，**完全省略该模块**，不要写“未发现”或任何占位说明。
   - 优先识别：功能不可用、报错、卡死、丢失、数值/奖励异常、匹配/结算/账号/支付/道具异常、规则描述不清、玩家明确表达“不确定是不是设计如此”。
   - 排除纯情绪抱怨、泛泛建议、平衡性偏好；除非反馈中包含明确异常线索。
   - 模块必须使用 Markdown 表格，字段固定为：`问题类型`、`待确认问题`、`玩家信息`、`玩家原文翻译`，不得出现 `确认建议` 列。
   - `问题类型` 必须使用短标签，避免窄列难读，例如：`奖励异常`、`规则不清`、`显示异常`、`账号问题`、`支付异常`、`匹配异常`；不要写长句。
   - `玩家原文翻译` 必须把玩家原文翻译为中文；如确需保留原文，只能在同一单元格括号中简短补充，不新增列。
   - 多名玩家反馈同一问题时合并为一行，`玩家信息` 和 `玩家原文翻译` 保留最有代表性的 1–3 条；玩家信息使用 `<open_text>` 中已有的 ID 和画像信息前缀，但 ID 值不要写成 `MLBB ID:xxx` 这种累赘格式。
9. 不要复制 `<stats>` 整块，但可以原样引用其中的表格\
"""

# 报告免责声明（确定性插入到标题下方，不依赖 LLM）
REPORT_DISCLAIMER = "> 该报告使用智能调研分析工具产出，如有疑问，请联系开发者@宋润佳(Nancy)"
# 定性模式完整免责声明（倍市得/crosstab 模式不插此段，用 skip_qual=True 跳过）
QUALITATIVE_DISCLAIMER = "> 该调研为定性调研，报告中所有涉及打分、统计的数据仅作为参考，不具备定量意义，也无法与用研的满意度定量评分对比，同时不适用于定量分数的评价体系。请阅读者重点关注玩家的主观反馈内容。"
# 核心结论包裹标记（writer 按要求输出，飞书导出时据此定位转高亮块）
CORE_START = "<!--CORE_START-->"
CORE_END = "<!--CORE_END-->"


def _inject_disclaimer(md: str, skip_qual: bool = False) -> str:
    """在第一行 `# 标题` 之后插入免责声明引用行；幂等；无 H1 则插到最前。

    skip_qual=True（倍市得/crosstab 模式）：
      - 只插短的 REPORT_DISCLAIMER
      - 主动清除报告里若有的旧定性免责声明（历史报告兼容）
    skip_qual=False（定性模式，默认）：
      - 同时插 REPORT_DISCLAIMER + QUALITATIVE_DISCLAIMER
    """
    if not md:
        return md

    # crosstab 模式：主动清除旧定性免责声明（兼容历史报告）
    if skip_qual and QUALITATIVE_DISCLAIMER in md:
        md = md.replace("\n" + QUALITATIVE_DISCLAIMER, "").replace(QUALITATIVE_DISCLAIMER + "\n", "")

    has_report = REPORT_DISCLAIMER in md
    has_qual = QUALITATIVE_DISCLAIMER in md

    # 已经齐备就不再插
    if has_report and (skip_qual or has_qual):
        return md

    lines = md.split("\n")

    # 已有 REPORT_DISCLAIMER 但缺 QUALITATIVE_DISCLAIMER（仅定性模式可能触发）
    if has_report and not has_qual and not skip_qual:
        for i, ln in enumerate(lines):
            if ln.strip() == REPORT_DISCLAIMER:
                lines.insert(i + 1, QUALITATIVE_DISCLAIMER)
                return "\n".join(lines)

    for i, ln in enumerate(lines):
        if ln.startswith("# ") and not ln.startswith("## "):
            lines.insert(i + 1, "")
            insert_at = i + 2
            if not has_report:
                lines.insert(insert_at, REPORT_DISCLAIMER)
                insert_at += 1
            if not has_qual and not skip_qual:
                lines.insert(insert_at, QUALITATIVE_DISCLAIMER)
            return "\n".join(lines)

    # 没有 H1：插到最前
    prefix = []
    if not has_report:
        prefix.append(REPORT_DISCLAIMER)
    if not has_qual and not skip_qual:
        prefix.append(QUALITATIVE_DISCLAIMER)
    return "\n".join(prefix) + "\n\n" + md


def _strip_core_markers(md: str) -> str:
    """移除核心结论包裹标记行（仅飞书导出用于定位高亮块，其它导出不应出现）。"""
    if not md:
        return md
    return "\n".join(ln for ln in md.split("\n") if ln.strip() not in (CORE_START, CORE_END))


def _prep_export_md(md: str, skip_qual: bool = False) -> str:
    """通用导出前处理：补免责声明（幂等）+ 去掉核心结论标记。"""
    return _strip_core_markers(_inject_disclaimer(md, skip_qual=skip_qual))

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
        "version": 9,  # 改了默认值就 +1：未被用户编辑过的会自动升级
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
# Session 管理（文件持久化，支持多 worker）
#
# 每个 session 存为 data/sessions/<uuid>.json。
# 写入走 tmp + os.replace 保证原子性，多进程安全。
# annotate_sessions 仍用内存（标注会话生命周期短，不跨请求保活）。
# ============================================================

_SESSION_DIR = Path("data") / "sessions"
SESSION_TTL = 7200  # 2 小时，用于 sweep


def _session_path(sid: str) -> Path:
    # 校验 sid 格式，防止路径穿越
    if not re.match(r"^[0-9a-f\-]{32,36}$", sid):
        raise HTTPException(status_code=400, detail="无效的会话 ID")
    return _SESSION_DIR / f"{sid}.json"


def _sweep_old_sessions() -> None:
    """启动时清理过期文件，避免 data/sessions/ 无限增长。"""
    if not _SESSION_DIR.exists():
        return
    cutoff = time.time() - SESSION_TTL
    for p in _SESSION_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
        except Exception:
            pass


def new_session() -> str:
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    sid = str(uuid.uuid4())
    _write_session(sid, {"ts": time.time()})
    return sid


def get_session(sid: str) -> dict:
    p = _session_path(sid)
    if not p.exists():
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新上传文件")
    with open(p, "r", encoding="utf-8") as f:
        sess = json.load(f)
    # JSON 不支持整数 key，恢复 open_text / crosstab_questions 的 int key
    for field in ("open_text", "crosstab_questions"):
        if isinstance(sess.get(field), dict):
            sess[field] = {int(k): v for k, v in sess[field].items()}
    sess["ts"] = time.time()
    return sess


def save_session(sid: str, sess: dict) -> None:
    """显式持久化 session。所有写操作后必须调用。"""
    sess["ts"] = time.time()
    _write_session(sid, sess)


def _write_session(sid: str, sess: dict) -> None:
    _SESSION_DIR.mkdir(parents=True, exist_ok=True)
    p = _session_path(sid)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sess, f, ensure_ascii=False)
    os.replace(tmp, p)  # 原子替换，Windows 上 os.replace 也是原子的


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


def _history_no_value(report_no: str) -> int:
    m = re.match(r"^R-(\d+)$", str(report_no or "").strip())
    return int(m.group(1)) if m else 0


def _ensure_history_report_numbers(history: list, *, save: bool = True) -> list:
    if not history:
        return history
    dirty = False
    used = {_history_no_value(h.get("report_no", "")) for h in history}
    used.discard(0)
    next_no = max(used or {0}) + 1
    missing = [h for h in history if not h.get("report_no")]
    missing.sort(key=lambda h: h.get("created_at", ""))
    for h in missing:
        while next_no in used:
            next_no += 1
        h["report_no"] = f"R-{next_no:03d}"
        used.add(next_no)
        dirty = True
    if dirty and save:
        _save_history(history)
    return history


def _next_history_report_no(history: list) -> str:
    _ensure_history_report_numbers(history, save=False)
    max_no = max((_history_no_value(h.get("report_no", "")) for h in history), default=0)
    return f"R-{max_no + 1:03d}"


def _qa_user_count(entry: dict) -> int:
    return sum(1 for m in entry.get("qa_messages", []) if m.get("role") == "user")


def _owner_from_login(login: dict | None) -> dict:
    login = login or {}
    email = str(login.get("email", "")).strip().lower()
    open_id = str(login.get("open_id", "")).strip()
    if email:
        owner_key = f"email:{email}"
    elif open_id:
        owner_key = f"open_id:{open_id}"
    else:
        owner_key = ""
    return {
        "owner_key": owner_key,
        "owner_email": email,
        "owner_open_id": open_id,
        "owner_name": str(login.get("name", "")).strip(),
    }


def _history_owner_key(entry: dict | None) -> str:
    entry = entry or {}
    owner_key = str(entry.get("owner_key", "")).strip()
    if owner_key:
        return owner_key
    email = str(entry.get("owner_email", "")).strip().lower()
    if email:
        return f"email:{email}"
    open_id = str(entry.get("owner_open_id", "")).strip()
    if open_id:
        return f"open_id:{open_id}"
    return ""


def _visible_to_owner(item: dict | None, login: dict | None) -> bool:
    if not FEISHU_LOGIN_REQUIRED:
        return True
    viewer_key = _owner_from_login(login).get("owner_key", "")
    item_key = _history_owner_key(item)
    return bool(viewer_key and item_key and viewer_key == item_key)


def _assign_session_owner(sess: dict, login: dict | None) -> None:
    if not sess.get("owner_key"):
        sess.update(_owner_from_login(login))


def _find_history_for_login(history: list, hist_id: str, login: dict | None) -> dict | None:
    entry = next((h for h in history if h.get("id") == hist_id), None)
    if not entry or not _visible_to_owner(entry, login):
        return None
    return entry


def _trim_history_for_owner(history: list, owner_key: str) -> list:
    if not owner_key:
        return history[:MAX_HISTORY]
    seen = 0
    kept = []
    for entry in history:
        if _history_owner_key(entry) == owner_key:
            seen += 1
            if seen > MAX_HISTORY:
                continue
        kept.append(entry)
    return kept


AUDIT_FEATURES = [
    {"key": "auth", "label": "\u767b\u5f55\u4e0e\u8d26\u53f7"},
    {"key": "survey", "label": "\u95ee\u5377\u5206\u6790"},
    {"key": "quant", "label": "\u5b9a\u91cf\u5206\u6790"},
    {"key": "annotate", "label": "\u6570\u636e\u6807\u6ce8"},
    {"key": "report", "label": "\u62a5\u544a\u5bfc\u51fa\u4e0e\u8ffd\u95ee"},
    {"key": "settings", "label": "\u5e73\u53f0\u8bbe\u7f6e"},
    {"key": "admin", "label": "\u6743\u9650\u7ba1\u7406"},
]
AUDIT_FEATURE_LABELS = {item["key"]: item["label"] for item in AUDIT_FEATURES}


def _load_audit_logs() -> list[dict]:
    if not os.path.exists(AUDIT_LOG_FILE):
        return []
    try:
        with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("logs", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_audit_logs(logs: list[dict]) -> None:
    with open(AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs[:MAX_AUDIT_LOGS], f, ensure_ascii=False, indent=2)


def _client_ip(request: Request | None) -> str:
    if not request:
        return ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _audit_user(login: dict | None) -> dict:
    login = login or {}
    email = str(login.get("email", "")).strip().lower()
    open_id = str(login.get("open_id", "")).strip()
    user_key = f"email:{email}" if email else (f"open_id:{open_id}" if open_id else "anonymous")
    return {
        "user_key": user_key,
        "user_email": email,
        "user_name": str(login.get("name", "")).strip(),
        "open_id": open_id,
    }


def _append_audit_log(entry: dict) -> None:
    try:
        logs = _load_audit_logs()
        logs.insert(0, entry)
        _save_audit_logs(logs)
    except Exception as exc:
        print(f"[audit] write failed: {exc}", flush=True)


def _audit_log_from_login(
    request: Request | None,
    login: dict | None,
    feature: str,
    action: str,
    detail: str = "",
    *,
    status: str = "success",
    metadata: dict | None = None,
) -> None:
    entry = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "feature": feature,
        "feature_label": AUDIT_FEATURE_LABELS.get(feature, feature),
        "action": action,
        "detail": str(detail or "")[:1000],
        "status": status,
        "ip": _client_ip(request),
        "metadata": metadata or {},
        **_audit_user(login),
    }
    _append_audit_log(entry)


async def audit_log(
    request: Request | None,
    feature: str,
    action: str,
    detail: str = "",
    *,
    status: str = "success",
    metadata: dict | None = None,
) -> None:
    try:
        login = await _current_login(request) if request else None
        _audit_log_from_login(request, login, feature, action, detail, status=status, metadata=metadata)
    except Exception as exc:
        print(f"[audit] collect failed: {exc}", flush=True)


def _admin_user_rows() -> list[dict]:
    users = _load_whitelist()
    result = []
    for u in users:
        e = u.get("email", "").lower()
        result.append({
            "email": e,
            "perms": u.get("perms", ["survey", "annotate"]),
            "enabled": u.get("enabled", True),
            "is_admin": False,
        })
    admin_emails = set(FEISHU_ADMIN_EMAILS) | set(FEISHU_ALLOWED_EMAILS)
    existing = {u["email"] for u in result}
    for e in sorted(admin_emails):
        if e not in existing:
            result.insert(0, {"email": e, "perms": ["survey", "annotate"], "enabled": True, "is_admin": True})
        else:
            for u in result:
                if u["email"] == e:
                    u["is_admin"] = True
    return result


def _short_text(value: str, limit: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."



def _sanitize_report_title(title: str) -> str:
    cleaned = re.sub(r"[\r\n\t]+", " ", str(title or "")).strip()
    cleaned = re.sub(r'[\\/:*?"<>|]', "_", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="报告名称不能为空")
    return cleaned[:120]


def _replace_report_h1(report_md: str, title: str) -> str:
    report_md = report_md or ""
    if re.search(r"^#\s+.+?$", report_md, re.MULTILINE):
        return re.sub(r"^#\s+.+?$", f"# {title}", report_md, count=1, flags=re.MULTILINE)
    return f"# {title}\n\n{report_md.lstrip()}"


def save_to_history(session_id: str, sess: dict) -> None:
    report_md = sess.get("report_md", "")
    if not report_md:
        return
    history = _load_history()
    _ensure_history_report_numbers(history, save=False)
    old_entry = next((h for h in history if h.get("id") == session_id), None)
    qa_messages = sess.get("qa_messages")
    if qa_messages is None and old_entry:
        qa_messages = old_entry.get("qa_messages", [])
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "未命名报告"
    owner = {
        "owner_key": sess.get("owner_key") or (old_entry or {}).get("owner_key", ""),
        "owner_email": sess.get("owner_email") or (old_entry or {}).get("owner_email", ""),
        "owner_open_id": sess.get("owner_open_id") or (old_entry or {}).get("owner_open_id", ""),
        "owner_name": sess.get("owner_name") or (old_entry or {}).get("owner_name", ""),
    }
    entry = {
        "id": session_id,
        "report_no": old_entry.get("report_no") if old_entry else _next_history_report_no(history),
        "filename": sess.get("filename", "unknown"),
        "title": title,
        "created_at": old_entry.get("created_at") if old_entry else datetime.now().isoformat(),
        "report_md": report_md,
        "plan": sess.get("plan"),
        "stats_md": sess.get("stats_md"),
        "analyst_conv_id": sess.get("analyst_conv_id", ""),
        "qa_messages": qa_messages or [],
        "rows_fed": True,  # 历史 QA 跳过投喂 rows（对话已包含上下文）
        "mode": sess.get("mode", ""),  # 保存模式，飞书导出时据此判断 skip_qual
        **owner,
    }
    history = [h for h in history if h["id"] != session_id]
    history.insert(0, entry)
    history = _trim_history_for_owner(history, owner.get("owner_key", ""))
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
- 含多个选项、可多选 → multi_choice，并尽量给出 options 清单
- 长文本主观回答 → open_text
- 标注【疑似矩阵题】的，按子项判断 matrix_scale（每子项打分）或 matrix_multi（每子项可多选），rows 用给出的子项标签

同义归并（value_aliases，重要）：
- 仅对 single_choice / profile_dim / multi_choice / matrix_multi 这类「选项题」给出。
- 我已在每列附上「去重取值」。请把**语义相同但写法/语种不同**的取值（如 神话/Mythic/Mítica，中国/China/CN）归并到**同一个中文标准值**：key=中文标准值，value=该标准值对应的所有原始变体（含中文标准值本身可不必重复列出）。
- options 使用这些合并后的中文标准值；中文标准值可以不直接出现在原始数据里，但必须能由 value_aliases 中的真实取值支撑。
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
        if role in ("single_choice", "profile_dim", "multi_choice", "matrix_multi") and q.get("options"):
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

    # 检测是否存在画像维度列，生成对应的画像约束指令
    profile_dims = [q for q in confirmed_columns if (q.get("role") or q.get("confirmed_type")) == "profile_dim"]
    if not profile_dims:
        profile_constraint = (
            "\n⚠️ 画像约束（严格执行）：本问卷中用户**没有将任何题目标注为画像维度**。\n"
            "- cross_tabs 数组**必须为空** []\n"
            "- open_questions **不得**建议将任何题目用作用户画像或分组维度\n"
            "- 报告不应包含任何「用户画像」/「人群结构」分析章节\n"
        )
    else:
        dim_names = "、".join(
            f"「{q.get('name_zh') or q.get('name') or '?'}」" for q in profile_dims
        )
        profile_constraint = (
            f"\n画像维度约束：本问卷的画像维度列为 {dim_names}。"
            f"cross_tabs 的 profile_index **只能**使用上述列对应的列号，不得使用其他单选题做交叉分析。\n"
        )

    return (
        f"{sample_md}\n\n"
        f"{confirmed_block}\n\n"
        f"重要：以上题型和选项已由用户在界面中逐一确认；选择题选项必须以 <confirmed_column_types> 中的「选项」为权威，不得根据题干、表头或样本重新猜测选项，也不得围绕已确认选项再次提问。\n"
        f"注意：以上题型已由用户在界面中逐一确认，**不得**在 open_questions 中再次对题型进行发问。"
        f"矩阵题的多个列号务必整体归入同一个 part。"
        f"{profile_constraint}\n"
        f"{extra_instructions}"
    )


def _build_plan_revision_query(plan: dict, headers: list[str], confirmed_columns: list[dict], user_text: str) -> str:
    header_lines = "\n".join(f"- 列{i}: {h}" for i, h in enumerate(headers))
    confirmed_json = json.dumps(confirmed_columns or [], ensure_ascii=False, indent=2)
    plan_json = json.dumps(plan or {}, ensure_ascii=False, indent=2)
    return (
        "你正在修订一份问卷分析方案。请根据用户的修改意见，在当前方案基础上输出一份完整的新 plan JSON。\n\n"
        "严格要求：\n"
        "1. 只能输出一个完整 JSON 对象，不要输出解释、确认语、Markdown 文本或 ```json 围栏外的内容。\n"
        "2. JSON 必须包含 columns、parts、cross_tabs、open_questions 字段，并通过既有 schema 校验。\n"
        "3. columns 必须保留用户已确认的题型、列号、选项、矩阵题分组等权威信息；不要重新猜测题型或选项。\n"
        "4. parts 必须使用实际存在的列号；矩阵题成员列必须整体归入同一个 part。\n"
        "5. 若用户意见只要求调整章节/分析重点，只改 parts、cross_tabs 或 open_questions，不要无故改 columns。\n\n"
        f"<headers>\n{header_lines}\n</headers>\n\n"
        f"<confirmed_columns_json>\n{confirmed_json}\n</confirmed_columns_json>\n\n"
        f"<current_plan_json>\n{plan_json}\n</current_plan_json>\n\n"
        f"<user_revision_request>\n{user_text.strip()}\n</user_revision_request>\n\n"
        "请现在返回修订后的完整 JSON 对象。"
    )


def _build_crosstab_planner_query(
    questionnaire_text: str,
    available_questions: list[str],
    open_question_names: list[str],
) -> str:
    """跑数表模式：给章节策划 planner 的初始 query（任务/输出格式由 Dify 应用 system prompt 定义）。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    avail = "\n".join(f"- {q}" for q in available_questions) or "（无）"
    opens = "\n".join(f"- {q}" for q in open_question_names) or "（无）"
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<available_questions>\n{avail}\n</available_questions>\n\n"
        f"<open_questions_list>\n{opens}\n</open_questions_list>\n\n"
        "请基于以上规划报告章节大纲，按 system prompt 约定的 JSON 格式输出。"
    )


def _build_crosstab_plan_revision_query(
    questionnaire_text: str,
    current_parts: list[dict],
    user_text: str,
) -> str:
    """跑数表模式：章节大纲的修订 query。"""
    q_text = (questionnaire_text or "").strip()
    if len(q_text) > 12000:
        q_text = q_text[:12000] + "\n…（问卷过长，已截断）"
    outline = json.dumps(current_parts or [], ensure_ascii=False, indent=2)
    return (
        f"<questionnaire>\n{q_text}\n</questionnaire>\n\n"
        f"<current_outline>\n{outline}\n</current_outline>\n\n"
        f"<user_request>\n{user_text.strip()}\n</user_request>\n\n"
        "请在当前大纲基础上按用户意见调整，按 system prompt 约定的 JSON 格式输出。"
    )


def _render_crosstab_plan_card(plan: dict) -> str:
    """跑数表模式：把章节大纲 + 待确认问题渲染成给用户/历史看的 markdown。"""
    lines = ["## 报告章节大纲"]
    for i, p in enumerate(plan.get("parts", []), 1):
        scope = p.get("scope", "")
        lines.append(f"{i}. **{p['name']}**" + (f" — {scope}" if scope else ""))
    oqs = plan.get("open_questions") or []
    if oqs:
        lines.append("")
        lines.append("## 待确认问题")
        for q in oqs:
            lines.append(f"- {q}")
    return "\n".join(lines)


async def _batch_qualitative_analysis(
    open_text: dict,
    plan: dict,
    headers: list,
    session_id: str,
):
    """大样本定性分析四阶段批处理。

    异步生成器，yield ("progress", msg) 或 ("result", clustered_themes)。
    clustered_themes 结构：
    {
        col_idx: {
            "col_name": str,
            "total": int,
            "themes": [{"id","name","description","count","percentage",
                        "positive_count","positive_pct","positive_summary",
                        "negative_count","negative_pct","negative_summary",
                        "quotes": [str]}],
            "other_themes": [{"name","count","percentage"}]
        }
    }
    """
    import json as _json
    from dify import workflow_run, STOP_SIGNAL

    clustered_themes: dict = {}

    for col_idx, entries in open_text.items():
        col = next((c for c in plan["columns"] if c["index"] == col_idx), None)
        col_name = (col and col.get("name")) or (
            headers[col_idx] if col_idx < len(headers) else f"列{col_idx}"
        )
        total = len(entries)
        yield ("progress", f"【{col_name}】开始分析（共 {total} 条）")

        # ── Phase A：分批提取主题候选 ──────────────────────────────────────
        batches = [entries[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
        all_candidates: list[dict] = []

        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】提取主题（批次 {bi}/{len(batches)}）")
            responses_text = "\n".join(
                f"[{i}] {e.get('text', '')}" for i, e in enumerate(batch)
            )
            raw = await workflow_run(
                inputs={
                    "question": col_name,
                    "responses": responses_text,
                    "count": len(batch),
                },
                api_key=DIFY_THEME_EXTRACT_KEY,
                log_prefix=f"A col={col_idx} batch={bi}",
            )
            if raw == STOP_SIGNAL:
                yield ("progress", f"【{col_name}】主题提取遇到错误，跳过该列")
                break
            try:
                parsed = _json.loads(raw)
                all_candidates.extend(parsed.get("themes", []))
            except Exception:
                pass

        if not all_candidates:
            continue

        # ── Phase B：合并去重 ──────────────────────────────────────────────
        yield ("progress", f"【{col_name}】合并主题候选（共 {len(all_candidates)} 个）")
        candidates_text = "\n".join(
            f"- {t.get('name', '')}：{t.get('description', '')}"
            for t in all_candidates
        )
        raw_b = await workflow_run(
            inputs={
                "question": col_name,
                "theme_candidates": candidates_text,
                "total_responses": total,
            },
            api_key=DIFY_THEME_MERGE_KEY,
            log_prefix=f"B col={col_idx}",
        )
        if raw_b == STOP_SIGNAL or not raw_b:
            yield ("progress", f"【{col_name}】主题合并失败，跳过该列")
            continue
        try:
            merged = _json.loads(raw_b)
            final_themes: list[dict] = merged.get("themes", [])
        except Exception:
            yield ("progress", f"【{col_name}】主题合并结果解析失败，跳过该列")
            continue

        if not final_themes:
            continue

        theme_list_text = _json.dumps(
            [{"id": t["id"], "name": t["name"], "description": t["description"]}
             for t in final_themes],
            ensure_ascii=False,
        )

        # ── Phase C：回跑分类 ──────────────────────────────────────────────
        # counts[theme_id] = {"total": int, "pos": int, "neg": int, "neutral": int, "mixed": int}
        counts: dict[str, dict] = {t["id"]: {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
                                    for t in final_themes}
        counts["other"] = {"total": 0, "pos": 0, "neg": 0, "neutral": 0, "mixed": 0}
        # quotes_pool[theme_id] = list of (sentiment, text)
        quotes_pool: dict[str, list] = {t["id"]: [] for t in final_themes}

        for bi, batch in enumerate(batches, 1):
            yield ("progress", f"【{col_name}】分类回复（批次 {bi}/{len(batches)}）")
            responses_text = "\n".join(
                f"[{i}] {e.get('text', '')}" for i, e in enumerate(batch)
            )
            raw_c = await workflow_run(
                inputs={
                    "question": col_name,
                    "theme_list": theme_list_text,
                    "responses": responses_text,
                },
                api_key=DIFY_CLASSIFY_KEY,
                log_prefix=f"C col={col_idx} batch={bi}",
            )
            if raw_c == STOP_SIGNAL:
                continue
            try:
                cls_data = _json.loads(raw_c)
            except Exception:
                continue

            for item in cls_data.get("classifications", []):
                try:
                    resp_idx = int(str(item.get("response_id", "")).strip("[]"))
                    original_text = batch[resp_idx].get("text", "") if resp_idx < len(batch) else ""
                except (ValueError, IndexError):
                    continue

                assignments = item.get("assignments", [])
                for assign in assignments:
                    tid = assign.get("theme_id", "other")
                    sentiment = assign.get("sentiment", "neutral")
                    if tid not in counts:
                        tid = "other"
                    counts[tid]["total"] += 1
                    if sentiment == "positive":
                        counts[tid]["pos"] += 1
                    elif sentiment == "negative":
                        counts[tid]["neg"] += 1
                    elif sentiment == "mixed":
                        counts[tid]["mixed"] += 1
                    else:
                        counts[tid]["neutral"] += 1
                    if tid != "other" and len(quotes_pool[tid]) < 10 and original_text:
                        quotes_pool[tid].append((sentiment, original_text))

        # ── 统计汇总 ──────────────────────────────────────────────────────
        total_mentions = sum(v["total"] for v in counts.values())
        if total_mentions == 0:
            continue

        themes_out = []
        other_themes_out = []

        for t in final_themes:
            tid = t["id"]
            c = counts[tid]
            cnt = c["total"]
            pct = round(cnt / total_mentions * 100, 1)
            pos_cnt = c["pos"]
            neg_cnt = c["neg"]
            pos_pct = round(pos_cnt / cnt * 100, 1) if cnt else 0.0
            neg_pct = round(neg_cnt / cnt * 100, 1) if cnt else 0.0

            # 代表性引用：每种情感最多取 1-2 条
            pool = quotes_pool.get(tid, [])
            pos_q = [txt for sent, txt in pool if sent == "positive"][:2]
            neg_q = [txt for sent, txt in pool if sent == "negative"][:2]
            neu_q = [txt for sent, txt in pool if sent not in ("positive", "negative")][:2]
            quotes = (pos_q + neg_q + neu_q)[:6]
            # 不足 3 条时从 pool 中补充未使用的原文，保证 Writer 有足够素材
            if len(quotes) < 3:
                used = set(quotes)
                extras = [txt for _, txt in pool if txt not in used]
                quotes = quotes + extras[:max(0, 3 - len(quotes))]

            entry = {
                "id": tid,
                "name": t["name"],
                "description": t.get("description", ""),
                "count": cnt,
                "percentage": pct,
                "positive_count": pos_cnt,
                "positive_pct": pos_pct,
                "positive_summary": t.get("positive_summary") or "",
                "negative_count": neg_cnt,
                "negative_pct": neg_pct,
                "negative_summary": t.get("negative_summary") or "",
                "quotes": quotes,
            }
            if pct < OTHER_THEME_PCT:
                other_themes_out.append({"name": t["name"], "count": cnt, "percentage": pct})
            else:
                themes_out.append(entry)

        themes_out.sort(key=lambda x: x["count"], reverse=True)
        other_themes_out.sort(key=lambda x: x["count"], reverse=True)

        clustered_themes[col_idx] = {
            "col_name": col_name,
            "total": total,
            "themes": themes_out,
            "other_themes": other_themes_out,
        }
        yield ("progress", f"【{col_name}】分析完成，识别 {len(themes_out)} 个主要主题")

    yield ("result", clustered_themes)


def _build_large_sample_writer_query(
    stats_md: str,
    clustered_themes: dict,
    plan: dict,
    headers: list[str],
) -> str:
    parts_lines = []
    for i, p in enumerate(plan["parts"], 1):
        if "column_indexes" in p:
            col_names = []
            for idx in p["column_indexes"]:
                col = next((c for c in plan["columns"] if c["index"] == idx), None)
                nm = (col and col.get("name")) or (headers[idx] if idx < len(headers) else f"列{idx}")
                rl = col["role"] if col else "?"
                col_names.append(f"{nm}({rl})")
            parts_lines.append(f"  Part {i} {p['name']}: {'; '.join(col_names)}")
        else:
            scope = p.get("scope", "")
            parts_lines.append(f"  Part {i} {p['name']}" + (f": {scope}" if scope else ""))
    plan_summary = "<plan>\n报告结构：\n" + "\n".join(parts_lines) + "\n</plan>"

    theme_blocks = []
    for col_idx, data in clustered_themes.items():
        col_name = data["col_name"]
        total = data["total"]
        lines = [f"### 问题：{col_name}（共 {total:,} 条有效回答）\n"]

        for i, t in enumerate(data["themes"], 1):
            lines.append(f"**主题{i}：{t['name']}**（提及 {t['count']:,} 人次，占 {t['percentage']}%）")
            if t["positive_summary"] or t["positive_count"]:
                lines.append(f"- 正面（{t['positive_count']:,} / {t['positive_pct']}%）：{t['positive_summary']}")
            if t["negative_summary"] or t["negative_count"]:
                lines.append(f"- 负面（{t['negative_count']:,} / {t['negative_pct']}%）：{t['negative_summary']}")
            if t["quotes"]:
                lines.append(f"- 代表原文（请在报告中完整引用，并附中文翻译）：")
                for q in t["quotes"]:
                    lines.append(f'  > "{q}"')
            lines.append("")

        if data["other_themes"]:
            other_parts = "、".join(
                f"{o['name']}（{o['percentage']}%）" for o in data["other_themes"]
            )
            lines.append(f"**其他声音**（合计占比较低）：{other_parts}")

        theme_blocks.append("\n".join(lines))

    open_text_md = (
        "<open_text_themes>\n" + "\n\n".join(theme_blocks) + "\n</open_text_themes>"
        if theme_blocks else "<open_text_themes>（无开放题聚类结果）</open_text_themes>"
    )

    requirements = _get_large_sample_writer_requirements()

    return (
        "**任务**：基于以下大样本问卷分析结果撰写调研报告。\n\n"
        f"{plan_summary}\n\n"
        f"<stats>\n{stats_md}\n</stats>\n\n"
        f"{open_text_md}\n\n"
        f"**要求**：\n{requirements}"
    )


def _get_large_sample_writer_requirements() -> str:
    return """一、报告结构（严格按此顺序）
1. **## 核心结论**（必须是第一个二级章节）
   - 列出整份报告中最重要的 5-8 条发现，每条一行，格式：「**结论标题**：具体说明（含数字）」
   - 覆盖所有 Part 的关键洞察，让读者读完此节即可掌握全部重点
2. 按方案中的 Part 顺序逐章展开
3. **## 行动建议**（最后一节，3-5 条，每条必须有对应数据依据）

二、结论驱动
- 以"多少人持有什么观点"为核心叙事框架
- 每个结论必须附具体数字（人数或占比），禁止使用"部分用户""少数玩家"等模糊表述

三、受访者画像展示
- 画像分布数据用 Markdown 表格呈现（列：维度 / 选项 / 人数占比），不要纯文字罗列
- 表格之后用 1-2 句话解读画像特征

四、主观题原文展示（关键）
- 每个主题/观点至少引用 3 条代表性玩家原文
- 展示格式：先展示原始语言原文（用引号括起），下方紧跟中文翻译（若原文已是中文则免翻译）
  示例：
  > "She's very outdated compared to other mage heroes."（该英雄与其他法师相比显得十分过时。）
  > "Modelnya kurang dipoles."（模型精致度不足。）
  > "模型感觉太老了，需要 revamp。"
- 引用的原文要能支撑该主题的核心论断，优先选择信息量最丰富的

五、语言风格
- 简洁直接，去掉冗长铺垫和过渡句
- 报告语言为中文；玩家原文保留原语种并附中文翻译"""


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
            ids = entry.get("ids", {})
            mlbb_vals = [str(v).strip() for k, v in ids.items() if "mlbb" in str(k).casefold() and str(v).strip()]
            player_vals = [str(v).strip() for k, v in ids.items() if "mlbb" not in str(k).casefold() and str(v).strip()]
            id_parts = []
            if player_vals:
                id_parts.append(f"玩家ID={' / '.join(player_vals)}")
            if mlbb_vals:
                id_parts.append(f"MLBBID={' / '.join(mlbb_vals)}")
            profile_str = " / ".join(f"{k}={v}" for k, v in entry.get("profile", {}).items())
            prefix = " | ".join(filter(None, [" | ".join(id_parts), f"画像={profile_str}" if profile_str else ""]))
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
    requirements += (
        "\n\n补充：引用玩家原文时必须沿用 `<open_text>` 前缀里的玩家身份信息。"
        "`玩家ID=...` 和 `MLBBID=...` 是两个独立身份字段，报告表格中要拆成 `玩家ID`、`MLBBID` 两列，单元格只放值。"
        "如果出现 `MLBBID=123456(57001)`，括号内是区服编号，必须作为 MLBBID 的一部分展示，"
        "不得拆到「画像信息」或其它列；只有 `<open_text>` 前缀里真的存在 `画像=...` 时，才展示画像信息。"
    )

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


def _annotate_ai_log(message: str, **fields) -> None:
    payload = " ".join(f"{k}={v!r}" for k, v in fields.items())
    print(f"[annotate.ai_detect] {message}" + (f" {payload}" if payload else ""), flush=True)


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


async def sse_dify_completion_stream(
    query: str,
    user_id: str,
    api_key: str,
    input_var: str = "survey_batch",
) -> AsyncGenerator[str, None]:
    """调用 Dify completion-messages 端点（文本生成应用）。"""
    import httpx

    inputs = {input_var: query}
    for fallback_key in ("query", "text", "content"):
        inputs.setdefault(fallback_key, query)

    payload = {
        "inputs": inputs,
        "response_mode": "streaming",
        "user": user_id,
    }
    req_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=1800, follow_redirects=True) as client:
        async with client.stream(
            "POST", f"{DIFY_API_BASE}/completion-messages",
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
                if event == "message":
                    yield data.get("answer", "")
                if event == "error":
                    raise RuntimeError(f"Dify error: {data.get('code')} {data.get('message')}")
                if event in ("message_end", "workflow_finished"):
                    break


def _looks_like_dify_endpoint_mismatch(err: Exception) -> bool:
    text = str(err).lower()
    return any(k in text for k in [
        "completion-messages",
        "chat-messages",
        "app mode",
        "app_mode",
        "not support",
        "not supported",
        "invalid_param",
        "endpoint",
        "completion app",
        "chat app",
    ])


async def call_dify_compatible(
    query: str,
    user_id: str,
    api_key: str,
    conversation_id: str = "",
) -> tuple[str, str, str, str]:
    """优先按 chat 应用调用；若疑似端点/应用类型不匹配，自动改用 completion 应用。"""
    try:
        chunks: list[str] = []
        final_conv = conversation_id
        async for chunk, conv_id in sse_dify_stream(query, user_id, conversation_id, api_key):
            if chunk:
                chunks.append(chunk)
            if conv_id:
                final_conv = conv_id
        return "".join(chunks), final_conv, "chat", ""
    except Exception as e:
        if not _looks_like_dify_endpoint_mismatch(e):
            raise
        fallback_reason = str(e)

    chunks: list[str] = []
    async for chunk in sse_dify_completion_stream(query, user_id, api_key):
        if chunk:
            chunks.append(chunk)
    return "".join(chunks), "", "completion", fallback_reason


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(title="问卷分析平台")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
_sweep_old_sessions()  # 启动时清理过期 session 文件

static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(static_dir, "index.html"))


@app.get("/login")
async def login_page(request: Request, next: str = "/"):
    safe_next = _safe_next_path(next)
    login = await _current_login(request)
    if login and _login_allowed(login):
        return RedirectResponse(safe_next)
    return FileResponse(os.path.join(static_dir, "login.html"))


def _safe_next_path(raw_next: str | None) -> str:
    if not raw_next:
        return "/"
    raw_next = str(raw_next).strip()
    if not raw_next.startswith("/") or raw_next.startswith("//"):
        return "/"
    if "\r" in raw_next or "\n" in raw_next:
        return "/"
    if raw_next.startswith("/api/feishu/callback"):
        return "/"
    return raw_next


def _login_url(next_path: str = "/", error: str = "") -> str:
    url = f"/login?next={quote(_safe_next_path(next_path), safe='')}"
    if error:
        url += f"&error={quote(error, safe='')}"
    return url


def _is_public_path(path: str) -> bool:
    if path in {"/login", "/favicon.ico"}:
        return True
    return path.startswith("/static/") or path.startswith("/api/feishu/")


def _wants_api_response(request: Request) -> bool:
    path = request.url.path
    accept = request.headers.get("accept", "")
    return path.startswith("/api/") or "text/event-stream" in accept or "application/json" in accept


# ── 白名单（运行时可写）──────────────────────────────────────

def _load_whitelist() -> list[dict]:
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("users", []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_whitelist(users: list[dict]) -> None:
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)


def _email(login: dict | None) -> str:
    return str((login or {}).get("email", "")).strip().lower()

def _open_id(login: dict | None) -> str:
    return str((login or {}).get("open_id", "")).strip()

def _whitelist_match(u: dict, login: dict) -> bool:
    """whitelist 条目按 email 或 open_id 匹配。"""
    if not u.get("enabled", True):
        return False
    e = _email(login)
    oid = _open_id(login)
    uid = u.get("email", "").strip().lower()  # 字段复用，可存 email 或 open_id
    return bool((e and uid == e) or (oid and uid == oid))


def _is_admin(login: dict | None) -> bool:
    e = _email(login)
    if not e:
        return False
    if FEISHU_ADMIN_EMAILS and e in FEISHU_ADMIN_EMAILS:
        return True
    # FEISHU_ALLOWED_EMAILS 里的人也视为管理员（向下兼容）
    if FEISHU_ALLOWED_EMAILS and e in FEISHU_ALLOWED_EMAILS:
        return True
    return False


def _get_user_perms(login: dict | None) -> list[str]:
    if not FEISHU_LOGIN_REQUIRED:
        return ["survey", "annotate"]
    if not login:
        return []
    if _is_admin(login):
        return ["survey", "annotate"]
    for u in _load_whitelist():
        if _whitelist_match(u, login):
            return list(u.get("perms", ["survey", "annotate"]))
    return []


def _login_allowed(login: dict | None) -> bool:
    if not FEISHU_LOGIN_REQUIRED:
        return True
    if not login:
        return False
    if _is_admin(login):
        return True
    for u in _load_whitelist():
        if _whitelist_match(u, login):
            return True
    # 没有任何白名单配置时，允许所有已登录用户（开放模式）
    if not FEISHU_ADMIN_EMAILS and not FEISHU_ALLOWED_EMAILS and not _load_whitelist():
        return True
    return False


def _login_denied_reason(login: dict | None = None) -> str:
    login = login or {}
    name = login.get("name", "")
    email = login.get("email", "").strip()
    open_id = login.get("open_id", "").strip()
    if not email and not open_id:
        return "未能识别账号，请联系管理员（飞书邮箱或 contact 权限可能未授权）"
    id_str = email or open_id
    name_str = f"（{name}）" if name else ""
    hint = f"Open ID: {open_id}" if not email else ""
    return f"账号 {id_str}{name_str} 无访问权限，请联系管理员添加。{hint}".strip()


def _unauthorized_response(request: Request):
    next_path = _safe_next_path(str(request.url.path))
    if request.url.query:
        next_path = _safe_next_path(f"{next_path}?{request.url.query}")
    if _wants_api_response(request):
        return JSONResponse(
            {"detail": "请先登录飞书", "login_url": _login_url(next_path)},
            status_code=401,
        )
    return RedirectResponse(_login_url(next_path))


def _forbidden_response(request: Request, login: dict | None = None):
    msg = _login_denied_reason(login)
    if _wants_api_response(request):
        return JSONResponse({"detail": msg}, status_code=403)
    return RedirectResponse(_login_url("/", msg))


@app.middleware("http")
async def feishu_auth_middleware(request: Request, call_next):
    if not FEISHU_LOGIN_REQUIRED or _is_public_path(request.url.path):
        return await call_next(request)

    login = await _current_login(request)
    if not login:
        resp = _unauthorized_response(request)
        resp.delete_cookie(COOKIE_NAME)
        return resp
    if not _login_allowed(login):
        return _forbidden_response(request, login)
    return await call_next(request)


# ── 上传 ──────────────────────────────────────────────

@app.post("/api/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
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
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = file.filename or "upload.csv"
    _assign_session_owner(sess, await _current_login(request))
    save_session(sid, sess)

    result = {
        "session_id": sid,
        "filename": file.filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
    }
    await audit_log(
        request,
        "survey",
        "上传数据",
        f"文件：{file.filename or 'unknown'}；样本行数：{len(rows) - 1}",
        metadata={"session_id": sid, "rows": len(rows) - 1},
    )
    return result


# ── 跑数表模式上传（问卷 + 回答数据 + 跑数统计表）──────────────────────

_Q_TITLE_RE = re.compile(r"^Q(\d+)\[")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _questionnaire_title_map(q_rows: list[list]) -> dict[int, str]:
    """从问卷行里抽 Q号 → 题目文本(用于给清数列起可读名)。"""
    m: dict[int, str] = {}
    for r in q_rows:
        if not r:
            continue
        c0 = str(r[0]).strip() if len(r) > 0 else ""
        mt = _Q_TITLE_RE.match(c0)
        if mt:
            text = str(r[1]).strip() if len(r) > 1 else ""
            text = _HTML_TAG_RE.sub("", text).strip()
            if text:
                m[int(mt.group(1))] = text
    return m


def _build_crosstab_columns(headers: list[str], q_title_map: dict[int, str]) -> list[dict]:
    """跑数表模式：确定性构建列元数据（无需 AI 题型识别）。

    只关心三类：open_text(供聚类)、id/profile(原文署名)，其余 ignore
    （数字来自跑数表，清数闭合列不参与统计）。
    """
    cols: list[dict] = []
    for i, h in enumerate(headers):
        hs = str(h).strip()
        low = hs.lower()
        role = "ignore"
        name = hs
        if hs.endswith("__open"):
            role = "open_text"
            mt = re.match(r"Q(\d+)", hs)
            if mt and int(mt.group(1)) in q_title_map:
                name = q_title_map[int(mt.group(1))]
        elif "zone_id" in low or "zoneid" in low:
            role = "ignore"
        elif "role_id" in low or "roleid" in low:
            role = "id"
        elif low in ("response id", "responseid"):
            role = "id"
        elif hs in ("段位", "等级", "性别", "年龄", "区服", "国家", "地区", "服务器"):
            role = "profile_dim"
        cols.append({
            "index": i,
            "name": name,
            "role": role,
            "source": "crosstab",
            "column_indexes": [i],
        })
    return cols


@app.post("/api/upload/crosstab")
async def upload_crosstab(
    request: Request,
    survey_file: UploadFile = File(...),    # 问卷（题目意图上下文）
    data_file: UploadFile = File(...),      # 清数：清洗后的问卷回答（用于主观题原文）
    crosstab_file: UploadFile = File(...),  # 跑数表：倍市得交叉统计表（数字来源）
):
    """倍市得「跑数表模式」上传：平台不再自算统计，数字直接取自跑数表，
    只对主观题做聚类。三文件均必需。"""
    # 1) 清数（回答数据）→ rows
    data_content = await data_file.read()
    try:
        rows = _parse_file(data_file.filename or "data.xlsx", data_content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"回答数据解析失败：{e}")
    if not rows or len(rows) <= 1:
        raise HTTPException(status_code=400, detail="回答数据为空或只有表头")

    # 2) 跑数表 → 结构化 → stats markdown
    ct_content = await crosstab_file.read()
    try:
        parsed = crosstab_parser.parse(ct_content)
        crosstab_md = crosstab_parser.render_to_markdown(parsed)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"跑数表解析失败：{e}")

    # 3) 问卷 → 纯文本（题目意图上下文）+ Q号→题名映射（给清数列起可读名）
    survey_content = await survey_file.read()
    try:
        q_rows = _parse_file(survey_file.filename or "survey.xlsx", survey_content)
        questionnaire_text = "\n".join(
            " | ".join(str(c) for c in r if str(c).strip())
            for r in q_rows if any(str(c).strip() for c in r)
        )
        q_title_map = _questionnaire_title_map(q_rows)
    except Exception:
        questionnaire_text = ""
        q_title_map = {}

    # 4) 确定性建列（跳过 AI 题型识别）+ 跑数表题目清单（给 planner）
    columns = _build_crosstab_columns(rows[0], q_title_map)
    crosstab_questions = crosstab_parser.question_names(parsed)

    sid = new_session()
    sess = get_session(sid)
    sess["rows"] = rows
    sess["filename"] = data_file.filename or "data.xlsx"
    sess["mode"] = "crosstab"
    sess["crosstab_md"] = crosstab_md
    sess["questionnaire_text"] = questionnaire_text
    sess["crosstab_questions"] = crosstab_questions
    # 列已确定性建好，直接作为"已确认"，跳过题型确认步骤
    sess["columns_detected"] = columns
    sess["confirmed_columns"] = columns
    _assign_session_owner(sess, await _current_login(request))
    save_session(sid, sess)

    result = {
        "session_id": sid,
        "filename": data_file.filename,
        "total_rows": len(rows) - 1,
        "headers": rows[0],
        "preview": rows[1: min(6, len(rows))],
        "mode": "crosstab",
        "crosstab_questions": len(parsed.get("questions", [])),
        "crosstab_segments": [s["label"] for s in parsed.get("segments", [])],
    }
    await audit_log(
        request,
        "survey",
        "上传跑数表数据",
        f"问卷：{survey_file.filename or '?'}；数据：{data_file.filename or '?'}；"
        f"跑数表：{crosstab_file.filename or '?'}；样本行数：{len(rows) - 1}",
        metadata={"session_id": sid, "rows": len(rows) - 1, "mode": "crosstab"},
    )
    return result


# ── 题型识别（Step 2，LLM，SSE）──────────────────────

@app.get("/api/columns/{session_id}")
async def get_columns(session_id: str, request: Request):
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
            questions = _sanitize_choice_options(rows, questions)
            # 跑数表模式安全网：倍市得清数中以 __open 结尾的列必是开放题，
            # 强制 role=open_text，避免 AI 误判导致主观题漏聚类。
            if sess.get("mode") == "crosstab":
                hdrs = rows[0]
                for q in questions:
                    idx = q.get("index")
                    if isinstance(idx, int) and 0 <= idx < len(hdrs) \
                            and str(hdrs[idx]).strip().endswith("__open"):
                        q["role"] = "open_text"
            sess["columns_detected"] = questions
            save_session(session_id, sess)
            await audit_log(
                request,
                "survey",
                "识别题型",
                f"会话：{session_id}；识别列数：{len(questions)}",
                metadata={"session_id": session_id, "columns": len(questions)},
            )
            yield sse_event({"type": "columns_ready", "columns": questions})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


class ColumnConfirmRequest(BaseModel):
    columns: list[dict]


@app.post("/api/columns/{session_id}/confirm")
async def confirm_columns(session_id: str, req: ColumnConfirmRequest, request: Request):
    """存储用户确认（或修改）后的列题型。"""
    sess = get_session(session_id)
    sess["confirmed_columns"] = req.columns
    save_session(session_id, sess)
    await audit_log(
        request,
        "survey",
        "确认数据列",
        f"会话：{session_id}；确认列数：{len(req.columns)}",
        metadata={"session_id": session_id, "columns": len(req.columns)},
    )
    return {"ok": True}


# ── Plan（SSE）────────────────────────────────────────

@app.get("/api/plan/{session_id}")
async def get_plan(session_id: str, request: Request):
    sess = get_session(session_id)
    rows = sess.get("rows")
    confirmed_columns = sess.get("confirmed_columns")

    is_crosstab = sess.get("mode") == "crosstab"

    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据，请先上传文件")
    if is_crosstab:
        if not DIFY_CROSSTAB_PLANNER_KEY:
            raise HTTPException(status_code=500, detail="未配置 DIFY_CROSSTAB_PLANNER_KEY")
    elif not DIFY_PLANNER_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_PLANNER_KEY")

    async def generate():
        try:
            if is_crosstab:
                # ── 跑数表模式：AI 读问卷原文规划章节大纲 ──────────────────
                cols = confirmed_columns or []
                open_names = [c["name"] for c in cols if c.get("role") == "open_text"]
                avail = sess.get("crosstab_questions", [])
                query = _build_crosstab_planner_query(
                    sess.get("questionnaire_text", ""), avail, open_names
                )
                ans_chunks: list[str] = []
                conv = ""
                async for chunk, cid in sse_dify_stream(query, session_id, "", DIFY_CROSSTAB_PLANNER_KEY):
                    if chunk:
                        ans_chunks.append(chunk)
                        yield sse_event({"type": "chunk", "content": chunk})
                    if cid:
                        conv = cid
                ctp, err = survey_plan.parse_crosstab_plan("".join(ans_chunks))
                if not ctp:
                    yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
                    retry_q = (
                        f"上次输出无法解析: {err}。请只输出一个 JSON 对象"
                        "(含 parts 和 open_questions)，用 ```json``` 围栏，不要解释文字。"
                    )
                    retry_chunks: list[str] = []
                    async for chunk, cid in sse_dify_stream(retry_q, session_id, conv, DIFY_CROSSTAB_PLANNER_KEY):
                        if chunk:
                            retry_chunks.append(chunk)
                        if cid:
                            conv = cid
                    ctp, err = survey_plan.parse_crosstab_plan("".join(retry_chunks))
                if not ctp:
                    yield sse_event({"type": "error", "message": f"章节大纲解析失败：{err}"}); return

                plan = {
                    "mode": "crosstab",
                    "columns": cols,
                    "parts": ctp["parts"],
                    "open_questions": ctp["open_questions"],
                    "cross_tabs": [],
                }
                sess["plan"] = plan
                sess["planner_conv_id"] = conv
                save_session(session_id, sess)
                card_text = _render_crosstab_plan_card(plan)
                await audit_log(
                    request, "survey", "生成章节大纲",
                    f"会话：{session_id}；章节数：{len(plan['parts'])}",
                    metadata={"session_id": session_id, "parts": len(plan["parts"]), "mode": "crosstab"},
                )
                yield sse_event({"type": "plan_ready", "plan": plan, "card_text": card_text, "headers": rows[0]})
                return

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
                yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
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
            save_session(session_id, sess)
            card_text = survey_plan.render_plan_for_user(plan, headers)
            await audit_log(
                request,
                "survey",
                "生成分析方案",
                f"会话：{session_id}；Part 数：{len(plan.get('parts', []))}",
                metadata={"session_id": session_id, "parts": len(plan.get("parts", []))},
            )
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
async def confirm_plan(req: PlanConfirmRequest, request: Request):
    sess = get_session(req.session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    planner_conv_id = sess.get("planner_conv_id", "")

    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失，请重新上传文件")

    if survey_plan.is_user_approval(req.user_text):
        await audit_log(
            request,
            "survey",
            "确认分析方案",
            f"会话：{req.session_id}",
            metadata={"session_id": req.session_id},
        )
        return JSONResponse({"approved": True})

    async def generate():
        try:
            # ── 跑数表模式：章节大纲修订 ──────────────────────────────
            if sess.get("mode") == "crosstab":
                conv = planner_conv_id
                rev_q = _build_crosstab_plan_revision_query(
                    sess.get("questionnaire_text", ""), plan.get("parts", []), req.user_text
                )
                rchunks: list[str] = []
                async for chunk, cid in sse_dify_stream(rev_q, req.session_id, "", DIFY_CROSSTAB_PLANNER_KEY):
                    if chunk:
                        rchunks.append(chunk)
                        yield sse_event({"type": "chunk", "content": chunk})
                    if cid:
                        conv = cid
                ctp, err = survey_plan.parse_crosstab_plan("".join(rchunks))
                if not ctp:
                    yield sse_event({"type": "error", "message": f"修订章节大纲解析失败：{err}"}); return
                new_plan = dict(plan)
                new_plan["parts"] = ctp["parts"]
                new_plan["open_questions"] = ctp["open_questions"]
                sess["plan"] = new_plan
                sess["planner_conv_id"] = conv
                save_session(req.session_id, sess)
                card_text = _render_crosstab_plan_card(new_plan)
                await audit_log(
                    request, "survey", "修订章节大纲",
                    f"会话：{req.session_id}；修改意见：{_short_text(req.user_text)}",
                    metadata={"session_id": req.session_id, "mode": "crosstab"},
                )
                yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": rows[0]})
                return

            new_conv_id = planner_conv_id
            answer_chunks: list[str] = []
            headers = rows[0]
            revision_query = _build_plan_revision_query(
                plan,
                headers,
                sess.get("confirmed_columns", []),
                req.user_text,
            )
            async for chunk, conv_id in sse_dify_stream(
                revision_query, req.session_id, "", DIFY_PLANNER_KEY
            ):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    new_conv_id = conv_id

            full_answer = "".join(answer_chunks)
            new_plan, err = survey_plan.parse_plan_from_llm(
                full_answer,
                len(headers)
            )
            if not new_plan:
                retry_query = (
                    f"{revision_query}\n\n"
                    "上一次输出无法解析为 JSON。请修正并只返回完整 JSON 对象。\n"
                    f"解析错误：{err}\n"
                    f"<previous_output>\n{full_answer[:4000]}\n</previous_output>"
                )
                retry_chunks: list[str] = []
                retry_conv_id = new_conv_id
                yield sse_event({"type": "progress", "message": "方案格式校验中，正在修订输出…"})
                yield sse_event({"type": "chunk", "content": "\n\n正在按严格 JSON 格式重新修订方案...\n"})
                async for chunk, conv_id in sse_dify_stream(
                    retry_query, req.session_id, "", DIFY_PLANNER_KEY
                ):
                    if chunk:
                        retry_chunks.append(chunk)
                        yield sse_event({"type": "chunk", "content": chunk})
                    if conv_id:
                        retry_conv_id = conv_id
                new_plan, err = survey_plan.parse_plan_from_llm("".join(retry_chunks), len(headers))
                if new_plan:
                    new_conv_id = retry_conv_id
            if not new_plan:
                yield sse_event({"type": "error", "message": f"修订方案解析失败：{err}"}); return

            if sess.get("confirmed_columns"):
                new_plan = survey_plan.merge_confirmed_into_plan(new_plan, sess["confirmed_columns"])

            sess["plan"] = new_plan
            sess["planner_conv_id"] = new_conv_id
            save_session(req.session_id, sess)
            card_text = survey_plan.render_plan_for_user(new_plan, headers)
            await audit_log(
                request,
                "survey",
                "修订分析方案",
                f"会话：{req.session_id}；修改意见：{_short_text(req.user_text)}",
                metadata={"session_id": req.session_id},
            )
            yield sse_event({"type": "plan_ready", "plan": new_plan, "card_text": card_text, "headers": headers})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 统计 ────────────────────────────────────────────

@app.post("/api/stats/{session_id}")
async def compute_stats(session_id: str, request: Request):
    sess = get_session(session_id)
    plan = sess.get("plan")
    rows = sess.get("rows")
    if not plan or not rows:
        raise HTTPException(status_code=400, detail="会话状态丢失")
    loop = asyncio.get_event_loop()
    if sess.get("mode") == "crosstab":
        # 跑数表模式：数字直接取自跑数表，平台不自算统计；只收集开放题原文供聚类。
        stats_md = sess.get("crosstab_md", "")
        open_text = await loop.run_in_executor(
            None, survey_stats.collect_open_text, rows, plan
        )
    else:
        stats_md, open_text = await loop.run_in_executor(
            None, survey_stats.compute, rows, plan
        )
    sess["stats_md"] = stats_md
    sess["open_text"] = open_text
    sess["rows_fed"] = False
    save_session(session_id, sess)
    await audit_log(
        request,
        "survey",
        "计算统计",
        f"会话：{session_id}；样本行数：{max(0, len(rows) - 1)}",
        metadata={"session_id": session_id, "rows": max(0, len(rows) - 1)},
    )
    return {"stats_md": stats_md}


# ── 报告（SSE）──────────────────────────────────────

@app.get("/api/report/{session_id}")
async def generate_report(session_id: str, request: Request):
    sess = get_session(session_id)
    _assign_session_owner(sess, await _current_login(request))
    plan = sess.get("plan")
    rows = sess.get("rows")
    stats_md = sess.get("stats_md")
    open_text = sess.get("open_text", {})

    if not all([plan, rows, stats_md]):
        raise HTTPException(status_code=400, detail="请先完成统计计算")

    total_open_text = sum(len(v) for v in open_text.values())
    is_crosstab = sess.get("mode") == "crosstab"
    # 跑数表模式恒定走聚类（不受 ≥500 阈值限制），确保主观题共性问题被发现
    use_large_mode = is_crosstab or (total_open_text >= LARGE_SAMPLE_THRESHOLD)

    if use_large_mode:
        if not DIFY_LARGE_ANALYST_KEY:
            raise HTTPException(status_code=500, detail="未配置 DIFY_LARGE_ANALYST_KEY")
    else:
        if not DIFY_ANALYST_KEY:
            raise HTTPException(status_code=500, detail="未配置 DIFY_ANALYST_KEY")

    async def generate():
        try:
            if use_large_mode:
                # ── 大样本 / 跑数表模式：主观题四阶段批处理聚类 ──────────────
                start_msg = (
                    f"跑数表模式：数字取自跑数表，开始对 {total_open_text} 条主观题回复做聚类"
                    if is_crosstab
                    else f"检测到大样本（{total_open_text} 条开放题回复），启用批处理模式"
                )
                yield sse_event({"type": "progress", "message": start_msg})
                clustered_themes: dict = {}
                async for item in _batch_qualitative_analysis(open_text, plan, rows[0], session_id):
                    if item[0] == "progress":
                        yield sse_event({"type": "progress", "message": item[1]})
                    else:
                        clustered_themes = item[1]

                yield sse_event({"type": "progress", "message": "主题分析完成，开始生成报告..."})
                writer_query = _build_large_sample_writer_query(stats_md, clustered_themes, plan, rows[0])
                # 跑数表模式：把问卷原文作为题目意图上下文一并喂给 Writer
                if is_crosstab:
                    q_text = (sess.get("questionnaire_text") or "").strip()
                    if q_text:
                        if len(q_text) > 8000:
                            q_text = q_text[:8000] + "\n…（问卷过长，已截断）"
                        writer_query = (
                            f"<questionnaire>\n以下是问卷原文（仅供理解题目意图与背景，"
                            f"不要直接搬运）：\n{q_text}\n</questionnaire>\n\n" + writer_query
                        )
                analyst_key = DIFY_LARGE_ANALYST_KEY
            else:
                # ── 标准模式（原有逻辑）─────────────────────────────────
                writer_query = _build_writer_query(stats_md, open_text, plan, rows[0])
                analyst_key = DIFY_ANALYST_KEY

            answer_chunks: list[str] = []
            final_conv_id = ""

            async for chunk, conv_id in sse_dify_stream(writer_query, session_id, "", analyst_key):
                if chunk:
                    answer_chunks.append(chunk)
                    yield sse_event({"type": "chunk", "content": chunk})
                if conv_id:
                    final_conv_id = conv_id

            full_report = "".join(answer_chunks)
            drifted = survey_stats.find_numbers_not_in_stats(full_report, stats_md)
            if drifted:
                print(f"[stats] WARN drifted numbers: {drifted[:20]}")

            full_report = _inject_disclaimer(full_report, skip_qual=(sess.get("mode") == "crosstab"))
            sess["report_md"] = full_report
            sess["analyst_conv_id"] = final_conv_id
            sess["rows_fed"] = False

            save_session(session_id, sess)
            save_to_history(session_id, sess)
            await audit_log(
                request,
                "survey",
                "生成报告",
                f"会话：{session_id}；文件：{sess.get('filename', 'unknown')}；模式：{'大样本' if use_large_mode else '标准'}",
                metadata={"session_id": session_id, "filename": sess.get("filename", "unknown"), "large_mode": use_large_mode},
            )

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
async def qa(req: QARequest, request: Request):
    sess = get_session(req.session_id)
    _assign_session_owner(sess, await _current_login(request))
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

            answer_text = "".join(answer_chunks)
            sess["analyst_conv_id"] = new_conv_id or analyst_conv_id
            sess["rows_fed"] = True
            sess.setdefault("qa_messages", []).extend([
                {"role": "user", "content": req.question, "ts": datetime.now().isoformat()},
                {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
            ])
            save_session(req.session_id, sess)
            save_to_history(req.session_id, sess)
            await audit_log(
                request,
                "report",
                "追问当前报告",
                f"会话：{req.session_id}；问题：{_short_text(req.question)}",
                metadata={"session_id": req.session_id},
            )
            yield sse_event({"type": "qa_done", "answer": answer_text})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 历史 QA（从历史条目恢复会话）────────────────────

class HistoryQARequest(BaseModel):
    history_id: str
    question: str


@app.post("/api/history-qa")
async def history_qa(req: HistoryQARequest, request: Request):
    """从历史记录中续聊 QA（无行数据，直接使用 analyst conv_id）。"""
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, req.history_id, login)
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
            answer_text = "".join(answer_chunks)
            for h in history:
                if h["id"] == req.history_id:
                    h["analyst_conv_id"] = new_conv_id or analyst_conv_id
                    h.setdefault("qa_messages", []).extend([
                        {"role": "user", "content": req.question, "ts": datetime.now().isoformat()},
                        {"role": "ai", "content": answer_text, "ts": datetime.now().isoformat()},
                    ])
                    break
            _save_history(history)
            await audit_log(
                request,
                "report",
                "追问历史报告",
                f"历史报告：{entry.get('title', req.history_id)}；问题：{_short_text(req.question)}",
                metadata={"history_id": req.history_id},
            )

            yield sse_event({"type": "qa_done", "answer": answer_text})
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


PDF_PAGE_RULE = "@page { size: 10in 14in; margin: 0; }"
PDF_CSS = PDF_PAGE_RULE + """
* { box-sizing: border-box; }
html {
  margin: 0;
  background: #fff;
}
body {
  font-family: "Noto Sans CJK SC", "Noto Sans CJK", "WenQuanYi Micro Hei", "Microsoft YaHei", "PingFang SC", "Source Han Sans SC", Arial, sans-serif;
  font-size: 13px;
  line-height: 1.75;
  color: #222;
  background: #fff;
  width: auto;
  max-width: 900px;
  margin: 0 auto;
  padding: 36px 44px 48px;
}
h1 {
  font-size: 22px;
  font-weight: 700;
  color: #1a1a1a;
  border-bottom: 2px solid #7c3aed;
  padding-bottom: 8px;
  margin: 0 0 18px;
}
h2 {
  font-size: 17px;
  font-weight: 600;
  color: #2d2d2d;
  margin: 24px 0 10px;
  border-left: 4px solid #7c3aed;
  padding-left: 10px;
  page-break-after: avoid;
}
h3 { font-size: 14.5px; color: #444; margin: 18px 0 8px; page-break-after: avoid; }
h4 { font-size: 13.5px; color: #555; margin: 14px 0 6px; page-break-after: avoid; }
p { margin: 0 0 9px; }
ul, ol { margin: 6px 0 12px 22px; padding: 0; }
li { margin-bottom: 4px; }
blockquote {
  border-left: 3px solid #999;
  padding: 6px 14px;
  margin: 10px 0;
  color: #666;
  font-style: italic;
  background: #f9f9f9;
}
table {
  border-collapse: collapse;
  width: 100%;
  max-width: 100%;
  margin: 14px 0;
  font-size: 11.5px;
  line-height: 1.55;
  page-break-inside: avoid;
  table-layout: auto;
}
thead { display: table-header-group; }
tr { page-break-inside: avoid; }
th {
  background: #f0ebff;
  color: #3d1d8a;
  font-weight: 600;
  padding: 7px 10px;
  border: 1px solid #d6c8ff;
  text-align: left;
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: normal;
}
th:first-child, td:first-child { min-width: 72px; }
td {
  padding: 6px 10px;
  border: 1px solid #e0e0e0;
  vertical-align: top;
  white-space: normal;
  word-break: normal;
  overflow-wrap: anywhere;
}
tr:nth-child(even) td { background: #fafafa; }
img { max-width: 100%; height: auto; }
code { background: #f3f0ff; color: #5b21b6; padding: 1px 5px; border-radius: 3px; font-size: 12px; }
pre { background: #f5f5f5; padding: 12px; border-radius: 4px; overflow-wrap: break-word; white-space: pre-wrap; }
code, pre { font-family: "Noto Sans Mono CJK SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei", "Microsoft YaHei", monospace; }
strong { color: #111; font-weight: 600; }
hr { border: none; border-top: 1px solid #e0e0e0; margin: 20px 0; }
.core-highlight-box {
  background: rgba(139, 92, 246, 0.07);
  border: 1.5px solid rgba(139, 92, 246, 0.22);
  border-radius: 10px;
  padding: 14px 18px 10px;
  margin: 10px 0 16px;
  page-break-inside: avoid;
}
.core-highlight-box h2,
.core-highlight-box h3,
.core-highlight-box h4 { margin-top: 0; }
.core-highlight-box h3 {
  margin: 14px 0 8px;
  padding: 5px 9px;
  border-radius: 7px;
  background: rgba(139, 92, 246, 0.10);
  color: #5b21b6;
  font-size: 14px;
}
.core-highlight-box li { margin-bottom: 8px; }
"""


def _set_pdf_page_height(doc: str, height_in: float) -> str:
    height_in = max(6.0, min(float(height_in), 500.0))
    return doc.replace(PDF_PAGE_RULE, f"@page {{ size: 10in {height_in:.2f}in; margin: 0; }}", 1)


def _wrap_report_highlights(html_text: str) -> str:
    html_text = re.sub(r"<!--CORE_START-->\s*", '<div class="core-highlight-box">', html_text)
    html_text = re.sub(r"\s*<!--CORE_END-->", "</div>", html_text)

    summary_title = r"(?:本章总结|本节总结|章节总结|本部分总结)"
    heading_pat = re.compile(
        rf"(<h[34][^>]*>\s*{summary_title}\s*[:：]?\s*</h[34]>)(.*?)(?=<h[1-6][^>]*>|$)",
        re.S,
    )
    html_text = heading_pat.sub(r'<div class="core-highlight-box">\1\2</div>', html_text)

    paragraph_pat = re.compile(
        rf"(<p>\s*{summary_title}\s*[:：].*?</p>(?:(?!<h[1-6][^>]*>|<table|<pre|<div).)*?)"
        rf"(?=<h[1-6][^>]*>|$)",
        re.S,
    )
    return paragraph_pat.sub(r'<div class="core-highlight-box">\1</div>', html_text)


def report_markdown_to_pdf(md_text: str) -> bytes:
    md_text = _inject_disclaimer(md_text or "")
    body = markdown_lib.markdown(
        md_text,
        extensions=["extra", "sane_lists", "nl2br"],
        output_format="html5",
    )
    body = _wrap_report_highlights(body)
    title_m = re.search(r"^#\s+(.+?)$", md_text, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    doc = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{html.escape(title)}</title>
  <style>{PDF_CSS}</style>
</head>
<body>{body}</body>
</html>"""
    return html_to_pdf_bytes(doc)


def _find_pdf_browser() -> str:
    candidates = [
        os.getenv("PDF_BROWSER_PATH", "").strip(),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("microsoft-edge"),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/lib/chromium/chromium",
        "/usr/lib/chromium-browser/chromium-browser",
        "/opt/google/chrome/chrome",
        "/usr/bin/microsoft-edge",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise RuntimeError("未找到可用于生成 PDF 的 Chrome/Edge/Chromium，请安装浏览器或设置 PDF_BROWSER_PATH")


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_cdp_target(port: int, timeout_seconds: float = 10.0) -> str:
    deadline = time.time() + timeout_seconds
    last_err = ""
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/json", timeout=0.5) as resp:
                targets = json.loads(resp.read().decode("utf-8"))
            for target in targets:
                if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                    return target["webSocketDebuggerUrl"]
        except Exception as exc:
            last_err = str(exc)
        time.sleep(0.1)
    raise RuntimeError(f"等待浏览器调试端口超时：{last_err}")


def _html_to_pdf_with_browser(doc: str) -> bytes:
    browser = _find_pdf_browser()
    last_err: Exception | None = None
    for headless_arg in ("--headless=new", "--headless"):
        try:
            return _html_to_pdf_with_browser_cmd(doc, browser, headless_arg)
        except Exception as exc:
            last_err = exc
            print(f"[pdf] browser render failed with {headless_arg}: {exc}", flush=True)
    raise RuntimeError(f"浏览器 PDF 生成失败：{last_err}")


def _html_to_pdf_with_browser_cmd(doc: str, browser: str, headless_arg: str) -> bytes:
    with tempfile.TemporaryDirectory(prefix="survey_pdf_") as tmp:
        tmp_path = Path(tmp)
        html_path = tmp_path / "report.html"
        profile_path = tmp_path / "profile"
        html_path.write_text(doc, encoding="utf-8")

        port = _free_local_port()
        cmd = [
            browser,
            headless_arg,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_path}",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--window-size=960,800",
            html_path.as_uri(),
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            ws_url = _wait_for_cdp_target(port)
            from websockets.sync.client import connect

            with connect(ws_url, open_timeout=10, max_size=None) as ws:
                msg_id = 0

                def cdp(method: str, params: dict | None = None) -> dict:
                    nonlocal msg_id
                    msg_id += 1
                    ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
                    while True:
                        data = json.loads(ws.recv())
                        if data.get("id") != msg_id:
                            continue
                        if "error" in data:
                            raise RuntimeError(f"{method} failed: {data['error']}")
                        return data.get("result", {})

                cdp("Page.enable")
                cdp("Runtime.evaluate", {
                    "expression": (
                        "new Promise(resolve => {"
                        " if (document.readyState === 'complete') resolve(true);"
                        " else window.addEventListener('load', () => resolve(true), {once:true});"
                        "})"
                    ),
                    "awaitPromise": True,
                    "returnByValue": True,
                })
                height_result = cdp("Runtime.evaluate", {
                    "expression": (
                        "Math.ceil(Math.max("
                        "document.body.scrollHeight,"
                        "document.documentElement.scrollHeight,"
                        "document.body.offsetHeight,"
                        "document.documentElement.offsetHeight"
                        "))"
                    ),
                    "returnByValue": True,
                })
                height_px = int(height_result.get("result", {}).get("value") or 1200)
                width_in = 10.0
                height_in = max(6.0, min((height_px + 24) / 96, 500.0))
                pdf_params = {
                    "printBackground": True,
                    "paperWidth": width_in,
                    "paperHeight": height_in,
                    "marginTop": 0,
                    "marginBottom": 0,
                    "marginLeft": 0,
                    "marginRight": 0,
                    "preferCSSPageSize": False,
                    "scale": 1,
                    "generateDocumentOutline": True,
                }
                try:
                    pdf_result = cdp("Page.printToPDF", pdf_params)
                except RuntimeError as exc:
                    if "generateDocumentOutline" not in str(exc):
                        raise
                    pdf_params.pop("generateDocumentOutline", None)
                    pdf_result = cdp("Page.printToPDF", pdf_params)
                return base64.b64decode(pdf_result["data"])
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def html_to_pdf_bytes(doc: str) -> bytes:
    renderer = os.getenv("PDF_RENDERER", "").strip().lower()
    if renderer == "browser":
        try:
            return _html_to_pdf_with_browser(doc)
        except Exception as exc:
            print(f"[pdf] requested browser renderer failed, falling back to WeasyPrint: {exc}", flush=True)

    try:
        return _html_to_pdf_with_weasyprint(doc)
    except Exception as exc:
        print(f"[pdf] WeasyPrint renderer failed, retrying browser renderer: {exc}", flush=True)

    try:
        return _html_to_pdf_with_browser(doc)
    except Exception as exc:
        print(f"[pdf] browser renderer failed after WeasyPrint fallback: {exc}", flush=True)
        raise


def _html_to_pdf_with_weasyprint(doc: str) -> bytes:
    from weasyprint import HTML  # type: ignore

    html_obj = HTML(string=doc)
    rendered = html_obj.render()
    pages = getattr(rendered, "pages", []) or []
    if len(pages) <= 1:
        print("[pdf] rendered with WeasyPrint on one page", flush=True)
        return rendered.write_pdf()

    total_height_px = sum(float(getattr(page, "height", 14 * 96) or 14 * 96) for page in pages)
    total_height_in = min(max(total_height_px / 96 + 1.0, 14.0), 500.0)
    print(
        f"[pdf] WeasyPrint first pass produced {len(pages)} pages; rerendering as {total_height_in:.2f}in single page",
        flush=True,
    )
    return HTML(string=_set_pdf_page_height(doc, total_height_in)).write_pdf()


@app.get("/api/export/word/{session_id}")
async def export_word(session_id: str, request: Request):
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
    await audit_log(request, "report", "下载 Word", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe}.docx",
    )


@app.get("/api/export/markdown/{session_id}")
async def export_markdown(session_id: str, request: Request):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    report_md = _prep_export_md(report_md)
    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    await audit_log(request, "report", "下载 Markdown", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(report_md.encode("utf-8"), "text/markdown; charset=utf-8", f"{safe}.md")


@app.get("/api/export/pdf/{session_id}")
async def export_pdf(session_id: str, request: Request):
    sess = get_session(session_id)
    report_md = sess.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="还没有生成报告")

    title_m = re.search(r"^#\s+(.+?)$", report_md, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)

    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(None, report_markdown_to_pdf, report_md)
    await audit_log(request, "report", "下载 PDF", f"报告：{title}", metadata={"session_id": session_id})
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe}.pdf")


@app.get("/api/export/word-history/{history_id}")
async def export_word_history(history_id: str, request: Request):
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    report_md = _prep_export_md(entry.get("report_md", ""))
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    docx_bytes = await loop.run_in_executor(None, markdown_to_docx, report_md)
    await audit_log(request, "report", "下载历史 Word", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return _make_download_response(
        docx_bytes,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        f"{safe}.docx",
    )


@app.get("/api/export/pdf-history/{history_id}")
async def export_pdf_history(history_id: str, request: Request):
    login = await _current_login(request)
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    report_md = entry.get("report_md", "")
    if not report_md:
        raise HTTPException(status_code=400, detail="该历史记录没有报告内容")
    safe = re.sub(r'[\\/:*?"<>|]', "_", entry.get("title", "调研报告"))
    loop = asyncio.get_event_loop()
    pdf_bytes = await loop.run_in_executor(None, report_markdown_to_pdf, report_md)
    await audit_log(request, "report", "下载历史 PDF", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return _make_download_response(pdf_bytes, "application/pdf", f"{safe}.pdf")


# ── 飞书 OAuth 登录 + 文档导出 ────────────────────────

# Web 登录态（内存 + 本地文件；服务热更新/重启后仍保留 7 天会话）
def _load_web_logins() -> dict[str, dict]:
    if not os.path.exists(WEB_LOGINS_FILE):
        return {}
    try:
        with open(WEB_LOGINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {
            str(k): v for k, v in data.items()
            if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
        }
    except Exception:
        return {}


def _save_web_logins() -> None:
    now = time.time()
    stale = [k for k, v in web_logins.items() if float(v.get("expires_at") or 0) <= now]
    for k in stale:
        web_logins.pop(k, None)
    with open(WEB_LOGINS_FILE, "w", encoding="utf-8") as f:
        json.dump(web_logins, f, ensure_ascii=False, indent=2)


web_logins: dict[str, dict] = _load_web_logins()
oauth_states: dict[str, dict] = {}


def _sync_web_logins_from_disk() -> None:
    """Keep multi-worker processes in sync with the persisted login store."""
    web_logins.clear()
    web_logins.update(_load_web_logins())


def _extract_core_lines(md: str) -> list[str]:
    """取出 <!--CORE_START-->..<!--CORE_END--> 之间的内容行（供飞书高亮块用）。"""
    if not md or CORE_START not in md:
        return []
    try:
        seg = md.split(CORE_START, 1)[1].split(CORE_END, 1)[0]
    except IndexError:
        return []
    return [ln for ln in seg.split("\n") if ln.strip()]


def _drop_first_h1(md: str) -> str:
    """飞书文档标题栏已展示 title，正文导入前去掉首个 H1，避免重复标题。"""
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# ") and not line.startswith("## "):
            del lines[i]
            while i < len(lines) and not lines[i].strip():
                del lines[i]
            return "\n".join(lines).lstrip()
    return md


def _extract_feishu_callout_sections(md: str) -> list[dict]:
    sections: list[dict] = []
    core_lines = _extract_core_lines(md)
    if core_lines:
        sections.append({"title": "核心结论", "lines": core_lines, "occurrence": 1})

    occurrence_counts: dict[str, int] = {}
    lines = md.split("\n")
    summary_re = re.compile(r"^(#{3,4})\s+(本章总结|本节总结|章节总结|本部分总结)\s*[:：]?\s*$")
    i = 0
    while i < len(lines):
        match = summary_re.match(lines[i].strip())
        if not match:
            i += 1
            continue
        title = match.group(2)
        level = len(match.group(1))
        body: list[str] = []
        i += 1
        while i < len(lines):
            heading = re.match(r"^(#{1,6})\s+", lines[i].strip())
            if heading and len(heading.group(1)) <= level:
                break
            body.append(lines[i])
            i += 1
        occurrence_counts[title] = occurrence_counts.get(title, 0) + 1
        sections.append({"title": title, "lines": body, "occurrence": occurrence_counts[title]})
    return sections


async def _current_login(request: Request) -> dict | None:
    """从 cookie 取登录态；token 临过期则尝试刷新。返回 None 表示未登录。"""
    sid = request.cookies.get(COOKIE_NAME, "")
    _sync_web_logins_from_disk()
    login = web_logins.get(sid)
    if not login:
        return None
    now = time.time()
    if login.get("expires_at", 0) and login["expires_at"] < now:
        web_logins.pop(sid, None)
        _save_web_logins()
        return None
    if login.get("exp", 0) < time.time() + 120 and login.get("refresh"):
        try:
            fresh = await feishu_export.refresh_token(login["refresh"])
            login.update(fresh)
            _save_web_logins()
        except Exception:
            web_logins.pop(sid, None)
            _save_web_logins()
            return None
    return login


@app.get("/api/feishu/login")
async def feishu_login(next: str = "/"):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）")
    state = secrets.token_urlsafe(16)
    oauth_states[state] = {"next": _safe_next_path(next), "ts": time.time()}
    # 清理过期 state
    now = time.time()
    for k in list(oauth_states.keys()):
        if oauth_states[k]["ts"] + 600 < now:
            del oauth_states[k]
    return RedirectResponse(feishu_export.build_authorize_url(state))


@app.get("/api/feishu/callback")
async def feishu_callback(request: Request, code: str = "", state: str = ""):
    st = oauth_states.pop(state, None)
    if not st:
        raise HTTPException(status_code=400, detail="state 无效或已过期，请重新登录")
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权 code")
    try:
        login = await feishu_export.exchange_code(code)
    except Exception as e:
        return RedirectResponse(_login_url(st.get("next") or "/", f"飞书授权失败：{e}"))
    if FEISHU_LOGIN_REQUIRED and not _login_allowed(login):
        return RedirectResponse(_login_url(st.get("next") or "/", _login_denied_reason(login)))
    now = time.time()
    login["created_at"] = now
    login["expires_at"] = now + FEISHU_SESSION_SECONDS
    sid = secrets.token_urlsafe(24)
    _sync_web_logins_from_disk()
    web_logins[sid] = login
    _save_web_logins()
    _audit_log_from_login(request, login, "auth", "飞书登录", "飞书授权登录成功")
    resp = RedirectResponse(_safe_next_path(st.get("next") or "/"))
    secure = feishu_export.FEISHU_REDIRECT_URI.startswith("https://")
    resp.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax", secure=secure, max_age=FEISHU_SESSION_SECONDS)
    return resp


@app.get("/api/feishu/me")
async def feishu_me(request: Request):
    login = await _current_login(request)
    allowed = _login_allowed(login)
    return {
        "configured": feishu_export.is_configured(),
        "login_required": FEISHU_LOGIN_REQUIRED,
        "logged_in": bool(login),
        "allowed": allowed,
        "name": (login or {}).get("name", ""),
        "email": (login or {}).get("email", ""),
        "open_id": (login or {}).get("open_id", ""),
        "login_url": _login_url("/"),
        "error": "" if (not login or allowed) else _login_denied_reason(login),
        "perms": _get_user_perms(login),
        "is_admin": _is_admin(login),
    }


@app.post("/api/feishu/logout")
async def feishu_logout(request: Request):
    login = await _current_login(request)
    sid = request.cookies.get(COOKIE_NAME, "")
    _sync_web_logins_from_disk()
    web_logins.pop(sid, None)
    _save_web_logins()
    _audit_log_from_login(request, login, "auth", "退出登录", "用户主动退出飞书登录")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ── 管理员：白名单 CRUD ──────────────────────────────────────

async def _require_admin(request: Request):
    login = await _current_login(request)
    if not _is_admin(login):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return login


@app.get("/api/admin/users")
async def admin_list_users(request: Request):
    await _require_admin(request)
    users = _load_whitelist()
    result = []
    for u in users:
        e = u.get("email", "").lower()
        result.append({
            "email": e,
            "perms": u.get("perms", ["survey", "annotate"]),
            "enabled": u.get("enabled", True),
            "is_admin": False,
        })
    # 追加管理员条目（仅展示，不可通过 UI 删除）
    admin_emails = set(FEISHU_ADMIN_EMAILS) | set(FEISHU_ALLOWED_EMAILS)
    existing = {u["email"] for u in result}
    for e in sorted(admin_emails):
        if e not in existing:
            result.insert(0, {"email": e, "perms": ["survey", "annotate"], "enabled": True, "is_admin": True})
        else:
            for u in result:
                if u["email"] == e:
                    u["is_admin"] = True
    return {"users": result}


@app.get("/api/admin/audit-logs")
async def admin_audit_logs(
    request: Request,
    start: str = "",
    end: str = "",
    user: str = "",
    feature: str = "",
    limit: int = 300,
):
    await _require_admin(request)
    limit = max(20, min(int(limit or 300), 1000))
    user = (user or "").strip().lower()
    feature = (feature or "").strip()
    start = (start or "").strip()
    end = (end or "").strip()
    if len(start) == 16:
        start = f"{start}:00"
    if len(end) == 16:
        end = f"{end}:59"

    logs = _load_audit_logs()
    filtered = []
    for item in logs:
        ts = str(item.get("ts", ""))
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        if user and str(item.get("user_email", "")).strip().lower() != user:
            continue
        if feature and item.get("feature") != feature:
            continue
        filtered.append(item)

    return {
        "logs": filtered[:limit],
        "users": _admin_user_rows(),
        "features": AUDIT_FEATURES,
        "total": len(filtered),
        "limit": limit,
    }


class AdminUserRequest(BaseModel):
    email: str
    perms: list[str] = ["survey", "annotate"]
    enabled: bool = True


@app.post("/api/admin/users")
async def admin_add_user(req: AdminUserRequest, request: Request):
    await _require_admin(request)
    identifier = req.email.strip().lower()
    if not identifier:
        raise HTTPException(status_code=400, detail="邮箱或 Open ID 不能为空")
    users = _load_whitelist()
    if any(u.get("email", "").lower() == identifier for u in users):
        raise HTTPException(status_code=409, detail="该账号已存在")
    valid_perms = [p for p in req.perms if p in ("survey", "annotate")]
    users.append({"email": identifier, "perms": valid_perms or ["survey"], "enabled": req.enabled})
    _save_whitelist(users)
    await audit_log(
        request,
        "admin",
        "添加用户",
        f"{identifier}，权限：{', '.join(valid_perms or ['survey'])}，状态：{'启用' if req.enabled else '禁用'}",
    )
    return {"ok": True}


class AdminUserPatch(BaseModel):
    perms: list[str] | None = None
    enabled: bool | None = None


@app.patch("/api/admin/users/{email}")
async def admin_update_user(email: str, req: AdminUserPatch, request: Request):
    await _require_admin(request)
    email = email.strip().lower()
    users = _load_whitelist()
    for u in users:
        if u.get("email", "").lower() == email:
            if req.perms is not None:
                u["perms"] = [p for p in req.perms if p in ("survey", "annotate")]
            if req.enabled is not None:
                u["enabled"] = req.enabled
            _save_whitelist(users)
            parts = []
            if req.perms is not None:
                parts.append(f"权限：{', '.join(u.get('perms', []))}")
            if req.enabled is not None:
                parts.append(f"状态：{'启用' if req.enabled else '禁用'}")
            await audit_log(request, "admin", "更新用户", f"{email}；" + "；".join(parts))
            return {"ok": True}
    raise HTTPException(status_code=404, detail="用户不存在")


@app.delete("/api/admin/users/{email}")
async def admin_delete_user(email: str, request: Request):
    await _require_admin(request)
    email = email.strip().lower()
    # 不允许删除 admin 邮箱
    if email in FEISHU_ADMIN_EMAILS or email in FEISHU_ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="管理员账号不可删除")
    users = _load_whitelist()
    new_users = [u for u in users if u.get("email", "").lower() != email]
    if len(new_users) == len(users):
        raise HTTPException(status_code=404, detail="用户不存在")
    _save_whitelist(new_users)
    await audit_log(request, "admin", "删除用户", email)
    return {"ok": True}


async def _export_to_feishu(report_md: str, login: dict, skip_qual: bool = False) -> str:
    """将报告上传为飞书文档（docx），文档归登录用户所有，并通过机器人发消息通知。"""
    full = _prep_export_md(report_md, skip_qual=skip_qual)  # 补免责声明 + 去掉 CORE_START/END 标记
    title_m = re.search(r"^#\s+(.+?)$", full, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else "调研报告"
    open_id = login.get("open_id", "")
    user_token = login.get("token", "")
    if not user_token:
        raise RuntimeError("缺少用户 access_token，请重新登录飞书后再导出")
    url, _ = await feishu_export.create_doc_as_user(title, full, user_token)
    print(f"[feishu-export] created doc title={title!r} url={url}")
    if open_id:
        await feishu_export.send_message_to_user(
            open_id,
            f"您的调研报告《{title}》已创建为飞书文档，点击查看：{url}"
        )
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
        url = await _export_to_feishu(report_md, login, skip_qual=(sess.get("mode") == "crosstab"))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"创建飞书文档失败：{e}")
    await audit_log(request, "report", "导出飞书文档", f"会话：{session_id}", metadata={"session_id": session_id})
    return {"url": url, "type": "doc"}


@app.post("/api/export/feishu-history/{history_id}")
async def export_feishu_history(history_id: str, request: Request):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用")
    login = await _current_login(request)
    if not login:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    history = _load_history()
    entry = _find_history_for_login(history, history_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    try:
        url = await _export_to_feishu(
            entry.get("report_md", ""), login,
            skip_qual=(entry.get("mode") == "crosstab"),
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"创建飞书文档失败：{e}")
    await audit_log(request, "report", "导出历史飞书文档", f"报告：{entry.get('title', history_id)}", metadata={"history_id": history_id})
    return {"url": url, "type": "doc"}


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
async def update_prompt(key: str, req: PromptUpdateRequest, request: Request):
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
    await audit_log(request, "settings", "修改 Prompt", f"{key}；备注：{_short_text(req.note or '')}")
    return {"ok": True, "key": key}


# ── UI 文案管理 ────────────────────────────────────────

UI_TEXTS_FILE = os.path.join(DATA_DIR, "ui_texts.json")

DEFAULT_UI_TEXTS: dict = {
    "panel_col_desc": {
        "key": "panel_col_desc",
        "label": "数据确认说明",
        "current": "AI 已识别每道题的题型与中文题名，请逐一核对并修正。题型直接影响后续统计口径。",
    },
    "panel_plan_desc": {
        "key": "panel_plan_desc",
        "label": "分析方案说明",
        "current": "AI 已规划以下分析方案，请确认或提出修改意见",
    },
    "panel_report_desc": {
        "key": "panel_report_desc",
        "label": "生成报告说明",
        "current": "AI 正在基于确定性统计数据撰写完整报告",
    },
    "panel_done_desc": {
        "key": "panel_done_desc",
        "label": "报告完成说明",
        "current": "报告已生成完毕，可下载或继续追问",
    },
    "qa_hint": {
        "key": "qa_hint",
        "label": "追问提示文字",
        "current": "对报告有疑问？直接提问，AI 会回到原始数据找答案",
    },
    "ann_panel_upload_desc": {
        "key": "ann_panel_upload_desc",
        "label": "数据标注·上传说明",
        "current": "上传问卷原始数据，支持 CSV / Excel（最大 50MB）",
    },
    "ann_panel_col_desc": {
        "key": "ann_panel_col_desc",
        "label": "数据标注·列确认说明",
        "current": "AI 已自动检测 ID 列和主观题列，请核对。主观题列将用于 AI 识别和质量打标。",
    },
    "ann_panel_run_desc": {
        "key": "ann_panel_run_desc",
        "label": "数据标注·识别中说明",
        "current": "正在分批分析受访者回答，请耐心等待",
    },
    "ann_panel_quality_desc": {
        "key": "ann_panel_quality_desc",
        "label": "数据标注·打标中说明",
        "current": "正在分批标注每道主观题的回答质量，请耐心等待",
    },
    "ann_panel_done_desc": {
        "key": "ann_panel_done_desc",
        "label": "数据标注·完成说明",
        "current": "所有标注任务已完成，可下载 Excel 文件",
    },
}


def _load_ui_texts() -> dict:
    if not os.path.exists(UI_TEXTS_FILE):
        _save_ui_texts(DEFAULT_UI_TEXTS)
        return DEFAULT_UI_TEXTS
    with open(UI_TEXTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    dirty = False
    for k, v in DEFAULT_UI_TEXTS.items():
        if k not in data:
            data[k] = v
            dirty = True
    if dirty:
        _save_ui_texts(data)
    return data


def _save_ui_texts(texts: dict) -> None:
    with open(UI_TEXTS_FILE, "w", encoding="utf-8") as f:
        json.dump(texts, f, ensure_ascii=False, indent=2)


@app.get("/api/ui-texts")
async def get_ui_texts():
    return _load_ui_texts()


class UiTextUpdateRequest(BaseModel):
    content: str


@app.put("/api/ui-texts/{key}")
async def update_ui_text(key: str, req: UiTextUpdateRequest, request: Request):
    texts = _load_ui_texts()
    if key not in texts:
        raise HTTPException(status_code=404, detail=f"ui-text '{key}' 不存在")
    texts[key]["current"] = req.content
    _save_ui_texts(texts)
    await audit_log(request, "settings", "修改页面文案", f"{key}；内容：{_short_text(req.content)}")
    return {"ok": True, "key": key}


# ── 历史记录 ──────────────────────────────────────────

@app.get("/api/history")
async def get_history(request: Request):
    login = await _current_login(request)
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    visible_history = [h for h in history if _visible_to_owner(h, login)]
    await audit_log(request, "report", "查看历史记录", f"历史报告数：{len(visible_history)}")
    # 列表视图不返回完整 report_md（节省带宽）
    return [
        {
            "id": h["id"],
            "report_no": h.get("report_no", ""),
            "filename": h["filename"],
            "title": h["title"],
            "created_at": h["created_at"],
            "has_qa": bool(h.get("analyst_conv_id")),
            "qa_count": _qa_user_count(h),
        }
        for h in visible_history
    ]


@app.get("/api/history/{hist_id}")
async def get_history_item(hist_id: str, request: Request):
    login = await _current_login(request)
    history = _load_history()
    history = _ensure_history_report_numbers(history)
    entry = _find_history_for_login(history, hist_id, login)
    if not entry:
        raise HTTPException(status_code=404, detail="历史记录不存在")
    await audit_log(request, "report", "打开历史报告", f"报告：{entry.get('title', hist_id)}", metadata={"history_id": hist_id})
    return entry


class HistoryTitleUpdateRequest(BaseModel):
    title: str


class HistoryTitleUpdateByIdRequest(BaseModel):
    id: str
    title: str


def _update_history_title_by_id(hist_id: str, title: str, login: dict | None = None) -> dict:
    hist_id = str(hist_id or "").strip()
    new_title = _sanitize_report_title(title)
    history = _load_history()
    history = _ensure_history_report_numbers(history, save=False)
    entry = _find_history_for_login(history, hist_id, login)
    if not entry:
        try:
            sess = get_session(hist_id)
            if sess.get("report_md") and _visible_to_owner(sess, login):
                _assign_session_owner(sess, login)
                sess["report_md"] = _replace_report_h1(sess.get("report_md", ""), new_title)
                save_session(hist_id, sess)
                save_to_history(hist_id, sess)
                history = _load_history()
                history = _ensure_history_report_numbers(history, save=False)
                entry = _find_history_for_login(history, hist_id, login)
        except HTTPException:
            pass
    if not entry:
        print(f"[history-title] not found id={hist_id!r} existing={[h.get('id') for h in history]}")
        raise HTTPException(status_code=404, detail="未找到这份报告，请刷新历史记录后重试")

    entry["title"] = new_title
    entry["report_md"] = _replace_report_h1(entry.get("report_md", ""), new_title)

    try:
        sess = get_session(hist_id)
        if sess.get("report_md"):
            sess["report_md"] = _replace_report_h1(sess.get("report_md", ""), new_title)
            save_session(hist_id, sess)
    except HTTPException:
        pass  # session 已过期，只更新历史记录即可

    _save_history(history)
    return {
        "ok": True,
        "id": hist_id,
        "report_no": entry.get("report_no", ""),
        "title": new_title,
        "report_md": entry.get("report_md", ""),
    }


@app.patch("/api/history-title")
async def update_history_title_by_body(req: HistoryTitleUpdateByIdRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(req.id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{req.id} → {result.get('title', req.title)}", metadata={"history_id": req.id})
    return result


@app.post("/api/history-title")
async def update_history_title_by_body_post(req: HistoryTitleUpdateByIdRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(req.id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{req.id} → {result.get('title', req.title)}", metadata={"history_id": req.id})
    return result


@app.patch("/api/history/{hist_id}/title")
async def update_history_title(hist_id: str, req: HistoryTitleUpdateRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(hist_id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{hist_id} → {result.get('title', req.title)}", metadata={"history_id": hist_id})
    return result


@app.post("/api/history/{hist_id}/title")
async def update_history_title_post(hist_id: str, req: HistoryTitleUpdateRequest, request: Request):
    login = await _current_login(request)
    result = _update_history_title_by_id(hist_id, req.title, login)
    await audit_log(request, "report", "修改报告名称", f"{hist_id} → {result.get('title', req.title)}", metadata={"history_id": hist_id})
    return result


# ============================================================
# 数据标注模块
# ============================================================

annotate_sessions: dict[str, dict] = {}


def _new_annotate_session() -> str:
    _clean_sessions()
    sid = str(uuid.uuid4())
    annotate_sessions[sid] = {"ts": time.time()}
    return sid


def _get_annotate_session(sid: str) -> dict:
    sess = annotate_sessions.get(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="标注会话不存在或已过期，请重新上传文件")
    sess["ts"] = time.time()
    return sess


# ── 上传（标注专用）─────────────────────────────────────

async def _translate_headers(headers: list) -> list:
    """将表头翻译为中文简体，失败时原样返回。"""
    if not DIFY_AI_DETECT_KEY:
        return headers
    query = (
        "将以下问卷列名按顺序翻译为中文简体，只输出 JSON 数组，不加其他任何内容：\n"
        + json.dumps(headers, ensure_ascii=False)
    )
    full_text = ""
    try:
        async for chunk, _ in sse_dify_stream(query, "hdr-translate", "", DIFY_AI_DETECT_KEY):
            full_text += chunk
        result = _parse_string_array(full_text)
        if result and len(result) == len(headers):
            return result
    except Exception:
        pass
    return headers


def _parse_string_array(text: str) -> list[str] | None:
    """从 LLM 输出中提取字符串数组，容忍值内部的裸双引号。"""
    m = re.search(r'\[.*\]', text, re.DOTALL)
    if not m:
        return None
    raw = m.group()
    # 先尝试直接解析
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return [str(r) for r in result]
    except json.JSONDecodeError:
        pass
    # 兜底：逐行提取——取每行最外层引号之间的内容
    items = []
    for line in raw.splitlines():
        line = line.strip().rstrip(',')
        if line.startswith('"') and line.endswith('"') and len(line) >= 2:
            items.append(line[1:-1])
    return items if items else None


@app.post("/api/annotate/upload")
async def annotate_upload(request: Request, file: UploadFile = File(...)):
    content = await file.read()
    try:
        rows = _parse_file(file.filename or "upload.csv", content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not rows or len(rows) <= 1:
        raise HTTPException(status_code=400, detail="文件为空或只有表头")

    headers = rows[0]
    body    = rows[1:]

    # 自动检测列（用 server.py 现有逻辑，含矩阵题过滤）
    id_col         = annotate.detect_id_column(headers, rows)
    open_text_cols = _detect_open_text_cols(rows, headers)

    # 矩阵子列索引（前端隐藏，不在可选列表中显示）
    matrix_col_idxs: list[int] = []
    for g in _group_googleform_matrix(headers):
        if g["type"] == "matrix":
            matrix_col_idxs.extend(g["member_indexes"])

    # 翻译表头（用于前端展示，不影响处理逻辑）
    headers_zh = await _translate_headers(headers)

    sid = _new_annotate_session()
    annotate_sessions[sid].update({
        "rows":            rows,
        "headers":         headers,
        "headers_zh":      headers_zh,
        "filename":        file.filename or "upload.csv",
        "id_col":          id_col,
        "open_text_cols":  open_text_cols,
    })

    result = {
        "session_id":       sid,
        "filename":         file.filename,
        "total_rows":       len(body),
        "headers":          headers,
        "headers_zh":       headers_zh,
        "id_col":           id_col,
        "open_text_cols":   open_text_cols,
        "matrix_col_idxs":  matrix_col_idxs,
        "preview":          rows[1: min(4, len(rows))],
    }
    await audit_log(
        request,
        "annotate",
        "上传标注数据",
        f"文件：{file.filename or 'unknown'}；样本行数：{len(body)}",
        metadata={"session_id": sid, "rows": len(body)},
    )
    return result


# ── 确认列 + 任务选择 ────────────────────────────────────

class AnnotateConfirmRequest(BaseModel):
    id_col: int
    open_text_cols: list[int]
    tasks: dict          # {ai_detect: bool, quality: bool}
    background: str = "" # 可选调研背景，用于 AI 检测


@app.post("/api/annotate/{sid}/confirm-columns")
async def annotate_confirm_columns(sid: str, req: AnnotateConfirmRequest, request: Request):
    sess = _get_annotate_session(sid)
    sess["id_col"]        = req.id_col
    sess["open_text_cols"] = req.open_text_cols
    sess["tasks"]         = req.tasks
    sess["background"]    = req.background
    sess["ai_results"]    = []
    sess["confirmed_ai_ids"] = []
    sess["quality_results"]  = []
    task_names = []
    if req.tasks.get("ai_detect"):
        task_names.append("AI 作答识别")
    if req.tasks.get("quality"):
        task_names.append("回答质量打标")
    await audit_log(
        request,
        "annotate",
        "确认标注任务",
        f"会话：{sid}；主观题列数：{len(req.open_text_cols)}；任务：{', '.join(task_names) or '未选择'}",
        metadata={"session_id": sid, "open_text_cols": len(req.open_text_cols), "tasks": req.tasks},
    )
    return {"ok": True}


# ── AI 作答识别（SSE）───────────────────────────────────

@app.get("/api/annotate/{sid}/run-ai-detect")
async def annotate_run_ai_detect(sid: str, request: Request):
    sess = _get_annotate_session(sid)
    rows           = sess.get("rows", [])
    headers        = sess.get("headers", [])
    id_col         = sess.get("id_col", 1)
    open_text_cols = sess.get("open_text_cols", [])
    background     = sess.get("background", "")

    if not rows or not open_text_cols:
        raise HTTPException(status_code=400, detail="会话状态不完整，请重新上传")
    if not DIFY_AI_DETECT_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_AI_DETECT_KEY")

    body = rows[1:]
    batch_size = annotate.AI_DETECT_BATCH
    batches = [body[i: i + batch_size] for i in range(0, len(body), batch_size)]
    total_batches = len(batches)

    def _has_open_text(row: list) -> bool:
        return any((str(row[c]) if c < len(row) else "").strip() for c in open_text_cols)

    def _row_id(row: list) -> str:
        return str(row[id_col]).strip() if id_col < len(row) else ""

    def _empty_ai_result(row: list, reason: str) -> dict:
        return {
            "id": _row_id(row),
            "ai_prob": 0,
            "is_polished": "low",
            "reason": reason,
            "evidence": "",
            "translations": {},
        }

    def _chunks(items: list, size: int) -> list[list]:
        return [items[i: i + size] for i in range(0, len(items), size)]

    async def _run_ai_direct(batch_rows: list[list], label: str) -> tuple[list[dict], str]:
        query = annotate.build_ai_detect_query(
            batch_rows, headers, open_text_cols, id_col, label, background
        )
        _annotate_ai_log("subbatch start", sid=sid, batch=label, rows=len(batch_rows), query_len=len(query))
        try:
            text, final_conv, mode, fallback_reason = await call_dify_compatible(
                query, f"{sid}-split-{label}", DIFY_AI_DETECT_KEY
            )
            _annotate_ai_log(
                "subbatch dify done",
                sid=sid,
                batch=label,
                mode=mode,
                answer_len=len(text or ""),
                fallback=bool(fallback_reason),
            )
            if not (text or "").strip():
                return [], "Dify 返回空内容"
            results, err = annotate.parse_ai_detect_result(text)
            if results:
                return results, ""

            retry_q = (
                f"上次输出无法解析（{err}）。请重新处理下面这批数据，并严格按 schema 用 ```json``` 围栏输出，"
                "不要附加任何解释文字。\n\n"
                f"{query}"
            )
            retry_text, _, retry_mode, retry_fallback = await call_dify_compatible(
                retry_q, f"{sid}-split-retry-{label}", DIFY_AI_DETECT_KEY, final_conv
            )
            _annotate_ai_log(
                "subbatch retry done",
                sid=sid,
                batch=label,
                mode=retry_mode,
                answer_len=len(retry_text or ""),
                fallback=bool(retry_fallback),
            )
            results, retry_err = annotate.parse_ai_detect_result(retry_text)
            return results, "" if results else retry_err
        except Exception as exc:
            _annotate_ai_log("subbatch failed", sid=sid, batch=label, error=str(exc)[:1000])
            return [], str(exc)

    async def generate():
        all_results: list[dict] = []
        try:
            yield sse_event({
                "type": "started",
                "rows": len(body),
                "total_batches": total_batches,
                "batch_size": batch_size,
                "msg": f"已连接，准备分析 {len(body)} 行，约 {total_batches} 批",
            })

            for batch_num, batch in enumerate(batches, 1):
                empty_rows = [r for r in batch if not _has_open_text(r)]
                active_batch = [r for r in batch if _has_open_text(r)]
                missing_ids = sum(1 for r in active_batch if not _row_id(r))
                yield sse_event({
                    "type": "batch_started",
                    "batch": batch_num,
                    "done": batch_num - 1,
                    "total": total_batches,
                    "rows": len(active_batch),
                    "skipped": len(empty_rows),
                    "missing_ids": missing_ids,
                    "msg": f"正在分析第 {batch_num}/{total_batches} 批（{len(active_batch)} 行，跳过 {len(empty_rows)} 行空主观题）",
                })
                if empty_rows:
                    all_results.extend(_empty_ai_result(r, "主观题为空，系统自动判定为非 AI 作答") for r in empty_rows)
                if missing_ids:
                    msg = f"第 {batch_num} 批有 {missing_ids} 行缺少玩家唯一 ID，结果可能无法正确回填"
                    _annotate_ai_log("missing ids", sid=sid, batch=batch_num, count=missing_ids)
                    yield sse_event({"type": "warn", "msg": msg})
                if not active_batch:
                    msg = f"第 {batch_num} 批没有可分析的主观题内容，已跳过 AI 调用"
                    _annotate_ai_log("skip empty batch", sid=sid, batch=batch_num, rows=len(batch))
                    yield sse_event({"type": "warn", "msg": msg})
                    yield sse_event({
                        "type": "batch_done",
                        "batch": batch_num,
                        "done": batch_num,
                        "total": total_batches,
                        "count": len(empty_rows),
                        "msg": f"第 {batch_num}/{total_batches} 批完成，空主观题 {len(empty_rows)} 行已自动跳过",
                    })
                    continue

                query = annotate.build_ai_detect_query(
                    active_batch, headers, open_text_cols, id_col, batch_num, background
                )
                _annotate_ai_log(
                    "batch start",
                    sid=sid,
                    batch=batch_num,
                    rows=len(active_batch),
                    skipped=len(empty_rows),
                    missing_ids=missing_ids,
                    query_len=len(query),
                )
                try:
                    dify_task = asyncio.create_task(call_dify_compatible(
                        query, sid, DIFY_AI_DETECT_KEY
                    ))
                    while not dify_task.done():
                        yield sse_event({
                            "type": "dify_waiting",
                            "batch": batch_num,
                            "total": total_batches,
                            "msg": "正在等待 AI 返回，请勿关闭页面",
                        })
                        await asyncio.sleep(12)
                    answer_text, final_conv, mode, fallback_reason = await dify_task
                    _annotate_ai_log(
                        "dify done",
                        sid=sid,
                        batch=batch_num,
                        mode=mode,
                        answer_len=len(answer_text or ""),
                        fallback=bool(fallback_reason),
                    )
                    yield sse_event({
                        "type": "dify_done",
                        "batch": batch_num,
                        "mode": mode,
                        "answer_len": len(answer_text or ""),
                        "msg": f"第 {batch_num} 批 AI 返回完成（{mode}，{len(answer_text or '')} 字符）",
                    })
                    if fallback_reason:
                        _annotate_ai_log(
                            "fallback",
                            sid=sid,
                            batch=batch_num,
                            reason=fallback_reason[:500],
                        )
                        yield sse_event({
                            "type": "warn",
                            "msg": f"第 {batch_num} 批 chat 调用不匹配，已自动改用 completion 调用：{fallback_reason[:240]}",
                        })
                except Exception as e:
                    _annotate_ai_log("dify failed", sid=sid, batch=batch_num, error=str(e)[:1000])
                    yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批 Dify 调用失败：{e}"})
                    split_results: list[dict] = []
                    if len(active_batch) > 1:
                        yield sse_event({
                            "type": "warn",
                            "msg": f"第 {batch_num} 批已自动拆成更小子批次重试，避免 Dify 插件/模型超时",
                        })
                        for sub_idx, sub_batch in enumerate(_chunks(active_batch, 2), 1):
                            sub_label = f"{batch_num}.{sub_idx}"
                            sub_results, sub_err = await _run_ai_direct(sub_batch, sub_label)
                            if sub_results:
                                split_results.extend(sub_results)
                                all_results.extend(sub_results)
                                yield sse_event({
                                    "type": "warn",
                                    "msg": f"第 {sub_label} 子批次重试成功，获得 {len(sub_results)} 条结果",
                                })
                            else:
                                yield sse_event({
                                    "type": "warn",
                                    "msg": f"第 {sub_label} 子批次仍失败：{sub_err}",
                                })
                    yield sse_event({
                        "type": "batch_done",
                        "batch": batch_num,
                        "done": batch_num,
                        "total": total_batches,
                        "count": len(split_results),
                        "msg": f"第 {batch_num}/{total_batches} 批完成，拆分重试获得 {len(split_results)} 条结果，跳过 {len(empty_rows)} 行空主观题",
                    })
                    continue

                results, err = annotate.parse_ai_detect_result(answer_text)
                if not results:
                    snippet = (answer_text or "")[:500].replace("\n", " ")
                    _annotate_ai_log(
                        "parse failed",
                        sid=sid,
                        batch=batch_num,
                        answer_len=len(answer_text or ""),
                        error=err,
                        snippet=snippet,
                    )
                    retry_q = (
                        f"上次输出无法解析（{err}）。请重新处理下面这批数据，并严格按 schema 用 ```json``` 围栏输出，"
                        "不要附加任何解释文字。\n\n"
                        f"{query}"
                    )
                    yield sse_event({
                        "type": "warn",
                        "msg": f"第 {batch_num} 批首次解析失败，正在自动重试：{err}；返回长度 {len(answer_text or '')}；片段：{snippet[:180]}",
                    })
                    try:
                        retry_task = asyncio.create_task(call_dify_compatible(
                            retry_q, sid, DIFY_AI_DETECT_KEY, final_conv
                        ))
                        while not retry_task.done():
                            yield sse_event({
                                "type": "dify_waiting",
                                "batch": batch_num,
                                "total": total_batches,
                                "msg": "正在等待 AI 重试返回，请勿关闭页面",
                            })
                            await asyncio.sleep(12)
                        retry_text, _, retry_mode, retry_fallback = await retry_task
                        _annotate_ai_log(
                            "retry done",
                            sid=sid,
                            batch=batch_num,
                            mode=retry_mode,
                            answer_len=len(retry_text or ""),
                            fallback=bool(retry_fallback),
                        )
                        results, err = annotate.parse_ai_detect_result(retry_text)
                    except Exception as e:
                        _annotate_ai_log("retry failed", sid=sid, batch=batch_num, error=str(e)[:1000])
                        results, err = [], str(e)

                if not results:
                    _annotate_ai_log("batch no results", sid=sid, batch=batch_num, error=err)
                    yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批解析失败：{err}"})
                    split_results = []
                    if len(active_batch) > 1:
                        yield sse_event({
                            "type": "warn",
                            "msg": f"第 {batch_num} 批将拆成更小子批次继续重试",
                        })
                        for sub_idx, sub_batch in enumerate(_chunks(active_batch, 2), 1):
                            sub_label = f"{batch_num}.{sub_idx}"
                            sub_results, sub_err = await _run_ai_direct(sub_batch, sub_label)
                            if sub_results:
                                split_results.extend(sub_results)
                                yield sse_event({
                                    "type": "warn",
                                    "msg": f"第 {sub_label} 子批次重试成功，获得 {len(sub_results)} 条结果",
                                })
                            else:
                                yield sse_event({
                                    "type": "warn",
                                    "msg": f"第 {sub_label} 子批次仍失败：{sub_err}",
                                })
                    if split_results:
                        all_results.extend(split_results)
                        results = split_results
                else:
                    all_results.extend(results)
                    _annotate_ai_log("batch parsed", sid=sid, batch=batch_num, count=len(results))

                yield sse_event({
                    "type": "batch_done",
                    "batch": batch_num,
                    "done": batch_num,
                    "total": total_batches,
                    "count": len(results),
                    "msg": f"第 {batch_num}/{total_batches} 批完成，获得 {len(results)} 条 AI 结果，跳过 {len(empty_rows)} 行空主观题",
                })

            sess["ai_results"] = all_results
            high_prob = [r for r in all_results if r.get("ai_prob", 0) >= 80]
            await audit_log(
                request,
                "annotate",
                "完成 AI 作答识别",
                f"会话：{sid}；结果数：{len(all_results)}；高概率数：{len(high_prob)}",
                metadata={"session_id": sid, "results": len(all_results), "high_prob": len(high_prob)},
            )
            yield sse_event({
                "type":      "ai_detect_done",
                "results":   all_results,
                "high_prob": high_prob,
            })
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# 用户确认 AI 结果
class AnnotateConfirmAIRequest(BaseModel):
    confirmed_ai_ids: list[str]  # 用户确认为 AI 作答的 player ID 列表


@app.post("/api/annotate/{sid}/confirm-ai")
async def annotate_confirm_ai(sid: str, req: AnnotateConfirmAIRequest, request: Request):
    sess = _get_annotate_session(sid)
    sess["confirmed_ai_ids"] = req.confirmed_ai_ids
    await audit_log(
        request,
        "annotate",
        "确认 AI 作答结果",
        f"会话：{sid}；确认 AI 作答数：{len(req.confirmed_ai_ids)}",
        metadata={"session_id": sid, "confirmed_count": len(req.confirmed_ai_ids)},
    )
    return {"ok": True, "confirmed_count": len(req.confirmed_ai_ids)}


# ── 质量打标（SSE）──────────────────────────────────────

@app.get("/api/annotate/{sid}/run-quality")
async def annotate_run_quality(sid: str, request: Request):
    sess = _get_annotate_session(sid)
    rows              = sess.get("rows", [])
    headers           = sess.get("headers", [])
    id_col            = sess.get("id_col", 1)
    open_text_cols    = sess.get("open_text_cols", [])
    confirmed_ai_ids  = set(sess.get("confirmed_ai_ids", []))

    if not rows or not open_text_cols:
        raise HTTPException(status_code=400, detail="会话状态不完整，请重新上传")
    if not DIFY_QUALITY_KEY:
        raise HTTPException(status_code=500, detail="未配置 DIFY_QUALITY_KEY")

    # 质量打标仅处理非 AI 行
    body = rows[1:]
    non_ai_body = [r for r in body if str(r[id_col]).strip() not in confirmed_ai_ids] if body and id_col < len(headers) else body
    batch_size = annotate.QUALITY_BATCH
    batches = [non_ai_body[i: i + batch_size] for i in range(0, len(non_ai_body), batch_size)]
    total_batches = len(batches)

    async def generate():
        all_results: list[dict] = []
        try:
            for batch_num, batch in enumerate(batches, 1):
                yield sse_event({"type": "progress", "done": batch_num - 1, "total": total_batches,
                                 "msg": f"正在打标第 {batch_num}/{total_batches} 批（{len(batch)} 行）…"})

                query = annotate.build_quality_label_query(
                    batch, headers, open_text_cols, id_col, batch_num
                )
                answer_chunks: list[str] = []
                final_conv = ""
                async for chunk, conv_id in sse_dify_stream(query, sid, "", DIFY_QUALITY_KEY):
                    if chunk:
                        answer_chunks.append(chunk)
                    if conv_id:
                        final_conv = conv_id

                results, err = annotate.parse_quality_result("".join(answer_chunks))
                if not results:
                    retry_q = (
                        f"上次输出无法解析（{err}）。请严格按 schema 用 ```json``` 围栏重新输出，"
                        "不要附加任何解释文字。"
                    )
                    retry_chunks: list[str] = []
                    async for chunk, _ in sse_dify_stream(retry_q, sid, final_conv, DIFY_QUALITY_KEY):
                        if chunk:
                            retry_chunks.append(chunk)
                    results, err = annotate.parse_quality_result("".join(retry_chunks))

                if not results:
                    yield sse_event({"type": "warn", "msg": f"第 {batch_num} 批解析失败：{err}"})
                else:
                    all_results.extend(results)

            sess["quality_results"] = all_results
            await audit_log(
                request,
                "annotate",
                "完成回答质量打标",
                f"会话：{sid}；结果数：{len(all_results)}",
                metadata={"session_id": sid, "results": len(all_results)},
            )
            yield sse_event({"type": "quality_done", "count": len(all_results)})
        except Exception as e:
            import traceback; traceback.print_exc()
            yield sse_event({"type": "error", "message": str(e)})

    return StreamingResponse(generate(), media_type="text/event-stream")


# ── 下载标注 Excel ───────────────────────────────────────

@app.get("/api/annotate/{sid}/download")
async def annotate_download(sid: str, request: Request):
    sess = _get_annotate_session(sid)
    rows              = sess.get("rows")
    headers           = sess.get("headers")
    id_col            = sess.get("id_col", 1)
    open_text_cols    = sess.get("open_text_cols", [])
    tasks             = sess.get("tasks", {})
    ai_results        = sess.get("ai_results", [])
    confirmed_ai_ids  = set(sess.get("confirmed_ai_ids", []))
    quality_results   = sess.get("quality_results", [])
    filename          = sess.get("filename", "annotated")

    if not rows:
        raise HTTPException(status_code=400, detail="会话中没有数据")

    loop = asyncio.get_event_loop()
    excel_bytes = await loop.run_in_executor(
        None,
        annotate.generate_annotated_excel,
        rows, headers, ai_results, confirmed_ai_ids,
        quality_results, open_text_cols, id_col, tasks,
    )

    stem = re.sub(r"\.(csv|xlsx|xls)$", "", filename, flags=re.IGNORECASE)
    safe = re.sub(r'[\\/:*?"<>|]', "_", stem)
    await audit_log(request, "annotate", "下载标注结果", f"会话：{sid}；文件：{filename}", metadata={"session_id": sid})
    return _make_download_response(
        excel_bytes,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        f"{safe}_标注结果.xlsx",
    )


# ── 启动 ────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
