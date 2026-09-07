import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services import survey_service


def _payloads(events: list[str]) -> list[dict]:
    return [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event.startswith("data: ")
    ]


class ReportLLMUsageTrackerTests(unittest.IsolatedAsyncioTestCase):
    def test_writer_attempt_logs_correlate_runs_without_changing_usage(self):
        tracker = survey_service._ReportLLMUsageTracker()
        record = survey_service._report_writer_attempt_callback(
            tracker.callback("writing"), session_id="session-a", run_id="run-a",
            phase="writing", step="part", part_index=2,
        )
        base = {
            "request_id": "request-a", "call_id": "call-a", "attempt": 1,
            "model": "model-a", "protocol": "messages", "fallback": False,
            "attempt_kind": "initial", "max_output_tokens": 16000,
        }
        with patch("builtins.print") as log:
            record({"status": "started", **base})
            record({
                "status": "failed", **base, "elapsed_seconds": 188.25,
                "http_status": 200, "error_type": "_LLMRequestError",
                "error_category": "output_truncated", "finish_reason": "max_tokens",
                "usage": {"input_tokens": 100, "output_tokens": 16000, "total_tokens": 16100},
                "usage_complete": True,
            })
        entries = [json.loads(call.args[0].split(" ", 1)[1]) for call in log.call_args_list]
        self.assertEqual([e["status"] for e in entries], ["started", "failed"])
        self.assertTrue(all(call.kwargs["flush"] for call in log.call_args_list))
        self.assertEqual(entries[-1]["session_id"], "session-a")
        self.assertEqual(entries[-1]["run_id"], "run-a")
        self.assertEqual(entries[-1]["step"], "part")
        self.assertEqual(entries[-1]["part_index"], 2)
        self.assertEqual(entries[-1]["elapsed_seconds"], 188.25)
        self.assertEqual(entries[-1]["error_category"], "output_truncated")
        self.assertEqual(entries[-1]["usage"]["output_tokens"], 16000)
        self.assertEqual(tracker.snapshot()["totals"]["call_count"], 1)
        self.assertEqual(tracker.snapshot()["totals"]["total_tokens"], 16100)
        self.assertNotIn("error_category", json.dumps(tracker.snapshot()))

    def test_writer_attempt_logs_exclude_secrets_and_untrusted_content(self):
        record = survey_service._report_writer_attempt_callback(
            lambda _event: None, session_id="session-a", run_id="run-a",
            phase="writing", step="part", part_index=1,
        )
        with (
            patch.object(survey_service, "current_llm_api_key", return_value="user-secret"),
            patch.object(survey_service, "LLM_API_KEY", "service-secret"),
            patch("builtins.print") as log,
        ):
            record({
                "status": "failed", "call_id": "call-a", "model": "model-a",
                "requested_model": "route-service-secret",
                "response_model": "route-user-secret",
                "error_category": "read_timeout", "error_type": "ReadTimeout",
                "finish_reason": "private\nanswer", "elapsed_seconds": float("nan"),
                "usage": {"input_tokens": "private", "output_tokens": 3, "total_tokens": 3,
                          "raw": "private prompt"},
                "error": "private prompt", "messages": "private prompt",
                "answer": "private answer", "headers": {"Authorization": "user-secret"},
            })
        raw = log.call_args.args[0]
        for secret in ("user-secret", "service-secret", "private", "Authorization"):
            self.assertNotIn(secret, raw)
        entry = json.loads(raw.split(" ", 1)[1])
        self.assertEqual(entry["response_model"], "redacted")
        self.assertEqual(entry["requested_model"], "redacted")
        self.assertIsNone(entry["usage"]["input_tokens"])
        self.assertIsNone(entry["elapsed_seconds"])

    def test_broken_log_sink_does_not_interrupt_usage(self):
        tracker = survey_service._ReportLLMUsageTracker()
        record = survey_service._report_writer_attempt_callback(
            tracker.callback("writing"), session_id="session-a", run_id="run-a",
            phase="writing", step="core_review",
        )
        with patch("builtins.print", side_effect=OSError("log closed")):
            record({"status": "started", "call_id": "call-a", "model": "model-a"})
            record({
                "status": "completed", "call_id": "call-a", "model": "model-a",
                "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
            })
        self.assertEqual(tracker.snapshot()["totals"]["total_tokens"], 5)
        self.assertEqual(tracker.snapshot()["totals"]["active_calls"], 0)

    def test_interleaved_report_logs_keep_run_and_part_contexts_isolated(self):
        callbacks = [
            survey_service._report_writer_attempt_callback(
                lambda _event: None, session_id=f"session-{index}", run_id=f"run-{index}",
                phase="writing", step="part", part_index=index,
            )
            for index in (1, 2)
        ]
        with patch("builtins.print") as log:
            for index, status in ((0, "started"), (1, "started"), (1, "failed"), (0, "completed")):
                callbacks[index]({"status": status, "call_id": f"call-{index + 1}"})
        entries = [json.loads(call.args[0].split(" ", 1)[1]) for call in log.call_args_list]
        self.assertEqual([e["run_id"] for e in entries], ["run-1", "run-2", "run-2", "run-1"])
        for entry in entries:
            index = entry["part_index"]
            self.assertEqual(entry["session_id"], f"session-{index}")
            self.assertEqual(entry["call_id"], f"call-{index}")

    def test_tracker_aggregates_concurrency_fallback_and_missing_usage(self):
        tracker = survey_service._ReportLLMUsageTracker()
        record = tracker.callback("themes")

        record({
            "status": "started", "call_id": "call-1",
            "model": "route-a", "fallback": False,
        })
        record({
            "status": "started", "call_id": "call-2",
            "model": "model-a", "fallback": False,
        })
        record({
            "status": "started", "call_id": "call-3",
            "model": "model-b", "fallback": True,
        })

        active = tracker.snapshot()
        self.assertEqual(
            active["phases"]["themes"]["active_models"],
            {"route-a": 1, "model-a": 1, "model-b": 1},
        )
        self.assertEqual(active["totals"]["active_calls"], 3)

        record({
            "status": "completed",
            "call_id": "call-1",
            "model": "route-a",
            "response_model": "actual-a",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
            },
        })
        record({
            "status": "failed",
            "call_id": "call-2",
            "model": "model-a",
            "usage_complete": True,
            "usage": {
                "input_tokens": 2,
                "output_tokens": 1,
                "total_tokens": 3,
            },
        })
        record({
            "status": "completed",
            "call_id": "call-3",
            "model": "model-b",
            "response_model": "actual-b",
            "usage": None,
        })
        # A duplicate terminal event must not double-count tokens or close another call.
        record({
            "status": "completed",
            "call_id": "call-1",
            "model": "route-a",
            "response_model": "actual-a",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 30,
                "total_tokens": 150,
            },
        })

        snapshot = tracker.snapshot()
        themes = snapshot["phases"]["themes"]
        self.assertEqual(themes["models_used"], ["actual-a", "model-a", "actual-b"])
        self.assertEqual(themes["fallback_models_used"], ["actual-b"])
        self.assertEqual(themes["call_count"], 3)
        self.assertEqual(themes["usage_reported_call_count"], 2)
        self.assertEqual(themes["usage_missing_call_count"], 1)
        self.assertEqual(themes["total_tokens"], 153)
        self.assertEqual(themes["active_calls"], 0)
        self.assertEqual(themes["active_models"], {})
        self.assertEqual(snapshot["totals"]["total_tokens"], 153)

    def test_tracker_marks_partial_usage_as_known_lower_bound(self):
        tracker = survey_service._ReportLLMUsageTracker()
        record = tracker.callback("themes")
        record({
            "status": "started",
            "call_id": "partial-call",
            "model": "model-a",
            "fallback": False,
        })
        record({
            "status": "failed",
            "call_id": "partial-call",
            "model": "model-a",
            "usage_complete": False,
            "usage": {
                "input_tokens": 30,
                "output_tokens": 0,
                "total_tokens": 30,
            },
        })

        usage = tracker.snapshot()["totals"]
        self.assertEqual(usage["total_tokens"], 30)
        self.assertEqual(usage["usage_reported_call_count"], 1)
        self.assertEqual(usage["usage_missing_call_count"], 1)
        self.assertEqual(usage["active_calls"], 0)

    async def test_report_stream_emits_and_persists_real_usage_snapshots(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "评分"], ["p-1", "5"]],
            "plan": {
                "columns": [
                    {"index": 0, "name": "玩家ID", "role": "id"},
                    {"index": 1, "name": "评分", "role": "rating"},
                ],
                "parts": [{"name": "体验评价", "column_indexes": [1]}],
                "branch_rules": [],
            },
            "branch_rules": [],
            "stats_md": "## 评分\n\n有效样本(总计): 总体=1",
            "open_text": {},
        }
        answers = iter([
            "# 问卷分析报告",
            "## Part 1 体验评价\n\n本节总结。",
            "NONE",
            "<!--CORE_START-->\n## 核心结论\n总体评价积极。\n<!--CORE_END-->",
            "PASS",
            (
                "## 行动建议\n\n"
                "1. **持续验证体验**（优先级：中）\n"
                "   - **核心判断：** 当前样本评价积极。\n"
                "   - **产品动作：** 保持方案并继续收集反馈。\n"
                "   - **验证方式：** 跟踪后续评分。\n"
                "   - **依据：** 当前评分结果。\n"
                "   - **不确定性/前提：** 样本量仍有限。"
            ),
        ])
        call_sequence = 0

        async def fake_writer(_messages, _query, *, on_attempt_event=None):
            nonlocal call_sequence
            call_sequence += 1
            call_id = f"writer-{call_sequence}"
            on_attempt_event({
                "status": "started",
                "call_id": call_id,
                "model": "model-a",
                "requested_model": "model-a",
                "fallback": False,
            })
            await asyncio.sleep(0.02)
            on_attempt_event({
                "status": "completed",
                "call_id": call_id,
                "model": "model-a",
                "requested_model": "model-a",
                "fallback": False,
                "usage_complete": True,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                },
            })
            return next(answers), "model-a"

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(
                survey_service,
                "_current_login",
                new=AsyncMock(return_value=None),
            ),
            patch.object(survey_service, "_direct_writer_round", new=fake_writer),
            patch("builtins.print") as log,
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(
                survey_service.survey_stats,
                "find_numbers_not_in_stats",
                return_value=[],
            ),
        ):
            events = [
                event
                async for event in survey_service.report_stream("sid", object())
            ]

        payloads = _payloads(events)
        entries = [
            json.loads(call.args[0].split(" ", 1)[1])
            for call in log.call_args_list
            if str(call.args[0]).startswith("[report-llm-attempt] ")
        ]
        starts = [e for e in entries if e["status"] == "started"]
        self.assertEqual([e["step"] for e in starts], [
            "title", "part", "bug_check", "core", "core_review", "action",
        ])
        self.assertEqual(len(entries), 12)
        self.assertEqual(len({e["run_id"] for e in entries}), 1)
        self.assertEqual({e["session_id"] for e in entries}, {"sid"})
        self.assertEqual(starts[1]["part_index"], 1)
        self.assertTrue(all(e["part_index"] is None for e in starts if e["step"] != "part"))
        status_events = [
            item for item in payloads if item.get("type") == "report_llm_status"
        ]
        self.assertTrue(status_events)
        self.assertTrue(any(
            item["report_llm_usage"]["phases"]["writing"]["active_models"]
            == {"model-a": 1}
            for item in status_events
        ), status_events)

        done = next(item for item in payloads if item.get("type") == "report_done")
        usage = done["report_llm_usage"]
        self.assertEqual(usage["phases"]["writing"]["call_count"], 6)
        self.assertEqual(usage["phases"]["writing"]["total_tokens"], 90)
        self.assertEqual(usage["totals"]["total_tokens"], 90)
        self.assertEqual(usage["totals"]["usage_missing_call_count"], 0)
        self.assertEqual(
            sess["report_versions"][-1]["report_llm_usage"],
            usage,
        )

    async def test_report_error_flushes_closed_usage_snapshot(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "评分"], ["p-1", "5"]],
            "plan": {
                "columns": [
                    {"index": 0, "name": "玩家ID", "role": "id"},
                    {"index": 1, "name": "评分", "role": "rating"},
                ],
                "parts": [{"name": "体验评价", "column_indexes": [1]}],
                "branch_rules": [],
            },
            "branch_rules": [],
            "stats_md": "## 评分\n\n有效样本(总计): 总体=1",
            "open_text": {},
        }

        async def broken_writer(_messages, _query, *, on_attempt_event=None):
            on_attempt_event({
                "status": "started",
                "call_id": "broken-writer",
                "model": "model-a",
                "requested_model": "model-a",
                "fallback": False,
            })
            raise RuntimeError("writer failed")

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(
                survey_service,
                "_current_login",
                new=AsyncMock(return_value=None),
            ),
            patch.object(survey_service, "_direct_writer_round", new=broken_writer),
            patch("traceback.print_exc"),
        ):
            events = [
                event
                async for event in survey_service.report_stream("sid-error", object())
            ]

        payloads = _payloads(events)
        self.assertEqual(payloads[-1]["type"], "error")
        status_events = [
            item for item in payloads if item.get("type") == "report_llm_status"
        ]
        final_usage = status_events[-1]["report_llm_usage"]
        self.assertEqual(final_usage["totals"]["active_calls"], 0)
        self.assertEqual(final_usage["totals"]["call_count"], 1)
        self.assertEqual(final_usage["totals"]["usage_missing_call_count"], 1)

    async def test_early_report_validation_error_keeps_original_sse_error(self):
        invalid_sess = {
            "filename": "responses.xlsx",
            "rows": [],
            "plan": {},
        }
        with (
            patch.object(survey_service, "get_session", return_value=invalid_sess),
            patch.object(
                survey_service,
                "_current_login",
                new=AsyncMock(return_value=None),
            ),
            patch("traceback.print_exc"),
        ):
            events = [
                event
                async for event in survey_service.report_stream(
                    "sid-invalid",
                    object(),
                )
            ]

        payloads = _payloads(events)
        self.assertEqual(payloads[-2]["type"], "report_llm_status")
        self.assertEqual(payloads[-1]["type"], "error")
        self.assertNotIn("UnboundLocalError", payloads[-1]["message"])


if __name__ == "__main__":
    unittest.main()
