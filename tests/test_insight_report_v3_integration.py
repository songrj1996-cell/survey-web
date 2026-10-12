from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.services import report_modes, report_versions, survey_service


def _session():
    return {
        "filename": "v3-integration.xlsx",
        "owner_key": "email:owner@example.com",
        "rows": [["体验反馈"], ["失败时会有压力"]],
        "plan": {
            "columns": [{"index": 0, "name": "体验反馈", "role": "open_text"}],
            "parts": [{"name": "体验", "column_indexes": [0]}],
            "cross_tabs": [],
            "open_questions": [0],
        },
        "branch_rules": [],
        "stats_md": "<metadata>总样本: 1, 有效样本: 1</metadata>",
        "stats_blocks": [
            {
                "title": "性别",
                "part": "",
                "table_markdown": "| 选项 | 人数 |\n|---|---:|\n| 女性 | 1 |",
            },
            {
                "title": "性别",
                "part": "Part 1 体验",
                "table_markdown": "| 选项 | 人数 |\n|---|---:|\n| 女性 | 1 |",
            },
            {
                "title": "体验选择",
                "part": "Part 1 体验",
                "table_markdown": "| 选项 | 人数 |\n|---|---:|\n| A | 1 |",
            },
        ],
        "stats_source": "python",
        "open_text": {
            0: [{
                "respondent_key": "row:1",
                "text": "失败时会有压力",
                "ids": {},
                "profile": {},
                "segments": {},
            }],
        },
        "qualitative_context": {"problem": "理解玩家体验阻力"},
        "report_mode": "insight",
        "pending_report_style": "full",
        "analysis_mode": "qualitative",
        "file_sha256": "a" * 64,
    }


def _payloads(events):
    return [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event.startswith("data: ")
    ]


def _fake_v3_generator(calls, artifact_overrides=None):
    async def generate(**kwargs):
        calls.append(kwargs)
        yield ("analysis_progress", {
            "phase": "themes",
            "phase_index": 1,
            "phase_total": 4,
            "status": "completed",
            "message": "逐题提炼完成",
            "impact": "none",
            "item_total": 1,
            "item_index": 1,
        })
        yield ("artifacts", {
            "insight_v3_findings": {
                "schema_version": "insight-findings-v1",
                "source_binding": {"test": True},
                "source_registry": {},
                "questions": [],
                "diagnostics": {"stop_reason": "all_questions_finished"},
            },
            **(artifact_overrides or {}),
        })
        yield ("analysis_progress", {
            "phase": "writing",
            "phase_index": 2,
            "phase_total": 4,
            "status": "completed",
            "message": "章节写作完成",
            "impact": "none",
        })
        yield ("analysis_progress", {
            "phase": "audit",
            "phase_index": 3,
            "phase_total": 4,
            "status": "completed",
            "message": "语义审校完成",
            "impact": "none",
            "audit_status": "passed",
            "unresolved_count": 0,
        })
        yield ("artifacts", {
            "viewpoint_diagnostics": {
                "schema_version": "insight-v3-diagnostics-v3",
                "engine": "insight_v3",
                "questions": [{"scope_key": "0", "status": "complete"}],
                "chapters": [{"chapter_id": "c01", "status": "complete"}],
                "analysis_focus": {
                    "status": "missing",
                    "message": "未提供分析主线，章节按问卷结构组织，未规划人群专章",
                },
                "actions": {"status": "complete", "action_count": 1, "items": []},
                "evidence_index": {"entry_count": 1, "reference_count": 1},
                "sample_overview": {"table_count": 1, "table_titles": ["性别"]},
                "audit": {
                    "status": "passed",
                    "unresolved_count": 0,
                    "planned_unit_count": 3,
                    "audited_unit_count": 3,
                    "initial_coverage_rate": 1.0,
                    "capacity_issue_count": 0,
                    "unit_status_counts": {
                        "passed": 3,
                        "evidence_fallback": 0,
                        "repaired": 0,
                        "needs_review": 0,
                        "incomplete": 0,
                    },
                    "initial_batch_count": 2,
                    "repair_batch_count": 0,
                    "reaudit_batch_count": 0,
                    "concurrency": 4,
                    "peak_concurrency": 2,
                },
                "frequency_review": {
                    "status": "clear",
                    "candidate_count": 0,
                    "auto_blocked_count": 0,
                    "candidates": [],
                },
            },
            "writer_models_used": ["mock-v3-writer"],
        })
        yield (
            "result",
            "# 玩家体验洞察\n\n## 一、调研背景\n\n背景。\n\n"
            "## 二、样本概况\n\n概况。\n\n## 三、核心发现\n\n发现。\n\n"
            "## 四、行动建议\n\n建议。\n\n## 附录 A：完整证据索引\n\n证据。",
        )

    return generate


def _append_v3_base(sess):
    frozen = report_modes.freeze_report_inputs(sess)
    return report_versions.append_report_version(
        sess,
        {
            "report_style": "full",
            "report_mode": "insight",
            "report_engine": "insight_v3",
            "report_status": "complete",
            "input_snapshot": frozen,
            "report_md": "# 旧 V3 报告",
            "title": "旧 V3 报告",
            "qa_context_md": "<qa_context>旧报告</qa_context>",
            "qa_messages": [],
            "qa_provider": "",
            "qa_model": "",
            "analyst_conv_id": "",
            "analyst_app": "insight_v3",
            "report_writer_provider": "direct_llm",
            "report_writer_model": "old-v3-model",
            "viewpoint_diagnostics": {"engine": "insight_v3"},
            "comparison_validation": {"status": "passed"},
        },
        kind="initial",
    )


@contextmanager
def _isolated_v3_runtime(sess, *, enabled, artifact_overrides=None):
    calls = []
    inject = Mock(side_effect=AssertionError("V3 must not use Part-title stats injection"))
    legacy_writer = AsyncMock(side_effect=AssertionError("V3 must not use legacy writer"))
    with ExitStack() as stack:
        save_session = stack.enter_context(patch.object(survey_service, "save_session"))
        save_history = stack.enter_context(patch.object(survey_service, "save_to_history"))
        stack.enter_context(patch.object(survey_service, "get_session", return_value=sess))
        stack.enter_context(patch.object(
            survey_service,
            "_current_login",
            new=AsyncMock(return_value={"email": "owner@example.com"}),
        ))
        stack.enter_context(patch.object(survey_service, "_assign_session_owner"))
        stack.enter_context(patch.object(survey_service, "_ensure_branch_rules"))
        stack.enter_context(patch.object(survey_service, "INSIGHT_REPORT_V3_ENABLED", enabled))
        pipeline_call = stack.enter_context(patch.object(
            survey_service,
            "generate_insight_report_v3",
            new=_fake_v3_generator(calls, artifact_overrides),
        ))
        stack.enter_context(patch.object(
            survey_service,
            "_direct_writer_round",
            new=legacy_writer,
        ))
        stack.enter_context(patch.object(
            survey_service,
            "inject_qualitative_stats",
            new=inject,
        ))
        render_appendix = stack.enter_context(patch.object(
            survey_service,
            "render_stats_appendix",
            return_value="## 统计附录\n\n确定性附录表。",
        ))
        stack.enter_context(patch.object(
            survey_service,
            "normalize_glossary_terms",
            side_effect=lambda value: value,
        ))
        stack.enter_context(patch.object(
            survey_service,
            "_inject_disclaimer",
            side_effect=lambda report, mode="": report,
        ))
        stack.enter_context(patch.object(
            survey_service,
            "_inject_research_background",
            side_effect=lambda report, _context: report,
        ))
        stack.enter_context(patch.object(
            survey_service,
            "_build_qa_context",
            side_effect=lambda _source, report="": f"<qa_context>{report}</qa_context>",
        ))
        stack.enter_context(patch.object(
            survey_service.survey_stats,
            "build_comparison_fact_catalog",
            return_value=[],
        ))
        stack.enter_context(patch.object(
            survey_service.survey_stats,
            "analyze_comparison_claims",
            return_value={"issues": [], "checked_claim_count": 0},
        ))
        stack.enter_context(patch.object(
            survey_service.survey_stats,
            "find_numbers_not_in_stats",
            return_value=[],
        ))
        stack.enter_context(patch.object(survey_service, "audit_log", new=AsyncMock()))
        stack.enter_context(patch("traceback.print_exc"))
        yield {
            "calls": calls,
            "inject": inject,
            "legacy_writer": legacy_writer,
            "save_session": save_session,
            "save_history": save_history,
            "render_appendix": render_appendix,
            "pipeline_call": pipeline_call,
        }


class InsightV3SurveyIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_replay_request_has_a_non_disconnecting_receive_channel(self):
        from scripts.replay_insight_report_v3 import _request

        request = _request()
        self.assertFalse(await request.is_disconnected())
        self.assertFalse(await request.is_disconnected())

    async def test_new_insight_uses_v3_appendix_snapshot_and_four_phase_progress(self):
        sess = _session()
        with _isolated_v3_runtime(sess, enabled=True) as runtime:
            events = [
                event
                async for event in survey_service.report_stream(
                    "v3-initial",
                    object(),
                    generation_kind="initial",
                )
            ]

        payloads = _payloads(events)
        self.assertFalse(any(item.get("type") == "error" for item in payloads))
        done = next(item for item in payloads if item.get("type") == "report_done")
        self.assertIn("## 统计附录", done["report_md"])
        self.assertLess(done["report_md"].index("## 四、行动建议"), done["report_md"].index("## 附录 A"))
        self.assertLess(done["report_md"].index("## 附录 A"), done["report_md"].index("## 统计附录"))
        progress = [item for item in payloads if item.get("type") == "analysis_progress"]
        observed = {
            (item["phase"], item["phase_index"], item["phase_total"])
            for item in progress
        }
        self.assertTrue({
            ("themes", 1, 4),
            ("writing", 2, 4),
            ("audit", 3, 4),
            ("finalize", 4, 4),
        }.issubset(observed))

        snapshot = report_versions.resolve_report_version(sess, 1)
        self.assertEqual(snapshot["report_engine"], "insight_v3")
        self.assertEqual(snapshot["analyst_app"], "insight_v3")
        self.assertNotIn("analysis_artifacts", snapshot)
        self.assertEqual(snapshot["viewpoint_diagnostics"]["engine"], "insight_v3")
        audit_diagnostics = snapshot["viewpoint_diagnostics"]["audit"]
        self.assertEqual(audit_diagnostics["initial_coverage_rate"], 1.0)
        self.assertEqual(sum(audit_diagnostics["unit_status_counts"].values()), 3)
        self.assertEqual(audit_diagnostics["capacity_issue_count"], 0)
        self.assertEqual(snapshot["comparison_validation"]["status"], "passed")
        runtime["inject"].assert_not_called()
        appendix_blocks = runtime["render_appendix"].call_args.args[0]
        self.assertEqual([block["title"] for block in appendix_blocks], ["体验选择"])
        runtime["legacy_writer"].assert_not_awaited()
        self.assertEqual(len(runtime["calls"]), 1)
        self.assertEqual(runtime["calls"][0]["rows"], sess["rows"])

    async def test_complete_v3_stats_artifact_supplies_cross_tables_to_appendix(self):
        sess = _session()
        cross = {"title": "评价 / 按偏好交叉", "part": "Part 1", "table_key": "cross-table",
                 "table_markdown": "| 偏好 | 好玩 | 样本量 |\n|---|---|---|\n| 无偏好 | 2 | 3 |"}
        with _isolated_v3_runtime(sess, enabled=True, artifact_overrides={
            "insight_v3_stats_tables": [cross],
        }) as runtime:
            events = [event async for event in survey_service.report_stream("v3-cross-stats", object(), generation_kind="initial")]
        self.assertFalse(any(item.get("type") == "error" for item in _payloads(events)))
        self.assertEqual(runtime["render_appendix"].call_args.args[0], [cross])

    async def test_saved_v3_regeneration_is_rejected_when_feature_flag_is_off(self):
        sess = _session()
        _append_v3_base(sess)

        with _isolated_v3_runtime(sess, enabled=False) as runtime:
            events = [
                event
                async for event in survey_service.report_stream(
                    "v3-regenerate-disabled",
                    object(),
                    generation_kind="regenerate",
                    base_version=1,
                )
            ]

        payloads = _payloads(events)
        error = next(item for item in payloads if item.get("type") == "error")
        self.assertIn("V3 开关已关闭", error["message"])
        self.assertEqual(len(runtime["calls"]), 0)
        runtime["legacy_writer"].assert_not_awaited()
        self.assertEqual(len(report_versions.normalize_report_versions(sess)), 1)

    async def test_structural_regeneration_saves_revised_focus_before_pipeline(self):
        sess = _session()
        _append_v3_base(sess)
        revised_focus = {
            "core_question": "哪些阻力最值得优先处理",
            "report_organization": "先写关键阻力，再写机会点",
            "supporting_analyses": ["结合开放题场景"],
            "evidence_role": "原话支撑判断，统计描述样本",
            "expected_deliverables": ["关键阻力", "机会点"],
            "avoid_structures": ["不得按题目逐项罗列"],
        }

        async def fake_preflight(**kwargs):
            revised = deepcopy(kwargs["plan"])
            revised["analysis_focus"] = deepcopy(revised_focus)
            yield ("result", {
                "supported": True,
                "effective_plan": revised,
                "report_instruction": kwargs["instruction"],
                "planning_instruction": kwargs["instruction"],
                "error_message": "",
                "diagnostics": {
                    "schema_version": "insight-v3-regeneration-v1",
                    "prompt_version": "regeneration-prompt-v1-impact-gated",
                    "status": "plan_revised",
                    "classification": {
                        "categories": ["chapter_structure", "analysis_focus"],
                        "supported": True,
                        "capability": "supported",
                        "reason": "结构调整",
                        "normalized_request": kwargs["instruction"],
                    },
                    "logical_calls": 2,
                    "models": ["mock-classifier", "mock-reviser"],
                },
            })

        instruction = "先写关键阻力，再写机会点"
        with (
            _isolated_v3_runtime(sess, enabled=True) as runtime,
            patch.object(
                survey_service,
                "prepare_insight_regeneration",
                new=fake_preflight,
            ),
        ):
            events = [
                event
                async for event in survey_service.report_stream(
                    "v3-structural-regeneration",
                    object(),
                    instruction=instruction,
                    generation_kind="regenerate",
                    base_version=1,
                )
            ]

        payloads = _payloads(events)
        self.assertFalse(any(item.get("type") == "error" for item in payloads))
        self.assertEqual(len(runtime["calls"]), 1)
        pipeline_kwargs = runtime["calls"][0]
        self.assertEqual(pipeline_kwargs["plan"]["analysis_focus"], revised_focus)
        self.assertEqual(pipeline_kwargs["report_instruction"], instruction)
        self.assertEqual(pipeline_kwargs["planning_instruction"], instruction)
        self.assertIsNotNone(pipeline_kwargs["baseline_source_contract"])

        regenerated = report_versions.resolve_report_version(sess, 2)
        self.assertEqual(regenerated["base_version"], 1)
        self.assertEqual(regenerated["instruction"], instruction)
        frozen = regenerated["input_snapshot"]
        self.assertEqual(frozen["plan"]["analysis_focus"], revised_focus)
        self.assertEqual(frozen["v3_regeneration"]["instruction"], instruction)
        self.assertEqual(
            frozen["v3_regeneration"]["classification"]["categories"],
            ["chapter_structure", "analysis_focus"],
        )

    async def test_unsupported_data_scope_regeneration_creates_no_version_or_report_body(self):
        sess = _session()
        _append_v3_base(sess)
        before = deepcopy(sess)

        async def fake_preflight(**_kwargs):
            yield ("result", {
                "supported": False,
                "effective_plan": None,
                "report_instruction": _kwargs["instruction"],
                "planning_instruction": "",
                "error_message": "当前不支持修改数据范围；本次未生成新版本。",
                "diagnostics": {
                    "status": "rejected",
                    "logical_calls": 0,
                    "models": [],
                },
            })

        with (
            _isolated_v3_runtime(sess, enabled=True) as runtime,
            patch.object(
                survey_service,
                "prepare_insight_regeneration",
                new=fake_preflight,
            ),
        ):
            events = [
                event
                async for event in survey_service.report_stream(
                    "v3-unsupported-regeneration",
                    object(),
                    instruction="只分析最近一个月新收集的回答",
                    generation_kind="regenerate",
                    base_version=1,
                )
            ]

        payloads = _payloads(events)
        error = next(item for item in payloads if item.get("type") == "error")
        self.assertIn("本次未生成新版本", error["message"])
        self.assertEqual(len(runtime["calls"]), 0)
        self.assertEqual(len(report_versions.normalize_report_versions(sess)), 1)
        diagnostic_event = next(item for item in payloads if item.get("type") == "report_regeneration_diagnostics")
        self.assertEqual(diagnostic_event["diagnostics"]["status"], "rejected")
        self.assertLess(payloads.index(diagnostic_event), payloads.index(error))
        for key in ("plan", "report_versions", "active_report_version", "next_report_version"):
            self.assertEqual(sess.get(key), before.get(key))
        runtime["save_session"].assert_not_called()
        runtime["save_history"].assert_not_called()

    async def test_missing_native_language_rejects_real_preflight_without_new_version(self):
        sess = _session()
        sess["plan"]["columns"].append({
            "index": 1, "name": "MLBB客户端语言", "role": "single_choice",
            "use_as_profile": True, "profile_scope": "analysis", "options": ["英语", "印尼语"],
        })
        _append_v3_base(sess)
        with _isolated_v3_runtime(sess, enabled=True) as runtime:
            events = [event async for event in survey_service.report_stream(
                "v3-missing-native-language", object(),
                instruction="每个 part 都区分母语英语和母语印尼语玩家，分别写结论。",
                generation_kind="regenerate", base_version=1,
            )]
        error = next(item for item in _payloads(events) if item.get("type") == "error")
        self.assertIn("没有母语画像字段", error["message"])
        self.assertEqual(runtime["calls"], [])
        self.assertEqual(len(report_versions.normalize_report_versions(sess)), 1)


if __name__ == "__main__":
    unittest.main()
