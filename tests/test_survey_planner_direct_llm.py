import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services import survey_service


def _plan_json() -> str:
    return json.dumps(
        {
            "columns": [
                {"index": 0, "name": "段位", "role": "profile_dim"},
                {"index": 1, "name": "主玩位置", "role": "profile_dim"},
                {
                    "index": 2,
                    "name": "界面满意度",
                    "role": "scale",
                    "min": 1,
                    "max": 5,
                },
            ],
            "parts": [
                {"name": "玩家画像", "column_indexes": [0, 1]},
                {"name": "界面评价", "column_indexes": [2]},
            ],
            "cross_tabs": [{"profile_index": 0, "question_index": 2}],
            "open_questions": [],
            "summary": "先分析玩家画像，再分析界面评价。",
        },
        ensure_ascii=False,
    )


class DirectSurveyPlannerTests(unittest.IsolatedAsyncioTestCase):
    async def test_columns_stream_uses_multilingual_direct_model_chain(self):
        rows = [
            ["Which role do you usually play?", "Seberapa puas Anda dengan antarmuka baru?"],
            ["Tank", "1"],
            ["Mage", "2"],
            ["Marksman", "3"],
            ["Tank", "4"],
            ["Mage", "5"],
        ]
        sess = {"rows": rows}
        answer = """```json
{"questions":[
  {"name_zh":"主玩位置","role":"profile_dim","column_indexes":[0],
   "options":["坦克","法师","射手"],
   "value_aliases":{"坦克":["Tank"],"法师":["Mage"],"射手":["Marksman"]}},
  {"name_zh":"新界面满意度","role":"scale","column_indexes":[1],
   "scale_min":1,"scale_max":5}
]}
```"""
        collect = AsyncMock(return_value=(answer, "gpt-5.6-terra"))

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(
                survey_service,
                "_get_column_detect_system_prompt",
                return_value="column system",
            ),
            patch.object(survey_service, "LLM_COLUMN_MODEL", "gpt-5.6-terra"),
            patch.object(
                survey_service,
                "LLM_COLUMN_FALLBACK_MODELS",
                ("qwen3.7-plus",),
            ),
        ):
            events = [
                event async for event in survey_service.columns_stream("sid", object())
            ]

        self.assertTrue(any('"type": "columns_ready"' in event for event in events))
        self.assertEqual(sess["column_provider"], "direct_llm")
        self.assertEqual(sess["column_model"], "gpt-5.6-terra")
        self.assertEqual(sess["columns_detected"][0]["name_zh"], "主玩位置")
        self.assertEqual(
            collect.await_args.kwargs["models"],
            ("gpt-5.6-terra", "qwen3.7-plus"),
        )
        messages = collect.await_args.args[0]
        self.assertEqual(messages[0], {"role": "system", "content": "column system"})
        self.assertIn("Which role do you usually play?", messages[1]["content"])
        self.assertIn("Seberapa puas Anda", messages[1]["content"])

    async def test_columns_stream_repairs_invalid_json_without_dify_conversation(self):
        rows = [["Rank"], ["Mythic"], ["Epic"]]
        valid = """```json
{"questions":[{"name_zh":"段位","role":"profile_dim","column_indexes":[0],
"options":["神话","史诗"],"value_aliases":{"神话":["Mythic"],"史诗":["Epic"]}}]}
```"""
        collect = AsyncMock(
            side_effect=[
                ("not-json", "gpt-5.6-terra"),
                (valid, "gpt-5.6-terra"),
            ]
        )
        sess = {"rows": rows}

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(
                survey_service,
                "_get_column_detect_system_prompt",
                return_value="column system",
            ),
        ):
            events = [
                event async for event in survey_service.columns_stream("sid", object())
            ]

        self.assertEqual(collect.await_count, 2)
        repair_messages = collect.await_args.args[0]
        self.assertEqual(repair_messages[2]["role"], "assistant")
        self.assertEqual(repair_messages[2]["content"], "not-json")
        self.assertIn("无法通过校验", repair_messages[3]["content"])
        self.assertTrue(any('"type": "columns_ready"' in event for event in events))

    async def test_qualitative_plan_uses_shared_planner_model_chain(self):
        rows = [
            ["Rank", "Which role do you usually play?", "Satisfaction"],
            ["Mythic", "Tank", "5"],
        ]
        confirmed = [
            {
                "name_zh": "段位",
                "role": "profile_dim",
                "column_indexes": [0],
                "options": ["神话"],
            },
            {
                "name_zh": "主玩位置",
                "role": "profile_dim",
                "column_indexes": [1],
                "options": ["坦克"],
            },
            {
                "name_zh": "界面满意度",
                "role": "scale",
                "column_indexes": [2],
                "scale_min": 1,
                "scale_max": 5,
            },
        ]
        sess = {"rows": rows, "confirmed_columns": confirmed}
        collect = AsyncMock(return_value=(_plan_json(), "gpt-5.6-sol"))

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(survey_service, "_ensure_branch_rules", return_value=[]),
            patch.object(
                survey_service,
                "_get_survey_planner_system_prompt",
                return_value="planner system",
            ),
            patch.object(survey_service, "LLM_PLANNER_MODEL", "gpt-5.6-sol"),
            patch.object(
                survey_service,
                "LLM_PLANNER_FALLBACK_MODELS",
                ("claude-sonnet-5",),
            ),
        ):
            events = [event async for event in survey_service.plan_stream("sid", object())]

        self.assertTrue(any('"type": "plan_ready"' in event for event in events))
        self.assertEqual(sess["planner_provider"], "direct_llm")
        self.assertEqual(sess["planner_model"], "gpt-5.6-sol")
        self.assertEqual(sess["planner_conv_id"], "")
        self.assertEqual(
            collect.await_args.kwargs["models"],
            ("gpt-5.6-sol", "claude-sonnet-5"),
        )
        self.assertEqual(
            collect.await_args.args[0][0],
            {"role": "system", "content": "planner system"},
        )

    async def test_crosstab_plan_and_revision_keep_available_questions(self):
        rows = [["题目"], ["数据"]]
        sess = {
            "mode": "crosstab",
            "rows": rows,
            "confirmed_columns": [{"name": "改进建议", "role": "open_text"}],
            "questionnaire_text": "Q1. How satisfied are you?\nQ2. Why?",
            "crosstab_questions": ["满意度", "改进建议"],
        }
        initial = """```json
{"parts":[{"name":"总体评价","scope":"满意度"},{"name":"改进方向","scope":"改进建议"}],
"open_questions":[]}
```"""
        revised = """```json
{"parts":[{"name":"满意度与改进","scope":"满意度、改进建议"}],"open_questions":[]}
```"""
        collect = AsyncMock(
            side_effect=[
                (initial, "gpt-5.6-sol"),
                (revised, "claude-sonnet-5"),
            ]
        )

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(survey_service, "_ensure_branch_rules", return_value=[]),
            patch.object(
                survey_service,
                "_get_crosstab_planner_system_prompt",
                return_value="crosstab system",
            ),
        ):
            initial_events = [
                event async for event in survey_service.plan_stream("sid", object())
            ]
            revision_events = [
                event
                async for event in survey_service.plan_revision_stream(
                    "sid", "合并为一章", object()
                )
            ]

        self.assertTrue(any('"type": "plan_ready"' in event for event in initial_events))
        self.assertTrue(any('"type": "plan_ready"' in event for event in revision_events))
        revision_query = collect.await_args.args[0][1]["content"]
        self.assertIn("<available_questions>", revision_query)
        self.assertIn("- 满意度", revision_query)
        self.assertIn("- 改进建议", revision_query)
        self.assertIn("<open_questions_list>", revision_query)
        self.assertEqual(sess["planner_model"], "claude-sonnet-5")
        self.assertEqual(sess["planner_provider"], "direct_llm")


if __name__ == "__main__":
    unittest.main()
