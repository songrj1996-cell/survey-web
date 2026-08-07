import json
import unittest
from unittest.mock import AsyncMock, patch

import survey_plan
from app.schemas.requests import QualitativeContextRequest
from app.services import survey_service


def _analysis_focus() -> dict:
    return {
        "core_question": "新界面是否值得继续投入，以及优先解决什么",
        "report_organization": "先建立跨案例对比框架，再用各案例作为证据",
        "supporting_analyses": ["比较不同段位玩家的满意度", "归纳界面改进原因"],
        "evidence_role": "客观题用于确认差异，开放题用于解释原因",
        "expected_deliverables": ["投入判断", "问题优先级"],
        "avoid_structures": ["不要按案例逐章平铺"],
    }


def _plan_json(*, include_analysis_focus: bool = True) -> str:
    plan = {
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
    }
    if include_analysis_focus:
        plan["analysis_focus"] = _analysis_focus()
    return json.dumps(plan, ensure_ascii=False)


def _open_text_plan_json_with_unexpected_focus() -> str:
    return json.dumps(
        {
            "columns": [{"index": 0, "name": "反馈", "role": "open_text"}],
            "parts": [{"name": "反馈分析", "column_indexes": [0]}],
            "cross_tabs": [],
            "open_questions": [],
            "analysis_focus": {"unexpected": "must be ignored on this branch"},
            "summary": "分析开放题反馈。",
        },
        ensure_ascii=False,
    )


class DirectSurveyPlannerTests(unittest.IsolatedAsyncioTestCase):
    def test_analysis_approach_is_free_form_and_optional_for_legacy_requests(self):
        approach = "先跨案例比较，再围绕决策问题组织证据；案例 A 只作反证。"

        current = QualitativeContextRequest(analysis_approach=approach)
        legacy = QualitativeContextRequest()

        self.assertEqual(current.analysis_approach, approach)
        self.assertEqual(legacy.analysis_approach, "")

    def test_analysis_focus_is_validated_only_when_required_for_legacy_plans(self):
        legacy, legacy_error = survey_plan.parse_plan_from_llm(
            _plan_json(include_analysis_focus=False),
            3,
        )
        required, required_error = survey_plan.parse_plan_from_llm(
            _plan_json(include_analysis_focus=False),
            3,
            require_analysis_focus=True,
        )
        focused, focused_error = survey_plan.parse_plan_from_llm(
            _plan_json(),
            3,
            require_analysis_focus=True,
        )
        minimal_payload = json.loads(_plan_json())
        minimal_payload["analysis_focus"]["supporting_analyses"] = []
        minimal_payload["analysis_focus"]["avoid_structures"] = []
        minimal, minimal_error = survey_plan.parse_plan_from_llm(
            json.dumps(minimal_payload, ensure_ascii=False),
            3,
            require_analysis_focus=True,
        )

        self.assertIsNone(legacy_error)
        self.assertNotIn("analysis_focus", legacy)
        self.assertIsNone(required)
        self.assertIn("analysis_focus", required_error)
        self.assertIsNone(focused_error)
        self.assertEqual(focused["analysis_focus"], _analysis_focus())
        self.assertIsNone(minimal_error)
        self.assertEqual(minimal["analysis_focus"]["supporting_analyses"], [])
        self.assertEqual(minimal["analysis_focus"]["avoid_structures"], [])

    def test_analysis_focus_rejects_missing_or_wrong_typed_fields(self):
        cases = {
            "missing expected deliverables": lambda focus: focus.pop("expected_deliverables"),
            "unexpected field": lambda focus: focus.update({"extra": "not allowed"}),
            "string used for a list": lambda focus: focus.update(
                {"supporting_analyses": "比较不同段位"}
            ),
            "empty list": lambda focus: focus.update({"expected_deliverables": []}),
            "non-string list member": lambda focus: focus.update(
                {"avoid_structures": ["不要逐案例平铺", 7]}
            ),
            "empty required string": lambda focus: focus.update({"core_question": "  "}),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                payload = json.loads(_plan_json())
                mutate(payload["analysis_focus"])

                parsed, error = survey_plan.parse_plan_from_llm(
                    json.dumps(payload, ensure_ascii=False),
                    3,
                    require_analysis_focus=True,
                )

                self.assertIsNone(parsed)
                self.assertIn("analysis_focus", error)

        optional_payload = json.loads(_plan_json())
        optional_payload["analysis_focus"]["evidence_role"] = 7
        optional, optional_error = survey_plan.parse_plan_from_llm(
            json.dumps(optional_payload, ensure_ascii=False),
            3,
        )
        self.assertIsNone(optional)
        self.assertIn("analysis_focus.evidence_role", optional_error)

    def test_plan_allows_disjoint_single_choice_filters_to_reuse_columns(self):
        plan = {
            "columns": [
                {
                    "index": 0,
                    "name": "控制模式",
                    "role": "single_choice",
                    "options": ["中等按钮模式", "默认模式", "大按钮模式"],
                    "value_aliases": {"中等按钮模式": ["Orta Buton Modu"]},
                },
                {"index": 1, "name": "选择原因", "role": "open_text"},
                {"index": 2, "name": "满意度", "role": "scale", "min": 1, "max": 5},
            ],
            "parts": [
                {"name": "整体选择", "column_indexes": [0]},
                {
                    "name": "中等按钮反馈",
                    "column_indexes": [1, 2],
                    "filter": {"column_index": 0, "allowed_options": ["Orta Buton Modu"]},
                },
                {
                    "name": "默认模式反馈",
                    "column_indexes": [1, 2],
                    "filter": {"column_index": 0, "allowed_options": ["默认模式"]},
                },
            ],
            "cross_tabs": [],
            "open_questions": [],
            "summary": "按模式分析",
        }

        parsed, error = survey_plan.parse_plan_from_llm(
            json.dumps(plan, ensure_ascii=False), 3,
        )

        self.assertIsNone(error)
        self.assertEqual(
            parsed["parts"][1]["filter"]["allowed_options"],
            ["中等按钮模式"],
        )
        merged = survey_plan.merge_confirmed_into_plan(parsed, [
            {
                "name_zh": "控制模式",
                "role": "single_choice",
                "column_indexes": [0],
                "options": ["中等按钮模式", "默认模式", "大按钮模式"],
                "value_aliases": {"中等按钮模式": ["Orta Buton Modu"]},
            },
            {"name_zh": "选择原因", "role": "open_text", "column_indexes": [1]},
            {
                "name_zh": "满意度",
                "role": "scale",
                "column_indexes": [2],
                "scale_min": 1,
                "scale_max": 5,
            },
        ])
        self.assertEqual(merged["parts"][1]["column_indexes"], [1, 2])
        self.assertEqual(merged["parts"][2]["column_indexes"], [1, 2])

    def test_plan_rejects_overlapping_filters_for_reused_columns(self):
        plan = {
            "columns": [
                {"index": 0, "name": "模式", "role": "single_choice", "options": ["A", "B"]},
                {"index": 1, "name": "原因", "role": "open_text"},
            ],
            "parts": [
                {"name": "整体", "column_indexes": [0]},
                {"name": "A1", "column_indexes": [1], "filter": {"column_index": 0, "allowed_options": ["A"]}},
                {"name": "A2", "column_indexes": [1], "filter": {"column_index": 0, "allowed_options": ["A"]}},
            ],
            "cross_tabs": [],
            "open_questions": [],
            "summary": "",
        }

        parsed, error = survey_plan.parse_plan_from_llm(json.dumps(plan), 2)

        self.assertIsNone(parsed)
        self.assertIn("overlapping part filters", error)

    def test_plan_rejects_filter_column_inside_its_filtered_part(self):
        plan = {
            "columns": [
                {"index": 0, "name": "模式", "role": "single_choice", "options": ["A", "B"]},
                {"index": 1, "name": "原因", "role": "open_text"},
            ],
            "parts": [
                {
                    "name": "A模式反馈",
                    "column_indexes": [0, 1],
                    "filter": {"column_index": 0, "allowed_options": ["A"]},
                },
                {
                    "name": "B模式反馈",
                    "column_indexes": [1],
                    "filter": {"column_index": 0, "allowed_options": ["B"]},
                },
            ],
            "cross_tabs": [],
            "open_questions": [],
            "summary": "",
        }

        parsed, error = survey_plan.parse_plan_from_llm(json.dumps(plan), 2)

        self.assertIsNone(parsed)
        self.assertIn("must not analyze its own filter column", error)

    def test_plan_requires_unfiltered_overview_for_filter_parent(self):
        plan = {
            "columns": [
                {"index": 0, "name": "模式", "role": "single_choice", "options": ["A"]},
                {"index": 1, "name": "原因", "role": "open_text"},
                {"index": 2, "name": "其他分群", "role": "single_choice", "options": ["X"]},
            ],
            "parts": [
                {"name": "其他分群概览", "column_indexes": [2]},
                {
                    "name": "模式题放错章节",
                    "column_indexes": [0],
                    "filter": {"column_index": 2, "allowed_options": ["X"]},
                },
                {
                    "name": "A模式反馈",
                    "column_indexes": [1],
                    "filter": {"column_index": 0, "allowed_options": ["A"]},
                },
            ],
            "cross_tabs": [],
            "open_questions": [],
            "summary": "",
        }

        parsed, error = survey_plan.parse_plan_from_llm(json.dumps(plan), 3)

        self.assertIsNone(parsed)
        self.assertIn("filter column 0 must appear in an unfiltered overview part", error)

    def test_questionnaire_columns_require_llm_only_until_translation_is_cached(self):
        with patch.object(
            survey_service,
            "get_session",
            return_value={"column_provider": "questionnaire"},
        ):
            self.assertTrue(survey_service.columns_require_llm("sid"))

        with patch.object(
            survey_service,
            "get_session",
            return_value={
                "column_provider": "questionnaire",
                "questionnaire_translation_status": "translated",
            },
        ):
            self.assertFalse(survey_service.columns_require_llm("sid"))

    async def test_questionnaire_columns_are_translated_without_reclassifying(self):
        sess = {
            "rows": [
                ["Familiarity [The Mist]", "Familiarity [Northern Vale]"],
                ["Very familiar", "Never heard of it"],
            ],
            "column_provider": "questionnaire",
            "columns_detected": [{
                "source_question_id": "Q5",
                "name_zh": "How familiar are you with the following?",
                "role": "matrix_single",
                "column_indexes": [0, 1],
                "rows": ["The Mist", "Northern Vale"],
                "options": ["Very familiar", "Never heard of it"],
                "options_original": ["Very familiar", "Never heard of it"],
            }],
        }
        answer = json.dumps({
            "translations": [{
                "question_id": "Q5",
                "name_zh": "您对以下内容的熟悉程度如何？",
                "options_zh": ["非常熟悉", "从未听说过"],
                "rows_zh": ["迷雾之地", "北境山谷"],
            }],
        }, ensure_ascii=False)
        collect = AsyncMock(return_value=(answer, "gpt-5.6-terra"))

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service, "collect_chat_completion", new=collect),
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

        translated = sess["columns_detected"][0]
        self.assertEqual(translated["role"], "matrix_single")
        self.assertEqual(translated["column_indexes"], [0, 1])
        self.assertEqual(translated["name_zh"], "您对以下内容的熟悉程度如何？")
        self.assertEqual(translated["options"], ["非常熟悉", "从未听说过"])
        self.assertEqual(
            translated["value_aliases"]["非常熟悉"],
            ["Very familiar"],
        )
        self.assertEqual(sess["questionnaire_translation_status"], "translated")
        self.assertTrue(any('"type": "columns_ready"' in event for event in events))

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

    async def test_standard_plan_and_revision_receive_analysis_approach(self):
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
        analysis_approach = "先建立跨案例框架，案例只作为证据，不按案例逐章平铺"
        sess = {
            "rows": rows,
            "confirmed_columns": confirmed,
            "qualitative_context": {"analysis_approach": analysis_approach},
        }
        collect = AsyncMock(side_effect=[
            (_plan_json(), "gpt-5.6-sol"),
            (_plan_json(), "claude-sonnet-5"),
        ])

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
            initial_events = [
                event async for event in survey_service.plan_stream("sid", object())
            ]
            revision_events = [
                event
                async for event in survey_service.plan_revision_stream(
                    "sid",
                    "重新设计主线，不要沿用现有 Part，案例仅作证据",
                    object(),
                )
            ]

        self.assertTrue(any('"type": "plan_ready"' in event for event in initial_events))
        self.assertTrue(any('"type": "plan_ready"' in event for event in revision_events))
        self.assertEqual(sess["planner_provider"], "direct_llm")
        self.assertEqual(sess["planner_model"], "claude-sonnet-5")
        self.assertEqual(sess["planner_conv_id"], "")
        self.assertEqual(collect.await_count, 2)
        self.assertEqual(
            collect.await_args_list[0].kwargs["models"],
            ("gpt-5.6-sol", "claude-sonnet-5"),
        )
        self.assertEqual(
            collect.await_args_list[0].args[0][0]["role"],
            "system",
        )
        self.assertIn(
            "<analysis_focus_mode>enabled</analysis_focus_mode>",
            collect.await_args_list[0].args[0][0]["content"],
        )
        initial_query = collect.await_args_list[0].args[0][1]["content"]
        revision_query = collect.await_args_list[1].args[0][1]["content"]
        self.assertIn(analysis_approach, initial_query)
        self.assertIn(analysis_approach, revision_query)
        self.assertIn("局部调整", revision_query)
        self.assertIn("主线重建", revision_query)
        self.assertIn("先重建完整 analysis_focus", revision_query)
        self.assertIn("parts、cross_tabs", revision_query)
        self.assertIn("columns 仍保持权威不变", revision_query)
        self.assertIn("重新设计主线", revision_query)
        self.assertIn("不要沿用现有 Part", revision_query)
        self.assertIn("案例仅作证据", revision_query)

    async def test_quantitative_and_predicted_large_plans_disable_analysis_focus(self):
        analysis_approach = "按跨案例分析主线组织报告"
        business_problem = "判断反馈是否需要安排修复"
        scenarios = {
            "quantitative": {
                "analysis_mode": "quantitative",
                "expects_business_context": False,
                "rows": [["反馈"], ["小样本反馈"]],
                "confirmed_columns": [
                    {"name_zh": "反馈", "role": "open_text", "column_indexes": [0]}
                ],
            },
            "predicted_large": {
                "expects_business_context": True,
                "rows": [["反馈"]]
                + [[f"第 {index} 条反馈"] for index in range(survey_service.LARGE_SAMPLE_THRESHOLD + 1)],
                "columns_detected": [
                    {"name_zh": "反馈", "role": "open_text", "column_indexes": [0]}
                ],
            },
        }

        for label, sess in scenarios.items():
            with self.subTest(label=label):
                sess["qualitative_context"] = {
                    "problem": business_problem,
                    "analysis_approach": analysis_approach,
                }
                collect = AsyncMock(side_effect=[
                    (_open_text_plan_json_with_unexpected_focus(), "model-a"),
                    (_open_text_plan_json_with_unexpected_focus(), "model-b"),
                ])

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
                    patch.object(survey_service, "_get_planner_extra", return_value="planner extra"),
                    patch(
                        "app.services.report_engine._get_planner_extra",
                        return_value="planner extra",
                    ),
                ):
                    initial_events = [
                        event async for event in survey_service.plan_stream("sid", object())
                    ]
                    # 模拟旧会话残留 focus；禁用分支的修订请求也不得把它带回模型上下文。
                    sess["plan"]["analysis_focus"] = _analysis_focus()
                    revision_events = [
                        event
                        async for event in survey_service.plan_revision_stream(
                            "sid", "调整章节表达", object()
                        )
                    ]

                self.assertTrue(any('"type": "plan_ready"' in event for event in initial_events))
                self.assertTrue(any('"type": "plan_ready"' in event for event in revision_events))
                self.assertNotIn("analysis_focus", sess["plan"])
                self.assertEqual(collect.await_count, 2)
                for call_index, call in enumerate(collect.await_args_list):
                    system_text = call.args[0][0]["content"]
                    query_text = call.args[0][1]["content"]
                    self.assertIn(
                        "<analysis_focus_mode>disabled</analysis_focus_mode>",
                        system_text,
                    )
                    self.assertIn(
                        "<analysis_focus_mode>disabled</analysis_focus_mode>",
                        query_text,
                    )
                    if sess["expects_business_context"]:
                        self.assertIn(business_problem, query_text)
                    else:
                        self.assertNotIn(business_problem, query_text)
                    self.assertNotIn(analysis_approach, query_text)
                    if call_index == 1:
                        self.assertNotIn(_analysis_focus()["core_question"], query_text)

    async def test_standard_revision_repairs_a_plan_missing_analysis_focus(self):
        sess = {
            "rows": [["段位", "主玩位置", "满意度"], ["神话", "坦克", "5"]],
            "confirmed_columns": [
                {"name_zh": "段位", "role": "profile_dim", "column_indexes": [0]},
                {"name_zh": "主玩位置", "role": "profile_dim", "column_indexes": [1]},
                {
                    "name_zh": "满意度",
                    "role": "scale",
                    "column_indexes": [2],
                    "scale_min": 1,
                    "scale_max": 5,
                },
            ],
            "plan": json.loads(_plan_json(include_analysis_focus=False)),
        }
        collect = AsyncMock(side_effect=[
            (_plan_json(include_analysis_focus=False), "model-a"),
            (_plan_json(), "model-b"),
        ])

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
        ):
            events = [
                event
                async for event in survey_service.plan_revision_stream(
                    "sid", "调整章节名称", object()
                )
            ]

        self.assertEqual(collect.await_count, 2)
        self.assertTrue(any("方案格式校验中" in event for event in events))
        self.assertEqual(sess["plan"]["analysis_focus"], _analysis_focus())

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
