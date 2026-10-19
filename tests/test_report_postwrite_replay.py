"""Full replay isolation/budget tests; no external network."""
import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch
from scripts.replay_report_postwrite import (FullWireGuard, normalize_wire_usage, adapt_full_packet,
                                           persist_full_result, trial_budget, full_main, prepare_full, load_registry_reuse,
                                           resume_budget, validate_resume_request, resume_main)
from app.services.report_postwrite_inputs import validate_frozen, freeze_inputs
from app.services.report_postwrite_pipeline import initial_draft_state, audit_summary
from tests.test_report_postwrite_flow import PROMPTS, REPORT
from tests.test_report_postwrite_audit import session, note_review


class ReplayTests(unittest.TestCase):
    def test_offline_count_guard_blocks_model_and_http(self):
        from scripts.replay_report_postwrite import offline_count_guard
        from app.services.report_postwrite_pipeline import BoundedCaller
        import httpx
        with offline_count_guard() as attempts:
            with self.assertRaises(AssertionError):
                BoundedCaller().call("quantity", [])
            with self.assertRaises(AssertionError):
                httpx.Client().get("https://invalid.example")
        self.assertEqual(attempts, ["model_or_http", "model_or_http"])

    def test_offline_count_cli_rejects_live_before_creating_output(self):
        import subprocess, sys
        from scripts.replay_report_postwrite import REPO
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "output"
            process = subprocess.run([sys.executable, "-B", str(REPO / "scripts/replay_report_postwrite.py"),
                "--recount-from", folder, "--output-dir", str(output), "--live"], capture_output=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertFalse(output.exists())

    def test_resume_budget_adds_history_without_recharging_new_allowance(self):
        seed = {"calls": [{"state": "complete", "usage_complete": True, "charged_or_reserved_tokens": 106354,
                           "elapsed_seconds": 343.533}], "elapsed_seconds": 343.583}
        budget, cumulative = resume_budget(SimpleNamespace(), seed)
        self.assertEqual((budget["max_calls"], budget["max_tokens"], budget["max_seconds"]), (8, 1566633, 2336))
        self.assertEqual((cumulative["max_calls"], cumulative["max_tokens"], cumulative["max_seconds"]), (9, 1672987, 2679.583))
        self.assertEqual(budget["output_tokens"], 64000)
        self.assertEqual(budget["call_seconds"], 480)
        for key, value in (("max_calls", 9), ("token_budget", 1566634), ("seconds", 2337)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                resume_budget(SimpleNamespace(**{key: value}), seed)
        seed["calls"][0]["usage_complete"] = False
        with self.assertRaises(ValueError):
            resume_budget(SimpleNamespace(), seed)

    def test_resume_remaining_allowance_preserves_all_four_historical_calls(self):
        seed = {"calls": [{"state": "complete", "usage_complete": True, "charged_or_reserved_tokens": n}
                           for n in (106354, 209096, 210949, 213322)], "elapsed_seconds": 706.439}
        budget, total = resume_budget(SimpleNamespace(), seed)
        self.assertEqual(budget["max_tokens"], 1566633)
        self.assertEqual((total["max_calls"], total["max_tokens"], total["max_seconds"]), (12, 2306354, 3042.439))

    def test_prepare_partial_inherited_batches_keeps_89_and_refuses_damaged_ancestor(self):
        from copy import deepcopy
        from tests.test_report_postwrite_flow import partial_meaning_fixture
        from app.services.report_postwrite_inputs import fingerprint
        from app.core import config
        from scripts.replay_report_postwrite import prepare_resume, write_json, sha
        frozen, legacy, old, requests = partial_meaning_fixture()
        def save_source(root, state, local_requests):
            values = {"checkpoint.json": state, "results.json": state, "frozen-inputs.json": frozen,
                "saved-snapshot.json": {"report_audit": {"checkpoint": state}, "input_snapshot": frozen},
                "summary.json": {"usage": state["usage"]},
                "trial-spec.json": {"model": config.LLM_POSTWRITE_MODEL, "reasoning": config.LLM_POSTWRITE_REASONING,
                    "requests": [{"stage": "meaning", "fingerprint": fingerprint(r),
                        "target_ids": [c["id"] for c in json.loads(r[-1]["content"])["targets"]]} for r in local_requests.values()]},
                "wire-usage.json": [{"complete": True, "message_stop": True, "stop_reason": "end_turn",
                    "actual_model": config.LLM_POSTWRITE_MODEL, "normalized_usage": {"total_tokens": 140}} for _ in local_requests]}
            for name, value in values.items(): write_json(root / name, value)
            for index, (key, request) in enumerate(local_requests.items(), 1):
                write_json(root / "requests" / f"{index:02d}.json", request)
                (root / "responses").mkdir(exist_ok=True)
                (root / "responses" / f"{index:02d}.txt").write_text(state["responses"][key], encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            parent, source, output = [Path(folder) / name for name in ("parent", "source", "output")]
            pairs = list(requests.items())
            save_source(parent, legacy, dict(pairs[:1]))
            old["meaning_reuse"]["provenance"] = {"source_run": str(parent),
                "protected_hashes": {str(parent / "checkpoint.json"): sha(parent / "checkpoint.json")}}
            save_source(source, old, dict(pairs[1:]))
            before = deepcopy(old)
            _, _, reused, plan = prepare_resume(SimpleNamespace(resume_from=source, output_dir=output))
            self.assertEqual(reused["steps"], before["steps"])
            self.assertEqual(plan["reused_claims"], 89)
            self.assertEqual([r["target_ids"][0] for r in plan["requests"]], ["C074", "C091", "C121", "C151"])
            (parent / "responses/01.txt").write_text("{}")
            with self.assertRaisesRegex(ValueError, "原始响应"):
                prepare_resume(SimpleNamespace(resume_from=source, output_dir=output))

    def test_prepare_reuses_meaning_and_never_plans_first_batch_again(self):
        from tests.test_report_postwrite_flow import legacy_meaning_fixture
        from app.services.report_postwrite_inputs import fingerprint
        from app.core import config
        from scripts.replay_report_postwrite import prepare_resume, write_json
        frozen, old, requests = legacy_meaning_fixture()
        key, request = next(iter(requests.items()))
        with tempfile.TemporaryDirectory() as folder:
            source, output = Path(folder) / "old", Path(folder) / "new"
            values = {"checkpoint.json": old, "results.json": old, "frozen-inputs.json": frozen,
                "saved-snapshot.json": {"report_audit": {"checkpoint": old}, "input_snapshot": frozen},
                "summary.json": {"usage": old["usage"]}, "requests/01.json": request,
                "trial-spec.json": {"model": config.LLM_POSTWRITE_MODEL, "reasoning": config.LLM_POSTWRITE_REASONING,
                    "requests": [{"stage": "meaning", "fingerprint": fingerprint(request),
                                  "target_ids": [f"C{i:03d}" for i in range(1,31)]}]},
                "wire-usage.json": [{"complete": True, "message_stop": True, "stop_reason": "end_turn",
                                     "actual_model": config.LLM_POSTWRITE_MODEL, "normalized_usage": {"total_tokens": 140}}]}
            for name, value in values.items():
                write_json(source / name, value)
            (source / "responses").mkdir()
            (source / "responses/01.txt").write_text(old["responses"][key], encoding="utf-8")
            _, _, migrated, plan = prepare_resume(SimpleNamespace(resume_from=source, output_dir=output))
            self.assertEqual(plan["requests"][0]["target_ids"], ["C031"])
            self.assertEqual(plan["reused_claims"], 30)
            self.assertEqual(migrated["responses"], old["responses"])
            self.assertTrue((output / "reuse-validation.json").exists())
            (source / "responses/01.txt").write_text("{}")
            with self.assertRaisesRegex(ValueError, "原始响应"):
                prepare_resume(SimpleNamespace(resume_from=source, output_dir=output))

    def test_resume_guard_rejects_writer_registry_changed_parameters_or_extra_call(self):
        from app.services.report_postwrite_inputs import fingerprint
        request = [{"role": "user", "content": '{"targets":[{"id":"C1"}]}'}]
        kwargs = {"models": ["claude-opus-5-5"], "reasoning_effort": "medium", "max_tokens": 64000, "max_http_attempts": 1}
        plan = {"requests": [{"fingerprint": fingerprint(request)}], "model": "claude-opus-5-5", "reasoning": "medium", "budget": {"output_tokens": 64000}}
        validate_resume_request(request, kwargs, plan, 0)
        for changed in ([{"content": "write a report"}], [{"content": '{"blocks":[]}'}]):
            with self.assertRaises(ValueError):
                validate_resume_request(changed, kwargs, plan, 0)
        for key, value in (("models", ["other"]), ("reasoning_effort", "high"), ("max_tokens", 24000), ("max_http_attempts", 2)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_resume_request(request, {**kwargs, key: value}, plan, 0)
        with self.assertRaises(ValueError):
            validate_resume_request(request, kwargs, plan, 1)

    def test_resume_live_marker_prevents_preparation_and_new_calls(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "live-started.json").write_text("{}")
            with patch("scripts.replay_report_postwrite.prepare_resume") as prepare, self.assertRaises(ValueError):
                resume_main(SimpleNamespace(output_dir=output))
            prepare.assert_not_called()

    def saved_source(self, root):
        from app.core import config
        from app.services.report_postwrite_audit import validate_registry, registry_payload, messages
        frozen = freeze_inputs(session())
        obj = {"claims": [{"id": "C1", "original": REPORT.split("\n\n")[1], "block_ids": ["B0002"]}],
               "coverage": [{"block_id": "B0001", "no_claim": True}, {"block_id": "B0002", "claim_ids": ["C1"]}],
               "tasks": [], "not_checked": ["解释已登记，原意待核验", "报告未表达的分项无需登记"]}
        raw = json.dumps(obj, ensure_ascii=False)
        state = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
        state.update(registry_raw=raw, registry=validate_registry(obj, REPORT, frozen["postwrite_packet"]),
                     calls=[{"stage": "registry", "state": "complete", "usage_complete": True,
                             "actual_model": config.LLM_POSTWRITE_MODEL}], usage={"requests": 1, "tokens": 140})
        files = {"results.json": state, "frozen-inputs.json": frozen,
                 "requests/01.json": messages(PROMPTS["registry"], registry_payload(REPORT, frozen["postwrite_packet"])),
                 "wire-usage.json": [{"complete": True, "message_stop": True, "stop_reason": "end_turn"}],
                 "trial-spec.json": {"model": config.LLM_POSTWRITE_MODEL, "reasoning": config.LLM_POSTWRITE_REASONING}}
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        dispositions = note_review(raw, ["pending_verification", "not_applicable"])
        dispositions["source_files"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}
        return state, dispositions

    def test_reuse_saved_response_keeps_notes_and_original_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original, dispositions = self.saved_source(root)
            frozen, prompts, state = load_registry_reuse(root, dispositions)
            self.assertTrue(state["registry"]["complete"])
            self.assertEqual(state["audit"]["status"], "partial")
            self.assertEqual(state["registry"]["claims"], original["registry"]["claims"])
            self.assertEqual(len(state["registry"]["verification_notes"]), 1)
            self.assertEqual(len(state["registry"]["registration_notes"]), 1)
            self.assertEqual(state["usage"], original["usage"])
            self.assertTrue(all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
                                for name, digest in dispositions["source_files"].items()))

    def test_reuse_rejects_changed_source_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, dispositions = self.saved_source(root)
            (root / "results.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "指纹"):
                load_registry_reuse(root, dispositions)

    def test_reuse_rejects_truncated_wire_or_changed_request(self):
        for name, value, message in (("wire-usage.json", [{"complete": True, "message_stop": True, "stop_reason": "max_tokens"}], "正常结束"),
                                     ("requests/01.json", [{"content": PROMPTS["registry"]}, {"content": "{}"}], "原请求")):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                _, dispositions = self.saved_source(root)
                (root / name).write_text(json.dumps(value), encoding="utf-8")
                dispositions["source_files"][name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
                with self.assertRaisesRegex(ValueError, message):
                    load_registry_reuse(root, dispositions)

    def test_offline_cli_rejects_live_or_overlapping_output_before_execution(self):
        import contextlib
        import io
        from scripts.replay_report_postwrite import main
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source"
            for extra, output in ((["--live"], Path(folder) / "new"), ([], source / "nested")):
                argv = ["replay", "--reuse-registry-from", str(source), "--note-dispositions", str(Path(folder) / "notes.json"),
                        "--output-dir", str(output), *extra]
                with patch("sys.argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                     patch("scripts.replay_report_postwrite.offline_registry_reuse") as run, self.assertRaises(SystemExit):
                    main()
                run.assert_not_called()
                self.assertFalse(output.exists())

    def test_registry_budget_is_one_request_200k_15_minutes(self):
        args = SimpleNamespace(registry_only=True, max_calls=None, token_budget=None, seconds=None)
        budget = trial_budget(args)
        self.assertEqual(budget["max_calls"], 1)
        self.assertEqual(budget["max_tokens"], 200000)
        self.assertEqual(budget["max_seconds"], 900)
        self.assertEqual(budget["call_seconds"], 900)
        self.assertEqual(budget["registry_output_tokens"], 64000)
        for key, value in (("max_calls", 2), ("token_budget", 200001), ("seconds", 901)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                trial_budget(SimpleNamespace(**{**vars(args), key: value}))

    def test_previous_full_profile_keeps_24k_cap(self):
        budget = trial_budget(SimpleNamespace(registry_only=False))
        self.assertEqual(budget["max_calls"], 12)
        self.assertEqual(budget["registry_output_tokens"], 24000)
        self.assertEqual(budget["call_seconds"], 480)

    def test_registry_wire_reservation_and_second_call_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            budget = trial_budget(SimpleNamespace(registry_only=True))
            guard = FullWireGuard(Path(folder), {**budget, "max_tokens": 50000}, {})
            with self.assertRaises(ValueError):
                guard.before([{"content": "small"}], output_tokens=64000)
            self.assertEqual(guard.records, [])
            guard = FullWireGuard(Path(folder), budget, {})
            guard.before([{"content": "small"}], output_tokens=64000)
            self.assertEqual(guard.current["requested_output_cap"], 64000)
            guard.current.update(complete=True, normalized_usage={"total_tokens": 100})
            with self.assertRaises(ValueError):
                guard.before([{"content": "small"}], output_tokens=64000)

    def test_existing_live_marker_prevents_rerun(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            (output / "live-started.json").write_text("{}")
            args = SimpleNamespace(registry_only=True, draft_dir=output, output_dir=output)
            with patch("scripts.replay_report_postwrite.prepare_full") as prepare, self.assertRaises(ValueError):
                full_main(args)
            prepare.assert_not_called()

    def test_changed_registry_input_stops_before_call(self):
        frozen = freeze_inputs(session())
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "requests").mkdir()
            (root / "requests" / "01.json").write_text("[]")
            (root / "trial-spec.json").write_text("{}")
            (root / "source-packet.json").write_text("{}")
            (root / "report-draft.md").write_text(REPORT, encoding="utf-8")
            args = SimpleNamespace(registry_only=True, draft_dir=root, baseline_run=root, fixture_root=None)
            with patch("scripts.replay_report_postwrite.adapt_full_packet", return_value=frozen), self.assertRaisesRegex(ValueError, "登记输入"):
                prepare_full(args)

    def test_cache_usage_included_once(self):
        result = normalize_wire_usage({"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30})
        self.assertEqual(result, {"input_tokens": 60, "output_tokens": 5, "cached_input_tokens": 50, "total_tokens": 65})
        with self.assertRaises(ValueError):
            normalize_wire_usage({"input_tokens": 10})

    def test_wire_counts_and_unknown_usage_block_next_request(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = FullWireGuard(Path(folder), {"max_calls": 2, "max_tokens": 20000, "max_seconds": 60, "output_tokens": 1000}, {})
            request = [{"content": "test"}]
            guard.before(request)
            with self.assertRaises(ValueError):
                guard.before(request)
            guard.current.update(complete=True, normalized_usage={"total_tokens": 100})
            guard.before(request)
            guard.current.update(complete=True, normalized_usage={"total_tokens": 100})
            with self.assertRaises(ValueError):
                guard.before(request)
            self.assertEqual(len(guard.records), 2)

    def test_tokens_stop_before_request(self):
        with tempfile.TemporaryDirectory() as folder:
            guard = FullWireGuard(Path(folder), {"max_calls": 2, "max_tokens": 1000, "max_seconds": 60, "output_tokens": 1000}, {})
            with self.assertRaises(ValueError):
                guard.before([{"content": "test"}])
            self.assertEqual(guard.records, [])

    def test_adapter_preserves_question_rows_and_ignores_old_answers(self):
        raw = {"questions": {"H": {"type": "open", "original": "Why?", "translation": "原因"},
                             "G": {"type": "single_choice", "original": "Rating", "translation": "评分"}},
               "option_mapping": {"G": {"Good": "好"}}, "branches": {}, "research_task": {}, "source": {"sample": 2},
               "source_reference_rule": "列行", "statistics": {"S01": {"n": 1, "description": "合成统计"}},
               "records": [{"player": "P002", "source_row": 2, "answers": {"G": "Good", "H": "fun"}, "selections_zh": {"G": "好"}},
                           {"player": "P003", "source_row": 3, "answers": {"G": "Good", "H": ""}, "selections_zh": {"G": "好"}}],
               "old_verdicts": {"C1": "pass"}}
        with patch("scripts.replay_report_postwrite.verify_statistics", return_value=raw["statistics"]):
            frozen = adapt_full_packet(raw)
        packet = validate_frozen(frozen)
        units = {u["ref"]: u for u in packet["source_units"]}
        self.assertNotIn("H3", units)
        self.assertEqual(units["H2"]["original"], "fun")
        self.assertEqual(units["G2"]["raw_selection"], "Good")
        self.assertEqual(units["G2"]["original"], "好")
        self.assertNotIn("old_verdicts", packet)
        self.assertEqual(len(packet["players"]), 2)


class WireParameterTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_cap_recorded_and_duplicate_attempt_blocked(self):
        import httpx
        calls = []
        class Response:
            status_code = 200
            async def aiter_lines(self):
                yield 'data: {"type":"message_stop"}'
        @asynccontextmanager
        async def fake_stream(client, *args, **kwargs):
            calls.append(kwargs)
            yield Response()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            guard = FullWireGuard(root, trial_budget(SimpleNamespace(registry_only=True)), {})
            guard.before([{"content": "small"}], output_tokens=64000)
            with patch.object(httpx.AsyncClient, "stream", new=fake_stream):
                async with guard.installed(), httpx.AsyncClient() as client:
                    with self.assertRaisesRegex(ValueError, "额度"):
                        async with client.stream("POST", "https://invalid.example", json={"max_tokens": 24000}):
                            pass
                    self.assertEqual(calls, [])
                    async with client.stream("POST", "https://invalid.example", json={"max_tokens": 64000, "model": "fake", "stream": True}) as response:
                        async for _ in response.aiter_lines():
                            pass
                    with self.assertRaisesRegex(ValueError, "重试"):
                        async with client.stream("POST", "https://invalid.example", json={"max_tokens": 64000}):
                            pass
            self.assertEqual(len(calls), 1)
            self.assertEqual(guard.current["request_parameters"]["max_tokens"], 64000)
            self.assertTrue(guard.current["message_stop"])


class SaveReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_partial_result_has_no_paid_calls(self):
        import os
        from app.storage import sessions, history
        from app.services import survey_service
        from app.core import config
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            frozen = freeze_inputs(session())
            result = initial_draft_state(frozen, prompts=PROMPTS, draft=REPORT)
            result.update(stop_reason="本批未完成", usage={"requests": 0, "tokens": 0})
            result["audit"] = audit_summary(result)
            with patch.object(sessions, "_SESSION_DIR", output / "sessions"), \
                 patch.object(history, "HISTORY_FILE", str(output / "history.json")), \
                 patch.object(survey_service, "PostwriteRunStore", side_effect=lambda: __import__('app.storage.report_postwrite_runs', fromlist=['RunStore']).RunStore(output / "runs")):
                checks = await persist_full_result(output, frozen, PROMPTS, result)
            self.assertTrue(checks["original_version_unchanged"])
            self.assertEqual(checks["extra_model_requests"], 0)
            self.assertIn("核验未完成", (output / "export-body.md").read_text(encoding="utf-8"))
