"""选择题未匹配值与 Google Form Other 的回归测试。"""
import unittest

from app.services.question_detect import _sanitize_choice_options
from survey_stats import compute


def _sanitize(values: list[str], options: list[str]) -> dict:
    rows = [["Q"]] + [[value] for value in values]
    questions = [{
        "name_zh": "Q",
        "role": "single_choice",
        "column_indexes": [0],
        "options": options,
    }]
    return _sanitize_choice_options(rows, questions)[0]


class ChoiceResidualTests(unittest.TestCase):
    def test_numbered_tail_value_stays_out_of_other(self):
        question = _sanitize(
            ["1~5"] * 4 + ["6~10"] * 3 + ["11~15"] * 2 + ["16~20", "More than 25"],
            ["1~5", "6~10", "11~15", "16~20"],
        )

        self.assertEqual(question["options"], ["1~5", "6~10", "11~15", "16~20"])
        self.assertNotIn("other_text", question)
        self.assertEqual(question["unmatched_values"], [{
            "value": "More than 25",
            "count": 1,
            "suggested_handling": "standard_option",
        }])

    def test_free_text_is_left_for_one_question_level_decision(self):
        question = _sanitize(
            ["排位"] * 4 + ["经典"] * 3 + ["乱斗"] * 2 + ["希望加入单挑模式", "希望新增剧情模式"],
            ["排位", "经典", "乱斗"],
        )

        self.assertNotIn("other_text", question)
        self.assertEqual(
            [item["suggested_handling"] for item in question["unmatched_values"]],
            ["review", "review"],
        )

    def test_confirmed_other_is_the_only_case_aggregated_in_stats(self):
        rows = [["Q"], ["排位"], ["经典"], ["希望加入单挑模式"]]
        base_column = {
            "index": 0,
            "role": "single_choice",
            "name": "Q",
            "options": ["排位", "经典"],
        }
        plan = {"columns": [base_column], "parts": [{"name": "测试", "column_indexes": [0]}]}
        stats, open_text = compute(rows, plan)
        self.assertIn("希望加入单挑模式", stats)
        self.assertNotIn("Other / 其他", stats)
        self.assertEqual(open_text, {})

        other_column = {
            **base_column,
            "options": ["排位", "经典", "Other / 其他"],
            "other_text": {"enabled": True, "option": "Other / 其他"},
        }
        stats, open_text = compute(rows, {"columns": [other_column], "parts": plan["parts"]})
        self.assertIn("Other / 其他", stats)
        self.assertEqual(open_text[0][0]["text"], "希望加入单挑模式")

    def test_explicit_other_values_do_not_absorb_kept_raw_or_standard_options(self):
        rows = [
            ["Q"],
            ["排位"],
            ["这是一个较长但正式的标准选项"],
            ["暂时说不清"],
            ["希望加入单挑模式"],
        ]
        column = {
            "index": 0,
            "role": "single_choice",
            "name": "Q",
            "options": ["排位", "这是一个较长但正式的标准选项", "Other / 其他"],
            "other_text": {
                "enabled": True,
                "option": "Other / 其他",
                "values": ["希望加入单挑模式"],
            },
        }
        plan = {"columns": [column], "parts": [{"name": "测试", "column_indexes": [0]}]}

        stats, open_text = compute(rows, plan)

        self.assertIn("这是一个较长但正式的标准选项", stats)
        self.assertIn("暂时说不清", stats)
        self.assertIn("Other / 其他", stats)
        self.assertNotIn("希望加入单挑模式", stats)
        self.assertEqual([entry["text"] for entry in open_text[0]], ["希望加入单挑模式"])

    def test_explicit_other_values_are_respected_for_multi_choice(self):
        rows = [
            ["Q"],
            ["排位，经典"],
            ["排位，暂时说不清"],
            ["经典，希望加入单挑模式"],
        ]
        column = {
            "index": 0,
            "role": "multi_choice",
            "name": "Q",
            "delimiter": "，",
            "options": ["排位", "经典", "Other / 其他"],
            "other_text": {
                "enabled": True,
                "option": "Other / 其他",
                "values": ["希望加入单挑模式"],
            },
        }
        plan = {"columns": [column], "parts": [{"name": "测试", "column_indexes": [0]}]}

        stats, open_text = compute(rows, plan)

        self.assertIn("暂时说不清", stats)
        self.assertIn("Other / 其他", stats)
        self.assertNotIn("希望加入单挑模式", stats)
        self.assertEqual([entry["text"] for entry in open_text[0]], ["希望加入单挑模式"])

    def test_multi_choice_other_text_with_commas_stays_intact(self):
        other_text = "I'm more of an observer, and people often look for a partner, which does not interest me."
        rows = [["Q"], [f"Ranked,{other_text}"]]
        column = {
            "index": 0,
            "role": "multi_choice",
            "name": "Q",
            "delimiter": ",",
            "options": ["Ranked", "Other / 其他"],
            "other_text": {"enabled": True, "option": "Other / 其他"},
        }
        plan = {"columns": [column], "parts": [{"name": "测试", "column_indexes": [0]}]}

        stats, open_text = compute(rows, plan)

        self.assertIn("Ranked", stats)
        self.assertIn("Other / 其他", stats)
        self.assertEqual([entry["text"] for entry in open_text[0]], [other_text])

    def test_final_confirmed_option_is_matched_before_bulk_other(self):
        long_option = "A long official option, with punctuation"
        other_text = "A free-form explanation, with another comma"
        rows = [["Q"], [f"Ranked,{long_option}"], [f"Ranked,{other_text}"]]
        column = {
            "index": 0,
            "role": "multi_choice",
            "name": "Q",
            "delimiter": ",",
            "options": ["Ranked", long_option, "Other / 其他"],
            "other_text": {"enabled": True, "option": "Other / 其他"},
        }
        plan = {"columns": [column], "parts": [{"name": "测试", "column_indexes": [0]}]}

        stats, open_text = compute(rows, plan)

        self.assertIn(long_option, stats)
        self.assertEqual([entry["text"] for entry in open_text[0]], [other_text])

    def test_detection_preview_keeps_unknown_comma_text_together(self):
        other_text = "I'm more of an observer, and people often look for a partner"
        rows = [["Q"], ["Ranked,Classic"], [f"Ranked,{other_text}"]]
        questions = [{
            "name_zh": "Q",
            "role": "multi_choice",
            "column_indexes": [0],
            "delimiter": ",",
            "options": ["Ranked", "Classic"],
        }]

        question = _sanitize_choice_options(rows, questions)[0]

        self.assertEqual(
            [item["value"] for item in question["unmatched_values"]],
            [other_text],
        )


if __name__ == "__main__":
    unittest.main()
