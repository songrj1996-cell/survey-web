# 数据标注：直连 LLM 架构

数据标注不再调用 Dify Workflow。后端在
`app/services/annotate_workflow.py` 中编排三个独立任务，统一通过
`app/integrations/llm_client.py` 调用公司 LLM 分发服务：

- AI 作答识别
- 逐题质量打标
- 统一中文翻译

原来两个 Dify DSL 中相同的 `translation_repair` 分支已合并为统一翻译任务；
AI 作答识别和质量打标仍保持各自独立的业务口径与输出结构。

## 标注流程

```text
上传问卷回收表
  → 本地解析、识别候选列
  → 统一翻译非中文表头并由用户确认列
  → AI 作答识别：按玩家综合全部主观题回答
  → 人工确认疑似 AI 作答结果
  → 逐题质量打标：无效 / 普通 / 优秀 / N/A
  → 统一补齐非中文回答的中文翻译
  → 本地汇总、导出 Excel 并写入历史
```

翻译任务同时服务于表头翻译和回答翻译。中文原文、纯数字和无需翻译的游戏术语
由本地规则直接保留；模型只处理仍缺失的非中文内容。

## 三个模型任务

| 任务 | 主要输入 | 主要输出 | 默认首选 | 默认备选 | 推理强度 | 输出上限 |
|---|---|---|---|---|---|---|
| AI 作答识别 | 同一玩家的全部主观题回答 | `ai_prob`、`polish_prob`、中文理由、正反证据原文 | `gpt-5.6-sol` | `claude-sonnet-5` | `high` | 32K |
| 逐题质量打标 | 玩家 ID 与各主观题回答 | 每题标签、中文理由和连续原文证据 | `gpt-5.6-terra` | `claude-sonnet-5` | `medium` | 32K |
| 统一中文翻译 | `id`、单元格 `key` 与原文 | 对应 `id` / `key` 的完整中文译文 | `gpt-5.6-terra` | `gemini-3.5-flash` | `medium` | 16K |

模型配置复用 `LLM_API_BASE` 和 `LLM_API_KEY`，并可分别通过以下环境变量覆盖：

- `LLM_ANNOTATE_AI_MODEL` / `LLM_ANNOTATE_AI_FALLBACK_MODELS`
- `LLM_ANNOTATE_QUALITY_MODEL` / `LLM_ANNOTATE_QUALITY_FALLBACK_MODELS`
- `LLM_ANNOTATE_TRANSLATION_MODEL` / `LLM_ANNOTATE_TRANSLATION_FALLBACK_MODELS`
- 三个任务各自的 `*_REASONING` 和 `*_MAX_TOKENS`

完整示例见项目根目录的 `.env.example`。迁移后不再需要
`DIFY_AI_DETECT_KEY`、`DIFY_QUALITY_KEY` 或通用 Dify 配置。

## 批处理与完整性

- 保留 AI 识别 45K、质量打标 40K 的单次输入字符预算。
- 保留按行数和字符数拆批、并发上限及 SSE 心跳，长任务期间前端会持续收到进度。
- AI 识别以玩家为单位综合判断，`ai_prob` 与 `polish_prob` 相互独立；仅使用 AI
  润色不等同于实质内容由 AI 生成。
- 质量打标逐题判断，整体质量由本地规则根据逐题结果汇总，不交给模型自由生成。
- AI 结果、质量结果和翻译结果分别记录缺失集合，避免一个阶段的缺失被误报为另一个阶段失败。
- AI 识别按缺失玩家补跑，质量打标只补跑缺失或无效的题目列，已通过校验的结果会保留。
- 翻译先按每批 20 项处理，缺失项再按每批 5 项补跑；超过单次字符预算的长文本会分段翻译后合并。

## 输出校验与降级

- AI 识别校验玩家 ID 是否与输入唯一匹配、概率整数范围、理由，以及证据是否为回答中的连续原文。
- 质量打标校验每个玩家和每道题是否完整，标签只能是“无效反馈”、
  “普通反馈”、“优秀反馈”或 `N/A`，证据必须来自对应单元格。
- 翻译校验 `id` / `key` 是否与请求一致、译文是否非空，并保留无需翻译的中文或术语内容。
- JSON 无法解析时会先让同一模型按原任务定向再生成一次；模型调用失败或再次无效时，
  再按任务切换到备选模型。仍缺失的结果保留明确状态，供补跑和前端提示使用。
- 翻译缺失不会删除已经通过校验的 AI 识别或质量标签；导出只使用可信结果，
  不会用不完整的模型输出覆盖已有有效标注。

## 提示词管理

三套 System Prompt 的默认值来自迁移时提供的两个 DSL：

- `web-回答打标-AI识别-mode路由.yml`
- `web-回答打标-质量识别-mode路由.yml`

管理员可在设置页分别编辑“AI 作答识别”“逐题质量打标”和“统一中文翻译”提示词，
对应内部键为 `annotate_ai_system`、`annotate_quality_system` 和
`annotate_translation_system`。修改后的下一次标注调用立即生效，无需重启服务。

## 验证

```powershell
python -m unittest tests.test_annotate
python -m compileall app
python scripts/check_boundaries.py
git diff --check
```
