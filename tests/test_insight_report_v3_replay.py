from copy import deepcopy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.core.llm_context import current_llm_attempt_observer
from scripts import replay_insight_report_v3 as replay


class InsightV3ReplayTests(unittest.IsolatedAsyncioTestCase):
    def test_source_audit_experiment_budgets_cannot_be_extended(self):
        from scripts.replay_insight_report_v3_source_audit import Budget, ExperimentBudgetExceeded
        budget = Budget(logical_limit=100, http_limit=1000)
        for _ in range(18):
            budget.start_logical()
        with self.assertRaises(ExperimentBudgetExceeded):
            budget.start_logical()
        for _ in range(36):
            self.assertEqual(budget.reserve_http(), 2)
        with self.assertRaises(ExperimentBudgetExceeded):
            budget.reserve_http()

    def test_source_audit_experiment_refuses_input_output_overlap(self):
        from scripts.replay_insight_report_v3_source_audit import validate_paths
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "findings.json", root / "session.json"
            first.write_text("{}", encoding="utf-8")
            second.write_text("{}", encoding="utf-8")
            args = SimpleNamespace(runtime_root=root / "runtime", output=root / "result.json", findings=first, session=second)
            self.assertEqual(validate_paths(args)[0], root / "runtime")
            args.env_file = root / "missing.env"
            with self.assertRaises(ValueError):
                validate_paths(args)
            args.env_file = None
            args.output = first
            with self.assertRaises(ValueError):
                validate_paths(args)
            args.output = root / "result.json"
            args.runtime_root = root
            with self.assertRaises(ValueError):
                validate_paths(args)

    def test_quality_metrics_separate_body_and_appendix(self):
        report = "# 报告\n\n## 三、核心发现\n\n解释关系。\n\n## 附录 A：完整证据索引\n\n证据。"
        metrics = replay._report_quality_metrics(report)
        self.assertEqual(metrics["core_characters"], len("解释关系。"))
        self.assertEqual(metrics["evidence_point_rows"], 0)
        self.assertEqual(metrics["unexpanded_stat_tokens"], 0)
        self.assertGreater(metrics["appendix_characters"], 0)

    def test_unknown_usage_is_not_zero_and_open_attempt_is_incomplete(self):
        for events in ([], [{"status": "started"}], [{"status": "failed"}]):
            usage = replay._attempt_summary(events)["usage"]
            self.assertFalse(usage["available"])
            self.assertFalse(usage["complete"])
            self.assertIsNone(usage["total_tokens"])
        usage = replay._attempt_summary([
            {"status": "started"}, {"status": "started"},
            {"status": "completed", "usage_complete": True,
             "usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10}},
        ])["usage"]
        self.assertEqual(usage["total_tokens"], 10)
        self.assertFalse(usage["complete"])

    def test_attempt_record_excludes_secrets_and_payloads(self):
        result = replay._diagnostic_attempt({
            "call_id": "call-1", "status": "failed", "attempt_kind": "model_fallback",
            "http_status": 502, "error_category": "http_error", "api_key": "secret",
            "headers": {"Authorization": "secret"}, "messages": ["player text"],
        })
        self.assertEqual(set(result), {"call_id", "status", "attempt_kind", "http_status", "error_category"})

    def test_version_state_detects_plan_changes_without_exposing_reports(self):
        session = {"plan": {"title": "old"}, "report_versions": [{"version": 1, "report_md": "private"}]}
        before = replay._version_state(session)
        session["plan"]["title"] = "new"
        self.assertNotEqual(before["plan_and_versions_sha256"], replay._version_state(session)["plan_and_versions_sha256"])
        self.assertNotIn("private", json.dumps(before))

    async def test_stream_failure_keeps_diagnostics_attempts_and_version_integrity(self):
        session = {
            "rows": [["Q"], ["text"]], "plan": {"columns": []}, "open_text": {0: ["text"]},
            "stats_md": "stats", "file_sha256": "a" * 64,
            "report_versions": [{"version": 3, "report_md": "old"}],
            "active_report_version": 3, "next_report_version": 4,
        }
        diagnostics = {"status": "rejected", "logical_calls": 2,
                       "rejection": {"stage": "profile_dimension", "code": "dimension_mismatch"},
                       "candidate_analysis_focus": {"core_question": "母语差异"}, "candidate_applied": False}

        async def stream(*_args, **_kwargs):
            observer = current_llm_attempt_observer()
            await observer({"call_id": "c1", "status": "started", "model": "test"})
            await observer({"call_id": "c1", "status": "failed", "error_type": "TimeoutError"})
            for item in (
                {"type": "report_llm_status", "report_llm_usage": {"attempts": 1}},
                {"type": "report_regeneration_diagnostics", "diagnostics": diagnostics},
                {"type": "error", "message": "未生成新版本"},
            ):
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"

        args = SimpleNamespace(session_id="test-session", base_version=3, instruction="按性别分组")
        with (patch("app.storage.sessions.get_session", side_effect=lambda _sid: deepcopy(session)),
              patch("app.services.survey_service.report_stream", new=stream)):
            with self.assertRaises(replay.ReplayFailure) as caught:
                await replay._run(args)
        evidence = caught.exception.evidence
        self.assertEqual(evidence["regeneration_diagnostics"], diagnostics)
        self.assertTrue(evidence["version_state"]["unchanged"])
        self.assertEqual(evidence["metrics"]["all_attempts"]["http_attempts"], 1)
        self.assertEqual(evidence["metrics"]["all_attempts"]["failures"], {"TimeoutError": 1})
        self.assertIsNone(evidence["metrics"]["all_attempts"]["usage"]["total_tokens"])
        self.assertEqual(evidence["metrics"]["stream_report_llm_usage"], {"attempts": 1})
        self.assertEqual(len(evidence["attempt_events"]), 2)


class InsightV3ReplayMainTests(unittest.TestCase):
    def test_main_persists_failure_evidence_instead_of_generic_exception_only(self):
        evidence = {"status": "failed", "error": "rejected", "error_type": "RuntimeError",
                    "regeneration_diagnostics": {"status": "rejected", "logical_calls": 2},
                    "version_state": {"unchanged": True}, "metrics": {"http_attempts": 3}}
        parent = Path(os.environ["DATA_DIR"]).parent if os.environ.get("DATA_DIR") else None
        with tempfile.TemporaryDirectory(dir=parent) as directory:
            args = SimpleNamespace(runtime_root=Path(directory), env_file=None,
                                   question_concurrency=2, chapter_concurrency=2, run_label="test")
            with (patch.object(replay, "_parse_args", return_value=args),
                  patch.object(replay, "_run", new=AsyncMock(side_effect=replay.ReplayFailure(evidence))),
                  patch.object(replay.os, "chdir"), patch.dict(os.environ), patch("builtins.print")):
                self.assertEqual(replay.main(), 1)
            saved = json.loads((Path(directory) / "evidence/full-report-failure-test.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["regeneration_diagnostics"], evidence["regeneration_diagnostics"])
            self.assertTrue(saved["version_state"]["unchanged"])
            self.assertEqual(saved["metrics"]["http_attempts"], 3)


class FrozenSourceAuditReplayTests(unittest.IsolatedAsyncioTestCase):
    def _fixture(self, root):
        from scripts import replay_insight_report_v3_source_audit as source_replay
        finding = {"id": "7/f1", "claim": "失败归因会带来压力", "kind": "pain",
                   "question": "失败时有什么感受", "caveat": "仅代表本题", "response_ids": ["r1"]}
        source = {"prompt_version": "frozen-findings", "findings": [finding],
                  "source_registry": {"r1": {"response_id": "r1", "text": "队友归责让我有压力。"}}}
        session = {"rows": [["问题"], ["队友归责让我有压力。"]], "plan": {"columns": []}}
        inputs = [root / "findings.json", root / "session.json"]
        for path, data in zip(inputs, (source, session)):
            path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        block = {"id": "b1", "heading": "协作压力", "body": "队友归责使失败体验更难承受。",
                 "finding_ids": ["7/f1"], "evidence": []}
        frozen = {"state": "finished", "input_sha256": {str(p): source_replay.sha(p) for p in inputs},
                  "findings": source["findings"], "versions": {"writer": "frozen-writer"},
                  "chapters_before_audit": [{"id": "q7", "title": "体验", "blocks": [deepcopy(block)],
                                             "diagnostics": {"status": "degraded"}}],
                  "core_before_audit": {"blocks": [deepcopy(block)], "diagnostics": {"status": "degraded"},
                                        "sample_overview": {"body": "本次仅核对冻结样本。"}},
                  "actions_before_audit": {"actions": [], "diagnostics": {"status": "complete"}}}
        audit_input = root / "frozen.json"
        audit_input.write_text(json.dumps(frozen, ensure_ascii=False), encoding="utf-8")
        args = SimpleNamespace(findings=inputs[0], session=inputs[1], audit_input=audit_input,
                               runtime_root=root / "runtime", output=root / "result.json", env_file=None)
        return args, source, frozen

    def test_frozen_snapshot_requires_complete_matching_inputs_and_saved_text(self):
        from scripts import replay_insight_report_v3_source_audit as source_replay
        with tempfile.TemporaryDirectory() as folder:
            args, source, frozen = self._fixture(Path(folder))
            inputs = [args.findings, args.session]
            self.assertEqual(source_replay.load_frozen_audit(args.audit_input, inputs=inputs, source=source), frozen)
            variants = []
            for key, value in (("state", "started"), ("input_sha256", {}), ("findings", []),
                               ("chapters_before_audit", []), ("core_before_audit", {}),
                               ("actions_before_audit", {})):
                changed = deepcopy(frozen)
                changed[key] = value
                variants.append((key, changed))
            for key, changed in variants:
                with self.subTest(field=key):
                    args.audit_input.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        source_replay.load_frozen_audit(args.audit_input, inputs=inputs, source=source)
            args.audit_input.write_text(json.dumps(frozen), encoding="utf-8")
            args.session.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hashes do not match"):
                source_replay.load_frozen_audit(args.audit_input, inputs=inputs, source=source)

    def test_frozen_input_is_protected_from_runtime_and_output_overlap(self):
        from scripts import replay_insight_report_v3_source_audit as source_replay
        with tempfile.TemporaryDirectory() as folder:
            args, _, _ = self._fixture(Path(folder))
            self.assertEqual(source_replay.validate_paths(args)[2][-1], args.audit_input)
            args.output = args.audit_input
            with self.assertRaises(ValueError):
                source_replay.validate_paths(args)
            args.output = Path(folder) / "result.json"
            args.runtime_root = Path(folder) / "frozen-runtime"
            args.runtime_root.mkdir()
            args.audit_input = args.runtime_root / "frozen.json"
            args.audit_input.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                source_replay.validate_paths(args)

    async def _run_frozen_case(self, status="passed", *, error=None, invalid_snapshot=False):
        from app.core import config
        from app.services import report_engine, report_source_translation
        from app.services.insight_report_v3 import audit, chapters
        from scripts import replay_insight_report_v3_source_audit as source_replay
        with tempfile.TemporaryDirectory() as folder:
            args, source, frozen = self._fixture(Path(folder))
            if invalid_snapshot:
                args.audit_input.write_text("{}", encoding="utf-8")
            original_bytes = {p: p.read_bytes() for p in (args.findings, args.session, args.audit_input)}
            result = {"diagnostics": {"status": status}, "core_blocks": frozen["core_before_audit"]["blocks"],
                      "chapters": frozen["chapters_before_audit"], "actions": [],
                      "sample_overview": frozen["core_before_audit"]["sample_overview"], "unresolved": []}
            audit_call = AsyncMock(return_value=deepcopy(result), side_effect=error)
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ))
                stack.enter_context(patch.object(config, "DATA_DIR", str(args.runtime_root)))
                stack.enter_context(patch.object(config, "LLM_API_KEY", "synthetic-test-key"))
                stack.enter_context(patch.object(audit, "audit_report_sections", new=audit_call))
                stack.enter_context(patch("builtins.print"))
                guards = [stack.enter_context(patch.object(owner, name, side_effect=AssertionError("unexpected generation/network")))
                          for owner, name in ((chapters, "write_chapters"), (chapters, "write_core_findings"),
                                              (chapters, "write_actions"), (chapters, "extract_group_findings"),
                                              (report_engine, "_direct_json_call"),
                                              (report_engine, "collect_chat_completion"),
                                              (report_source_translation, "collect_chat_completion"))]
                if invalid_snapshot:
                    with self.assertRaisesRegex(ValueError, "finished saved experiment"):
                        await source_replay.run(args)
                    audit_call.assert_not_called()
                    self.assertFalse(args.output.exists())
                    saved, exit_code = None, None
                else:
                    exit_code = await source_replay.run(args)
                    saved = json.loads(args.output.read_text(encoding="utf-8"))
                    audit_call.assert_awaited_once()
                    self.assertEqual(audit_call.call_args.kwargs["chapters"], frozen["chapters_before_audit"])
                    self.assertEqual(audit_call.call_args.kwargs["core_blocks"], frozen["core_before_audit"]["blocks"])
                    self.assertEqual(audit_call.call_args.kwargs["sample_overview"], frozen["core_before_audit"]["sample_overview"])
                    self.assertEqual(saved["audit_preflight"]["units"], 4)
                    self.assertEqual(saved["findings"], source["findings"])
                    self.assertEqual(saved["logical_calls"], 0)
                    self.assertEqual(saved["http_attempts"], 0)
                    self.assertEqual(saved["semantic_acceptance"], "not_reviewed")
                    self.assertTrue(saved["inputs_unchanged"])
                    self.assertEqual(args.output.with_suffix(".md").exists(), error is None)
                    with self.assertRaises(ValueError):
                        source_replay.validate_paths(args)
                for guard in guards:
                    guard.assert_not_called()
            self.assertTrue(all(p.read_bytes() == old for p, old in original_bytes.items()))
            return exit_code, saved

    async def test_frozen_replay_skips_generation_and_preserves_inherited_degradation(self):
        exit_code, saved = await self._run_frozen_case()
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved["state"], "finished")
        self.assertEqual(saved["execution_status"], "complete")
        self.assertFalse(saved["generation_replayed"])
        self.assertTrue(saved["frozen_text_unchanged_before_audit"])
        self.assertEqual(saved["versions"]["writer"], "frozen-writer")
        self.assertEqual(saved["inherited_generation_diagnostics"]["core"]["status"], "degraded")

    async def test_incomplete_audit_is_not_reported_as_successful_acceptance(self):
        exit_code, saved = await self._run_frozen_case("needs_review")
        self.assertEqual(exit_code, 0)
        self.assertEqual(saved["execution_status"], "degraded_or_needs_review")

    async def test_audit_exception_saves_failure_without_final_markdown(self):
        exit_code, saved = await self._run_frozen_case(error=RuntimeError("synthetic audit failure"))
        self.assertEqual(exit_code, 1)
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["error_type"], "RuntimeError")

    async def test_invalid_frozen_snapshot_stops_before_audit_or_output(self):
        await self._run_frozen_case(invalid_snapshot=True)


if __name__ == "__main__":
    unittest.main()
