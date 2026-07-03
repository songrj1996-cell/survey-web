# survey-web 项目规则

## 必读文件

开始任何任务前，必须先阅读并遵守：

- `harness-workbench/RULES.md`
- `harness-workbench/ARCHITECTURE.md`

`RULES.md` 是本项目的行为准则，`ARCHITECTURE.md` 是理解系统运行方式和模块边界的参考。

---

## 最关键的规则

### 1. 改动前必须确认

动任何代码文件前，必须先告诉用户：

1. 打算改哪个文件
2. 具体改什么
3. 为什么要改
4. 改完后怎么验证

得到用户明确同意后，才能开始修改。

### 2. 发现问题只报告，不擅自修改

阅读或修改代码时发现的额外问题、bug、风险或优化点，只能告知用户，不能顺手修改。

### 3. 改动范围严格等于用户要求

用户要求改 A，就只改 A。未经要求的任何改动，一律不做。

### 4. 保护 data/ 目录

`data/` 下存放运行时真实数据。不得删除、清空、覆盖或格式化其中任何文件和目录。

### 5. 密钥和配置

不得在代码中硬编码 API Key、外部服务地址、部署路径或敏感配置。配置统一走 `.env` 和 `app/core/config.py`。

### 6. 验证和说明

改动完成后，必须说明做了哪些验证；如果没有运行验证，必须说明原因。

---

## Python 环境与验证

本机可用的 Python 环境：

- `python`：可用，当前为 Python 3.14.3。
- `uv`：可用。

不要使用：

- `python3`
- `conda`
- `.venv/`：该虚拟环境已废弃，指向的 Python 3.10 已不存在。

验证约定：

- 后端边界检查命令固定为：`python scripts/check_boundaries.py`
- Python 语法检查优先使用：`python -m compileall app`
- 如果 Codex 沙箱里 `python` 无法运行，不要反复尝试 `.venv`、`python3`、`conda` 或下载新的解释器；改为做静态检查（例如 diff/read-back/语法层面的人工检查），并在最终说明中明确写出“未运行 Python 验证”的原因。
