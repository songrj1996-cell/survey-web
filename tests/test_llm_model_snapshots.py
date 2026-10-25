"""Offline snapshot isolation, recovery and report provenance tests."""
import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from app.core.llm_context import bind_llm_model_snapshot, current_llm_model_snapshot
from app.core.llm_model_registry import fingerprint
from app.services import llm_model_settings as settings, report_versions
from tests.test_llm_model_routing import snapshot


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_publication_during_operation_does_not_replace_frozen_version(self):
        old, new = snapshot(1), snapshot(2, "new-primary", "new-backup")
        collect = AsyncMock(return_value=("ok", "synthetic"))
        current = old
        async def load():
            return deepcopy(current)
        @settings.model_operation
        async def operation():
            nonlocal current
            await settings.call_model(collect, "survey.plan", [])
            current = new
            await asyncio.create_task(settings.call_model(collect, "survey.report", []))
            output = {}
            settings.record_model_snapshot(output)
            return output
        with patch.object(settings, "build_model_snapshot", load):
            output = await operation()
            await settings.call_model(collect, "survey.plan", [])
        self.assertEqual([c.kwargs["model_config_version"] for c in collect.await_args_list], [1, 1, 2])
        self.assertEqual(output["llm_model_snapshot"]["configuration_sha256"], old["configuration_sha256"])
        self.assertIsNone(current_llm_model_snapshot())

    async def test_resume_uses_saved_version_after_publication_and_rejects_corruption(self):
        collect = AsyncMock(return_value=("ok", "test-primary"))
        @settings.model_operation(resume_from="previous", persist_result=True)
        async def operation(previous=None):
            await settings.call_model(collect, "insight.writer", [])
            return {}
        with patch.object(settings, "build_model_snapshot", AsyncMock(return_value=snapshot(2))):
            result = await operation(previous={"llm_model_snapshot": snapshot(1)})
            self.assertEqual(result["llm_model_snapshot"]["version"], 1)
            self.assertEqual(collect.call_args.kwargs["model_config_version"], 1)
            damaged = snapshot(1)
            damaged["configuration"]["tasks"]["insight.writer"]["primary"]["max_tokens"] += 1
            with self.assertRaisesRegex(ValueError, "快照校验"):
                await operation(previous={"llm_model_snapshot": damaged})
            with self.assertRaisesRegex(ValueError, "缺少模型配置"):
                await operation(previous={"draft": "old without provenance"})
        self.assertEqual(collect.await_count, 1)

    async def test_concurrent_operations_do_not_share_settings(self):
        async def run(saved):
            with bind_llm_model_snapshot(saved):
                await asyncio.sleep(0)
                collect = AsyncMock(return_value=("ok", "synthetic"))
                await settings.call_model(collect, "survey.report", [])
                return collect.call_args.kwargs["models"]
        left, right = await asyncio.gather(run(snapshot(1)), run(snapshot(2, "other", "other-backup")))
        self.assertEqual(left[0], "test-primary")
        self.assertEqual(right[0], "other")

    async def test_stream_can_be_advanced_and_closed_in_different_tasks(self):
        @settings.model_operation
        async def stream():
            yield current_llm_model_snapshot()["version"]
            await asyncio.sleep(0)
            yield current_llm_model_snapshot()["version"]
        with patch.object(settings, "build_model_snapshot", AsyncMock(return_value=snapshot(1))):
            iterator = stream()
            self.assertEqual(await anext(iterator), 1)
            self.assertIsNone(current_llm_model_snapshot())
            with bind_llm_model_snapshot(snapshot(2)):
                self.assertEqual(await asyncio.create_task(anext(iterator)), 1)
                await asyncio.create_task(iterator.aclose())
                self.assertEqual(current_llm_model_snapshot()["version"], 2)

    async def test_restoring_report_version_preserves_its_own_model_snapshot(self):
        source = {}
        with bind_llm_model_snapshot(snapshot(1)):
            report_versions.append_report_version(source, {"report_md": "# first"})
        with bind_llm_model_snapshot(snapshot(2)):
            report_versions.append_report_version(source, {"report_md": "# second"})
        self.assertEqual(report_versions.resolve_report_version(source, 1)["llm_model_snapshot"]["version"], 1)
        self.assertEqual(report_versions.resolve_report_version(source, 2)["llm_model_snapshot"]["version"], 2)
        self.assertEqual(source["llm_model_snapshot"]["version"], 2)

    async def test_unknown_writer_capacity_remains_advisory(self):
        from app.services.report_writer_capacity import estimate_writer_capacity
        saved = snapshot()
        with bind_llm_model_snapshot(saved):
            advice = estimate_writer_capacity([{"content": "complete input"}], model="test-primary",
                                              output_tokens=4096, estimate_input=lambda _: 1000)
            self.assertEqual(advice["context_tokens"], 100000)
            unknown = estimate_writer_capacity([], model="absent", output_tokens=4096, estimate_input=lambda _: 0)
            self.assertEqual(unknown["status"], "unknown")

    async def test_quick_checkpoint_reuses_original_configuration_after_publication(self):
        from app.services import report_quick_pipeline
        from tests.test_report_quick_pipeline import question, answer
        import json
        async def collect(messages, **options):
            return answer(json.loads(messages[-1]["content"])), options["models"][0]
        mock = AsyncMock(side_effect=collect)
        with bind_llm_model_snapshot(snapshot(1)):
            first = await report_quick_pipeline.run_quick_pipeline([question()], collect=mock)
        self.assertEqual(first["report_status"], "complete")
        calls = mock.await_count
        with bind_llm_model_snapshot(snapshot(2, "new", "new-backup")):
            resumed = await report_quick_pipeline.run_quick_pipeline([question()], collect=mock,
                checkpoint=first["checkpoint"], retry_failed=True)
        self.assertEqual(resumed["llm_model_snapshot"]["version"], 1)
        self.assertEqual(mock.await_count, calls)

    async def test_annotation_missing_rows_reuse_stage_snapshot(self):
        from app.services import annotate_workflow as annotate
        sess = {"rows": []}
        seen = []
        async def fake_stream(*args):
            seen.append(current_llm_model_snapshot()["version"])
            yield "synthetic result"
        with patch.object(annotate, "get_annotate_session", return_value=sess), \
             patch.object(annotate, "_ai_detect_stream", fake_stream), \
             patch.object(annotate, "_quality_stream", fake_stream):
            with bind_llm_model_snapshot(snapshot(1)):
                [item async for item in annotate.ai_detect_stream("synthetic", None)]
            with bind_llm_model_snapshot(snapshot(2)):
                [item async for item in annotate.ai_detect_stream("synthetic", None, {"r1"})]
                [item async for item in annotate.quality_stream("synthetic", None)]
        self.assertEqual(seen, [1, 1, 2])
        self.assertEqual(sess["llm_model_stage_snapshots"]["ai"]["llm_model_snapshot"]["version"], 1)

    async def test_old_partial_annotation_without_snapshot_stops_before_model(self):
        from app.services import annotate_workflow as annotate
        sess = {"ai_results": [{"id": "old-result"}]}
        inner = AsyncMock()
        with patch.object(annotate, "get_annotate_session", return_value=sess), \
             patch.object(annotate, "_ai_detect_stream", inner), bind_llm_model_snapshot(snapshot(2)):
            events = [item async for item in annotate.ai_detect_stream("synthetic", None, {"r2"})]
        self.assertIn("缺少模型配置快照", events[0])
        inner.assert_not_called()

    async def test_legacy_version_never_borrows_current_versions_model_snapshot(self):
        source = {"report_md": "# legacy"}
        with bind_llm_model_snapshot(snapshot(2)):
            report_versions.append_report_version(source, {"report_md": "# new"})
        self.assertNotIn("llm_model_snapshot", report_versions.resolve_report_version(source, 1))
        self.assertEqual(report_versions.resolve_report_version(source, 2)["llm_model_snapshot"]["version"], 2)

    async def test_queued_background_annotation_and_retry_keep_original_settings(self):
        from tests.test_group_tasks import WorkflowCase, LOGIN
        from app.schemas.group_tasks import RunTask
        from app.services import group_task_runner as runner
        from app.storage import group_workflow as store
        fixture = WorkflowCase()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        task = fixture.make_task(labels=(None,))
        seen = []
        async def fail(current, row, checkpoint):
            seen.append(current_llm_model_snapshot()["version"])
            raise RuntimeError("synthetic failure")
        with patch.object(runner, "require_background_llm_api_key", return_value="fixture-key"), \
             patch.object(runner, "start_llm_usage_task", return_value=None), \
             patch.object(runner, "annotate_group_feedback", fail):
            with bind_llm_model_snapshot(snapshot(1)):
                await runner.prepare_run(task["id"], RunTask(expected_version=task["version"]), LOGIN)
            with bind_llm_model_snapshot(snapshot(2)):
                await runner._run_slice(task["id"], asyncio.Semaphore(1))
                await runner.prepare_run(task["id"], RunTask(expected_version=task["version"]), LOGIN)
        self.assertEqual(seen, [1])
        saved = store.read("tasks", task["id"])
        self.assertEqual(saved["run"]["llm_model_snapshot"]["version"], 1)


if __name__ == "__main__":
    unittest.main()
