# 「帖子评论舆情分析」Dify 工作流配置

后端 `comment_analysis.py`（预处理 + 统计）与 `server.py`（并发编排 + 两个 API）调用**一个 Dify Workflow 应用**完成全部 LLM 步骤，靠 `mode` 路由到不同分支。

- 应用类型：**Workflow（工作流）**，走 `/workflows/run`（blocking）接口
- 一个 Key：`.env` 的 `DIFY_COMMENT_ANALYSIS_KEY=`
- 改 `.env` 后需**重启服务**（`--reload` 不重载环境变量）

> ⚠️ **最容易踩的坑：改了草稿一定要点「发布」。** Dify 的 API 永远跑**最后一次发布**的版本，不是你画布里正在编辑的草稿。节点变量绑定、prompt 改动，发布后才对 API 生效。

---

## 一、整体架构

```
用户上传评论文件 + 帖子标题 + 帖子原文
        │
   Python 预处理（comment_analysis.py）
   解析 CSV/XLSX → 规则清洗 → 分层抽样(≤5000) + 最长评论候选(≤1500)
        │
   Python 并发编排（server.py，Semaphore 限流 6 路）
   ├─ relevance (每批并发)       ── 调 Dify mode=relevance → 逐条判断是否与帖子主题/正文相关
   ├─ extract   (每批并发)       ── 仅对相关评论调 Dify mode=extract  → 各批候选主题
   ├─ merge     (单次)           ── 调 Dify mode=merge    → 5~10 个最终主题(带 theme_id)
   ├─ classify  (每批并发)       ── 调 Dify mode=classify → 每条相关评论的多标签归类
   ├─ 本地统计                    ── 占比 / 情感分布 / 代表引用 / 低频归「其他」
   ├─ report   (单次)            ── 调 Dify mode=report   → 中文舆情简报(Markdown)，先返回主报告
   └─ quote_select_batch/final   ── 与主线并行，从最长评论候选中精选最多 50 条玩家原文，完成后追加进报告
```

---

## 二、START 节点输入变量（5 个，全部 String）

| 变量名 | 必填 | 说明 |
|--------|------|------|
| `mode` | ✅ | 路由开关：`relevance` / `extract` / `merge` / `classify` / `report` / `quote_select_batch` / `quote_select_final` |
| `post_title` | ✅ | 帖子标题 |
| `post_content` | ✅ | 帖子原文（建议用「段落」类型，避免过长截断） |
| `comments_json` | | 当前批次评论，relevance/classify 为 `[{idx,text}]`，extract 为 `["评论1","评论2",...]` |
| `themes_json` | | 主题数据，含义随 mode 变化（候选 / 最终主题 / 统计汇总） |

**条件分支**：7 个 CASE 判断 `mode` 等于 relevance/extract/merge/classify/report/quote_select_batch/quote_select_final，分别路由到对应 LLM；每个 LLM 接一个「结束」节点，**输出变量统一为 `text`（String）**。

> 各 LLM 节点 prompt 里引用 `comments_json` / `themes_json` 等变量时，**必须用变量选择器（`{x}` 按钮或输入 `/`）从「开始」节点选中**，生成蓝色变量引用块。**手打的 `{{var}}` 文本可能不会真正绑定**，导致节点收到空输入（症状：merge 凭空编通用主题、classify 对任何输入都只回一条 `other`）。

---

## 三、各 mode 节点的输入 / 输出契约

所有结构化节点（relevance/extract/merge/classify）**只输出 JSON，不要包 ```` ```json ```` 代码块、不要解释**。后端用容错解析 `comment_analysis.loads_loose`（兼容代码块、首尾括号），但源头干净最稳。

### relevance（LLM1，主题相关性筛选，单批并发）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`、`comments_json`
- 输出：JSON 数组，**只返回需要保留的评论**，每个对象必须带回 `idx`。未返回的 `idx` 后端默认剔除。

完整 prompt：

```text
你是评论分析的前置筛选器。请先阅读帖子标题和帖子正文，理解这篇帖子真正讨论的对象、活动、玩法、皮肤、英雄、规则、奖励、时间、价格、视觉内容和核心诉求。

输入：
- post_title：帖子标题
- post_content：帖子正文
- comments_json：评论数组，格式为 [{"idx": 0, "text": "..."}]

任务：
从 comments_json 中筛选出“与帖子主题/正文相关，且有业务分析价值”的评论。
只输出需要保留的评论；无关评论、低价值评论、纯索要评论不要输出。

保留标准：
direct：评论明确评价或询问帖子中的核心对象或内容，例如活动、皮肤、英雄、玩法、奖励规则、上线时间、价格、外观、配色、特效、获取方式、兑换机制等。
implicit：评论没有重复帖子关键词，但明显是在评价帖子对象，例如“太贵了”“好看”“不值得”“什么时候上线”“这个颜色一般”“获取太难”。

剔除标准：
off_topic：讨论游戏其他系统、匹配、排位、队友、网络、外挂、客服，且无法连接到帖子主题。
low_value_reward_request：只是在索要皮肤、钻石、奖励、礼包、金币、点券、免费资源，没有对帖子内容、获取机制、价格、规则或体验提出任何具体观点。
noise：无上下文抱怨、灌水、玩笑、纯表情、重复喊话、无法分析的短句。

特别规则：
- 不要因为评论是真实玩家反馈就判为相关；必须与帖子主题或正文有清楚联系。
- 如果帖子是皮肤内容，匹配机制、排位队友、网络延迟默认 off_topic，除非评论明确把它们和帖子内容联系起来。
- “give me skin”“free skin pls”“pahingi skin”“minta skin”“求皮肤”“送我皮肤”“请给我免费皮肤”“plz give me xxx skin”这类纯索要内容必须剔除，即使帖子本身是皮肤帖。
- 如果评论讨论免费获取机制、兑换规则、活动难度、价格合理性，可以保留。例如“Can this skin be obtained for free through the event?”、“The free acquisition path is too hard”。
- 多语言评论按语义判断，不要只看关键词。
- 如果无法判断是否有业务分析价值，剔除。

输出要求：
- 只输出 JSON 数组。
- 只返回需要保留的评论。
- 每个返回对象必须带回原始 idx。
- 不要输出 Markdown。
- 不要输出解释。
- 不要包代码块。

输出格式：
[
  {
    "idx": 0,
    "is_related": true,
    "relation": "direct",
    "reason": "评论明确评价帖子中皮肤的价格"
  },
  {
    "idx": 3,
    "is_related": true,
    "relation": "implicit",
    "reason": "评论未提皮肤名，但在评价帖子对象的外观"
  }
]
```

### extract（LLM2，单批主题提取，每批并发）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`、`comments_json`
- 注意：输入评论已经过 relevance 筛选。extract 只能提炼与帖子主题/正文相关的主题；如果仍发现无关评论，不要把无关内容提炼成正式主题。

完整 prompt：

```text
你是评论主题提取器。请先阅读帖子标题和帖子正文，明确帖子讨论的真实对象。然后从 comments_json 中提取玩家观点主题。

输入：
- post_title：帖子标题
- post_content：帖子正文
- comments_json：已经通过前置筛选的评论数组，格式为 ["评论1", "评论2", ...]

任务：
从这些评论中提取候选主题。主题必须来自评论原文，并且必须与帖子标题或正文中的对象相关。

严格规则：
- 不要引入帖子正文不存在的对象。
- 如果帖子讨论的是新皮肤，不要生成“新英雄”“英雄期待”“英雄强度”等主题，除非正文明确说的是新英雄。
- 不要把纯索要皮肤、钻石、奖励的评论提炼成正式主题。
- 如果仍看到低价值索要评论，忽略它，不要生成“请求免费皮肤”这类主题。
- 主题名称要具体、业务可读，例如“皮肤价格偏高”“外观设计认可”“获取方式不清晰”“上线时间询问”。
- 不要生成泛化主题，例如“玩家反馈”“积极期待”“其他建议”。

输出要求：
- 只输出 JSON 数组。
- 每个对象包含 theme_name、description、sentiment。
- sentiment 只能是 positive / negative / neutral。
- 不要输出 Markdown。
- 不要输出解释。
- 不要包代码块。

输出格式：
[
  {
    "theme_name": "皮肤价格偏高",
    "description": "玩家认为该皮肤价格或获取成本偏高",
    "sentiment": "negative"
  },
  {
    "theme_name": "外观设计认可",
    "description": "玩家认可皮肤外观、配色、特效或整体视觉表现",
    "sentiment": "positive"
  }
]
```

### merge（LLM3，主题合并去重，单次）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`、`themes_json`（= 所有批次 extract 候选汇总）
- **`theme_id` 必须有且稳定**：它会作为 classify 的输入主题清单，classify 回的 `theme_ids` 要能对得上，否则统计时全归 other。

完整 prompt：

```text
你是评论主题合并器。请先阅读帖子标题和帖子正文，确认帖子真实讨论对象。然后将 themes_json 中的候选主题合并为最终主题。

输入：
- post_title：帖子标题
- post_content：帖子正文
- themes_json：候选主题数组，来自多个批次的 extract 输出

任务：
将重复、相近、上下位关系的候选主题合并为 5 到 10 个最终主题。最终主题必须严格服务于帖子标题和正文，不得引入帖子中不存在的对象。

严格规则：
- 如果帖子讨论的是皮肤，不要生成“新英雄期待”“英雄强度”“英雄机制”等主题，除非帖子正文明确讨论英雄本体。
- 如果候选主题中出现与帖子正文不一致的对象，直接丢弃或合并到更准确的皮肤/活动主题。
- 不要保留“请求免费皮肤”“求钻石”“求奖励”这类低价值主题。
- 不要为了凑数量生成主题；少于 5 个高质量主题也可以。
- theme_id 必须稳定、英文小写、下划线命名。
- theme_name 必须是中文、具体、业务可读。
- description 要明确该主题覆盖什么评论，不要泛泛而谈。

输出要求：
- 只输出 JSON 数组。
- 每个对象必须包含 theme_id、theme_name、description、sentiment。
- sentiment 只能是 positive / negative / neutral。
- 不要输出 Markdown。
- 不要输出解释。
- 不要包代码块。

输出格式：
[
  {
    "theme_id": "theme_skin_price",
    "theme_name": "皮肤价格与获取成本",
    "description": "玩家讨论皮肤价格、获取门槛、是否值得购买或兑换",
    "sentiment": "negative"
  },
  {
    "theme_id": "theme_visual_design",
    "theme_name": "外观与特效表现",
    "description": "玩家评价皮肤外观、配色、模型、特效和整体视觉吸引力",
    "sentiment": "positive"
  }
]
```

### classify（LLM4，单批多标签分类，每批并发）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`、`comments_json`（当前批评论，格式为 `[{idx,text}]`）、`themes_json`（= merge 输出的 `theme_id`+`theme_name`+`description`）
- **多标签**：一条评论涉及几个主题就把几个 `theme_id` 放进 `theme_ids` 数组；不属于任何主题填 `["other"]`。后端按 `idx` 把原文对回，靠 `theme_ids` 分别计入各主题。
- 后端兼容旧单标签字段 `theme_id`（无 `theme_ids` 时按单元素处理）。

完整 prompt：

```text
你是评论分类器。请先阅读帖子标题和帖子正文，再阅读 themes_json 中的最终主题列表。然后将 comments_json 中每条评论归类到最匹配的主题。

输入：
- post_title：帖子标题
- post_content：帖子正文
- comments_json：评论数组，格式为 [{"idx": 0, "text": "..."}]
- themes_json：最终主题数组，包含 theme_id、theme_name、description

任务：
为每条评论返回分类结果。每条输入评论都必须返回一个对象，并带回原始 idx。

严格规则：
- 只能使用 themes_json 中已有的 theme_id，不能发明新 theme_id。
- 如果评论不适合任何主题，theme_ids 填 ["other"]。
- 如果评论只是索要免费皮肤、钻石、奖励、礼包，没有观点或原因，必须归为 ["other"]，不要强行归到“期待”“价格”“获取方式”等主题。
- 如果帖子是皮肤内容，不要把评论归到“新英雄期待”或类似英雄主题；除非 themes_json 和帖子正文都明确存在英雄主题。
- 多标签只在评论确实同时表达多个具体观点时使用。
- is_quote_candidate 只给有代表性、有信息量的评论；纯索要、玩笑、灌水、短句不要作为代表引用。
- translation 只在 is_quote_candidate=true 时填写简体中文翻译，否则留空。

输出要求：
- 只输出 JSON 数组。
- 输出数量必须与输入评论数量一致。
- 每个对象必须带回 idx。
- 不要输出 Markdown。
- 不要输出解释。
- 不要包代码块。

输出格式：
[
  {
    "idx": 0,
    "theme_ids": ["theme_skin_price"],
    "sentiment": "negative",
    "is_quote_candidate": true,
    "translation": "这款皮肤太贵了，不值得购买"
  },
  {
    "idx": 1,
    "theme_ids": ["other"],
    "sentiment": "neutral",
    "is_quote_candidate": false,
    "translation": ""
  }
]
```

### report（LLM5，生成舆情简报，单次）
- 模型：GPT-5.2
- 输入：`post_title`、`post_content`、`themes_json`（= 后端统计汇总 JSON，见下）
- 输出：**Markdown 纯文本**（首尾不要包代码块）
- 后端塞给 `themes_json` 的统计汇总结构：
  ```json
  {
    "post_title": "...",
    "total_comments": 3200,
    "source_sample_count": 5000,
    "off_topic_count": 1800,
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
- `total_comments` 是通过 relevance 筛选后的主题相关评论数；报告不要把被剔除的 `off_topic_count` 当作业务观点分析。
- **多标签注意**：`themes` 各主题 `percentage` 是"提及该主题的评论数 / 总评论数"，**之和可能 >100%**。如需更准确，可在 report prompt 里把措辞写成"提及占比"。

完整 prompt：

```text
你是中文评论舆情简报写手。请基于 themes_json 中的统计结果生成报告。你必须以数据和代表性评论为依据，不要引入 themes_json 中没有的信息。

输入：
- post_title：帖子标题
- post_content：帖子正文
- themes_json：后端统计汇总 JSON，包含 total_comments、source_sample_count、off_topic_count、sentiment_overall、themes、other_themes

写作目标：
为业务方总结与帖子主题/正文相关、且有业务分析价值的玩家反馈。

严格规则：
- total_comments 是通过前置筛选后的主题相关有效评论数。
- off_topic_count 是已剔除的无关或低价值评论数，不要把它当作业务观点展开。
- 不要写“很多玩家想要免费皮肤/钻石/奖励”这类纯索要内容，除非 themes_json 中有明确的、经过统计的获取机制或价格主题。
- 不要把皮肤帖写成新英雄帖；不要出现帖子正文不存在的对象。
- 不要编造百分比、人数、主题、代表评论。
- 不要把 other_themes 当作主要结论，只能作为低频补充。
- 代表性评论只使用 themes_json 中 quotes 字段提供的内容。
- 不要输出“玩家核心观点”章节。

报告结构：
## 舆情简报
开头用一句话概括正面、中性、负面占比和整体情绪倾向，必须引用 themes_json 中的 sentiment_overall。
随后用 3-5 条 bullet 总结最重要发现，每条必须对应 themes_json 中的主题或情感统计。可以引用少量代表性评论，但不要逐主题铺开成长篇观点列表。

写作风格：
- 中文。
- 简洁、业务化、面向运营/产品团队。
- 不要写技术过程。
- 不要输出 Markdown 代码块。
```

### quote_select_batch（LLM6，长评论候选初筛，每批并发）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`、`comments_json`（清洗后字数较长的候选评论，格式为 `[{idx,text}]`）
- 输出：JSON 数组，最多 5 条；必须带回 `idx`、原始 `text` 和中文 `translation`，可带 `score` / `reason`。

完整 prompt：

```text
你是玩家评论原文精选器。请先阅读帖子标题和帖子正文，理解帖子真正讨论的对象、活动、玩法、皮肤、英雄、规则、奖励、时间、价格、视觉内容和核心诉求。

输入：
- post_title：帖子标题
- post_content：帖子正文
- comments_json：评论数组，格式为 [{"idx": 0, "text": "..."}]

任务：
从 comments_json 中选出适合作为“玩家评论原文精选”的候选评论。候选必须同时满足：
1. 与帖子标题或正文主题明确相关。
2. 不是纯索要皮肤、钻石、奖励、礼包、金币、点券或免费资源。
3. 不是灌水、玩笑、纯情绪喊话、无上下文抱怨、重复文本。
4. 表达相对完整，能体现具体观点、原因、体验、建议、疑问或情绪。
5. 原文有业务阅读价值，适合给产品/运营团队查看。

优先选择：
- 观点完整、有原因或细节的评论。
- 能代表真实玩家体验、担忧、期待、建议的评论。
- 与帖子核心对象强相关的评论。
- 多语言评论按语义判断，不要只看关键词。

剔除：
- off_topic：匹配、排位、队友、网络、外挂、客服等与帖子无关内容。
- low_value_reward_request：give me skin、free skin pls、pahingi skin、minta skin、求皮肤、送我皮肤、给钻石等纯索要内容。
- noise：短句、表情、重复喊话、无分析价值内容。
- 如果无法判断是否有业务价值，剔除。

输出要求：
- 只输出 JSON 数组。
- 最多返回 5 条候选。
- 必须带回原始 idx 和原始 text，不要改写、不要截断原文。
- translation 字段名必须固定写成 `translation`，值必须是 text 的简体中文翻译；如果原文已经是中文，也可以原样填入 translation。
- score 为 1-100，表示精选价值。
- reason 用简短中文说明为什么值得保留。
- 不要输出 Markdown。
- 不要输出解释。
- 不要包代码块。

输出格式：
[
  {
    "idx": 12,
    "text": "原始评论文本",
    "translation": "中文翻译文本",
    "score": 92,
    "reason": "评论具体说明了获取路径太难，并解释了玩家体验"
  }
]
```

### quote_select_final（LLM7，最终精选最多 50 条，单次）
- 模型：GPT-4o-mini
- 输入：`post_title`、`post_content`、`comments_json`（各批 `quote_select_batch` 候选汇总）
- 输出：JSON 数组，最多 50 条；必须包含 `idx` / `text` / `translation`。

完整 prompt：

```text
你是玩家评论原文最终精选器。请先阅读帖子标题和帖子正文，然后从 comments_json 中选出最终展示给业务方的玩家评论原文。

输入：
- post_title：帖子标题
- post_content：帖子正文
- comments_json：候选评论数组，格式为 [{"idx": 0, "text": "...", "score": 90, "reason": "..."}]

任务：
从候选中选出最多 50 条最适合展示在报告末尾的玩家评论，并提供中文翻译。

选择标准：
1. 必须与帖子主题或正文内容相关。
2. 必须有业务分析价值。
3. 优先表达完整、信息量高、观点清晰、包含原因/体验/建议的评论。
4. 尽量覆盖不同观点，不要让同质评论占满列表。
5. 保留原文，不改写、不截断；同时输出简体中文翻译。
6. 如果高质量候选不足 50 条，可以少于 50 条。

必须剔除：
- 纯索要皮肤、钻石、奖励、礼包、金币、点券或免费资源。
- 与帖子无关的匹配、排位、队友、网络、外挂、客服等抱怨。
- 灌水、玩笑、纯表情、重复喊话、无上下文短句。
- 与帖子正文不存在的对象强绑定的评论。

输出要求：
- 只输出 JSON 数组。
- 最多 50 条。
- 每个对象必须包含 idx、text、translation。
- translation 字段名必须固定写成 `translation`，值必须是 text 的简体中文翻译；如果原文已经是中文，也可以原样填入 translation。
- 可以包含 score、reason；后端展示 translation，并保留原文 text。
- 不要输出 Markdown。
- 不要输出解释。
- 不要包代码块。

输出格式：
[
  {
    "idx": 3,
    "text": "原始评论文本",
    "translation": "中文翻译文本",
    "score": 95,
    "reason": "观点完整且与帖子核心对象直接相关"
  }
]
```

---

## 四、Python 侧关键逻辑（comment_analysis.py）

可调参数集中在文件顶部：

- **清洗规则**（任一命中即丢）：
  - 长度 < `MIN_LEN`(15)
  - 键盘乱打（单字符占比 > 60% 且长度 > 20）
  - 不含任意语言字母（用 Unicode category `L*` 判断，**泰语/阿拉伯语/越南语/西里尔语等多语种评论都会保留**，只过滤纯数字/符号）
  - 完全去重（大小写不敏感）
  - 纯索要奖励（整条只索要钻石/皮肤等，正则整条锚定匹配；夹带其他意见的保留）
- **初始分层抽样** `SAMPLE_MAX_N`(5000)：先跑 5000 条
- **动态补样池** `SAMPLE_POOL_MAX_N`(15000)：若相关评论不足 `RELATED_TARGET_N`(1000)，继续按 5000 条一轮补样，直到达标或样本池耗尽
- **相关性批次** `RELEVANCE_BATCH_SIZE`(40)：要求逐条带回 `idx`，小批量降低漏返风险
- **主题提取批次** `BATCH_SIZE`(250)：5000 条 → 20 批
- **分类批次** `CLASSIFY_BATCH_SIZE`(40)：要求逐条带回 `idx`，后端会重试和自动拆批
- **玩家原文精选候选** `LONG_CANDIDATE_MAX_N`(1500)：规则清洗后按评论字数保留最长候选
- **精选初筛批次** `QUOTE_SELECT_BATCH_SIZE`(75)：每批最多保留 `QUOTE_SELECT_BATCH_KEEP_N`(5) 条候选
- **最终精选池** `QUOTE_SELECT_FINAL_POOL_N`(150)：送入 final 节点的候选上限，避免输入过大
- **最终展示** `QUOTE_SELECT_FINAL_N`(50)：报告最多展示 50 条玩家评论原文
- **低频合并** `OTHER_THEME_PCT`(3.0)：占比 < 3% 的主题归入「其他」
- **并发** `COMMENT_ANALYSIS_CONCURRENCY`(6，在 server.py)：20 批不全开，用 `asyncio.Semaphore` 限流，配合 `workflow_run` 的指数退避重试，避免上游 429

---

## 五、后端 API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/comment-analysis/upload` | multipart：`file` + `post_title` + `post_content`，三者必填。只保存文件并创建 session；重型预处理走 SSE |
| GET | `/api/comment-analysis/preprocess/{session_id}` | SSE。解析→清洗→抽样→存 session。结束推 `comment_preprocess_done`，含 `valid_count` / `sample_count` / `filter_stats` |
| GET | `/api/comment-analysis/run/{session_id}` | SSE。主线做 relevance / extract / merge / classify / report，精选评论支线并行运行。主报告完成先推 `comment_done`；精选完成后再推 `comment_quotes_done`，失败推 `comment_quotes_error` |

结果同时写入 session 的 `report_md`，复用现有 `/api/export/pdf/{sid}` 与 `/api/export/feishu/{sid}` 导出。

---

## 六、前端

- 侧边栏新增「评论分析」一级导航（`nav-comment`），`switchMode('comment')` 切换
- 三步面板：`cm-panel-1`（上传+标题+原文）/ `cm-panel-2`（流式进度）/ `cm-panel-3`（结果）
- 结果区：AI 舆情简报 Markdown + PDF/飞书导出；不再单独展示“整体情感分布”和“玩家核心观点”卡片
- 代码位置：`static/index.html`、`static/app.js`（`cmState` / `cmGoStep` / `cmStart` / `cmRun` / `cmRenderResult`）、`static/style.css`（`.cm-*`）

---

## 七、自测命令

不依赖前端、直接连真机 Dify 跑完整链路（小样本）：

```bash
python3 - << 'PY'
import asyncio, server
sess = {'kind':'comment','comment_post_title':'测试帖','comment_post_content':'这是一篇测试帖正文',
        'comment_sample':['comment one ...','comment two ...']}
async def run():
    async for k,p in server._comment_analysis_pipeline(sess):
        print(k, p if k!='result' else {kk:p[kk] for kk in ('sentiment_overall','total_classified')})
asyncio.run(run())
PY
```

排错口诀：**merge 凭空编主题 / classify 只回一条 other → 八成是变量没绑定或改了草稿没发布。** 单独 Test Run 那个节点时记得把 `mode` 填对（如 `merge`），否则会走 ELSE 分支、0 token、输出空 `{}`。
