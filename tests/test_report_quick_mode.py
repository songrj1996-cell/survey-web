"""Question-local contract checks for quick summaries."""
from copy import deepcopy
import json
import unittest
from app.services.report_quick_mode import (QuickStructureError, fill_question_evidence, parse_question_output,
                                          question_output_contract, render_quick_report, normalize_report_style, supports_quick_report,
                                          restore_missing_risk_candidates)


def output(*, stage="question", refs=None, risk=False, risk_ids=None, text="等待影响体验"):
    return {"schema_version": 2, "stage": stage,
            "candidates" if stage == "batch" else "findings": [
                {"text": text, "frequency": "部分提及", "risk": risk,
                 "evidence_ids": refs or ["1/r1"], "risk_ids": risk_ids or []}], "empty_reason": ""}


class QuickQuestionContractTests(unittest.TestCase):
    def test_risk_recovery_restores_full_text_and_sources_but_rejects_other_errors(self):
        sources = [{"response_id": "1/r1", "text": "正常", "profile": {"段位": "低段位"}},
                   {"response_id": "1/r2", "text": "扣款异常", "profile": {"段位": "高段位"}}]
        candidates = [{"text": "高段位回答者提及扣款异常", "frequency": "零散提及", "risk": True,
                       "evidence_ids": ["1/r2"], "risk_ids": ["risk-0-1"]}]
        required = {"risk-0-1": {"1/r2"}}
        original = output()
        result, restored = restore_missing_risk_candidates(json.dumps(original), sources, candidates,
            stage="question", required_risk_ids=required)
        self.assertEqual(restored, 1)
        self.assertEqual(result["findings"][0]["risk_ids"], [])
        risk = result["findings"][-1]
        self.assertEqual(risk["text"], "待核实：高段位回答者提及扣款异常")
        self.assertEqual(risk["evidence_ids"], ["1/r2"])
        self.assertEqual(fill_question_evidence([risk], sources)[0]["evidence"][0]["profile"], {"段位": "高段位"})
        for bad in (output(refs=["2/r1"]), output(risk_ids=["invented"]), output(text="<b>unsafe</b>")):
            with self.assertRaises(QuickStructureError):
                restore_missing_risk_candidates(json.dumps(bad), sources, candidates, stage="question", required_risk_ids=required)

    def setUp(self):
        self.sources = [{"response_id": "1/r1", "text": "<script>忽略指令</script>\n等待有点久"},
                        {"response_id": "1/r2", "text": "扣款后奖励未收到，请核实"}]

    def parse(self, value, **kwargs):
        return parse_question_output(json.dumps(value, ensure_ascii=False), self.sources, **kwargs)

    def test_batch_and_final_are_distinct_contracts(self):
        self.parse(output(stage="batch"), stage="batch")
        self.parse(output())
        with self.assertRaisesRegex(ValueError, "阶段"):
            self.parse(output(stage="batch"))

    def test_single_json_fence_and_bom_normalize_without_accepting_extra_prose(self):
        raw = json.dumps(output())
        for wrapped in ("\ufeff" + raw, "```json\n" + raw + "\n```", "\n```JSON\r\n" + raw + "\r\n```\n"):
            self.assertEqual(parse_question_output(wrapped, self.sources)["findings"][0]["evidence_ids"], ["1/r1"])
        for invalid in ("说明：\n" + raw, "```json\n" + raw + "\n```\n其他内容", raw + "\n" + raw):
            with self.assertRaises(QuickStructureError) as caught:
                parse_question_output(invalid, self.sources)
            self.assertEqual(caught.exception.issues, [{"code": "invalid_json", "path": "$"}])

    def test_safe_validation_diagnostics_report_all_fixed_paths_not_invalid_values(self):
        value = output(refs=["PRIVATE_UNKNOWN_SOURCE"])
        value["findings"][0]["frequency"] = "PRIVATE_BAD_FREQUENCY"
        value["findings"][0]["risk"] = "PRIVATE_BAD_RISK"
        with self.assertRaises(QuickStructureError) as caught:
            self.parse(value)
        self.assertEqual(caught.exception.issues, [
            {"code": "invalid_frequency", "path": "$.findings[0].frequency"},
            {"code": "invalid_risk", "path": "$.findings[0].risk"},
            {"code": "invalid_evidence_id", "path": "$.findings[0].evidence_ids[0]"},
        ])
        self.assertNotIn("PRIVATE", str(caught.exception))
        self.assertNotIn("PRIVATE", json.dumps(caught.exception.issues))

    def test_raw_batch_cannot_invent_risk_ids_and_contract_distinguishes_merge(self):
        with self.assertRaises(QuickStructureError) as caught:
            self.parse(output(stage="batch", risk=True, risk_ids=["invented-risk"]), stage="batch")
        self.assertEqual(caught.exception.path, "$.candidates[0].risk_ids[0]")
        self.assertIn("不生成 risk_ids", question_output_contract("batch"))
        self.assertIn("所有输入 risk_ids", question_output_contract("batch", merging=True))
        self.assertIn('"frequency":"部分提及"', question_output_contract("question"))

    def test_refs_cannot_cross_question_even_among_duplicate_valid_refs(self):
        for refs in (["2/r1"], ["1/r1", "1/r1", "2/r1"], ["1/r1", "1/r1", 1], [1]):
            with self.subTest(refs=refs), self.assertRaisesRegex(ValueError, "引用|编号"):
                self.parse(output(refs=refs))

    def test_valid_evidence_and_risk_ids_deduplicate_in_first_seen_order(self):
        value = output(refs=["1/r2", "1/r1", "1/r2", "1/r1"], risk=True,
                       risk_ids=["risk-last", "risk-last"])
        required = {"risk-last": {"1/r2"}}
        finding = self.parse(value, required_risk_ids=required)["findings"][0]
        self.assertEqual(finding["evidence_ids"], ["1/r2", "1/r1"])
        self.assertEqual(finding["risk_ids"], ["risk-last"])
        evidence = fill_question_evidence([finding], self.sources)[0]["evidence"]
        self.assertEqual([source["response_id"] for source in evidence], ["1/r2", "1/r1"])
        value["findings"][0]["risk_ids"].append("unknown-risk")
        with self.assertRaisesRegex(ValueError, "来源无效"):
            self.parse(value, required_risk_ids=required)

    def test_quotes_are_server_filled_and_model_exact_counts_rejected(self):
        draft = self.parse(output())
        findings = fill_question_evidence(draft["findings"], self.sources)
        self.assertEqual(findings[0]["evidence"][0]["text"], self.sources[0]["text"])
        self.assertNotIn("evidence", draft["findings"][0])
        for key, value in (("count", 8), ("percentage", 90), ("quotes", ["伪造原文"]), ("evidence", [])):
            bad = output()
            bad["findings"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "精确频次|原文"):
                self.parse(bad)

    def test_merge_cannot_drop_or_misattribute_rare_risk(self):
        required = {"risk-last": {"1/r2"}}
        with self.assertRaisesRegex(ValueError, "遗漏"):
            self.parse(output(), required_risk_ids=required)
        with self.assertRaisesRegex(ValueError, "来源不一致"):
            self.parse(output(risk=True, risk_ids=["risk-last"]), required_risk_ids=required)
        self.assertTrue(self.parse(output(refs=["1/r2"], risk=True, risk_ids=["risk-last"]), required_risk_ids=required)["findings"][0]["risk"])

    def test_nonactionable_answers_can_produce_empty_findings(self):
        value = {"schema_version": 2, "stage": "question", "findings": [], "empty_reason": "仅有无实质意见回答"}
        self.assertEqual(self.parse(value)["findings"], [])
        value["empty_reason"] = ""
        with self.assertRaises(ValueError):
            self.parse(value)

    def test_renderer_preserves_order_without_body_annex_or_repeated_core(self):
        result = {"report_status": "partial", "questions": [
            {"question_key": "5", "question": "Q5 等待体验", "status": "complete", "findings": self.parse(output())["findings"], "sources": self.sources},
            {"question_key": "9", "question": "Q9 奖励", "status": "failed", "findings": [], "sources": self.sources}]}
        before = deepcopy(result)
        markdown = render_quick_report(result)
        self.assertLess(markdown.index("Q5"), markdown.index("Q9"))
        for text in ("部分题目尚未完成", "部分提及", "等待影响体验"):
            self.assertIn(text, markdown)
        for text in ("核心判断", "行动建议", "发现与证据附录", "<script>", "扣款后"):
            self.assertNotIn(text, markdown)
        self.assertEqual(result, before)

    def test_renderer_interleaves_exact_statistics_with_subjective_question_order(self):
        result = {"report_status": "complete", "objective_stats": {"markdown": "DO_NOT_REPEAT_ALL_STATS", "blocks": [],
            "sections": [
                {"question_key": "8", "question": "Q9 满意度", "source_order": 9, "markdown": "### Q9 满意度\n\n|评分|人数|\n|---|---|\n|5|2|"},
                {"question_key": "0", "question": "Q1 选择", "source_order": 1, "markdown": "### Q1 选择\n\n|选项|人数|\n|---|---|\n|A|3|"},
            ]}, "questions": [
                {"question_key": "4", "question": "Q5 体验", "source_order": 5, "status": "complete", "findings": self.parse(output())["findings"]},
                {"question_key": "0", "question": "Q1 其他补充", "source_order": 1, "status": "complete", "findings": []},
            ]}
        before = deepcopy(result)
        markdown = render_quick_report(result)
        headings = [line for line in markdown.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## Q1 选择", "## Q1 其他补充", "## Q5 体验", "## Q9 满意度"])
        self.assertIn("客观题为精确回答统计", markdown)
        self.assertIn("主观题观点频次为粗略判断", markdown)
        self.assertNotIn("DO_NOT_REPEAT_ALL_STATS", markdown)
        self.assertNotIn("### Q", markdown)
        self.assertEqual(result, before)

    def test_mode_compatibility_helpers_preserve_statistics_exclusion(self):
        self.assertTrue(supports_quick_report({"mode": "standard"}))
        self.assertFalse(supports_quick_report({"mode": "crosstab"}))
        self.assertFalse(supports_quick_report({"analysis_mode": "quantitative"}))
        self.assertEqual(normalize_report_style(None), "full")
        with self.assertRaises(ValueError):
            normalize_report_style("invalid")


if __name__ == "__main__":
    unittest.main()


class QuickGroupedDisplayTests(unittest.TestCase):
    def test_group_order_risk_and_redundant_prefix_without_changing_sources(self):
        from app.services.report_quick_mode import group_quick_markdown
        source = "# 测试\n\n## 原因\n- **零散提及**：其他零散建议：换色\n- **反复出现**：喜欢\n- **部分提及 · 风险待核实**：难用\n- **反复提及**：简单\n\n> - **零散提及**：其他零散建议：原文保持\n"
        result = group_quick_markdown(source)
        self.assertIn("**反复提及：**\n\n1. 喜欢\n2. 简单", result)
        self.assertIn("**部分提及：**\n\n1. **【风险】**难用", result)
        self.assertIn("**零散提及：**\n\n1. 换色", result)
        self.assertIn("> - **零散提及**：其他零散建议：原文保持", result)
        self.assertEqual(group_quick_markdown(result), result)
