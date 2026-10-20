"""为一次 HTTP 请求中的全部日志绑定同一个操作人。"""
from __future__ import annotations

from starlette.requests import Request

from app.core.runtime_logging import (
    bind_runtime_operator,
    register_access_operator,
    reset_runtime_operator,
    runtime_log,
    runtime_operator_from_login,
)
from app.services.auth import _login_for_request_logging


class RequestLoggingContextMiddleware:
    """纯 ASGI 中间件：覆盖普通响应、流式响应和 Uvicorn access log。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        try:
            login = _login_for_request_logging(request)
            operator = runtime_operator_from_login(login)
        except Exception as exc:  # 身份日志失败不能阻断业务请求
            operator = "身份无法识别"
            token = bind_runtime_operator(operator)
            try:
                runtime_log(
                    "请求身份", "warning", "读取本次请求的操作人失败，业务请求将继续",
                    error=exc,
                )
            finally:
                reset_runtime_operator(token)

        token = bind_runtime_operator(operator)
        register_access_operator(scope.get("client"), operator)
        async def send_with_log_context(message):
            if message.get("type") == "http.response.start":
                try:
                    headers = dict(message.get("headers") or [])
                    content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
                    register_access_operator(
                        scope.get("client"), operator,
                        streaming=content_type == b"text/event-stream",
                    )
                except Exception:
                    pass  # Logging metadata must never affect the HTTP response.
            await send(message)
        try:
            await self.app(scope, receive, send_with_log_context)
        finally:
            reset_runtime_operator(token)
