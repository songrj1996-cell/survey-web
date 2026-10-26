"""Offline money/approval/replay-boundary checks. No real gateway transport."""
import asyncio
from contextlib import asynccontextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from scripts import replay_model_management as replay


def card():
    return {"catalog_verified": True, "observed_at": datetime.now(timezone.utc).isoformat(),
            "source_url": "https://gateway.invalid/models", "fx": {"planning_cny_per_usd": "7"},
            "models": {"fixture-model": {"currency": "USD", "list_input_per_million": "4",
                "list_output_per_million": "20", "displayed_input_per_million": "3.28",
                "displayed_output_per_million": "16.4", "list_cache_write_per_million": "5",
                "displayed_cache_write_per_million": "4.1",
                "context_tokens": 1000000, "max_output_tokens": 128000}}}


class BudgetTests(unittest.TestCase):
    def test_conservative_charge_includes_full_output_and_ignores_discount(self):
        quote = card()
        upper = replay.cost_cny(quote, "fixture-model", 496478, 64000)
        discounted = replay.cost_cny(quote, "fixture-model", 496478, 64000, displayed_discount=True)
        self.assertEqual(replay.money(upper), "26.34")
        self.assertEqual(replay.money(discounted), "21.60")
        budget = replay.Budget(Decimal("55"))
        budget.reserve(upper)
        budget.reserve(upper)
        self.assertEqual(budget.reserved_cny, Decimal("52.68"))
        with self.assertRaises(replay.ReplayStop):
            budget.reserve("0.01")

    def test_money_boundary_rounds_up_and_cannot_exceed_approved_fifty_five(self):
        with self.assertRaises(replay.ReplayStop):
            replay.Budget(Decimal("55.0001"))
        budget = replay.Budget(Decimal("55"), max_requests=100)
        budget.reserve("54.99")
        with self.assertRaises(replay.ReplayStop):
            budget.reserve("0.01001")
        self.assertEqual(budget.reserved_cny, Decimal("54.99"))
        self.assertEqual(budget.attempts, 1)

    def test_cache_write_quote_cannot_be_assumed_free_when_missing(self):
        quote = card()
        del quote["models"]["fixture-model"]["list_cache_write_per_million"]
        with self.assertRaises(replay.ReplayStop):
            replay.cost_cny(quote, "fixture-model", 496478, 64000)

    def test_lower_cache_write_price_cannot_reduce_ordinary_input_reservation(self):
        quote = card()
        quote["models"]["fixture-model"]["list_cache_write_per_million"] = "1"
        self.assertEqual(replay.money(replay.cost_cny(quote, "fixture-model", 496478, 64000)), "22.87")

    def test_unknown_usage_never_refunds_or_allows_next_request(self):
        budget = replay.Budget(Decimal("50"))
        budget.reserve("20")
        budget.settle(usage_complete=False)
        self.assertEqual(budget.reserved_cny, Decimal("20"))
        with self.assertRaisesRegex(replay.ReplayStop, "未知"):
            budget.reserve("1")

    def test_timeout_and_invalid_amounts_stop_before_reservation(self):
        budget = replay.Budget(Decimal("50"), max_seconds=1)
        budget.started = time.monotonic() - 2
        with self.assertRaises(replay.ReplayStop):
            budget.reserve("1")
        self.assertEqual(budget.attempts, 0)
        for value in [True, float("nan"), "NaN", "Infinity", "-1", None]:
            with self.subTest(value=repr(value)), self.assertRaises(replay.ReplayStop):
                replay.number(value)

    def test_missing_model_price_and_invalid_tokens_are_not_free(self):
        with self.assertRaises(replay.ReplayStop):
            replay.cost_cny(card(), "unpriced", 10, 10)
        for value in [None, True, -1, 1.5]:
            with self.subTest(value=value), self.assertRaises(replay.ReplayStop):
                replay.cost_cny(card(), "fixture-model", value, 1024)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.plan = {"sample": {"privacy_flags": {}}, "rate_card": card(), "estimate": {"within_budget": True}}
        self.approval = {"approved": True, "plan_sha256": replay.digest(self.plan),
                         **{k: True for k in ("samples_reviewed", "fees_reviewed", "billing_bound_confirmed", "baseline_equivalence_reviewed")}}

    def test_all_explicit_approval_fields_and_exact_plan_are_required(self):
        self.assertEqual(replay.approval_blockers(self.plan, self.approval), [])
        for key in self.approval:
            approval = dict(self.approval)
            approval.pop(key)
            self.assertTrue(replay.approval_blockers(self.plan, approval), key)
        plan = deepcopy(self.plan)
        plan["estimate"]["within_budget"] = False
        self.assertTrue(replay.approval_blockers(plan, self.approval))

    def test_stale_quote_and_privacy_flags_cannot_be_approved_away(self):
        for change in ("stale", "privacy", "unverified"):
            plan = deepcopy(self.plan)
            if change == "stale":
                plan["rate_card"]["observed_at"] = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            elif change == "privacy":
                plan["sample"]["privacy_flags"] = {"email_matches": 1}
            else:
                plan["rate_card"]["catalog_verified"] = False
            approval = {**self.approval, "plan_sha256": replay.digest(plan)}
            self.assertTrue(replay.approval_blockers(plan, approval), change)

    def test_live_flag_without_approval_does_not_load_config_or_send(self):
        text = io.StringIO()
        with patch.object(replay, "configure", side_effect=AssertionError("configuration must remain untouched")), \
             patch.object(httpx.AsyncClient, "stream", side_effect=AssertionError("network forbidden")), \
             redirect_stdout(text):
            code = replay.main(["--live", "--output-dir", "unused", "--env-file", "missing.env"])
        self.assertEqual(code, 2)
        record = json.loads(text.getvalue())
        self.assertIn("尚未批准", record["message"])
        self.assertIsNotNone(datetime.fromisoformat(record["timestamp"]).utcoffset())


class PhysicalRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_protocol_retry_cannot_send_second_http_request(self):
        sent = []
        @asynccontextmanager
        async def fake_stream(client, method, url, **kwargs):
            sent.append(kwargs)
            yield object()
        record = {"http_requests": 0}
        with patch.object(httpx.AsyncClient, "stream", fake_stream):
            async with httpx.AsyncClient() as client:
                async with replay.single_http_guard("fixture-model", 64000, record):
                    async with client.stream("POST", "https://gateway.invalid/v1/messages", json={"model": "fixture-model", "max_tokens": 64000}):
                        pass
                    with self.assertRaises(replay.ReplayStop):
                        async with client.stream("POST", "https://gateway.invalid/v1/chat/completions", json={"model": "fixture-model", "max_tokens": 64000}):
                            pass
                self.assertIs(httpx.AsyncClient.stream, fake_stream)
        self.assertEqual(len(sent), 1)

    async def test_wrong_output_cap_is_rejected_before_send(self):
        with patch.object(httpx.AsyncClient, "stream", side_effect=AssertionError("must not send")):
            async with httpx.AsyncClient() as client:
                async with replay.single_http_guard("fixture-model", 64000, {"http_requests": 0}):
                    with self.assertRaises(replay.ReplayStop):
                        async with client.stream("POST", "https://gateway.invalid/v1/messages", json={"model": "fixture-model", "max_tokens": 1024}):
                            pass


class PreparationTests(unittest.TestCase):
    def test_three_digit_pseudonyms_are_recognized_without_accepting_names(self):
        with tempfile.TemporaryDirectory(dir=os.environ["DATA_DIR"]) as folder:
            source = Path(folder) / "privacy.json"
            for players, expected in [(["P001", "P078", "P00001"], True),
                                      (["P001", "example-name"], False), ([], False)]:
                with self.subTest(players=players):
                    source.write_text(json.dumps({"postwrite_packet": {"players": players}}), encoding="utf-8")
                    inventory = replay.privacy_inventory(source, "survey")
                    self.assertIs(inventory["player_ids_are_pseudonyms"], expected)
                    self.assertEqual(inventory["privacy_status"], "pending_manual_review")

    def test_preparation_is_offline_private_and_does_not_overwrite_review(self):
        root = Path(os.environ["DATA_DIR"]).resolve()
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            folder = Path(folder)
            source = folder / "frozen.json"
            source.write_text(json.dumps({"postwrite_packet": {"players": ["P00001"], "questions": [],
                "source_units": [{"kind": "open_text", "original": "PRIVATE_MATERIAL user@example.test", "player": "P00001"}]}}, ensure_ascii=False), encoding="utf-8")
            before = replay.file_sha(source)
            rates, env_file, output = folder / "rates.json", folder / "test.env", folder / "prepared"
            rates.write_text(json.dumps(card()), encoding="utf-8")
            env_file.write_text('LLM_API_BASE=https://gateway.invalid/v1\nLLM_POSTWRITE_MODEL=fixture-model\nLLM_POSTWRITE_REASONING=default\n', encoding="utf-8")
            code = """import httpx,sys,runpy
async def forbidden(*args,**kw): raise AssertionError('network forbidden')
httpx.AsyncHTTPTransport.handle_async_request=forbidden
sys.argv=['replay_model_management.py',*sys.argv[1:]]
runpy.run_path('scripts/replay_model_management.py',run_name='__main__')
"""
            args = [sys.executable, "-c", code, "--output-dir", str(output), "--env-file", str(env_file),
                    "--survey-source", str(source), "--rate-card", str(rates)]
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
            for key in ("LLM_API_BASE", "LLM_POSTWRITE_MODEL", "LLM_POSTWRITE_REASONING", "LLM_MODEL_SETTINGS_FILE"):
                environment.pop(key, None)
            run = subprocess.run(args, cwd=replay.ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["estimate"]["actual_model_calls"], 0)
            self.assertEqual(plan["sample"]["privacy_flags"]["email_matches"], 1)
            self.assertEqual(plan["scope"], "writer_pair_only_not_complete_pipeline")
            for path in output.glob("*.json"):
                self.assertNotIn("PRIVATE_MATERIAL", path.read_text(encoding="utf-8"))
                self.assertNotIn("user@example.test", path.read_text(encoding="utf-8"))
            plan_before = replay.file_sha(output / "plan.json")
            repeated = subprocess.run(args, cwd=replay.ROOT, env=environment, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(replay.file_sha(output / "plan.json"), plan_before)
            self.assertEqual(replay.file_sha(source), before)
            self.assertFalse((output / "live-started.json").exists())


class MockExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, folder, *, complete=True):
        # Both transports are synthetic. Exercising --live internals is not a live call.
        source = ('''import httpx
async def collect_chat_completion(messages, **options):
    async with httpx.AsyncClient() as client:
        async with client.stream('POST', 'https://gateway.invalid/v1/messages', json={
            'model': options['models'][0], 'max_tokens': options['max_tokens']}):
            pass
    options['on_attempt_event']({'status':'completed','model':options['models'][0],
        'usage_complete': COMPLETE, 'usage': {'input_tokens':2,'output_tokens':3,'total_tokens':5}})
    return 'synthetic draft',options['models'][0]
''').replace('COMPLETE', repr(complete)).encode()
        namespace = {}
        exec(compile(source, '<synthetic-collector>', 'exec'), namespace)
        from app.core.llm_model_registry import environment_defaults
        from app.integrations import llm_client
        settings = environment_defaults()
        writer = {"model": "fixture-model", "reasoning": "default", "max_tokens": 1024}
        settings["models"].append({"id": "fixture-model", "efforts": ["default"],
                                    "context_tokens": 1000000, "max_output_tokens": 128000})
        settings["tasks"]["insight.writer"].update(primary=writer, fallbacks=[], max_http_attempts=1)
        quote = card()
        reserve = replay.money(replay.cost_cny(quote, "fixture-model", 100, 1024))
        plan = {"sample": {"privacy_flags": {}}, "rate_card": quote, "estimate": {"within_budget": True,
                "per_request_reserved_cny": reserve}, "material": {"input_budget_proxy": 100},
                "writer": writer, "configuration": settings, "baseline_ref": "fixture-commit",
                "baseline_client_sha256": replay.hashlib.sha256(source).hexdigest(),
                "output_dir": str(folder), "budget": {"cap_cny": "50", "max_seconds": 1200}}
        approval = {"approved": True, "plan_sha256": replay.digest(plan),
                    **{k: True for k in ("samples_reviewed", "fees_reviewed", "billing_bound_confirmed", "baseline_equivalence_reviewed")}}
        sends = []
        @asynccontextmanager
        async def fake_stream(client, method, url, **kwargs):
            sends.append(kwargs["json"])
            yield object()
        with patch.object(replay, "verify_frozen", return_value=[{"role": "user", "content": "synthetic"}]), \
             patch.object(replay.subprocess, "check_output", return_value=source), \
             patch.object(llm_client, "collect_chat_completion", namespace["collect_chat_completion"]), \
             patch.object(httpx.AsyncClient, "stream", fake_stream), \
             patch.object(httpx.AsyncHTTPTransport, "handle_async_request", side_effect=AssertionError("network forbidden")), \
             redirect_stdout(io.StringIO()):
            if complete:
                result = await replay.execute(plan, approval, SimpleNamespace(LLM_API_KEY="fixture-key"))
                self.assertEqual(result["status"], "drafts_await_manual_review")
                self.assertEqual([r["http_requests"] for r in result["records"]], [1, 1])
                self.assertEqual(len(sends), 2)
                with self.assertRaises(FileExistsError):
                    await replay.execute(plan, approval, SimpleNamespace(LLM_API_KEY="fixture-key"))
                self.assertEqual(len(sends), 2)
            else:
                with self.assertRaisesRegex(replay.ReplayStop, "未知"):
                    await replay.execute(plan, approval, SimpleNamespace(LLM_API_KEY="fixture-key"))
                self.assertEqual(len(sends), 1)
                self.assertTrue((folder / "baseline-result.json").exists())
                self.assertFalse((folder / "candidate-reserved.json").exists())

    async def test_mock_pair_records_two_requests_and_rejects_accidental_rerun(self):
        with tempfile.TemporaryDirectory(dir=os.environ["DATA_DIR"]) as directory:
            await self.exercise(Path(directory))

    async def test_unknown_first_usage_stops_mock_candidate_before_send(self):
        with tempfile.TemporaryDirectory(dir=os.environ["DATA_DIR"]) as directory:
            await self.exercise(Path(directory), complete=False)


if __name__ == "__main__":
    unittest.main()
