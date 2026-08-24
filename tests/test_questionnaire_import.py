"""倍市得原问卷与可读回收数据确定性匹配测试。"""

import io
import json
import unittest
from unittest.mock import patch

import openpyxl
from fastapi import HTTPException

from app.services.questionnaire_import import (
    apply_questionnaire_translations,
    build_questionnaire_translation_query,
    parse_bested_qualitative_upload,
    parse_questionnaire_translations,
)
from app.services.survey_service import handle_survey_upload
from survey_plan import expand_confirmed_to_columns
from survey_stats import compute


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


def _coded_questionnaire_bytes() -> bytes:
    return _workbook_bytes({
        "问卷内容": [
            ["题号", "题目"],
            ["Q1[描述题]", "调研说明"],
            ["Q2[多选题]", "常用操作"],
            ["选项", ""],
            ["10", "忽略消息"],
            ["20", "举报账号"],
            ["Q3[单选题]", "当前状态"],
            ["选项", ""],
            ["3", "未绑定"],
            ["7", "已绑定"],
            ["Q4[填空题]", "补充建议"],
            ["Q5[描述题]", "下一部分说明"],
            ["Q6[多选题]", "判断依据"],
            ["选项", ""],
            ["4", "查看设置"],
            ["9", "收到通知"],
            ["Q7[填空题]", "结束语"],
        ],
    })


def _coded_response_bytes(
    *,
    first_binary_value: str = "1",
    alias_option_count: int = 2,
    second_multi_header: str = "Q2__20",
    single_choice_value: str = "7",
) -> bytes:
    alias_options = [
        ["", "4.Checking settings"],
        ["", "9.Receiving a notification"],
    ][:alias_option_count]
    return _workbook_bytes({
        "data": [
            [
                "role_id",
                "Q2__10",
                second_multi_header,
                "Q2__20__open",
                "Q3",
                "Q4__open",
                "Q30__4",
                "Q30__9",
                "Q7__open",
            ],
            ["P01", first_binary_value, "0", "", single_choice_value, "建议一", "0", "1", "完成"],
            ["P02", "1", "1", "补充内容", "3", "建议二", "1", "0", "完成"],
        ],
        "code": [
            ["1", "Q2.Common actions"],
            ["", "option"],
            ["", "10.Ignore the message"],
            ["", "20.Report the account"],
            ["2", "Q3.Current status"],
            ["", "option"],
            ["", "3.Not connected"],
            ["", "7.Connected"],
            ["3", "Q4.Additional suggestion"],
            ["4", "Q30.How do you determine the status?"],
            ["", "option"],
            *alias_options,
            ["5", "Q7.Ending"],
        ],
    })


def _multilingual_readable_questionnaire_bytes() -> bytes:
    return _workbook_bytes({
        "问卷内容": [
            ["题号", "题目"],
            ["Q1[单选题]", "当前状态"],
            ["选项", ""],
            ["3", "未绑定"],
            ["7", "已绑定"],
            ["Q2[多选题]", "常用操作"],
            ["选项", ""],
            ["10", "忽略消息"],
            ["20", "举报账号"],
            ["Q3[多选题]", "了解渠道"],
            ["选项", ""],
            ["1", "游戏内公告"],
            ["2", "好友推荐"],
            ["Q4[矩阵单选题]", "功能评价"],
            ["选项", ""],
            ["1", "满意"],
            ["2", "不满意"],
            ["矩阵行", ""],
            ["1", "易用性"],
            ["2", "稳定性"],
            ["Q5[填空题]", "补充建议"],
        ],
    })


def _multilingual_readable_response_bytes() -> bytes:
    return _workbook_bytes({
        "data": [
            [
                "Current status",
                "Common actions__Ignore message",
                "Common actions__Report account",
                "Discovery channels",
                "Feature rating__Usability",
                "Feature rating__Stability",
                "Additional comments",
            ],
            [
                "Connected",
                "Ignore message",
                "",
                "In-game notice,Friend recommendation",
                "Satisfied",
                "Dissatisfied",
                "Keep this player response in English.",
            ],
            [
                "Not connected",
                "Ignore message",
                "Report account",
                "Friend recommendation",
                "Dissatisfied",
                "Satisfied",
                "",
            ],
        ],
        "code": [
            ["1", "Q1.Current status"],
            ["", "option"],
            ["", "3.Not connected"],
            ["", "7.Connected"],
            ["2", "Q2.Common actions"],
            ["", "option"],
            ["", "10.Ignore message"],
            ["", "20.Report account"],
            ["3", "Q3.Discovery channels"],
            ["", "option"],
            ["", "1.In-game notice"],
            ["", "2.Friend recommendation"],
            ["4", "Q4.Feature rating"],
            ["", "option"],
            ["", "1.Satisfied"],
            ["", "2.Dissatisfied"],
            ["", "subquestion"],
            ["", "1.Usability"],
            ["", "2.Stability"],
            ["5", "Q5.Additional comments"],
            ["", "option"],
            ["", "1.open"],
        ],
    })


class BestedQuestionnaireImportTests(unittest.TestCase):
    def test_description_question_without_response_column_does_not_break_qid_mapping(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[描述题]", "本次调研主要了解账号安全认知"],
                ["Q2[多选题]", "收到可疑消息后会怎么做"],
                ["选项", ""],
                ["1", "忽略消息"],
                ["2", "举报账号"],
            ],
        })
        response = _workbook_bytes({
            "data": [
                ["收到可疑消息后会怎么做__忽略消息", "收到可疑消息后会怎么做__举报账号"],
                ["忽略消息", ""],
                ["", "举报账号"],
            ],
            "code": [["2", "Q2.收到可疑消息后会怎么做"]],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["matched_questions"], 1)
        self.assertEqual(imported["rows"][0], ["收到可疑消息后会怎么做"])
        self.assertEqual(imported["questions"][0]["source_question_id"], "Q2")
        self.assertEqual(imported["questions"][0]["role"], "multi_choice")
        self.assertIn("Q1[描述题]", imported["questionnaire_text"])

    def test_description_question_in_code_still_does_not_require_a_data_column(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[描述题]", "本次调研主要了解账号安全认知"],
                ["Q2[单选题]", "是否收到过可疑消息"],
                ["选项", ""],
                ["1", "是"],
                ["2", "否"],
            ],
        })
        response = _workbook_bytes({
            "data": [["是否收到过可疑消息"], ["是"], ["否"]],
            "code": [
                ["1", "Q1.本次调研主要了解账号安全认知"],
                ["2", "Q2.是否收到过可疑消息"],
            ],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["rows"][0], ["是否收到过可疑消息"])
        self.assertEqual(len(imported["questions"]), 1)
        self.assertEqual(imported["questions"][0]["source_question_id"], "Q2")

    def test_coded_multilingual_export_decodes_values_and_keeps_source_qids(self):
        imported = parse_bested_qualitative_upload(
            _coded_response_bytes(),
            _coded_questionnaire_bytes(),
        )

        self.assertEqual(imported["matched_questions"], 5)
        self.assertEqual(imported["rows"][0], [
            "常用操作",
            "当前状态",
            "补充建议",
            "判断依据",
            "结束语",
            "role_id",
            "Q2__20__open",
        ])
        self.assertEqual(imported["rows"][1][:4], [
            "忽略消息",
            "已绑定",
            "建议一",
            "收到通知",
        ])
        self.assertEqual(imported["rows"][2][:4], [
            "忽略消息\n举报账号",
            "未绑定",
            "建议二",
            "查看设置",
        ])
        source_ids = [
            question.get("source_question_id")
            for question in imported["questions"][:4]
        ]
        self.assertEqual(source_ids, ["Q2", "Q3", "Q4", "Q6"])
        self.assertNotIn("Q30", source_ids)
        self.assertIn("Q1[描述题]", imported["questionnaire_text"])
        self.assertIn("Q5[描述题]", imported["questionnaire_text"])
        self.assertEqual(imported["questions"][5]["role"], "id")
        self.assertEqual(imported["questions"][6]["role"], "ignore")

    def test_whatsapp_mention_is_not_treated_as_a_contact_field(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[多选题]", "收到消息要求添加WhatsApp时，哪些做法合适？"],
                ["选项", ""],
                ["1", "忽略消息"],
                ["2", "举报账号"],
                ["Q2[多选题]", "请留下您最常用的1-2个联系方式"],
                ["选项", ""],
                ["1", "WhatsApp"],
                ["2", "手机号"],
            ],
        })
        response = _workbook_bytes({
            "data": [
                [
                    "收到消息要求添加WhatsApp时，哪些做法合适？__忽略消息",
                    "收到消息要求添加WhatsApp时，哪些做法合适？__举报账号",
                    "请留下您最常用的1-2个联系方式__WhatsApp",
                    "请留下您最常用的1-2个联系方式__手机号",
                ],
                ["忽略消息", "", "WhatsApp", ""],
            ],
            "code": [
                ["1", "Q1.收到消息要求添加WhatsApp时，哪些做法合适？"],
                ["2", "Q2.请留下您最常用的1-2个联系方式"],
            ],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["questions"][0]["role"], "multi_choice")
        self.assertEqual(imported["questions"][1]["role"], "ignore")

    def test_direct_whatsapp_fields_remain_ignored(self):
        titles = [
            "WhatsApp",
            "Your WhatsApp ID",
            "WhatsApp number",
            "Please provide your WhatsApp",
        ]
        for title in titles:
            with self.subTest(title=title):
                questionnaire = _workbook_bytes({
                    "问卷内容": [
                        ["题号", "题目"],
                        ["Q1[填空题]", title],
                    ],
                })
                response = _workbook_bytes({
                    "data": [[title], ["test-contact"]],
                    "code": [["1", f"Q1.{title}"]],
                })

                imported = parse_bested_qualitative_upload(
                    response,
                    questionnaire,
                )

                self.assertEqual(imported["questions"][0]["role"], "ignore")

    def test_coded_qid_alias_requires_matching_option_codes(self):
        with self.assertRaisesRegex(ValueError, "选项编码不一致"):
            parse_bested_qualitative_upload(
                _coded_response_bytes(alias_option_count=1),
                _coded_questionnaire_bytes(),
            )

    def test_coded_single_question_qid_mismatch_requires_neighbors(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[单选题]", "当前状态"],
                ["选项", ""],
                ["1", "未绑定"],
                ["2", "已绑定"],
            ],
        })
        response = _workbook_bytes({
            "data": [["Q99"], ["1"]],
            "code": [
                ["1", "Q99.Current status"],
                ["", "option"],
                ["", "1.Not connected"],
                ["", "2.Connected"],
            ],
        })

        with self.assertRaisesRegex(ValueError, "题号错位缺少相邻题号校验"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_open_text_rejects_choice_code_structure(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[填空题]", "补充建议"],
            ],
        })
        response = _workbook_bytes({
            "data": [["Q1"], ["1"]],
            "code": [
                ["1", "Q1.Gender"],
                ["", "option"],
                ["", "1.Male"],
                ["", "2.Female"],
            ],
        })

        with self.assertRaisesRegex(ValueError, "选项结构与原问卷填空题不一致"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_choice_rejects_open_text_code_structure(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[单选题]", "当前状态"],
                ["选项", ""],
                ["1", "已完成"],
            ],
        })
        response = _workbook_bytes({
            "data": [["Q1"], ["1"]],
            "code": [
                ["1", "Q1.Additional comments"],
                ["", "option"],
                ["", "1.open"],
            ],
        })

        with self.assertRaisesRegex(ValueError, "填空题标记与原问卷题型不一致"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_coded_values_reach_statistics_as_questionnaire_options(self):
        imported = parse_bested_qualitative_upload(
            _coded_response_bytes(),
            _coded_questionnaire_bytes(),
        )
        columns = expand_confirmed_to_columns(imported["questions"])
        analyzed_indexes = [
            column["index"] for column in columns
            if column["role"] not in {"id", "ignore"}
        ]
        plan = {
            "columns": columns,
            "parts": [{
                "name": "编码导出验证",
                "column_indexes": analyzed_indexes,
            }],
        }

        stats, open_text = compute(imported["rows"], plan)

        self.assertIn("| 忽略消息 | 2 | 100.0% |", stats)
        self.assertIn("| 举报账号 | 1 | 50.0% |", stats)
        self.assertIn("| 已绑定 | 1 | 50.0% |", stats)
        self.assertIn("| 收到通知 | 1 | 50.0% |", stats)
        self.assertNotIn("Q30", stats)
        self.assertEqual(len(open_text[2]), 2)

    def test_coded_option_order_maps_by_stable_codes(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[单选题]", "偏好颜色"],
                ["选项", ""],
                ["1", "红色"],
                ["2", "蓝色"],
                ["Q2[多选题]", "偏好模式"],
                ["选项", ""],
                ["1", "排位"],
                ["2", "经典"],
            ],
        })
        response = _workbook_bytes({
            "data": [["Q1", "Q2__2", "Q2__1"], ["2", "1", "0"]],
            "code": [
                ["1", "Q1.Preferred color"],
                ["", "option"],
                ["", "2.Blue"],
                ["", "1.Red"],
                ["2", "Q2.Preferred mode"],
                ["", "option"],
                ["", "2.Classic"],
                ["", "1.Ranked"],
            ],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["rows"][1], ["蓝色", "经典"])

    def test_readable_english_export_uses_chinese_questionnaire_text(self):
        imported = parse_bested_qualitative_upload(
            _multilingual_readable_response_bytes(),
            _multilingual_readable_questionnaire_bytes(),
        )

        self.assertEqual(imported["rows"][0], [
            "当前状态",
            "常用操作",
            "了解渠道",
            "功能评价 [易用性]",
            "功能评价 [稳定性]",
            "补充建议",
        ])
        self.assertEqual(imported["rows"][1], [
            "已绑定",
            "忽略消息",
            "游戏内公告\n好友推荐",
            "满意",
            "不满意",
            "Keep this player response in English.",
        ])
        self.assertEqual(imported["rows"][2][:5], [
            "未绑定",
            "忽略消息\n举报账号",
            "好友推荐",
            "不满意",
            "满意",
        ])
        self.assertEqual(
            [question["name_zh"] for question in imported["questions"]],
            ["当前状态", "常用操作", "了解渠道", "功能评价", "补充建议"],
        )

        columns = expand_confirmed_to_columns(imported["questions"])
        plan = {
            "columns": columns,
            "parts": [{
                "name": "中文问卷权威文本验证",
                "column_indexes": [column["index"] for column in columns],
            }],
        }
        stats, open_text = compute(imported["rows"], plan)

        self.assertIn("| 已绑定 | 1 | 50.0% |", stats)
        self.assertIn("| 忽略消息 | 2 | 100.0% |", stats)
        self.assertIn("| 游戏内公告 | 1 | 50.0% |", stats)
        self.assertIn("满意", stats)
        self.assertNotIn("| Connected |", stats)
        self.assertNotIn("| Ignore message |", stats)
        self.assertNotIn("Satisfied", stats)
        self.assertEqual(
            open_text[5][0]["text"],
            "Keep this player response in English.",
        )

    def test_readable_same_language_title_mismatch_still_fails(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[填空题]", "补充建议"],
            ],
        })
        response = _workbook_bytes({
            "data": [["其他问题"], ["回答"]],
            "code": [["1", "Q1.其他问题"]],
        })

        with self.assertRaisesRegex(ValueError, "题干在原问卷与回答文件中不一致"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_readable_english_question_with_chinese_name_is_not_a_chinese_questionnaire(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[填空题]", "How familiar are you with 中国 server settings?"],
            ],
        })
        response = _workbook_bytes({
            "data": [["A different English question"], ["answer"]],
            "code": [["1", "Q1.A different English question"]],
        })

        with self.assertRaisesRegex(ValueError, "题干在原问卷与回答文件中不一致"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_readable_english_title_may_contain_a_chinese_product_name(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[填空题]", "你是否使用微信？"],
            ],
        })
        response = _workbook_bytes({
            "data": [["Do you use 微信?"], ["Yes"]],
            "code": [["1", "Q1.Do you use 微信?"]],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["rows"], [["你是否使用微信？"], ["Yes"]])

    def test_readable_code_order_maps_by_stable_codes(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[矩阵单选题]", "功能评价"],
                ["选项", ""],
                ["1", "红色"],
                ["2", "蓝色"],
                ["矩阵行", ""],
                ["1", "易用性"],
                ["2", "稳定性"],
            ],
        })
        response = _workbook_bytes({
            "data": [
                ["Feature rating__Stability", "Feature rating__Usability"],
                ["Blue", "Red"],
            ],
            "code": [
                ["1", "Q1.Feature rating"],
                ["", "option"],
                ["", "2.Blue"],
                ["", "1.Red"],
                ["", "subquestion"],
                ["", "2.Stability"],
                ["", "1.Usability"],
            ],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["rows"][0], [
            "功能评价 [易用性]",
            "功能评价 [稳定性]",
        ])
        self.assertEqual(imported["rows"][1], ["红色", "蓝色"])

    def test_readable_unmapped_single_choice_value_fails(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[单选题]", "MLBB ID"],
                ["选项", ""],
                ["1", "是"],
                ["2", "否"],
                ["Q2[填空题]", "补充建议"],
            ],
        })
        response = _workbook_bytes({
            "data": [["MLBB ID", "补充建议"], ["Yes", "无"]],
            "code": [
                ["1", "Q1.MLBB ID"],
                ["2", "Q2.补充建议"],
            ],
        })

        with self.assertRaisesRegex(ValueError, "无法映射到原问卷的选项"):
            parse_bested_qualitative_upload(response, questionnaire)

    def test_coded_multi_choice_rejects_non_binary_values(self):
        with self.assertRaisesRegex(ValueError, "非 0/1 的多选编码"):
            parse_bested_qualitative_upload(
                _coded_response_bytes(first_binary_value="2"),
                _coded_questionnaire_bytes(),
            )

    def test_coded_multi_choice_requires_exact_option_code_columns(self):
        with self.assertRaisesRegex(ValueError, "拆分回答列无法完整匹配"):
            parse_bested_qualitative_upload(
                _coded_response_bytes(second_multi_header="Q2__21"),
                _coded_questionnaire_bytes(),
            )

    def test_coded_single_choice_rejects_unknown_option_code(self):
        with self.assertRaisesRegex(ValueError, "未知选项编码"):
            parse_bested_qualitative_upload(
                _coded_response_bytes(single_choice_value="99"),
                _coded_questionnaire_bytes(),
            )

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

    def test_readable_matrix_code_subquestions_are_not_parsed_as_options(self):
        questionnaire = _workbook_bytes({
            "问卷内容": [
                ["题号", "题目"],
                ["Q1[矩阵单选题]", "功能评价"],
                ["选项", ""],
                ["1", "满意"],
                ["2", "不满意"],
                ["矩阵行", ""],
                ["1", "易用性"],
                ["2", "稳定性"],
            ],
        })
        response = _workbook_bytes({
            "data": [
                ["功能评价__易用性", "功能评价__稳定性"],
                ["满意", "不满意"],
            ],
            "code": [
                ["1", "Q1.功能评价"],
                ["", "option"],
                ["", "1.满意"],
                ["", "2.不满意"],
                ["", "subquestion"],
                ["", "1.易用性"],
                ["", "2.稳定性"],
            ],
        })

        imported = parse_bested_qualitative_upload(response, questionnaire)

        self.assertEqual(imported["matched_questions"], 1)
        self.assertEqual(imported["questions"][0]["role"], "matrix_single")
        self.assertEqual(imported["questions"][0]["options"], ["满意", "不满意"])
        self.assertEqual(imported["questions"][0]["rows"], ["易用性", "稳定性"])

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
