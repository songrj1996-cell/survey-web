# 飞书知识库证据链接自动更新

快速报告的证据和返回链接最初使用原文档的 docx 地址。文档移入知识库后，任务查询对应的 wiki 节点，把已有链接改用 wiki 地址，保留原段落 ID。

此功能不重新生成报告，不调用模型，不改文档名称、证据文字、统计表或目录层级。仅处理平台登记的快速报告。完整报告不自动登记；手工登记的文档如果不符合快速报告结构，会停止修复。

## 触发方式

1. 启用后，导出快速报告会登记文档标识和原始地址。订阅和后续请求在后台运行。
2. 后台订阅该文档的飞书事件，并立即检查一次是否已移入知识库。
3. 收到阅读、标题修改、编辑事件后，查询对应的 wiki 节点。
4. 检查目标段落和现有链接，按读取到的文档版本提交更新，并回读确认。

这里没有轮询所有飞书文档。后台默认每 2 秒查看本地待办；同一文档的事件会合并，默认冷却 30 秒。

如果迁移事件恰好在一次“尚未进入知识库”的检查过程中到达，会安排一次延后复查；没有新事件时不会持续查询该文档。

**不保证“移动”这个动作本身必定发出事件。**真实验收须覆盖移入后打开、改名和编辑的投递情况。飞书投递延迟、事件权限、知识库策略须在真实租户联调后确认，本地模拟通过不能替代这一验收。

已完成的文档会忽略后续事件，包括任务自身修改造成的编辑事件。该机制针对这次从 docx 到 wiki 的地址转换；完成后不会持续改写用户后来手工设置的新链接，也不会恢复被用户删除的证据。

## 部署配置

默认关闭。代码部署、应用配置及真实文档订阅和更新，应分别按项目规则取得批准。

在已有部署环境中设置：

    FEISHU_WIKI_AUTO_UPDATE_ENABLED=false
    FEISHU_EVENT_VERIFICATION_TOKEN=<同一飞书应用的 Verification Token>
    FEISHU_EVENT_ENCRYPT_KEY=<同一飞书应用的 Encrypt Key>
    FEISHU_NAVIGATION_JOB_TIMEOUT_SECONDS=45
    FEISHU_NAVIGATION_WORKER_INTERVAL_SECONDS=2
    FEISHU_NAVIGATION_COOLDOWN_SECONDS=30
    FEISHU_NAVIGATION_MAX_ATTEMPTS=3
    FEISHU_NAVIGATION_SEED_DOC_URLS=

不要提交真实密钥。已有 FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_BASE 继续使用原配置，无需增加 Python 依赖。

飞书开放平台配置步骤：

1. 在同一应用的“事件与回调”中选择 HTTP 请求地址模式，设置公开 HTTPS 地址：当前平台域名后加 /api/feishu/events。此接口使用飞书 Token、签名和加密验证，不依赖浏览器登录。
2. 将应用的 Verification Token 和 Encrypt Key 填入服务端环境，部署并重启后完成地址验证。开关关闭时也允许经过验证的 challenge，便于先配置。
3. 添加云文档被阅读（drive.file.read_v1）、标题修改（drive.file.title_updated_v1）、编辑（drive.file.edit_v1）三个事件。
4. 按控制台提示开通事件和文档订阅所需的应用权限并发布生效。应用还需要读取 wiki 节点、读取文档块及更新文档的接口权限和资源权限；平台登录成功不代表这些权限均已具备。
5. 将 FEISHU_WIKI_AUTO_UPDATE_ENABLED 设置为 true，重启服务，再进行真实文档验收。

应用事件订阅和逐篇文档订阅均需成功。代码先查询是否已订阅，再按需订阅，避免恢复任务时重复订阅。

参考：[官方事件处理和验签](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/dispatcher_handler.py)、[文档阅读事件](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/drive/v1/model/p2_drive_file_read_v1.py)、[文档事件订阅](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/drive/v1/model/subscribe_file_request.py)。

## 旧文档一次性登记

旧导出文档不会被自动发现或扫描。将明确指定的**原始 docx 地址**加入 FEISHU_NAVIGATION_SEED_DOC_URLS，多个地址用逗号分隔；不要把 docx token 直接替换成 wiki token。

下次启动时仅登记这些文档。已存在的记录不会重置，成功登记后可从配置中移除种子地址。已进入知识库的文档在首次后台检查时处理。

用户当前已验证过的旧报告也需要这次明确登记。本地开发验证不执行真实登记或更新。

## 状态、重试与数据

状态保存在 DATA_DIR/feishu_navigation/registry.json，进程锁为同目录 registry.lock，原子写入的临时文件也只在该目录生成。记录包含文档 ID、URL、最近事件 ID、状态、尝试次数和错误代码，不保存报告正文或凭据。

启用后会新增或更新此目录。当前开发验证仅使用工作树 .test-tmp/ 下的隔离目录，**不写真实 DATA_DIR**。线上启用前应明确实际 DATA_DIR，并批准相应目录的数据写入。

管理人员可在正确部署环境只读查看状态：

    from app.storage.feishu_navigation import list_records
    for record in list_records():
        print(record["doc_token"], record["status"], record.get("last_error", ""))

| 状态 | 含义 |
|---|---|
| pending | 等待处理、冷却或有限重试 |
| processing | 正在执行，持有有期限的任务占用记录 |
| watching | 已订阅，最近检查尚未进入知识库 |
| completed | 可验证的证据和返回链接已使用 wiki 地址 |
| completed_with_skips | 可验证链接已处理，部分缺失或被改动的目标已跳过 |
| failed | 无权限、结构不符或本轮重试次数耗尽，需查看错误 |

每轮默认最多 3 次尝试，每次总预算 45 秒，退避默认 30、60 秒，并遵守 Retry-After。不会降级到“无版本限制”写入。并发编辑导致版本请求失败时，下次重新读取文档。处理中进程退出后，其他进程在占用到期后接手，旧占用不能覆盖新任务结果。

重复事件不重置进行中的重试预算；失败任务收到新的文档事件后可再次尝试。日志只输出错误类型或代码。部分批次完成时，其余链接保留原地址，下一轮跳过已正确的链接。

登记上限为 5000 篇；单次读取上限为 10000 个块。超过时停止，不猜测缺失数据。大量登记和事件仍会增加本地 I/O 和飞书接口用量，尚未做生产负载测试。

## 验收

本地使用合成样本及 MockTransport 验证：正文、样式和评论保持；双向目标保持；手动修复过部分链接；错链与外部链接；事件验签与解密；重复事件；两进程抢占；重启恢复；权限、限频、超时和部分批次；完整报告兼容；关闭时不运行。

真实租户验收须另外执行：

1. 新导出快速报告，确认登记、订阅成功，docx 内链接可用。
2. 移入知识库，打开文档并改名，确认事件送达和状态变化。
3. 在 wiki 页内点击证据与返回链接，确认当前页跳转；覆盖多处引用和补充证据。
4. 用明确指定的旧快速报告验证种子登记。
5. 测试并发人工编辑、权限撤销和恢复，确认没有覆盖文字或错误宣告成功。

## 停用

设置 FEISHU_WIKI_AUTO_UPDATE_ENABLED=false 并重启，会停止后台更新和新登记；经过验证的事件只应答，不入队。已改好的链接保留。关闭功能不会删除记录，也不会取消可能由同一应用其他功能使用的文档订阅。

无需重新生成报告。若需修改现有记录、反向改写文档或删除目录，应另行批准并准备恢复方案，不要清空运行时目录。

## 本地交付记录（2026-09-10）

- 基线：64e232d；工作分支：codex/bugfix-feishu-wiki-navigation。
- 69 项定向 unittest 通过，覆盖证据导航、自动任务、既有飞书格式、快速报告和历史版本导出。
- Python compileall、后端分层检查、git diff --check 通过。
- 验证日志：工作树 .test-tmp/validation-final.log。DATA_DIR、素材目录和 Python 缓存均指向 .test-tmp/。
- 本次未调用真实飞书订阅或文档写接口，未配置线上事件，未部署；真实事件投递和生产负载仍未验证。
- 报告质量：合成样本确认修改仅涉及链接地址，正文、样式、评论标识和目标段落保持；真实飞书端的并发编辑、评论及格式效果仍需联调。
- 报告生成速度：没有新增模型调用，也未改生成流程。开启功能会增加本地登记写入、后台 I/O 和飞书请求，繁忙时可能争用资源；没有生产耗时数据。
- 合并判断：默认关闭的代码可进入合并审核；直接线上启用为 NO-GO，须先完成配置和真实租户验收。
- 共享文件涉及 app/main.py、app/core/config.py、飞书集成与导出编排，合并前需重新核对主干是否变化。
- 建议顺序：审核并合入关闭状态的代码、配置回调并验收、批准实际数据目录写入及文档操作后启用。停用开关可停止新任务，已经更新的链接保留。
