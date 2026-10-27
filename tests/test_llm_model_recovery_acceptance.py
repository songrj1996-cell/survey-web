"""Offline publication/restart/recovery and cancellation acceptance.

Restart really starts another Python process; only synthetic isolated JSON is read.
Integrated lifecycle fixes run as ordinary acceptance criteria; the historical
baseline reproduction remains isolated from the current implementation.
"""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.core.llm_context import bind_llm_api_key, bind_llm_attempt_observer
from app.integrations import llm_client
from app.services import llm_model_settings as settings, llm_usage
from app.storage import sessions
from tests import test_llm_model_business_acceptance as helpers


# A separate interpreter imports config only after receiving isolated environment.
# The transport rejects every URL except the synthetic gateway, including retries.
RESTART_CODE = r'''
import asyncio, io, json, sys
from contextlib import redirect_stdout
from pathlib import Path
import httpx
from app.core import config
from app.core.llm_context import bind_llm_api_key
from app.integrations import llm_client
from app.services.report_quick_pipeline import run_quick_pipeline
from tests.test_report_quick_pipeline import answer

folder = Path(sys.argv[1])
payload = json.loads((folder / "restart-input.json").read_text(encoding="utf-8"))
calls = []
llm_client.LLM_API_BASE = "https://model-acceptance.invalid/v1"
llm_client.LLM_REPORT_MAX_ATTEMPTS = 1

async def fake_gateway(transport, request):
    assert request.url.host == "model-acceptance.invalid", "External HTTP forbidden"
    assert str(request.url).endswith("/responses")
    body = json.loads(request.content)
    calls.append(body)
    response = answer(json.loads(body["input"][-1]["content"]))
    events = [{"type":"response.output_text.delta", "delta":response},
        {"type":"response.completed", "response":{"status":"completed", "model":body["model"],
            "usage":{"input_tokens":11,"output_tokens":7,"total_tokens":18}}}]
    return httpx.Response(200, headers={"content-type":"text/event-stream"},
        content="".join("data: " + json.dumps(e, ensure_ascii=False) + "\n\n" for e in events).encode())

httpx.AsyncHTTPTransport.handle_async_request = fake_gateway

async def run():
    with bind_llm_api_key("offline-acceptance-fixture"):
        result = await run_quick_pipeline(payload["questions"], checkpoint=payload["checkpoint"],
            retry_failed=True, collect=llm_client.collect_chat_completion)
        first_count = len(calls)
        repeated = await run_quick_pipeline(payload["questions"], checkpoint=result["checkpoint"],
            retry_failed=True, collect=llm_client.collect_chat_completion)
    return {"result":result, "calls":calls, "first_count":first_count,
            "repeated_status":repeated["report_status"], "pid":__import__("os").getpid()}

with redirect_stdout(io.StringIO()):
    output = asyncio.run(run())
print("RESTART_RESULT=" + json.dumps(output, ensure_ascii=False))
'''


class RecoveryAcceptanceTests(helpers.AcceptanceCase):
    async def test_usage_wrapper_close_releases_owned_stream_before_returning(self):
        """MM-O06: the HTTP-facing credentials/usage wrapper owns its child."""
        from app.services import llm_credentials
        closed = asyncio.Event()
        async def child():
            try:
                yield "synthetic progress"
                await asyncio.Event().wait()
            finally:
                closed.set()
        source = child()
        wrapped = llm_credentials.stream_with_llm_api_key(source, helpers.FIXTURE_KEY)
        try:
            await anext(wrapped)
            await wrapped.aclose()
            self.assertTrue(closed.is_set(), "Outer usage stream closed before its owned child")
        finally:
            await source.aclose()
            await wrapped.aclose()

    async def test_usage_wrapper_can_close_from_a_different_consumer_task(self):
        """MM-O06: context tokens must not cross yielded output boundaries."""
        from app.services import llm_credentials
        async def child():
            yield "synthetic progress"
        source = child()
        wrapped = llm_credentials.stream_with_llm_api_key(source, helpers.FIXTURE_KEY)
        try:
            self.assertEqual(await asyncio.create_task(anext(wrapped)), "synthetic progress")
            await wrapped.aclose()
        finally:
            await source.aclose()

    async def test_interview_public_close_reclaims_pending_http(self):
        """MM-O07: audit every relay, including the new configuration wrapper."""
        await self.check_interview_close(inner=False)

    async def test_usage_wrapper_interleaved_streams_keep_separate_credentials(self):
        """MM-O06: synthetic interleaving must not borrow the other stream's key."""
        from app.core.llm_context import current_llm_api_key
        from app.services import llm_credentials
        async def child():
            yield "ready"
            yield current_llm_api_key()
        source_a, source_b = child(), child()
        stream_a = llm_credentials.stream_with_llm_api_key(source_a, "synthetic-key-a")
        stream_b = llm_credentials.stream_with_llm_api_key(source_b, "synthetic-key-b")
        try:
            await anext(stream_a)
            await anext(stream_b)
            self.assertEqual(await anext(stream_a), "synthetic-key-a")
        finally:
            await stream_b.aclose()
            await stream_a.aclose()
            await source_b.aclose()
            await source_a.aclose()

    async def test_interview_inner_close_reclaims_pending_http(self):
        """MM-O07: the pre-existing stage/heartbeat relays have the same gap."""
        await self.check_interview_close(inner=True)

    async def check_interview_close(self, *, inner):
        from app.core.interview_parsing import parse_interview_workbook, serialize_interview_workbook
        from app.services import interview_service as service
        from tests.test_interview_report import _workbook_bytes, _Request
        workbook = parse_interview_workbook("synthetic.xlsx", _workbook_bytes())
        sid = self.session(kind="interview", mode="interview", filename="synthetic.xlsx",
            interview_workbook=workbook, interview_source_text=serialize_interview_workbook(workbook))
        self.block_stage = "interview.extract"
        workers = []
        original_gateway = httpx.AsyncHTTPTransport.handle_async_request
        async def gateway(transport, request):
            workers.append(asyncio.current_task())
            return await original_gateway(transport, request)
        with patch.object(service, "_visible_to_owner", return_value=True), \
             patch.object(service, "LLM_STREAM_HEARTBEAT_SECONDS", 0.01), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", gateway), \
             bind_llm_api_key(helpers.FIXTURE_KEY):
            entry = service._interview_report_stream if inner else service.interview_report_stream
            stream = entry(sid, _Request())
            try:
                for _ in range(8):
                    await asyncio.wait_for(anext(stream), 5)
                    if self.started.is_set():
                        break
                self.assertTrue(self.started.is_set())
                await stream.aclose()
                self.assertTrue(workers)
                self.assertTrue(all(worker.done() for worker in workers), "Interview HTTP still pending after stream close")
            finally:
                await stream.aclose()
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def test_report_close_at_session_ready_releases_running_marker(self):
        """MM-O08: closing before the pump starts must still release the guard."""
        service, sid = self.quick_report_case()
        stream = service.report_stream(sid, object())
        try:
            first = helpers.decode_events([await anext(stream)])[0]
            self.assertEqual(first["type"], "session_ready")
            await stream.aclose()
            self.assertNotIn(sid, service._REPORT_CANCEL_EVENTS)
            self.assertEqual(self.calls, [])
        finally:
            await stream.aclose()
            service._REPORT_CANCEL_EVENTS.pop(sid, None)

    async def test_plan_close_reclaims_pending_http(self):
        """MM-O07: survey planning uses the same unclosed heartbeat relays."""
        await self.check_survey_stage_close("plan")

    async def test_columns_close_reclaims_pending_http(self):
        """MM-O07: include column recognition, not only report generation."""
        await self.check_survey_stage_close("columns")

    async def check_survey_stage_close(self, stage):
        from app.services import survey_service as service
        sid = self.session(mode="standard", rows=[["段位", "主玩位置", "界面满意度"], ["金", "中", "4"]])
        self.block_stage = "survey." + stage
        workers = []
        original_gateway = httpx.AsyncHTTPTransport.handle_async_request
        async def gateway(transport, request):
            workers.append(asyncio.current_task())
            return await original_gateway(transport, request)
        with patch.object(service, "_ensure_branch_rules", return_value=[]), \
             patch.object(service, "audit_log", AsyncMock()), \
             patch.object(service, "LLM_STREAM_HEARTBEAT_SECONDS", 0.01), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", gateway), \
             bind_llm_api_key(helpers.FIXTURE_KEY):
            stream = getattr(service, stage + "_stream")(sid, object())
            try:
                for _ in range(8):
                    await asyncio.wait_for(anext(stream), 5)
                    if self.started.is_set():
                        break
                self.assertTrue(self.started.is_set())
                await stream.aclose()
                self.assertTrue(workers)
                self.assertTrue(all(worker.done() for worker in workers), "Survey HTTP still pending after stream close")
            finally:
                await stream.aclose()
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def test_annotation_cancel_reclaims_pending_http_without_fallback(self):
        service, sid, sess, _ = self.annotation_quality_case()
        self.block_stage = "annotate.quality"
        workers = []
        original_gateway = httpx.AsyncHTTPTransport.handle_async_request
        async def gateway(transport, request):
            workers.append(asyncio.current_task())
            return await original_gateway(transport, request)
        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", gateway):
            consumer = asyncio.create_task(self.consume(service.quality_stream(sid, object()), sid=sid, category="annotate"))
            try:
                await asyncio.wait_for(self.started.wait(), 5)
                consumer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await consumer
                self.assertTrue(workers)
                self.assertTrue(all(worker.done() for worker in workers))
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(sess["quality_status"], "incomplete")
                self.assertEqual(llm_usage.get_user_llm_usage(helpers.LOGIN, period="all")["records"][0]["status"], "cancelled")
            finally:
                consumer.cancel()
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(consumer, *workers, return_exceptions=True)

    async def test_annotation_close_reclaims_pending_http_before_returning(self):
        """MM-O04: stage wrappers must close their child iterators explicitly."""
        await self.check_annotation_close("quality")

    async def test_annotation_ai_close_reclaims_pending_http_before_returning(self):
        """MM-O04 also affects the AI entry through the same stage wrappers."""
        await self.check_annotation_close("ai")

    async def test_annotation_ai_inner_close_marks_task_incomplete(self):
        """MM-O05: closing the inner stream must leave a resumable incomplete state."""
        sess = await self.check_annotation_close("ai", inner=True)
        self.assertEqual(sess["ai_status"], "incomplete")

    async def check_annotation_close(self, stage, *, inner=False):
        service, sid, sess, _ = self.annotation_quality_case()
        sess["tasks"] = {"ai_detect": stage == "ai", "quality": stage == "quality"}
        self.block_stage = "annotate." + stage
        workers = []
        original_gateway = httpx.AsyncHTTPTransport.handle_async_request
        async def gateway(transport, request):
            workers.append(asyncio.current_task())
            return await original_gateway(transport, request)
        with patch.object(httpx.AsyncHTTPTransport, "handle_async_request", gateway), \
             patch.object(service, "_ANNOTATE_SSE_HEARTBEAT_SECONDS", 0.01), bind_llm_api_key(helpers.FIXTURE_KEY):
            entry = (service._quality_stream if stage == "quality" else service._ai_detect_stream) if inner else (
                service.quality_stream if stage == "quality" else service.ai_detect_stream)
            if inner:
                # Supply the same frozen settings as the public entry while
                # testing the inner stream's own cancellation state.
                entry = settings.model_operation(entry)
            stream = entry(sid, object())
            advancing = None
            try:
                await anext(stream)
                advancing = asyncio.create_task(anext(stream))
                await asyncio.wait_for(self.started.wait(), 5)
                await asyncio.wait_for(advancing, 5)
                await stream.aclose()
                self.assertTrue(workers)
                self.assertTrue(all(worker.done() for worker in workers), "Closing the annotation stream left its HTTP worker running")
                self.assertEqual(len(self.calls), 1, "Closing must not trigger another model or fallback")
                self.assertIsNone(settings.current_llm_model_snapshot())
            finally:
                if advancing is not None:
                    advancing.cancel()
                    await asyncio.gather(advancing, return_exceptions=True)
                await stream.aclose()
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        return sess

    async def test_quality_close_then_publication_resumes_original_snapshot_and_results(self):
        from app.services import annotate_workflow as service
        sess = await self.check_annotation_close("quality")
        saved = deepcopy(sess["llm_model_stage_snapshots"]["quality"])
        retained = deepcopy(sess.get("quality_results", []))
        await self.publish("b")
        self.block_stage = None
        self.calls.clear()
        events = await self.consume(service.quality_stream("offline-quality-acceptance", object()), category="annotate")
        self.assertIn("quality_done", [event["type"] for event in events])
        self.assertEqual(sess["llm_model_stage_snapshots"]["quality"], saved)
        self.assertEqual(saved["llm_model_snapshot"]["version"], 1)
        self.assertTrue(all(call["model"].endswith("a-primary") for call in self.calls))
        for result in retained:
            recovered = next(item for item in sess["quality_results"] if item["id"] == result["id"])
            self.assertEqual(recovered["originals"], result["originals"])
            if not result.get("overall_pending"):
                self.assertEqual(recovered, result)
        self.assertEqual(sess["quality_status"], "complete")
        self.assert_usage(["annotate.quality", "annotate.translation"], version=1)

    async def test_quality_close_after_done_preserves_completed_state(self):
        """MM-O09: a completed result stays complete after its connection closes."""
        service, sid, sess, _ = self.annotation_quality_case()
        with bind_llm_api_key(helpers.FIXTURE_KEY):
            stream = service.quality_stream(sid, object())
            try:
                while True:
                    event = helpers.decode_events([await anext(stream)])[0]
                    if event["type"] == "quality_done":
                        break
                self.assertEqual(sess["quality_status"], "complete")
                saved = deepcopy(sess["quality_results"])
                calls = len(self.calls)
                await stream.aclose()
                self.assertEqual(sess["quality_results"], saved)
                self.assertEqual(len(self.calls), calls)
                self.assertEqual(sess["quality_status"], "complete")
            finally:
                await stream.aclose()

    async def test_annotation_quality_retry_only_missing_player_keeps_old_settings(self):
        service, sid, sess, results = self.annotation_quality_case()
        self.answers["annotate.quality"] = [deepcopy(results[0])]
        events = await self.consume(service.quality_stream(sid, object()), sid=sid, category="annotate")
        self.assertIn("quality_done", [e["type"] for e in events])
        self.assertEqual(sess["missing_quality_ids"], ["P2"])
        self.assertEqual(sess["quality_status"], "incomplete")
        first = deepcopy(next(r for r in sess["quality_results"] if r["id"] == "P1"))
        await self.publish("b")
        self.calls.clear()
        self.answers["annotate.quality"] = deepcopy(results)
        events = await self.consume(service.quality_stream(sid, object(), retry_ids={"P2"}), sid=sid, category="annotate")
        self.assertIn("quality_done", [e["type"] for e in events])
        self.assertEqual(sess["quality_status"], "complete")
        self.assertEqual(next(r for r in sess["quality_results"] if r["id"] == "P1"), first)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('"P1"', self.calls[0]["input"][-1]["content"])
        self.assertIn('"P2"', self.calls[0]["input"][-1]["content"])
        self.assertTrue(self.calls[0]["model"].endswith("a-primary"))
        self.assertEqual(sess["llm_model_stage_snapshots"]["quality"]["llm_model_snapshot"]["version"], 1)
        self.assert_usage(["annotate.quality"], version=1)

    async def test_annotation_translation_retry_keeps_labels_and_frozen_stage(self):
        service, sid, sess, _ = self.annotation_quality_case()
        self.answers["annotate.translation"] = []
        events = await self.consume(service.quality_stream(sid, object()), sid=sid, category="annotate")
        self.assertIn("quality_done", [e["type"] for e in events])
        self.assertEqual(sess["quality_status"], "complete")
        self.assertEqual(set(sess["missing_translation_ids"]), {"P1", "P2"})
        labels = [{k: deepcopy(v) for k, v in r.items() if k != "translations"} for r in sess["quality_results"]]
        await self.publish("b")
        self.calls.clear()
        self.answers["annotate.translation"] = [
            {"id": "P1", "key": "col_1", "translation": "排队需要十分钟。"},
            {"id": "P2", "key": "col_1", "translation": "我在晚上匹配不到对局。"}]
        events = await self.consume(service.quality_stream(sid, object(), retry_ids={"P2"}), sid=sid, category="annotate")
        done = next(e for e in events if e["type"] == "quality_done")
        self.assertEqual(done["missing_translation_ids"], ["P1"])
        self.assertEqual([{k: v for k, v in r.items() if k != "translations"} for r in sess["quality_results"]], labels)
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn('"P1"', self.calls[0]["input"][-1]["content"])
        self.assertTrue(self.calls[0]["model"].endswith("a-primary"))
        self.assert_usage(["annotate.translation"], version=1)
        self.calls.clear()
        events = await self.consume(service.quality_stream(sid, object(), retry_ids={"P1"}), sid=sid, category="annotate")
        self.assertEqual(next(e for e in events if e["type"] == "quality_done")["missing_translation_ids"], [])
        self.assertEqual(len(self.calls), 1)
        self.assert_usage(["annotate.translation"], version=1)

    async def test_quick_report_history_completion_preserves_success_and_frozen_inputs(self):
        from tests.test_report_quick_pipeline import answer
        service, sid = self.quick_report_case(two_questions=True)
        def partial(request):
            payload = json.loads(request["input"][-1]["content"])
            if payload["question_key"] == "2":
                raise httpx.ReadTimeout("synthetic question failure")
            return answer(payload)
        self.answers["quick.summary"] = partial
        events = await self.consume(service.report_stream(sid, object()), sid=sid)
        self.assertIn("report_done", [e["type"] for e in events])
        original = deepcopy(sessions.get_session(sid)["report_versions"][0])
        self.assertEqual(original["report_status"], "partial")
        success = deepcopy(next(q for q in original["quick_summary"]["questions"] if q["status"] == "complete"))
        await self.publish("b")
        self.calls.clear()
        self.answers["quick.summary"] = lambda request: answer(json.loads(request["input"][-1]["content"]))
        events = await self.consume(service.report_stream(sid, object(), retry_failed=True, base_version=1), sid=sid)
        self.assertIn("report_done", [e["type"] for e in events])
        saved = sessions.get_session(sid)
        completed = saved["report_versions"][0]
        self.assertEqual(len(saved["report_versions"]), 1)
        self.assertEqual(completed["report_status"], "complete")
        self.assertEqual(completed["input_snapshot"], original["input_snapshot"])
        self.assertEqual(next(q for q in completed["quick_summary"]["questions"] if q["question_key"] == success["question_key"]), success)
        self.assertEqual(completed["llm_model_snapshot"], original["llm_model_snapshot"])
        history_store_version = service._load_history()[0]["report_versions"][0]
        self.assertEqual(history_store_version, completed)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.calls[0]["input"][-1]["content"])["question_key"], "2")
        self.assertTrue(self.calls[0]["model"].endswith("a-primary"))
        self.assert_usage(["quick.summary"], version=1)
        self.calls.clear()
        await self.consume(service.report_stream(sid, object(), retry_failed=True, base_version=1), sid=sid)
        self.assertEqual(self.calls, [])
        self.assertEqual(service._load_history()[0]["report_versions"][0], history_store_version)

    async def test_inflight_fallback_stays_old_and_next_business_operation_uses_new(self):
        from app.services import survey_service as service
        from tests.test_survey_planner_direct_llm import _plan_json
        self.answers["survey.plan"] = _plan_json()
        self.failures["survey.plan"] = "503"
        self.block_stage = "survey.plan"
        sid_a = self.session(rows=[["段位", "主玩位置", "界面满意度"], ["金", "中", "4"]])
        with patch.object(service, "audit_log", AsyncMock()), \
             patch.object(service, "_ensure_branch_rules", return_value=[]):
            running = asyncio.create_task(self.consume(service.plan_stream(sid_a, object()), sid=sid_a))
            try:
                await asyncio.wait_for(self.started.wait(), timeout=10)
                await self.publish("b")
                self.block_stage = None
                self.release.set()
                events_a = await asyncio.wait_for(running, timeout=15)
            finally:
                if not running.done():
                    running.cancel()
                await asyncio.gather(running, return_exceptions=True)
            self.failures.clear()
            sid_b = self.session(rows=[["段位", "主玩位置", "界面满意度"], ["银", "下", "3"]])
            events_b = await self.consume(service.plan_stream(sid_b, object()), sid=sid_b)
        for events in (events_a, events_b):
            self.assertIn("plan_ready", [item["type"] for item in events])
        self.assertEqual([item["model"].split("-plan-")[1] for item in self.calls],
                         ["a-primary", "a-backup", "b-primary"])
        self.assertEqual(self.usage_creation_errors, [], "Usage task creation failed")
        records = llm_usage.get_user_llm_usage(helpers.LOGIN, period="all")["records"]
        by_id = {item["reference_id"]: item for item in records}
        self.assertEqual({v["model_config_version"] for v in by_id[sid_a]["model_settings_used"]}, {1})
        self.assertEqual({v["model_config_version"] for v in by_id[sid_b]["model_settings_used"]}, {2})
        self.assertEqual(self.calls[0]["input"], self.calls[1]["input"])

    async def test_fresh_process_retries_only_missing_question_with_original_settings(self):
        from app.services import report_quick_pipeline as pipeline
        from tests.test_report_quick_pipeline import question, answer
        questions = [question("1", ["已完成问题的完整材料"]), question("2", [helpers.PRIVATE_MATERIAL])]
        before_hash = hashlib.sha256(json.dumps(questions, ensure_ascii=False).encode()).hexdigest()
        def first_run(request):
            payload = json.loads(request["input"][-1]["content"])
            if helpers.PRIVATE_MATERIAL in json.dumps(payload, ensure_ascii=False):
                raise httpx.ReadTimeout("synthetic process interruption")
            return answer(payload)
        self.answers["quick.summary"] = first_run
        recorder = llm_usage.start_llm_usage_task(helpers.LOGIN, category="survey", action="恢复前检查点")
        with bind_llm_api_key(helpers.FIXTURE_KEY), bind_llm_attempt_observer(recorder.on_attempt_event):
            partial = await pipeline.run_quick_pipeline(questions, collect=llm_client.collect_chat_completion)
        recorder.finish("failed")
        self.assertEqual(partial["report_status"], "partial")
        completed = deepcopy(partial["questions"][0])
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(partial["checkpoint"]["llm_model_snapshot"]["version"], 1)
        (self.folder / "restart-input.json").write_text(json.dumps({"questions": questions,
            "checkpoint": partial["checkpoint"]}, ensure_ascii=False), encoding="utf-8")
        await self.publish("b")
        child_env = {**os.environ, "DATA_DIR": str(self.folder),
            "LLM_MODEL_SETTINGS_FILE": str(self.folder / "models.json"),
            "LLM_API_KEY": helpers.FIXTURE_KEY, "LLM_API_BASE": "https://model-acceptance.invalid/v1",
            "PYTHONIOENCODING": "utf-8", "FEISHU_LOGIN_REQUIRED": "false"}
        process = await asyncio.create_subprocess_exec(sys.executable, "-B", "-c", RESTART_CODE,
            str(self.folder), cwd=str(Path(__file__).resolve().parents[1]), env=child_env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=45)
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
        line = next(value for value in stdout.decode("utf-8").splitlines() if value.startswith("RESTART_RESULT="))
        resumed = json.loads(line.partition("=")[2])
        self.assertNotEqual(resumed["pid"], os.getpid())
        self.assertEqual(resumed["result"]["report_status"], "complete")
        self.assertEqual(resumed["result"]["questions"][0], completed)
        self.assertEqual(resumed["result"]["llm_model_snapshot"]["version"], 1)
        self.assertEqual(resumed["first_count"], 1)
        self.assertEqual(len(resumed["calls"]), 1, "Complete checkpoint must not call again")
        self.assertTrue(resumed["calls"][0]["model"].endswith("-a-primary"))
        self.assertIn(helpers.PRIVATE_MATERIAL, json.dumps(resumed["calls"], ensure_ascii=False))
        self.assertEqual(hashlib.sha256(json.dumps(questions, ensure_ascii=False).encode()).hexdigest(), before_hash)

    async def test_missing_or_damaged_checkpoint_stops_before_transport(self):
        from app.services.report_quick_pipeline import run_quick_pipeline
        from tests.test_report_quick_pipeline import question
        saved = await settings.build_model_snapshot()
        damaged = deepcopy(saved)
        damaged["configuration"]["tasks"]["quick.summary"]["primary"]["max_tokens"] += 1
        for previous in ({"old_partial": True}, {"llm_model_snapshot": damaged}):
            with self.subTest(previous=list(previous)):
                with self.assertRaisesRegex(ValueError, "缺少模型配置|快照校验"):
                    await run_quick_pipeline([question()], checkpoint=previous, retry_failed=True,
                                             collect=llm_client.collect_chat_completion)
        self.assertEqual(self.calls, [])

    async def test_cancelling_quick_business_cleans_pending_http_without_fallback(self):
        from app.services.report_quick_pipeline import run_quick_pipeline
        from tests.test_report_quick_pipeline import question
        self.block_stage = "quick.summary"
        self.answers["quick.summary"] = "never returned"
        recorder = llm_usage.start_llm_usage_task(helpers.LOGIN, category="survey", action="取消快速报告")
        async def run():
            with bind_llm_api_key(helpers.FIXTURE_KEY), bind_llm_attempt_observer(recorder.on_attempt_event):
                return await run_quick_pipeline([question()], collect=llm_client.collect_chat_completion)
        running = asyncio.create_task(run())
        try:
            await asyncio.wait_for(self.started.wait(), timeout=10)
            running.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await running
            recorder.finish("cancelled")
            record = self.assert_usage(["quick.summary"], missing=1)
            self.assertEqual(record["status"], "cancelled")
            self.assertEqual(len(self.calls), 1)
        finally:
            self.release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_known_issue_comment_close_cancels_quote_worker(self):
        """MM-O01: closing the comment stream must reclaim its quote child."""
        from app.services import comment_run
        started = asyncio.Event()
        workers = []
        async def quotes(sess):
            workers.append(asyncio.current_task())
            started.set()
            await asyncio.Event().wait()
        sid = self.session(comment_long_candidates=["synthetic quote"])
        with patch.object(comment_run, "_select_comment_raw_quotes", quotes):
            stream = comment_run.comment_run_stream(sid, object())
            try:
                await anext(stream)
                await asyncio.wait_for(started.wait(), timeout=5)
                await stream.aclose()
                await asyncio.sleep(0)
                self.assertTrue(all(worker.done() for worker in workers), "quote worker still running after close")
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                await stream.aclose()

    async def test_comment_quote_cleanup_gap_also_reproduces_on_original_baseline(self):
        """Evidence that MM-O01 precedes model management, without a checkout."""
        from app.services import comment_run
        process = subprocess.run(["git", "show", "8f0bd89:app/services/comment_run.py"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, check=True, timeout=10)
        baseline = types.ModuleType("offline_comment_baseline")
        exec(compile(process.stdout.decode("utf-8"), "<baseline-comment-run>", "exec"), baseline.__dict__)
        for module in (baseline,):
            with self.subTest(module=module.__name__):
                workers = []
                started = asyncio.Event()
                async def quotes(sess):
                    workers.append(asyncio.current_task())
                    started.set()
                    await asyncio.Event().wait()
                sid = self.session(comment_long_candidates=["synthetic"])
                with patch.object(module, "_select_comment_raw_quotes", quotes):
                    stream = module.comment_run_stream(sid, object())
                    try:
                        await anext(stream)
                        await asyncio.wait_for(started.wait(), timeout=5)
                        await stream.aclose()
                        await asyncio.sleep(0)
                        self.assertEqual(len(workers), 1)
                        self.assertFalse(workers[0].done())
                    finally:
                        for worker in workers:
                            worker.cancel()
                        await asyncio.gather(*workers, return_exceptions=True)
                        await stream.aclose()


if __name__ == "__main__":
    unittest.main()
