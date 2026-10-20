"""Synthetic stream/lifecycle checks; no real models, network or runtime data."""
import asyncio
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.responses import sse_event
from app.core.runtime_logging import bind_runtime_operator, reset_runtime_operator
from app.services import report_runtime_logging as logs
from app.services import survey_service as service


class ProgressTests(unittest.TestCase):
    def test_structured_exception_cause_explains_failure_without_private_body(self):
        from app.integrations.llm_client import _LLMRequestError
        root = _LLMRequestError("PRIVATE_VENDOR_ERROR", status_code=401)
        outer = RuntimeError("PRIVATE_WRAPPER")
        outer.__cause__ = root
        output = io.StringIO()
        with redirect_stdout(output):
            logs.SurveyProgressLog("synthetic").fail(error=outer)
        self.assertIn("模型服务拒绝了认证", output.getvalue())
        self.assertIn("个人模型凭据", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_quick_progress_counts_are_deduplicated_without_question_text(self):
        output = io.StringIO()
        with redirect_stdout(output):
            progress = logs.SurveyProgressLog("synthetic")
            for completed in (0, 0, 1, 1, 2):
                progress.observe(sse_event({"type": "analysis_progress", "phase": "quick_questions",
                    "run_completed": completed, "run_total": 2, "question": "PRIVATE_QUESTION"}))
        self.assertEqual(output.getvalue().count("本轮已完成"), 3)
        self.assertIn("2/2 道题目", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())

    def test_waiting_is_throttled_and_only_claims_last_known_stage(self):
        clock = [0.0]
        output = io.StringIO()
        with patch.object(logs, "time", SimpleNamespace(monotonic=lambda: clock[0])), \
             patch.object(logs.config, "LLM_STREAM_HEARTBEAT_SECONDS", 20), redirect_stdout(output):
            progress = logs.SurveyProgressLog("synthetic")
            progress.start()
            progress.observe(sse_event({"type": "analysis_progress", "phase": "postwrite", "stage": "writer"}))
            for second in range(1, 62):
                clock[0] = float(second)
                progress.observe(sse_event({"type": "heartbeat"}))
            progress.observe(sse_event({"type": "report_done", "report_status": "complete", "version": 1}))
            clock[0] = 100
            progress.wait()
        text = output.getvalue()
        self.assertEqual(text.count("仍在等待当前阶段"), 3)
        self.assertIn("撰写初稿", text)
        self.assertIn("已等待 60 秒", text)
        self.assertNotIn("正常推进", text)
        self.assertEqual(text.count("报告结果已生成并保存"), 1)

    def test_failure_and_partial_are_not_full_success_and_private_text_is_never_printed(self):
        for event in (
            {"type": "error", "message": "PRIVATE_RAW sk-secret@example.test", "failure": {"error_category": "output_truncated"}},
            {"type": "report_done", "report_status": "partial", "report_md": "PRIVATE_REPORT", "report_audit": {"stop_reason": "PRIVATE_REASON"}},
        ):
            with self.subTest(event=event["type"]):
                output = io.StringIO()
                with redirect_stdout(output):
                    progress = logs.SurveyProgressLog("synthetic")
                    progress.observe(sse_event(event))
                self.assertNotIn("报告结果已生成并保存", output.getvalue())
                self.assertNotIn("PRIVATE", output.getvalue())
                self.assertIn("截断" if event["type"] == "error" else "部分核验或题目尚未完成", output.getvalue())

    def test_save_failure_is_distinct_and_queued_progress_cannot_change_saving(self):
        output = io.StringIO()
        with redirect_stdout(output):
            progress = logs.SurveyProgressLog("synthetic")
            progress.enter("saving")
            progress.observe(sse_event({"type": "analysis_progress", "phase": "postwrite", "stage": "writer"}))
            progress.fail(error=OSError("PRIVATE_DISK_PATH"))
        self.assertIn("未能确认保存成功", output.getvalue())
        self.assertIn("阶段：保存报告结果", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        self.assertNotIn("模型服务", output.getvalue())

    def test_unfinished_stream_and_unknown_failure_are_honest(self):
        output = io.StringIO()
        with redirect_stdout(output):
            logs.SurveyProgressLog("one").finish()
            logs.SurveyProgressLog("two").fail(error=ValueError("private-provider-body"))
        self.assertIn("尚未收到任务完成记录", output.getvalue())
        self.assertIn("具体原因尚未确认", output.getvalue())
        self.assertNotIn("private-provider-body", output.getvalue())


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_interleaved_streams_and_cross_task_close_restore_context(self):
        seen = []
        @logs.log_survey_stream("report")
        async def stream(session_id):
            try:
                for _ in range(2):
                    seen.append((session_id, logs._CURRENT.get().session_id))
                    yield sse_event({"type": "heartbeat"})
            finally:
                seen.append((session_id, logs._CURRENT.get().session_id))
        output = io.StringIO()
        with redirect_stdout(output):
            first, second = stream("one"), stream("two")
            await anext(first)
            self.assertIsNone(logs._CURRENT.get())
            await anext(second)
            self.assertIsNone(logs._CURRENT.get())
            await anext(first)
            await asyncio.create_task(first.aclose())
            await second.aclose()
        self.assertTrue(all(actual == expected for actual, expected in seen))
        self.assertIsNone(logs._CURRENT.get())

    async def test_real_save_failure_never_emits_saved_success_or_changes_rollback(self):
        from tests.test_survey_report_versions import _base_session, _writer, _isolated_report_runtime
        for save_fails in (False, True):
            with self.subTest(save_fails=save_fails):
                sess = _base_session()
                writer = _writer("PRIVATE_REPORT_BODY")
                output = io.StringIO()
                with _isolated_report_runtime(sess, writer) as runtime, \
                     patch.object(service, "_use_postwrite_engine", return_value=False), redirect_stdout(output):
                    if save_fails:
                        runtime["save_history"].side_effect = OSError("PRIVATE_DISK_DETAILS")
                    result = [json.loads(event[6:]) async for event in service.report_stream("save-check", object())]
                done = [event for event in result if event.get("type") == "report_done"]
                self.assertEqual(bool(done), not save_fails)
                self.assertEqual("报告结果已生成并保存" in output.getvalue(), not save_fails)
                if save_fails:
                    self.assertIn("未能确认保存成功", output.getvalue())
                    self.assertNotIn("report_versions", sess)
                else:
                    self.assertEqual(done[0]["version"], 1)
                    runtime["save_history"].assert_called_once()
                self.assertNotIn("PRIVATE_REPORT_BODY", output.getvalue())
                self.assertNotIn("PRIVATE_DISK_DETAILS", output.getvalue())

    async def test_real_postwrite_partial_preserves_draft_and_logs_incomplete_audit(self):
        from tests.test_report_postwrite_flow import runtime, fake_collect, session
        sess = session()
        calls = []
        async def collect(messages, **kwargs):
            calls.append(deepcopy(messages))
            if "blocks" in json.loads(messages[-1]["content"]):
                raise RuntimeError("PRIVATE_AUDIT_BODY")
            return await fake_collect(messages, **kwargs)
        output = io.StringIO()
        with runtime(sess, collect), redirect_stdout(output):
            result = [json.loads(event[6:]) async for event in service.report_stream("partial-check", None)]
        done = next(event for event in result if event.get("type") == "report_done")
        self.assertEqual(done["report_status"], "partial")
        self.assertEqual(len(calls), 2)
        self.assertTrue(sess["report_md"])
        self.assertIn("部分核验或题目尚未完成", output.getvalue())
        self.assertNotIn("报告结果已生成并保存", output.getvalue())
        self.assertNotIn("PRIVATE_AUDIT_BODY", output.getvalue())
        self.assertIn(sess["postwrite_run_id"][-8:], output.getvalue())

    async def test_observer_preserves_every_byte_and_original_exception(self):
        events = [sse_event({"type": "chunk", "content": "PRIVATE_ANSWER"}),
                  sse_event({"type": "report_done", "report_status": "partial", "version": 3})]
        @logs.log_survey_stream("report")
        async def stream(session_id):
            for event in events:
                yield event
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual([event async for event in stream("synthetic")], events)
        self.assertNotIn("PRIVATE", output.getvalue())

        failure = RuntimeError("private upstream body")
        @logs.log_survey_stream("report")
        async def failed(session_id):
            yield events[0]
            raise failure
        with redirect_stdout(output), self.assertRaises(RuntimeError) as raised:
            _ = [event async for event in failed("synthetic")]
        self.assertIs(raised.exception, failure)
        self.assertNotIn("private upstream body", output.getvalue())

    async def test_output_failure_does_not_change_business_events(self):
        events = [sse_event({"type": "report_done", "report_status": "complete"})]
        @logs.log_survey_stream("report")
        async def stream(session_id):
            for event in events:
                yield event
        with patch.object(logs, "runtime_log", side_effect=OSError("closed output")):
            self.assertEqual([event async for event in stream("synthetic")], events)

    async def test_cancellation_closes_source_and_cannot_claim_complete(self):
        entered, closed = asyncio.Event(), asyncio.Event()
        @logs.log_survey_stream("report")
        async def stream(session_id):
            try:
                entered.set()
                await asyncio.Event().wait()
                yield "unreachable"
            finally:
                closed.set()
        async def consume():
            return [event async for event in stream("synthetic")]
        output = io.StringIO()
        with redirect_stdout(output):
            task = asyncio.create_task(consume())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(closed.is_set())
        self.assertIn("已停止", output.getvalue())
        self.assertNotIn("已生成并保存", output.getvalue())
        self.assertIsNone(logs._CURRENT.get())

    async def test_concurrent_requests_keep_operator_and_stage_separate(self):
        output = io.StringIO()
        @logs.log_survey_stream("report")
        async def stream(session_id):
            logs.report_log_phase("writer" if session_id == "one" else "quantity")
            await asyncio.sleep(0)
            yield sse_event({"type": "report_done", "report_status": "complete"})
        async def consume(sid, name):
            token = bind_runtime_operator(name)
            try:
                return [event async for event in stream(sid)]
            finally:
                reset_runtime_operator(token)
        with redirect_stdout(output):
            await asyncio.gather(consume("one", "用户甲"), consume("two", "用户乙"))
        first = [line for line in output.getvalue().splitlines() if "用户甲" in line]
        second = [line for line in output.getvalue().splitlines() if "用户乙" in line]
        self.assertTrue(first and second)
        self.assertNotIn("核对人数范围", "\n".join(first))
        self.assertNotIn("撰写初稿", "\n".join(second))
        self.assertIsNone(logs._CURRENT.get())

    async def test_real_report_dispatch_does_not_change_mode_calls_or_saved_completion(self):
        for mode in ("full", "quick", "postwrite"):
            with self.subTest(mode=mode):
                events = [sse_event({"type": "analysis_progress", "phase": "writing", "status": "running"}),
                          sse_event({"type": "report_done", "report_status": "complete", "version": 1})]
                called = []
                def engine_for(name):
                    async def engine(sid, request, **options):
                        called.append((name, sid, options))
                        for event in events:
                            yield event
                    return engine
                sess = {"report_mode": "quick" if mode == "quick" else "insight"}
                output = io.StringIO()
                with ExitStack() as stack, redirect_stdout(output):
                    stack.enter_context(patch.object(service, "_current_login", AsyncMock(return_value=None)))
                    stack.enter_context(patch.object(service, "require_session_access", return_value=deepcopy(sess)))
                    stack.enter_context(patch.object(service, "_use_postwrite_engine", return_value=mode == "postwrite"))
                    for name, engine_mode in (("_full_report_stream", "full"), ("_quick_report_stream", "quick"),
                                              ("_postwrite_report_stream", "postwrite")):
                        stack.enter_context(patch.object(service, name, engine_for(engine_mode)))
                    result = [event async for event in service.report_stream("dispatch-test", None)]
                self.assertEqual(result[1:], events)
                self.assertEqual(len(called), 1)
                self.assertEqual(called[0][0], mode)
                self.assertEqual("retry_failed" in called[0][2], mode != "full")
                self.assertIn("报告结果已生成并保存", output.getvalue())
                self.assertNotIn("dispatch-test", service._REPORT_CANCEL_EVENTS)
