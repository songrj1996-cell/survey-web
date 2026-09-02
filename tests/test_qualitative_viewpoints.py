import json
import unittest
from unittest.mock import AsyncMock, patch

import survey_stats

from app.services import report_engine
from app.services.qualitative_viewpoints import (
    build_viewpoint_diagnostics,
    build_report_viewpoint_stats,
    finalize_viewpoint_diagnostics,
    render_viewpoint_stats,
)


class QualitativeViewpointTests(unittest.TestCase):
    def test_open_answers_keep_same_respondent_key_across_questions(self):
        rows = [
            ["玩家ID", "界面反馈", "功能反馈"],
            ["p1", "按钮太多", "入口难找"],
            ["", "布局清楚", ""],
        ]
        plan = {
            "columns": [
                {"index": 0, "name": "玩家ID", "role": "id"},
                {"index": 1, "name": "界面反馈", "role": "open_text"},
                {"index": 2, "name": "功能反馈", "role": "open_text"},
            ],
            "parts": [
                {"name": "体验反馈", "column_indexes": [1, 2]},
            ],
        }

        open_text = survey_stats.collect_open_text(rows, plan)

        self.assertEqual(open_text[1][0]["respondent_key"], "玩家ID=p1")
        self.assertEqual(open_text[2][0]["respondent_key"], "玩家ID=p1")
        self.assertEqual(open_text[1][1]["respondent_key"], "row:2")

    def test_writer_catalog_separates_question_and_cross_question_viewpoints(self):
        clustered = {
            1: {
                "col_name": "界面反馈",
                "total": 3,
                "all_themes": [
                    {"id": "t01", "name": "按钮数量", "count": 2, "percentage": 66.7},
                ],
            }
        }
        report_viewpoints = [{
            "id": "RVIEW:t01",
            "name": "熟悉度与理解难度",
            "count": 2,
            "denominator": 4,
            "percentage": 50.0,
            "source_questions": ["使用频率", "界面反馈"],
        }]

        rendered = render_viewpoint_stats(clustered, report_viewpoints)

        self.assertIn("[QVIEW:1:t01]", rendered)
        self.assertIn("2名玩家提及，占本题3名有效回答玩家的66.7%", rendered)
        self.assertIn("[RVIEW:t01]", rendered)
        self.assertIn("占相关题目4名有效回答玩家的50.0%", rendered)
        self.assertIn("目录外的综合判断必须标为“分析推断”", rendered)

    def test_viewpoint_diagnostics_persist_only_sanitized_catalog_fields(self):
        clustered = {
            1: {
                "col_name": "界面反馈",
                "total": 3,
                "all_themes": [{
                    "id": "t01",
                    "name": "按钮数量",
                    "description": "secret-description",
                    "count": 2,
                    "percentage": 66.7,
                    "source_quotes": ["secret-player-quote"],
                    "respondent_keys": ["secret-player-id"],
                }],
            }
        }
        report_viewpoints = [{
            "id": "RVIEW:t01",
            "name": "入口理解",
            "description": "secret-report-description",
            "count": 2,
            "denominator": 3,
            "percentage": 66.7,
            "source_questions": ["界面反馈"],
            "quotes": ["secret-report-quote"],
        }]
        rendered = render_viewpoint_stats(clustered, report_viewpoints)

        diagnostics = build_viewpoint_diagnostics(
            clustered,
            report_viewpoints,
            rendered,
            cluster_diagnostics={
                "1": {
                    "status": "failed",
                    "quality_status": "degraded",
                    "phase_a": [{
                        "error": "TimeoutError: secret-player-error-detail",
                    }],
                },
            },
            cluster_metrics={
                "scope_concurrency": 2,
                "elapsed_seconds": 1.5,
                "unsafe_extra": "secret-metric",
            },
        )
        serialized = json.dumps(diagnostics, ensure_ascii=False)

        self.assertEqual(diagnostics["catalog"]["entry_count"], 2)
        self.assertEqual(diagnostics["catalog"]["question_viewpoint_count"], 1)
        self.assertEqual(diagnostics["catalog"]["report_viewpoint_count"], 1)
        self.assertEqual(len(diagnostics["catalog"]["rendered_sha256"]), 64)
        self.assertEqual(
            diagnostics["cluster"]["metrics"],
            {"scope_concurrency": 2, "elapsed_seconds": 1.5},
        )
        self.assertEqual(
            diagnostics["cluster"]["error_type_counts"], {"timeout": 1}
        )
        self.assertEqual(
            diagnostics["cluster"]["error_stage_counts"], {"phase_a": 1}
        )
        self.assertNotIn("secret-", serialized)
        self.assertNotIn("respondent_keys", serialized)
        self.assertNotIn("quotes", serialized)
        self.assertNotIn("description", serialized)

    def test_viewpoint_diagnostics_distinguish_failure_boundaries(self):
        catalog = build_viewpoint_diagnostics(
            {
                1: {
                    "col_name": "界面反馈",
                    "total": 1,
                    "themes": [{
                        "id": "t01", "name": "入口难找",
                        "count": 1, "percentage": 100.0,
                    }],
                }
            },
            [],
            "<subjective_viewpoint_stats>catalog</subjective_viewpoint_stats>",
        )
        report_without_mentions = "**观点：入口难找**\n\n- **主要发现**：需要优化。"

        context_missing = finalize_viewpoint_diagnostics(
            catalog,
            report_without_mentions,
            writer_context_included=False,
        )
        writer_omission = finalize_viewpoint_diagnostics(
            catalog,
            report_without_mentions,
            writer_context_included=True,
        )
        catalog_unavailable = finalize_viewpoint_diagnostics(
            build_viewpoint_diagnostics({}, [], ""),
            report_without_mentions,
            writer_context_included=False,
        )
        complete = finalize_viewpoint_diagnostics(
            catalog,
            report_without_mentions + "\n\n**提及情况：** 1名玩家提及。",
            writer_context_included=True,
        )
        complete_with_list_item = finalize_viewpoint_diagnostics(
            catalog,
            report_without_mentions + "\n\n- **提及情况：** 1名玩家提及。",
            writer_context_included=True,
        )
        writer_no_viewpoints = finalize_viewpoint_diagnostics(
            catalog,
            "## Part 1 界面反馈\n\n没有输出观点块。",
            writer_context_included=True,
        )

        self.assertEqual(
            context_missing["writer_output"]["status"], "context_missing"
        )
        self.assertEqual(
            writer_omission["writer_output"]["status"], "writer_omission"
        )
        self.assertEqual(
            catalog_unavailable["writer_output"]["status"],
            "catalog_unavailable",
        )
        self.assertEqual(complete["writer_output"]["status"], "complete")
        self.assertEqual(
            complete_with_list_item["writer_output"]["status"], "complete"
        )
        self.assertEqual(
            writer_no_viewpoints["writer_output"]["status"],
            "writer_no_viewpoints",
        )
        self.assertEqual(
            writer_omission["writer_output"]["missing_mention_count"], 1
        )


class CrossQuestionViewpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_cross_question_count_deduplicates_players_and_uses_relevant_sources(self):
        clustered = {
            1: {
                "col_name": "界面反馈",
                "all_themes": [{
                    "id": "t01", "name": "按钮数量", "count": 2,
                    "source_quotes": ["按钮太多"],
                }],
            },
            2: {
                "col_name": "使用反馈",
                "all_themes": [{
                    "id": "t01", "name": "入口理解", "count": 2,
                    "source_quotes": ["入口难找"],
                }],
            },
        }
        open_text = {
            1: [
                {"respondent_key": "p1", "text": "按钮太多"},
                {"respondent_key": "p2", "text": "按钮合适"},
            ],
            2: [
                {"respondent_key": "p1", "text": "入口难找"},
                {"respondent_key": "p3", "text": "熟悉后好找"},
            ],
        }
        plan = {
            "columns": [
                {"index": 1, "name": "界面反馈", "role": "open_text"},
                {"index": 2, "name": "使用反馈", "role": "open_text"},
            ],
            "parts": [{"name": "界面体验", "column_indexes": [1, 2]}],
        }
        merged_themes = [
            {"id": "t01", "name": "按钮数量", "description": "按钮多少"},
            {"id": "t02", "name": "熟悉度与理解", "description": "熟悉度和理解难度"},
        ]
        classified = {
            "classifications": [
                {"response_id": "0", "assignments": [
                    {"theme_id": "t01", "sentiment": "negative"},
                    {"theme_id": "t02", "sentiment": "neutral"},
                ]},
                {"response_id": "1", "assignments": [
                    {"theme_id": "t01", "sentiment": "positive"},
                ]},
                {"response_id": "2", "assignments": [
                    {"theme_id": "t02", "sentiment": "negative"},
                ]},
                {"response_id": "3", "assignments": [
                    {"theme_id": "t02", "sentiment": "positive"},
                ]},
            ]
        }

        attempt_callback = object()
        merge_call = AsyncMock(return_value={"data": {"themes": merged_themes}})
        classify_call = AsyncMock(return_value=classified)
        with (
            patch.object(
                report_engine,
                "_get_theme_merge_system_prompt_base",
                return_value="merge-base",
            ),
            patch.object(
                report_engine,
                "_direct_json_call",
                new=merge_call,
            ),
            patch.object(
                report_engine,
                "_classify_batch_direct",
                new=classify_call,
            ),
        ):
            events = [
                item async for item in build_report_viewpoint_stats(
                    clustered,
                    open_text,
                    plan,
                    ["ID", "界面反馈", "使用反馈"],
                    on_attempt_event=attempt_callback,
                )
            ]

        result = next(item[1] for item in events if item[0] == "result")
        by_id = {item["id"]: item for item in result}
        self.assertEqual(by_id["RVIEW:t01"]["count"], 2)
        self.assertEqual(by_id["RVIEW:t01"]["denominator"], 2)
        self.assertEqual(by_id["RVIEW:t02"]["count"], 2)
        self.assertEqual(by_id["RVIEW:t02"]["denominator"], 3)
        merge_system_prompt = merge_call.await_args.args[0]
        merge_query = merge_call.await_args.args[1]
        self.assertIn("最终主题不设置最少或最多数量", merge_system_prompt)
        self.assertIn('"candidate_id": "c0001"', merge_query)
        self.assertIs(
            merge_call.await_args.kwargs["on_attempt_event"],
            attempt_callback,
        )
        self.assertIs(
            classify_call.await_args.kwargs["on_attempt_event"],
            attempt_callback,
        )


if __name__ == "__main__":
    unittest.main()
