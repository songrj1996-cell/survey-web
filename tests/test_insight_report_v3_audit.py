import asyncio
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from app.services.insight_report_v3 import audit


def _fixture(*, second_block: bool = False):
    findings = {
        "1/f1": {
            "id": "1/f1",
            "claim": "失败归因会带来压力",
            "kind": "pain",
            "question": "遇到失败时有什么感受",
            "caveat": "仅代表本题",
            "response_ids": ["global-1"],
            "skewed_toward": [],
        },
    }
    registry = {
        "global-1": {
            "response_id": "global-1",
            "respondent_ref": "player-1",
            "text": "输了以后队友只怪我，所以压力很大。",
            "profile": {},
        },
    }
    core = [{
        "id": "core-1",
        "heading": "失败归因带来压力",
        "body": "失败时被队友归责会放大体验压力。",
        "finding_ids": ["1/f1"],
        "evidence": [{
            "response_id": "global-1",
            "evidence_point": "失败后被队友归责会放大压力",
            "translation_zh": "输了以后队友只怪我，所以压力很大。",
            "translation_status": "source_zh",
        }],
    }]
    chapters = []
    if second_block:
        chapters = [{
            "id": "c01",
            "title": "压力来源",
            "blocks": [{
                "id": "b1",
                "heading": "协作不足增加压力",
                "body": "队友归责使失败体验更难承受。",
                "finding_ids": ["1/f1"],
                "evidence": [],
            }],
            "diagnostics": {"status": "complete"},
        }]
    return core, chapters, findings, registry


def _result(data, *, model="mock-audit", error=""):
    return {
        "data": data,
        "model": model,
        "repaired": False,
        "error": error,
        "duration_seconds": 0.01,
    }


class InsightV3AuditTests(unittest.IsolatedAsyncioTestCase):
    def test_dynamic_packing_covers_real_scale_unit_counts(self):
        for unit_count in (100, 101, 127):
            with self.subTest(unit_count=unit_count):
                registry = {
                    f"global-{index}": {"text": f"来源文本 {index}"}
                    for index in range(unit_count)
                }
                units = [{
                    "claim_id": f"c{index + 1:03d}",
                    "kind": "evidence_point",
                    "section_title": "测试章节",
                    "findings": [],
                    "source_ids": [f"global-{index}"],
                    "summary": f"证据要点 {index}",
                } for index in range(unit_count)]
                batches, oversized = audit._pack_units(
                    units,
                    source_registry=registry,
                    query_builder=audit._audit_query,
                )
                self.assertEqual(oversized, [])
                self.assertEqual(sum(map(len, batches)), unit_count)
                self.assertTrue(all(
                    len(batch) <= audit.INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT
                    for batch in batches
                ))
                self.assertTrue(all(
                    len(audit._audit_query(batch, registry))
                    <= audit.INSIGHT_V3_AUDIT_INPUT_CHARS
                    for batch in batches
                ))
                self.assertGreater(len(batches), 2)

    def test_single_unit_over_input_target_runs_as_isolated_batch(self):
        registry = {
            "global-1": {"text": "超长来源" * 200},
            "global-2": {"text": "普通来源"},
        }
        units = [
            {
                "claim_id": "c001",
                "kind": "block",
                "section_title": "测试章节",
                "findings": [],
                "source_ids": ["global-1"],
                "heading": "超长单元",
                "body": "正文",
            },
            {
                "claim_id": "c002",
                "kind": "evidence_point",
                "section_title": "测试章节",
                "findings": [],
                "source_ids": ["global-2"],
                "summary": "普通要点",
            },
        ]
        short_size = len(audit._audit_query([units[1]], registry))
        with patch.object(audit, "INSIGHT_V3_AUDIT_INPUT_CHARS", short_size + 1):
            batches, over_target = audit._pack_units(
                units,
                source_registry=registry,
                query_builder=audit._audit_query,
            )
        self.assertEqual(batches, [[units[0]], [units[1]]])
        self.assertEqual(over_target, [units[0]])

    async def test_over_target_unit_is_audited_and_not_counted_as_capacity_issue(self):
        core, chapters, findings, registry = _fixture()
        units = audit.build_audit_units(
            core_blocks=core,
            chapters=chapters,
            findings_by_id=findings,
            source_registry=registry,
        )
        target = len(audit._audit_query([units[0]], registry)) - 1
        call = AsyncMock(return_value=_result({"issues": []}))
        with (
            patch.object(audit, "INSIGHT_V3_AUDIT_INPUT_CHARS", target),
            patch.object(audit, "INSIGHT_V3_AUDIT_CONCURRENCY", 1),
            patch.object(audit, "_direct_json_call", new=call),
        ):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["initial_coverage_rate"], 1.0)
        self.assertEqual(diagnostics["capacity_issue_count"], 0)
        self.assertEqual(diagnostics["unit_status_counts"]["incomplete"], 0)
        self.assertEqual(diagnostics["over_target_initial_unit_count"], 1)
        self.assertEqual(diagnostics["over_target_call_count"], 1)
        self.assertTrue(diagnostics["calls"][0]["input_target_exceeded"])

    async def test_model_sees_only_short_source_ids(self):
        core, chapters, findings, registry = _fixture()
        units = audit.build_audit_units(
            core_blocks=core,
            chapters=chapters,
            findings_by_id=findings,
            source_registry=registry,
        )
        query = audit._audit_query(units, registry)
        self.assertIn('"source_id":"r001"', query)
        self.assertNotIn("global-1", query)

    async def test_clean_audit_passes_without_rewriting(self):
        core, chapters, findings, registry = _fixture()
        call = AsyncMock(return_value=_result({"issues": []}))
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        self.assertEqual(result["diagnostics"]["status"], "passed")
        self.assertEqual(result["diagnostics"]["logical_call_count"], 1)
        self.assertEqual(result["core_blocks"], core)
        self.assertEqual(result["unresolved"], [])

    async def test_unsupported_evidence_point_falls_back_to_index(self):
        core, chapters, findings, registry = _fixture()
        issue = {
            "claim_id": "c002",
            "problem_type": "unsupported_detail",
            "problem": "证据要点加入了原文没有的影响",
            "severity": "blocking",
        }
        with patch.object(
            audit, "_direct_json_call", new=AsyncMock(return_value=_result({"issues": [issue]})),
        ):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        self.assertEqual(result["diagnostics"]["status"], "repaired")
        self.assertEqual(result["diagnostics"]["evidence_point_fallback_count"], 1)
        self.assertEqual(result["core_blocks"][0]["evidence"][0]["evidence_point"], "")
        self.assertEqual(core[0]["evidence"][0]["evidence_point"], "失败后被队友归责会放大压力")

    async def test_body_issue_gets_one_repair_and_one_reaudit(self):
        core, chapters, findings, registry = _fixture()
        issue = {
            "claim_id": "c001",
            "problem_type": "unsupported_causality",
            "problem": "正文将并列表述写成因果",
            "severity": "blocking",
        }
        call = AsyncMock(side_effect=[
            _result({"issues": [issue]}),
            _result({"repairs": [{
                "claim_id": "c001",
                "heading": "失败归因与压力同时出现",
                "body": "反馈同时提到队友归责和压力感受。",
            }]}, model="mock-writer"),
            _result({"issues": []}),
        ])
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        self.assertEqual(result["diagnostics"]["status"], "repaired")
        self.assertEqual(result["diagnostics"]["logical_call_count"], 3)
        self.assertEqual(result["diagnostics"]["repaired_count"], 1)
        self.assertEqual(result["core_blocks"][0]["body"], "反馈同时提到队友归责和压力感受。")

    async def test_unresolved_after_single_repair_is_preserved_for_manual_review(self):
        core, chapters, findings, registry = _fixture()
        initial = {
            "claim_id": "c001",
            "problem_type": "unsupported_detail",
            "problem": "正文有无来源细节",
            "severity": "blocking",
        }
        remaining = {**initial, "problem": "修复后仍有无来源细节"}
        call = AsyncMock(side_effect=[
            _result({"issues": [initial]}),
            _result({"repairs": [{
                "claim_id": "c001",
                "heading": "失败压力",
                "body": "队友归责与压力感受同时出现。",
            }]}),
            _result({"issues": [remaining]}),
        ])
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        self.assertEqual(result["diagnostics"]["status"], "needs_review")
        self.assertEqual(result["unresolved"][0]["reason"], "unresolved_after_repair")
        self.assertEqual(call.await_count, 3)

    async def test_dynamic_batches_cover_all_units_without_budget_overflow(self):
        core, chapters, findings, registry = _fixture(second_block=True)
        units = audit.build_audit_units(
            core_blocks=core,
            chapters=chapters,
            findings_by_id=findings,
            source_registry=registry,
        )
        single_limit = max(len(audit._audit_query([unit], registry)) for unit in units) + 1
        call = AsyncMock(return_value=_result({"issues": []}))
        with (
            patch.object(audit, "INSIGHT_V3_AUDIT_INPUT_CHARS", single_limit),
            patch.object(audit, "INSIGHT_V3_AUDIT_CONCURRENCY", 1),
            patch.object(audit, "_direct_json_call", new=call),
        ):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["status"], "passed")
        self.assertEqual(diagnostics["initial_coverage_rate"], 1.0)
        self.assertEqual(diagnostics["capacity_issue_count"], 0)
        self.assertEqual(diagnostics["audited_unit_count"], len(units))
        self.assertEqual(call.await_count, len(units))

    async def test_all_repair_and_reaudit_batches_run_once(self):
        core, chapters, findings, registry = _fixture(second_block=True)
        first_issue = {
            "claim_id": "c001",
            "problem_type": "unsupported_causality",
            "problem": "正文将并列表述写成因果",
            "severity": "blocking",
        }
        second_issue = {
            "claim_id": "c003",
            "problem_type": "unsupported_detail",
            "problem": "正文增加了原文没有的程度",
            "severity": "blocking",
        }
        call = AsyncMock(side_effect=[
            _result({"issues": [first_issue]}),
            _result({"issues": []}),
            _result({"issues": [second_issue]}),
            _result({"repairs": [{
                "claim_id": "c001",
                "heading": "失败归因与压力同时出现",
                "body": "反馈同时提到队友归责和压力感受。",
            }]}),
            _result({"repairs": [{
                "claim_id": "c003",
                "heading": "协作不足与压力同时出现",
                "body": "反馈同时提到队友归责和压力感受。",
            }]}),
            _result({"issues": []}),
            _result({"issues": []}),
        ])
        with (
            patch.object(audit, "INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT", 1),
            patch.object(audit, "INSIGHT_V3_AUDIT_CONCURRENCY", 1),
            patch.object(audit, "_direct_json_call", new=call),
        ):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["status"], "repaired")
        self.assertEqual(diagnostics["initial_batch_count"], 3)
        self.assertEqual(diagnostics["repair_batch_count"], 2)
        self.assertEqual(diagnostics["reaudit_batch_count"], 2)
        self.assertEqual(diagnostics["logical_call_count"], 7)
        self.assertEqual(diagnostics["repaired_count"], 2)
        self.assertEqual(diagnostics["unit_status_counts"]["repaired"], 2)
        self.assertEqual(sum(diagnostics["unit_status_counts"].values()), 3)

    async def test_failed_initial_batch_is_incomplete_not_passed(self):
        core, chapters, findings, registry = _fixture()
        call = AsyncMock(side_effect=[
            _result(None, error="output_truncated"),
            _result({"issues": []}),
        ])
        with (
            patch.object(audit, "INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT", 1),
            patch.object(audit, "INSIGHT_V3_AUDIT_CONCURRENCY", 1),
            patch.object(audit, "_direct_json_call", new=call),
        ):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        diagnostics = result["diagnostics"]
        self.assertEqual(diagnostics["status"], "incomplete")
        self.assertEqual(diagnostics["audited_unit_count"], 1)
        self.assertEqual(diagnostics["unit_status_counts"]["incomplete"], 1)
        self.assertEqual(diagnostics["unit_status_counts"]["passed"], 1)
        self.assertEqual(result["unresolved"][0]["reason"], "initial_audit_failed")

    async def test_bounded_runner_obeys_configured_concurrency(self):
        active = 0
        peak = 0

        def factory(value):
            async def run():
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(0.01)
                active -= 1
                return value
            return run

        with patch.object(audit, "INSIGHT_V3_AUDIT_CONCURRENCY", 2):
            results, observed_peak = await audit._run_bounded_factories([
                factory(index) for index in range(6)
            ])
        self.assertEqual(results, list(range(6)))
        self.assertEqual(peak, 2)
        self.assertEqual(observed_peak, 2)

    async def test_attempt_failure_category_is_bound_to_step_and_batch(self):
        core, chapters, findings, registry = _fixture()

        async def fake_call(*_args, **kwargs):
            callback = kwargs["on_attempt_event"]
            await callback({
                "status": "started",
                "max_output_tokens": 12000,
            })
            await callback({
                "status": "failed",
                "error_category": "output_truncated",
            })
            await callback({
                "status": "started",
                "max_output_tokens": 12000,
            })
            await callback({"status": "completed"})
            return _result({"issues": []})

        with patch.object(audit, "_direct_json_call", new=fake_call):
            result = await audit.audit_report_sections(
                core_blocks=core,
                chapters=chapters,
                findings_by_id=findings,
                source_registry=registry,
                profiles=[],
            )
        call = result["diagnostics"]["calls"][0]
        self.assertEqual(call["upstream_attempt_count"], 2)
        self.assertEqual(call["attempt_failures"], {"output_truncated": 1})
        self.assertEqual(call["max_output_tokens"], 12000)
        self.assertEqual(
            result["diagnostics"]["attempt_failures_by_step"],
            {"initial_audit": {"output_truncated": 1}},
        )

    def test_frequency_discovery_is_review_only_and_does_not_mutate_content(self):
        core, chapters, _findings, _registry = _fixture()
        core[0]["body"] = "多数玩家认为责任提示不清楚。"
        before = deepcopy(core)
        result = audit.scan_frequency_review_candidates(
            core_blocks=core,
            chapters=chapters,
            actions=[],
        )
        self.assertEqual(result["status"], "needs_manual_review")
        self.assertGreaterEqual(result["candidate_count"], 1)
        self.assertEqual(result["auto_blocked_count"], 0)
        self.assertEqual(core, before)


if __name__ == "__main__":
    unittest.main()
