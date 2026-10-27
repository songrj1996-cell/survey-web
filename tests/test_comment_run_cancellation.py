"""Offline quote-worker ownership on every comment-analysis exit path."""
import asyncio
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

import httpx

from app.services import comment_run


class CommentCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.logs = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.logs))
        self.session = {"comment_long_candidates": ["PRIVATE_QUOTE_MATERIAL"],
                        "owner_email": "private@example.test"}
        self.result = {"report_md": "# 合成主报告\n\n完整主报告和来源。"}
        self.quotes = [{"text": "合成原文", "translation": "合成译文"}]
        self.workers = []
        self.worker_started = asyncio.Event()
        self.worker_stopped = asyncio.Event()
        self.quote_release = asyncio.Event()
        self.main_waiting = asyncio.Event()
        self.main_release = asyncio.Event()
        self.main_saved = asyncio.Event()
        self.block_main = False
        self.no_result = False
        self.main_error = False
        self.quote_error = False
        self.saves = []
        self.history = []
        self.stack.enter_context(patch.object(comment_run, "get_session", return_value=self.session))
        self.stack.enter_context(patch.object(comment_run, "_comment_sample_note_md", return_value=""))
        self.stack.enter_context(patch.object(comment_run, "_comment_report_title", return_value="离线报告"))
        self.stack.enter_context(patch.object(comment_run, "_comment_append_selected_raw_comments",
            side_effect=lambda report, quotes: report + "\n\n精选原文：合成原文"))
        def save(sid, value):
            self.saves.append(deepcopy(value))
            self.main_saved.set()
        self.stack.enter_context(patch.object(comment_run, "save_session", side_effect=save))
        self.stack.enter_context(patch.object(comment_run, "save_to_history",
            side_effect=lambda sid, value: self.history.append(deepcopy(value))))
        self.stack.enter_context(patch.object(httpx.AsyncHTTPTransport, "handle_async_request",
            side_effect=AssertionError("No upstream HTTP permitted")))

        async def quotes(sess):
            self.workers.append(asyncio.current_task())
            self.worker_started.set()
            try:
                await self.quote_release.wait()
                if self.quote_error:
                    raise RuntimeError("synthetic quote failure")
                return deepcopy(self.quotes)
            finally:
                self.worker_stopped.set()
        async def pipeline(sess):
            yield "progress", "合成主流程开始"
            if self.block_main:
                self.main_waiting.set()
                await self.main_release.wait()
            if self.main_error:
                raise RuntimeError("synthetic pipeline failure")
            if not self.no_result:
                yield "result", deepcopy(self.result)
        self.stack.enter_context(patch.object(comment_run, "_select_comment_raw_quotes", side_effect=quotes))
        self.stack.enter_context(patch.object(comment_run, "_comment_analysis_pipeline", pipeline))

    async def asyncTearDown(self):
        # Failed pre-fix assertions must not leave orphan tasks in the test loop.
        for worker in self.workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)

    async def consume(self):
        return [json.loads(chunk[6:].strip())
                async for chunk in comment_run.comment_run_stream("synthetic-session", object())]

    def assert_clean(self):
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(self.worker_stopped.is_set(), "Quote worker has not exited")
        self.assertTrue(all(worker.done() for worker in self.workers))
        self.assertNotIn("PRIVATE_QUOTE_MATERIAL", self.logs.getvalue())
        self.assertNotIn("private@example.test", self.logs.getvalue())

    async def test_normal_completion_keeps_main_report_and_selected_quotes(self):
        self.quote_release.set()
        events = await self.consume()
        self.assertEqual([item["type"] for item in events].count("comment_done"), 1)
        self.assertEqual([item["type"] for item in events].count("comment_quotes_done"), 1)
        self.assertEqual(self.session["comment_selected_raw_comments"], self.quotes)
        self.assertTrue(self.history[-1]["report_md"].startswith(self.result["report_md"]))
        self.assertIn("精选原文", self.history[-1]["report_md"])
        self.assertEqual(len(self.saves), 2)
        self.assert_clean()

    async def test_close_after_first_progress_reclaims_quote_worker(self):
        stream = comment_run.comment_run_stream("synthetic-session", object())
        try:
            await anext(stream)
            await asyncio.wait_for(self.worker_started.wait(), 2)
            await stream.aclose()
            self.assert_clean()
            self.assertEqual(self.saves, [])
            self.assertRegex(self.logs.getvalue(), r"\d{4}-\d\d-\d\d .*\+08:00")
            self.assertIn("停止", self.logs.getvalue())
        finally:
            await stream.aclose()

    async def test_cancel_during_main_processing_reclaims_quote_worker(self):
        self.block_main = True
        consumer = asyncio.create_task(self.consume())
        try:
            await asyncio.wait_for(self.main_waiting.wait(), 2)
            await asyncio.wait_for(self.worker_started.wait(), 2)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assert_clean()
            self.assertEqual(self.saves, [])
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_cancel_while_awaiting_quotes_keeps_archived_main_report(self):
        consumer = asyncio.create_task(self.consume())
        try:
            await asyncio.wait_for(self.main_saved.wait(), 2)
            await asyncio.wait_for(self.worker_started.wait(), 2)
            consumer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await consumer
            self.assert_clean()
            self.assertEqual(self.history[-1]["report_md"], self.result["report_md"])
            self.assertEqual(self.session["report_md"], self.result["report_md"])
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_close_at_saved_report_boundary_keeps_archive_and_reclaims_worker(self):
        stream = comment_run.comment_run_stream("synthetic-session", object())
        try:
            while True:
                event = json.loads((await anext(stream))[6:].strip())
                if event["type"] == "comment_done":
                    break
            await asyncio.wait_for(self.worker_started.wait(), 2)
            await stream.aclose()
            self.assert_clean()
            self.assertEqual(self.history[-1]["report_md"], self.result["report_md"])
            self.assertEqual(len(self.history), 1)
        finally:
            await stream.aclose()

    async def test_no_main_result_reclaims_unfinished_quote_worker(self):
        self.no_result = True
        # Ensure quote work has actually begun before the no-result exit.
        self.block_main = True
        consumer = asyncio.create_task(self.consume())
        try:
            await asyncio.wait_for(self.worker_started.wait(), 2)
            await asyncio.wait_for(self.main_waiting.wait(), 2)
            self.main_release.set()
            events = await consumer
            self.assertIn("error", [item["type"] for item in events])
            self.assert_clean()
            self.assertEqual(self.saves, [])
        finally:
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_main_exception_is_reported_after_quote_worker_cleanup(self):
        self.main_error = True
        self.block_main = True
        stream = comment_run.comment_run_stream("synthetic-session", object())
        try:
            await anext(stream)
            await asyncio.wait_for(self.worker_started.wait(), 2)
            await anext(stream)
            self.main_release.set()
            event = json.loads((await anext(stream))[6:].strip())
            self.assertEqual(event["type"], "error")
            self.assert_clean()
        finally:
            await stream.aclose()

    async def test_quote_failure_preserves_main_report_and_original_error_event(self):
        self.quote_error = True
        self.quote_release.set()
        events = await self.consume()
        self.assertIn("comment_done", [item["type"] for item in events])
        self.assertIn("comment_quotes_error", [item["type"] for item in events])
        self.assertEqual(self.history[-1]["report_md"], self.result["report_md"])
        self.assert_clean()

    async def test_completed_quote_at_close_boundary_is_not_cancelled_or_overwrites_report(self):
        stream = comment_run.comment_run_stream("synthetic-session", object())
        try:
            while json.loads((await anext(stream))[6:].strip())["type"] != "comment_done":
                pass
            await asyncio.wait_for(self.worker_started.wait(), 2)
            self.quote_release.set()
            await asyncio.gather(*self.workers)
            await stream.aclose()
            self.assert_clean()
            self.assertFalse(self.workers[0].cancelled())
            self.assertEqual(self.history[-1]["report_md"], self.result["report_md"])
        finally:
            await stream.aclose()


if __name__ == "__main__":
    unittest.main()
