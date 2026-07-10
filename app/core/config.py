"""core/config:项目级配置统一入口。

环境变量、Dify/飞书配置、DATA_DIR 及各数据文件路径、阈值、默认提示词文案、
免责声明文案、核心结论标记。所有配置只此一处定义,其余模块从这里 import。

边界:只读 .env 与定义常量,不含业务逻辑、不读写业务数据文件(数据读写在 storage)。
"""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env"))

DIFY_API_BASE      = os.getenv("DIFY_API_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_API_KEY       = os.getenv("DIFY_API_KEY", "")         # dify 客户端 fallback 变量
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
DIFY_COMMENT_ANALYSIS_KEY = os.getenv("DIFY_COMMENT_ANALYSIS_KEY", "")  # 帖子评论舆情分析（单 Workflow，mode 路由）

# 大样本分析阈值：开放题总回复数超过此值时自动启用批处理模式
LARGE_SAMPLE_THRESHOLD = 500
BATCH_SIZE = 300  # 每批发给 Dify 的回复数量
OTHER_THEME_PCT = 5.0  # 占比低于此值的主题合并入「其他声音」

# 评论舆情分析：并发上限（同时打 Dify 的批次数）。20 批全开易触发上游 429，
# 用 Semaphore 限流，配合 workflow_run 的指数退避重试，兼顾速度与稳定。
COMMENT_ANALYSIS_CONCURRENCY = 6
COMMENT_QUOTE_SELECT_CONCURRENCY = 2
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
DATA_DIR = os.getenv("DATA_DIR") or os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

PROMPTS_FILE   = os.path.join(DATA_DIR, "prompts.json")
WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")
WEB_LOGINS_FILE = os.path.join(DATA_DIR, "web_logins.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
AUDIT_LOG_FILE = os.path.join(DATA_DIR, "audit_logs.json")
APP_SETTINGS_FILE = os.path.join(DATA_DIR, "app_settings.json")
UI_TEXTS_FILE = os.path.join(DATA_DIR, "ui_texts.json")
ANNOTATE_RESULT_DIR = Path(DATA_DIR) / "annotate_results"
MAX_HISTORY  = 20
MAX_AUDIT_LOGS = max(200, _env_int("AUDIT_LOG_MAX", 5000))

# ============================================================
# 默认提示词文案（作为 prompts 的初始/兜底值；持久化逻辑在 storage/prompts）
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
9. 不要复制 `<stats>` 整块，但可以原样引用其中的表格
10. 报告需体现「证据 → 洞察 → 产品含义 → 建议动作」的叙事逻辑：每个主要观点尽量绑定用户原话或统计依据，说明这些证据反映了什么洞察、这对产品意味着什么，进而给出可执行动作；用户未提供业务背景时，也要按此标准和通用高质量定性报告要求提升表达与建议质量，不因缺少背景而降低产出标准。
11. 若上文存在 `<business_context>` 或已挂载知识库检索：术语库仅用于准确理解被调研业务的术语含义，不作为内容来源；优秀案例库仅供学习表达范式、洞察结构与建议颗粒度，**不得**直接复制案例中的具体事实、数据或原文。
12. 在核心结论和 `## Bug 或待确认问题` 模块（如有）之后，追加 `## 行动建议` 模块（3-5 条）：每条须包含具体产品动作、建议优先级（高/中/低）、如何验证该建议（例如需要哪类数据、用户调研或 A/B 实验）、以及该建议存在的不确定性或前提假设；每条建议必须能在 `<stats>` 或 `<open_text>` 中找到对应依据，不得凭空提出。\
"""

# 报告免责声明（确定性插入到标题下方，不依赖 LLM）
REPORT_DISCLAIMER = "> 该报告使用智能调研分析工具产出，如有疑问，请联系开发者@宋润佳(Nancy)"
# 定性模式完整免责声明（倍市得/crosstab 模式与评论分析模式不插此段）
QUALITATIVE_DISCLAIMER = "> 该调研为定性调研，报告中所有涉及打分、统计的数据仅作为参考，不具备定量意义，也无法与用研的满意度定量评分对比，同时不适用于定量分数的评价体系。请阅读者重点关注玩家的主观反馈内容。"
# 评论舆情分析模式免责声明（mode=="comment" 时插此段，替代定性声明）
COMMENT_DISCLAIMER = "> 该报告基于抽样评论，由智能分析工具自动完成主题归类与情感统计，所有占比、情感分布等数据均为模型判断结果，可能存在误差，仅供参考，不具备严格统计意义。请重点关注评论原文及代表性引用所反映的真实声音。"
# 核心结论包裹标记（writer 按要求输出，飞书导出时据此定位转高亮块）
CORE_START = "<!--CORE_START-->"
CORE_END = "<!--CORE_END-->"

DEFAULT_PLANNER_EXTRA = """\
请按 JSON schema 输出列分类、part 划分、交叉分析建议、open_questions。
open_questions 中的问题请以「我计划…，请确认是否这样做？」的格式提出，不要对已由用户确认的列类型再次提问。
若上文存在 `<business_context>`：请优先围绕其中的分析目标、目标用户和最关心的问题来规划报告章节与分析重点（可适当调整 parts 顺序、cross_tabs 或 open_questions，使其更贴合该业务目标），但仍需遵守已确认的题型/选项等既有约束，不因业务目标而改变 columns。\
"""
