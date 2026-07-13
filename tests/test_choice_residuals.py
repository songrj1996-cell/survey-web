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


if __name__ == "__main__":
    unittest.main()
