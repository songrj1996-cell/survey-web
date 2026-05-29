"""survey-web 专用配置，仅加载 Dify 相关环境变量。"""
import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DIFY_API_BASE = os.getenv("DIFY_API_BASE", "https://api.dify.ai/v1").rstrip("/")
DIFY_API_KEY  = os.getenv("DIFY_API_KEY", "")         # dify.py 的 fallback 变量
DIFY_PLANNER_KEY = os.getenv("DIFY_PLANNER_KEY", "")
DIFY_ANALYST_KEY = os.getenv("DIFY_ANALYST_KEY", "")
