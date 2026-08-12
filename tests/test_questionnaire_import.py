"""倍市得原问卷与可读回收数据确定性匹配测试。"""

import io
import json
import unittest
from unittest.mock import patch

import openpyxl
from fastapi import HTTPException

from app.services.questionnaire_import import (
    BestedQuestionnaireParseResult,
    apply_questionnaire_translations,
    build_questionnaire_translation_query,
    parse_bested_questionnaire,
    parse_bested_qualitative_upload,
    parse_questionnaire_translations,
)
from app.services.survey_service import handle_survey_upload


def _workbook_bytes(sheets: dict[str, list[list]]) -> bytes:
    workbook = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        worksheet = workbook.active if first else workbook.create_sheet()
        first = False
        worksheet.title = name
        for row in rows:
            worksheet.append(row)
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _questionnaire_bytes() -> bytes:
    return _workbook_bytes({
        "问卷内容": [
            ["题号", "题目"],
            ["Q1[多选题]", "常用模式"],
            ["选项", ""],
            ["1", "排位"],
            ["2", "经典"],
            ["Q2[矩阵单选题]", "功能评价"],
            ["选项", ""],
            ["1", "满意"],
            ["2", "一般"],
            ["3", "不满意"],
            ["矩阵行", ""],
            ["1", "易用性"],
            ["2", "稳定性"],
            ["Q3[填空题]", "WhatsApp 联系方式"],
        ],
    })


def _response_bytes(matrix_second_header: str = "功能评价__稳定性") -> bytes:
    return _workbook_bytes({
        "data": [
            [
                "role_id",
                "常用模式__排位",
                "常用模式__经典",
                "功能评价__易用性",
                matrix_second_header,
                "WhatsApp 联系方式",
            ],
            ["P01", "排位", "", "满意", "一般", "123"],
            ["P02", "排位", "经典", "一般", "满意", ""],
        ],
        "code": [
            ["编码", "题目"],
            ["1", "Q1.常用模式"],
            ["2", "Q2.功能评价"],
            ["3", "Q3.WhatsApp 联系方式"],
        ],
    })


class BestedQuestionnaireImportTests(unittest.TestCase):
    def test_public_parser_preserves_provider_rows_and_source_locations(self):
        questionnaire = _workbook_bytes({
            "说明": [["这不是问卷工作表"]],
            "问卷内容": [
                ["题号", "题目"],
                ["Q7[矩阵多选题]", "使用体验"],
                ["选项", ""],
                ["1", "流畅"],
                ["2", "稳定"],
                ["矩阵行", ""],
                ["1", "客户端"],
                ["2", "服务器"],
                ["Q8[填空题]", "其他建议"],
            ],
        })

        parsed = parse_bested_questionnaire(questionnaire)

        self.assertIsInstance(parsed, BestedQuestionnaireParseResult)
        self.assertEqual(parsed.sheet_name, "问卷内容")
        self.assertEqual(parsed.provider_rows[0], ("题号", "题目"))
        self.assertIn("Q7[矩阵多选题] | 使用体验", parsed.questionnaire_text)

        matrix, open_text = parsed.questions
        self.assertEqual(
            (
                matrix.qid,
                matrix.source_type,
                matrix.role,
                matrix.title,
            ),
            (7, "矩阵多选题", "matrix_multi", "使用体验"),
        )
        self.assertEqual(matrix.options, ("流畅", "稳定"))
        self.assertEqual(matrix.rows, ("客户端", "服务器"))
        self.assertEqual(matrix.sheet_name, "问卷内容")
        self.assertEqual(matrix.source_row, 2)
        self.assertEqual(matrix.source_cell, "A2")
        self.assertEqual(matrix.raw_heading, "Q7[矩阵多选题]")
        self.assertEqual(matrix.raw_rows[0], ("Q7[矩阵多选题]", "使用体验"))
        self.assertEqual(matrix.raw_rows[-1], ("2", "服务器"))
        self.assertEqual(open_text.source_row, 9)
        self.assertEqual(open_text.source_cell, "A9")

    def test_questionnaire_types_and_split_columns_are_mapped_without_ai(self):
        imported = parse_bested_qualitative_upload(
            _response_bytes(),
            _questionnaire_bytes(),
        )

        self.assertEqual(imported["matched_questions"], 3)
        self.assertEqual(imported["rows"][0], [
            "常用模式",
            "功能评价 [易用性]",
            "功能评价 [稳定性]",
            "WhatsApp 联系方式",
            "role_id",
        ])
        self.assertEqual(imported["rows"][1][0], "排位")
        self.assertEqual(imported["rows"][2][0], "排位\n经典")

        multi, matrix, contact, respondent_id = imported["questions"]
        self.assertEqual(multi["role"], "multi_choice")
        self.assertEqual(multi["options"], ["排位", "经典"])
        self.assertEqual(matrix["role"], "matrix_single")
        self.assertEqual(matrix["rows"], ["易用性", "稳定性"])
        self.assertEqual(matrix["options"], ["满意", "一般", "不满意"])
        self.assertEqual(matrix["column_indexes"], [1, 2])
        self.assertEqual(contact["role"], "ignore")
        self.assertEqual(respondent_id["role"], "id")

    def test_qualitative_upload_keeps_legacy_return_protocol(self):
        with patch(
            "app.integrations.bested_questionnaire_client."
            "_discover_questionnaire_media",
            side_effect=AssertionError("legacy path must not inspect media"),
        ):
            imported = parse_bested_qualitative_upload(
                _response_bytes(),
                _questionnaire_bytes(),
            )

        self.assertEqual(
            set(imported),
            {"rows", "questions", "questionnaire_text", "matched_questions"},
        )
        self.assertIsInstance(imported["rows"], list)
        self.assertIsInstance(imported["questions"], list)
        self.assertIsInstance(imported["questionnaire_text"], str)
        self.assertEqual(imported["matched_questions"], 3)

    def test_same_column_multi_choice_matches_full_options_with_commas(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[多选题]", "常用模式"],
                ["选项", ""],
                ["1", "排位"],
                ["2", "经典, 娱乐"],
            ],
        })
        response = _workbook_bytes({
            "data": [
                ["常用模式"],
                ["排位"],
                ["排位,经典, 娱乐"],
            ],
            "code": [["1", "Q1.常用模式"]],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["rows"][1][0], "排位")
        self.assertEqual(imported["rows"][2][0], "排位\n经典, 娱乐")
        self.assertEqual(imported["questions"][0]["delimiter"], "\n")
        self.assertEqual(
            imported["questions"][0]["options"],
            ["排位", "经典, 娱乐"],
        )

    def test_unknown_same_column_multi_choice_value_blocks_import(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[多选题]", "常用模式"],
                ["选项", ""],
                ["1", "排位"],
                ["2", "经典"],
            ],
        })
        response = _workbook_bytes({
            "data": [["常用模式"], ["排位,未知模式"]],
            "code": [["1", "Q1.常用模式"]],
        })

        with self.assertRaisesRegex(ValueError, "同列多选答案"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_incomplete_matrix_match_blocks_instead_of_falling_back(self):
        with self.assertRaisesRegex(ValueError, "无法完整匹配"):
            parse_bested_qualitative_upload(
                _response_bytes("功能评价__速度"),
                _questionnaire_bytes(),
            )

    def test_translation_changes_only_text_and_keeps_original_value_aliases(self):
        questions = [{
            "source_question_id": "Q5",
            "name_zh": "How familiar are you with the following?",
            "role": "matrix_single",
            "column_indexes": [4, 5],
            "rows": ["The Mist", "Northern Vale"],
            "options": ["Very familiar", "Never heard of it"],
            "options_original": ["Very familiar", "Never heard of it"],
        }]
        answer = json.dumps({
            "translations": [{
                "question_id": "Q5",
                "name_zh": "您对以下内容的熟悉程度如何？",
                "options_zh": ["非常熟悉", "从未听说过"],
                "rows_zh": ["迷雾之地", "北境山谷"],
                "role": "open_text",
                "column_indexes": [99],
            }],
        }, ensure_ascii=False)

        translations = parse_questionnaire_translations(answer, questions)
        translated = apply_questionnaire_translations(
            questions, translations,
        )[0]

        self.assertEqual(translated["role"], "matrix_single")
        self.assertEqual(translated["column_indexes"], [4, 5])
        self.assertEqual(translated["name_zh"], "您对以下内容的熟悉程度如何？")
        self.assertEqual(translated["rows"], ["迷雾之地", "北境山谷"])
        self.assertEqual(translated["options"], ["非常熟悉", "从未听说过"])
        self.assertEqual(
            translated["value_aliases"],
            {
                "非常熟悉": ["Very familiar"],
                "从未听说过": ["Never heard of it"],
            },
        )

    def test_translation_rejects_changed_matrix_shape(self):
        questions = [{
            "source_question_id": "Q5",
            "name_zh": "How familiar are you?",
            "role": "matrix_single",
            "column_indexes": [0, 1],
            "rows": ["A", "B"],
            "options": ["Familiar", "Unknown"],
        }]
        answer = json.dumps({
            "translations": [{
                "question_id": "Q5",
                "name_zh": "您的熟悉程度如何？",
                "options_zh": ["熟悉", "不了解"],
                "rows_zh": ["项目A"],
            }],
        }, ensure_ascii=False)

        with self.assertRaisesRegex(ValueError, "rows_zh 数量"):
            parse_questionnaire_translations(answer, questions)

    def test_translation_query_does_not_expose_role_or_column_indexes(self):
        questions = [{
            "source_question_id": "Q2",
            "name_zh": "Feature rating",
            "role": "matrix_single",
            "column_indexes": [3, 4],
            "rows": ["Usability", "Stability"],
            "options": ["Good", "Bad"],
        }]

        query = build_questionnaire_translation_query(questions)

        self.assertNotIn("matrix_single", query)
        self.assertNotIn("column_indexes", query)
        self.assertIn('"question_id": "Q2"', query)


class BestedUploadServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_questionnaire_upload_stores_deterministic_columns_provider(self):
        session = {}
        with (
            patch("app.services.survey_service.new_session", return_value="sid"),
            patch("app.services.survey_service.get_session", return_value=session),
            patch("app.services.survey_service.save_session"),
            patch("app.services.survey_service._assign_session_owner"),
        ):
            result = await handle_survey_upload(
                "response.xlsx",
                _response_bytes(),
                None,
                source_type="bested",
                questionnaire_filename="questionnaire.xls",
                questionnaire_content=_questionnaire_bytes(),
            )

        self.assertTrue(result["questionnaire_used"])
        self.assertEqual(result["matched_questions"], 3)
        self.assertEqual(session["column_provider"], "questionnaire")
        self.assertEqual(session["columns_detected"][1]["role"], "matrix_single")

    async def test_mismatched_questionnaire_returns_400_without_ai_fallback(self):
        with self.assertRaises(HTTPException) as caught:
            await handle_survey_upload(
                "response.xlsx",
                _response_bytes("功能评价__速度"),
                None,
                source_type="bested",
                questionnaire_filename="questionnaire.xls",
                questionnaire_content=_questionnaire_bytes(),
            )

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("调研问卷匹配失败", caught.exception.detail)
