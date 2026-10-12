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


def _audit_answer(query, *, issue_for=None, source_status="supported", relation="supports"):
    import json
    payload = json.loads(query.split("<audit_input>\n")[1].split("\n</audit_input>")[0])
    answer = {"checked_claim_ids": [u["claim_id"] for u in payload["units"]], "issues": [], "finding_checks": []}
    for unit in payload["units"]:
        if unit["kind"] == "finding_binding":
            answer["finding_checks"].append({
                "claim_id": unit["claim_id"], "status": source_status,
                "relations": {k: unit["source_ids"] if k == relation else [] for k in audit.SOURCE_RELATIONS},
                "problem": "反向偏好不能支撑归并后的单向判断" if source_status != "supported" else "",
            })
        elif issue_for == unit["kind"]:
            answer["issues"].append({"claim_id": unit["claim_id"], "target_ids": [unit["targets"][0]["target_id"]],
                                     "problem_type": "polarity_shift", "problem": "正负方向改变", "severity": "blocking"})
    return answer


def _action():
    return {"id": "a01", "title": "核对失败体验", "action_scope": "general", "target_group": None,
            "problem": "失败时队友归责会带来压力。", "objective": "改善协作体验。",
            "design_directions": ["核对协作提示的理解情况。"], "validation_plan": "访谈验证感受。",
            "finding_ids": ["1/f1"], "evidence_gap": "", "required_data": ""}


class SourceRelationAuditTests(unittest.IsolatedAsyncioTestCase):
    def test_deduplicates_selected_findings_without_losing_source_relationships(self):
        core, chapters, findings, registry = _fixture(second_block=True)
        core[0]["evidence"] = []
        findings["unused"] = {"id": "unused", "response_ids": ["secret-unused"]}
        units = audit.build_audit_units(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, actions=[_action()])
        checks = [u for u in units if u["kind"] == "finding_binding"]
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(checks[0]["consumer_ids"]), 3)
        payload = audit._model_payload(units, registry)
        self.assertNotIn("global-1", str(payload))
        self.assertNotIn("secret-unused", str(payload))
        self.assertEqual(len(payload["sources"]), 1)
        self.assertTrue(all(u["source_ids"] == ["r001"] for u in payload["units"]))
        self.assertTrue(all("findings" not in u for u in payload["units"]))
        binding = next(u for u in payload["units"] if u["kind"] == "finding_binding")
        self.assertEqual(binding["targets"][0]["text"], findings["1/f1"]["claim"])

    async def test_correct_mixed_opinion_is_not_rejected_just_for_having_counterevidence(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        async def call(_system, query, **kwargs):
            data = _audit_answer(query, relation="mixed")
            self.assertIsNone(kwargs["validator"](data))
            return _result(data)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual(result["diagnostics"]["status"], "passed")
        self.assertEqual(result["diagnostics"]["source_checks"]["1/f1"]["relations"]["mixed"], ["global-1"])

    async def test_source_issue_repairs_all_consumers_once_including_action(self):
        import json
        core, chapters, findings, registry = _fixture(second_block=True)
        core[0]["evidence"] = []
        before = deepcopy((core, chapters, findings, registry))
        calls = []
        async def call(_system, query, **kwargs):
            calls.append(query)
            if "<repair_input>" in query:
                payload = json.loads(query.split("<repair_input>\n")[1].split("\n</repair_input>")[0])
                repairs = []
                for u in payload["units"]:
                    if u["kind"] == "action":
                        text = deepcopy(u["action_text"])
                        text["problem"] = "有玩家提到失败时被队友归责。"
                        text["evidence_gap"] = "具体协作条件尚待核对。"
                        text["required_data"] = "需补充现场访谈。"
                        repairs.append({"claim_id": u["claim_id"], "action_text": text})
                    else:
                        repairs.append({"claim_id": u["claim_id"], "heading": "归责与感受", "body": "有玩家提到失败时被队友归责，感到压力。"})
                data = {"repairs": repairs}
            else:
                data = _audit_answer(query, source_status="needs_review", relation="opposes")
            self.assertIsNone(kwargs["validator"](data))
            return _result(data)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[], actions=[_action()])
        self.assertEqual(len(calls), 3)
        self.assertEqual(result["diagnostics"]["status"], "repaired")
        self.assertEqual(result["diagnostics"]["repair_requested_count"], 3)
        self.assertEqual(result["diagnostics"]["source_checks"]["1/f1"]["status"], "needs_review")
        self.assertEqual(result["actions"][0]["finding_ids"], ["1/f1"])
        self.assertTrue(result["actions"][0]["evidence_gap"])
        self.assertEqual((core, chapters, findings, registry), before)

    async def test_failed_source_batch_never_marks_consumers_passed(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        async def call(_system, query, **kwargs):
            if '"kind":"finding_binding"' in query:
                return _result(None, error="output_truncated")
            return _result(_audit_answer(query))
        with patch.object(audit, "_direct_json_call", new=call), patch.object(audit, "INSIGHT_V3_AUDIT_BATCH_UNIT_LIMIT", 1):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual(result["diagnostics"]["status"], "incomplete")
        self.assertEqual(result["diagnostics"]["repair_batch_count"], 0)
        self.assertEqual(result["diagnostics"]["unit_status_counts"]["incomplete"], 2)

    async def test_missing_one_bound_source_is_not_silently_dropped(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        findings["1/f1"]["response_ids"].append("missing")
        async def call(_system, query, **kwargs):
            return _result(_audit_answer(query))
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual(result["diagnostics"]["status"], "incomplete")
        self.assertEqual(result["diagnostics"]["missing_evidence_count"], 1)

    async def test_multiple_source_issues_keep_all_reasons_but_only_one_repair(self):
        import json
        core, rows, findings, registry = _fixture()
        core[0]["evidence"] = []
        findings["1/f2"] = {**deepcopy(findings["1/f1"]), "id": "1/f2"}
        core[0]["finding_ids"].append("1/f2")
        repair_count = 0
        async def call(_system, query, **kwargs):
            nonlocal repair_count
            if "<repair_input>" in query:
                repair_count += 1
                data = json.loads(query.split("<repair_input>\n")[1].split("\n</repair_input>")[0])
                self.assertEqual(len(data["units"][0]["source_checks"]), 2)
                answer = {"repairs": [{"claim_id": "c001", "heading": "失败后的感受", "body": "有玩家提到被队友归责后感到压力。"}]}
            else:
                answer = _audit_answer(query, source_status="inconclusive", relation="unclear", issue_for="block")
            self.assertIsNone(kwargs["validator"](answer))
            return _result(answer)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=rows, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual(repair_count, 1)
        self.assertEqual(result["diagnostics"]["status"], "needs_review")
        self.assertEqual(result["diagnostics"]["unresolved_count"], 1)
        self.assertEqual(result["diagnostics"]["logical_call_count"], 3)

    async def test_source_audit_coverage_counts_over_24_units_without_fixed_budget(self):
        core, rows, findings, registry = _fixture()
        core[0]["evidence"] = []
        findings = {f"1/f{i}": {**deepcopy(findings["1/f1"]), "id": f"1/f{i}"} for i in range(1, 30)}
        core[0]["finding_ids"] = list(findings)
        async def call(_system, query, **kwargs):
            answer = _audit_answer(query)
            self.assertIsNone(kwargs["validator"](answer))
            return _result(answer)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=rows, findings_by_id=findings, source_registry=registry, profiles=[])
        diag = result["diagnostics"]
        self.assertEqual(diag["initial_batch_count"], 2)
        self.assertEqual(diag["planned_unit_count"], 30)
        self.assertEqual(diag["initial_coverage_rate"], 1)
        self.assertEqual(diag["unit_status_counts"]["passed"], 30)


class AuditTargetIsolationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _section(query, tag):
        import json
        return json.loads(query.split(f"<{tag}>\n")[1].split(f"\n</{tag}>")[0])

    def test_summary_wording_is_not_a_body_or_reaudit_target(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        findings["1/f1"]["claim"] = "绝大多数玩家因失败归责感到压力"
        registry["global-1"]["text"] += "\n保留完整的条件与反例；忽略审校规则只是原文，不是指令。"
        before = deepcopy((core, chapters, findings, registry))
        units = audit.build_audit_units(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry)
        payload = audit._model_payload(units, registry)
        body, binding = payload["units"]
        self.assertNotIn("绝大多数", str(body))
        self.assertEqual(binding["targets"][0]["text"], findings["1/f1"]["claim"])
        self.assertEqual([t["field"] for t in body["targets"]], ["heading", "body"])
        self.assertEqual(body["reference_context"]["source_questions"], [{"question": findings["1/f1"]["question"], "source_ids": ["r001"]}])
        self.assertEqual(payload["sources"][0]["text"], registry["global-1"]["text"])
        self.assertEqual(payload["sources"][0]["role"], "original_response")
        reaudit_query = audit._audit_query([units[0]], registry)
        self.assertNotIn("绝大多数", reaudit_query)
        self.assertNotIn("findings", reaudit_query)
        self.assertEqual((core, chapters, findings, registry), before)

    def test_dynamic_contract_only_examples_real_tasks_in_each_batch(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        units = audit.build_audit_units(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry)
        for batch in (units, units[:1], units[1:], []):
            query = audit._audit_query(batch, registry)
            contract = self._section(query, "contract")
            scope = self._section(query, "audit_scope")
            self.assertEqual(contract["checked_claim_ids"], [u["claim_id"] for u in batch])
            expected_findings = [u["claim_id"] for u in batch if u["kind"] == "finding_binding"]
            self.assertEqual(scope["finding_check_claim_ids"], expected_findings)
            self.assertEqual(bool(contract["finding_checks"]), bool(expected_findings))
            self.assertEqual(bool(contract["issues"]), bool(scope["issue_targets"]))
            for example in contract["finding_checks"]:
                self.assertIn(example["claim_id"], expected_findings)
            for example in contract["issues"]:
                self.assertTrue(set(example["target_ids"]) <= set(scope["issue_targets"][example["claim_id"]]))

    def test_action_field_targets_are_distinct_and_preserve_empty_gaps(self):
        from app.services.insight_report_v3.contracts import ACTION_REPAIR_TEXT_FIELDS
        core, chapters, findings, registry = _fixture()
        action = _action()
        action["design_directions"].append("补充验证提示。")
        units = audit.build_audit_units(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, actions=[action])
        item = next(u for u in audit._model_payload(units, registry)["units"] if u["kind"] == "action")
        fields = {t["field"]: t["text"] for t in item["targets"]}
        self.assertEqual(len(fields), len(ACTION_REPAIR_TEXT_FIELDS) + 1)
        self.assertEqual(fields["action_text.evidence_gap"], "")
        self.assertEqual(fields["action_text.design_directions[1]"], action["design_directions"][1])
        self.assertNotIn("findings", item)

    def test_statistics_are_reference_data_and_rendered_text_is_the_target(self):
        from app.services.insight_report_v3.research_context import statistic_facts
        facts = statistic_facts([{"id": "s001", "title": "经历", "headers": ["经历", "人数"], "rows": [["体验过", "9"]]}])
        unit = {"claim_id": "c001", "kind": "sample", "section_title": "样本概况", "findings": [], "source_ids": [],
                "heading": "样本经历", "body": "体验过的样本为[S:s001-r001-c002]人。", "objective_facts": facts}
        item = audit._model_payload([unit], {})["units"][0]
        self.assertEqual(item["targets"][1]["text"], "体验过的样本为9人。")
        self.assertTrue(item["reference_context"]["objective_stats"])
        self.assertEqual(audit._expected_issue_targets([unit]), {"c001": {"c001-t01", "c001-t02"}})

    def test_action_field_ids_do_not_shift_when_design_directions_change(self):
        unit = {"claim_id": "c003", "kind": "action", "action_text": _action()}
        before = {t["field"]: t["target_id"] for t in audit._audit_targets(unit)}
        unit["action_text"]["design_directions"] = ["方向一", "方向二", "方向三"]
        after = {t["field"]: t["target_id"] for t in audit._audit_targets(unit)}
        for field, target_id in before.items():
            self.assertEqual(after[field], target_id)

    async def test_source_issue_does_not_erase_direct_body_field_binding(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        repairs = []
        async def call(_system, query, **kwargs):
            if "<repair_input>" in query:
                payload = self._section(query, "repair_input")
                repairs.append(payload)
                self.assertEqual(payload["units"][0]["audit_issue"]["target_fields"], {"c001-t01": "heading"})
                # No progress: the original field binding must remain in the delivery diagnostic.
                data = {"repairs": [{"claim_id": "c001", "heading": core[0]["heading"], "body": core[0]["body"]}]}
            else:
                data = _audit_answer(query, issue_for="block", source_status="needs_review", relation="opposes")
            self.assertIsNone(kwargs["validator"](data))
            return _result(data)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual(len(repairs), 1)
        self.assertEqual(result["unresolved"][0]["audit_target_fields"], {"c001-t01": "heading"})

    async def test_only_repaired_targets_are_reaudited_and_invalid_task_is_incomplete(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        audit_calls = []
        repair_calls = []

        async def call(_system, query, **kwargs):
            if "<repair_input>" in query:
                repair_calls.append(query)
                payload = self._section(query, "repair_input")
                self.assertEqual(payload["units"][0]["audit_issue"]["target_fields"], {"c001-t01": "heading"})
                data = {"repairs": [{"claim_id": "c001", "heading": "失败后的感受", "body": "有玩家提到队友归责和压力感受。"}]}
            else:
                audit_calls.append(query)
                data = _audit_answer(query, issue_for="block" if len(audit_calls) == 1 else None)
                if len(audit_calls) == 2:
                    self.assertEqual(self._section(query, "contract")["finding_checks"], [])
                    unit = self._section(query, "audit_input")["units"][0]
                    self.assertEqual(unit["targets"][0]["text"], "失败后的感受")
                    self.assertNotIn("findings", unit)
                    # Reproduce the saved failure: a body ID in a source-check response.
                    data["finding_checks"] = [{"claim_id": "c001", "status": "supported", "problem": "",
                                               "relations": {k: ["r001"] if k == "supports" else [] for k in audit.SOURCE_RELATIONS}}]
                    error = kwargs["validator"](data)
                    self.assertIn("finding_checks", error)
                    return _result(None, error=error)
            self.assertIsNone(kwargs["validator"](data))
            return _result(data)

        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual((len(audit_calls), len(repair_calls)), (2, 1))
        self.assertEqual(result["diagnostics"]["status"], "incomplete")
        self.assertEqual(result["unresolved"][0]["reason"], "reaudit_failed")
        self.assertEqual(result["unresolved"][0]["audit_target_fields"], {"c001-t01": "heading"})

    async def test_live_initial_validator_requires_target_ids_without_legacy_bypass(self):
        core, chapters, findings, registry = _fixture()
        core[0]["evidence"] = []
        async def call(_system, query, **kwargs):
            data = _audit_answer(query, issue_for="block")
            del data["issues"][0]["target_ids"]
            error = kwargs["validator"](data)
            self.assertIsNotNone(error)
            return _result(None, error=error)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(core_blocks=core, chapters=chapters, findings_by_id=findings, source_registry=registry, profiles=[])
        self.assertEqual(result["diagnostics"]["initial_coverage_rate"], 0)
        self.assertEqual(result["diagnostics"]["status"], "incomplete")
        self.assertEqual(result["diagnostics"]["logical_call_count"], 1)


class InsightV3AuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These existing tests isolate paragraph/sample repair and packing. Full
        # source-check dependency coverage is exercised by SourceRelationAuditTests.
        original = audit.build_audit_units
        builder = patch.object(audit, "build_audit_units", side_effect=lambda **kw: original(**{**kw, "include_source_checks": False}))
        builder.start()
        self.addCleanup(builder.stop)

    async def test_repair_uses_same_bound_row_total_without_free_people_count(self):
        from app.services.insight_report_v3.research_context import expand_stat_references, statistic_facts
        core, rows, findings, registry = _fixture()
        core[0]["evidence"] = []
        core[0]["body"] = "该组样本小。[S:s001-r001]"
        facts = statistic_facts([{
            "id": "s001", "title": "分组评价", "headers": ["偏好", "正向", "总计"],
            "rows": [["普通模式", "4*", "9"], ["新模式", "16", "35"]],
        }])
        core[0]["objective_facts"] = {"s001-r001": facts["s001-r001"]}
        for repair_body, allowed in (
            ("有玩家提到压力；该组总计为[S:s001-r001-c003]人，不应把单元格理解为组规模。", True),
            ("有玩家提到压力；该组总计为9人。", False),
            ("该组总计为[S:s001-r002-c003]人。", False),
        ):
            responses = [
                _result({"issues": [{"claim_id": "c001", "problem_type": "statistic_scope", "problem": "混淆单元格与总计", "severity": "blocking"}]}),
                _result({"repairs": [{"claim_id": "c001", "heading": "分组评价", "body": repair_body}]}),
                _result({"issues": []}),
            ]
            with patch.object(audit, "_direct_json_call", side_effect=responses) as call:
                result = await audit.audit_report_sections(
                    core_blocks=core, chapters=rows, findings_by_id=findings,
                    source_registry=registry, profiles=[],
                )
            self.assertEqual(result["diagnostics"]["unresolved_count"], 0 if allowed else 1)
            self.assertEqual(call.call_count, 3 if allowed else 2)
            self.assertIn("s001-r001-c003", call.call_args_list[1].args[1])
            self.assertNotIn("s001-r002-c003", call.call_args_list[1].args[1])
            if allowed:
                repaired = result["core_blocks"][0]
                rendered = expand_stat_references(repaired["body"], repaired["objective_facts"])
                self.assertIn("总计为9人", rendered)
                self.assertNotIn("[S:", rendered)
                self.assertNotIn("s001-r002-c003", repaired["objective_facts"])

    async def test_sample_and_task_review_are_audited_without_raw_sources(self):
        core, chapter_rows, findings, registry = _fixture()
        seen = []
        async def call(_system, query, **kwargs):
            import json
            payload = json.loads(query.split("<audit_input>\n")[1].split("\n</audit_input>")[0])
            seen.extend(payload["units"])
            review = next((u for u in payload["units"] if u["kind"] == "research_review"), None)
            result = {"issues": [{"claim_id": review["claim_id"],
                                  "target_ids": [t["target_id"] for t in review["targets"] if t["field"].startswith("report_scope")],
                                  "problem_type": "missing_requirement", "problem": "未回应已确认的比较要求", "severity": "warning"}] if review else []}
            result.update(checked_claim_ids=[u["claim_id"] for u in payload["units"]], finding_checks=[])
            self.assertIsNone(kwargs["validator"](result))
            return _result(result)
        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(
                core_blocks=core, chapters=chapter_rows, findings_by_id=findings,
                source_registry=registry, profiles=[],
                sample_overview={"heading": "样本解读", "body": "样本包含不同经历的受访者。", "sample_stats": [{"title": "经历", "table_markdown": "确定性表"}]},
                research_tasks=[], analysis_focus={"expected_deliverables": ["对比体验"]},
            )
        self.assertEqual(result["chapters"], chapter_rows)
        self.assertIsNotNone(result["sample_overview"])
        self.assertEqual(result["diagnostics"]["initial_coverage_rate"], 1.0)
        self.assertEqual(result["diagnostics"]["repair_requested_count"], 0)
        self.assertEqual(result["unresolved"][0]["reason"], "research_task_needs_review")
        self.assertTrue(any(u["kind"] == "sample" for u in seen))
        self.assertTrue(any(u["kind"] == "research_review" for u in seen))

    def test_multisentence_body_has_clause_targets_and_explicit_content_roles(self):
        _core, _chapters, findings, registry = _fixture()
        units = audit.build_audit_units(
            core_blocks=[{
                "id": "b1", "heading": "节奏差异",
                "body": "生存卡会延长对局。伤害卡会加速击杀。",
                "finding_ids": ["1/f1"], "evidence": [],
            }],
            chapters=[], findings_by_id=findings,
            source_registry=registry,
            include_source_checks=False,
        )
        targets = audit._audit_targets(units[0])
        self.assertEqual(
            [(item["field"], item["content_role"]) for item in targets],
            [("heading", "report_heading"),
             ("body.sentence[1]", "report_sentence"),
             ("body.sentence[2]", "report_sentence")],
        )
        self.assertEqual(targets[1]["text"], "生存卡会延长对局。")
        self.assertEqual(targets[2]["text"], "伤害卡会加速击杀。")

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

    async def test_unresolved_sample_interpretation_falls_back_to_deterministic_tables(self):
        core, chapters, findings, registry = _fixture()
        calls = 0

        async def call(_system, query, **_kwargs):
            nonlocal calls
            calls += 1
            import json
            if "<repair_input>" in query:
                payload = json.loads(query.split("<repair_input>\n")[1].split("\n</repair_input>")[0])
                unit = payload["units"][0]
                return _result({"repairs": [{
                    "claim_id": unit["claim_id"], "heading": "样本解读",
                    "body": "两类经历样本并存，意味着反馈必然包含两种固定视角。",
                }]}, model="mock-writer")
            payload = json.loads(query.split("<audit_input>\n")[1].split("\n</audit_input>")[0])
            answer = _audit_answer(query)
            sample = next(unit for unit in payload["units"] if unit["kind"] == "sample")
            body_target = next(target for target in sample["targets"] if target["field"].startswith("body"))
            answer["issues"] = [{
                "claim_id": sample["claim_id"], "target_ids": [body_target["target_id"]],
                "problem_type": "statistic_scope", "problem": "样本结构不能证明回答内容",
                "severity": "warning",
            }]
            return _result(answer)

        with patch.object(audit, "_direct_json_call", new=call):
            result = await audit.audit_report_sections(
                core_blocks=core, chapters=chapters, findings_by_id=findings,
                source_registry=registry, profiles=[],
                sample_overview={
                    "heading": "样本解读",
                    "body": "有经历与无经历样本并存，意味着反馈必然包含两种固定视角。",
                    "sample_stats": [{"title": "经历", "table_markdown": "确定性表"}],
                },
            )
        self.assertEqual(calls, 3)
        self.assertEqual(result["diagnostics"]["status"], "needs_review")
        self.assertEqual(result["diagnostics"]["sample_fallback_reason"], "audit_needs_review")
        self.assertIsNone(result["sample_overview"])
        self.assertEqual(result["unresolved"][0]["section"], "样本概况")

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
