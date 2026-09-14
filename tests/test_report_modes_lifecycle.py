"""Selected-version input ownership and real quick orchestration, with mocked models only."""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services import report_modes as modes, report_versions as versions, survey_service as service
from app.services import export_download
from app.services import report_history
from app.services import report_engine, report_partial_rerun, report_quick_pipeline
from tests.test_survey_report_versions import _event_payloads


def source():
    return {"mode": "standard", "analysis_mode": "qualitative", "report_mode": "quick",
            "owner_key": "email:owner@example.com", "filename": "synthetic.csv",
            "rows": [["ID", "Q2 建议", "Q3 补充"], ["p1", "奖励很好", "有崩溃需核实"], ["p2", "奖励很好", ""]],
            "confirmed_columns": [{"role": "id", "column_indexes": [0]},
                                  {"role": "open_text", "name": "Q2 建议", "column_indexes": [1]},
                                  {"role": "open_text", "name": "Q3 补充", "column_indexes": [2]}],
            "selected_question_keys": ["1", "2"], "qualitative_context": {"background": "活动"}}


def snapshot(sess, title="一版"):
    return {"report_md": "# " + title, "report_style": "quick", "report_mode": "quick",
            "input_snapshot": modes.freeze_report_inputs(sess), "report_status": "complete"}


def profile_source():
    return {"mode": "standard", "report_mode": "quick", "owner_key": "email:owner@example.com",
            "rows": [["record", "uid", "rank", "games", "feedback", "other", "second uid"],
                     [0, "123 45", "Gold", 0, "Repeated feedback", "Freeform other", "private-second-id"],
                     [0, "123 45", "Silver", 1, "Repeated feedback", "A", "private-second-id"]],
            "confirmed_columns": [
                {"role": "id", "name_zh": "记录编号", "column_indexes": [0]},
                {"role": "mlbbid", "name_zh": "玩家编号", "column_indexes": [1]},
                {"role": "profile_dim", "name_zh": "段位", "column_indexes": [2], "value_aliases": {"黄金": ["Gold"]}},
                {"role": "profile_dim", "name_zh": "局数", "column_indexes": [3]},
                {"role": "open_text", "name_zh": "体验", "column_indexes": [4]},
                {"role": "single_choice", "name_zh": "偏好", "column_indexes": [5], "options": ["A", "其他"], "other_text": {"enabled": True}},
                {"role": "mlbbid", "name_zh": "另一编号", "column_indexes": [6]},
            ], "selected_question_keys": ["4", "5"]}


def legacy_profile_snapshot(sess):
    frozen = snapshot(sess)
    for question in frozen["input_snapshot"]["source_questions"]:
        question["sources"] = [{key: item[key] for key in ("response_id", "text", "source_id")}
                               for item in question["sources"]]
    return frozen


class FrozenModeTests(unittest.TestCase):
    def test_all_confirmed_profile_and_id_fields_are_retained_when_not_selected(self):
        sess = profile_source()
        before = deepcopy(sess)
        questions = modes.collect_source_questions(sess)
        self.assertEqual([question["question_key"] for question in questions], ["4", "5"])
        first, second = questions[0]["sources"]
        self.assertEqual(first["profile"], {"段位": "Gold", "局数": "0"})
        self.assertEqual(second["profile"], {"段位": "Silver", "局数": "1"})
        self.assertEqual(first["ids"], {"记录编号": "0", "玩家编号": "123 45", "另一编号": "private-second-id"})
        self.assertEqual(first["source_id"], second["source_id"])
        self.assertNotEqual(first["response_id"], second["response_id"])
        self.assertEqual(questions[1]["sources"][0]["profile"], first["profile"])
        self.assertEqual(sess, before)
        self.assertEqual([c["role"] for c in modes.analysis_columns(sess)][2:4], ["ignore", "ignore"])

    def test_duplicate_confirmed_metadata_names_do_not_drop_columns(self):
        sess = profile_source()
        sess["confirmed_columns"][6]["name_zh"] = "玩家编号"
        ids = modes.collect_source_questions(sess)[0]["sources"][0]["ids"]
        self.assertEqual(ids["玩家编号（列2）"], "123 45")
        self.assertEqual(ids["玩家编号（列7）"], "private-second-id")

    def test_legacy_metadata_uses_frozen_columns_and_exact_row_identity(self):
        sess = profile_source()
        old = legacy_profile_snapshot(sess)
        before = deepcopy(old)
        sess["confirmed_columns"][2].update(role="ignore", name_zh="当前错误名称")
        enriched = modes.enrich_source_metadata(old, sess)
        rows = modes.report_source_page(enriched, question_key="4")["items"]
        self.assertEqual(rows[0]["profile"], {"段位": "Gold", "局数": "0"})
        self.assertEqual(rows[1]["profile"], {"段位": "Silver", "局数": "1"})
        self.assertEqual(old, before)
        self.assertNotIn("当前错误名称", str(rows))

    def test_legacy_metadata_requires_fingerprint_and_every_source_identity_field(self):
        sess = profile_source()
        original = legacy_profile_snapshot(sess)
        for field in ("response_id", "text", "source_id"):
            old = deepcopy(original)
            old["input_snapshot"]["source_questions"][0]["sources"][0][field] += "-different"
            enriched = modes.enrich_source_metadata(old, sess)
            rows = modes.report_source_page(enriched, question_key="4")["items"]
            self.assertEqual(rows[0]["profile"], {}, field)
            self.assertEqual(rows[1]["profile"]["段位"], "Silver", field)
        old = deepcopy(original)
        old["input_snapshot"]["source_questions"][0]["question_key"] = "not-this-question"
        self.assertEqual(modes.report_source_page(modes.enrich_source_metadata(old, sess))["items"][0]["profile"], {})
        for remove in ("source_fingerprint", "confirmed_columns"):
            old = deepcopy(original)
            old["input_snapshot"].pop(remove)
            self.assertEqual(modes.report_source_page(modes.enrich_source_metadata(old, sess))["items"][0]["profile"], {})
        for owner_source in ({}, {**sess, "rows": []}, {**sess, "rows": [sess["rows"][0], *reversed(sess["rows"][1:])]}):
            self.assertEqual(modes.report_source_page(modes.enrich_source_metadata(original, owner_source))["items"][0]["profile"], {})

    def test_stored_metadata_is_authoritative_and_not_sent_to_quick_qa(self):
        sess = profile_source()
        stored = snapshot(sess)
        sources = stored["input_snapshot"]["source_questions"][0]["sources"]
        sources[0]["profile"]["数值"] = 0
        sess["rows"][1][2] = "Changed rank"
        page = modes.report_source_page(modes.enrich_source_metadata(stored, sess))
        self.assertEqual(page["items"][0]["profile"]["数值"], 0)
        context = modes.quick_qa_context("# Report", stored["input_snapshot"])
        for private in ("private-second-id", "Gold", "123 45", '"profile"', '"ids"'):
            self.assertNotIn(private, context)
        self.assertIn("Repeated feedback", context)

    def test_title_and_legacy_labels_are_read_only(self):
        self.assertEqual(modes.quick_report_title({"filename": "[UE] 设计图评估 (Responses) .xlsx"}), "[UE] 设计图评估 · 反馈总结")
        self.assertEqual(modes.quick_report_title({}, {"title": "我的报告"}), "我的报告")
        self.assertEqual(modes.quick_report_title({}), "问卷反馈总结报告")
        old = {"report_mode": "quick", "report_md": "# 快速总结\n- **反复出现**：正面\n- **部分提及 · 风险待核实**：问题\n原文反复出现，风险待核实"}
        before = deepcopy(old)
        rendered = modes.prepare_report_markdown(old)
        self.assertIn("**反复提及：**\n\n1. 正面", rendered)
        self.assertIn("**部分提及：**\n\n1. **【风险】**问题", rendered)
        self.assertIn("原文反复出现，风险待核实", rendered)
        self.assertEqual(old, before)

    def test_quick_objective_statistics_keeps_scale_selection_and_no_cross_tabs(self):
        sess = {"rows": [["ID", "评分", "段位", "不选"], ["a", 1, "Gold", 99],
                         ["b", 3, "黄金", 99], ["c", "bad", "Gold", 99], ["d", "", "", 99]],
                "confirmed_columns": [{"role": "id", "column_indexes": [0]},
                    {"role": "scale", "name": "评分", "column_indexes": [1], "scale_min": 1, "scale_max": 3},
                    {"role": "profile_dim", "name": "段位", "column_indexes": [2], "value_aliases": {"黄金": ["Gold"]}},
                    {"role": "scale", "name": "不选", "column_indexes": [3]}],
                "selected_question_keys": ["1", "2"]}
        before = deepcopy(sess)
        result = modes.quick_objective_statistics(sess)
        self.assertEqual(sess, before)
        self.assertEqual([s["question_key"] for s in result["sections"]], ["1", "2"])
        self.assertIn("均值: **2.00**", result["markdown"])
        self.assertIn("有效数字回答: 2 条", result["markdown"])
        self.assertIn("非数字回答（已剔除均值计算）: 1 条", result["markdown"])
        self.assertIn("| 1 | 1 | 50.0% |", result["markdown"])
        self.assertIn("| 黄金 | 3 | 100.0% |", result["markdown"])
        self.assertNotIn("不选", result["markdown"])
        self.assertNotIn("交叉", result["markdown"])
        self.assertNotIn("ID", result["markdown"])
        self.assertEqual(len(result["blocks"]), 2)

    def test_quick_matrix_and_other_keep_confirmed_definitions_and_branch_note(self):
        sess = {"rows": [["A", "B", "选择"], [1, 3, "Other"], [3, 5, "A"]],
                "confirmed_columns": [{"role": "matrix_scale", "name": "满意度", "column_indexes": [0, 1],
                                       "rows": ["界面", "玩法"], "scale_min": 1, "scale_max": 5},
                                      {"role": "single_choice", "name": "偏好", "column_indexes": [2],
                                       "options": ["A", "Other"]}],
                "branch_rules": [{"parent_name": "参加活动", "allowed_options": ["是"], "targets": [{"indexes": [0, 1]}]}]}
        result = modes.quick_objective_statistics(sess)
        self.assertEqual([s["question_key"] for s in result["sections"]], ["0:1", "2"])
        self.assertIn("界面", result["markdown"])
        self.assertIn("玩法", result["markdown"])
        self.assertIn("参加活动", result["markdown"])
        self.assertIn("50.0%", result["markdown"])

    def test_quick_readiness_uses_its_own_model_configuration(self):
        with patch.object(service, "get_session", return_value=source()), \
             patch.object(service, "is_quick_report_enabled", return_value=True), \
             patch.object(service, "LLM_REPORT_MODEL", ""), \
             patch.object(service, "LLM_QUICK_REPORT_MODEL", "quick-only-model"):
            self.assertFalse(service.validate_report_ready("synthetic"))

    def test_full_version_never_inherits_quick_outputs(self):
        sess = source()
        first = {**snapshot(sess), "quick_summary": {"questions": []}, "quick_checkpoint": {"fingerprint": "old"}, "report_status": "partial"}
        versions.append_report_version(sess, first, kind="initial")
        new = versions.append_report_version(sess, {"report_md": "# 洞察", "report_style": "full", "report_mode": "insight"}, kind="regenerate", base_version=1)
        for field in ("quick_summary", "quick_checkpoint", "input_snapshot"):
            self.assertNotIn(field, new)
        self.assertEqual(new["report_status"], "complete")

    def test_source_markdown_cannot_inject_active_markup(self):
        sess = source()
        sess["rows"][1][1] = '![tracking](https://invalid.example/image)\n# fake heading\n<script>bad</script>'
        exported = modes.prepare_report_markdown(snapshot(sess), "evidence")
        self.assertNotIn('![tracking](', exported)
        self.assertNotIn('\n# fake heading', exported)
        self.assertNotIn('<script>', exported)

    def test_branch_boundary_is_present_in_each_question_input(self):
        sess = source()
        sess["branch_rules"] = [{"parent_name": "是否参与", "allowed_options": ["是"], "targets": [{"indexes": [1]}]}]
        self.assertIn("是否参与", modes.collect_source_questions(sess)[0]["question"])

    def test_older_insight_retains_its_partial_source_after_current_plan_changes(self):
        from tests.test_report_partial_rerun import _entry
        entry = _entry()
        original = entry["report_versions"][0]
        original["input_snapshot"] = {"plan": deepcopy(entry["plan"]), "partial_rerun_source": deepcopy(entry["partial_rerun_source"])}
        entry["plan"]["parts"][0]["name"] = "新计划"
        entry["partial_rerun_source"] = {}
        capability = report_partial_rerun.partial_rerun_capability(entry, original)
        self.assertTrue(capability["available"])
        self.assertEqual(capability["parts"][0]["part_title"], "Part 1 概念评价")

    def test_selection_masks_analysis_without_losing_editor_definitions(self):
        sess = source()
        sess["selected_question_keys"] = ["2"]
        before = deepcopy(sess)
        actual = modes.analysis_columns(sess)
        self.assertEqual([c["role"] for c in actual], ["id", "ignore", "open_text"])
        self.assertEqual(sess, before)
        self.assertEqual([q["question_key"] for q in modes.collect_source_questions(sess)], ["2"])

    def test_default_selection_and_invalid_keys(self):
        columns = source()["confirmed_columns"]
        self.assertEqual(modes.selected_question_keys(columns), ["1", "2"])
        with self.assertRaises(ValueError):
            modes.selected_question_keys(columns, ["0"])

    def test_version_source_export_never_uses_current_editor_state(self):
        sess = source()
        versions.append_report_version(sess, snapshot(sess), kind="initial")
        sess["rows"][1][1] = "新版本原文"
        versions.append_report_version(sess, snapshot(sess, "二版"), kind="regenerate", base_version=1)
        with patch.object(export_download, "get_session", return_value=sess):
            original = export_download._get_session_report_source("id", 1)
        self.assertIn("奖励很好", modes.prepare_report_markdown(original, "evidence"))
        self.assertNotIn("新版本原文", modes.prepare_report_markdown(original, "evidence"))
        self.assertNotIn("奖励很好", modes.prepare_report_markdown(original))
        page = modes.report_source_page(original, question_key="1", offset=1, limit=1, q="奖励")
        self.assertEqual(page["total"], 2)
        self.assertEqual(page["items"][0]["response_id"], "1/r2")

    def test_legacy_version_does_not_borrow_new_versions_evidence(self):
        sess = source()
        versions.append_report_version(sess, {"report_md": "# legacy"}, kind="initial")
        versions.append_report_version(sess, snapshot(sess), kind="regenerate", base_version=1)
        with patch.object(export_download, "get_session", return_value=sess):
            original = export_download._get_session_report_source("id", 1)
        self.assertEqual(modes.report_source_page(original)["total"], 0)
        self.assertEqual(modes.resolve_report_mode(original), "insight")

    def test_inheritance_checks_source_then_restores_selected_configuration(self):
        sess = source()
        frozen = snapshot(sess)
        sess["selected_question_keys"] = ["2"]
        sess["report_mode"] = "statistics"
        restored = modes.inherit_report_inputs(sess, frozen)
        self.assertEqual(restored["report_mode"], "quick")
        self.assertEqual(restored["selected_question_keys"], ["1", "2"])
        sess["rows"][1][1] = "different"
        with self.assertRaises(ValueError):
            modes.inherit_report_inputs(sess, frozen)

    def test_history_commit_checks_selected_frozen_version(self):
        sess = source()
        versions.append_report_version(sess, snapshot(sess), kind="initial")
        sess["id"] = "h"
        history = [sess]
        result = snapshot(sess, "retry")
        with patch.object(report_history, "mutate_history", side_effect=lambda operation: operation(history)), \
             patch.object(report_history, "_find_history_for_login", return_value=sess):
            _, committed = report_history.append_quick_rerun_to_history("h", result, base_version=1,
                expected_input=deepcopy(result["input_snapshot"]), instruction="重试", login=None)
            self.assertEqual(committed["version"], 2)
            with self.assertRaises(Exception):
                report_history.append_quick_rerun_to_history("h", result, base_version=1,
                    expected_input={"changed": True}, instruction="重试", login=None)


class QuickLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_summary_receives_row_profiles_but_ids_stay_in_evidence(self):
        questions = modes.collect_source_questions(profile_source())
        calls = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            for source in payload["sources"]:
                self.assertEqual(set(source), {"response_id", "text", "offset", "end_offset", "source_length", "profile"})
            return self.answer(payload["sources"][0]["response_id"])
        result = await report_quick_pipeline.run_quick_pipeline(questions, collect=collect)
        self.assertEqual(result["report_status"], "complete")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["questions"][0]["sources"][0]["profile"], {"段位": "Gold", "局数": "0"})
        self.assertNotIn("private-second-id", json.dumps(calls))
        self.assertNotIn("123 45", json.dumps(calls))
        self.assertEqual([s["profile"] for s in calls[0]["sources"]], [{"段位": "Gold", "局数": "0"}, {"段位": "Silver", "局数": "1"}])
        self.assertEqual(calls[1]["sources"][0]["profile"], calls[0]["sources"][0]["profile"])
        self.assertEqual(result["questions"][0]["findings"][0]["evidence"][0]["ids"]["玩家编号"], "123 45")

    def runtime(self, sess, collector):
        stack = ExitStack()
        stack.enter_context(patch.object(service, "get_session", side_effect=lambda _: deepcopy(sess)))
        def save(_sid, value):
            sess.clear()
            sess.update(deepcopy(value))
        stack.enter_context(patch.object(service, "save_session", side_effect=save))
        stack.enter_context(patch.object(service, "save_to_history"))
        stack.enter_context(patch.object(service, "_current_login", new=AsyncMock(return_value={"email": "owner@example.com"})))
        stack.enter_context(patch.object(service, "is_quick_report_enabled", return_value=True))
        stack.enter_context(patch.object(service, "collect_chat_completion", new=collector))
        stack.enter_context(patch.object(service, "_get_prompt_text", return_value="test contract"))
        for forbidden in ("_batch_qualitative_analysis", "build_report_viewpoint_stats", "_direct_writer_round", "compute_survey_stats"):
            stack.enter_context(patch.object(service, forbidden, side_effect=AssertionError("quick entered " + forbidden)))
        return stack

    @staticmethod
    def answer(ref="1/r1"):
        return json.dumps({"schema_version": 2, "stage": "question", "findings": [
            {"text": "奖励受到认可", "frequency": "反复出现", "risk": False, "evidence_ids": [ref], "risk_ids": []}],
            "empty_reason": ""}, ensure_ascii=False), "mock-model"

    async def test_duplicate_upload_keeps_fresh_scale_definitions_in_new_report(self):
        fresh = source()
        fresh["rows"] = [["设计1", "设计2"], [5, 1], [1, 5], [4, 3]]
        fresh["confirmed_columns"] = [{"role": "scale", "name": name, "column_indexes": [i], "scale_min": 1, "scale_max": 5} for i, name in enumerate(("设计1", "设计2"))]
        fresh["selected_question_keys"] = ["0", "1"]
        old = deepcopy(fresh)
        old["confirmed_columns"][0]["role"] = "profile_dim"
        versions.append_report_version(old, {**snapshot(old), "title": "快速总结"}, kind="initial")
        before = deepcopy(old)
        saved = []
        def commit(_target, result, **kwargs):
            saved.append(deepcopy(result))
            self.assertEqual(kwargs["expected_input"], versions.resolve_report_version(old, 1)["input_snapshot"])
            return old, {**result, "version": 2}
        collector = AsyncMock(side_effect=AssertionError("objective only must not call model"))
        with self.runtime(fresh, collector), patch.object(service, "find_exact_survey_duplicate_entry", return_value=old), patch.object(service, "_load_history", return_value=[old]), patch.object(service, "_find_history_for_login", return_value=old), patch.object(service, "append_quick_rerun_to_history", side_effect=commit):
            service.prepare_duplicate_report_rerun("fresh-scale", {"email": "owner@example.com"}, history_id="old-scale", base_version=1)
            events = _event_payloads([event async for event in service.report_stream("fresh-scale", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(len(saved), 1)
        self.assertEqual(old, before)
        result = saved[0]
        self.assertEqual([c["role"] for c in result["input_snapshot"]["confirmed_columns"]], ["scale", "scale"])
        self.assertEqual(result["title"], "synthetic · 反馈总结")
        self.assertTrue(result["report_md"].startswith("# synthetic · 反馈总结"))
        sections = result["quick_summary"]["objective_stats"]["sections"]
        for section in sections:
            for label in ("均值", "中位数", "标准差"):
                self.assertIn(label, section["markdown"])
            self.assertLess(section["markdown"].index("| 1 |"), section["markdown"].index("| 5 |"))
        collector.assert_not_awaited()

    async def test_legacy_regeneration_backfills_only_new_version_from_frozen_columns(self):
        sess = profile_source()
        versions.append_report_version(sess, legacy_profile_snapshot(sess), kind="initial")
        before = versions.resolve_report_version(sess, 1)
        sess["confirmed_columns"][2].update(role="ignore", name_zh="当前编辑名称")
        calls = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=collect)):
            events = _event_payloads([event async for event in service.report_stream("legacy-profile", None, generation_kind="regenerate", base_version=1)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(versions.resolve_report_version(sess, 1), before)
        current = versions.resolve_report_version(sess, 2)
        self.assertEqual(current["input_snapshot"]["source_questions"][0]["sources"][0]["profile"], {"段位": "Gold", "局数": "0"})
        self.assertEqual(calls[0]["sources"][1]["profile"]["段位"], "Silver")
        self.assertNotIn("当前编辑名称", str(calls))
        self.assertNotIn("private-second-id", str(calls))

    async def test_retry_keeps_frozen_profiles_despite_editor_changes(self):
        sess = profile_source()
        async def first(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            if payload["question_key"] == "5":
                raise RuntimeError("synthetic failure")
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=first)):
            events = _event_payloads([event async for event in service.report_stream("retry-profile", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        before = versions.resolve_report_version(sess, 1)
        self.assertEqual(before["report_status"], "partial")
        sess["confirmed_columns"][2].update(role="ignore", name_zh="当前编辑名称")
        sess["input_snapshot"]["source_questions"][0]["sources"][0]["profile"] = {"段位": "Changed"}
        calls = []
        async def retry(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=retry)), patch.object(service, "enrich_source_metadata", side_effect=AssertionError("retry must not enrich frozen inputs")):
            events = _event_payloads([event async for event in service.report_stream("retry-profile", None, generation_kind="regenerate", base_version=1, retry_failed=True)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual([call["question_key"] for call in calls], ["5"])
        self.assertEqual(calls[0]["sources"][0]["profile"], {"段位": "Gold", "局数": "0"})
        self.assertEqual(versions.resolve_report_version(sess, 1)["input_snapshot"], before["input_snapshot"])
        self.assertEqual(len(versions.normalize_report_versions(sess)), 1)
        self.assertEqual(sess["quick_report_diagnostics"]["reused_questions"], 1)

    async def test_old_algorithm_retry_requires_full_regeneration_and_preserves_history(self):
        sess = profile_source()
        async def first(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            if payload["question_key"] == "5":
                raise RuntimeError("synthetic failure")
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=first)), patch.object(report_quick_pipeline, "ALGORITHM_VERSION", "question-summary-v2"):
            _ = [event async for event in service.report_stream("old-algorithm-profile", None)]
        before = versions.resolve_report_version(sess, 1)
        self.assertEqual(before["report_status"], "partial")
        calls = []
        async def collect(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=collect)):
            events = _event_payloads([event async for event in service.report_stream("old-algorithm-profile", None, generation_kind="regenerate", base_version=1, retry_failed=True)])
            self.assertTrue(any(e["type"] == "error" and "完整重新生成" in e.get("message", "") for e in events), events)
            self.assertEqual(calls, [])
            self.assertEqual(len(versions.normalize_report_versions(sess)), 1)
            events = _event_payloads([event async for event in service.report_stream("old-algorithm-profile", None, generation_kind="regenerate", base_version=1)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual({call["question_key"] for call in calls}, {"4", "5"})
        self.assertEqual(calls[0]["sources"][0]["profile"]["段位"], "Gold")
        self.assertEqual(versions.resolve_report_version(sess, 1), before)
        self.assertEqual(sess["report_status"], "complete")

    async def test_profile_checkpoint_upgrade_reuses_fourteen_questions_and_completes_same_version(self):
        sess = profile_source()
        sess["confirmed_columns"] = sess["confirmed_columns"][:4] + [
            {"role": "open_text", "name_zh": f"合成题 {i}", "column_indexes": [i]} for i in range(4, 20)]
        sess["rows"] = [row[:4] + [f"合成反馈 {i}" for i in range(4, 20)] for row in sess["rows"]]
        sess["selected_question_keys"] = [str(i) for i in range(4, 20)]
        async def first(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            if payload["question_key"] in ("18", "19"):
                raise RuntimeError("synthetic failure")
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=first)), patch.object(report_quick_pipeline, "ALGORITHM_VERSION", "question-summary-v3-profile"):
            events = _event_payloads([event async for event in service.report_stream("upgrade-profile", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        before = versions.resolve_report_version(sess, 1)
        self.assertEqual(len(before["quick_checkpoint"]["questions"]), 14)
        calls = []
        async def retry(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(sess, AsyncMock(side_effect=retry)), patch.object(service, "enrich_source_metadata", side_effect=AssertionError("retry must keep frozen input")):
            events = _event_payloads([event async for event in service.report_stream("upgrade-profile", None, generation_kind="regenerate", base_version=1, retry_failed=True)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual({p["question_key"] for p in calls}, {"18", "19"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sess["report_status"], "complete")
        self.assertEqual(sess["quick_report_diagnostics"]["reused_questions"], 14)
        self.assertEqual(versions.resolve_report_version(sess, 1)["input_snapshot"], before["input_snapshot"])
        self.assertEqual(len(versions.normalize_report_versions(sess)), 1)
        for old, new in zip(before["quick_summary"]["questions"][:14], sess["quick_summary"]["questions"][:14]):
            self.assertEqual(new, old)
        for payload in calls:
            self.assertEqual([s["profile"]["段位"] for s in payload["sources"]], ["Gold", "Silver"])
            original = next(q for q in before["input_snapshot"]["source_questions"] if q["question_key"] == payload["question_key"])
            self.assertEqual([(s["text"], s["profile"]) for s in payload["sources"]],
                             [(s["text"], s["profile"]) for s in original["sources"]])

    async def test_duplicate_upload_uses_new_confirmed_profiles_without_rewriting_base(self):
        fresh = profile_source()
        old = deepcopy(fresh)
        old["confirmed_columns"][2]["role"] = "ignore"
        versions.append_report_version(old, snapshot(old), kind="initial")
        before = deepcopy(old)
        saved, calls = [], []
        def commit(_target, result, **kwargs):
            self.assertEqual(kwargs["expected_input"], versions.resolve_report_version(old, 1)["input_snapshot"])
            saved.append(deepcopy(result))
            return old, {**result, "version": 2}
        async def collect(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            calls.append(payload)
            return self.answer(payload["sources"][0]["response_id"])
        with self.runtime(fresh, AsyncMock(side_effect=collect)), patch.object(service, "find_exact_survey_duplicate_entry", return_value=old), patch.object(service, "append_quick_rerun_to_history", side_effect=commit):
            service.prepare_duplicate_report_rerun("new-profile", {"email": "owner@example.com"}, history_id="old-profile", base_version=1)
            events = _event_payloads([event async for event in service.report_stream("new-profile", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(old, before)
        self.assertEqual(len(saved), 1)
        self.assertEqual(calls[0]["sources"][0]["profile"]["段位"], "Gold")
        self.assertEqual(saved[0]["input_snapshot"]["source_questions"][0]["sources"][1]["profile"]["段位"], "Silver")

    async def test_history_regeneration_backfill_requires_same_owner_and_matching_source(self):
        for condition in ("match", "missing", "different_owner", "different_rows"):
            with self.subTest(condition=condition):
                original = profile_source()
                history = {"owner_key": original["owner_key"], "report_mode": "quick"}
                versions.append_report_version(history, legacy_profile_snapshot(original), kind="initial")
                before = deepcopy(history)
                sess = {**deepcopy(history), "quick_history_rerun": True, "rerun_target_history_id": "legacy-history", "rerun_base_version": 1}
                owner = deepcopy(original)
                if condition == "different_owner":
                    owner["owner_key"] = "email:someone-else@example.com"
                elif condition == "different_rows":
                    owner["rows"][1][2] = "Changed rank"
                saved, calls = [], []
                def commit(_target, result, **kwargs):
                    saved.append(deepcopy(result))
                    self.assertEqual(kwargs["expected_input"], versions.resolve_report_version(history, 1)["input_snapshot"])
                    return history, {**result, "version": 2}
                async def collect(messages, **kwargs):
                    payload = json.loads(messages[1]["content"])
                    calls.append(payload)
                    return self.answer(payload["sources"][0]["response_id"])
                def get(sid):
                    return deepcopy((None if condition == "missing" else owner) if sid == "legacy-history" else sess)
                with self.runtime(sess, AsyncMock(side_effect=collect)), patch.object(service, "get_session", side_effect=get), patch.object(service, "_load_history", return_value=[history]), patch.object(service, "_find_history_for_login", return_value=history), patch.object(service, "append_quick_rerun_to_history", side_effect=commit):
                    events = _event_payloads([event async for event in service.report_stream("history-profile", None)])
                self.assertFalse([e for e in events if e["type"] == "error"], events)
                self.assertEqual(history, before)
                self.assertEqual(len(saved), 1)
                expected = {"段位": "Gold", "局数": "0"} if condition == "match" else {}
                self.assertEqual(calls[0]["sources"][0]["profile"], expected)

    async def test_pipeline_fallback_is_recorded_in_saved_model_usage(self):
        sess = source()
        sess["selected_question_keys"] = ["1"]
        calls = []
        async def collect(messages, **kwargs):
            model = kwargs["models"][0]
            calls.append(model)
            event = {"call_id": str(len(calls)), "model": model, "fallback": False}
            await kwargs["on_attempt_event"]({**event, "status": "started"})
            if model == "primary":
                await kwargs["on_attempt_event"]({**event, "status": "failed", "error_category": "rate_limited"})
                raise RuntimeError("synthetic rate limit")
            await kwargs["on_attempt_event"]({**event, "status": "completed", "response_model": model,
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}, "usage_complete": True})
            return self.answer()[0], model
        with self.runtime(sess, AsyncMock(side_effect=collect)), \
             patch.object(service, "LLM_QUICK_REPORT_MODEL", "primary"), \
             patch.object(service, "LLM_QUICK_REPORT_FALLBACK_MODELS", ("backup",)), \
             patch.object(report_quick_pipeline, "LLM_QUICK_REPORT_MODEL", "primary"), \
             patch.object(report_quick_pipeline, "LLM_QUICK_REPORT_FALLBACK_MODELS", ("backup",)):
            events = _event_payloads([event async for event in service.report_stream("fallback-usage", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(calls, ["primary", "backup"])
        saved = versions.resolve_report_version(sess, 1)
        self.assertEqual(saved["report_status"], "complete")
        usage = saved["report_llm_usage"]["totals"]
        self.assertEqual(usage["call_count"], 2)
        self.assertEqual(usage["active_calls"], 0)
        self.assertEqual(usage["fallback_models_used"], ["backup"])
        self.assertIn("backup", usage["models_used"])

    async def test_partial_failure_retry_reuses_success_and_completes_same_version(self):
        sess = source()
        async def first(messages, **kwargs):
            if "2/r1" in str(messages):
                raise RuntimeError("synthetic upstream failure")
            return self.answer()
        collector = AsyncMock(side_effect=first)
        with self.runtime(sess, collector):
            events = _event_payloads([event async for event in service.report_stream("lifecycle", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(sess["report_status"], "partial")
        before = versions.resolve_report_version(sess, 1)
        self.assertEqual(collector.await_count, 4)  # One success, three bounded attempts for the failed step.
        collector = AsyncMock(return_value=self.answer("2/r1"))
        with self.runtime(sess, collector):
            events = _event_payloads([event async for event in service.report_stream("lifecycle", None,
                generation_kind="regenerate", base_version=1, retry_failed=True)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(collector.await_count, 1)
        self.assertEqual(sess["report_status"], "complete")
        self.assertEqual(sess["quick_summary"]["questions"][0], before["quick_summary"]["questions"][0])
        self.assertEqual(len(versions.normalize_report_versions(sess)), 1)
        self.assertEqual(sess["quick_report_diagnostics"]["reused_questions"], 1)

    async def test_cancel_stops_actual_model_work(self):
        sess = source()
        started, stopped = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        with self.runtime(sess, AsyncMock(side_effect=blocked)):
            async def consume():
                return _event_payloads([event async for event in service.report_stream("cancel-test", None)])
            task = asyncio.create_task(consume())
            await asyncio.wait_for(started.wait(), 3)
            self.assertTrue(service.cancel_report_run("cancel-test")["cancelled"])
            events = await asyncio.wait_for(task, 3)
        self.assertTrue(stopped.is_set())
        self.assertTrue(any(e["type"] == "cancelled" for e in events))
        self.assertFalse(any(e["type"] == "report_done" for e in events))
        self.assertNotIn("cancel-test", service._REPORT_CANCEL_EVENTS)

    async def test_cancel_after_success_creates_partial_and_retry_only_finishes_remaining(self):
        sess = source()
        waiting = asyncio.Event()
        async def collector(messages, **kwargs):
            if "2/r1" in str(messages):
                waiting.set()
                await asyncio.Event().wait()
            return self.answer()
        with self.runtime(sess, AsyncMock(side_effect=collector)):
            async def consume():
                return _event_payloads([event async for event in service.report_stream("cancel-partial", None)])
            task = asyncio.create_task(consume())
            await asyncio.wait_for(waiting.wait(), 3)
            for _ in range(100):
                if sess.get("quick_run_checkpoint", {}).get("questions"):
                    break
                await asyncio.sleep(.01)
            service.cancel_report_run("cancel-partial")
            events = await asyncio.wait_for(task, 3)
        self.assertTrue(any(e["type"] == "report_done" and e["report_status"] == "partial" for e in events), events)
        mock = AsyncMock(return_value=self.answer("2/r1"))
        with self.runtime(sess, mock):
            events = _event_payloads([event async for event in service.report_stream("cancel-partial", None,
                generation_kind="regenerate", base_version=1, retry_failed=True)])
        self.assertEqual(mock.await_count, 1)
        self.assertTrue(any(e["type"] == "report_done" and e["report_status"] == "complete" for e in events), events)

    async def test_cancel_with_only_batch_progress_saves_recoverable_version(self):
        from tests.test_report_quick_pipeline import answer
        sess = source()
        sess["rows"] = [["ID", "Q2 体验", "Q3 补充"], *[[str(i), "完整体验" * 100, ""] for i in range(18)]]
        sess["selected_question_keys"] = ["1"]
        merging = asyncio.Event()
        async def collector(messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            if "candidates" in payload:
                merging.set()
                await asyncio.Event().wait()
            return answer(payload), "fake"
        with patch.object(report_quick_pipeline, "LLM_QUICK_REPORT_INPUT_CHARS", 5000):
            with self.runtime(sess, collector):
                async def consume():
                    return _event_payloads([event async for event in service.report_stream("batch-cancel", None)])
                task = asyncio.create_task(consume())
                await asyncio.wait_for(merging.wait(), 3)
                self.assertFalse(sess["quick_run_checkpoint"]["questions"])
                self.assertTrue(sess["quick_run_checkpoint"]["steps"])
                service.cancel_report_run("batch-cancel")
                events = await asyncio.wait_for(task, 3)
            self.assertTrue(any(e["type"] == "report_done" and e["report_status"] == "partial" for e in events))
            saved = deepcopy(versions.resolve_report_version(sess, 1))
            calls = []
            async def recovery(messages, **kwargs):
                payload = json.loads(messages[1]["content"])
                calls.append(payload)
                return answer(payload), "fake"
            with self.runtime(sess, recovery):
                events = _event_payloads([event async for event in service.report_stream("batch-cancel", None,
                    generation_kind="regenerate", base_version=1, retry_failed=True)])
        self.assertTrue(calls)
        self.assertTrue(all("candidates" in p for p in calls))
        self.assertEqual(versions.resolve_report_version(sess, 1)["input_snapshot"], saved["input_snapshot"])
        self.assertEqual(len(versions.normalize_report_versions(sess)), 1)
        self.assertTrue(any(e["type"] == "report_done" and e["report_status"] == "complete" for e in events))

    async def test_qa_uses_frozen_context_even_when_live_rows_change(self):
        sess = source()
        frozen = modes.freeze_report_inputs(sess)
        stored = modes.quick_qa_context("# 旧版报告", frozen)
        sess.update(input_snapshot=frozen, qa_context_md=stored)
        sess["rows"][1][1] = "THIS IS A DIFFERENT VERSION"
        with patch.object(service, "collect_chat_completion", new=AsyncMock(return_value=("回答", "mock"))) as model, \
             patch.object(service, "_get_report_qa_system_prompt", return_value="test"), \
             patch.object(service, "prepare_glossary_messages", side_effect=lambda x:x), \
             patch.object(service, "normalize_glossary_terms", side_effect=lambda x:x):
            _, _, used = await service._answer_qa_direct(sess, "为什么")
        self.assertEqual(used, stored)
        self.assertNotIn("THIS IS A DIFFERENT VERSION", str(model.call_args))
        self.assertIn("全部 3 条主观回复", report_engine._describe_qa_context_scope(stored))

    async def test_concise_insight_keeps_analysis_but_skips_duplicate_writing_rounds(self):
        from tests.test_survey_report_versions import _base_session, _isolated_report_runtime
        sess = _base_session()
        sess["report_mode"] = "insight"
        writer = AsyncMock(side_effect=[("# 洞察", "mock"), ("<!--CORE_START-->\n## 总体判断\n需要验证。\n<!--CORE_END-->", "mock")])
        with _isolated_report_runtime(sess, writer), patch.object(service, "_get_prompt_text", return_value="简洁判断"):
            events = _event_payloads([event async for event in service.report_stream("concise-insight", None)])
        self.assertFalse([e for e in events if e["type"] == "error"], events)
        self.assertEqual(writer.await_count, 2)
        self.assertNotIn("## 行动建议", sess["report_md"])


if __name__ == "__main__":
    unittest.main()
