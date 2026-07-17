import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.integrations.dify_client import sse_dify_stream


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}"


class _FakeResponse:
    def __init__(self, status_code, *, body=b"", lines=(), stream_error=None, headers=None):
        self.status_code = status_code
        self.body = body
        self.lines = list(lines)
        self.stream_error = stream_error
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aread(self):
        return self.body

    async def aiter_lines(self):
        for line in self.lines:
            yield line
        if self.stream_error:
            raise self.stream_error


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.stream_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, *args, **kwargs):
        response = self.responses[self.stream_calls]
        self.stream_calls += 1
        return response


async def _collect(client, *, max_attempts=4):
    items = []
    with patch("app.integrations.dify_client.httpx.AsyncClient", return_value=client):
        async for item in sse_dify_stream(
            "query", "user", "", "key", max_attempts=max_attempts
        ):
            items.append(item)
    return items


class DifyStreamRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_503_before_stream_output(self):
        client = _FakeClient([
            _FakeResponse(503, body=b"temporarily unavailable"),
            _FakeResponse(200, lines=[
                _sse({"event": "message", "answer": "ok", "conversation_id": "conv-1"}),
                _sse({"event": "message_end", "conversation_id": "conv-1"}),
            ]),
        ])

        with patch(
            "app.integrations.dify_client.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            items = await _collect(client)

        self.assertEqual(items, [("ok", ""), ("", "conv-1")])
        self.assertEqual(client.stream_calls, 2)
        sleep.assert_awaited_once_with(2)

    async def test_does_not_retry_non_retryable_4xx(self):
        client = _FakeClient([_FakeResponse(401, body=b"unauthorized")])

        with patch(
            "app.integrations.dify_client.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "Dify 401"):
                await _collect(client)

        self.assertEqual(client.stream_calls, 1)
        sleep.assert_not_awaited()

    async def test_does_not_retry_after_stream_output_started(self):
        error = httpx.ReadError("connection lost")
        client = _FakeClient([
            _FakeResponse(
                200,
                lines=[_sse({"event": "message", "answer": "partial"})],
                stream_error=error,
            ),
        ])

        with patch(
            "app.integrations.dify_client.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            with self.assertRaises(httpx.ReadError):
                await _collect(client)

        self.assertEqual(client.stream_calls, 1)
        sleep.assert_not_awaited()

    async def test_retries_transient_model_error_event_before_answer(self):
        client = _FakeClient([
            _FakeResponse(200, lines=[
                _sse({
                    "event": "error",
                    "code": "completion_request_error",
                    "message": "BedrockException: ModelUnavailable code 500",
                }),
            ]),
            _FakeResponse(200, lines=[
                _sse({"event": "message", "answer": "ok", "conversation_id": "conv-2"}),
                _sse({"event": "message_end", "conversation_id": "conv-2"}),
            ]),
        ])

        with patch(
            "app.integrations.dify_client.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            items = await _collect(client, max_attempts=3)

        self.assertEqual(items, [("ok", ""), ("", "conv-2")])
        self.assertEqual(client.stream_calls, 2)
        sleep.assert_awaited_once_with(2)

    async def test_does_not_retry_non_transient_model_error_event(self):
        client = _FakeClient([_FakeResponse(200, lines=[
            _sse({
                "event": "error",
                "code": "invalid_param",
                "message": "model configuration is invalid",
            }),
        ])])

        with patch(
            "app.integrations.dify_client.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            with self.assertRaisesRegex(RuntimeError, "invalid_param"):
                await _collect(client, max_attempts=3)

        self.assertEqual(client.stream_calls, 1)
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
