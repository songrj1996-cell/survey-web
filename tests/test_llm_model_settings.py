"""A/B tests: isolated configuration persistence, no application startup or models."""
import asyncio
from copy import deepcopy
import multiprocessing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.core import config
from app.core.feature_permissions import permission_levels
from app.core.llm_context import bind_llm_model_snapshot, current_llm_model_snapshot
from app.core.llm_model_registry import environment_defaults, task_catalog
from app.schemas.llm_model_settings import CheckModelSettingsRequest, PublishModelSettingsRequest, RestoreModelSettingsRequest
from app.services import llm_model_settings as service
from app.storage import llm_model_settings as storage


def _publish_in_process(path, payload, output):
    config.LLM_MODEL_SETTINGS_FILE = path
    try:
        asyncio.run(service.publish_model_settings(PublishModelSettingsRequest(**payload), "隔离测试"))
        output.put(200)
    except HTTPException as exc:
        output.put(exc.status_code)


class ModelSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="model-settings-", dir=config.DATA_DIR)
        self.path = Path(self.directory.name) / "models.json"
        self.patcher = patch.object(config, "LLM_MODEL_SETTINGS_FILE", str(self.path))
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.directory.cleanup)

    async def draft(self):
        state = await service.get_model_settings()
        value = deepcopy(state["configuration"])
        value["models"][0]["name"] += "（测试）"
        return state, value

    async def publish(self, state, value, **kwargs):
        return await service.publish_model_settings(PublishModelSettingsRequest(
            expected_revision=state["revision"], configuration=value, **kwargs), "测试管理员")

    async def test_read_and_check_never_create_files(self):
        state = await service.get_model_settings()
        result = await service.check_model_settings(CheckModelSettingsRequest(
            expected_revision=state["revision"], configuration=state["configuration"]))
        self.assertTrue(result["valid"])
        self.assertFalse(self.path.exists())
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])
        self.assertTrue(state["runtime_active"])
        self.assertEqual(state["version"], 0)

    async def test_all_default_stages_and_postwrite_rules_preserved(self):
        state = await service.get_model_settings()
        self.assertEqual(len({s["key"] for s in task_catalog()}), len(task_catalog()))
        self.assertEqual(len(state["tasks"]), 38)
        cfg = state["configuration"]["tasks"]["insight.writer"]
        self.assertEqual(cfg["primary"]["model"], config.LLM_POSTWRITE_MODEL)
        self.assertEqual(cfg["primary"]["max_tokens"], config.POSTWRITE_WRITER_OUTPUT_TOKENS)
        self.assertEqual(cfg["fallbacks"], [])
        self.assertEqual(cfg["max_http_attempts"], 1)
        self.assertTrue(all(m["context_tokens"] is None for m in state["configuration"]["models"]))

    async def test_publish_and_optimistic_conflict_preserve_file(self):
        state, value = await self.draft()
        saved = await self.publish(state, value)
        before = self.path.read_bytes()
        with self.assertRaises(HTTPException) as caught:
            await self.publish(state, value)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(saved["version"], 1)
        self.assertTrue(saved["runtime_active"])
        self.assertEqual(saved["history"][0]["actor_name"], "测试管理员")

    async def test_concurrent_publish_only_one_wins(self):
        state, value = await self.draft()
        results = await asyncio.gather(self.publish(state, value), self.publish(state, value), return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual([r.status_code for r in results if isinstance(r, HTTPException)], [409])

    async def test_changed_environment_invalidates_unsaved_initial_revision(self):
        state, value = await self.draft()
        with patch.object(config, "LLM_PLANNER_MODEL", "new-environment-model"):
            with self.assertRaises(HTTPException) as caught:
                await self.publish(state, value)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertFalse(self.path.exists())

    async def test_unwritable_parent_reports_safe_failure(self):
        state, value = await self.draft()
        with patch.object(storage.Path, "mkdir", side_effect=PermissionError("private path")):
            with self.assertRaises(HTTPException) as caught:
                await self.publish(state, value)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertNotIn("private path", str(caught.exception.detail))
        self.assertFalse(self.path.exists())

    async def test_cross_process_publication_is_serialized(self):
        state, value = await self.draft()
        payload = {"expected_revision": state["revision"], "configuration": value}
        context = multiprocessing.get_context("spawn")
        output = context.Queue()
        processes = [context.Process(target=_publish_in_process, args=(str(self.path), payload, output)) for _ in range(2)]
        for process in processes:
            process.start()
        for process in processes:
            await asyncio.to_thread(process.join, 15)
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(sorted([output.get(timeout=3), output.get(timeout=3)]), [200, 409])
        output.close()

    async def test_restore_is_read_only_and_republish_appends(self):
        initial, one = await self.draft()
        v1 = await self.publish(initial, one, note="第一次")
        two = deepcopy(one)
        two["models"][0]["name"] += "第二次"
        v2 = await self.publish(v1, two)
        before = self.path.read_bytes()
        restored = await service.restore_model_settings(1, RestoreModelSettingsRequest(expected_revision=v2["revision"]))
        self.assertEqual(self.path.read_bytes(), before)
        v3 = await self.publish(v2, restored["configuration"], restored_from=1)
        self.assertEqual([h["version"] for h in v3["history"]], [3, 2, 1])
        self.assertEqual(v3["configuration"], one)
        self.assertEqual(v3["history"][0]["restored_from"], 1)

    async def test_restore_provenance_cannot_be_forged(self):
        state, value = await self.draft()
        v1 = await self.publish(state, value)
        value["models"][0]["name"] += "extra"
        with self.assertRaises(HTTPException) as caught:
            await self.publish(v1, value, restored_from=1)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_restore_retains_previously_saved_unverified_legacy_values(self):
        state, original = await self.draft()
        first = await self.publish(state, original)
        modified = deepcopy(original)
        m = next(m for m in modified["models"] if m["id"] == modified["tasks"]["insight.writer"]["primary"]["model"])
        m.update(verified=True, context_tokens=1000000, max_output_tokens=100000, verification_note="合成测试依据")
        modified["tasks"]["insight.writer"]["primary"]["max_tokens"] = 1024
        second = await self.publish(first, modified)
        checked = await service.check_model_settings(CheckModelSettingsRequest(
            expected_revision=second["revision"], configuration=original, restored_from=1))
        self.assertTrue(checked["valid"])
        third = await self.publish(second, original, restored_from=1)
        self.assertEqual(third["configuration"], original)
        original_model = original["tasks"]["insight.writer"]["primary"]["model"]
        self.assertFalse(next(m for m in third["configuration"]["models"] if m["id"] == original_model)["verified"])

    async def test_unknown_capability_blocks_new_call_settings(self):
        state = await service.get_model_settings()
        value = deepcopy(state["configuration"])
        value["tasks"]["insight.writer"]["primary"]["max_tokens"] += 1
        result = await service.check_model_settings(CheckModelSettingsRequest(expected_revision=state["revision"], configuration=value))
        self.assertFalse(result["valid"])
        self.assertTrue(any("先在模型目录核实" in e for e in result["errors"]))
        self.assertFalse(self.path.exists())

    async def test_verified_candidate_settings_are_independent(self):
        state = await service.get_model_settings()
        value = deepcopy(state["configuration"])
        for m in value["models"]:
            m.update(context_tokens=1000000, max_output_tokens=100000, efforts=["default", "medium", "high"], verified=True, verification_note="合成测试资料")
        r = value["tasks"]["insight.writer"]
        other = next(m for m in value["models"] if m["id"] != r["primary"]["model"])
        r["primary"]["reasoning"] = "high"
        r["fallbacks"] = [{"model": other["id"], "reasoning": "medium", "max_tokens": 4096}]
        r["max_http_attempts"] = 2
        saved = await self.publish(state, value)
        self.assertEqual(saved["configuration"]["tasks"]["insight.writer"], r)

    async def test_invalid_semantic_combinations_are_rejected(self):
        state = await service.get_model_settings()
        cases = []
        duplicate = deepcopy(state["configuration"])
        duplicate["models"].append(deepcopy(duplicate["models"][0]))
        cases.append(duplicate)
        missing = deepcopy(state["configuration"])
        missing["tasks"].pop("survey.columns")
        cases.append(missing)
        duplicate_call = deepcopy(state["configuration"])
        duplicate_call["tasks"]["insight.writer"]["fallbacks"] = [deepcopy(duplicate_call["tasks"]["insight.writer"]["primary"])]
        cases.append(duplicate_call)
        for value in cases:
            with self.subTest():
                with self.assertRaises(HTTPException) as caught:
                    await self.publish(state, value)
                self.assertEqual(caught.exception.status_code, 422)
                self.assertFalse(self.path.exists())

    async def test_corrupt_json_and_shapes_never_get_repaired(self):
        for data in (b"{bad-json", b"[]", b'{"schema_version":99}'):
            self.path.write_bytes(data)
            with self.assertRaises(HTTPException) as caught:
                await service.get_model_settings()
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(self.path.read_bytes(), data)

    async def test_semantically_corrupt_saved_configuration_is_not_overwritten(self):
        state, value = await self.draft()
        saved = await self.publish(state, value)
        document = json.loads(self.path.read_text(encoding="utf-8"))
        document["current"]["configuration"]["tasks"].pop("survey.columns")
        self.path.write_text(json.dumps(document), encoding="utf-8")
        before = self.path.read_bytes()
        for operation in (service.get_model_settings(), self.publish(saved, value)):
            with self.assertRaises(HTTPException) as caught:
                await operation
            self.assertEqual(caught.exception.status_code, 503)
            self.assertEqual(self.path.read_bytes(), before)

    async def test_atomic_replace_failure_retains_previous_version(self):
        state, value = await self.draft()
        saved = await self.publish(state, value)
        before = self.path.read_bytes()
        value["models"][0]["name"] += " next"
        with patch.object(storage.os, "replace", side_effect=OSError("synthetic private details")):
            with self.assertRaises(HTTPException) as caught:
                await self.publish(saved, value)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("synthetic", str(caught.exception.detail))
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    async def test_history_retention_and_unchanged_publish(self):
        with patch.object(config, "LLM_MODEL_SETTINGS_HISTORY_LIMIT", 2):
            state, value = await self.draft()
            for i in range(3):
                value["models"][0]["name"] = f"合成版本 {i}"
                state = await self.publish(state, value)
            self.assertEqual([h["version"] for h in state["history"]], [3, 2])
            with self.assertRaises(HTTPException) as caught:
                await self.publish(state, value)
            self.assertEqual(caught.exception.status_code, 409)

    async def test_snapshot_isolated_across_concurrent_tasks_and_nested_binding(self):
        before = environment_defaults()
        snapshot = await service.build_model_snapshot()
        with bind_llm_model_snapshot(snapshot):
            snapshot["version"] = 999
            self.assertEqual(current_llm_model_snapshot()["version"], 0)
            exposed = current_llm_model_snapshot()
            exposed["version"] = 777
            self.assertEqual(current_llm_model_snapshot()["version"], 0)
            async def child(version):
                with bind_llm_model_snapshot({"version": version}):
                    await asyncio.sleep(0)
                    return current_llm_model_snapshot()["version"]
            self.assertEqual(await asyncio.gather(child(4), child(5)), [4, 5])
            self.assertEqual(current_llm_model_snapshot()["version"], 0)
        self.assertIsNone(current_llm_model_snapshot())
        self.assertEqual(environment_defaults(), before)

    async def test_strict_types_and_admin_only_permission(self):
        state = await service.get_model_settings()
        for field, value in (("max_tokens", True), ("max_tokens", "4096")):
            cfg = deepcopy(state["configuration"])
            cfg["tasks"]["insight.writer"]["primary"][field] = value
            with self.assertRaises(ValidationError):
                CheckModelSettingsRequest(expected_revision=state["revision"], configuration=cfg)
        bad = deepcopy(state["configuration"])
        bad["api_key"] = "never accepted"
        with self.assertRaises(ValidationError):
            CheckModelSettingsRequest(expected_revision=state["revision"], configuration=bad)
        self.assertNotIn("model_settings", permission_levels({"perms": ["model_settings"]}))
