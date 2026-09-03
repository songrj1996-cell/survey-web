import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

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


if __name__ == "__main__":
    unittest.main()
