# 「帖子评论舆情分析」Dify 工作流配置

后端 `comment_analysis.py`（预处理 + 统计）与 `server.py`（并发编排 + 两个 API）调用**一个 Dify Workflow 应用**完成全部 LLM 步骤，靠 `mode` 路由到不同分支。

- 应用类型：**Workflow（工作流）**，走 `/workflows/run`（blocking）接口
- 一个 Key：`.env` 的 `DIFY_COMMENT_ANALYSIS_KEY=`
- 改 `.env` 后需**重启服务**（`--reload` 不重载环境变量）

> ⚠️ **最容易踩的坑：改了草稿一定要点「发布」。** Dify 的 API 永远跑**最后一次发布**的版本，不是你画布里正在编辑的草稿。节点变量绑定、prompt 改动，发布后才对 API 生效。

---

## 一、整体架构

```
用户上传评论文件 + 帖子标题(+原文)
        │
   Python 预处理（comment_analysis.py）
   解析 CSV/XLSX → 规则清洗 → 分层抽样(≤5000) → 切批(250/批)
        │
   Python 并发编排（server.py，Semaphore 限流 6 路）
   ├─ filter   (仅当有帖子原文) ── 调 Dify mode=filter   → 关键词 → 本地二次过滤
   ├─ extract  (每批并发)        ── 调 Dify mode=extract  → 各批候选主题
   ├─ merge    (单次)            ── 调 Dify mode=merge    → 5~10 个最终主题(带 theme_id)
   ├─ classify (每批并发)        ── 调 Dify mode=classify → 每条评论的多标签归类
   ├─ 本地统计                    ── 占比 / 情感分布 / 代表引用 / 低频归「其他」
   └─ report   (单次)            ── 调 Dify mode=report   → 中文舆情简报(Markdown)
```

---

## 二、START 节点输入变量（5 个，全部 String）

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `mode` | ✅ | 路由开关：`filter` / `extract` / `merge` / `classify` / `report` |
| `post_title` | | 帖子标题 |
| `post_content` | | 帖子原文（建议用「段落」类型，避免过长截断） |
| `comments_json` | | 当前批次评论，JSON 数组字符串 `["评论1","评论2",...]` |
| `themes_json` | | 主题数据，含义随 mode 变化（候选 / 最终主题 / 统计汇总） |

**条件分支**：5 个 CASE 判断 `mode` 等于 filter/extract/merge/classify/report，分别路由到 LLM1~5；每个 LLM 接一个「结束」节点，**输出变量统一为 `text`（String）**。

> 各 LLM 节点 prompt 里引用 `comments_json` / `themes_json` 等变量时，**必须用变量选择器（`{x}` 按钮或输入 `/`）从「开始」节点选中**，生成蓝色变量引用块。**手打的 `{{var}}` 文本可能不会真正绑定**，导致节点收到空输入（症状：merge 凭空编通用主题、classify 对任何输入都只回一条 `other`）。

---

## 三、各 mode 节点的输入 / 输出契约

所有结构化节点（extract/merge/classify/filter）**只输出 JSON，不要包 ```` ```json ```` 代码块、不要解释**。后端用容错解析 `comment_analysis.loads_loose`（兼容代码块、首尾括号），但源头干净最稳。

### filter（LLM1，路径 B 专用）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`
- 输出：
  ```json
  {"topic_keywords": ["matchmaking", "survival"], "exclude_keywords": ["diamond", "skin", "please"]}
  ```
- Python 用法（保守过滤，少误杀）：一条评论**同时**「命中某个 `exclude_keyword`」**且**「不含任何 `topic_keyword`」才丢弃；沾了相关词就保留。仅当上传了 `post_content` 时才走这步。

### extract（LLM2，单批主题提取，每批并发）
- 模型：GPT-4o-mini
- 输入：`post_title`、`comments_json`
- 输出（JSON 数组）：
  ```json
  [{"theme_name": "匹配机制不合理", "sentiment": "positive/negative/neutral"}]
  ```

### merge（LLM3，主题合并去重，单次）
- 模型：GPT-4o-mini
- 输入：`themes_json`（= 所有批次 extract 候选汇总）
- 输出（JSON 数组，归并为 5~10 个）：
  ```json
  [{"theme_id": "theme_matchmaking", "theme_name": "匹配机制", "description": "...", "sentiment": "negative"}]
  ```
- **`theme_id` 必须有且稳定**：它会作为 classify 的输入主题清单，classify 回的 `theme_ids` 要能对得上，否则统计时全归 other。

### classify（LLM4，单批多标签分类，每批并发）
- 模型：GPT-4o-mini
- 输入：`comments_json`（当前批评论）、`themes_json`（= merge 输出的 `theme_id`+`theme_name`+`description`）
- 输出（JSON 数组，**顺序与输入评论完全一致，每条评论一个对象**）：
  ```json
  [
    {
      "theme_ids": ["theme_matchmaking", "theme_abuse"],
      "sentiment": "positive/negative/neutral",
      "is_quote_candidate": true,
      "translation": "当 is_quote_candidate 为 true 时填该评论的简体中文翻译，否则留空"
    }
  ]
  ```
- **多标签**：一条评论涉及几个主题就把几个 `theme_id` 放进 `theme_ids` 数组；不属于任何主题填 `["other"]`。后端按位置把原文对回，靠 `theme_ids` 分别计入各主题。
- 后端兼容旧单标签字段 `theme_id`（无 `theme_ids` 时按单元素处理）。

### report（LLM5，生成舆情简报，单次）
- 模型：GPT-5.2
- 输入：`post_title`、`themes_json`（= 后端统计汇总 JSON，见下）
- 输出：**Markdown 纯文本**（首尾不要包代码块）
- 后端塞给 `themes_json` 的统计汇总结构：
  ```json
  {
    "post_title": "...",
    "total_comments": 5000,
    "sentiment_overall": {"positive_pct": 33.3, "negative_pct": 50.0, "neutral_pct": 16.7},
    "themes": [
      {"name": "...", "description": "...", "percentage": 42.1, "count": 2105,
       "positive_pct": 10.0, "negative_pct": 80.0, "neutral_pct": 10.0,
       "quotes": ["「中文翻译」（原文: ...）"]}
    ],
    "other_themes": [{"name": "...", "count": 30, "percentage": 1.2}]
  }
  ```
- 代表性评论格式约定为 `「中文翻译」（原文: original text）`（后端已拼好）。
- **多标签注意**：`themes` 各主题 `percentage` 是"提及该主题的评论数 / 总评论数"，**之和可能 >100%**。如需更准确，可在 report prompt 里把措辞写成"提及占比"。

---

## 四、Python 侧关键逻辑（comment_analysis.py）

可调参数集中在文件顶部：

- **清洗规则**（任一命中即丢）：
  - 长度 < `MIN_LEN`(15)
  - 键盘乱打（单字符占比 > 60% 且长度 > 20）
  - 不含任意语言字母（用 Unicode category `L*` 判断，**泰语/阿拉伯语/越南语/西里尔语等多语种评论都会保留**，只过滤纯数字/符号）
  - 完全去重（大小写不敏感）
  - 纯索要奖励（整条只索要钻石/皮肤等，正则整条锚定匹配；夹带其他意见的保留）
- **分层抽样** `SAMPLE_MAX_N`(5000)：短(15-30)≈50% / 中(31-80)≈35% / 长(>80)≈15%，桶不足时名额重分配
- **批次** `BATCH_SIZE`(250)：5000 条 → 20 批
- **低频合并** `OTHER_THEME_PCT`(3.0)：占比 < 3% 的主题归入「其他」
- **并发** `COMMENT_ANALYSIS_CONCURRENCY`(6，在 server.py)：20 批不全开，用 `asyncio.Semaphore` 限流，配合 `workflow_run` 的指数退避重试，避免上游 429

---

## 五、后端 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/comment-analysis/upload` | multipart：`file` + `post_title` + `post_content`。解析→清洗→抽样→存 session。返回 `valid_count` / `sample_count` / `filter_stats` / `preview_comments` |
| GET | `/api/comment-analysis/run/{session_id}` | SSE。逐步推 `{"type":"progress","message":...}`，结束推 `{"type":"comment_done", themes, other_themes, sentiment_overall, report_md, ...}` |

结果同时写入 session 的 `report_md`，复用现有 `/api/export/pdf/{sid}` 与 `/api/export/feishu/{sid}` 导出。

---

## 六、前端

- 侧边栏新增「评论分析」一级导航（`nav-comment`），`switchMode('comment')` 切换
- 三步面板：`cm-panel-1`（上传+标题+原文）/ `cm-panel-2`（流式进度）/ `cm-panel-3`（结果）
- 结果区：整体情感分布条 + 可展开的观点卡片（点开看 `「翻译」（原文）` 代表引用）+ 「其他声音」标签 + AI 简报 Markdown + PDF/飞书导出
- 代码位置：`static/index.html`、`static/app.js`（`cmState` / `cmGoStep` / `cmStart` / `cmRun` / `cmRenderResult`）、`static/style.css`（`.cm-*`）

---

## 七、自测命令

不依赖前端、直接连真机 Dify 跑完整链路（小样本）：

```bash
python3 - << 'PY'
import asyncio, server
sess = {'kind':'comment','comment_post_title':'测试帖','comment_post_content':'',
        'comment_sample':['comment one ...','comment two ...']}
async def run():
    async for k,p in server._comment_analysis_pipeline(sess):
        print(k, p if k!='result' else {kk:p[kk] for kk in ('sentiment_overall','total_classified')})
asyncio.run(run())
PY
```

排错口诀：**merge 凭空编主题 / classify 只回一条 other → 八成是变量没绑定或改了草稿没发布。** 单独 Test Run 那个节点时记得把 `mode` 填对（如 `merge`），否则会走 ELSE 分支、0 token、输出空 `{}`。
