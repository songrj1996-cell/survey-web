import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

import survey_stats

from app.services import qualitative_viewpoints, report_engine
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
    @staticmethod
    def _fixture(question_count: int, themes_per_question: int):
        clustered = {}
        open_text = {}
        columns = []
        for column_index in range(1, question_count + 1):
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
                    }
                    for theme_index in range(themes_per_question)
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
        return clustered, open_text, plan, columns

    async def test_cross_question_count_deduplicates_players_and_uses_relevant_sources(self):
        clustered = {
            1: {
                "col_name": "界面反馈",
                "all_themes": [{
                    "id": "t01", "name": "理解成本", "count": 2,
                    "source_quotes": ["按钮太多"],
                }],
            },
            2: {
                "col_name": "使用反馈",
                "all_themes": [{
                    "id": "t01", "name": "理解成本", "count": 2,
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
        selection = {
            "status": "completed",
            "viewpoints": [{
                "id": "v01",
                "name": "理解成本",
                "description": "多处入口存在理解成本",
                "source_candidate_ids": ["c0001", "c0002"],
            }],
            "excluded_candidate_ids": [],
        }
        classified = {
            "classifications": [
                {
                    "response_id": str(index),
                    "assignments": [{"theme_id": "v01", "sentiment": "neutral"}],
                }
                for index in range(4)
            ],
            "fallback_count": 0,
        }

        attempt_callback = object()
        selection_call = AsyncMock(return_value={
            "data": selection,
            "model": "model-a",
            "raw_len": 100,
            "repaired": False,
            "error": "",
            "duration_seconds": 0.1,
        })
        classify_call = AsyncMock(return_value=classified)
        with (
            patch.object(report_engine, "_direct_json_call", new=selection_call),
            patch.object(report_engine, "_classify_batch_direct", new=classify_call),
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
        self.assertEqual(result[0]["id"], "RVIEW:v01")
        self.assertEqual(result[0]["count"], 3)
        self.assertEqual(result[0]["denominator"], 3)
        self.assertEqual(result[0]["source_scope_keys"], ["1", "2"])
        system_prompt = selection_call.await_args.args[0]
        query = selection_call.await_args.args[1]
        self.assertIn("筛选跨题共同观点", system_prompt)
        self.assertNotIn("最终主题不设置最少或最多数量", system_prompt)
        self.assertIn("<viewpoint_candidates_json>", query)
        self.assertIn('"source_scope_key": "1"', query)
        self.assertIs(
            selection_call.await_args.kwargs["on_attempt_event"],
            attempt_callback,
        )
        self.assertIs(
            classify_call.await_args.kwargs["on_attempt_event"],
            attempt_callback,
        )

    def test_cross_question_validator_rejects_old_or_non_cross_contracts(self):
        candidates = [
            {"source_scope_key": "1"},
            {"source_scope_key": "1"},
            {"source_scope_key": "2"},
        ]
        old_error = qualitative_viewpoints._validate_cross_question_viewpoints(
            {"themes": []},
            candidates,
        )
        self.assertIn("旧协议字段 themes", old_error)

        non_cross_error = qualitative_viewpoints._validate_cross_question_viewpoints(
            {
                "status": "completed",
                "viewpoints": [{
                    "id": "v01",
                    "name": "单题观点",
                    "description": "只来自一道题",
                    "source_candidate_ids": ["c0001", "c0002"],
                }],
                "excluded_candidate_ids": ["c0003"],
            },
            candidates,
        )
        self.assertIn("没有跨越", non_cross_error)

        valid = qualitative_viewpoints._validate_cross_question_viewpoints(
            {
                "status": "completed",
                "viewpoints": [{
                    "id": "v01",
                    "name": "共同观点",
                    "description": "两道题共同支持",
                    "source_candidate_ids": ["c0001", "c0003"],
                }],
                "excluded_candidate_ids": ["c0002"],
            },
            candidates,
        )
        self.assertIsNone(valid)

    async def test_real_scale_catalog_uses_one_selection_pass_and_records_reduction(self):
        clustered, open_text, plan, columns = self._fixture(10, 17)
        observed_candidate_counts = []

        async def selection_call(_system, query, **_kwargs):
            payload = query.split("<viewpoint_candidates_json>\n", 1)[1].split(
                "\n</viewpoint_candidates_json>", 1
            )[0]
            candidates = json.loads(payload)
            observed_candidate_counts.append(len(candidates))
            selected_ids = ["c0001", "c0018"]
            excluded_ids = [
                item["candidate_id"]
                for item in candidates
                if item["candidate_id"] not in selected_ids
            ]
            return {
                "data": {
                    "status": "completed",
                    "viewpoints": [{
                        "id": "v01",
                        "name": "跨题共同观点",
                        "description": "至少两道题共同出现",
                        "source_candidate_ids": selected_ids,
                    }],
                    "excluded_candidate_ids": excluded_ids,
                },
                "model": "model-a",
                "raw_len": 500,
                "repaired": False,
                "error": "",
                "duration_seconds": 0.1,
            }

        async def classify_call(_question, themes, batch, **_kwargs):
            return {
                "classifications": [
                    {
                        "response_id": str(index),
                        "assignments": [{
                            "theme_id": themes[0]["id"],
                            "sentiment": "neutral",
                        }],
                    }
                    for index in range(len(batch))
                ],
                "fallback_count": 0,
            }

        with (
            patch.object(report_engine, "_direct_json_call", new=selection_call),
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
        self.assertEqual(observed_candidate_counts, [170])
        self.assertEqual(diagnostics["input_candidate_count"], 170)
        self.assertEqual(diagnostics["final_input_candidate_count"], 1)
        self.assertEqual(diagnostics["selected_candidate_count"], 2)
        self.assertEqual(diagnostics["excluded_candidate_count"], 168)
        self.assertEqual(diagnostics["reduction_levels"], 0)
        self.assertEqual(diagnostics["planned_logical_call_count"], 2)
        self.assertEqual(diagnostics["completed_logical_call_count"], 2)
        self.assertEqual(diagnostics["calls"][0]["retention_ratio"], round(1 / 170, 4))
        self.assertEqual(diagnostics["final_viewpoint_count"], 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["source_scope_keys"]), {
            str(index) for index in range(1, 11)
        })

    async def test_old_nonreducing_output_stops_after_one_selection_call(self):
        clustered, open_text, plan, columns = self._fixture(2, 25)
        call_count = 0

        async def invalid_selection(_system, _query, **kwargs):
            nonlocal call_count
            call_count += 1
            data = {"themes": []}
            return {
                "data": None,
                "model": "model-a",
                "raw_len": 100,
                "repaired": True,
                "error": kwargs["validator"](data),
                "duration_seconds": 0.1,
            }

        classify_call = AsyncMock()
        with (
            patch.object(report_engine, "_direct_json_call", new=invalid_selection),
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
        self.assertEqual(call_count, 1)
        self.assertEqual(diagnostics["status"], "failed")
        self.assertEqual(diagnostics["stop_reason"], "no_progress")
        self.assertEqual(diagnostics["completed_logical_call_count"], 1)
        self.assertEqual(result, [])
        classify_call.assert_not_awaited()

    async def test_stage_deadline_cancels_selection_without_repeating_candidates(self):
        clustered, open_text, plan, columns = self._fixture(2, 2)
        started = asyncio.Event()

        async def blocked_selection(*_args, **_kwargs):
            started.set()
            await asyncio.Event().wait()

        with (
            patch.object(
                qualitative_viewpoints,
                "LLM_CROSS_QUESTION_STAGE_TIMEOUT_SECONDS",
                0.01,
            ),
            patch.object(report_engine, "_direct_json_call", new=blocked_selection),
        ):
            events = [
                item async for item in build_report_viewpoint_stats(
                    clustered,
                    open_text,
                    plan,
                    ["玩家ID", *(column["name"] for column in columns)],
                )
            ]

        self.assertTrue(started.is_set())
        diagnostics = next(item[1] for item in events if item[0] == "diagnostics")
        self.assertEqual(diagnostics["stop_reason"], "stage_timeout")
        self.assertLess(diagnostics["elapsed_seconds"], 0.5)
        self.assertEqual(next(item[1] for item in events if item[0] == "result"), [])

if __name__ == "__main__":
    unittest.main()
