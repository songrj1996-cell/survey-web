import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.services import report_engine


class LargeSampleDirectLLMTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_json_call_uses_qualitative_timeout_and_degrades(self):
        async def slow_completion(*_args, **_kwargs):
            await asyncio.sleep(0.05)
            return '{"themes": []}', "slow-model"

        with (
            patch.object(report_engine, "LLM_QUALITATIVE_CALL_TIMEOUT_SECONDS", 0.01),
            patch.object(report_engine, "collect_chat_completion", new=slow_completion),
        ):
            result = await report_engine._direct_json_call(
                "system",
                "query",
                models=("slow-model",),
                max_tokens=1024,
                reasoning_effort=None,
                validator=lambda _data: None,
            )

        self.assertIsNone(result["data"])
        self.assertEqual(result["error"], "TimeoutError")
        self.assertGreater(result["duration_seconds"], 0)

    def test_theme_extract_uses_response_ids_and_hydrates_verbatim_quotes(self):
        batch = [
            {"text": "The interface is clean."},
            {"text": "Tombolnya terlalu kecil."},
        ]
        query, source = report_engine._build_theme_extract_query("界面反馈", batch)
        data = {
            "themes": [
                {
                    "name": "界面设计",
                    "description": "界面视觉和按钮体验",
                    "positive_summary": "界面整洁",
                    "negative_summary": "按钮较小",
                    "representative_response_ids": ["r0001", "r0002"],
                }
            ]
        }

        self.assertIn("[r0001] The interface is clean.", query)
        self.assertIsNone(report_engine._validate_theme_candidates(data, source))
        hydrated = report_engine._hydrate_theme_candidate_quotes(data, source)
        self.assertEqual(hydrated["themes"][0]["representative_quotes"], source)
        self.assertNotIn("representative_response_ids", hydrated["themes"][0])

    def test_theme_extract_validator_rejects_unknown_or_non_string_ids(self):
        base = {
            "name": "界面设计",
            "description": "界面体验",
            "positive_summary": None,
            "negative_summary": None,
        }
        unknown = {
            "themes": [{**base, "representative_response_ids": ["r9999"]}]
        }
        malformed = {
            "themes": [{**base, "representative_response_ids": [["r0001"]]}]
        }

        self.assertIn(
            "不存在的回答 ID",
            report_engine._validate_theme_candidates(unknown, ["原文"]),
        )
        self.assertIn(
            "不重复",
            report_engine._validate_theme_candidates(malformed, ["原文"]),
        )

    def test_theme_extract_validator_requires_verbatim_quotes(self):
        source = ["The interface is clean.", "Tombolnya terlalu kecil."]
        valid = {
            "themes": [
                {
                    "name": "界面设计",
                    "description": "界面视觉和按钮体验",
                    "positive_summary": "界面整洁",
                    "negative_summary": "按钮较小",
                    "representative_quotes": list(source),
                }
            ]
        }

        self.assertIsNone(
            report_engine._validate_theme_candidates(valid, source)
        )
        valid["themes"][0]["representative_quotes"][0] = "界面很整洁"
        self.assertIn(
            "并非逐字来自输入",
            report_engine._validate_theme_candidates(valid, source),
        )

    def test_merge_validator_enforces_count_ids_and_candidate_quotes(self):
        candidates = [
            {
                "name": f"主题{i}",
                "description": f"范围{i}",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": [f"quote-{i}"],
            }
            for i in range(1, 11)
        ]
        merged = {
            "themes": [
                {
                    "id": f"t{i:02d}",
                    "name": f"最终主题{i}",
                    "description": f"最终范围{i}",
                    "positive_summary": None,
                    "negative_summary": None,
                    "representative_quotes": [f"quote-{i}"],
                }
                for i in range(1, 11)
            ]
        }

        self.assertIsNone(
            report_engine._validate_merged_themes(merged, candidates)
        )
        merged["themes"] = merged["themes"][:9]
        self.assertIn(
            "10–25",
            report_engine._validate_merged_themes(merged, candidates),
        )

    def test_classification_normalizer_rejects_duplicates_and_invalid_values(self):
        data = {
            "classifications": [
                {
                    "response_id": "0",
                    "assignments": [
                        {"theme_id": "t01", "sentiment": "positive"}
                    ],
                },
                {
                    "response_id": "1",
                    "assignments": [
                        {"theme_id": "t01", "sentiment": "negative"}
                    ],
                },
                {
                    "response_id": "1",
                    "assignments": [
                        {"theme_id": "t02", "sentiment": "neutral"}
                    ],
                },
                {
                    "response_id": "2",
                    "assignments": [
                        {"theme_id": "bad", "sentiment": "positive"}
                    ],
                },
                {
                    "response_id": "3",
                    "assignments": [
                        {"theme_id": "other", "sentiment": "neutral"}
                    ],
                },
            ]
        }

        normalized = report_engine._normalize_classifications(
            data,
            ["0", "1", "2", "3"],
            {"t01", "t02", "other"},
        )

        self.assertEqual(set(normalized), {"0", "3"})
        self.assertEqual(
            normalized["3"],
            [{"theme_id": "other", "sentiment": "neutral"}],
        )

    async def test_classification_repairs_missing_ids_then_falls_back_to_other(self):
        final_themes = [
            {"id": "t01", "name": "界面设计", "description": "界面体验"}
        ]
        batch = [
            {"text": "Clean UI"},
            {"text": "Small buttons"},
            {"text": "😂"},
        ]
        first = {
            "data": {
                "classifications": [
                    {
                        "response_id": "0",
                        "assignments": [
                            {"theme_id": "t01", "sentiment": "positive"}
                        ],
                    }
                ]
            },
            "model": "gpt-5.6-terra",
            "raw_len": 100,
            "error": "",
        }
        miss = {
            "data": {
                "classifications": [
                    {
                        "response_id": "1",
                        "assignments": [
                            {"theme_id": "t01", "sentiment": "negative"}
                        ],
                    }
                ]
            },
            "model": "claude-sonnet-5",
            "raw_len": 80,
            "error": "",
        }

        with patch.object(
            report_engine,
            "_direct_json_call",
            new=AsyncMock(side_effect=[first, miss]),
        ) as call:
            result = await report_engine._classify_batch_direct(
                "界面反馈",
                final_themes,
                batch,
            )

        self.assertEqual(call.await_count, 2)
        self.assertEqual(result["repaired_count"], 1)
        self.assertEqual(result["fallback_count"], 1)
        self.assertEqual(
            result["classifications"][2]["assignments"],
            [{"theme_id": "other", "sentiment": "neutral"}],
        )
        miss_query = call.await_args_list[1].args[1]
        self.assertIn("[1] Small buttons", miss_query)
        self.assertIn("[2] 😂", miss_query)
        self.assertNotIn("[0] Clean UI", miss_query)

    async def test_pipeline_uses_unique_respondent_coverage_percentage(self):
        entries = [
            {"text": "A", "respondent_key": "p1"},
            {"text": "B", "respondent_key": "p1"},
            {"text": "C", "respondent_key": "p2"},
            {"text": "D", "respondent_key": "p3"},
        ]
        open_text = {0: entries}
        plan = {
            "columns": [{"index": 0, "name": "反馈", "role": "open_text"}],
            "branch_rules": [],
        }
        candidate = {
            "name": "候选主题",
            "description": "候选范围",
            "positive_summary": None,
            "negative_summary": None,
            "representative_quotes": ["A"],
        }
        final_themes = [
            {
                "id": "t01",
                "name": "界面设计",
                "description": "界面体验",
                "positive_summary": "正面",
                "negative_summary": "负面",
                "representative_quotes": ["A"],
            },
            {
                "id": "t02",
                "name": "性能表现",
                "description": "性能体验",
                "positive_summary": None,
                "negative_summary": "卡顿",
                "representative_quotes": ["B"],
            },
        ]
        direct = AsyncMock(
            side_effect=[
                {"data": {"themes": [candidate]}, "model": "extract", "raw_len": 10, "error": ""},
                {"data": {"themes": [candidate]}, "model": "extract", "raw_len": 10, "error": ""},
                {"data": {"themes": final_themes}, "model": "merge", "raw_len": 20, "error": ""},
            ]
        )
        classified = AsyncMock(
            side_effect=[
                {
                    "classifications": [
                        {
                            "response_id": "0",
                            "assignments": [
                                {"theme_id": "t01", "sentiment": "positive"},
                                {"theme_id": "t02", "sentiment": "negative"},
                            ],
                        },
                        {
                            "response_id": "1",
                            "assignments": [
                                {"theme_id": "t01", "sentiment": "negative"}
                            ],
                        },
                    ],
                    "model": "classify",
                    "raw_len": 20,
                    "repaired_count": 0,
                    "fallback_count": 0,
                    "error": "",
                },
                {
                    "classifications": [
                        {
                            "response_id": "0",
                            "assignments": [
                                {"theme_id": "t01", "sentiment": "neutral"},
                                {"theme_id": "t02", "sentiment": "negative"},
                            ],
                        },
                        {
                            "response_id": "1",
                            "assignments": [
                                {"theme_id": "other", "sentiment": "neutral"}
                            ],
                        },
                    ],
                    "model": "classify",
                    "raw_len": 20,
                    "repaired_count": 0,
                    "fallback_count": 0,
                    "error": "",
                },
            ]
        )

        with (
            patch.object(report_engine, "BATCH_SIZE", 2),
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    open_text,
                    plan,
                    ["反馈"],
                    "sid",
                    deduplicate_respondents=True,
                )
            ]

        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        clustered = next(item[1] for item in items if item[0] == "result")
        themes = {theme["id"]: theme for theme in clustered[0]["themes"]}

        self.assertEqual(themes["t01"]["count"], 2)
        self.assertEqual(themes["t01"]["percentage"], 66.7)
        self.assertEqual(themes["t02"]["count"], 2)
        self.assertEqual(themes["t02"]["percentage"], 66.7)
        self.assertEqual(
            diagnostics["0"]["percentage_basis"],
            "unique_respondent_coverage",
        )
        self.assertEqual(diagnostics["0"]["percentage_denominator"], 3)

    async def test_pipeline_reports_retry_recovery_and_quality_impact(self):
        entries = [{"text": "The interface is clean.", "respondent_key": "p1"}]
        open_text = {0: entries}
        plan = {
            "columns": [{"index": 0, "name": "界面反馈", "role": "open_text"}],
            "parts": [{"name": "体验", "column_indexes": [0]}],
            "branch_rules": [],
        }
        calls = 0

        async def direct(*_args, on_repair=None, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                on_repair("主题证据校验未通过")
                return {
                    "data": {
                        "themes": [{
                            "name": "界面设计",
                            "description": "界面体验",
                            "positive_summary": "界面整洁",
                            "negative_summary": None,
                            "representative_response_ids": ["r0001"],
                        }]
                    },
                    "model": "extract",
                    "raw_len": 20,
                    "repaired": True,
                    "error": "",
                }
            return {
                "data": {
                    "themes": [{
                        "id": "t01",
                        "name": "界面设计",
                        "description": "界面体验",
                        "positive_summary": "界面整洁",
                        "negative_summary": None,
                        "representative_quotes": ["The interface is clean."],
                    }]
                },
                "model": "merge",
                "raw_len": 20,
                "repaired": False,
                "error": "",
            }

        classified = AsyncMock(return_value={
            "classifications": [{
                "response_id": "0",
                "assignments": [{"theme_id": "t01", "sentiment": "positive"}],
            }],
            "model": "classify",
            "raw_len": 10,
            "repaired_count": 0,
            "fallback_count": 0,
            "error": "",
        })

        with (
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    open_text,
                    plan,
                    ["界面反馈"],
                    "sid",
                    deduplicate_respondents=True,
                )
            ]

        progress = [item[1] for item in items if item[0] == "analysis_progress"]
        self.assertTrue(any(item["status"] == "retrying" for item in progress))
        self.assertTrue(any(item["status"] == "recovered" for item in progress))
        self.assertEqual(progress[-1]["status"], "completed")
        self.assertEqual(progress[-1]["respondent_count"], 1)
        clustered = next(item[1] for item in items if item[0] == "result")
        self.assertEqual(
            clustered[0]["themes"][0]["source_quotes"],
            ["The interface is clean."],
        )

    async def test_pipeline_reports_final_degradation_without_claiming_data_loss(self):
        direct = AsyncMock(return_value={
            "data": None,
            "model": "",
            "raw_len": 0,
            "repaired": False,
            "error": "LLM generation failed after retries",
        })
        with (
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_direct_json_call", new=direct),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    {0: [{"text": "原始玩家回答", "respondent_key": "p1"}]},
                    {
                        "columns": [{"index": 0, "name": "反馈", "role": "open_text"}],
                        "parts": [{"name": "体验", "column_indexes": [0]}],
                        "branch_rules": [],
                    },
                    ["反馈"],
                    "sid",
                    deduplicate_respondents=True,
                )
            ]

        progress = [item[1] for item in items if item[0] == "analysis_progress"]
        final = progress[-1]
        self.assertEqual(final["status"], "degraded")
        self.assertIn("全部原文", final["message"])
        self.assertIn("不会丢失玩家原文", final["impact"])
        clustered = next(item[1] for item in items if item[0] == "result")
        self.assertEqual(clustered, {})

    async def test_pipeline_runs_scopes_with_bounded_concurrency_and_stable_order(self):
        open_text = {
            0: [{"text": "回答 A", "respondent_key": "p1"}],
            1: [{"text": "回答 B", "respondent_key": "p2"}],
        }
        plan = {
            "columns": [
                {"index": 0, "name": "问题 A", "role": "open_text"},
                {"index": 1, "name": "问题 B", "role": "open_text"},
            ],
            "parts": [{"name": "体验", "column_indexes": [0, 1]}],
            "branch_rules": [],
        }
        active = 0
        max_active = 0

        async def direct(system_prompt, query, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01 if "问题 A" in query else 0.001)
            active -= 1
            text = "回答 A" if "问题 A" in query else "回答 B"
            if system_prompt == "extract":
                themes = [{
                    "name": "主题",
                    "description": "描述",
                    "positive_summary": None,
                    "negative_summary": None,
                    "representative_quotes": [text],
                }]
                model = "extract"
            else:
                themes = [{
                    "id": "t01",
                    "name": "主题",
                    "description": "描述",
                    "positive_summary": None,
                    "negative_summary": None,
                    "representative_quotes": [text],
                }]
                model = "merge"
            return {
                "data": {"themes": themes},
                "model": model,
                "raw_len": 10,
                "repaired": False,
                "error": "",
                "duration_seconds": 0.01,
            }

        async def classified(_question, _themes, _batch):
            return {
                "classifications": [{
                    "response_id": "0",
                    "assignments": [{"theme_id": "t01", "sentiment": "neutral"}],
                }],
                "model": "classify",
                "raw_len": 10,
                "repaired_count": 0,
                "fallback_count": 0,
                "error": "",
                "duration_seconds": 0.01,
            }

        with (
            patch.object(report_engine, "LLM_QUALITATIVE_SCOPE_CONCURRENCY", 2),
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    open_text, plan, ["问题 A", "问题 B"], "sid",
                    deduplicate_respondents=True,
                )
            ]

        metrics = next(item[1] for item in items if item[0] == "analysis_metrics")
        clustered = next(item[1] for item in items if item[0] == "result")
        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        self.assertEqual(max_active, 2)
        self.assertEqual(metrics["scope_concurrency"], 2)
        self.assertGreater(metrics["elapsed_seconds"], 0)
        self.assertEqual(list(clustered), [0, 1])
        self.assertEqual(list(diagnostics), ["0", "1"])
        self.assertIn("elapsed_seconds", diagnostics["0"])

    async def test_parallel_scope_failure_keeps_successful_scope(self):
        open_text = {
            0: [{"text": "失败回答", "respondent_key": "p1"}],
            1: [{"text": "成功回答", "respondent_key": "p2"}],
        }
        plan = {
            "columns": [
                {"index": 0, "name": "失败题", "role": "open_text"},
                {"index": 1, "name": "成功题", "role": "open_text"},
            ],
            "parts": [{"name": "体验", "column_indexes": [0, 1]}],
            "branch_rules": [],
        }

        async def direct(system_prompt, query, **_kwargs):
            if "失败题" in query:
                return {
                    "data": None,
                    "model": "",
                    "raw_len": 0,
                    "repaired": False,
                    "error": "timeout",
                    "duration_seconds": 0.01,
                }
            themes = ([{
                "name": "成功主题",
                "description": "描述",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": ["成功回答"],
            }] if system_prompt == "extract" else [{
                "id": "t01",
                "name": "成功主题",
                "description": "描述",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": ["成功回答"],
            }])
            return {
                "data": {"themes": themes},
                "model": "ok",
                "raw_len": 10,
                "repaired": False,
                "error": "",
                "duration_seconds": 0.01,
            }

        classified = AsyncMock(return_value={
            "classifications": [{
                "response_id": "0",
                "assignments": [{"theme_id": "t01", "sentiment": "neutral"}],
            }],
            "model": "classify",
            "raw_len": 10,
            "repaired_count": 0,
            "fallback_count": 0,
            "error": "",
            "duration_seconds": 0.01,
        })
        with (
            patch.object(report_engine, "LLM_QUALITATIVE_SCOPE_CONCURRENCY", 2),
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    open_text, plan, ["失败题", "成功题"], "sid",
                    deduplicate_respondents=True,
                )
            ]

        clustered = next(item[1] for item in items if item[0] == "result")
        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        self.assertNotIn(0, clustered)
        self.assertIn(1, clustered)
        self.assertEqual(diagnostics["0"]["status"], "failed")
        self.assertEqual(diagnostics["1"]["status"], "ok")

    def test_merge_query_keeps_full_candidate_evidence(self):
        candidates = [
            {
                "name": "界面设计",
                "description": "界面体验",
                "positive_summary": "更整洁",
                "negative_summary": "按钮太小",
                "representative_quotes": ["The UI is clean."],
            }
        ]

        query = report_engine._build_theme_merge_query(
            "界面反馈",
            candidates,
            100,
        )

        self.assertIn('"positive_summary": "更整洁"', query)
        self.assertIn('"negative_summary": "按钮太小"', query)
        self.assertIn("The UI is clean.", query)


if __name__ == "__main__":
    unittest.main()
