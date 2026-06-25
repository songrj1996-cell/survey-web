# Survey-Web 开发进度文档

> 分支：`main`（原 `feat/crosstab-report` 已于 2026-06-22 合并为主干）
> 本地最新版本：`v3.2-dev`（基于 `8063bda` 后的本地未提交改造）
> 分支起步基线：`c64f229`（2026-06-03）
> **当前云上部署版本：`4c8f4fd`（2026-06-15）** —— v3.0 / v3.1 及端口改动尚未部署到云端
> 最后更新：2026-06-25
> 代码路径：`c:\Users\admin\Desktop\survey-web`
> 旧主干已归档为远端分支 `legacy-main-v2.5`

---

## 〇、当前主干状态（2026-06-25）

- 原 `feat/crosstab-report` 跑数表分支已**合并为主干 `main`**，并推送至远端 `origin/main`。
- 历史 `main`（旧倍市得 stats 改造方案）归档为 `origin/legacy-main-v2.5`，本主干**不含** `beisde_parser.py` / `stats_advanced.py` 等旧代码。
- 平台现包含两条互不影响的报告生成路径：
  - **定性分析**（5 步，标准模式）：v3.0 起改为「**同会话分章多轮生成**」，规避 Dify 单次 10 分钟超时。
  - **定量分析 / 倍市得跑数表**（4 步，crosstab 模式）：解析现成跑数表 + 主观题聚类 + 大样本 Writer。
- v3.1 完成前端品牌化改版（「调研分析平台」）、Dify workflow 输出兼容、开放题 LLM 失败兜底。
- v3.2-dev 本地新增「评论分析」链路：大文件流式预处理、**主题相关性筛选 + 动态补样**、抽样分析、Dify 评论主题/分类/简报、**玩家评论原文精选（后台并行）**、评论历史报告、重复文件提醒开关。
- v3.2-dev 同时落地三项跨模块改造：**新增「评论分析」独立权限**（survey/annotate/comment 三权限，老用户自动迁移）、**历史记录改版**（最近 20 条、类型/日期筛选、数据标注结果可下载、评论报告可回看）、**数据标注结果落历史**（完成即落盘 Excel，历史卡片直接下载）。

> ⚠️ **部署落后提醒**：云端当前运行 `4c8f4fd`（6/15）。本地包含未部署的 v3.0 / v3.1，以及 2026-06-25 的评论分析 v3.2-dev 改造。下次部署需一并回归：标准模式分章多轮、前端改版、评论分析上传/预处理/相关性筛选/原文精选/历史报告/重复提醒、comment 权限迁移、历史记录改版、标注结果落历史。

---

## 一、跑数表模式是什么

一个**精简、可快速投产**的报告生成路径，专做倍市得「跑数表模式」。核心思路是**减法**：用研在倍市得后台已产出专业交叉统计表（跑数表），平台**不再自算任何统计**，只解析现成数字 + 主观题聚类 + LLM 编织。该路径从 6/3 干净基线起步，不含 `beisde_parser.py` / `stats_advanced.py` 等旧代码。

### 为什么做

旧方案把统计计算放在平台内，开发中数据环节频繁报错。新思路是**减法**：

- 用研在倍市得后台已产出专业交叉统计表（跑数表），平台**不再自算任何统计**；
- 平台只做：① 解析跑数表拿现成数字；② 对主观题按题聚类（共性问题 + 主要观点 + 标志性原文）；③ LLM 把数字 + 主题编织成报告。

---

## 二、基线继承（来自 c64f229，未改动）

- 定性分析全流程（上传 → AI 识别题型 → 方案 → 写报告 → 追问）
- 多格式导出：Word / PDF / 飞书文档 / Markdown
- 数据标注模块
- 飞书 OAuth 登录（强制）+ 邮箱白名单权限 + 操作审计日志

---

## 三、各步骤 Python / LLM 分工

**所有统计数字零 LLM —— Python 直读跑数表，LLM 只碰语义/文字活。**

| 步骤 | Python（确定性） | LLM |
|------|----------------|-----|
| 1 上传 | 跑数表解析、清数解析、问卷转文本、自动建列 | 无 |
| 2 方案确认 | 解析/校验章节 JSON、渲染卡片 | **读问卷原文 → 章节大纲 + 待确认问题**；修改 → 重出章节 |
| 3 生成报告 | **stats_md = 跑数表直接渲染（零计算）**、收开放题原文、聚类的计数/占比汇总 | 主观题聚类语义三阶段、**写报告** |
| 4 报告&追问 | PDF / Word / 飞书文档 导出 | 追问问答 |

---

## 四、目标流程（4 步，不经过「数据确认」）

```
1. 上传        三文件（问卷 + 清数 + 跑数表）→ 自动建列，直达方案确认
2. 方案确认    AI 读问卷原文 → 章节大纲 + 待确认问题（非数据类）→ 人工确认/编辑
3. 生成报告    跑数表数字 + 主观题聚类 → 大样本 Writer
4. 报告&追问   展示 + 追问 + 导出 PDF / 飞书文档
```

定性分析路径（5 步）保持不变，互不影响。

---

## 五、已完成改动明细

### 5.1 跑数表解析器（`crosstab_parser.py`，新增）

- zip + XML 逐格还原单元格，绕开 openpyxl 对 WPS 导出 `dimension` 写错导致读空表的问题。
- 分组维度 / 分段列数完全动态（手感版 = 总体 + 段位2 + 常玩分路5 = 8段；未来分组会变）。
- 自动识别两版模板：当前版（单列百分比）+ 旧版（计数/百分比配对，取百分比列）。
- 打分题均分行识别；矩阵题按冒号拆题组 + 子项。
- `question_names()` 暴露跑数表中文题目清单（矩阵题去重）供 planner 用。

### 5.2 主观题聚类引擎（`dify.py` / `server.py`，从 c3545a4 移植）

- `dify.workflow_run()`：调 Dify Workflow 应用（阻塞模式，带重试）。
- `_batch_qualitative_analysis()`：四阶段——A 分批提主题 → B 合并去重 → C 回跑分类（含情感）→ D 汇总（主题 + 计数 + 占比 + 正负摘要 + 代表原文）。
- 每主题代表原文：正/负/中性各最多 2 条，总量最多 6 条；不足 3 条时从 pool 补充，保证 Writer 有足够素材。
- `_build_large_sample_writer_query()` / `_get_large_sample_writer_requirements()`：大样本 Writer prompt 构造。
- 4 个 Dify Workflow key：`DIFY_THEME_EXTRACT_KEY`、`DIFY_THEME_MERGE_KEY`、`DIFY_CLASSIFY_KEY`、`DIFY_LARGE_ANALYST_KEY`。

### 5.3 跑数表模式流程（`server.py`）

- **`POST /api/upload/crosstab`**：三文件上传（问卷 + 清数 + 跑数表），确定性自动建列（`__open` → open_text，画像列 → profile_dim，其余 → ignore），上传即完成列确认，直达方案确认。
- **`/api/plan`**：crosstab 分支调 `DIFY_CROSSTAB_PLANNER_KEY`，输入问卷原文 + 可用题目清单 + 开放题清单，输出 `{parts:[{name, scope}], open_questions:[]}`（章节语义化，不绑定列号；只问报告结构类问题）。
- **`/api/plan/confirm`**：crosstab 分支支持章节大纲修订（保留问卷原文上下文重新出章节）。
- **`/api/stats`**：crosstab 模式跳过数值计算，`stats_md` 取跑数表渲染，`open_text` 只收开放题原文。
- **`/api/report`**：crosstab 模式恒定走聚类（不受 ≥500 阈值限制），注入问卷原文作为 Writer 意图上下文；`analyst_app` 字段记录使用的 Dify 应用，追问和历史导出据此选正确 key。

### 5.4 辅助函数（`server.py`）

- `_build_crosstab_columns()`：确定性建列，含问卷 Q#→题名映射（`_questionnaire_title_map()`）。
- `_build_crosstab_planner_query()` / `_build_crosstab_plan_revision_query()`：planner prompt 构造。
- `_render_crosstab_plan_card()`：章节大纲卡片渲染（name + scope，不显示列号）。
- `_analyst_key_for_report(obj)`：追问/历史导出时根据 `analyst_app` / `mode` 选正确 Dify key，修复跨应用 conversation 404。

### 5.5 plan 解析（`survey_plan.py`）

- 新增 `parse_crosstab_plan()`：轻校验，只校验 `parts`（含 name/scope）和 `open_questions`，不要求 columns/列号。

### 5.6 开放题原文收集（`survey_stats.py`）

- 新增 `collect_open_text(rows, plan)`：只收开放题原文（带 ids/profile），不跑数值统计。

### 5.7 文件持久化 session（`server.py`）

- 替换原内存 `sessions = {}` → 每个 session 存为 `data/sessions/<uuid>.json`，`tmp + os.replace` 原子写入，**多 worker 安全，重启不丢 session**（TTL = 2 小时）。
- 所有写 session 的端点末尾加 `save_session()` 调用；`get_session()` 加载时自动恢复 int key。
- 启动时调用 `_sweep_old_sessions()` 清理过期文件。
- `save_to_history()` 同步存入 `mode`、`analyst_app` 字段，供历史报告导出判断。

### 5.8 免责声明按模式分流（`server.py`）

- `_inject_disclaimer(md, skip_qual=False)`：crosstab 模式只插短句，并主动清除旧定性免责声明；定性模式保留完整两段。
- `_prep_export_md(md, skip_qual=False)`：透传 `skip_qual`。
- 所有导出路径（PDF / Word / Markdown / 飞书文档，当前 session + 历史记录）均按 `mode`/`plan.mode` fallback 正确传参。

### 5.9 飞书导出改为飞书文档（`server.py`）

- `_export_to_feishu()` 改调 `feishu_export.create_doc_as_user()`，文档以 OAuth 登录用户身份创建（文档归该用户所有）。
- 飞书文档导出走 `_prep_export_md()`，会 strip `<!--CORE_START-->` / `<!--CORE_END-->` 标记。
- 历史报告飞书导出端点同步更新（原"上传历史飞书 PDF" → "导出历史飞书文档"，`type: "doc"`）。
- `.env` 需要 `FEISHU_SCOPE=drive:file:upload`；已有账号需退出重新登录才能获得新权限。

### 5.10 前端（`static/app.js` / `index.html` / `style.css`）

- 启用首页「**定量分析（倍市得跑数表）**」入口，三文件上传后直达方案确认（Step 3），跳过题型确认（Step 2）。
- 步骤条按模式：crosstab = 4 步（上传 / 方案确认 / 生成报告 / 报告&追问）；定性 = 5 步。
- 方案卡片：crosstab 模式渲染 `name + scope` 语义章节，隐藏交叉分析区块，保留待确认问题。
- 报告流式阶段：progress 事件实时展示聚类进度；用 `marked.parse` 实时渲染，消除 `**` 字面量显示。
- 报告完成后渲染：`renderMarkdown()` 预处理 `**bold**`，修复 Unicode 序号（①②）+ 中文括号旁加粗不渲染的问题。
- 方案修订：失败时自动恢复原方案卡片；`showPlanCard` 末尾强制同步按钮状态（防 disabled 残留）。
- `consumeSSE` 空 catch 改为区分 JSON parse 错和 onEvent 渲染错，渲染错 reject + console.error。
- 进度状态栏 `#progress-status-text`：长耗时阶段显示"最后更新时间 + 当前步骤"。
- 按钮/文案同步：「上传飞书 PDF」→「导出飞书文档」；弹窗说明同步更新。

### 5.11 Writer 要求（`server.py` / `_get_large_sample_writer_requirements`）

- 报告结构固定顺序：核心结论 → Part 1 受访者画像（固定）→ 其余 Part → 行动建议。
- 受访者画像用 Markdown 表格展示，后跟 1-2 句解读。
- 主观题每观点至少引用 3 条原文，展示原语种 + 中文翻译；若可用原文不足 3 条则展示全部，不编造。
- **满意度优先原则**：报告中若有满意度/好评率相关数据，必须作为核心结论最靠前的 1-2 条展示。

### 5.13 Writer Part 编号修复 + 满意度数据结构化注入（`server.py`）

- `_build_large_sample_writer_query()`：`parts_lines` 初始化时固定写入「Part 1 受访者画像（固定）」，原方案 parts 从 Part 2 开始编号，彻底解决 Writer 收到矛盾结构信息导致出现两个 Part 1 或编号错位的问题。
- 新增 `_extract_satisfaction_stats(stats_md)`：扫描 `<stats>` 中标题含「满意度」的 `##` 节，有则注入 `<priority_metrics>` 块。
- `_get_large_sample_writer_requirements(has_satisfaction)` 新增参数：有满意度数据时将「满意度优先原则」由模糊描述改为明确指向 `<priority_metrics>`，无数据时保持原文。

### 5.14 飞书文档导出修复（`server.py` / `feishu_export.py`）

- **根本原因**：`_export_to_feishu()` 原来调 `create_doc_as_user()`（用 `user_access_token`），需要飞书开放平台「用户身份」版 `drive:drive` 和 `docs:document:import` 权限；而平台只开了「应用身份」版，导致 code=99991679。
- **修复**：改调 `create_doc_via_bot()`（用 `app_access_token`），与已开通的「应用身份」权限完全匹配，文档建好后自动 transfer_owner 给登录用户。
- `feishu_export.py`：`FEISHU_SCOPE` 改回硬编码 `""`（与 survey-web 保持一致），不读 env，不限制 OAuth 授权范围。`.env` 中 `FEISHU_SCOPE=` 保留但置空。
- 两个导出端点均加 `print([feishu-export*][ERROR] ...)` 调试输出，方便排查飞书原始错误码。

### 5.12 部署修复（`deploy.sh` / `.gitattributes`）

- 新增 `.gitattributes`，全仓库文本文件统一 LF，`*.sh eol=lf` 防止 Windows 把 `\r` 写入脚本导致 Bash 解析失败。
- `deploy.sh` 转 LF，`bash -n` 验证通过。`22fad39` 起默认端口改为 `18081`，host 改为 `0.0.0.0`，部署目录 `/opt/survey-web`，systemd 服务名 `survey-web`，运行用户 `www-data`。

### 5.15 标准模式分章多轮生成报告（`server.py`，v3.0）

> **背景**：原标准（定性）模式单次大请求把全量数据 + 全部要求一次性喂给 Writer，章节多时单次推理超过 Dify 10 分钟上限而超时。v3.0 改为**同一 conversation 多轮**：第 1 轮喂全量上下文但只写标题并建立会话，之后复用 `conversation_id`，每轮只写一个 Part / Bug 模块 / 核心结论，原文不再重发，单轮输出短不超时。

- 新增辅助函数：`_writer_parts_meta()`（章节元信息编号）、`_build_writer_context()`（拼 plan 摘要 + open_text + requirements）、`_build_writer_first_query()`（第 1 轮：全上下文 + 只写标题）、`_build_writer_part_query()`（逐 Part）、`_build_writer_bug_query()`（Bug 模块单独一轮）、`_build_writer_core_query()`（核心结论最后一轮）。
- 轮次：标题(1) → 逐 Part(N) → Bug 模块(1) → 核心结论(1)，共 `len(parts)+3` 轮，前端 progress 实时显示「分章生成 x/N」。
- **标题轮防抢跑**：只保留首个 `## ` 之前的文本作为标题段，防止模型一次性写完后续章节。
- **Bug 模块按需插入**：模型回 `NONE`（或无 `## Bug` 标题）则不插入，仅在真有待确认问题时输出。
- **核心结论最后生成**：汇总全部章节后拼到报告顶部（标题 → 核心结论 → 各 Part → Bug 模块）。
- 移除 `open_text` 前 200 条截断限制，开放题全量传入。
- 该多轮逻辑仅作用于**标准/定性模式**；crosstab 模式仍走单次大样本 Writer（`DIFY_LARGE_ANALYST_KEY`）。

### 5.16 前端品牌化改版（`static/*`，v3.1）

- 品牌化：产品更名「**调研分析平台**」（原 Survey Insight），新增 favicon + 顶部 logo（`static/web-icon.jpg`），登录页同步；后端新增 `/favicon.ico` 路由。
- 新增「**选择分析入口**」首页 + 倍市得跑数表三文件上传 UI（问卷 / 数据 / 跑数表）重做。
- `static/style.css` 大幅重构，统一为「数据分析工作台」克制风格；新增前端设计规范见 `skills/survey-web-frontend-development`。

### 5.17 Dify workflow 输出兼容（`dify.py`，v3.1）

- 新增 `_pick_workflow_output(outputs)` / `_stringify_workflow_output()`：Dify workflow 应用的输出变量名不固定（不一定叫 `output`），现按 `output / result / answer / text` 优先取，单输出则直接取唯一字段，否则取首个非空字段——在 Dify 里重命名输出变量不再导致平台静默取到空串。
- `workflow_run()` 补充命中日志：打印 `output_key` / 全部 `outputs` 键名 / `output_len`，便于排查。

### 5.18 开放题兜底 + 宽松解析 + 聚类诊断（`server.py`，v3.1）

- 新增 `_build_open_text_fallback_md()`：某开放题聚类（提主题 / 合并 / 分类任一阶段）失败或为空时，构造确定性的原文兜底 Markdown（`<open_text_fallback>`），保证 Writer 仍有素材，不至于整段开放题缺失；带上下文上限保护。
- 聚类各阶段失败时通过 progress 事件提示「将尝试使用原文兜底」，并在报告生成前汇总 `failed_cols` 告知用户哪些主观题走了兜底。
- 题型识别 LLM 失败时本地兜底：用启发式 + 矩阵分组拼出 questions。
- JSON 宽松解析：逐行提取兜底，容忍模型输出的非严格 JSON。

### 5.19 评论分析大文件链路（`comment_analysis.py` / `server.py` / `static/*`，v3.2-dev）

> **目标**：支持常见 10-20 万条评论、偶发 50 万条评论；AI 侧不全量分析，先抽样、再按帖子主题筛出相关评论后分析，避免 Dify 成本和超时不可控。当前功能为本地未提交改造，已在真实文件上跑通预处理和完整分析，但仍需继续观察 Dify classify 稳定性。

#### 后端流程

- **上传拆分**：
  - `POST /api/comment-analysis/upload` 只负责保存原始文件到 `data/comment_uploads/<sid>.<ext>`、创建 session、计算文件 `sha256`。
  - `GET /api/comment-analysis/preprocess/{session_id}` 负责 SSE 流式预处理：读取文件、识别评论列、清洗、抽样、保存 `comment_sample`。
  - `GET /api/comment-analysis/run/{session_id}` 只在预处理完成后启动 Dify 分析。
- **流式预处理**（独立 SSE，前端实时显示扫描/有效/抽样进度）：
  - CSV / XLSX 均不再构造完整 `comments` / `valid` 两份大列表。
  - CSV 使用 `errors="replace"` 容错读取，单个脏编码字节不会导致整批失败。
  - XLSX 使用 `openpyxl` 只读模式按行读取。
  - 默认扫描上限 `COMMENT_MAX_SCAN_ROWS = 500_000`；空行连续 `EXCEL_STOP_AFTER_BLANK_ROWS = 2_000` 时提前停止。
  - `StreamingCommentSampler` 同时维护一个「长评论候选堆」（清洗后字数最长的最多 `LONG_CANDIDATE_MAX_N = 1500` 条），供后续「玩家评论原文精选」使用。
  - 抽样池上限提升到 `SAMPLE_POOL_MAX_N = 15000`（初始分析仍先取 `SAMPLE_MAX_N = 5000`），为相关性筛选不足时的动态补样留余量。
- **评论列识别**：
  - 优先精确匹配 `message / 评论 / 内容 / 留言 / content / text / comment`。
  - 排除 `comment_id / commentid / 评论id / 评论_id`，避免把 ID 列误判为正文列。
  - 表头不明确时按前 500 行内容打分：平均长度、语言字符比例、唯一值比例、是否像 ID/时间戳。
- **清洗与抽样**：
  - 单条评论即时清洗：过短、键盘乱打、无语言字符、纯索要奖励、重复。
  - 重复检测改为保存 `sha1(text.lower())`，降低内存。
  - `StreamingCommentSampler` 按长度分层做 reservoir sample，最终最多 `SAMPLE_MAX_N = 5000` 条。
- **Dify 分析链路**（全程同一 Workflow，靠 `mode` 路由 + Semaphore 限流）：
  - `relevance`（主题相关性筛选，**取代原 filter 关键词路径**）→ `extract` → `merge` → `classify` → 本地统计 → `report`。
  - **Phase 0 相关性筛选 + 动态补样**：先对初始 5000 抽样逐批（`RELEVANCE_BATCH_SIZE = 40`，带 idx）问 Dify 哪些评论与帖子主题/正文相关；若相关数不足 `RELATED_TARGET_N = 1000`，从样本池继续补抽下一批 5000，直到达标或抽样池（15000）耗尽。只有相关评论进入后续 extract/classify。
  - relevance 是筛选任务：Dify 可只返回相关 idx，未返回的默认按「无关」处理，不为无关评论反复重试；格式不符时拆批重试。
  - `extract` 批次 `BATCH_SIZE = 250`；`merge`/`extract`/`classify`/`report` 均补传 `post_content` 作为上下文。
  - `classify` 批次 `CLASSIFY_BATCH_SIZE = 40`，输入协议 `[{idx, text}]`，Dify 输出必须带回 `idx`；本地按 idx 回填，缺失重试，仍缺失自动拆小批补跑；遇 Dify 400（STOP_SIGNAL）直接中止整批，避免静默丢评论导致统计偏小却显示成功。
- **玩家评论原文精选（Phase 6，后台并行）**：
  - 从长评论候选池分批（`QUOTE_SELECT_BATCH_SIZE = 75`，每批保留 `QUOTE_SELECT_BATCH_KEEP_N = 5`）经 `quote_select_batch` 初选，再用 `quote_select_final` 从候选池（上限 `QUOTE_SELECT_FINAL_POOL_N = 150`）精选最多 `QUOTE_SELECT_FINAL_N = 50` 条，翻译成简体中文。
  - 该任务在 run SSE 中作为**独立 asyncio task 并行启动**：舆情主报告先生成并返回（`comment_done`），原文精选完成后再通过 `comment_quotes_done` 事件追加到报告末尾；失败走 `comment_quotes_error`，**不影响已生成的主报告**。
  - 报告中「## 玩家评论原文精选」以**有序列表**呈现，每条译文压成单行；模块级函数 `_comment_selected_raw_comments_md` / `_select_comment_raw_quotes` 负责渲染与精选（已清理早期遗留的不可达旧实现）。
- **样本口径**：
  - 生成报告时插入样本口径引用块：扫描行数、非空评论数、有效评论数、抽样数、**相关性筛选用量 / 相关条数 / 剔除无关条数**、是否截断。
  - `comment_sample_meta` / `comment_relevance_stats` 保存到 session 和历史记录，供前端/导出展示。
  - 帖子标题 + **帖子正文均改为必填**（相关性筛选依赖正文）。

#### 历史报告与重复文件提醒

- 评论分析完成后调用 `save_to_history(session_id, sess)`，历史记录 `mode = "comment"`。
- 历史记录额外保存：
  - `comment_file_hash`
  - `comment_source_filename`
  - `comment_result`
  - `comment_sample_meta`
  - `comment_valid_count`
  - `comment_sample_count`
  - `comment_scan_rows`
  - `comment_nonempty_count`
- 新增平台设置文件：`data/app_settings.json`。
  - 默认：`comment_duplicate_reminder_enabled = true`
  - 管理员接口：`GET /api/app-settings`、`PATCH /api/app-settings`
- 上传同文件时，后端按当前用户可见历史 + `comment_file_hash` 查重。
  - 开关开启：返回 `duplicate_report`，前端弹窗让用户选择「查看历史报告 / 仍然重新分析 / 取消」。
  - 开关关闭：不提醒，直接跑。

#### 前端改造

- 评论分析上传成功后立刻进入进度页，先消费 `preprocess` SSE，再自动消费 `run` SSE；原文精选异步到达后局部刷新报告。
- 上传页改为左右布局（左上传区 + 右帖子标题/正文，正文必填）；结果页由「情感条 + 观点卡片」简化为**纯 Markdown 报告**。
- 历史记录改为**全局入口**（不再只在问卷模式显示）。
- 打开评论历史时回到评论分析结果页（Step 3），而不是问卷报告工作区。
- 设置抽屉新增管理员页签「平台设置」，提供「评论分析·重复文件提醒」开关。
- 静态资源版本：
  - `style.css?v=22`
  - `app.js?v=27`

#### 已修问题

- Excel 读取完成后上传卡住：根因是英文纯索要奖励复杂正则可能灾难性回溯，已改为线性 token 判断。
- 大文件上传前端静止：重活移到预处理 SSE，前端持续展示扫描/有效/抽样进度。
- 22 万 CSV 因混入非 UTF-8 脏字节失败：CSV 读取改为 `errors="replace"`。
- 表头包含 `comment_id,message` 时误选 `comment_id`：新增正文列优先级和 ID 列排除规则。
- Dify classify 返回条数少于输入：批次降到 40，并改为 idx 协议 + 缺失重试 + 自动拆小批。

### 5.20 新增「评论分析」独立权限（`server.py` / `static/app.js`，v3.2-dev）

- 权限项全集 `ALL_PERMS = ["survey", "annotate", "comment"]`，管理页/新增成员/勾选均加 comment 列。
- 白名单引入 schema 版本号 `perms_v`（`_PERMS_SCHEMA_VERSION = 2`）：`_migrate_whitelist_perms()` 一次性给已有访问权限的历史用户补 `comment`，迁移后管理员再取消也不会被重新加上。
- 前端 `applyPermGating()` 改为「切到第一个有权限的模式」，无 comment 权限则隐藏评论分析入口。

### 5.21 历史记录改版 + 数据标注结果落历史（`server.py` / `static/*`，v3.2-dev）

- 历史上限 `MAX_HISTORY` 由 **5 → 20**。
- 历史抽屉重做：双列卡片 + **报告类型筛选（问卷/评论/标注）+ 生成时间区间筛选**，卡片显示来源标签、追问状态、数量信息（有效样本/有效评论/标注行数）。
- `/api/history` 列表新增 `mode` / `row_count` / `comment_valid_count` 等字段；`_history_effective_row_count()` 兼容老报告从正文回扫样本量。
- **数据标注结果纳入历史**：AI 检测完成 / 确认 / 质量打标完成时，若结果完整即自动落盘 Excel 到 `data/annotate_results/<sid>.xlsx` 并写历史（`mode="annotate"`）；新增 `GET /api/annotate-history/{id}/download`（带路径越界校验）。标注下载逻辑统一抽到 `_build_annotate_excel_from_session()`，当前会话下载与历史下载共用。
- 标注 AI 检测确认表补回**开放题原文**（`originals`），修复原先「原文列实际显示译文」的占位问题。
- 审计日志表格改为定宽 + 点击行**展开详情卡片**。

### 5.22 免责声明按 mode 三态分流（`server.py`，v3.2-dev）

- `_inject_disclaimer` / `_prep_export_md` / `report_markdown_to_pdf` / `_export_to_feishu` 的 `skip_qual: bool` 参数统一重构为 `mode: str`。
- 按 mode 选第二条声明：`crosstab` 不插、`comment` 插评论分析专用声明（`COMMENT_DISCLAIMER`）、其余插定性声明；并自动清除不属于当前模式的历史残留声明（兼容历史报告 / 模式切换）。
- 所有导出端点（Word / PDF / Markdown / 飞书，当前 session + 历史）同步改为传 `mode`。

---

## 六、文件职责汇总

| 文件 | 说明 |
|------|------|
| `crosstab_parser.py` | **新增**。跑数表解析 + `render_to_markdown()` + `question_names()` |
| `dify.py` | 移植 `workflow_run()`；v3.1 新增 workflow 输出变量名兼容 `_pick_workflow_output()` |
| `server.py` | 聚类引擎；crosstab 上传/planner/stats/report 分支；标准模式分章多轮 Writer（v3.0）；评论分析流水线（相关性筛选+动态补样+分类+原文精选，v3.2）；文件 session；免责声明按 mode 分流；导出路径；comment 权限与白名单迁移；历史改版与标注结果落历史；开放题兜底/宽松解析；辅助函数 |
| `survey_stats.py` | 新增 `collect_open_text()`；`compute` 等原逻辑未改 |
| `survey_plan.py` | 新增 `parse_crosstab_plan()`（轻校验） |
| `comment_analysis.py` | **新增/扩展**。评论分析预处理：CSV/XLSX 评论列识别、流式清洗、分层 reservoir 抽样、长评论候选堆、本地统计聚合 |
| `static/app.js` | 选择分析入口；定量 4 步流程；评论分析上传/预处理/运行/原文精选异步刷新；comment 权限门控；历史改版（类型/日期筛选+标注下载）；设置页平台开关；审计日志展开；流式渲染；UX 修复 |
| `static/index.html` | 选择分析入口；三文件上传区；评论分析面板；设置页「平台设置」；步骤条；进度状态栏；品牌 logo |
| `static/style.css` | 三文件上传 UI；评论分析 UI；章节样式；进度状态栏样式；v3.1 工作台风格重构 |
| `static/login.html` | 登录页，v3.1 品牌化同步 |
| `static/web-icon.jpg` | **新增**。favicon / 顶部 logo |
| `docs/用研定量报告撰写规则与模板.md` | **新增**。定量报告写作规则与模板（核心判断优先、结论三要素、可回溯证据等），供 Writer prompt 与人工对齐参考 |
| `data/app_settings.json` | **运行时生成**。平台设置，目前包含评论分析重复文件提醒开关 |
| `data/annotate_results/` | **运行时生成**。数据标注完成后落盘的结果 Excel，供历史记录下载 |
| `skills/survey-analysis-workflow/SKILL.md` | **新增**。调研分析业务流程规范（业务怎么走：流程步骤、Python/LLM 分工、数据边界） |
| `skills/survey-web-frontend-development/SKILL.md` | **新增**。前端设计与交互规范（界面长什么样：工作台风格、组件、布局一致性） |
| `.env.example` | Dify key + `FEISHU_SCOPE=` 说明 |
| `.gitattributes` | **新增**。全仓库 LF 强制，防 CRLF 导致 Bash 报错 |

---

## 七、Git 提交记录

| Commit | 说明 |
|--------|------|
| `c64f229` | 基线（6/3 云上部署版） |
| `50de302` | Phase 1：跑数表解析器 + 聚类引擎移植 + crosstab 上传/报告接入 + 前端入口 |
| `86b8e4e` | fix：文件持久化 session，解决多 worker 跨进程 404 |
| `978d32d` | Phase 2：简化 4 步流程（crosstab planner + 自动建列 + 步骤条 + 方案卡片精简）+ CRLF fix + DEV_PROGRESS |
| `4c0a5be` | fix：6 条 UX 修复（progress 展示/方案卡恢复/catch 改进/重试状态/按钮/时间戳） |
| `e57953d` | feat：报告内容增强（核心结论/画像表格/原文翻译）+ 飞书文档导出 + 免责声明简化 |
| `e0f2b19` | fix：Codex review ①②③⑤⑥（历史飞书/按钮文案/quotes 补足/免责分流/feishu _prep_export_md） |
| `4cb563a` | fix：所有导出路径透传 skip_qual + 旧历史 mode fallback + prompt 措辞 |
| `4c0a5be` | fix：6 条 UX 修复 |
| `89a6a36` | fix：`renderMarkdown` 预处理 `**bold**`，修复 Unicode 序号旁加粗不渲染 |
| `5ea1086` | fix：流式报告改用 `marked.parse` 实时渲染 |
| `fb19f99` | feat：Writer 要求固定画像为 Part 1 + 满意度数据优先进核心结论 |
| `67e9bc2` | feat：Writer 结构修复 + 满意度注入 + 飞书文档导出修复 |
| `00889b3` | fix：数据标注漏返检测 + 重试 + 下载阻断 |
| `4c8f4fd` | fix：题型识别/alias管理/大样本阈值/数据持久化多项修复（**← 当前云上部署版本**） |
| `22fad39` | fix：deploy.sh 默认端口改为 18081，host 改为 0.0.0.0（未部署） |
| `de126e0` | **v3.0**：标准模式分章多轮生成报告（未部署） |
| `8063bda` | **v3.1**：前端改版 + workflow 输出兼容 + 开放题兜底（本地最新，未部署） |
| —— | 2026-06-22：`feat/crosstab-report` 合并为 `main`，旧 main 归档为 `legacy-main-v2.5` |
| —— | 2026-06-24：评论分析大文件链路初版（预处理/抽样/classify idx 协议/重复提醒，本地） |
| —— | 2026-06-25：**v3.2-dev 本次提交**：评论分析相关性筛选+动态补样、玩家评论原文精选（后台并行）、comment 独立权限+白名单迁移、历史记录改版、数据标注结果落历史可下载、免责声明按 mode 分流、清理评论流水线遗留死代码 |

---

## 八、运行方式

**本地开发：**
```
cd C:\Users\admin\Desktop\survey-web
C:\Users\admin\Desktop\survey-web\.venv\Scripts\python.exe server.py
# 或指定端口：python -m uvicorn server:app --host 127.0.0.1 --port 8020
```

**ngrok（固定域名）：**
```
C:\Users\admin\.antigravity\ngrok.exe http --url=passing-jersey-reggae.ngrok-free.dev <端口>
```

**云上部署（Ubuntu + systemd，监听 0.0.0.0:18081）：**
```
# 服务器上拉取 main 后，以 root 运行：
sudo ./deploy.sh
```
> 数据目录 `/opt/survey-web-data`，部署目录 `/opt/survey-web`，服务名 `survey-web`。
> 云端当前为 `4c8f4fd`；部署 v3.0/v3.1 时记得一并带上 `22fad39` 的端口改动。

**所需 `.env` 配置：**

| 变量 | 用途 |
|------|------|
| `DIFY_PLANNER_KEY` | 定性分析方案（原有） |
| `DIFY_ANALYST_KEY` | 定性分析报告写手（原有） |
| `DIFY_COLUMN_KEY` | 题型识别（原有） |
| `DIFY_CROSSTAB_PLANNER_KEY` | **跑数表模式**章节大纲策划（新建 Dify Chat 应用，system prompt 见下） |
| `DIFY_THEME_EXTRACT_KEY` | 主观题聚类 - 主题提取（Workflow） |
| `DIFY_THEME_MERGE_KEY` | 主观题聚类 - 主题合并（Workflow） |
| `DIFY_CLASSIFY_KEY` | 主观题聚类 - 回复分类（Workflow） |
| `DIFY_LARGE_ANALYST_KEY` | 报告写手（大样本/跑数表版） |
| `DIFY_COMMENT_ANALYSIS_KEY` | 评论舆情分析 Workflow（mode 路由：filter / extract / merge / classify / report） |
| `FEISHU_SCOPE` | 置空即可（`FEISHU_SCOPE=`），代码已硬编码为空，不限制 OAuth 授权范围 |
| 飞书其他配置 | OAuth 登录、白名单 |

> **飞书导出说明**：导出使用机器人身份（`app_access_token`），需在飞书开放平台为应用开通「应用身份」版 `drive:drive` 和 `docs:document:import` 权限。

---

## 九、crosstab planner 的 Dify system prompt

新建一个 Dify Chat 应用，把以下内容贴为 System Prompt，key 填入 `DIFY_CROSSTAB_PLANNER_KEY`：

```
你是资深用户研究报告策划。任务：读懂一份调研问卷的逻辑与意图，为后续报告规划清晰的章节大纲，并在必要时就报告结构向用户提澄清问题。

【你会在用户消息里收到】
- <questionnaire>：问卷原文（题目/选项/说明），这是你理解调研逻辑的主要依据。
- <available_questions>：本次实际有数据的题目清单（中文题名），章节只应覆盖这些题目。
- <open_questions_list>：开放题（主观题）清单。
- 若为修订：还会有 <current_outline>（当前章节大纲）和 <user_request>（用户修改意见），请在其基础上调整。

【你要做】
1. 按问卷逻辑把题目组织成 3-6 个主题化章节，每章给出：name（简洁中文章节名）、scope（一句话说明本章覆盖哪些题目/主题）。
2. 开放题（主观反馈）安排到合适章节或单独成章。
3. 提 0-3 条待确认问题（open_questions），仅限报告结构层面：章节侧重、详略、报告语言、是否要执行摘要等。

【硬性约束】
- 绝对不要提任何与数据本身相关的问题（题型/口径/样本/统计方法都已人工处理完，不在你职责内）。
- 章节只覆盖 <available_questions> 里真实存在的题目，不要虚构内容。
- 只输出一个 JSON 对象，用 ```json``` 围栏包裹，无任何解释文字。

【输出格式】
{
  "parts": [{"name": "章节名", "scope": "本章覆盖的题目/主题"}],
  "open_questions": ["我计划……，请确认是否这样组织？"]
}
```

---

## 十、验证状态

**已验证（含真实 Dify 联调）：**
- 跑数表解析：手感（8段）+ 卡蒂塔（6段）两版，分段/基数/占比/均分/矩阵均正确
- `collect_open_text`：手感清数 5 个开放题列正确收集（Q8=187、Q10=431）
- 文件 session 读写、多 worker 路由正常（不再随机 404）
- `POST /api/upload/crosstab` 真实 HTTP 联调通过
- 全链路（上传 → 方案确认 → 生成报告）已真实跑通，报告质量基本符合预期
- 报告展示：加粗正常渲染（含 Unicode 序号场景）

**已验证（2026-06-12 新增）：**
- 飞书文档导出：改用 `create_doc_via_bot` 后导出成功，文档归登录用户所有

**已验证（2026-06-15，对应云上部署版 `4c8f4fd`）：**
- 题型识别 / alias 管理 / 大样本阈值 / 数据持久化多项修复
- 数据标注漏返检测 + 重试 + 下载阻断

**待验证（v3.0 / v3.1，本地已实现、尚未部署到云端）：**
- v3.0 标准模式分章多轮生成：本地逻辑完成，需用真实多章节方案跑通验证轮次拼接与不超时
- v3.1 前端品牌化改版、Dify workflow 输出兼容、开放题兜底：需在云端环境回归

**已验证（2026-06-24，评论分析 v3.2-dev 本地）：**
- 1.5MB xlsx 评论文件：预处理 `scan=19853 / valid=16719 / sample=5000`，耗时约 `0.45s`。
- 12 万条模拟 CSV：预处理 `scan=120000 / valid=113684 / sample=5000`，耗时约 `1.11s`。
- 真实评论分析：classify 批次 25 + idx 协议 + Dify 提示词调整后曾完整跑出结果；后续为提速把批次调到 40，需继续观察稳定性。
- CSV 脏编码：遇到非 UTF-8 字节后改为 `errors="replace"`，不再因单个脏字节中断。
- 表头 `comment_id,message`：已修复为识别 `message`，不再误选 `comment_id`。

**待验证（评论分析 v3.2-dev）：**
- classify 批次 `40` 在真实 5000 抽样下的稳定性与耗时。
- **相关性筛选 + 动态补样**：相关数不足 1000 时连续补抽多轮的进度展示、补样上限（15000）触顶行为、relevance 节点漏返按无关处理是否合理。
- **玩家评论原文精选**：`quote_select_batch` / `quote_select_final` 真实产出质量、翻译字段命中、后台并行任务在主报告完成后追加是否稳定，失败隔离（`comment_quotes_error`）是否不影响主报告。
- 评论历史报告：生成后是否正确进入历史抽屉、回看 Step 3、导出 PDF/飞书是否正常。
- 重复文件提醒开关：开启/关闭两种状态下上传同文件的前端弹窗与后端返回。
- **comment 权限迁移**：老白名单用户加载后是否自动补 `comment`、管理员取消后是否不再回填。
- **历史记录改版**：类型/日期筛选、数据标注结果落盘下载（含路径越界校验）、审计日志展开。

**已知遗留问题（待后续跟进）：**
- 追问 Dify 404：可能发生于服务重启前的旧 session（新生成的报告应已修复，需持续观察）
- 数据表格展示：仅 markdown 表格，暂无数据条/颜色分组（评估后决定是否做）

---

## 十一、已知限制 / 技术债

| # | 说明 |
|---|------|
| 1 | 卡蒂塔旧模板（含绝对值）解析时总计基数显示为 1。非当前生产模板，仅回归用，可接受。 |
| 2 | 主观题 profile 取的是清数里的码值（如「2」），非可读标签。后续可加 value_aliases 映射。 |
| 3 | annotate_sessions 仍为内存（标注会话生命周期短，不跨 worker 保活，暂不影响使用）。 |
| 4 | 报告生成调用 4 个 Dify workflow，主力主观题分批，耗时数分钟，属正常。 |
| 5 | 数据表格为纯 markdown，无数据条/颜色分组。实现方案已评估（中等难度），待需求确认后实施。 |
| 6 | 评论分析 classify 仍依赖 Dify 严格返回完整 idx；本地已有重试/拆批兜底，但大文件耗时与 Dify 输出稳定性仍需持续观察。 |
| 7 | 评论分析百分比当前是「抽样评论占比」，不是严格全量总体占比；报告中已插入样本口径说明。 |
| 8 | 评论历史报告目前复用通用历史列表；评论模式下默认过滤 `mode=comment`，如需跨模式统一筛选可后续补 UI。 |

---

## 十二、与旧方案的隔离

- 当前主干 `main`（原 crosstab 分支）基于 c64f229，**不含**任何倍市得旧代码（beisde_parser / stats_advanced）。
- 旧主干方案已归档为远端分支 `origin/legacy-main-v2.5`，需要时可单独检出。
- 主干已推送至 `origin/main`，云端按 `main` 部署（当前云上为 `4c8f4fd`，落后本地 3 个提交）。

---

*本文档随主干开发更新。*
