import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.integrations import llm_client
from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer


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
    def setUp(self):
        self.login_required_patch = patch.object(
            llm_client,
            "FEISHU_LOGIN_REQUIRED",
            False,
        )
        self.login_required_patch.start()

    def tearDown(self):
        self.login_required_patch.stop()

    def assert_attempt_pairs(self, events):
        started = {}
        terminal = {}
        for event in events:
            self.assertTrue(event.get("call_id"))
            self.assertIn(event.get("protocol"), {"messages", "responses", "chat"})
            call_id = event["call_id"]
            if event["status"] == "started":
                self.assertNotIn(call_id, started)
                started[call_id] = event
            else:
                self.assertIn(event["status"], {"completed", "failed"})
                self.assertIsInstance(event.get("usage_complete"), bool)
                self.assertNotIn(call_id, terminal)
                terminal[call_id] = event

        self.assertEqual(set(started), set(terminal))
        for call_id, start_event in started.items():
            terminal_event = terminal[call_id]
            for key in (
                "attempt",
                "model",
                "requested_model",
                "protocol",
                "fallback",
            ):
                self.assertEqual(start_event[key], terminal_event[key])

    async def test_cancelled_attempt_emits_terminal_failed_event(self):
        started = asyncio.Event()
        blocker = asyncio.Event()
        events = []

        async def slow_request(
            _client,
            _messages,
            _model,
            _protocol,
            _max_tokens,
            _reasoning_effort,
            observation,
            **_kwargs,
        ):
            observation.response_model = "model-a-actual"
            observation.usage = {
                "input_tokens": 7,
                "output_tokens": 0,
                "total_tokens": 7,
            }
            observation.usage_complete = False
            started.set()
            await blocker.wait()

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "_protocol_order", return_value=("chat",)),
            patch.object(llm_client, "_request_once", new=slow_request),
            patch.object(
                llm_client.httpx,
                "AsyncClient",
                return_value=_FakeClient([]),
            ),
        ):
            task = asyncio.create_task(llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("model-a",),
                on_attempt_event=events.append,
            ))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertEqual(
            [event["status"] for event in events],
            ["started", "failed"],
        )
        self.assert_attempt_pairs(events)
        self.assertEqual(events[1]["response_model"], "model-a-actual")
        self.assertEqual(events[1]["usage"], {
            "input_tokens": 7,
            "output_tokens": 0,
            "total_tokens": 7,
        })
        self.assertFalse(events[1]["usage_complete"])

    async def test_claude_uses_messages_and_retries_midstream_safely(self):
        client = _FakeClient([
            _FakeResponse(
                lines=[
                    _sse({
                        "type": "message_start",
                        "message": {
                            "model": "claude-test-actual",
                            "usage": {"input_tokens": 18, "output_tokens": 0},
                        },
                    }),
                    _sse({
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "partial"},
                    }),
                ],
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
        events = []

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
            ], on_attempt_event=events.append)

        self.assertEqual(answer, "complete answer")
        self.assertEqual(model, "claude-test")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(call["url"].endswith("/messages") for call in client.calls))
        self.assertEqual(client.calls[0]["json"]["system"], "rules")
        self.assertEqual(client.calls[0]["json"]["messages"], [
            {"role": "user", "content": "write"},
        ])
        sleep.assert_awaited_once_with(1)
        self.assertEqual(
            [event["status"] for event in events],
            ["started", "failed", "started", "completed"],
        )
        self.assert_attempt_pairs(events)
        self.assertEqual(events[1]["response_model"], "claude-test-actual")
        self.assertEqual(events[1]["usage"], {
            "input_tokens": 18,
            "output_tokens": 0,
            "total_tokens": 18,
        })
        self.assertFalse(events[1]["usage_complete"])

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

    async def test_responses_reports_actual_model_and_normalized_usage(self):
        client = _FakeClient([_FakeResponse(lines=[
            _sse({"type": "response.output_text.delta", "delta": "answer"}),
            _sse({
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "model": "gpt-test-2026-08-31",
                    "usage": {
                        "input_tokens": 12,
                        "output_tokens": 5,
                        "total_tokens": 17,
                    },
                },
            }),
        ])])
        events = []

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("gpt-test",),
                on_attempt_event=events.append,
            )

        self.assertEqual((answer, model), ("answer", "gpt-test"))
        self.assertEqual([event["status"] for event in events], [
            "started",
            "completed",
        ])
        self.assert_attempt_pairs(events)
        self.assertEqual(events[0]["protocol"], "responses")
        self.assertEqual(events[0]["attempt"], 1)
        self.assertEqual(events[1]["usage"], {
            "input_tokens": 12,
            "output_tokens": 5,
            "total_tokens": 17,
        })
        self.assertTrue(events[1]["usage_complete"])
        self.assertEqual(events[1]["response_model"], "gpt-test-2026-08-31")

    async def test_messages_combines_input_with_final_cumulative_output_usage(self):
        client = _FakeClient([_FakeResponse(lines=[
            _sse({
                "type": "message_start",
                "message": {
                    "model": "claude-test-actual",
                    "usage": {"input_tokens": 21, "output_tokens": 0},
                },
            }),
            _sse({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "answer"},
            }),
            _sse({
                "type": "message_delta",
                "delta": {},
                "usage": {"output_tokens": 3},
            }),
            _sse({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 8},
            }),
        ])])
        events = []

        async def record_event(event):
            events.append(event)

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("claude-test",),
                on_attempt_event=record_event,
            )

        self.assertEqual((answer, model), ("answer", "claude-test"))
        self.assertEqual(events[-1]["status"], "completed")
        self.assertEqual(events[-1]["response_model"], "claude-test-actual")
        self.assertEqual(events[-1]["usage"], {
            "input_tokens": 21,
            "output_tokens": 8,
            "total_tokens": 29,
        })
        self.assertTrue(events[-1]["usage_complete"])
        self.assert_attempt_pairs(events)

    async def test_chat_requests_and_reports_stream_usage(self):
        client = _FakeClient([_FakeResponse(lines=[
            _sse({
                "model": "chat-test-actual",
                "choices": [{"delta": {"content": "chat answer"}}],
            }),
            _sse({
                "model": "chat-test-actual",
                "choices": [],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 4,
                    "total_tokens": 13,
                },
            }),
            "data: [DONE]",
        ])])
        events = []

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "_protocol_order", return_value=("chat",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("chat-test",),
                on_attempt_event=events.append,
            )

        self.assertEqual((answer, model), ("chat answer", "chat-test"))
        self.assertEqual(
            client.calls[0]["json"]["stream_options"],
            {"include_usage": True},
        )
        self.assertEqual(events[-1]["response_model"], "chat-test-actual")
        self.assertEqual(events[-1]["usage"], {
            "input_tokens": 9,
            "output_tokens": 4,
            "total_tokens": 13,
        })
        self.assertTrue(events[-1]["usage_complete"])
        self.assert_attempt_pairs(events)

    async def test_chat_retries_without_usage_option_when_gateway_rejects_it(self):
        client = _FakeClient([
            _FakeResponse(
                400,
                body=b'{"error":{"message":"Unsupported parameter: stream_options.include_usage"}}',
            ),
            _FakeResponse(lines=[
                _sse({"choices": [{"delta": {"content": "compatible"}}]}),
                "data: [DONE]",
            ]),
        ])
        events = []

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "_protocol_order", return_value=("chat",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("chat-test",),
                on_attempt_event=events.append,
            )

        self.assertEqual((answer, model), ("compatible", "chat-test"))
        self.assertIn("stream_options", client.calls[0]["json"])
        self.assertNotIn("stream_options", client.calls[1]["json"])
        self.assertEqual([event["status"] for event in events], [
            "started",
            "failed",
            "started",
            "completed",
        ])
        self.assert_attempt_pairs(events)
        self.assertEqual(
            [event["attempt"] for event in events],
            [1, 1, 2, 2],
        )
        self.assertNotEqual(events[0]["call_id"], events[2]["call_id"])
        self.assertTrue(all(event["protocol"] == "chat" for event in events))
        self.assertIsNone(events[1]["usage"])
        self.assertFalse(events[1]["usage_complete"])
        self.assertIsNone(events[-1]["usage"])
        self.assertFalse(events[-1]["usage_complete"])

    async def test_claude_5_uses_adaptive_thinking_and_effort(self):
        client = _FakeClient([_FakeResponse(lines=[
            _sse({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "answer"},
            }),
            _sse({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
        ])])

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("claude-sonnet-5",),
                max_tokens=16000,
                reasoning_effort="medium",
            )

        self.assertEqual((answer, model), ("answer", "claude-sonnet-5"))
        payload = client.calls[0]["json"]
        self.assertEqual(payload["thinking"], {"type": "adaptive"})
        self.assertEqual(payload["output_config"], {"effort": "medium"})
        self.assertEqual(payload["max_tokens"], 16000)

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
        events = []

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "bad-model"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ("good-model",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "user", "content": "write"},
            ], on_attempt_event=events.append)

        self.assertEqual((answer, model), ("fallback ok", "good-model"))
        self.assertEqual(
            [call["json"]["model"] for call in client.calls],
            ["bad-model", "good-model"],
        )
        self.assertEqual(
            [event["status"] for event in events],
            ["started", "failed", "started", "completed"],
        )
        self.assert_attempt_pairs(events)
        self.assertEqual(events[1]["status"], "failed")
        self.assertEqual(events[1]["model"], "bad-model")
        self.assertEqual(events[1]["requested_model"], "bad-model")
        self.assertEqual(events[1]["protocol"], "responses")
        self.assertFalse(events[1]["fallback"])
        self.assertEqual(events[1]["attempt"], 1)
        self.assertIsNone(events[1]["usage"])
        self.assertEqual(events[2]["attempt"], 2)
        self.assertTrue(events[2]["fallback"])
        self.assertEqual(events[2]["requested_model"], "bad-model")

    async def test_missing_key_fails_before_network_call(self):
        with (
            patch.object(llm_client, "LLM_API_KEY", ""),
            patch.object(llm_client.httpx, "AsyncClient") as client,
        ):
            with self.assertRaisesRegex(RuntimeError, "LLM API Key"):
                await llm_client.collect_chat_completion([])
        client.assert_not_called()

    async def test_production_never_falls_back_to_platform_key(self):
        with (
            patch.object(llm_client, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(llm_client, "LLM_API_KEY", "platform-secret"),
            patch.object(llm_client.httpx, "AsyncClient") as client,
        ):
            with self.assertRaisesRegex(RuntimeError, "当前用户"):
                await llm_client.collect_chat_completion([])
        client.assert_not_called()

    async def test_concurrent_contexts_keep_user_keys_isolated(self):
        captured = []

        async def fake_request_once(
            _client,
            _messages,
            _model,
            _protocol,
            _max_tokens,
            _reasoning_effort,
            _observation,
            *,
            api_key,
            **_kwargs,
        ):
            captured.append(api_key)
            await asyncio.sleep(0)
            return llm_client._LLMResult(answer=f"answer:{api_key}")

        async def invoke(key):
            with bind_llm_api_key(key):
                return await llm_client.collect_chat_completion(
                    [{"role": "user", "content": "question"}],
                    models=("model-a",),
                )

        with (
            patch.object(llm_client, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(llm_client, "_protocol_order", return_value=("chat",)),
            patch.object(llm_client, "_request_once", new=fake_request_once),
            patch.object(
                llm_client.httpx,
                "AsyncClient",
                return_value=_FakeClient([]),
            ),
        ):
            results = await asyncio.gather(invoke("user-key-a"), invoke("user-key-b"))

        self.assertCountEqual(captured, ["user-key-a", "user-key-b"])
        self.assertCountEqual(
            [result[0] for result in results],
            ["answer:user-key-a", "answer:user-key-b"],
        )

    async def test_context_usage_observer_and_explicit_callback_both_receive_events(self):
        context_events = []
        explicit_events = []

        async def fake_request_once(*_args, **_kwargs):
            return llm_client._LLMResult(
                answer="done",
                response_model="actual-model",
                usage={"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                usage_complete=True,
            )

        with (
            bind_llm_attempt_observer(context_events.append),
            patch.object(llm_client, "_protocol_order", return_value=("chat",)),
            patch.object(llm_client, "_request_once", new=fake_request_once),
            patch.object(
                llm_client.httpx,
                "AsyncClient",
                return_value=_FakeClient([]),
            ),
        ):
            result = await llm_client.collect_chat_completion(
                [{"role": "user", "content": "question"}],
                models=("requested-model",),
                api_key="personal-key",
                on_attempt_event=explicit_events.append,
            )

        self.assertEqual(result, ("done", "requested-model"))
        self.assertEqual(
            [event["status"] for event in context_events],
            ["started", "completed"],
        )
        self.assertEqual(explicit_events, context_events)
        self.assertEqual(context_events[-1]["usage"]["total_tokens"], 10)
        self.assertEqual(context_events[-1]["response_model"], "actual-model")

    async def test_explicit_user_key_sets_header_and_is_redacted_from_errors(self):
        secret = "personal-user-secret"
        client = _FakeClient([
            _FakeResponse(
                401,
                body=f'{{"error":"rejected {secret}"}}'.encode(),
            ),
        ])
        with (
            patch.object(llm_client, "FEISHU_LOGIN_REQUIRED", True),
            patch.object(llm_client, "_protocol_order", return_value=("chat",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            with self.assertRaises(RuntimeError) as caught:
                await llm_client.collect_chat_completion(
                    [{"role": "user", "content": "question"}],
                    models=("chat-test",),
                    api_key=secret,
                )

        self.assertEqual(
            client.calls[0]["headers"]["Authorization"],
            f"Bearer {secret}",
        )
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("***", str(caught.exception))

    async def test_truncated_primary_output_is_rejected_and_falls_back(self):
        client = _FakeClient([
            _FakeResponse(lines=[
                _sse({
                    "type": "message_start",
                    "message": {
                        "model": "claude-short-actual",
                        "usage": {"input_tokens": 30, "output_tokens": 0},
                    },
                }),
                _sse({
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "incomplete"},
                }),
                _sse({
                    "type": "message_delta",
                    "delta": {"stop_reason": "max_tokens"},
                    "usage": {"output_tokens": 10},
                }),
            ]),
            _FakeResponse(lines=[
                _sse({"type": "response.output_text.delta", "delta": "complete"}),
                _sse({"type": "response.completed", "response": {"status": "completed"}}),
            ]),
        ])
        events = []

        with (
            patch.object(llm_client, "LLM_API_BASE", "https://llm.example/v1"),
            patch.object(llm_client, "LLM_API_KEY", "secret"),
            patch.object(llm_client, "LLM_REPORT_MODEL", "claude-short"),
            patch.object(llm_client, "LLM_REPORT_FALLBACK_MODELS", ("gpt-long",)),
            patch.object(llm_client.httpx, "AsyncClient", return_value=client),
        ):
            answer, model = await llm_client.collect_chat_completion([
                {"role": "user", "content": "write"},
            ], on_attempt_event=events.append)

        self.assertEqual((answer, model), ("complete", "gpt-long"))
        self.assertEqual(
            [event["status"] for event in events],
            ["started", "failed", "started", "completed"],
        )
        self.assert_attempt_pairs(events)
        self.assertEqual(events[1]["response_model"], "claude-short-actual")
        self.assertEqual(events[1]["usage"], {
            "input_tokens": 30,
            "output_tokens": 10,
            "total_tokens": 40,
        })
        self.assertTrue(events[1]["usage_complete"])


if __name__ == "__main__":
    unittest.main()
