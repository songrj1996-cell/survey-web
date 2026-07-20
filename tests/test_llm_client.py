import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.integrations import llm_client


def _sse(data: dict) -> str:
    return f"data: {json.dumps(data)}"


class _FakeResponse:
    def __init__(self, status_code=200, *, body=b"", lines=(), stream_error=None):
        self.status_code = status_code
        self.body = body
        self.lines = list(lines)
        self.stream_error = stream_error

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
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses[len(self.calls) - 1]


class DirectLLMClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_uses_messages_and_retries_midstream_safely(self):
        client = _FakeClient([
            _FakeResponse(
                lines=[_sse({
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "partial"},
                })],
                stream_error=httpx.ReadError("connection lost"),
            ),
            _FakeResponse(lines=[
                _sse({
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "complete "},
                }),
                _sse({
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "answer"},
                }),
                _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
            ]),
        ])

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "claude-test"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ()),
            patch.object(llm_client, "LLM_REPORT_MAX_ATTEMPTS", 2),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
            patch.object(llm_client.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "write"},
            ])

        self.assertEqual(answer, "complete answer")
        self.assertEqual(model, "claude-test")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["url"].endswith("/messages") for call in client.calls))
        self.assertEqual(client.calls[0]["json"]["system"], "rules")
        self.assertEqual(client.calls[0]["json"]["messages"], [
            {"role": "user", "content": "write"},
        ])
        sleep.assert_awaited_once_with(1)

    async def test_gpt_uses_responses_protocol(self):
        client = _FakeClient([_FakeResponse(lines=[
            _sse({"type": "response.output_text.delta", "delta": "response "}),
            _sse({"type": "response.output_text.delta", "delta": "ok"}),
            _sse({"type": "response.completed", "response": {"status": "completed"}}),
        ])])

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "gpt-test"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ()),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "system", "content": "rules"},
                {"role": "user", "content": "write"},
            ])

        self.assertEqual((answer, model), ("response ok", "gpt-test"))
        self.assertTrue(client.calls[0]["url"].endswith("/responses"))
        self.assertEqual(client.calls[0]["json"]["instructions"], "rules")

    async def test_incompatible_protocol_switches_before_model_fallback(self):
        incompatible = (
            b'{"error":{"message":"no model_info.compatible[\\"/messages\\"] '
            b'upstream configured for endpoint"}}'
        )
        client = _FakeClient([
            _FakeResponse(500, body=incompatible),
            _FakeResponse(lines=[
                _sse({"type": "response.output_text.delta", "delta": "same model ok"}),
                _sse({"type": "response.completed", "response": {"status": "completed"}}),
            ]),
        ])

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "claude-test"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ("gpt-fallback",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "user", "content": "write"},
            ])

        self.assertEqual((answer, model), ("same model ok", "claude-test"))
        self.assertTrue(client.calls[0]["url"].endswith("/messages"))
        self.assertTrue(client.calls[1]["url"].endswith("/responses"))

    async def test_invalid_primary_model_switches_to_configured_fallback(self):
        client = _FakeClient([
            _FakeResponse(400, body=b'{"error":{"message":"unknown model"}}'),
            _FakeResponse(lines=[
                _sse({"type": "response.output_text.delta", "delta": "fallback ok"}),
                _sse({"type": "response.completed", "response": {"status": "completed"}}),
            ]),
        ])

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "bad-model"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ("good-model",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "user", "content": "write"},
            ])

        self.assertEqual((answer, model), ("fallback ok", "good-model"))
        self.assertEqual(
            [call["json"]["model"] for call in client.calls],
            ["bad-model", "good-model"],
        )

    async def test_missing_key_fails_before_network_call(self):
        with (
            patch.object(llm_client, "LLM_API_KEY", ""),
            patch.object(llm_client.httpx, "AsyncClient") as client,
        ):
            with self.assertRaisesRegex(RuntimeError, "LLM_API_KEY"):
                await llm_client.collect_chat_completion([])
        client.assert_not_called()

    async def test_truncated_primary_output_is_rejected_and_falls_back(self):
        client = _FakeClient([
            _FakeResponse(lines=[
                _sse({
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "incomplete"},
                }),
                _sse({"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}),
            ]),
            _FakeResponse(lines=[
                _sse({"type": "response.output_text.delta", "delta": "complete"}),
                _sse({"type": "response.completed", "response": {"status": "completed"}}),
            ]),
        ])

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "claude-short"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ("gpt-long",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "user", "content": "write"},
            ])

        self.assertEqual((answer, model), ("complete", "gpt-long"))


if __name__ == "__main__":
    unittest.main()
