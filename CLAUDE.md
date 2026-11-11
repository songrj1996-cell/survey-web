# survey-web 项目规则

## 完整规则

完整的行为准则在 [harness-workbench/RULES.md](harness-workbench/RULES.md)，开始任何任务前必须先读这个文件。

**本文件只是入口摘要。若本文件与 `RULES.md` 的表述不一致，一律以 `RULES.md` 为准。**

---

## 最关键的几条（必须记住）

### 1. 改动前必须确认

新增、编辑、删除或重命名任何文件前，按 [harness-workbench/RULES.md](harness-workbench/RULES.md) 第一节的要求向用户说明并取得明确同意；同一方案批准后，在方案范围内不重复确认。授权分档（0 档免审批 / 1 档方案一次批准 / 2 档每次单独批准）也见该节。

### 2. 发现问题只报告，不擅自修改

阅读或修改代码时发现的任何额外问题（bug、不一致、可优化之处），只能告知用户，不能自行修改。

### 3. 改动范围严格等于用户要求

用户说改 A，只改 A。不做任何未被要求的额外改动，无论理由多充分。

### 4. data/ 目录禁止删除、清空或覆盖

`data/` 下存放运行时真实数据，任何情况下不得删除、清空或覆盖其中的文件和目录。

### 5. 密钥和配置只走 .env + config.py

不在任何代码文件中硬编码 API Key、服务地址或敏感配置。

---

## 项目架构参考

系统全景图见 [harness-workbench/ARCHITECTURE.md](harness-workbench/ARCHITECTURE.md)。
