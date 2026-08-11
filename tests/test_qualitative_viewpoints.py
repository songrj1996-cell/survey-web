import unittest
from unittest.mock import AsyncMock, patch

import survey_stats

from app.services import report_engine
from app.services.qualitative_viewpoints import (
    build_report_viewpoint_stats,
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

        with (
            patch.object(
                report_engine,
                "_direct_json_call",
                new=AsyncMock(return_value={"data": {"themes": merged_themes}}),
            ),
            patch.object(
                report_engine,
                "_classify_batch_direct",
                new=AsyncMock(return_value=classified),
            ),
        ):
            events = [
                item async for item in build_report_viewpoint_stats(
                    clustered, open_text, plan, ["ID", "界面反馈", "使用反馈"]
                )
            ]

        result = next(item[1] for item in events if item[0] == "result")
        by_id = {item["id"]: item for item in result}
        self.assertEqual(by_id["RVIEW:t01"]["count"], 2)
        self.assertEqual(by_id["RVIEW:t01"]["denominator"], 2)
        self.assertEqual(by_id["RVIEW:t02"]["count"], 2)
        self.assertEqual(by_id["RVIEW:t02"]["denominator"], 3)


if __name__ == "__main__":
    unittest.main()
