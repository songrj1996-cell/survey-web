"""调研分析平台 Web 后端入口。

应用的全部装配(FastAPI 实例、中间件、静态资源、登录门控、各业务路由)都在 app/main.py。
本文件仅暴露 `app`,使部署/启动命令 `uvicorn server:app` 保持不变。

代码结构(自 6696 行单文件重构而来):
- app/core/         配置、权限判断、通用响应、审计构造、文本/文件解析
- app/storage/      本地 JSON 持久化(会话/历史/审计/设置/提示词/白名单/文案/登录态)
- app/integrations/ 外部系统封装(Dify、飞书)
- app/services/     业务编排(题型识别、报告引擎、报告渲染、评论/标注流水线、报告归历史、导出)
- app/schemas/      请求体模型
- app/routers/      HTTP 接口(survey/crosstab/comment/annotate/export/feishu/admin/settings/history)
"""
from app.main import app  # noqa: F401

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
