"""Meaningful negative controls for the isolated real-business acceptance launcher."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.accept_report_postwrite_e2e import AcceptanceBudget, AcceptanceStop, configure_isolation, stage_for, prepare_resume


PREPARATION = [{"role": "user", "content": "识别题目"}]


async def success(messages, **kwargs):
    assert kwargs["models"] == ["test-claude"]
    assert kwargs["max_http_attempts"] == 1
    await kwargs["on_attempt_event"]({"status": "started", "attempt": 1})
    await kwargs["on_attempt_event"]({"status": "completed", "usage_complete": True,
        "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}, "finish_reason": "end_turn"})
    return "real-shaped-output", "test-claude"


class BudgetTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_only_allows_exact_pending_payload_and_new_budget(self):
        payload = {"tasks": [{"id": "pending", "players": ["p"]}], "source_units": []}
        with tempfile.TemporaryDirectory() as folder:
            budget = AcceptanceBudget(folder, "test-claude", max_calls=1, max_tokens=10000,
                                      max_seconds=1200, allowed_payloads=[payload])
            await budget.call(success, [{"content": json.dumps(payload)}], max_tokens=1024)
            with self.assertRaises(AcceptanceStop):
                await budget.call(success, [{"content": json.dumps(payload)}], max_tokens=1024)
            self.assertEqual(budget.summary()["tokens"], 110)
            self.assertEqual(len(budget.entries), 1)
        for bad in [{"report": "old", "blocks": []}, {"report": "old", "targets": []},
                    {"tasks": [{"id": "completed"}], "source_units": []}, {"columns": []}]:
            with tempfile.TemporaryDirectory() as folder:
                budget = AcceptanceBudget(folder, "test-claude", allowed_payloads=[payload])
                with self.assertRaises(AcceptanceStop):
                    await budget.call(success, [{"content": json.dumps(bad)}], max_tokens=1024)
                self.assertFalse(budget.entries)

    async def test_all_stages_share_one_actual_http_budget_and_no_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = AcceptanceBudget(folder, "test-claude", max_calls=2)
            await budget.call(success, PREPARATION, max_tokens=1024)
            writer = [{"content": json.dumps({"players": [], "research_task": {}, "questions": []})}]
            await budget.call(success, writer, max_tokens=1024)
            with self.assertRaises(AcceptanceStop):
                await budget.call(success, writer, max_tokens=1024)
            self.assertEqual(budget.summary()["actual_http_requests"], 2)
            self.assertEqual(budget.summary()["tokens"], 220)
            self.assertEqual([e["stage"] for e in budget.entries], ["preparation", "writer"])
            with self.assertRaises(ValueError):
                AcceptanceBudget(folder, "test-claude")

    async def test_preflight_token_and_wall_limits_do_not_invoke_model(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = AcceptanceBudget(folder, "test-claude", max_tokens=1024)
            with self.assertRaises(AcceptanceStop):
                await budget.call(success, PREPARATION, max_tokens=1024)
            self.assertFalse(budget.entries)
        with tempfile.TemporaryDirectory() as folder:
            now = [0]
            budget = AcceptanceBudget(folder, "test-claude", clock=lambda: now[0], max_seconds=1)
            await budget.call(success, PREPARATION, max_tokens=1024)
            now[0] = 2
            with self.assertRaises(AcceptanceStop):
                await budget.call(success, PREPARATION, max_tokens=1024)
            self.assertEqual(len(budget.entries), 1)

    async def test_unknown_usage_truncation_output_violation_and_failure_stop_future_calls(self):
        cases = [({}, None), ({"usage_complete": True, "finish_reason": "max_tokens"}, None),
                 ({"usage_complete": True, "usage": {"output_tokens": 2048}}, None),
                 ({"usage_complete": True}, RuntimeError("synthetic failure"))]
        for changes, error in cases:
            with self.subTest(changes=changes, error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                async def broken(messages, **kwargs):
                    item = {"status": "completed", "usage_complete": False, "usage": {"total_tokens": 200}, **changes}
                    await kwargs["on_attempt_event"]({"status": "started"})
                    await kwargs["on_attempt_event"](item)
                    if error:
                        raise error
                    return "output", "test-claude"
                budget = AcceptanceBudget(folder, "test-claude")
                with self.assertRaises((AcceptanceStop, RuntimeError)):
                    await budget.call(broken, PREPARATION, max_tokens=1024)
                with self.assertRaises(AcceptanceStop):
                    await budget.call(success, PREPARATION, max_tokens=1024)
                self.assertEqual(len(budget.entries), 1)

    async def test_four_preparation_calls_cannot_spend_report_allocation(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = AcceptanceBudget(folder, "test-claude")
            for _ in range(4):
                await budget.call(success, PREPARATION, max_tokens=1024)
            with self.assertRaises(AcceptanceStop):
                await budget.call(success, PREPARATION, max_tokens=1024)
            self.assertEqual(len(budget.entries), 4)


class IsolationTests(unittest.TestCase):
    def test_checkpoint_copy_preserves_source_and_forbids_budget_reset(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder) / "prior", Path(folder) / "resume"
            run = source / "data" / "report_postwrite_runs" / "a.json"
            run.parent.mkdir(parents=True)
            state = {"draft": "old draft", "registry": {"complete": True},
                     "calls": [{"state": "complete", "usage_complete": True}], "usage": {"tokens": 123}}
            run.write_text(json.dumps({"state": state}), encoding="utf-8")
            (source / "data" / "history.json").write_text('[{"version":1}]', encoding="utf-8")
            (source / "calls.json").write_text('[{"actual_tokens":123}]', encoding="utf-8")
            before = run.read_bytes()
            self.assertEqual(prepare_resume(target, source), state)
            self.assertEqual((target / "data" / "report_postwrite_runs" / "a.json").read_bytes(), before)
            self.assertEqual(run.read_bytes(), before)
            self.assertFalse((target / "calls.json").exists())
            self.assertTrue((target / "prior-calls.json").exists())
            with self.assertRaises(ValueError):
                prepare_resume(target, source)

    def test_isolation_precedes_import_and_credentials_are_not_written(self):
        repo = Path(__file__).resolve().parents[1]
        cwd, cache = os.getcwd(), __import__("sys").pycache_prefix
        try:
            with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {"LLM_API_KEY": "secret-synthetic"}):
                root = Path(folder) / "run"
                values = configure_isolation(root, repo)
                self.assertTrue(Path(values["DATA_DIR"]).is_relative_to(root))
                self.assertEqual(Path.cwd(), root)
                self.assertNotIn("secret-synthetic", (root / "acceptance.env").read_text(encoding="utf-8"))
                with self.assertRaises(ValueError):
                    configure_isolation(repo / "bad-acceptance", repo)
                os.chdir(cwd)
        finally:
            os.chdir(cwd)
            __import__("sys").pycache_prefix = cache

    def test_stage_classification_does_not_confuse_registration_and_counts(self):
        for payload, expected in [({"report": "x", "blocks": []}, "registry"),
                                  ({"report": "x", "targets": []}, "meaning"),
                                  ({"tasks": [], "source_units": []}, "quantity"),
                                  ({"columns": []}, "preparation")]:
            self.assertEqual(stage_for([{"content": json.dumps(payload)}]), expected)
