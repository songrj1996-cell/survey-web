# 飞书导出（OAuth 网页登录）配置

报告可一键导成飞书云文档，**文档归"登录的发起人本人"所有**（用其 user_access_token 创建）。

## 飞书开发者后台配置

1. 用与 feishu-bot 同一个企业自建应用（或新建一个自建应用）。
2. **添加「网页应用」能力**（或在「安全设置」开启网页授权 / 重定向 URL 白名单）。
3. **重定向 URL** 填：`<网页部署后的公网地址>/api/feishu/callback`
   - 生产：`https://survey.你们域名/api/feishu/callback`（飞书一般要求 https）
   - 本地联调：用 ngrok 暴露后填 ngrok 的 https 地址（飞书通常不接受 127.0.0.1）
   - 必须与 `.env` 的 `FEISHU_REDIRECT_URI` **逐字一致**
4. 开通权限（应用权限/用户身份）：
   - 获取用户 user_access_token（authen）
   - 获取用户邮箱信息（用于整站登录白名单）
   - 创建/导入云文档（drive 文件上传、import_tasks）
   - 文档块读写（docx，用于「核心结论」高亮块；缺失则自动回退为普通段落，不报错）

## .env

```
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_BASE=https://open.feishu.cn/open-apis      # 私有化部署改成你们的内网域名 + /open-apis
FEISHU_REDIRECT_URI=https://<部署域名>/api/feishu/callback
FEISHU_SCOPE=                                       # 可留空（用应用默认权限）

# 整站飞书登录（邮箱白名单）
FEISHU_LOGIN_REQUIRED=true
FEISHU_ALLOWED_EMAILS=user1@example.com,user2@example.com
FEISHU_SESSION_DAYS=7
```

> 改 `.env` 后需**重启服务**（uvicorn --reload 不会重载环境变量）。

## 整站登录模式

开启 `FEISHU_LOGIN_REQUIRED=true` 后，访问平台首页和业务接口都必须先通过飞书登录。登录成功后服务端会校验飞书返回的邮箱是否在 `FEISHU_ALLOWED_EMAILS` 中；未获取到邮箱或邮箱不在白名单都会拒绝进入平台。

浏览器只保存 7 天的 `HttpOnly` 会话 cookie（可用 `FEISHU_SESSION_DAYS` 调整）。7 天内如果 user_access_token 过期，服务端会使用 refresh_token 自动续期；服务重启后内存登录态会丢失，需要重新登录。

## 使用

1. 打开平台，点左下角「登录飞书」→ 跳飞书授权 → 回到平台（左下角显示你的名字）。
2. 生成报告后，Step 5 点「生成飞书文档」→ 文档创建在**你自己的飞书空间**，返回链接（自动复制）。

## 说明 / 排错

- 登录态存在服务端内存，重启失效（重新登录即可）；user_access_token 约 2h 过期，会用 refresh_token 自动续期，续期失败则提示重登。
- 报关于「未配置飞书应用」：检查 `FEISHU_APP_ID/SECRET/REDIRECT_URI` 三者是否都填了。
- 授权回调报 state 失效：重新点登录（state 10 分钟过期）。
- 「核心结论高亮块」是 best-effort：若 docx 块接口权限不足或失败，核心结论会以普通段落留在正文，不影响其余内容。
