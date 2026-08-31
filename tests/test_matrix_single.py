"""矩阵单选从题型确认到统计渲染的回归测试。"""

import unittest

from survey_plan import expand_confirmed_to_columns
from survey_stats import compute


class MatrixSingleTests(unittest.TestCase):
    def test_confirmed_matrix_single_expands_shared_options_to_each_row(self):
        columns = expand_confirmed_to_columns([{
            "name_zh": "功能评价",
            "role": "matrix_single",
            "column_indexes": [0, 1],
            "rows": ["易用性", "稳定性"],
            "options": ["满意", "一般", "不满意"],
        }])

        self.assertEqual([column["role"] for column in columns], [
            "matrix_single",
            "matrix_single",
        ])
        self.assertEqual([column["matrix_row"] for column in columns], [
            "易用性",
            "稳定性",
        ])
        self.assertEqual(columns[0]["options"], ["满意", "一般", "不满意"])
        self.assertEqual(columns[1]["options"], ["满意", "一般", "不满意"])

    def test_stats_uses_each_matrix_row_nonempty_count_as_denominator(self):
        rows = [
            ["功能评价 [易用性]", "功能评价 [稳定性]"],
            ["满意", "一般"],
            ["满意", "满意"],
            ["", "不满意"],
        ]
        columns = expand_confirmed_to_columns([{
            "name_zh": "功能评价",
            "role": "matrix_single",
            "column_indexes": [0, 1],
            "rows": ["易用性", "稳定性"],
            "options": ["满意", "一般", "不满意"],
        }])
        plan = {
            "columns": columns,
            "parts": [{"name": "功能体验", "column_indexes": [0, 1]}],
        }

        stats, open_text = compute(rows, plan)

        self.assertIn("矩阵单选", stats)
        self.assertIn("| 易用性 | 2 (100.0%) | 0 (0.0%) | 0 (0.0%) | 2 |", stats)
        self.assertIn("| 稳定性 | 1 (33.3%) | 1 (33.3%) | 1 (33.3%) | 3 |", stats)
        self.assertEqual(open_text, {})

    def test_ranking_stats_include_all_numeric_and_localized_answers(self):
        permutations = (
            [(1, 2, 3)] * 10
            + [(1, 3, 2)] * 27
            + [(2, 1, 3)] * 9
            + [(2, 3, 1)] * 12
            + [(3, 1, 2)] * 13
            + [(3, 2, 1)] * 21
        )
        rows = [["概念排名 [概念1]", "概念排名 [概念2]", "概念排名 [概念3]"]]
        rows.extend([
            [float(rank) if row_index < 33 else f"Peringkat {rank}" for rank in ranks]
            for row_index, ranks in enumerate(permutations)
        ])
        columns = expand_confirmed_to_columns([{
            "name_zh": "概念排名",
            "role": "matrix_single",
            "column_indexes": [0, 1, 2],
            "rows": ["概念1", "概念2", "概念3"],
            "options": ["第1名", "第2名", "第3名"],
            "analysis_semantic": "ranking",
            "ranking_size": 3,
        }])
        plan = {
            "columns": columns,
            "parts": [{"name": "概念排名", "column_indexes": [0, 1, 2]}],
        }

        stats, _ = compute(rows, plan)

        self.assertIn("矩阵单选排名", stats)
        self.assertIn("| 概念1 | 37 (40.2%) | 21 (22.8%) | 34 (37.0%) | 92 |", stats)
        self.assertIn("| 概念2 | 22 (23.9%) | 31 (33.7%) | 39 (42.4%) | 92 |", stats)
        self.assertIn("| 概念3 | 33 (35.9%) | 40 (43.5%) | 19 (20.7%) | 92 |", stats)
        self.assertIn("数值越低表示整体越靠前", stats)
