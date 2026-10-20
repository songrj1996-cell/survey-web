import asyncio
import io
import logging
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from starlette.requests import Request

from app.core.config import COOKIE_NAME
from app.core.runtime_logging import HumanAccessFormatter, runtime_log
from app.services import auth, request_logging


def _scope(*, user: str = "", cookie: str = "") -> dict:
    headers = []
    if user:
        headers.append((b"x-test-user", user.encode("utf-8")))
    if cookie:
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/history",
        "raw_path": b"/api/history",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 18082),
        "state": {},
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


class RequestLoggingContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_access_log_at_response_headers_distinguishes_mixed_plan_endpoint(self):
        for content_type in (b"application/json", b"text/event-stream; charset=utf-8"):
            with self.subTest(content_type=content_type):
                scope = _scope()
                scope.update(path="/api/plan/confirm", method="POST")
                messages, lines = [], []

                async def app(_scope, _receive, send):
                    await send({"type": "http.response.start", "status": 200,
                                "headers": [(b"content-type", content_type)]})
                    await send({"type": "http.response.body", "body": b"unchanged"})

                async def capture(message):
                    messages.append(message)
                    if message["type"] == "http.response.start":
                        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                            '%s - "%s %s HTTP/%s" %d',
                            ("127.0.0.1:50000", "POST", "/api/plan/confirm", "1.1", 200), None)
                        lines.append(HumanAccessFormatter().format(record))

                with patch.object(request_logging, "_login_for_request_logging",
                                  return_value={"name": "合成用户", "email": "private@example.test"}):
                    await request_logging.RequestLoggingContextMiddleware(app)(scope, _receive, capture)
                self.assertEqual(messages[-1]["body"], b"unchanged")
                self.assertIn("操作人：合成用户", lines[0])
                self.assertNotIn("private@example.test", lines[0])
                self.assertIn("调整分析方案：结果传输连接已建立" if content_type.startswith(b"text/event-stream")
                              else "确认分析方案完成", lines[0])

    async def test_metadata_failure_does_not_change_http_response(self):
        sent = []
        async def app(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"original"})
        async def capture(message):
            sent.append(message)
        with patch.object(request_logging, "_login_for_request_logging", return_value=None), \
             patch.object(request_logging, "register_access_operator", side_effect=[None, RuntimeError("output failed")]):
            await request_logging.RequestLoggingContextMiddleware(app)(_scope(), _receive, capture)
        self.assertEqual([item["type"] for item in sent], ["http.response.start", "http.response.body"])
        self.assertEqual(sent[-1]["body"], b"original")

    def test_request_logging_wraps_authentication_guard(self):
        from app.main import app

        self.assertIs(app.user_middleware[0].cls, request_logging.RequestLoggingContextMiddleware)

    async def test_concurrent_users_never_share_operator_context(self):
        ready = 0
        ready_lock = asyncio.Lock()
        release = asyncio.Event()

        async def app(scope, receive, send):
            nonlocal ready
            async with ready_lock:
                ready += 1
                if ready == 2:
                    release.set()
            await release.wait()
            user = dict(scope["headers"])[b"x-test-user"].decode("utf-8")
            runtime_log("并发测试", "info", f"{user} 的请求正在处理")
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = request_logging.RequestLoggingContextMiddleware(app)

        def login_for_request(request):
            user = request.headers["x-test-user"]
            return {"name": user, "email": f"{user.lower()}@example.com"}

        output = io.StringIO()
        with patch.object(
            request_logging,
            "_login_for_request_logging",
            side_effect=login_for_request,
        ), redirect_stdout(output):
            await asyncio.gather(
                middleware(_scope(user="Alice"), _receive, lambda _message: _async_none()),
                middleware(_scope(user="Bob"), _receive, lambda _message: _async_none()),
            )

        lines = output.getvalue().splitlines()
        alice_line = next(line for line in lines if "Alice 的请求" in line)
        bob_line = next(line for line in lines if "Bob 的请求" in line)
        self.assertIn("操作人：Alice", alice_line)
        self.assertNotIn("Bob", alice_line)
        self.assertNotIn("example.com", alice_line)
        self.assertIn("操作人：Bob", bob_line)
        self.assertNotIn("Alice", bob_line)
        self.assertNotIn("example.com", bob_line)

    async def test_streaming_and_access_logs_keep_operator_until_response_finishes(self):
        async def app(_scope_value, _receive_value, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await asyncio.sleep(0)
            runtime_log("流式报告", "progress", "正在继续输出报告")
            record = logging.LogRecord(
                "uvicorn.access",
                logging.INFO,
                "",
                0,
                '%s - "%s %s HTTP/%s" %d',
                ("127.0.0.1:50000", "GET", "/api/history", "1.1", 200),
                None,
            )
            print(HumanAccessFormatter().format(record))
            await send({"type": "http.response.body", "body": b"done"})

        middleware = request_logging.RequestLoggingContextMiddleware(app)
        output = io.StringIO()
        with patch.object(
            request_logging,
            "_login_for_request_logging",
            return_value={"name": "流式用户", "email": "stream@example.com"},
        ), redirect_stdout(output):
            await middleware(_scope(), _receive, lambda _message: _async_none())
            runtime_log("流式报告", "info", "请求结束后的后台状态")

        lines = output.getvalue().splitlines()
        self.assertIn("操作人：流式用户", lines[0])
        self.assertIn("操作人：流式用户", lines[1])
        self.assertNotIn("example.com", "\n".join(lines))
        self.assertIn("操作人：系统任务", lines[2])

    async def test_login_candidate_is_read_once_per_request(self):
        sid = "test-session"
        login = {
            "name": "缓存测试",
            "email": "cache@example.com",
            "expires_at": time.time() + 3600,
            "exp": time.time() + 3600,
        }
        request = Request(_scope(cookie=sid))
        with patch.dict(auth.web_logins, {sid: login}, clear=True), patch.object(
            auth,
            "_sync_web_logins_from_disk",
        ) as sync:
            logging_login = auth._login_for_request_logging(request)
            authenticated_login = await auth._current_login(request)

        self.assertIs(logging_login, login)
        self.assertIs(authenticated_login, login)
        sync.assert_called_once_with()


async def _async_none():
    return None


if __name__ == "__main__":
    unittest.main()
