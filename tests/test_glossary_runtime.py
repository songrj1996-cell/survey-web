import json
import unittest
from unittest.mock import AsyncMock, patch

from app.services import (
    comment_pipeline,
    interview_service,
    report_engine,
    survey_service,
)


def _prepared(messages: list[dict]) -> list[dict]:
    """测试替身：复制消息并追加一条可观察的术语规则。"""
    return [dict(message) for message in messages] + [
        {"role": "system", "content": "术语规则：Phantom Blade -> 幻影之刃"}
    ]


def _normalized_text(value):
    if not isinstance(value, str):
        return value
    return value.replace("Phantom Blade", "幻影之刃")


def _normalized_data(value, *, field: str = "", protected_keys=None):
    """模拟结构化规范化：保留 key、标识字段和来源引用。"""
    protected = {
        "id",
        "idx",
        "theme_id",
        "response_id",
        "sentiment",
        "source_refs",
    }
    protected.update(protected_keys or set())
    if isinstance(value, dict):
        return {
            key: _normalized_data(
                item,
                field=key,
                protected_keys=protected,
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        if field in protected:
            return list(value)
        return [
            _normalized_data(
                item,
                field=field,
                protected_keys=protected,
            )
            for item in value
        ]
    if field in protected:
        return value
    return _normalized_text(value)


class _Request:
    async def is_disconnected(self):
        return False


class GlossarySurveyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_survey_call_prepares_messages_without_rewriting_json(self):
        raw_json = '{"options":["Phantom Blade"]}'
        collect = AsyncMock(return_value=(raw_json, "planner-model"))
        source_messages = [
            {"role": "system", "content": "planner rules"},
            {"role": "user", "content": "规划 Phantom Blade"},
        ]

        with (
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(
                survey_service,
                "prepare_glossary_messages",
                side_effect=_prepared,
            ),
        ):
            results = [
                result
                async for _event, result in survey_service._run_direct_llm(
                    source_messages,
                    models=("planner-model",),
                )
                if result is not None
            ]

        self.assertEqual(results, [(raw_json, "planner-model")])
        self.assertEqual(len(source_messages), 2)
        self.assertEqual(
            collect.await_args.args[0][-1]["content"],
            "术语规则：Phantom Blade -> 幻影之刃",
        )

    async def test_writer_and_qa_use_prepared_copies_and_normalize_text(self):
        collect = AsyncMock(
            side_effect=[
                ("Phantom Blade 是主要主题。", "writer-model"),
                ("追问仍应写作 Phantom Blade。", "qa-model"),
            ]
        )
        writer_messages = [{"role": "system", "content": "writer rules"}]
        source = {
            "qa_context_md": "<report>Phantom Blade 使用反馈</report>",
            "qa_messages": [],
        }

        with (
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(
                survey_service,
                "prepare_glossary_messages",
                side_effect=_prepared,
            ),
            patch.object(
                survey_service,
                "normalize_glossary_terms",
                side_effect=_normalized_text,
            ),
        ):
            writer_answer, writer_model = await survey_service._direct_writer_round(
                writer_messages,
                "分析 Phantom Blade",
            )
            qa_answer, qa_model, _ = await survey_service._answer_qa_direct(
                source,
                "Phantom Blade 的依据是什么？",
            )

        self.assertEqual(writer_answer, "幻影之刃 是主要主题。")
        self.assertEqual(writer_model, "writer-model")
        self.assertEqual(qa_answer, "追问仍应写作 幻影之刃。")
        self.assertEqual(qa_model, "qa-model")
        self.assertEqual(
            writer_messages,
            [
                {"role": "system", "content": "writer rules"},
                {"role": "user", "content": "分析 Phantom Blade"},
                {"role": "assistant", "content": "幻影之刃 是主要主题。"},
            ],
        )
        for call in collect.await_args_list:
            sent_messages = call.args[0]
            self.assertEqual(
                sent_messages[-1]["content"],
                "术语规则：Phantom Blade -> 幻影之刃",
            )

    def test_questionnaire_and_plan_json_are_normalized_after_parsing(self):
        translations = {
            "q1": {
                "name_zh": "Phantom Blade 评分",
                "options_zh": ["Phantom Blade", "从未使用"],
                "rows_zh": [],
            }
        }
        with patch.object(
            survey_service,
            "normalize_glossary_terms",
            side_effect=_normalized_text,
        ):
            normalized = survey_service._normalize_questionnaire_translation_texts(
                translations
            )

        self.assertEqual(
            normalized["q1"]["name_zh"],
            "幻影之刃 评分",
        )
        self.assertEqual(normalized["q1"]["options_zh"][0], "Phantom Blade")
        self.assertIn("Phantom Blade", translations["q1"]["name_zh"])

    def test_question_and_plan_machine_values_remain_original(self):
        questions = [
            {
                "name_zh": "Phantom Blade 使用情况",
                "options": ["Phantom Blade", "未使用"],
                "column_indexes": [3],
            }
        ]
        plan = {
            "columns": [
                {
                    "index": 3,
                    "name": "Phantom Blade",
                    "options": ["Phantom Blade", "未使用"],
                }
            ],
            "parts": [
                {
                    "name": "Phantom Blade 玩家",
                    "column_indexes": [3],
                    "filter": {
                        "column_index": 3,
                        "allowed_options": ["Phantom Blade"],
                    },
                }
            ],
            "open_questions": ["是否重点分析 Phantom Blade？"],
        }

        with patch.object(
            survey_service,
            "normalize_glossary_terms",
            side_effect=_normalized_text,
        ):
            normalized_questions = survey_service._normalize_question_display_texts(
                questions
            )
            normalized_plan = survey_service._normalize_plan_display_texts(plan)

        self.assertEqual(normalized_questions[0]["name_zh"], "幻影之刃 使用情况")
        self.assertEqual(normalized_questions[0]["options"][0], "Phantom Blade")
        self.assertEqual(normalized_plan["columns"], plan["columns"])
        self.assertEqual(
            normalized_plan["parts"][0]["filter"]["allowed_options"],
            ["Phantom Blade"],
        )
        self.assertEqual(normalized_plan["parts"][0]["name"], "幻影之刃 玩家")


class GlossaryStructuredRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_sample_json_is_normalized_only_after_validation(self):
        raw = {
            "themes": [
                {
                    "id": "t01",
                    "name": "Phantom Blade",
                    "description": "Phantom Blade 使用体验",
                    "representative_quotes": ["Phantom Blade 原始回答"],
                }
            ]
        }
        collect = AsyncMock(return_value=(json.dumps(raw), "theme-model"))
        normalized_inputs = []

        def normalize(value, *, protected_keys=None):
            normalized_inputs.append(value)
            return _normalized_data(value, protected_keys=protected_keys)

        with (
            patch.object(report_engine, "collect_chat_completion", new=collect),
            patch.object(
                report_engine,
                "prepare_glossary_messages",
                side_effect=_prepared,
            ),
            patch.object(
                report_engine,
                "normalize_glossary_data",
                side_effect=normalize,
            ),
        ):
            result = await report_engine._direct_json_call(
                "theme rules",
                "responses mention Phantom Blade",
                models=("theme-model",),
                max_tokens=1024,
                reasoning_effort=None,
                validator=lambda value: None if isinstance(value, dict) else "invalid",
            )

        self.assertIsInstance(normalized_inputs[0], dict)
        self.assertEqual(result["data"]["themes"][0]["id"], "t01")
        self.assertEqual(result["data"]["themes"][0]["name"], "幻影之刃")
        self.assertEqual(
            result["data"]["themes"][0]["representative_quotes"],
            ["Phantom Blade 原始回答"],
        )
        self.assertEqual(
            collect.await_args.args[0][-1]["content"],
            "术语规则：Phantom Blade -> 幻影之刃",
        )

    async def test_comment_common_calls_prepare_messages_and_normalize_outputs(self):
        collect = AsyncMock(
            side_effect=[
                (
                    '[{"idx":0,"text":"Phantom Blade raw",'
                    '"translation":"Phantom Blade"}]',
                    "json-model",
                ),
                ("## 核心结论\nPhantom Blade 反馈集中。", "text-model"),
            ]
        )

        with (
            patch.object(comment_pipeline, "collect_chat_completion", new=collect),
            patch.object(
                comment_pipeline,
                "prepare_glossary_messages",
                side_effect=_prepared,
            ),
            patch.object(
                comment_pipeline,
                "normalize_glossary_data",
                side_effect=_normalized_data,
            ),
            patch.object(
                comment_pipeline,
                "normalize_glossary_terms",
                side_effect=_normalized_text,
            ),
        ):
            structured, _, _ = await comment_pipeline._comment_json_call(
                task="评论翻译",
                system_prompt="json rules",
                query='{"text":"Phantom Blade"}',
                models=("json-model",),
                reasoning_effort="low",
                max_tokens=512,
                validate=lambda value: "" if isinstance(value, list) else "invalid",
            )
            report, _, _ = await comment_pipeline._comment_text_call(
                task="评论简报",
                system_prompt="markdown rules",
                query='{"theme":"Phantom Blade"}',
                models=("text-model",),
                reasoning_effort="medium",
                max_tokens=512,
                validate=lambda value: "" if value.startswith("## 核心结论") else "invalid",
            )

        self.assertEqual(structured[0]["idx"], 0)
        self.assertEqual(structured[0]["text"], "Phantom Blade raw")
        self.assertEqual(structured[0]["translation"], "幻影之刃")
        self.assertIn("幻影之刃", report)
        for call in collect.await_args_list:
            self.assertEqual(
                call.args[0][-1]["content"],
                "术语规则：Phantom Blade -> 幻影之刃",
            )


class GlossaryInterviewRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_interview_prepares_every_stage_but_parses_json_before_normalizing(self):
        raw_json = json.dumps(
            {
                "modules": [
                    {
                        "title": "Phantom Blade",
                        "record_excerpt": "Phantom Blade 原始记录",
                        "source_refs": ["Phantom Blade!A1"],
                    }
                ]
            }
        )
        collect = AsyncMock(
            side_effect=[
                (
                    "## Phantom Blade\n\n### 模块判断\n"
                    "Phantom Blade 体验。[来源：Phantom Blade!A1]",
                    "write-model",
                ),
                (raw_json, "extract-model"),
            ]
        )

        with (
            patch.object(interview_service, "collect_chat_completion", new=collect),
            patch.object(
                interview_service,
                "prepare_glossary_messages",
                side_effect=_prepared,
            ),
            patch.object(
                interview_service,
                "normalize_glossary_terms",
                side_effect=_normalized_text,
            ) as normalize_text,
            patch.object(
                interview_service,
                "normalize_glossary_data",
                side_effect=_normalized_data,
            ),
        ):
            write_results = [
                item
                async for item in interview_service._collect_stage(
                    messages=[{"role": "user", "content": "写 Phantom Blade"}],
                    model="write-model",
                    reasoning="medium",
                    request=_Request(),
                    stage="write",
                    percent=30,
                )
            ]
            extract_results = [
                item
                async for item in interview_service._collect_stage(
                    messages=[{"role": "user", "content": "提取 Phantom Blade"}],
                    model="extract-model",
                    reasoning="medium",
                    request=_Request(),
                    stage="extract",
                    percent=8,
                )
            ]
            extract_text = extract_results[-1][1][0]
            parsed = interview_service._parse_json_object(extract_text, "证据归并")

        self.assertIn("## 幻影之刃", write_results[-1][1][0])
        self.assertIn("幻影之刃 体验", write_results[-1][1][0])
        self.assertIn("[来源：Phantom Blade!A1]", write_results[-1][1][0])
        self.assertIn("Phantom Blade", extract_text)
        self.assertEqual(parsed["modules"][0]["title"], "幻影之刃")
        self.assertEqual(
            parsed["modules"][0]["record_excerpt"],
            "Phantom Blade 原始记录",
        )
        self.assertEqual(
            parsed["modules"][0]["source_refs"],
            ["Phantom Blade!A1"],
        )
        self.assertGreaterEqual(normalize_text.call_count, 1)
        normalized_calls = [call.args[0] for call in normalize_text.call_args_list]
        self.assertNotIn(raw_json, normalized_calls)
        for call in collect.await_args_list:
            self.assertEqual(
                call.args[0][-1]["content"],
                "术语规则：Phantom Blade -> 幻影之刃",
            )


if __name__ == "__main__":
    unittest.main()
