"""[过渡 shim] 配置已迁移至 app/core/config.py。

保留本文件仅为兼容 dify.py 的 `from config import DIFY_API_BASE, DIFY_API_KEY`。
步骤2 将 dify.py 迁入 app/integrations 并改为从 app.core.config 导入后,删除本文件。
"""
from app.core.config import *  # noqa: F401,F403
