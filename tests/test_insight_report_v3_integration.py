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


def _fake_v3_generator(calls):
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
            "status": "skipped",
            "message": "阶段三尚未启用",
            "impact": "仍需人工复核",
        })
        yield ("artifacts", {
            "viewpoint_diagnostics": {
                "schema_version": "insight-v3-diagnostics-v2",
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
                "audit": {"status": "not_enabled", "unresolved_count": 0},
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


@contextmanager
def _isolated_v3_runtime(sess, *, enabled):
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
            new=_fake_v3_generator(calls),
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
        self.assertEqual(snapshot["comparison_validation"]["status"], "passed")
        runtime["inject"].assert_not_called()
        appendix_blocks = runtime["render_appendix"].call_args.args[0]
        self.assertEqual([block["title"] for block in appendix_blocks], ["体验选择"])
        runtime["legacy_writer"].assert_not_awaited()
        self.assertEqual(len(runtime["calls"]), 1)

    async def test_saved_v3_regenerates_with_v3_when_feature_flag_is_off(self):
        sess = _session()
        frozen = report_modes.freeze_report_inputs(sess)
        report_versions.append_report_version(
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
        self.assertFalse(any(item.get("type") == "error" for item in payloads))
        self.assertEqual(len(runtime["calls"]), 1)
        runtime["legacy_writer"].assert_not_awaited()
        regenerated = report_versions.resolve_report_version(sess, 2)
        self.assertEqual(regenerated["report_engine"], "insight_v3")
        self.assertEqual(regenerated["base_version"], 1)


if __name__ == "__main__":
    unittest.main()
