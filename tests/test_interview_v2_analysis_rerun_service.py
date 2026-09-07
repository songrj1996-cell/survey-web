import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.core import security
from app.services import interview_v2_analysis_rerun_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError
from app.storage import interview_v2_store as store
from tests import test_interview_v2_analysis_rerun as f
from tests import test_interview_v2_analysis_rerun_store as support


def payload(module_id=f.MODULE_A):
    return {
        "from_stage": "analysis_module", "base_analysis_run_id": f.BASE, "module_id": module_id,
        "preserve_manual_report_edits": True, "reuse_unchanged_artifacts": True, "force": False,
    }


class AnalysisModuleRerunServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iv2-module-service-")
        self.addCleanup(self.temp.cleanup)
        self.patch(store.config, "INTERVIEW_V2_DATA_DIR", Path(self.temp.name))
        self.patch(security, "FEISHU_LOGIN_REQUIRED", True)
        self.ready, self.inputs, self.base = support.seed_project()
        self.patch(service, "_ready_project", self.ready)
        self.prompt = self.patch(service, "_prompt_bundle", ("test analysis prompt", deepcopy(f.PROMPTS)))
        self.model = self.patch(service, "collect_chat_completion", mock=AsyncMock(side_effect=self.output))

    def patch(self, target, name, value=None, *, mock=None):
        p = patch.object(target, name, new=mock) if mock is not None else (
            patch.object(target, name, return_value=value) if name.startswith("_") else patch.object(target, name, value)
        )
        result = p.start()
        self.addCleanup(p.stop)
        return result

    async def output(self, messages, **_kwargs):
        raw = messages[-1]["content"].split("\n", 1)[1].rsplit("\n", 1)[0]
        module_input = json.loads(raw)
        return json.dumps(f.raw_output(module_input, "重跑发现"), ensure_ascii=False), "rerun-model"

    async def run_rerun(self, key="service-key", request=None, login=None):
        return await service.create_analysis_module_rerun(f.PROJECT, request or payload(), login or f.LOGIN, key)

    async def test_one_model_call_reuses_other_module_and_preserves_report_files(self):
        report_dir = Path(self.temp.name) / "projects" / f.PROJECT / "reports"
        report_dir.mkdir()
        report = report_dir / "approved.json"
        artifact = report_dir / "approved.docx"
        store._atomic_write_json(report, {"status": "approved", "sections": [{"locked": True, "content": "人工正文"}]})
        artifact.write_bytes(b"immutable-test-export")
        before = (report.read_bytes(), artifact.read_bytes())
        result = await self.run_rerun()
        self.model.assert_awaited_once()
        model_text = self.model.call_args.args[0][-1]["content"]
        self.assertIn(f.MODULE_A, model_text)
        self.assertNotIn(f.MODULE_B, model_text)
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["analysis_version_number"])
        self.assertTrue(result["is_current_version"])
        self.assertFalse(result["rerun"]["reports_rewritten"])
        self.assertEqual(before, (report.read_bytes(), artifact.read_bytes()))
        self.assertEqual(self.base, store.load_analysis_run(f.PROJECT, f.BASE)["revision"])
        old = next(x for x in self.base["findings"] if x["module_id"] == f.MODULE_B)
        new = next(x for x in result["findings"] if x["module_id"] == f.MODULE_B)
        self.assertEqual(old["statement"], new["statement"])

    async def test_same_key_replays_even_after_head_or_upstream_moves(self):
        first = await self.run_rerun()
        replay = await self.run_rerun()
        self.assertEqual(first["analysis_run_id"], replay["analysis_run_id"])
        self.assertTrue(replay["rerun"]["reused"])
        current = store.load_current_analysis_run(f.PROJECT)
        store.save_analysis_run_cas(project_id=f.PROJECT, base_analysis_run_id=first["analysis_run_id"], revision={**current["revision"], "analysis_run_id": f.NEXT})
        replay = await self.run_rerun()
        self.assertFalse(replay["is_current_version"])
        self.assertEqual("stale", replay["status"])
        self.assertEqual(2, replay["analysis_version_number"])
        self.model.assert_awaited_once()
        self.assertEqual(f.NEXT, store.load_current_analysis_run(f.PROJECT)["revision"]["analysis_run_id"])

    async def test_changed_scope_or_prompt_conflicts_without_new_model_call(self):
        await self.run_rerun()
        for request in (payload(f.MODULE_B), payload()):
            if request["module_id"] == f.MODULE_A:
                self.prompt.return_value = ("changed", {"prompt": {"version": 2}})
            with self.assertRaises(InterviewV2ImportError) as caught:
                await self.run_rerun(request=request)
            self.assertEqual("RERUN_IDEMPOTENCY_CONFLICT", caught.exception.code)
        self.model.assert_awaited_once()

    async def test_foreign_owner_or_module_denied_before_any_model_call(self):
        for request, login in ((payload(), {"email": "other@example.com"}), (payload("module_" + "0" * 32), f.LOGIN)):
            with self.assertRaises(InterviewV2ImportError) as caught:
                await self.run_rerun(request=request, login=login)
            self.assertEqual(404, caught.exception.status_code)
        self.model.assert_not_awaited()

    async def test_stale_base_rejected_before_llm_and_releases_claim(self):
        state_path = store._analysis_boundary_state_path(f.PROJECT)
        state = store._read_json(state_path)
        store._atomic_write_json(state_path, {**state, "is_stale": True})
        with self.assertRaises(InterviewV2ImportError) as caught:
            await self.run_rerun()
        self.assertEqual("ANALYSIS_INPUT_CHANGED", caught.exception.code)
        self.model.assert_not_awaited()
        store._atomic_write_json(state_path, state)
        self.assertEqual("completed", (await self.run_rerun())["status"])

    async def test_invalid_output_keeps_head_and_same_key_can_retry(self):
        self.model.side_effect = None
        self.model.return_value = (json.dumps({"module_id": f.MODULE_B, "findings": []}), "bad-model")
        with self.assertRaises(InterviewV2ImportError) as caught:
            await self.run_rerun()
        self.assertEqual("ANALYSIS_RERUN_MODEL_OUTPUT_INVALID", caught.exception.code)
        self.assertEqual(f.BASE, store.load_current_analysis_run(f.PROJECT)["revision"]["analysis_run_id"])
        self.model.side_effect = self.output
        self.assertEqual("completed", (await self.run_rerun())["status"])

    async def test_failure_and_cancellation_release_uncommitted_reservation(self):
        for failure in (RuntimeError("mock failure"), asyncio.CancelledError()):
            self.model.side_effect = failure
            with self.assertRaises((InterviewV2ImportError, asyncio.CancelledError)):
                await self.run_rerun()
            self.assertEqual(f.BASE, store.load_current_analysis_run(f.PROJECT)["revision"]["analysis_run_id"])
        self.model.side_effect = self.output
        self.assertEqual("completed", (await self.run_rerun())["status"])

    async def test_same_key_concurrent_request_never_duplicates_model_work(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.output(*args, **kwargs)

        self.model.side_effect = delayed
        first = asyncio.create_task(self.run_rerun())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            with self.assertRaises(InterviewV2ImportError) as caught:
                await self.run_rerun()
            self.assertEqual("RERUN_IN_PROGRESS", caught.exception.code)
        finally:
            release.set()
            result = await first
        self.assertEqual("completed", result["status"])
        self.model.assert_awaited_once()

    async def test_different_keys_race_only_one_result_advances_head(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0

        async def racing(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return await self.output(*args, **kwargs)

        self.model.side_effect = racing
        first = asyncio.create_task(self.run_rerun("race-a"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            winner = await self.run_rerun("race-b", payload(f.MODULE_B))
        finally:
            release.set()
        with self.assertRaises(InterviewV2ImportError) as caught:
            await first
        self.assertEqual("ANALYSIS_INPUT_CHANGED", caught.exception.code)
        state = store.load_current_analysis_run(f.PROJECT)["state"]
        self.assertEqual(2, state["current_version_number"])
        self.assertEqual(winner["analysis_run_id"], state["current_analysis_run_id"])

    async def test_commit_then_completion_failure_recovers_without_model_retry(self):
        original = store._atomic_write_json

        def fail_completion(path, value):
            if "operation_schema_version" in value and value.get("status") == "completed":
                raise OSError("mock completion write failure")
            return original(path, value)

        with patch.object(store, "_atomic_write_json", side_effect=fail_completion):
            with self.assertRaises(InterviewV2ImportError) as caught:
                await self.run_rerun()
        self.assertEqual("ANALYSIS_PERSISTENCE_FAILED", caught.exception.code)
        replay = await self.run_rerun()
        self.assertTrue(replay["rerun"]["reused"])
        self.model.assert_awaited_once()

    async def test_chained_module_reruns_keep_freeze_fingerprint_usable(self):
        first = await self.run_rerun()
        second_request = {**payload(f.MODULE_B), "base_analysis_run_id": first["analysis_run_id"]}
        second = await self.run_rerun("next-rerun", second_request)
        self.assertEqual(3, second["analysis_version_number"])
        self.assertEqual(2, self.model.await_count)
        self.assertEqual(self.base["input_fingerprint"], store.load_current_analysis_run(f.PROJECT)["revision"]["input_fingerprint"])
