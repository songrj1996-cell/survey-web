import unittest

from app.services.insight_report_v3.frequency import (
    CROSS_QUESTION_FREQUENCY_LABEL,
    FREQUENCY_TIER_RULES,
    is_frequency_tier_available,
    minimum_representative_count,
    minimum_support_count,
    model_frequency_rules,
    validate_frequency,
)


class InsightReportV3FrequencyTests(unittest.TestCase):
    def test_approved_tier_table_and_rounding(self):
        self.assertEqual(
            set(FREQUENCY_TIER_RULES),
            {
                "绝大多数", "普遍", "大量", "很多", "不少", "多名",
                "部分", "少数", "个别", "有玩家", "有受访者",
            },
        )
        self.assertEqual(minimum_support_count("普遍", 5), 3)
        self.assertEqual(minimum_support_count("大量", 95), 19)
        self.assertEqual(minimum_support_count("不少", 95), 5)
        self.assertEqual(minimum_representative_count("不少"), 3)
        self.assertEqual(minimum_representative_count("部分"), 3)

    def test_frequency_uses_unique_representatives_and_population_ceiling(self):
        self.assertIsNone(validate_frequency("不少", 5, 3, 20))
        self.assertIn("超过", validate_frequency("部分", 11, 2, 10))
        self.assertIn("代表样本", validate_frequency("不少", 5, 2, 20))
        self.assertIn("档位下界", validate_frequency("大量", 18, 5, 95))
        self.assertIn("代表样本", validate_frequency("部分", 3, 2, 20))

    def test_cross_question_wording_is_fixed_and_non_numeric(self):
        self.assertEqual(CROSS_QUESTION_FREQUENCY_LABEL, "多道题均有提及")

    def test_model_rules_mark_colliding_stronger_tier_unavailable(self):
        rules = "\n".join(model_frequency_rules(10))
        self.assertIn("绝大多数 / 普遍：本题不可用", rules)
        self.assertIn("大量 / 很多：support_count >= 6", rules)
        self.assertIn("不少 / 多名：support_count >= 5", rules)
        self.assertIn("部分：support_count >= 3，response_ids 至少 3 个", rules)
        self.assertIn("不可用", validate_frequency("绝大多数", 5, 5, 10))

    def test_available_tier_bounds_are_strictly_monotonic_across_sample_sizes(self):
        weak_to_strong = (
            "有玩家", "少数", "部分", "不少", "大量", "绝大多数",
        )
        for total in (10, 20, 40, 60, 95, 172):
            with self.subTest(respondent_total=total):
                available = [
                    tier for tier in weak_to_strong
                    if is_frequency_tier_available(tier, total)
                ]
                support_bounds = [
                    minimum_support_count(tier, total) for tier in available
                ]
                self.assertTrue(all(
                    lower < higher
                    for lower, higher in zip(support_bounds, support_bounds[1:])
                ))
                contracts = [
                    (
                        minimum_support_count(tier, total),
                        minimum_representative_count(tier),
                    )
                    for tier in available
                ]
                self.assertTrue(all(
                    lower != higher
                    for lower, higher in zip(contracts, contracts[1:])
                ))

        self.assertEqual(minimum_support_count("大量", 20), 6)
        self.assertEqual(minimum_support_count("大量", 40), 8)
        self.assertEqual(minimum_support_count("大量", 60), 12)
        self.assertEqual(minimum_support_count("大量", 172), 35)


if __name__ == "__main__":
    unittest.main()
