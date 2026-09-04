import asyncio
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

    def test_writer_catalog_can_be_scoped_to_one_part(self):
        clustered = {
            1: {
                "col_name": "界面反馈",
                "part_index": 1,
                "total": 2,
                "themes": [{
                    "id": "t01", "name": "按钮数量", "count": 1, "percentage": 50.0,
                }],
            },
            2: {
                "col_name": "功能反馈",
                "part_index": 2,
                "total": 2,
                "themes": [{
                    "id": "t01", "name": "入口理解", "count": 1, "percentage": 50.0,
                }],
            },
        }
        report_viewpoints = [
            {
                "id": "RVIEW:t01",
                "name": "界面共同观点",
                "count": 1,
                "denominator": 2,
                "percentage": 50.0,
                "source_questions": ["界面反馈"],
                "source_scope_keys": ["1"],
            },
            {
                "id": "RVIEW:t02",
                "name": "功能共同观点",
                "count": 1,
                "denominator": 2,
                "percentage": 50.0,
                "source_questions": ["功能反馈"],
                "source_scope_keys": ["2"],
            },
        ]

        rendered = render_viewpoint_stats(
            clustered,
            report_viewpoints,
            part_index=1,
        )

        self.assertIn("[QVIEW:1:t01]", rendered)
        self.assertNotIn("[QVIEW:2:t01]", rendered)
        self.assertIn("[RVIEW:t01]", rendered)
        self.assertNotIn("[RVIEW:t02]", rendered)

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
            synthesis_diagnostics={
                "status": "failed",
                "input_candidate_count": 170,
                "final_input_candidate_count": 170,
                "reduction_levels": 0,
                "partial_failure_count": 0,
                "calls": [{
                    "stage": "final",
                    "batch_index": 1,
                    "input_candidate_count": 170,
                    "output_theme_count": 0,
                    "status": "failed",
                    "model": "claude-sonnet-5",
                    "repaired": True,
                    "raw_len": 16000,
                    "error": "finish_reason=length",
                    "error_type": "other",
                    "finish_reason": "length",
                    "duration_seconds": 12.5,
                    "unsafe_extra": "secret-synthesis",
                }],
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
        self.assertEqual(diagnostics["synthesis"]["status"], "failed")
        self.assertEqual(
            diagnostics["synthesis"]["calls"][0]["error"],
            "finish_reason=length",
        )
        self.assertEqual(
            diagnostics["synthesis"]["calls"][0]["finish_reason"],
            "length",
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
    def test_cross_question_contract_allows_omissions_and_requires_two_questions(self):
        candidates = [
            {
                "source_question_id": "q1",
                "representative_quotes": ["入口难找"],
            },
            {
                "source_question_id": "q2",
                "representative_quotes": ["按钮入口不明显"],
            },
            {
                "source_question_id": "q3",
                "representative_quotes": ["仅本题出现"],
            },
        ]
        data = {"themes": [{
            "id": "t01",
            "name": "入口不易识别",
            "description": "不同题目都提到入口识别困难",
            "source_candidate_ids": ["c0001", "c0002"],
            "representative_quotes": [],
        }]}

        self.assertIsNone(
            report_engine._validate_cross_question_themes(data, candidates)
        )
        self.assertEqual(
            data["themes"][0]["representative_quotes"],
            ["入口难找", "按钮入口不明显"],
        )

        same_question = {"themes": [{
            "id": "t01",
            "name": "同题候选",
            "description": "不应成为跨题观点",
            "source_candidate_ids": ["c0001", "c0002"],
        }]}
        candidates[1]["source_question_id"] = "q1"
        self.assertIn(
            "至少两个不同题目",
            report_engine._validate_cross_question_themes(
                same_question,
                candidates,
            ),
        )

    async def test_cross_question_count_deduplicates_players_and_uses_relevant_sources(self):
        clustered = {
            1: {
                "col_name": "界面反馈",
                "all_themes": [{
                    "id": "t01", "name": "按钮数量", "count": 2,
                    "source_quotes": ["按钮太多"],
                    "respondent_keys": ["p1", "p2"],
                }],
            },
            2: {
                "col_name": "使用反馈",
                "all_themes": [{
                    "id": "t01", "name": "入口理解", "count": 2,
                    "source_quotes": ["入口难找"],
                    "respondent_keys": ["p1", "p3"],
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
        merged_themes = [{
            "id": "t01",
            "name": "界面入口复杂",
            "description": "界面控件和入口增加理解成本",
            "source_candidate_ids": ["c0001", "c0002"],
        }]

        attempt_callback = object()
        merge_call = AsyncMock(return_value={"data": {"themes": merged_themes}})
        classify_call = AsyncMock()
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
        self.assertEqual(by_id["RVIEW:t01"]["count"], 3)
        self.assertEqual(by_id["RVIEW:t01"]["denominator"], 3)
        self.assertEqual(by_id["RVIEW:t01"]["source_scope_keys"], ["1", "2"])
        merge_system_prompt = merge_call.await_args.args[0]
        merge_query = merge_call.await_args.args[1]
        self.assertIn("无需分配全部候选", merge_system_prompt)
        self.assertIn('"candidate_id": "c0001"', merge_query)
        self.assertNotIn("representative_quotes", merge_query)
        self.assertNotIn("respondent_keys", merge_query)
        self.assertIs(
            merge_call.await_args.kwargs["on_attempt_event"],
            attempt_callback,
        )
        classify_call.assert_not_awaited()

    async def test_large_candidate_catalog_is_grouped_in_one_compact_call(self):
        clustered = {}
        open_text = {}
        columns = []
        for question_index in range(10):
            column_index = question_index + 1
            columns.append({
                "index": column_index,
                "name": f"问题{column_index}",
                "role": "open_text",
            })
            clustered[column_index] = {
                "col_name": f"问题{column_index}",
                "part_index": 1,
                "all_themes": [
                    {
                        "id": f"t{theme_index + 1:02d}",
                        "name": f"问题{column_index}观点{theme_index + 1}",
                        "description": "具体玩家观点",
                        "count": 1,
                        "source_quotes": [f"引用-{column_index}-{theme_index + 1}"],
                        "respondent_keys": [f"p{column_index}"],
                    }
                    for theme_index in range(17)
                ],
            }
            open_text[column_index] = [{
                "respondent_key": f"p{column_index}",
                "text": f"回答-{column_index}",
            }]
        plan = {
            "columns": columns,
            "parts": [{
                "name": "综合体验",
                "column_indexes": [column["index"] for column in columns],
            }],
        }
        observed_batch_sizes = []

        async def merge_call(_system, query, **_kwargs):
            payload = query.split("<cross_question_candidates_json>\n", 1)[1].split(
                "\n</cross_question_candidates_json>", 1
            )[0]
            candidates = json.loads(payload)
            observed_batch_sizes.append(len(candidates))
            candidate_ids = [item["candidate_id"] for item in candidates]
            return {
                "data": {"themes": [{
                    "id": "t01",
                    "name": "跨题共同观点",
                    "description": "跨题共同含义",
                    "source_candidate_ids": candidate_ids,
                    "representative_quotes": [],
                }]},
                "model": "model-a",
                "raw_len": 500,
                "repaired": False,
                "error": "",
                "duration_seconds": 0.1,
            }

        classify_call = AsyncMock()
        with (
            patch.object(report_engine, "_direct_json_call", new=merge_call),
            patch.object(report_engine, "_classify_batch_direct", new=classify_call),
        ):
            events = [
                item async for item in build_report_viewpoint_stats(
                    clustered,
                    open_text,
                    plan,
                    ["玩家ID", *(column["name"] for column in columns)],
                )
            ]

        diagnostics = next(item[1] for item in events if item[0] == "diagnostics")
        result = next(item[1] for item in events if item[0] == "result")
        self.assertEqual(diagnostics["input_candidate_count"], 170)
        self.assertEqual(diagnostics["reduction_levels"], 0)
        self.assertEqual(diagnostics["final_input_candidate_count"], 170)
        self.assertEqual(diagnostics["selected_candidate_count"], 170)
        self.assertEqual(diagnostics["excluded_candidate_count"], 0)
        self.assertEqual(diagnostics["status"], "completed")
        self.assertEqual(len(diagnostics["calls"]), 1)
        self.assertEqual(observed_batch_sizes, [170])
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["source_scope_keys"]), {
            str(index) for index in range(1, 11)
        })
        classify_call.assert_not_awaited()

    async def test_cross_question_stage_has_one_total_timeout_budget(self):
        clustered = {}
        open_text = {}
        columns = []
        for column_index in (1, 2):
            columns.append({
                "index": column_index,
                "name": f"问题{column_index}",
                "role": "open_text",
            })
            clustered[column_index] = {
                "col_name": f"问题{column_index}",
                "part_index": 1,
                "all_themes": [
                    {
                        "id": f"t{theme_index + 1:02d}",
                        "name": f"问题{column_index}观点{theme_index + 1}",
                        "description": "具体玩家观点",
                        "count": 1,
                        "source_quotes": [f"引用-{column_index}-{theme_index + 1}"],
                        "respondent_keys": [f"p{column_index}"],
                    }
                    for theme_index in range(25)
                ],
            }
            open_text[column_index] = [{
                "respondent_key": f"p{column_index}",
                "text": f"回答-{column_index}",
            }]
        plan = {
            "columns": columns,
            "parts": [{"name": "综合体验", "column_indexes": [1, 2]}],
        }
        call_count = 0

        async def slow_merge(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(1)
            return {"data": {"themes": []}}

        classify_call = AsyncMock()
        with (
            patch.object(report_engine, "_direct_json_call", new=slow_merge),
            patch.object(report_engine, "_classify_batch_direct", new=classify_call),
            patch.object(report_engine, "LLM_QUALITATIVE_CALL_TIMEOUT_SECONDS", 0.01),
        ):
            events = [
                item async for item in build_report_viewpoint_stats(
                    clustered,
                    open_text,
                    plan,
                    ["玩家ID", "问题1", "问题2"],
                )
            ]

        diagnostics = next(item[1] for item in events if item[0] == "diagnostics")
        result = next(item[1] for item in events if item[0] == "result")
        self.assertEqual(diagnostics["status"], "failed")
        self.assertEqual(diagnostics["calls"], [
            {
                "stage": "selective_grouping",
                "batch_index": 1,
                "input_candidate_count": 50,
                "output_theme_count": 0,
                "status": "failed",
                "model": "",
                "repaired": False,
                "raw_len": 0,
                "error": "cross_question_synthesis_stage_timeout",
                "error_type": "timeout",
                "finish_reason": "",
                "duration_seconds": diagnostics["calls"][0]["duration_seconds"],
            }
        ])
        self.assertEqual(call_count, 1)
        self.assertEqual(result, [])
        classify_call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
