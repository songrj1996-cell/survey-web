# 帖子评论舆情分析：直连 LLM 架构

评论分析不再调用 Dify Workflow。后端在
`app/services/comment_pipeline.py` 中编排七个 LLM 任务，统一通过
`app/integrations/llm_client.py` 调用公司 LLM 分发服务。

## 分析流程

```text
上传 CSV / XLSX
  → 本地清洗、去重、分层抽样
  → relevance：相关性筛选（分批并发，不足目标时补抽）
  → extract：候选主题提取（分批并发）
  → merge：跨批主题合并去重（单次）
  → classify：评论多标签主题/情感分类（分批并发）
  → 本地统计：主题提及占比、情感分布、代表引用
  → report：中文舆情简报（单次）
```

玩家原文精选与主流水线并行：

```text
长评论候选池
  → quote_select_batch：每批初筛最多 5 条
  → quote_select_final：跨批去重并精选最多 50 条
  → 追加到报告末尾
```

## 模型配置

| 任务 | 默认首选 | 默认备选 |
|---|---|---|
| 相关性筛选 | `gpt-5.6-terra` | `deepseek-v4-flash` |
| 主题提取 | `claude-sonnet-5` | `gpt-5.6-terra` |
| 主题合并 | `claude-sonnet-5` | `gpt-5.6-sol` |
| 评论分类 | `gpt-5.6-terra` | `gemini-3.5-flash` |
| 舆情简报 | `claude-sonnet-5` | `gpt-5.6-sol` |
| 原文初筛 | `gpt-5.6-terra` | `deepseek-v4-flash` |
| 原文最终精选 | `claude-sonnet-5` | `gpt-5.6-terra` |

完整环境变量示例见项目根目录的 `.env.example`。这些任务复用
`LLM_API_BASE` 和 `LLM_API_KEY`，不需要评论分析专用的 Dify Key。

## 提示词管理

七套 System Prompt 的默认值来自迁移时导出的
`web-评论分析-planb` DSL，并已将 Dify 变量引用替换为直连 JSON 输入协议。
管理员可在设置页分别修改：

- 评论相关性筛选
- 评论主题提取
- 评论主题合并
- 评论分类
- 评论舆情简报
- 评论原文初筛
- 评论原文精选

修改后的下一次评论分析立即生效，无需重启服务。

## 结构校验与容错

- JSON 任务会校验数组类型、`idx`、主题 ID、情感枚举和翻译完整性。
- 结构失败时，同一模型定向再生成一次；仍失败则切换备选模型。
- 相关性和分类批次在多模型失败后会自动拆小补跑。
- 原文最终精选失败时，会回退到已校验的分批候选，不影响主报告。
- 主题占比由本地程序计算：提及该主题的评论数 / 相关评论总数。
  因为允许多标签，各主题占比之和可能超过 100%。

## 验证

```powershell
python -m unittest tests.test_comment_direct_llm tests.test_upload_formats
python -m compileall app
python scripts/check_boundaries.py
git diff --check
```
