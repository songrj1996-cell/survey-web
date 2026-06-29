"""services:业务流程编排。

边界:编排流程,调用 storage 读写数据、调用 integrations 访问外部系统;不依赖 FastAPI
路由对象、不处理前端 DOM、不硬编码文件路径(路径来自 core/config)。
"""
