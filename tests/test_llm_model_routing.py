"""Offline stage routing tests. Transport is fake; no credentials or network."""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.core.llm_context import bind_llm_model_snapshot
from app.core.llm_model_registry import environment_defaults, fingerprint
from app.integrations import llm_client
from app.services import llm_model_settings as settings
from tests.test_llm_client import _FakeClient, _FakeResponse, _sse


def snapshot(version=1, primary="test-primary", backup="test-backup"):
    configuration = environment_defaults()
    configuration["models"] = [
        {"id": name, "name": name, "efforts": ["default", "low", "high"],
         "context_tokens": 100000, "max_output_tokens": 12000,
         "verified": True, "verification_note": "synthetic fixture"}
        for name in (primary, backup)
    ]
    for task in configuration["tasks"].values():
        task.update(primary={"model": primary, "reasoning": "high", "max_tokens": 4096},
                    fallbacks=[{"model": backup, "reasoning": "low", "max_tokens": 2048}],
                    timeout_seconds=5, max_http_attempts=3)
    return {"version": version, "revision": "a" * 64, "configuration": configuration,
            "configuration_sha256": fingerprint(configuration)}


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.network = patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
                                    side_effect=AssertionError("network is forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    async def test_all_38_stages_use_their_own_candidate_parameters(self):
        saved = snapshot()
        collect = AsyncMock(return_value=("synthetic", "test-primary"))
        with bind_llm_model_snapshot(saved):
            for key in saved["configuration"]["tasks"]:
                await settings.call_model(collect, key, [{"role": "user", "content": "fixture"}],
                                          models=("old-model",), max_tokens=9999)
                options = collect.call_args.kwargs
                self.assertEqual(options["models"], ("test-primary", "test-backup"))
                self.assertEqual(options["model_task_key"], key)
                self.assertEqual(options["model_config_version"], 1)
                self.assertEqual(options["max_tokens"], 4096)
                self.assertEqual(options["reasoning_effort"], "high")
                self.assertEqual(options["candidate_settings"][1]["reasoning"], "low")

    async def test_unpublished_defaults_leave_existing_collector_contract_unchanged(self):
        saved = snapshot(0)
        options = {"models": ("legacy",), "max_tokens": 5555,
                   "reasoning_effort": None, "max_http_attempts": 1}
        collect = AsyncMock(return_value=("done", "legacy"))
        with bind_llm_model_snapshot(saved):
            await settings.call_model(collect, "survey.report", [], **options)
        self.assertEqual(collect.call_args.kwargs, options)

    async def test_selected_backup_gets_its_own_options(self):
        collect = AsyncMock(return_value=("done", "test-backup"))
        with bind_llm_model_snapshot(snapshot()):
            await settings.call_model(collect, "annotate.quality", [], selected_model="test-backup")
            self.assertEqual(collect.call_args.kwargs["models"], ("test-backup",))
            self.assertEqual(collect.call_args.kwargs["max_tokens"], 2048)
            self.assertEqual(collect.call_args.kwargs["reasoning_effort"], "low")
            with self.assertRaisesRegex(ValueError, "冻结配置"):
                await settings.call_model(collect, "annotate.quality", [], selected_model="absent")

    async def test_transport_fallback_sends_each_models_own_effort_and_output(self):
        client = _FakeClient([
            _FakeResponse(503, body=b"temporary failure"),
            _FakeResponse(lines=[_sse({"type": "response.output_text.delta", "delta": "ok"}),
                                 _sse({"type": "response.completed", "response": {"status": "completed"}})]),
        ])
        events = []
        with bind_llm_model_snapshot(snapshot()), \
             patch.object(llm_client.httpx, "AsyncClient", return_value=client), \
             patch.object(llm_client, "LLM_API_BASE", "https://synthetic.invalid"), \
             patch.object(llm_client, "LLM_REPORT_MAX_ATTEMPTS", 1):
            result = await settings.call_model(llm_client.collect_chat_completion, "survey.report", [],
                                               api_key="fixture-key", on_attempt_event=events.append)
        self.assertEqual(result, ("ok", "test-backup"))
        self.assertEqual([c["json"]["max_output_tokens"] for c in client.calls], [4096, 2048])
        self.assertEqual([c["json"]["reasoning"]["effort"] for c in client.calls], ["high", "low"])
        starts = [e for e in events if e["status"] == "started"]
        self.assertEqual([e["reasoning_effort"] for e in starts], ["high", "low"])
        self.assertTrue(all(e["model_config_version"] == 1 for e in events))

    async def test_schema_repairs_share_one_physical_http_budget(self):
        saved = snapshot()
        saved["configuration"]["tasks"]["survey.report"]["max_http_attempts"] = 2
        saved["configuration_sha256"] = fingerprint(saved["configuration"])
        client = _FakeClient([_FakeResponse(lines=[
            _sse({"type": "response.output_text.delta", "delta": "bad-json"}),
            _sse({"type": "response.completed", "response": {"status": "completed"}}),
        ]) for _ in range(2)])
        with bind_llm_model_snapshot(saved), settings.model_attempt_budget("survey.report"), \
             patch.object(llm_client.httpx, "AsyncClient", return_value=client), \
             patch.object(llm_client, "LLM_API_BASE", "https://synthetic.invalid"):
            for _ in range(2):
                await settings.call_model(llm_client.collect_chat_completion, "survey.report", [], api_key="fixture")
            with self.assertRaisesRegex(RuntimeError, "真实请求上限"):
                await settings.call_model(llm_client.collect_chat_completion, "survey.report", [], api_key="fixture")
        self.assertEqual(len(client.calls), 2)

    async def test_chat_protocol_honors_managed_effort_and_preserves_legacy_payload(self):
        clients = []
        for version in (0, 1):
            client = _FakeClient([_FakeResponse(lines=[
                _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}),
                "data: [DONE]",
            ])])
            with bind_llm_model_snapshot(snapshot(version)), \
                 patch.object(llm_client.httpx, "AsyncClient", return_value=client), \
                 patch.object(llm_client, "LLM_API_BASE", "https://synthetic.invalid"), \
                 patch.object(llm_client, "_protocol_order", return_value=("chat",)):
                await settings.call_model(llm_client.collect_chat_completion, "survey.report", [],
                    models=("test-primary",), max_tokens=4096, reasoning_effort="high", api_key="fixture")
            clients.append(client)
        self.assertNotIn("reasoning_effort", clients[0].calls[0]["json"])
        self.assertEqual(clients[1].calls[0]["json"]["reasoning_effort"], "high")

    async def test_capability_failure_sends_no_http_request(self):
        saved = snapshot()
        saved["configuration"]["tasks"]["survey.report"]["primary"]["max_tokens"] = 13000
        saved["configuration_sha256"] = fingerprint(saved["configuration"])
        collect = AsyncMock()
        with self.assertRaisesRegex(ValueError, "快照校验"):
            async with settings.model_operation_scope(saved):
                await settings.call_model(collect, "survey.report", [])
        collect.assert_not_awaited()

    async def test_comment_and_annotation_helpers_select_separate_stages(self):
        from app.services import comment_pipeline, annotate_workflow
        collect = AsyncMock(side_effect=RuntimeError("synthetic upstream failure"))
        with bind_llm_model_snapshot(snapshot()), \
             patch.object(comment_pipeline, "collect_chat_completion", collect):
            with self.assertRaises(RuntimeError):
                await comment_pipeline._comment_json_call(task="评论主题提取", system_prompt="fixture",
                    query="fixture", models=("old",), reasoning_effort="default", max_tokens=9999,
                    validate=lambda value: None)
        self.assertEqual({c.kwargs["model_task_key"] for c in collect.await_args_list}, {"comment.extract"})
        collect = AsyncMock(return_value=("[]", "test-primary"))
        with bind_llm_model_snapshot(snapshot()), \
             patch.object(annotate_workflow, "collect_chat_completion", collect):
            await annotate_workflow._collect_annotate_json(task="translation-fixture", system_prompt="fixture",
                user_prompt="fixture", models=("old",), reasoning_effort="default", max_tokens=9999,
                parser=lambda value: ([{"text": "translated"}], ""))
        self.assertEqual(collect.call_args.kwargs["model_task_key"], "annotate.translation")

    async def test_postwrite_stages_route_without_changing_full_input(self):
        from app.services.report_postwrite_pipeline import BoundedCaller
        request = [{"role": "user", "content": "all original evidence"}]
        collect = AsyncMock(return_value=("synthetic", "test-primary"))
        with bind_llm_model_snapshot(snapshot()):
            caller = BoundedCaller(collect=collect, max_calls=0, max_tokens=0, max_seconds=0, call_seconds=0)
            for stage, key in (("writer", "insight.writer"), ("registry", "insight.registry"),
                               ("meaning:abc", "insight.meaning"), ("quantity:abc", "insight.quantity")):
                await caller.call(stage, request)
                self.assertEqual(collect.call_args.args[0], request)
                self.assertEqual(collect.call_args.kwargs["model_task_key"], key)

    async def test_postwrite_backup_success_keeps_unknown_primary_usage_reserved(self):
        from app.services.report_postwrite_pipeline import BoundedCaller
        async def collect(messages, **options):
            event = options["on_attempt_event"]
            await event({"status": "failed", "model": "test-primary", "finish_reason": "length",
                         "usage": {"input_tokens": 100, "output_tokens": 4096}, "usage_complete": False})
            await event({"status": "completed", "model": "test-backup", "finish_reason": "stop",
                         "usage": {"input_tokens": 100, "output_tokens": 100}, "usage_complete": True})
            return "complete backup draft", "test-backup"
        with bind_llm_model_snapshot(snapshot()):
            caller = BoundedCaller(collect=collect, max_calls=0, max_tokens=0, max_seconds=0, call_seconds=0)
            output = await caller.call("writer", [{"role": "user", "content": "all sources"}])
        self.assertEqual(output, "complete backup draft")
        self.assertEqual(caller.ledger[0]["actual_model"], "test-backup")
        self.assertFalse(caller.ledger[0]["usage_complete"])
        self.assertEqual(caller.ledger[0]["usage"]["total_tokens"], 4396)
        self.assertGreaterEqual(caller.ledger[0]["charged_or_reserved_tokens"], 4096 * 3)

    async def test_timeout_cancels_collector_and_does_not_fall_back_silently(self):
        stopped = asyncio.Event()
        async def slow(*args, **kwargs):
            try:
                await asyncio.sleep(5)
            finally:
                stopped.set()
        saved = snapshot()
        with bind_llm_model_snapshot(saved), patch.object(settings.asyncio, "timeout", wraps=asyncio.timeout) as timeout:
            with self.assertRaises(TimeoutError):
                async with asyncio.timeout(.01):
                    await settings.call_model(slow, "survey.report", [])
        self.assertTrue(stopped.is_set())


if __name__ == "__main__":
    unittest.main()
