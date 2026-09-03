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
            "字符串",
            report_engine._validate_theme_candidates(malformed, ["原文"]),
        )

    def test_theme_extract_has_no_count_cap_and_normalizes_extra_evidence_ids(self):
        source = [f"原文{i}" for i in range(1, 6)]
        themes = [
            {
                "name": f"主题{i}",
                "description": f"独立范围{i}",
                "positive_summary": None,
                "negative_summary": None,
                "representative_response_ids": [
                    "r0001", "r0002", "r0002", "r0003", "r0004"
                ],
            }
            for i in range(1, 31)
        ]
        data = {"themes": themes}

        self.assertIsNone(report_engine._validate_theme_candidates(data, source))
        hydrated = report_engine._hydrate_theme_candidate_quotes(data, source)
        self.assertEqual(
            hydrated["themes"][0]["representative_quotes"],
            ["原文1", "原文2", "原文3"],
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

    def test_merge_validator_has_no_theme_count_limit_and_requires_full_mapping(self):
        candidates = [
            {
                "name": f"主题{i}",
                "description": f"范围{i}",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": [f"quote-{i}"],
            }
            for i in range(1, 31)
        ]
        merged = {
            "themes": [{
                "id": "t01",
                "name": "同一语义主题",
                "description": "全部候选语义等价",
                "positive_summary": None,
                "negative_summary": None,
                "source_candidate_ids": [f"c{i:04d}" for i in range(1, 31)],
                "representative_quotes": ["quote-1"],
            }]
        }

        self.assertIsNone(
            report_engine._validate_merged_themes(merged, candidates)
        )

        distinct = {
            "themes": [
                {
                    "id": f"t{i:02d}",
                    "name": f"最终主题{i}",
                    "description": f"最终范围{i}",
                    "positive_summary": None,
                    "negative_summary": None,
                    "source_candidate_ids": [f"c{i:04d}"],
                    "representative_quotes": [f"quote-{i}"],
                }
                for i in range(1, 31)
            ]
        }
        self.assertIsNone(
            report_engine._validate_merged_themes(distinct, candidates)
        )

        merged["themes"][0]["source_candidate_ids"] = [
            f"c{i:04d}" for i in range(1, 30)
        ]
        self.assertIn(
            "未分配",
            report_engine._validate_merged_themes(merged, candidates),
        )

    def test_merge_validator_rejects_duplicate_mapping_and_restores_real_quotes(self):
        candidates = [
            {
                "name": "界面设计",
                "description": "界面体验",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": ["界面清晰"],
            },
            {
                "name": "性能表现",
                "description": "性能体验",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": ["战斗卡顿"],
            },
        ]
        merged = {
            "themes": [
                {
                    "id": "t01",
                    "name": "界面设计",
                    "description": "界面体验",
                    "positive_summary": None,
                    "negative_summary": None,
                    "source_candidate_ids": ["c0001"],
                    "representative_quotes": ["界面清晰"],
                },
                {
                    "id": "t02",
                    "name": "性能表现",
                    "description": "性能体验",
                    "positive_summary": None,
                    "negative_summary": None,
                    "source_candidate_ids": ["c0002"],
                    "representative_quotes": ["战斗卡顿"],
                },
            ]
        }

        merged["themes"][1]["source_candidate_ids"] = ["c0001", "c0002"]
        self.assertIn(
            "重复分配",
            report_engine._validate_merged_themes(merged, candidates),
        )
        merged["themes"][1]["source_candidate_ids"] = ["c0002"]
        merged["themes"][1]["representative_quotes"] = ["并不存在的原文"]
        self.assertIsNone(
            report_engine._validate_merged_themes(merged, candidates)
        )
        self.assertEqual(
            merged["themes"][1]["representative_quotes"],
            ["战斗卡顿"],
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

    def test_classification_normalizer_accepts_more_than_three_explicit_themes(self):
        assignments = [
            {"theme_id": f"t{i:02d}", "sentiment": "neutral"}
            for i in range(1, 6)
        ]
        normalized = report_engine._normalize_classifications(
            {"classifications": [{"response_id": "0", "assignments": assignments}]},
            ["0"],
            {assignment["theme_id"] for assignment in assignments},
        )

        self.assertEqual(normalized["0"], assignments)

    async def test_classification_accepts_numeric_zero_response_id(self):
        final_themes = [
            {"id": "t01", "name": "界面设计", "description": "界面体验"}
        ]
        batch = [{"text": "Clean UI"}, {"text": "Small buttons"}]
        first = {
            "data": {
                "classifications": [
                    {
                        "response_id": 0,
                        "assignments": [
                            {"theme_id": "t01", "sentiment": "positive"}
                        ],
                    },
                    {
                        "response_id": 1,
                        "assignments": [
                            {"theme_id": "t01", "sentiment": "negative"}
                        ],
                    },
                ]
            },
            "model": "gpt-5.6-terra",
            "raw_len": 100,
            "error": "",
        }

        with patch.object(
            report_engine,
            "_direct_json_call",
            new=AsyncMock(return_value=first),
        ) as call:
            result = await report_engine._classify_batch_direct(
                "界面反馈",
                final_themes,
                batch,
            )

        self.assertEqual(call.await_count, 1)
        self.assertEqual(result["repaired_count"], 0)
        self.assertEqual(result["fallback_count"], 0)
        self.assertEqual(
            result["classifications"][0]["assignments"],
            [{"theme_id": "t01", "sentiment": "positive"}],
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

    async def test_pipeline_keeps_low_frequency_themes_in_full_report_material(self):
        entries = [
            {"text": f"回答{i}", "respondent_key": f"p{i}"}
            for i in range(1, 22)
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
            "representative_quotes": ["回答1"],
        }
        final_themes = [
            {
                "id": "t01",
                "name": "主要主题",
                "description": "主要反馈",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": ["回答1"],
            },
            {
                "id": "t02",
                "name": "低频独立主题",
                "description": "少数但独立的反馈",
                "positive_summary": None,
                "negative_summary": None,
                "representative_quotes": ["回答21"],
            },
        ]
        direct = AsyncMock(side_effect=[
            {"data": {"themes": [candidate]}, "model": "extract", "raw_len": 10, "error": ""},
            {"data": {"themes": final_themes}, "model": "merge", "raw_len": 20, "error": ""},
        ])
        classifications = [
            {
                "response_id": str(index),
                "assignments": [{
                    "theme_id": "t02" if index == 20 else "t01",
                    "sentiment": "neutral",
                }],
            }
            for index in range(21)
        ]
        classified = AsyncMock(return_value={
            "classifications": classifications,
            "model": "classify",
            "raw_len": 20,
            "repaired_count": 0,
            "fallback_count": 0,
            "error": "",
        })

        with (
            patch.object(report_engine, "BATCH_SIZE", 50),
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    open_text, plan, ["反馈"], "sid", deduplicate_respondents=True
                )
            ]

        clustered = next(item[1] for item in items if item[0] == "result")
        by_id = {theme["id"]: theme for theme in clustered[0]["themes"]}
        self.assertEqual(by_id["t02"]["count"], 1)
        self.assertEqual(by_id["t02"]["percentage"], 4.8)
        self.assertEqual(clustered[0]["other_themes"], [])

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

    async def test_phase_b_timeout_recovers_single_batch_structured_counts(self):
        entries = [
            {"text": f"回答{i}", "respondent_key": f"p{i}"}
            for i in range(1, 93)
        ]
        plan = {
            "columns": [{"index": 0, "name": "概念排名原因", "role": "open_text"}],
            "parts": [{"name": "概念偏好", "column_indexes": [0]}],
            "branch_rules": [],
        }
        candidates = [
            {
                "name": "偏好原因A",
                "description": "第一类偏好原因",
                "positive_summary": "认可原因A",
                "negative_summary": None,
                "representative_response_ids": ["r0001"],
            },
            {
                "name": "偏好原因B",
                "description": "第二类偏好原因",
                "positive_summary": None,
                "negative_summary": "质疑原因B",
                "representative_response_ids": ["r0092"],
            },
        ]
        direct = AsyncMock(side_effect=[
            {
                "data": {"themes": candidates},
                "model": "extract",
                "raw_len": 20,
                "repaired": False,
                "error": "",
                "duration_seconds": 0.01,
            },
            {
                "data": None,
                "model": "",
                "raw_len": 0,
                "repaired": False,
                "error": "TimeoutError",
                "duration_seconds": 300.0,
            },
        ])
        classifications = [
            {
                "response_id": str(index),
                "assignments": [{
                    "theme_id": "t01" if index < 46 else "t02",
                    "sentiment": "neutral",
                }],
            }
            for index in range(92)
        ]
        classified = AsyncMock(return_value={
            "classifications": classifications,
            "model": "classify",
            "raw_len": 100,
            "repaired_count": 0,
            "fallback_count": 0,
            "error": "",
            "duration_seconds": 0.01,
        })

        with (
            patch.object(report_engine, "BATCH_SIZE", 300),
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    {0: entries},
                    plan,
                    ["概念排名原因"],
                    "sid",
                    deduplicate_respondents=True,
                )
            ]

        self.assertEqual(direct.await_count, 2)
        self.assertEqual(classified.await_count, 1)
        merge_query = direct.await_args_list[1].args[1]
        self.assertIn('"candidate_id": "c0001"', merge_query)
        self.assertIn('"candidate_id": "c0002"', merge_query)
        self.assertIn("回答1", merge_query)
        self.assertIn("回答92", merge_query)

        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        diag = diagnostics["0"]
        self.assertEqual(diag["status"], "ok")
        self.assertEqual(diag["quality_status"], "ok")
        self.assertEqual(diag["classifications"], 92)
        self.assertEqual(diag["assignments"], 92)
        self.assertEqual(diag["classification_coverage"], 1.0)
        self.assertEqual(diag["classification_fallback_count"], 0)
        self.assertEqual(
            diag["phase_b"]["strategy"],
            "single_batch_candidate_recovery",
        )
        self.assertTrue(diag["phase_b"]["recovered"])
        self.assertEqual(diag["phase_b"]["initial_error"], "TimeoutError")
        self.assertEqual(diag["phase_b"]["error"], "")
        self.assertEqual(diag["phase_b"]["source_candidate_coverage"], 1.0)

        clustered = next(item[1] for item in items if item[0] == "result")
        self.assertEqual(clustered[0]["total"], 92)
        themes = {theme["id"]: theme for theme in clustered[0]["themes"]}
        self.assertEqual(themes["t01"]["count"], 46)
        self.assertEqual(themes["t02"]["count"], 46)
        progress = [item[1] for item in items if item[0] == "analysis_progress"]
        recovered = [
            item for item in progress
            if item.get("step") == "merging" and item.get("status") == "recovered"
        ]
        self.assertEqual(len(recovered), 1)
        self.assertIn("全部回答", recovered[0]["impact"])
        self.assertEqual(progress[-1]["status"], "completed")

    async def test_phase_b_failure_does_not_promote_multiple_batches(self):
        entries = [
            {"text": "回答A", "respondent_key": "p1"},
            {"text": "回答B", "respondent_key": "p2"},
        ]
        plan = {
            "columns": [{"index": 0, "name": "反馈", "role": "open_text"}],
            "parts": [{"name": "体验", "column_indexes": [0]}],
            "branch_rules": [],
        }

        async def direct(system_prompt, query, **_kwargs):
            if system_prompt == "extract":
                text = "回答A" if "回答A" in query else "回答B"
                return {
                    "data": {"themes": [{
                        "name": f"{text}主题",
                        "description": f"{text}描述",
                        "positive_summary": None,
                        "negative_summary": None,
                        "representative_response_ids": ["r0001"],
                    }]},
                    "model": "extract",
                    "raw_len": 10,
                    "repaired": False,
                    "error": "",
                    "duration_seconds": 0.01,
                }
            return {
                "data": None,
                "model": "",
                "raw_len": 0,
                "repaired": False,
                "error": "TimeoutError",
                "duration_seconds": 300.0,
            }

        classified = AsyncMock()
        with (
            patch.object(report_engine, "BATCH_SIZE", 1),
            patch.object(report_engine, "LLM_THEME_EXTRACT_CONCURRENCY", 1),
            patch.object(report_engine, "_get_theme_extract_system_prompt", return_value="extract"),
            patch.object(report_engine, "_get_theme_merge_system_prompt", return_value="merge"),
            patch.object(report_engine, "_direct_json_call", new=direct),
            patch.object(report_engine, "_classify_batch_direct", new=classified),
        ):
            items = [
                item
                async for item in report_engine._batch_qualitative_analysis(
                    {0: entries}, plan, ["反馈"], "sid",
                    deduplicate_respondents=True,
                    _batch_concurrency_override=1,
                )
            ]

        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        self.assertEqual(diagnostics["0"]["status"], "failed")
        self.assertFalse(diagnostics["0"]["phase_b"]["recovered"])
        self.assertEqual(diagnostics["0"]["phase_b"]["strategy"], "llm_merge")
        self.assertEqual(diagnostics["0"]["phase_b"]["error"], "TimeoutError")
        self.assertEqual(next(item[1] for item in items if item[0] == "result"), {})
        classified.assert_not_awaited()

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
        attempt_callback = object()
        callback_forwarded: list[bool] = []

        async def direct(system_prompt, query, **_kwargs):
            nonlocal active, max_active
            callback_forwarded.append(
                _kwargs.get("on_attempt_event") is attempt_callback
            )
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

        async def classified(_question, _themes, _batch, **_kwargs):
            callback_forwarded.append(
                _kwargs.get("on_attempt_event") is attempt_callback
            )
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
                    on_attempt_event=attempt_callback,
                )
            ]

        metrics = next(item[1] for item in items if item[0] == "analysis_metrics")
        clustered = next(item[1] for item in items if item[0] == "result")
        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        self.assertEqual(max_active, 2)
        self.assertTrue(callback_forwarded)
        self.assertTrue(all(callback_forwarded))
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
        self.assertIn('"candidate_id": "c0001"', query)


if __name__ == "__main__":
    unittest.main()
