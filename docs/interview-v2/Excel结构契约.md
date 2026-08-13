# 访谈报告 V2：Excel 结构契约

> Fixture Schema：`interview-workbook-physical-truth/1.0-draft`
> 运行时快照 Schema：`interview-workbook-physical-snapshot/1.0`
> 解析器：`interview-v2-workbook-parser/1.0`
> 本文定义批次 1 已实现的物理事实与边界，不把启发式建议伪装成人工真值。

## 三层数据

### 1. 原始物理层

不可变保存并可复现：

- 文件 SHA-256、文件名、大小和解析器版本；
- Sheet 名、顺序和有效范围；
- 单元格坐标、原始值、公式、合并范围、隐藏状态和显示线索；
- 单元格是否为空及规范化文本哈希。

原始层不得因人工修正或模型判断而覆盖。

批次 1 运行时快照顶层固定包含：

```text
schema_version / parser_version / original_filename
file_size / content_sha256 / snapshot_sha256
preflight / summary / sheets / warnings / confirmation_required
```

- `preflight` 记录 OOXML 校验、ZIP 条目数、解压后体积、最大压缩比、物理单元格数、合并区域覆盖面积、潜在物化单元格数、工作表/关系/辅助 XML 节点、样式/共享字符串部件计数、宏和外链检查结果；
- `summary` 只聚合 Sheet、非空单元格、文本字符、公式、合并范围和隐藏行列数量；
- `snapshot_sha256` 基于规范化快照计算，不包含文件名和哈希字段自身；相同物理内容改名后仍得到相同快照哈希；
- 完整物理快照只在服务端持久化；上传状态接口只返回脱敏计数和结构候选摘要，不返回单元格正文。

每个运行时 Sheet 至少保存：

```text
sheet_id / index / name / state / sheet_type
declared_range / content_range / dimensions
hidden_rows / hidden_columns / merged_ranges
non_empty_cell_count / text_char_count / formula_count
style_table / cells / column_profiles
candidate_structure / candidate_participant_region
```

`declared_range` 是文件声明，`content_range` 根据实际非空内容重新计算；两者不得互相替代。候选结构区和候选玩家区只用于下一步人工确认，不形成玩家身份绑定。

每个非空物理单元格至少保存坐标、行列号、原始值、规范化文本、展示值、值类型、公式及缓存状态、合并锚点、隐藏状态、`style_id` 和内容哈希；完整样式事实按 Sheet 在 `style_table` 去重保存，不在每个单元格重复内嵌。公式只读取文本和已有缓存值，绝不执行；没有缓存值时生成 `FORMULA_CACHE_UNAVAILABLE` 警告。

资源预算同时覆盖 ZIP 物理 `<c>` 节点、行列定义、工作表声明范围、合并区域累计面积、超链接展开范围、评论引用和所有持久化文本表示，并在构建完整快照前尽早阻断。`non_empty_cell_count` 仍表示业务上有内容的单元格，不与用于防内存放大的物理单元格或潜在物化单元格预算混为一义。

解析器不假定实际工作簿、工作表或共享字符串位于固定路径。它先按 `[Content_Types].xml` 与 Relationship Type 确定 `openpyxl` 将加载的实际部件，再对同一部件做预检；多个主工作簿、关系歧义、未知 Sheet 关系类型或部件路径逃逸均拒绝。当前 `openpyxl` 固定读取 `xl/styles.xml`，因此样式部件只支持该路径，避免预检与实际加载不同源。宏工作表、VBA 关系和外部链接按 ContentType/Relationship Type 阻断，不依赖部件路径名称。

由于 `openpyxl` 会全量加载样式和共享字符串，即使它们未被单元格引用，批次 1 还固定采用以下模块级安全上限：

| OOXML 项目 | 上限 |
|---|---:|
| `styles.xml` 解压后大小 | 8 MiB |
| `sharedStrings.xml` 解压后大小 | 16 MiB |
| 共享字符串 `<si>` 数量 | 32,768 |
| `font` / `fill` / `border` / `dxf` / `numFmt` 定义 | 各 4,096 |
| `xf` 定义 | 16,384 |

其他会在加载时物化的 OOXML 元数据采用以下模块级边界：

| OOXML 项目 | 上限 |
|---|---:|
| `[Content_Types].xml` / 主工作簿 XML / 单个关系 XML | 各 2 MiB |
| 单个评论 XML | 4 MiB |
| 单个工作表 XML | 16 MiB |
| 单个辅助 XML（表格、绘图、图表、VML 等） | 8 MiB |
| ContentType 节点 / 全包 Relationship 节点 | 8,192 / 32,768 |
| 全工作簿工作表 XML 节点 | 2,000,000 |
| 条件格式 / 数据验证 / 超链接 | 各 32,768 |
| `cfRule` | 65,536 |
| 表格或绘图引用 | 各 4,096 |
| 绘图锚点 | 各类型 16,384 |
| 评论 | 32,768 |
| 超链接与评论累计潜在物化单元格 | 250,000 |

这些检查和 worksheet 的 `dimension`、物理 `<c>`、`row`、`col`、`mergeCell` 检查均使用按 XML local-name 识别的流式解析，在调用 `openpyxl` 前完成，不能通过自定义命名空间前缀或替代 PartName 绕过。

批次 1 的最低导入门槛是：至少一张工作表能同时提出左侧结构区和候选玩家区，且候选玩家列不少于一列。任意单格或普通无访谈结构表格不能仅凭“非空”创建正式项目；候选区通过门槛后仍保持 `confirmation_required`，不形成玩家身份绑定。

## 批次 1 运行边界

- V2 默认关闭，关闭时不注册 API 路由，也不会创建 V2 数据目录；
- 当前隔离存储、幂等认领和预检租约使用本地文件与进程内锁，适配仓库现有 `uvicorn --workers 1` 部署；
- 启用多进程或多实例前，必须改为跨进程共享锁/数据库事务和持久任务队列，不能把当前进程内锁当作分布式并发保证；
- 后台任务中断后，同一幂等键重试会恢复 `QUARANTINED` 或租约过期的 `PRECHECKING`；正式启用前仍应设计定期恢复扫描和运维告警；
- 拒绝的上传清除隔离原文件和可能包含业务内容的文件名、报告重点，只保留哈希、大小、合同版本、状态、所有者审计字段和脱敏错误。

批次 1 只注册三个独立接口：

```text
POST /api/v1/interview-upload-attempts
GET  /api/v1/interview-upload-attempts/{upload_attempt_id}
GET  /api/v1/interview-imports/{import_id}
```

首次 POST 原子保存隔离文件后返回 `QUARANTINED`，响应结束后进入 `PRECHECKING`。GET 轮询如果发现仍为 `QUARANTINED` 或预检租约已过期，会安全地重新调度；CAS 与 claim token 保证重复调度不会让旧任务覆盖新任务。最终状态为 `ACCEPTED` 或 `REJECTED`。

### 2. 候选与确认层

分别保存：

- 结构列和候选玩家列；
- Sheet 组别、记录员和玩家绑定建议；
- 决策状态、来源、置信度、确认人和确认时间；
- 人工覆盖项及其基线版本。

建议不等于确认。多 Sheet 工作簿必须完成一次整体分组确认；仅列序一致不得静默合并玩家。

### 3. 证据与派生层

在映射确认后建立：

- 功能模块、主问题、追问和问题出现位置；
- 玩家自述、追问补充、研究员观察三类证据身份；
- 玩家属性事实与分析标签；
- 覆盖矩阵、玩家档案、跨玩家发现、统计事实和报告主张。

所有派生内容必须携带上游版本和证据引用；人数按内部玩家 ID 去重。

## 批次 0 Fixture Schema

仓库中的脱敏真值只允许保存结构元数据：

```json
{
  "schema_version": "interview-workbook-physical-truth/1.0-draft",
  "source": {
    "fixture_id": "system1",
    "sha256": "...",
    "contains_raw_interview_text": false
  },
  "workbook": {
    "sheet_count": 2,
    "formula_count": 0,
    "sheets": [
      {
        "sheet_id": "sheet_01",
        "source_sheet_name": "...",
        "used_range": "A1:S102",
        "structure_columns": "A:C",
        "candidate_participant_columns": "D:S",
        "candidate_participant_count": 16
      }
    ]
  },
  "decisions": {
    "group_mapping": "research_confirmation_required",
    "participant_bindings": "research_confirmation_required"
  }
}
```

禁止写入 fixture：

- 玩家原话、姓名、联系方式或属性正文；
- 原始单元格全文或可逆转的文本编码；
- 未经研究员确认的组别和跨 Sheet 玩家映射；
- 把“没有记录”推导成“未询问”或“没有需求”的结论。

## 已验证样本事实

### system1

- 工作簿包含 2 个记录 Sheet，物理范围分别为 `A1:S102` 和 `A1:T86`。
- 两张 Sheet 都使用 `A:C` 表达提纲结构，但玩家记录区起始列不同。
- Sheet 1 的候选玩家区为 `D:S`，共 16 列；Sheet 2 为 `H:T`，共 13 列。
- 两张 Sheet 都不含公式。
- 不能按相同列位置自动绑定玩家；是否同组及哪些列代表同一玩家仍需研究员确认。

### system3

- 工作簿包含 1 个记录 Sheet，物理范围为 `A1:O84`。
- `A:C` 为提纲结构，`D` 是空白分隔列，候选玩家区为 `E:O`，共 11 列。
- 不含公式。
- 单 Sheet 不需要跨 Sheet 玩家绑定，但每个候选列仍需确认“一列一玩家”的语义合同。

## 决策优先级

```text
用户已确认的分组、玩家和证据修正
> 原始单元格及明确字段
> 确定性位置与继承规则
> 样式、缩进和合并结构
> AI 语义建议
> 待人工确认
```

## 批次 1 结论

当前样本足以证明：

- 玩家区起始列不能写死；
- 同一项目内不同记录员的布局可以不同；
- 多 Sheet 的玩家身份不能仅依赖列序；
- 物理解析可先确定 Sheet、范围、列区和坐标，但组别、玩家映射及语义归属必须独立版本化。

当前样本尚不足以冻结自动化阈值，也没有形成经研究员批准的跨 Sheet 玩家绑定真值。

因此批次 1 只发布物理快照和待确认候选。`GROUP_MAPPING_CONFIRMATION_REQUIRED` 与 `PARTICIPANT_MAPPING_CONFIRMATION_REQUIRED` 表示需要研究员操作，不表示系统已经确认分组或玩家关系。
