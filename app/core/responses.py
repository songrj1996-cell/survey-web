"""core/responses:通用响应工具。

目前提供 SSE event 的统一格式化;后续可收纳文件下载响应、常见错误响应等。
"""
import json


def sse_event(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
