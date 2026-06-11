# Survey-Web 跑数表分支 开发进度文档

> 分支：`feat/crosstab-report`
> 基线：`c64f229`（2026-06-03 云上部署版）
> 最后更新：2026-06-11
> 代码路径：`c:\Users\admin\Desktop\survey-web-crosstab`

---

## 一、这个分支是什么

一个**精简、独立、可快速投产**的分支，专做倍市得「跑数表模式」的报告生成。与主仓库其他在研方案（本地未提交的旧倍市得 stats 改造等）**物理隔离**——本分支从 6/3 干净基线起步，不含 `beisde_parser.py` / `stats_advanced.py` 等旧代码。

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

### 5.12 部署修复（`deploy.sh` / `.gitattributes`）

- 新增 `.gitattributes`，全仓库文本文件统一 LF，`*.sh eol=lf` 防止 Windows 把 `\r` 写入脚本导致 Bash 解析失败。
- `deploy.sh` 转 LF，`bash -n` 验证通过。

---

## 六、文件职责汇总

| 文件 | 说明 |
|------|------|
| `crosstab_parser.py` | **新增**。跑数表解析 + `render_to_markdown()` + `question_names()` |
| `dify.py` | 移植 `workflow_run()` |
| `server.py` | 聚类引擎；crosstab 上传/planner/stats/report 分支；文件 session；免责声明分流；导出路径；辅助函数 |
| `survey_stats.py` | 新增 `collect_open_text()`；`compute` 等原逻辑未改 |
| `survey_plan.py` | 新增 `parse_crosstab_plan()`（轻校验） |
| `static/app.js` | 定量分析入口；4 步流程；方案卡片；流式渲染；markdown bold 预处理；UX 修复 |
| `static/index.html` | 定量分析卡片；三文件上传区；步骤条；进度状态栏 |
| `static/style.css` | 三文件上传 UI；章节样式；进度状态栏样式 |
| `.env.example` | 5 个 Dify key + `FEISHU_SCOPE=drive:file:upload` 说明 |
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

---

## 八、运行方式

```
cd C:\Users\admin\Desktop\survey-web-crosstab
C:\Users\admin\Desktop\survey-web\.venv\Scripts\python.exe server.py   # 端口 8000
# 或指定端口：python -m uvicorn server:app --host 127.0.0.1 --port 8020
```

**ngrok（固定域名）：**
```
C:\Users\admin\.antigravity\ngrok.exe http --url=passing-jersey-reggae.ngrok-free.dev <端口>
```

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
| `FEISHU_SCOPE` | 必须包含 `drive:file:upload`（飞书文档导出权限） |
| 飞书其他配置 | OAuth 登录、白名单 |

> **注意**：`.env` 修改 `FEISHU_SCOPE` 后，已登录用户需退出飞书重新登录，才能获得包含 drive 权限的新 token。

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

**已知遗留问题（待后续跟进）：**
- 追问 Dify 404：可能发生于服务重启前的旧 session（新生成的报告应已修复，需持续观察）
- 飞书文档导出需先退出重新登录一次以获得 drive scope
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

---

## 十二、与旧方案的隔离

- 本分支基于 c64f229，**不含**任何倍市得旧代码（beisde_parser / stats_advanced）。
- 旧方案仍在 `c:\Users\admin\Desktop\survey-web`（未提交改动原封不动），后续可单独提分支。
- 本分支**尚未推送 GitHub**，仅本地提交。

---

*本文档随分支开发更新。*
