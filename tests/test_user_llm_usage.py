import asyncio
import builtins
from contextlib import redirect_stdout
import errno
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, call, patch

import httpx
from fastapi import FastAPI

from app.core.llm_context import current_llm_attempt_observer
from app.routers import profile as profile_router
from app.services import llm_credentials, llm_usage
from app.storage import llm_usage as usage_storage


class UserLlmUsageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="user-llm-usage-")
        self.usage_file = os.path.join(self.temp_dir.name, "usage.json")
        self.usage_patch = patch.object(
            usage_storage,
            "USER_LLM_USAGE_FILE",
            self.usage_file,
        )
        self.usage_patch.start()
        self.first = {
            "open_id": "ou_usage_first",
            "email": "first@example.com",
            "name": "First",
        }
        self.second = {
            "open_id": "ou_usage_second",
            "email": "second@example.com",
            "name": "Second",
        }

    def tearDown(self):
        self.usage_patch.stop()
        self.temp_dir.cleanup()

    async def test_attempts_aggregate_fallback_and_missing_usage(self):
        recorder = llm_usage.start_llm_usage_task(
            self.first,
            category="survey",
            action="报告生成",
            title="体验问卷.xlsx",
            reference_id="session-a",
            history_id="session-a",
        )
        self.assertIsNotNone(recorder)
        await recorder.on_attempt_event({
            "status": "started",
            "call_id": "call-1",
            "model": "model-primary",
            "fallback": False,
        })
        await recorder.on_attempt_event({
            "status": "completed",
            "call_id": "call-1",
            "model": "model-primary",
            "response_model": "model-primary-actual",
            "fallback": False,
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "usage_complete": True,
        })
        await recorder.on_attempt_event({
            "status": "started",
            "call_id": "call-2",
            "model": "model-fallback",
            "fallback": True,
        })
        await recorder.on_attempt_event({
            "status": "failed",
            "call_id": "call-2",
            "model": "model-fallback",
            "fallback": True,
            "usage": None,
            "usage_complete": False,
        })
        recorder.finish("completed")

        payload = llm_usage.get_user_llm_usage(self.first, period="all")
        self.assertEqual(payload["summary"]["task_count"], 1)
        self.assertEqual(payload["summary"]["total_tokens"], 15)
        self.assertEqual(payload["summary"]["call_count"], 2)
        self.assertEqual(payload["summary"]["usage_missing_call_count"], 1)
        record = payload["records"][0]
        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["input_tokens"], 10)
        self.assertEqual(record["output_tokens"], 5)
        self.assertIn("model-primary-actual", record["models_used"])
        self.assertEqual(record["fallback_models_used"], ["model-fallback"])
        self.assertEqual(record["history_id"], "session-a")

    async def test_stream_contexts_are_isolated_and_preserve_failure_status(self):
        first_request = object()
        second_request = object()

        async def current_login(request):
            return self.first if request is first_request else self.second

        async def source(model, tokens, *, fail=False):
            observer = current_llm_attempt_observer()
            self.assertIsNotNone(observer)
            await observer({
                "status": "started",
                "call_id": f"{model}-call",
                "model": model,
                "fallback": False,
            })
            await asyncio.sleep(0)
            await observer({
                "status": "completed",
                "call_id": f"{model}-call",
                "model": model,
                "fallback": False,
                "usage": {
                    "input_tokens": tokens - 1,
                    "output_tokens": 1,
                    "total_tokens": tokens,
                },
                "usage_complete": True,
            })
            if fail:
                raise RuntimeError("workflow failed")
            yield "done"

        async def consume(request, model, tokens, *, fail=False):
            chunks = []
            try:
                async for chunk in llm_credentials.stream_with_llm_api_key(
                    source(model, tokens, fail=fail),
                    f"key-{model}",
                    request=request,
                    category="comment",
                    action="评论分析",
                    title=f"{model}.csv",
                ):
                    chunks.append(chunk)
            except RuntimeError:
                return "failed"
            return "".join(chunks)

        with patch.object(
            llm_credentials,
            "_current_login",
            new=AsyncMock(side_effect=current_login),
        ):
            results = await asyncio.gather(
                consume(first_request, "model-a", 21),
                consume(second_request, "model-b", 34, fail=True),
            )

        self.assertEqual(results, ["done", "failed"])
        first_payload = llm_usage.get_user_llm_usage(self.first, period="all")
        second_payload = llm_usage.get_user_llm_usage(self.second, period="all")
        self.assertEqual(first_payload["summary"]["total_tokens"], 21)
        self.assertEqual(second_payload["summary"]["total_tokens"], 34)
        self.assertEqual(first_payload["records"][0]["status"], "completed")
        self.assertEqual(second_payload["records"][0]["status"], "failed")
        self.assertEqual(first_payload["records"][0]["models_used"], ["model-a"])
        self.assertEqual(second_payload["records"][0]["models_used"], ["model-b"])

    async def test_cancelled_stream_is_recorded(self):
        request = object()

        async def cancelled_source():
            raise asyncio.CancelledError()
            yield "unreachable"

        with patch.object(
            llm_credentials,
            "_current_login",
            new=AsyncMock(return_value=self.first),
        ):
            with self.assertRaises(asyncio.CancelledError):
                async for _ in llm_credentials.stream_with_llm_api_key(
                    cancelled_source(),
                    "cancel-key",
                    request=request,
                    category="annotate",
                    action="回答质量识别",
                ):
                    pass

        payload = llm_usage.get_user_llm_usage(self.first, period="all")
        self.assertEqual(payload["records"][0]["status"], "cancelled")

    def test_filters_pagination_and_storage_do_not_expose_identity(self):
        for index, (category, status) in enumerate((
            ("survey", "completed"),
            ("comment", "failed"),
            ("survey", "completed"),
        )):
            recorder = llm_usage.start_llm_usage_task(
                self.first,
                category=category,
                action=f"task-{index}",
                title=f"title-{index}",
            )
            recorder.finish(status)
        other = llm_usage.start_llm_usage_task(
            self.second,
            category="survey",
            action="other-user-task",
        )
        other.finish("completed")

        first_page = llm_usage.get_user_llm_usage(
            self.first,
            period="all",
            category="survey",
            status="completed",
            limit=1,
        )
        self.assertEqual(first_page["summary"]["task_count"], 3)
        self.assertEqual(first_page["total_records"], 2)
        self.assertEqual(len(first_page["records"]), 1)
        self.assertEqual(first_page["next_offset"], 1)
        second_page = llm_usage.get_user_llm_usage(
            self.first,
            period="all",
            category="survey",
            status="completed",
            offset=first_page["next_offset"],
            limit=1,
        )
        self.assertIsNone(second_page["next_offset"])

        with open(self.usage_file, "r", encoding="utf-8") as file:
            raw = file.read()
        self.assertNotIn("first@example.com", raw)
        self.assertNotIn("ou_usage_first", raw)
        self.assertNotIn("second@example.com", raw)

    async def test_profile_endpoint_returns_only_requested_user_payload(self):
        expected = {
            "period": "7d",
            "summary": {"total_tokens": 42},
            "records": [],
        }
        with (
            patch.object(
                profile_router,
                "_profile_login",
                new=AsyncMock(return_value=self.first),
            ),
            patch.object(
                profile_router,
                "get_user_llm_usage",
                return_value=expected,
            ) as query,
        ):
            result = await profile_router.get_profile_llm_usage(
                object(),
                period="7d",
                category="survey",
                status="completed",
                offset=0,
                limit=20,
            )

        self.assertEqual(result, expected)
        query.assert_called_once_with(
            self.first,
            period="7d",
            category="survey",
            status="completed",
            offset=0,
            limit=20,
        )


class UsagePersistenceFailureTests(unittest.IsolatedAsyncioTestCase):
    """Real isolated storage, simulated filesystem failures, no model requests."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="usage-failure-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "usage.json"
        self.patch(usage_storage, "USER_LLM_USAGE_FILE", str(self.path))
        self.patch(httpx.AsyncHTTPTransport, "handle_async_request",
                   side_effect=AssertionError("External HTTP forbidden"))
        self.login = {"email": "private@example.test", "open_id": "private-owner-id", "name": "离线验收员"}
        self.logs = io.StringIO()
        self.output = redirect_stdout(self.logs)
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def patch(self, target, name, *args, **kwargs):
        context = patch.object(target, name, *args, **kwargs)
        result = context.start()
        self.addCleanup(context.stop)
        return result

    def create(self, reference):
        recorder = llm_usage.start_llm_usage_task(self.login, category="survey",
            action="合成用量检查", title="PRIVATE_REPORT_MATERIAL", reference_id=reference)
        recorder.finish("completed")
        return recorder

    def references(self):
        page = llm_usage.get_user_llm_usage(self.login, period="all")
        return {record["reference_id"] for record in page["records"]}

    def assert_safe_logs(self):
        text = self.logs.getvalue()
        for secret in (self.login["email"], self.login["open_id"], "PRIVATE_REPORT_MATERIAL",
                       "offline-private-key", self.directory.name):
            self.assertNotIn(secret, text)
        self.assertRegex(text, r"\d{4}-\d\d-\d\d .*\+08:00")
        self.assertIn("用量记录", text)
        self.assertIn("usage.json", text)

    async def query_endpoint(self):
        app = FastAPI()
        app.include_router(profile_router.router)
        with patch.object(profile_router, "_profile_login", AsyncMock(return_value=self.login)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://offline.test") as client:
                return await client.get("/api/profile/llm-usage?period=all")

    async def test_read_failure_returns_503_and_recovers_without_zero_usage(self):
        self.create("A")
        original = self.path.read_bytes()
        with patch.object(usage_storage, "open", side_effect=PermissionError(errno.EACCES, "PRIVATE_REPORT_MATERIAL"), create=True):
            response = await self.query_endpoint()
        self.assertEqual(response.status_code, 503)
        self.assertIn("暂时无法读取用量", response.json()["detail"])
        self.assertNotIn("summary", response.json())
        self.assertEqual(self.path.read_bytes(), original)
        recovered = await self.query_endpoint()
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.json()["total_records"], 1)
        self.assert_safe_logs()

    def test_read_failure_during_mutation_never_overwrites_previous_records(self):
        self.create("A")
        original = self.path.read_bytes()
        with patch.object(usage_storage, "open", side_effect=PermissionError(errno.EACCES, "denied"), create=True), \
             patch.object(usage_storage.os, "replace", wraps=os.replace) as replace:
            with self.assertRaises(RuntimeError):
                self.create("B")
            replace.assert_not_called()
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.references(), {"A"})
        self.assert_safe_logs()

    async def test_failed_creation_keeps_business_result_and_emits_diagnostic(self):
        self.create("A")
        original = self.path.read_bytes()
        calls = []
        async def business():
            calls.append("business")
            return "原业务结果"
        with patch.object(llm_credentials, "_current_login", AsyncMock(return_value=self.login)), \
             patch.object(usage_storage.os, "replace", side_effect=PermissionError(errno.EACCES, "offline-private-key")):
            result = await llm_credentials.run_with_llm_api_key(business(), "offline-private-key",
                request=object(), category="survey", action="合成检查", reference_id="B", title="合成任务")
        self.assertEqual(result, "原业务结果")
        self.assertEqual(calls, ["business"])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.references(), {"A"})
        self.assertIn("保存", self.logs.getvalue())
        self.assertIn("PermissionError", self.logs.getvalue())
        self.assert_safe_logs()

    def test_missing_file_and_legacy_records_remain_supported(self):
        self.assertEqual(usage_storage.load_llm_usage(), {})
        self.assertFalse(self.path.exists())
        self.path.write_text(json.dumps({"owners": {"legacy": [{"id": "old", "total_tokens": 17}]}}), encoding="utf-8")
        self.create("A")
        self.assertEqual(usage_storage.load_llm_usage()["legacy"], [{"id": "old", "total_tokens": 17}])
        self.assertEqual(self.references(), {"A"})

    def test_corrupt_or_invalid_structure_is_not_retried_or_replaced(self):
        samples = [b'{"owners":', b'\xff', b'null', b'[]', b'{}', b'{"owners": []}',
                   b'{"owners": {"a": {}}}', b'{"owners": {"a": [null]}}']
        for payload in samples:
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                with patch.object(usage_storage, "sleep") as sleep, \
                     patch.object(usage_storage.os, "replace") as replace:
                    with self.assertRaises(usage_storage.UsageStorageError):
                        self.create("B")
                sleep.assert_not_called()
                replace.assert_not_called()
                self.assertEqual(self.path.read_bytes(), payload)
        self.assert_safe_logs()

    def test_single_read_permission_error_recovers_without_losing_old_records(self):
        self.create("A")
        count = 0
        def read(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                raise PermissionError(errno.EACCES, "denied")
            return builtins.open(*args, **kwargs)
        with patch.object(usage_storage, "open", read, create=True), patch.object(usage_storage, "sleep") as sleep:
            self.create("B")
        self.assertEqual(self.references(), {"A", "B"})
        sleep.assert_called_once_with(0.02)
        self.assertIn("重试后恢复", self.logs.getvalue())

    def test_read_and_replace_retry_budgets_are_bounded(self):
        self.create("A")
        real_replace = os.replace
        read_count = replace_count = 0
        def read(*args, **kwargs):
            nonlocal read_count
            read_count += 1
            if read_count < 3:
                raise PermissionError(errno.EACCES, "denied")
            return builtins.open(*args, **kwargs)
        def replace(*args, **kwargs):
            nonlocal replace_count
            replace_count += 1
            if replace_count < 3:
                raise PermissionError(errno.EACCES, "denied")
            return real_replace(*args, **kwargs)
        with patch.object(usage_storage, "open", read, create=True), \
             patch.object(usage_storage.os, "replace", replace), patch.object(usage_storage, "sleep") as sleep:
            llm_usage.start_llm_usage_task(self.login, category="survey", action="B", title="合成任务", reference_id="B")
        self.assertEqual(read_count, 3)
        self.assertEqual(replace_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.02), call(0.05), call(0.02), call(0.05)])
        self.assertAlmostEqual(sum(c.args[0] for c in sleep.call_args_list), 0.14)
        self.assertEqual(self.references(), {"A", "B"})
        self.assertEqual(llm_usage.get_user_llm_usage(self.login, period="all")["total_records"], 2)

    def test_persistent_read_failure_stops_after_three_attempts(self):
        self.create("A")
        original = self.path.read_bytes()
        with patch.object(usage_storage, "open", side_effect=PermissionError(errno.EACCES, "denied"), create=True) as read, \
             patch.object(usage_storage, "sleep") as sleep:
            with self.assertRaises(llm_usage.UsageUnavailableError):
                llm_usage.get_user_llm_usage(self.login, period="all")
        self.assertEqual(read.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.02), call(0.05)])
        self.assertEqual(self.path.read_bytes(), original)

    def test_persistent_replace_failure_preserves_file_and_cleans_only_temp(self):
        self.create("A")
        original = self.path.read_bytes()
        with patch.object(usage_storage.os, "replace", side_effect=PermissionError(errno.EACCES, "denied")) as replace, \
             patch.object(usage_storage, "sleep") as sleep:
            with self.assertRaises(usage_storage.UsageStorageError):
                self.create("B")
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.02), call(0.05)])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_nonretryable_disk_failure_is_reported_without_retry(self):
        self.create("A")
        original = self.path.read_bytes()
        for operation in ("replace", "fsync"):
            with self.subTest(operation=operation), \
                 patch.object(usage_storage.os, operation, side_effect=OSError(errno.ENOSPC, "PRIVATE_REPORT_MATERIAL")) as failure, \
                 patch.object(usage_storage, "sleep") as sleep:
                with self.assertRaises(usage_storage.UsageStorageError):
                    self.create("B")
                self.assertEqual(failure.call_count, 1)
                sleep.assert_not_called()
                self.assertEqual(self.path.read_bytes(), original)
        self.assertIn("OSError", self.logs.getvalue())
        self.assert_safe_logs()

    async def test_attempt_write_can_recover_without_double_counting(self):
        recorder = self.create("A")
        recorder = llm_usage.start_llm_usage_task(self.login, category="survey", action="B", title="合成任务", reference_id="B")
        real_replace = os.replace
        count = 0
        def replace(*args):
            nonlocal count
            count += 1
            if count == 1:
                raise PermissionError(errno.EACCES, "denied")
            return real_replace(*args)
        with patch.object(usage_storage.os, "replace", replace), patch.object(usage_storage, "sleep"):
            await recorder.on_attempt_event({"call_id": "call-B", "model": "fake-model", "status": "started"})
        await recorder.on_attempt_event({"call_id": "call-B", "model": "fake-model", "status": "completed",
            "usage": None, "usage_complete": False})
        recorder.finish("completed")
        record = llm_usage.get_user_llm_usage(self.login, period="all")["records"][0]
        self.assertEqual(record["call_count"], 1)
        self.assertEqual(record["usage_missing_call_count"], 1)
        self.assertEqual(record["usage_reported_call_count"], 0)
        self.assertEqual(self.references(), {"A", "B"})

    async def test_finish_write_failure_does_not_replace_business_result(self):
        calls = []
        async def business():
            calls.append("business")
            return "合成完整报告"
        real_replace = os.replace
        count = 0
        def replace(*args):
            nonlocal count
            count += 1
            if count > 1:
                raise PermissionError(errno.EACCES, "denied")
            return real_replace(*args)
        with patch.object(llm_credentials, "_current_login", AsyncMock(return_value=self.login)), \
             patch.object(usage_storage.os, "replace", replace), patch.object(usage_storage, "sleep"):
            result = await llm_credentials.run_with_llm_api_key(business(), "offline-private-key",
                request=object(), category="survey", action="合成检查", reference_id="B", title="合成任务")
        self.assertEqual(result, "合成完整报告")
        self.assertEqual(calls, ["business"])
        self.assertEqual(count, 4)  # One insert plus three finish-persistence attempts.
        self.assertEqual(self.references(), {"B"})
        # Persistent failure is diagnosed, not falsely represented as completed.
        self.assertEqual(llm_usage.get_user_llm_usage(self.login, period="all")["records"][0]["status"], "running")
        self.assertIn("保存", self.logs.getvalue())
        self.assert_safe_logs()


if __name__ == "__main__":
    unittest.main()
