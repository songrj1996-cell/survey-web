"""调研分析平台后端应用包。

分层(依赖方向 routers → services → storage / integrations / core):
- core:         纯横切能力(配置、权限判断、通用响应、审计构造、文件解析)
- storage:      本地 JSON 持久化
- integrations: 外部系统封装(Dify、飞书)
- services:     业务流程编排
- schemas:      请求/响应数据结构
- routers:      HTTP 接口(每条业务线一个)
"""
