import unittest

import survey_stats
from app.services.survey_service import _apply_verified_comparison_repairs


def _fixture():
    header = "How satisfied are you with this design overall?"
    rows = [
        ["玩家ID", header, header, header],
        ["p-1", "3", "3", "4"],
        ["p-2", "4", "4", "4"],
        ["p-3", "4.52", "3.68", "3.76"],
    ]
    plan = {
        "columns": [
            {"index": 1, "role": "scale", "name": "概念1整体满意度", "min": 1, "max": 5},
            {"index": 2, "role": "scale", "name": "概念2整体满意度", "min": 1, "max": 5},
            {"index": 3, "role": "scale", "name": "概念3整体满意度", "min": 1, "max": 5},
        ],
        "parts": [],
    }
    # 均值严格为：概念3 3.92 > 概念1 3.84 > 概念2 3.56。
    return rows, plan


class ReportComparisonConsistencyTests(unittest.TestCase):
    def setUp(self):
        rows, plan = _fixture()
        self.catalog = survey_stats.build_comparison_fact_catalog(rows, plan)

    def analyze(self, text):
        return survey_stats.analyze_comparison_claims(text, self.catalog)

    def test_catalog_builds_verified_descending_order(self):
        self.assertEqual(len(self.catalog), 1)
        self.assertEqual(
            self.catalog[0]["descending"],
            ["概念3", "概念1", "概念2"],
        )
        values = {
            item["entity"]: round(item["value"], 2)
            for item in self.catalog[0]["members"]
        }
        self.assertEqual(values, {"概念1": 3.84, "概念2": 3.56, "概念3": 3.92})

    def test_catalog_uses_same_part_filter_scope_as_rendered_statistics(self):
        header = "How satisfied are you with this design overall?"
        rows = [
            ["分组", header, header, header],
            ["A", "5", "1", "4"],
            ["B", "1", "4", "4"],
        ]
        plan = {
            "columns": [
                {"index": 0, "role": "single_choice", "name": "分组"},
                {"index": 1, "role": "scale", "name": "概念1整体满意度", "min": 1, "max": 5},
                {"index": 2, "role": "scale", "name": "概念2整体满意度", "min": 1, "max": 5},
                {"index": 3, "role": "scale", "name": "概念3整体满意度", "min": 1, "max": 5},
            ],
            "parts": [
                {"column_indexes": [1], "filter": {"column_index": 0, "allowed_options": ["A"]}},
                {"column_indexes": [2], "filter": {"column_index": 0, "allowed_options": ["B"]}},
                {"column_indexes": [3]},
            ],
        }

        catalog = survey_stats.build_comparison_fact_catalog(rows, plan)

        self.assertEqual(catalog[0]["descending"], ["概念1", "概念2", "概念3"])

    def test_claim_is_bound_to_the_named_metric_when_entities_repeat_across_groups(self):
        rows = [
            ["整体", "整体", "整体", "视觉", "视觉", "视觉"],
            ["3", "2", "4", "5", "3", "2"],
        ]
        plan = {
            "columns": [
                {"index": 0, "role": "scale", "name": "概念1整体满意度", "min": 1, "max": 5},
                {"index": 1, "role": "scale", "name": "概念2整体满意度", "min": 1, "max": 5},
                {"index": 2, "role": "scale", "name": "概念3整体满意度", "min": 1, "max": 5},
                {"index": 3, "role": "scale", "name": "概念1视觉满意度", "min": 1, "max": 5},
                {"index": 4, "role": "scale", "name": "概念2视觉满意度", "min": 1, "max": 5},
                {"index": 5, "role": "scale", "name": "概念3视觉满意度", "min": 1, "max": 5},
            ],
            "parts": [],
        }
        catalog = survey_stats.build_comparison_fact_catalog(rows, plan)

        named = survey_stats.analyze_comparison_claims(
            "概念3整体满意度均值最高。",
            catalog,
        )
        ambiguous = survey_stats.analyze_comparison_claims(
            "概念3满意度均值最高。",
            catalog,
        )

        self.assertEqual(named["issues"], [])
        self.assertEqual(len(ambiguous["issues"]), 1)
        self.assertFalse(ambiguous["issues"][0]["repairable"])
        self.assertIn("无法唯一绑定", ambiguous["issues"][0]["reasons"][0])

    def test_detects_v1_wrong_highest_claim(self):
        result = self.analyze(
            "概念1的满意度均值为3.84，是三个概念中均值最高的一个。"
        )
        self.assertEqual(len(result["issues"]), 1)
        self.assertTrue(result["issues"][0]["repairable"])
        self.assertIn("概念1并非整体满意度最高项", result["issues"][0]["reasons"])

    def test_accepts_v2_correct_highest_claim(self):
        result = self.analyze(
            "概念3整体满意度均值3.92，是三个概念中均值最高的一个。"
        )
        self.assertEqual(result["issues"], [])
        self.assertEqual(result["checked_claim_count"], 1)

    def test_binds_extreme_to_nearest_entity_in_multi_entity_sentence(self):
        result = self.analyze(
            "概念1满意度均值为3.84，而概念3均值为3.92，是三个概念中最高的一个。"
        )
        self.assertEqual(result["issues"], [])

    def test_detects_wrong_pairwise_relation(self):
        result = self.analyze("概念1满意度均值高于概念3。")
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("概念1并不高于概念3", result["issues"][0]["reasons"])

    def test_detects_wrong_full_order(self):
        result = self.analyze("满意度均值从高到低依次为概念1、概念3、概念2。")
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("正文中的顺序与确定性统计排序不一致", result["issues"][0]["reasons"])

    def test_accepts_correct_full_order(self):
        result = self.analyze("满意度均值从高到低依次为概念3、概念1、概念2。")
        self.assertEqual(result["issues"], [])

    def test_detects_wrong_rank(self):
        result = self.analyze("概念2在满意度均值中排名第二。")
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn("概念2的名次不是第2", result["issues"][0]["reasons"])

    def test_detects_false_tie_claim(self):
        result = self.analyze("概念1和概念3满意度均值并列最高。")
        self.assertEqual(len(result["issues"]), 1)
        self.assertIn(
            "正文声称并列最高的项目并未共同处于最高值",
            result["issues"][0]["reasons"],
        )

    def test_accepts_true_tie_and_shared_rank(self):
        rows, plan = _fixture()
        rows[1][1:] = ["4", "3", "4"]
        rows[2][1:] = ["4", "3", "4"]
        rows[3][1:] = ["4", "3", "4"]
        tied_catalog = survey_stats.build_comparison_fact_catalog(rows, plan)
        result = survey_stats.analyze_comparison_claims(
            "概念1和概念3满意度均值并列第一。",
            tied_catalog,
        )
        self.assertEqual(result["issues"], [])

    def test_marks_ambiguous_ranking_as_manual_review(self):
        result = self.analyze("概念1排名高于概念3。")
        self.assertEqual(len(result["issues"]), 1)
        self.assertFalse(result["issues"][0]["repairable"])
        self.assertIn("比较表述未明确绑定到量表均值口径", result["issues"][0]["reasons"])

    def test_detects_all_ten_occurrences(self):
        report = "\n".join(
            f"第{i}处：概念1满意度均值为3.84，是三个概念中最高的一个。"
            for i in range(1, 11)
        )
        result = self.analyze(report)
        self.assertEqual(len(result["issues"]), 10)
        self.assertEqual(
            [issue["claim_id"] for issue in result["issues"]],
            [f"C{i:03d}" for i in range(1, 11)],
        )

    def test_applies_only_independently_verified_replacements(self):
        report = (
            "概念1满意度均值为3.84，是三个概念中最高的一个。\n"
            "概念2满意度均值高于概念3。"
        )
        initial = self.analyze(report)
        repaired, audit = _apply_verified_comparison_repairs(
            report,
            self.catalog,
            initial,
            {
                "C001": "概念3满意度均值为3.92，是三个概念中最高的一个。",
                "C002": "概念2满意度均值9.99，高于概念3。",
            },
            "3.92 3.84 3.56",
        )

        self.assertIn("概念3满意度均值为3.92", repaired)
        self.assertIn("概念2满意度均值高于概念3", repaired)
        self.assertEqual(audit["status"], "needs_review")
        self.assertEqual(audit["applied_count"], 1)
        self.assertEqual(audit["unresolved_count"], 1)
        self.assertEqual(audit["changes"][0]["original"], initial["issues"][0]["original_sentence"])

    def test_returns_repaired_when_all_detected_claims_are_fixed(self):
        report = "概念1满意度均值为3.84，是三个概念中最高的一个。"
        initial = self.analyze(report)
        repaired, audit = _apply_verified_comparison_repairs(
            report,
            self.catalog,
            initial,
            {"C001": "概念3满意度均值为3.92，是三个概念中最高的一个。"},
            "3.92 3.84 3.56",
        )

        self.assertNotEqual(repaired, report)
        self.assertEqual(audit["status"], "repaired")
        self.assertEqual(audit["unresolved"], [])


if __name__ == "__main__":
    unittest.main()
