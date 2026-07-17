"""题型识别的确定性结构校正规则。"""
import unittest

from app.services.question_detect import (
    _heuristic_type,
    _reconcile_question_roles,
    _sanitize_choice_options,
)


class QuestionDetectTests(unittest.TestCase):
    def test_long_repeated_lists_are_multi_choice_before_open_text(self):
        values = [
            "广告和刷屏消息过多，聊天内容粗俗或恶意挑衅",
            "广告和刷屏消息过多，消息刷新速度太快",
            "聊天内容粗俗或恶意挑衅，消息刷新速度太快",
            "广告和刷屏消息过多，聊天内容粗俗或恶意挑衅",
            "广告和刷屏消息过多，消息刷新速度太快",
        ]

        self.assertEqual(_heuristic_type("使用问题", values), "multi_choice")

    def test_multi_choice_with_other_text_keeps_repeated_options(self):
        values = [
            "广告/刷屏太多，消息刷新太快",
            "广告/刷屏太多，聊天内容粗俗/恶搞",
            "消息刷新太快，聊天内容粗俗/恶搞",
            "广告/刷屏太多，消息刷新太快",
            "其他：希望可以屏蔽陌生人",
        ]
        rows = [["Q"]] + [[value] for value in values]
        questions = [{"role": "open_text", "column_indexes": [0]}]

        question = _reconcile_question_roles(rows, questions)[0]
        question = _sanitize_choice_options(rows, [question])[0]

        self.assertEqual(question["role"], "multi_choice")
        self.assertTrue(question["low_confidence"])
        self.assertEqual(
            question["options"],
            ["广告/刷屏太多", "消息刷新太快", "聊天内容粗俗/恶搞"],
        )
        self.assertEqual(question["unmatched_values"][0]["value"], "其他：希望可以屏蔽陌生人")

    def test_single_choice_with_rare_other_text_is_reconciled(self):
        values = ["经常使用", "偶尔使用", "经常使用", "偶尔使用", "经常使用", "其他：活动时才用"]
        rows = [["Q"]] + [[value] for value in values]
        questions = [{"role": "open_text", "column_indexes": [0]}]

        question = _reconcile_question_roles(rows, questions)[0]
        question = _sanitize_choice_options(rows, [question])[0]

        self.assertEqual(question["role"], "single_choice")
        self.assertEqual(question["options"], ["经常使用", "偶尔使用"])
        self.assertEqual(question["unmatched_values"][0]["value"], "其他：活动时才用")

    def test_open_text_with_comma_lists_stays_open_text_without_repetition(self):
        values = [
            "我希望改善匹配节奏、优化举报流程并减少加载等待时间",
            "建议增加频道筛选、完善陌生人屏蔽并缩短消息冷却时间",
            "希望修复通知遗漏、增加历史记录并改善弱网情况下的加载表现",
            "建议优化新手引导、调整入口层级并提供更清晰的状态提示",
            "希望增加个性化设置、改善表情管理并降低页面切换时的卡顿",
        ]
        rows = [["Q"]] + [[value] for value in values]
        questions = [{"role": "open_text", "column_indexes": [0]}]

        question = _reconcile_question_roles(rows, questions)[0]

        self.assertEqual(question["role"], "open_text")


if __name__ == "__main__":
    unittest.main()
