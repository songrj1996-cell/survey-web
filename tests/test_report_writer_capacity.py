"""Offline capacity advice: boundaries, uncertainty, full output reserve and privacy."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from app.core import config
from app.core.llm_model_registry import estimate_candidate_capacity, capacity_estimate_metadata, fingerprint
from app.services.report_writer_capacity import estimate_writer_capacity


class CapacityTests(unittest.TestCase):
    def test_candidate_advice_boundaries_unknown_input_and_safe_metadata(self):
        request = [{"role": "user", "content": "合成材料" * 100}]
        original = deepcopy(request)
        with patch.object(config, "POSTWRITE_WRITER_WARNING_PERCENT", 80):
            # The complete input estimates 600 + 2048; reserve another 2048.
            for context, status in [(10000, "sufficient"), (5500, "near_limit"), (4696, "over_limit"), (None, "unknown")]:
                with self.subTest(context=context):
                    advice = estimate_candidate_capacity(request, model="backup", context_tokens=context, output_tokens=2048)
                    self.assertEqual(advice["status"], status)
                    self.assertEqual(advice["output_reserved"], 2048)
                    poisoned = {**advice, "message": "PRIVATE_MATERIAL", "impact": "PRIVATE_KEY", "extra": "PRIVATE"}
                    self.assertNotIn("PRIVATE", json.dumps(capacity_estimate_metadata(poisoned)))
                    self.assertEqual(capacity_estimate_metadata(poisoned), advice)
            for unsupported in ([{"role": "user", "content": [{"type": "image_url"}]}],
                                [{"role": "assistant", "content": "", "tool_calls": [{"arguments": "PRIVATE"}]}]):
                advice = estimate_candidate_capacity(unsupported, model="backup", context_tokens=10000, output_tokens=2048)
                self.assertEqual(advice["status"], "unknown")
                self.assertEqual(advice["reason"], "unsupported_input")
        self.assertEqual(request, original)
        self.assertEqual(capacity_estimate_metadata({"model": "PRIVATE\nKEY", "output_reserved": 2048}), {})

    def evaluate(self, tokens, *, output=64000, limits=None, warning=80.0, model="claude-opus-5-5"):
        with patch.object(config, "POSTWRITE_WRITER_CONTEXT_LIMITS", limits if limits is not None else
                          json.dumps({"claude-opus-5-5": 1000000})), \
                patch.object(config, "POSTWRITE_WRITER_WARNING_PERCENT", warning):
            return estimate_writer_capacity([{"role": "user", "content": "合成材料"}], model=model,
                                            output_tokens=output, estimate_input=lambda request: tokens)

    def test_boundaries_include_entire_output_reserve(self):
        for tokens, status, percent in [(736000, "sufficient", 80), (736001, "near_limit", 80.1),
                                        (856000, "near_limit", 92), (936000, "over_limit", 100),
                                        (1016000, "over_limit", 108)]:
            with self.subTest(tokens=tokens):
                result = self.evaluate(tokens)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["occupancy_percent"], percent)
                self.assertEqual(result["required_estimate"], tokens + 64000)
                self.assertEqual(result["output_reserved"], 64000)
                self.assertNotIn("保证", result["impact"])

    def test_request_is_passed_whole_and_is_not_changed(self):
        request = [{"role": "system", "content": "完整写作要求"},
                   {"role": "user", "content": json.dumps({"background": "背景", "questions": ["题目"],
                       "raw_answers": ["全部原文"], "statistics": {"players": 120}, "instruction": "补充要求"})}]
        before, seen = deepcopy(request), []
        def count(value):
            seen.append(deepcopy(value))
            return 425072
        result = estimate_writer_capacity(request, model=config.LLM_POSTWRITE_MODEL,
                                          output_tokens=64000, estimate_input=count)
        self.assertEqual(request, before)
        self.assertEqual(seen, [before])
        self.assertEqual(result["required_estimate"], 489072)
        self.assertNotIn("全部原文", json.dumps(result, ensure_ascii=False))

    def test_invalid_configuration_and_unknown_model_are_not_zero_usage(self):
        for limits in ("{bad json", "[]", '{"claude-opus-5-5":0}', '{"claude-opus-5-5":true}',
                       '{"claude-opus-5-5":"1000000"}', "{}"):
            with self.subTest(limits=limits):
                result = self.evaluate(100, limits=limits)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["occupancy_percent"])
                self.assertIn("继续生成", result["impact"])
        self.assertEqual(self.evaluate(100, model="different-model")["reason"], "unknown_model")
        for warning in (0, 100, float("nan"), float("inf"), "80", True):
            self.assertEqual(self.evaluate(100, warning=warning)["status"], "unknown")

    def test_invalid_count_or_output_does_not_raise_or_disclose(self):
        for tokens in (-1, None, True, 12.5, float("inf")):
            self.assertEqual(self.evaluate(tokens)["status"], "unknown")
        for output in (0, -1, True, None):
            self.assertEqual(self.evaluate(100, output=output)["status"], "unknown")
        def failed(request):
            raise RuntimeError("PRIVATE_ANSWER_AND_KEY")
        result = estimate_writer_capacity([], model=config.LLM_POSTWRITE_MODEL,
                                          output_tokens=64000, estimate_input=failed)
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_rounding_does_not_call_an_under_limit_request_over_limit(self):
        result = self.evaluate(935999)
        self.assertEqual(result["status"], "near_limit")
        self.assertIn("接近100%", result["message"])


class CandidateCapacityFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_writer_switch_updates_progress_checkpoint_and_keeps_requests_intact(self):
        import asyncio
        from app.core.llm_context import bind_llm_model_snapshot
        from app.integrations import llm_client
        from app.services.report_postwrite_audit import messages
        from app.services.report_postwrite_inputs import freeze_inputs
        from app.services.report_postwrite_pipeline import run_pipeline
        from tests.test_llm_model_routing import snapshot
        from tests.test_llm_client import _FakeClient, _FakeResponse, _sse
        from tests.test_report_postwrite_flow import PROMPTS, REPORT, fake_collect, session

        for context, outcome in [(4096, "success"), (None, "success"), (4096, "failed"),
                                 (None, "cancelled"), (4096, "progress_error")]:
            with self.subTest(context=context, outcome=outcome):
                saved = snapshot()
                saved["configuration"]["models"][1]["context_tokens"] = context
                saved["configuration_sha256"] = fingerprint(saved["configuration"])
                frozen = freeze_inputs(session())
                expected = messages(PROMPTS["writer"], {**frozen["postwrite_packet"], "instruction": "完整材料"}, "")
                terminal = (_FakeResponse(503, body=b"synthetic failure") if outcome == "failed" else
                            _FakeResponse(stream_error=asyncio.CancelledError()) if outcome == "cancelled" else
                            _FakeResponse(lines=[_sse({"type": "response.output_text.delta", "delta": REPORT}),
                                                _sse({"type": "response.completed", "response": {"status": "completed"}})]))
                client = _FakeClient([_FakeResponse(503, body=b"synthetic primary failure"), terminal])
                checkpoints, progress_events, attempt_events, timeline, audit_calls = [], [], [], [], []
                async def checkpoint(value):
                    checkpoints.append(deepcopy(value))
                    if value.get("writer_capacity", {}).get("model") == "test-backup":
                        timeline.append("backup_checkpoint")
                async def progress(value):
                    progress_events.append(deepcopy(value))
                    if value.get("writer_capacity", {}).get("model") == "test-backup":
                        timeline.append("backup_progress")
                        if outcome == "progress_error":
                            raise OSError("synthetic progress failure")
                stream = client.stream
                def observe_send(*args, **kwargs):
                    if kwargs["json"]["model"] == "test-backup":
                        timeline.append("backup_http")
                    return stream(*args, **kwargs)
                client.stream = observe_send
                async def collect(request, **kwargs):
                    if kwargs["model_task_key"] == "insight.writer":
                        return await llm_client.collect_chat_completion(request, api_key="fixture-key", **kwargs)
                    audit_calls.append(kwargs["model_task_key"])
                    return await fake_collect(request, **{**kwargs, "models": ["fixture"], "max_http_attempts": 1})
                with bind_llm_model_snapshot(saved), \
                     patch.object(llm_client.httpx, "AsyncClient", return_value=client), \
                     patch.object(llm_client, "LLM_API_BASE", "https://synthetic.invalid"), \
                     patch.object(llm_client, "LLM_REPORT_MAX_ATTEMPTS", 1):
                    pending = run_pipeline(frozen, prompts=PROMPTS, instruction="完整材料", collect=collect,
                        on_checkpoint=checkpoint, on_progress=progress, on_event=attempt_events.append,
                        caller_options={"max_calls": 0, "max_tokens": 0, "max_seconds": 0, "call_seconds": 0})
                    result = await pending
                    if outcome in {"success", "progress_error"}:
                        self.assertEqual(result["draft"], REPORT)
                        self.assertEqual(result["audit"]["writer_capacity"], result["writer_capacity"])
                        self.assertEqual(audit_calls, ["insight.registry", "insight.meaning"])
                    else:
                        self.assertEqual(result["calls"][0]["state"], "cancelled" if outcome == "cancelled" else "failed")
                        self.assertEqual(result["draft"], "")
                        self.assertEqual(audit_calls, [])
                expected_status = "unknown" if context is None else "over_limit"
                capacity = checkpoints[-1]["writer_capacity"]
                self.assertEqual(capacity["model"], "test-backup")
                self.assertEqual(capacity["output_reserved"], 2048)
                self.assertEqual(capacity["status"], expected_status)
                self.assertEqual(checkpoints[-1]["calls"][0]["events"][-1]["capacity_estimate"], capacity)
                self.assertEqual(len(client.calls), 2)
                for request in client.calls:
                    self.assertEqual(request["json"]["input"], expected[1:])
                    self.assertEqual(request["json"]["instructions"], expected[0]["content"])
                self.assertLess(timeline.index("backup_checkpoint"), timeline.index("backup_http"))
                self.assertLess(timeline.index("backup_progress"), timeline.index("backup_http"))
                writer_events = [e for e in attempt_events if e.get("model_task_key") == "insight.writer"]
                self.assertEqual([e["status"] for e in writer_events], ["started", "failed", "started",
                    "completed" if outcome in {"success", "progress_error"} else "failed"])


if __name__ == "__main__":
    unittest.main()
