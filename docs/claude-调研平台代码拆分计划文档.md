# 调研平台代码拆分计划文档

> 目标:把项目里两个"又大又乱"的文件 —— 后端 `server.py`(6696 行)和前端 `static/app.js`(3799 行)—— 拆成一组职责单一的小文件,确立清晰的**代码边界**,让以后改一处不再牵动一片。
>
> 本文档为**计划**,不含任何代码改动。实施前请逐层确认。
>
> 编写日期:2026-06-25 · 最近更新:2026-06-25(融合 Codex 评审意见)

---

## 〇、本版方案怎么来的(共识说明)

本方案经过两轮 AI 互评(Claude 与 Codex)收敛而成,最终分工:

- **代码边界(目录分层)采用 Codex 的六部分划分** —— 更干净,避免某一层变成新的"大杂烩"。
- **执行路线、前端加载方式、第一轮拆分范围采用 Claude 的保守路线** —— 风险更低、可分步验证。
- **命名碰撞、个别文件归属由 Claude 补充修正** —— 解决两层同名、工具文件无家可归的问题。

---

## 一、为什么要拆(背景与动机)

当前项目的真正风险点只有两个文件:

| 文件 | 行数 | 问题 |
|---|---|---|
| `server.py` | 6696 | 后端"总管",一个文件里塞了 9 类互不相干的活儿:问卷分析、跑数表、评论舆情、数据标注、飞书登录、权限管理、审计日志、提示词管理、文件导出。约 200 个函数、50+ 接口共用一堆零碎工具函数。 |
| `static/app.js` | 3799 | 前端"总操作台",122 个函数,把四套业务流程(问卷 / 跑数表 / 评论 / 标注)+ 设置抽屉 + 历史抽屉全堆在一起。 |

**核心痛点**:这些功能彼此基本不相干,却住在同一个文件、共用同一批全局变量和工具函数。改"评论分析"时,稍不留神就可能碰坏"问卷报告"或"飞书登录"——这正是"改了这个坏了那个"的根源。

> 行数为本地实测,**执行前请以当时的实际代码为准**(可能与历史版本有出入,不影响整体方向)。

---

## 二、拆分总原则(为什么这么拆)

按**分层 + 单向依赖**拆,层与层之间只能从上往下依赖,绝不反向:

```text
routers  ->  services  ->  storage / integrations / core
```

- **routers**:HTTP 接口,只收请求、调 service、发响应。
- **services**:业务流程编排(原计划里叫 engines,改用更通用的 `services`)。
- **storage**:本地 JSON 持久化。
- **integrations**:对接 Dify、飞书等外部系统。
- **core**:配置、权限、通用响应等纯横切能力。
- **schemas**:Pydantic 请求/响应数据结构(非运行时层,被 routers/services 引用)。

**为什么这样分**:
- **单向依赖**让边界清晰——上层用下层,下层永远不知道上层存在,改业务线不会波及地基。
- **每一类关注点各占一层**——持久化、外部系统、业务编排、接口彼此隔离,杜绝某一层沦为"什么都往里塞"的大杂烩。
- **共享的东西只有一个出处**——配置只来自 `core/config.py`,数据读写只走 `storage/*`,外部调用只走 `integrations/*`。

---

## 三、结构性决策(已确认)

| 决策 | 选择 | 理由 |
|---|---|---|
| 后端文件布局 | **`app/` 包,六部分:core / storage / integrations / services / schemas / routers**(schemas 为数据结构定义,非运行时层) | 边界最清晰;每部分职责单一,避免 core 变成大杂烩。 |
| 后端入口兼容 | **保留一行式根 `server.py`**(`from app.main import app`) | 让 `uvicorn server:app` 命令不变,**deploy.sh 无需改动**。 |
| 前端加载方式 | **多个 `<script>` 顺序加载,函数保持全局** | 项目无打包工具,HTML 里大量 `onclick` 直接调全局函数。保持全局作用域可让这些调用照常工作,改动小、回归风险低。**不采用 ES Modules**(会导致内联 onclick 失效)。 |
| 第一轮范围 | **主拆 `server.py` 与 `app.js`**。第一轮**不拆已有干净业务模块的内部逻辑**(survey_stats / survey_plan / comment_analysis / annotate / crosstab_parser);但 **`dify.py`、`feishu_export.py` 作为外部系统封装会迁入 `integrations/`(只移动 + 改 import,不改内部逻辑)**;CSS、HTML 不进第一批 | 控制单批改动量,先把最乱的两个文件治好;同时把"外部封装归位"这件低风险的事一并做掉 |

---

## 四、最终目录结构(拆完之后长什么样)

```text
survey-web/
├── server.py                       # 仅一行:from app.main import app(保部署命令不变)
│
├── app/
│   ├── main.py                     # 创建 FastAPI、CORS、静态资源、中间件、注册所有 router
│   │
│   ├── core/                       # ① 纯横切:无业务、无 I/O、无外部系统
│   │   ├── config.py               # 环境变量、Dify/飞书配置、DATA_DIR 及各数据文件路径、阈值、免责声明文案、核心结论标记
│   │   ├── security.py             # 仅纯逻辑:用户识别、白名单匹配、管理员/权限判断、登录态cookie解析、未登录/无权限响应构造(白名单/登录态/会话的文件读写走 storage,不在此读写)
│   │   ├── audit.py                # 审计事件结构、获取客户端 IP、统一"构造审计事件"的入口(只构造事件,不读写文件;真正追加/保存由 storage/audit_store.py 完成)
│   │   ├── responses.py            # 文件下载响应、SSE event 格式化、常见错误响应、下载文件名处理
│   │   └── parsing.py              # 上传文件解析(CSV / Excel → 行数据),纯无状态工具
│   │
│   ├── storage/                    # ② 本地 JSON 持久化:一种数据一个文件
│   │   ├── sessions.py             # 分析会话(data/sessions/*.json)新建 / 读取 / 保存 / 过期清理(原子写)
│   │   ├── logins.py               # web 登录态/会话 token(web_logins.json)读写(供 core/security.py 经中间件调用,不被 security 直接读)
│   │   ├── history.py              # 历史记录读写、报告编号、归属过滤、存入历史
│   │   ├── audit_store.py          # 审计日志文件读取 / 追加 / 裁剪(改名避免与 core/audit.py 碰撞)
│   │   ├── settings.py             # 系统设置读写 + 默认设置合并
│   │   ├── prompts.py              # 提示词读写 + 报告/章节等提示词配置
│   │   ├── whitelist.py            # 白名单读写 + 旧权限结构迁移
│   │   └── ui_texts.py             # 前端可配置文案读写
│   │
│   ├── integrations/               # ③ 外部系统封装
│   │   ├── dify_client.py          # Dify chat / completion / workflow_run / SSE 流解析 / 端点兼容(由 dify.py 迁入)
│   │   └── feishu_client.py        # 飞书 OAuth、token 刷新、用户信息、建文档、传 PDF、发消息、callout 处理(由 feishu_export.py 迁入)
│   │
│   ├── services/                   # ④ 业务流程编排(不碰 HTTP、不碰 DOM)
│   │   ├── question_detect.py      # 本地题型推断、Google Form 矩阵分组、题型识别问询构建
│   │   ├── survey_workflow.py      # 问卷主流程编排:题型 → 方案 → 统计 → 报告生成 → 追问
│   │   ├── report_writer.py        # planner/writer 问询构建、大样本分批、开放题兜底、统计上下文拼装
│   │   ├── report_render.py        # Markdown 清洗、标题替换、核心结论高亮、Markdown→Word、Markdown→PDF(docx 渲染归此)
│   │   ├── export_service.py       # Word/PDF/Markdown/飞书导出的业务决策与报告内容准备
│   │   ├── comment_workflow.py     # 评论分析编排(并发调 Dify、精选原文引用);纯逻辑仍在 comment_analysis.py
│   │   └── annotate_workflow.py    # 标注编排(AI 识别 / 质量打标问询、结果解析、Excel 生成);纯逻辑仍在 annotate.py
│   │
│   ├── schemas/                    # ⑤ 请求/响应数据结构
│   │   └── requests.py             # 所有 Pydantic 请求模型(列确认、方案确认、追问、管理员、提示词更新、设置更新、标注确认等)
│   │
│   └── routers/                    # ⑥ HTTP 接口:每条业务线一个
│       ├── survey.py               # 上传 / 题型 / 方案 / 统计 / 报告 / 追问
│       ├── crosstab.py             # 跑数表专属上传
│       ├── comment_analysis.py     # 评论:上传 / 预处理 / 运行
│       ├── annotate.py             # 标注:上传 / 列确认 / AI 识别 / 质量打标 / 下载
│       ├── export.py               # 导出:Word/PDF/Markdown/飞书
│       ├── feishu.py               # 飞书:登录 / 回调 / 身份 / 登出
│       ├── admin.py                # 白名单 CRUD + 审计日志查询
│       ├── settings_api.py         # 上传说明 / 提示词 / UI 文案 / 系统设置(改名避免与 storage/settings.py 碰撞)
│       └── history.py              # 历史:列表 / 详情 / 改标题
│
├── static/
│   ├── js/                         # app.js 拆分后(顺序加载、函数全局)
│   │   ├── core/
│   │   │   ├── state.js            # 全局状态容器(session / 模式 / 报告上下文 / 历史上下文 / 筛选条件等)
│   │   │   ├── dom.js              # DOM 查询、HTML 转义、通用显隐工具
│   │   │   ├── api.js              # fetch 封装、JSON / 文件上传请求、错误处理
│   │   │   ├── sse.js              # GET / POST SSE 收流 + 统一错误处理 + 进度状态栏
│   │   │   ├── markdown.js         # marked 配置、Markdown 渲染、标题解析/替换
│   │   │   ├── toast.js            # Toast 与弹窗、主题切换、抽屉开关、步骤条/导航
│   │   │   └── ……                 # (导航/抽屉等通用件如更适合可并入上面某文件)
│   │   ├── features/
│   │   │   ├── survey.js           # 问卷主流程:上传/预览/题型确认/方案/统计·报告生成入口
│   │   │   ├── report.js           # 报告展示、目录、标题编辑、上下文切换、导出按钮、追问(QA)
│   │   │   ├── crosstab.js         # 跑数表三文件上传交互
│   │   │   ├── comment.js          # 评论分析全流程(cm*)
│   │   │   ├── annotate.js         # 数据标注状态机(ann*)
│   │   │   ├── settings.js         # 设置抽屉:权限表 / 审计 / 提示词 / UI 文案 / 系统设置
│   │   │   ├── history.js          # 历史抽屉:列表 / 筛选 / 打开 / 改标题
│   │   │   └── feishu.js           # 飞书登录态、权限门控、飞书导出
│   │   └── main.js                 # 页面初始化、各 feature 初始化注册、全局模式切换入口(最后加载)
│   ├── style.css                   # 第一轮不动(后续可选阶段再拆)
│   ├── index.html                  # 第一轮仅改 <script> 引用与版本号
│   └── login.html                  # 不动
│
└── (第一轮保持不动:survey_stats.py、survey_plan.py、comment_analysis.py、
     crosstab_parser.py、annotate.py;dify.py 与 feishu_export.py 迁入 integrations;
     根目录 config.py 并入 app/core/config.py)
```

> **关于现有根目录 `config.py`(10 行)**:为避免与 `app/core/config.py` 重名混淆,拆分时**并入** `app/core/config.py` 并删除原文件。

`index.html` 末尾脚本引用改为(顺序固定,`core/*` 在前、`features/*` 居中、`main.js` 最后):

```html
<script src="https://cdn.jsdelivr.net/npm/marked@12.0.0/marked.min.js"></script>
<script src="/static/js/core/state.js?v=28"></script>
<script src="/static/js/core/dom.js?v=28"></script>
<script src="/static/js/core/api.js?v=28"></script>
<script src="/static/js/core/sse.js?v=28"></script>
<script src="/static/js/core/markdown.js?v=28"></script>
<script src="/static/js/core/toast.js?v=28"></script>
<script src="/static/js/features/survey.js?v=28"></script>
<script src="/static/js/features/report.js?v=28"></script>
<script src="/static/js/features/crosstab.js?v=28"></script>
<script src="/static/js/features/comment.js?v=28"></script>
<script src="/static/js/features/annotate.js?v=28"></script>
<script src="/static/js/features/settings.js?v=28"></script>
<script src="/static/js/features/history.js?v=28"></script>
<script src="/static/js/features/feishu.js?v=28"></script>
<script src="/static/js/main.js?v=28"></script>
```

---

## 五、代码边界约定(拆分时与拆分后都必须遵守)

这是本次拆分的"宪法",目的是让边界长期不腐化。**核心依赖方向:`routers → services → storage / integrations / core`,严禁反向。**

### 后端

#### `routers`
- **负责**:接收 HTTP 请求;基础参数校验;登录/权限/管理员校验;调用 service;把结果转成 HTTP 响应。
- **不负责**:不编排复杂业务流程;不直接拼 Dify prompt;不直接操作外部系统;不直接读写 JSON 文件;不承载报告生成、评论分析、标注分析等长流程。

#### `services`
- **负责**:编排业务流程;调用 `storage` 读写业务数据;调用 `integrations` 访问 Dify/飞书;处理业务规则、流程分支、状态更新;组织 SSE 进度事件。
- **不负责**:不依赖 FastAPI 路由对象;不处理页面 DOM/前端状态;不硬编码文件路径(路径来自 `core/config.py`)。

#### `storage`
- **负责**:读写本地 JSON;维护 session / history / audit / settings / prompts / whitelist / ui_texts;对外提供干净的数据读写函数。
- **不负责**:不调 Dify/飞书;不判断业务流程;不感知 HTTP 请求与前端页面。

#### `integrations`
- **负责**:封装外部系统调用(Dify 请求/流式/workflow;飞书登录/建文档/传 PDF/发消息)。
- **不负责**:不决定业务流程是否继续;不动 history/session;不拼接页面展示内容。

#### `core`
- **负责**:配置读取;路径常量;登录/权限/白名单等基础能力;通用响应工具;通用审计工具;通用文件解析工具。
- **不负责**:不放具体业务流程;不放某个单独功能的特殊逻辑。**一旦发现往 core 里塞业务,就说明它该去 services/storage/integrations。**
- **`core/security.py` 的依赖红线**:只放认证与权限判断的**纯逻辑**、登录态 cookie 解析、响应构造;白名单、登录态、会话等**文件读写必须走 `storage/`**(由 `app/main.py` 的登录中间件或 router 把数据读出来后再传给 security 判断),**security 不直接 import storage**,以保持 `core` 是最底层叶子、不出现 `core → storage` 反向依赖。若短期实现上确需在 security 内调用 storage,须在代码注释中标注为"已知例外",避免边界悄悄腐化。

#### `schemas`
- **负责**:Pydantic 请求模型、必要的响应结构、共享类型定义。
- **不负责**:不做业务逻辑;不读文件;不调外部服务。

### 前端

#### `static/js/core`
- **负责**:全局状态容器;fetch/SSE 封装;DOM 工具;Markdown 渲染;Toast/弹窗/主题/导航等通用能力。
- **不负责**:不做具体业务页面流程;不直接绑定某业务页面的大量事件。

#### `static/js/features`
- **负责**:各业务页面自己的交互流程、事件绑定、数据渲染;每个 feature **只操作自己负责的 DOM 区域**。
- **不建议**:`comment.js` 直接改 `annotate.js` 的状态;`settings.js` 直接控制问卷分析流程;多个 feature 同时维护同一份 DOM;多个 feature 各写一套重复的 fetch/toast/SSE。

#### 前端全局可见性约定(必须遵守)
- 拆成多个 classic `<script>` 后,**凡是被 HTML 内联事件(`onclick` 等)或被其他文件按名字调用的函数/对象,必须显式挂到 `window`**,例如 `window.switchMode = switchMode;`。
- **不要依赖"普通 script 下 function 自动全局"这个隐性规则**:顶层 `function foo(){}` 确实会成为全局,但 `const foo = …` / `let foo = …` 不会成为 `window` 属性,内联 `onclick` 调不到,排查困难。统一用显式挂载,行为可预期。
- 共享状态对象(如 `state`)若需跨文件或被内联事件访问,同样显式挂到 `window`(如 `window.state = state;`)。

### 命名碰撞约定(必须遵守)
- 审计:`core/audit.py`(构造事件)与 `storage/audit_store.py`(读写文件)**用不同名字**。
- 设置:`routers/settings_api.py`(接口)与 `storage/settings.py`(持久化)**用不同名字**。
- 业务线同名旧模块(根目录 `comment_analysis.py` / `annotate.py`)第一轮保留在根目录,与 `routers/` 下同业务文件分属不同包,**import 时认准包路径**;后续阶段再决定是否下沉到 `services/`。

---

## 六、建议的执行步骤(为什么按这个顺序)

**总策略:纯搬家、零功能改动;自底向上、一次一层;先搬接口再抽业务(两遍法);每层完成后停下来验证再继续。**

> **为什么自底向上(地基 → 外部 → 接口 → 业务 → 前端)?**
> 依赖是单向向下的。先把被依赖的底层(配置、存储、外部封装)稳定下来,上层迁移时引用目标已就位,不会悬空;每搬完一层都能立刻跑起来验证,把风险锁定在最小范围。
>
> **为什么"先搬接口、再抽业务"(两遍法)?**
> 第一遍只把接口整体挪进 `routers/`(逻辑可暂时留在原处或调用现有模块),机械、低风险、易验证;第二遍才把重逻辑从 router 抽进 `services/`。两遍各自单步风险更小,比"一步到位"更可控。

| 步骤 | 内容 | 完成后验证 |
|---|---|---|
| **0. 准备** | 建 `app/` 及 core/storage/integrations/services/schemas/routers 子目录与 `__init__.py`;建 `static/js/core`、`static/js/features`;确认当前服务能正常启动(基线) | 服务可启动,四条业务线均正常 |
| **1. 地基层** | 抽出 `core/`(config+security+audit+responses+parsing),合并根 `config.py`;抽出 `storage/` 八个文件;抽出 `schemas/requests.py`;`server.py` 暂改为从这些模块 import | 服务可启动;登录、数据文件读写、参数校验行为不变 |
| **2. 外部系统层** | `dify.py` → `integrations/dify_client.py`;`feishu_export.py` → `integrations/feishu_client.py` | 服务可启动;问卷报告、评论分析、飞书导出可跑通 |
| **3. 接口搬家(第一遍)** | 新建 `app/main.py` 装配;把各接口按业务线迁入 `routers/*`(重逻辑可暂留/调现有模块);根 `server.py` 瘦身为一行 | 服务可启动;所有 API 路径不变,逐条冒烟测试 |
| **4. 抽业务编排(第二遍)** | 把报告生成、评论分析、标注、导出等重逻辑从 router 抽进 `services/*`,router 变薄 | 服务可启动;四条业务线 + 导出结果与拆分前一致 |
| **5. 前端** | 按 core/features 把 `app.js` 切成 `static/js/*`;改 `index.html` 脚本引用与版本号 | 浏览器端四条业务线 + 设置/历史抽屉逐一点测,控制台无报错 |
| **6. 收尾** | 全量回归;删除 `app.js`、根 `config.py` 等已迁移旧文件 | 全功能无回归 |

**每步硬性要求:**
- 每步只搬运、不改逻辑;搬运后**全局搜索核对引用是否全部更新**。
- 每步结束都要能启动服务并通过对应验证,**未通过不进入下一步**。
- 每完成一步提交一次 git,便于精确回退。

---

## 七、每步完成后的检查清单

每完成一个阶段,至少检查:
- 应用可正常启动,服务器日志无新异常/告警。
- 登录状态正常;管理员与权限门控正常。
- 上传问卷 → 题型确认 → 方案 → 统计 → 报告生成全流程正常。
- Word / PDF / Markdown / 飞书导出正常。
- 历史记录读取、改标题、续聊正常。
- 评论分析可完成;数据标注可完成并下载结果。
- 跑数表上传与后续流程正常。
- 浏览器控制台无脚本加载错误或未定义函数。

---

## 八、影响面与风险

1. **新增 / 移动文件较多**(后端约 30 个文件分六层 + 前端约 15 个文件);但分批进行,每批可独立验证。
2. **三处装配点需改**:根 `server.py`(变一行)、`index.html`(换 script 标签)、各 Python 文件间的 `import`。
3. **`deploy.sh` 不需改**:入口命令 `uvicorn server:app` 因薄 `server.py` 而保持有效。
4. **`.env`、数据目录、对外接口路径、用户流程均不变**:对用户和已部署环境无感知。
5. **主要风险 = 引用遗漏 / 命名引错**:某处仍指向旧位置,或在两个同名文件间引错。靠"自底向上分层 + 每层验证 + 全局搜索引用 + 命名碰撞约定"来控制。
6. **前端加载顺序风险**:`core/*` 必须先于 `features/*`、`main.js` 最后;顺序错会导致"函数未定义"。改前端文件后统一升级 `?v=` 版本号,避免缓存旧脚本。

---

## 九、后续可选阶段(第一轮不做)

第一轮稳定后,如有需要再推进,且同样"一次一层、每步验证":

1. **下沉已干净的纯逻辑模块到 `services/`**:`survey_stats.py`、`survey_plan.py`、`comment_analysis.py`、`annotate.py`、`crosstab_parser.py`。可按统计类型 / 解析阶段进一步细分(如 stats_choice / stats_scale / stats_crosstab;comment_file_parser / preprocess / sampling / aggregate)。这些模块当前已职责清晰,**不急,收益验证后再做**。
2. **拆 `static/style.css`(5391 行)**:`base.css` / `layout.css` / `components.css` + 各业务 feature 样式(survey / report / history / settings / comment / annotate)。CSS 现在也很大,**不建议永久不拆**,但放在前端 JS 模块稳定之后。
3. **轻量整理 `static/index.html`**:按业务区块加注释、保持 id 不变、删冗余结构;若日后引入模板系统,再考虑 partials 化。

---

## 十、确认清单

- [ ] 同意 core / storage / integrations / services / schemas / routers 六部分的边界与职责
- [ ] 同意"自底向上 + 先搬接口再抽业务(两遍法) + 每层验证"的执行顺序
- [ ] 同意前端"顺序 `<script>` + 全局作用域"的加载方式
- [ ] 同意第一轮不动已干净模块、CSS、HTML,列为后续可选阶段
- [ ] 同意命名碰撞约定(audit / audit_store、settings_api / settings)
- [ ] 决定从哪一步开始实施(默认:步骤 0 → 1 → … 顺序推进)
