import unittest
from unittest.mock import AsyncMock, patch

from app.services import report_engine


class LargeSampleDirectLLMTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_pipeline_uses_response_coverage_percentage(self):
        entries = [
            {"text": "A"},
            {"text": "B"},
            {"text": "C"},
            {"text": "D"},
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
                )
            ]

        diagnostics = next(item[1] for item in items if item[0] == "diagnostics")
        clustered = next(item[1] for item in items if item[0] == "result")
        themes = {theme["id"]: theme for theme in clustered[0]["themes"]}

        self.assertEqual(themes["t01"]["count"], 3)
        self.assertEqual(themes["t01"]["percentage"], 75.0)
        self.assertEqual(themes["t02"]["count"], 2)
        self.assertEqual(themes["t02"]["percentage"], 50.0)
        self.assertEqual(
            diagnostics["0"]["percentage_basis"],
            "response_coverage",
        )
        self.assertEqual(diagnostics["0"]["percentage_denominator"], 4)

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
