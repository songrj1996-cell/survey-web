import unittest

import crosstab_parser
from app.services.report_render import _prep_export_md
from app.services.stats_presentation import (
    inject_qualitative_stats,
    render_qualitative_stats_by_part,
    render_stats_appendix,
)
from survey_stats import compute, structured_tables


class StatsPresentationTests(unittest.TestCase):
    def test_missing_multichoice_delimiter_prefers_normalized_newlines(self):
        rows = [
            ["剧情认知"],
            ["Origin story\nFaction split"],
            ["Origin story"],
        ]
        plan = {
            "columns": [{
                "index": 0,
                "name": "剧情认知",
                "role": "multi_choice",
                "options": ["起源故事", "阵营分裂"],
                "value_aliases": {
                    "起源故事": ["Origin story"],
                    "阵营分裂": ["Faction split"],
                },
            }],
            "parts": [{"name": "剧情认知", "column_indexes": [0]}],
        }

        stats_md, _ = compute(rows, plan)

        self.assertIn("| 起源故事 | 2 | 100.0% |", stats_md)
        self.assertIn("| 阵营分裂 | 1 | 50.0% |", stats_md)
        self.assertNotIn("Origin story", stats_md)

    def test_python_stats_builds_bar_and_matrix_heatmap(self):
        stats_md = """## Part 1 使用情况

### 使用频率

| 选项 | 人数 | 占比 |
|---|---|---|
| 每天 | 6 | 60.0% |
| 偶尔 | 4 | 40.0% |

### 功能评价

| 子项 | 满意 | 一般 | 回答人数 |
|---|---|---|---|
| 易用性 | 8 (80.0%) | 2 (20.0%) | 10 |
| 稳定性 | 5 (50.0%) | 5 (50.0%) | 10 |
"""

        blocks = structured_tables(stats_md)

        self.assertEqual([block["chart"]["type"] for block in blocks], ["bar", "heatmap"])
        self.assertEqual(blocks[0]["chart"]["series"][0]["values"], [60.0, 40.0])
        self.assertEqual(blocks[1]["chart"]["values"], [[80.0, 20.0], [50.0, 50.0]])

    def test_appendix_has_every_table_and_export_removes_only_chart_payload(self):
        blocks = [{
            "title": "使用频率",
            "part": "Part 1 使用情况",
            "chart": {
                "type": "bar",
                "labels": ["每天"],
                "series": [{"name": "占比", "values": [60.0]}],
            },
            "table_markdown": "| 选项 | 占比 |\n|---|---|\n| 每天 | 60.0% |",
        }]

        appendix = render_stats_appendix(blocks, "python")
        exported = _prep_export_md("# 报告\n\n" + appendix, mode="quantitative")

        self.assertIn("平台 Python 自动计算", appendix)
        self.assertIn("```stats-chart", appendix)
        self.assertIn("| 每天 | 60.0% |", exported)
        self.assertNotIn("stats-chart", exported)
        self.assertNotIn("定性分析结果", exported)

    def test_external_crosstab_builds_bar_and_matrix_heatmap(self):
        parsed = {
            "segments": [{"label": "总体"}, {"label": "男性"}],
            "questions": [
                {
                    "name": "使用频率",
                    "options": [
                        {"label": "每天", "values": {"总体": 0.6, "男性": 0.7}},
                        {"label": "偶尔", "values": {"总体": 0.4, "男性": 0.3}},
                    ],
                },
                {
                    "name": "功能评价-易用性",
                    "matrix_group": "功能评价",
                    "sub_item": "易用性",
                    "options": [
                        {"label": "满意", "values": {"总体": 0.8}},
                        {"label": "一般", "values": {"总体": 0.2}},
                    ],
                },
                {
                    "name": "功能评价-稳定性",
                    "matrix_group": "功能评价",
                    "sub_item": "稳定性",
                    "options": [
                        {"label": "满意", "values": {"总体": 0.5}},
                        {"label": "一般", "values": {"总体": 0.5}},
                    ],
                },
            ],
        }

        blocks = crosstab_parser.structured_tables(parsed)

        self.assertEqual([block["chart"]["type"] for block in blocks], ["bar", "heatmap"])
        self.assertEqual(blocks[1]["chart"]["values"], [[80.0, 20.0], [50.0, 50.0]])

    def test_qualitative_stats_are_inserted_once_inside_each_part_without_charts(self):
        stats_md = """## 画像维度概览

### 好友数量

| 取值 | 频数 | 占比 |
|---|---|---|
| 1~5 | 36 | 60.0% |
| 6~10 | 24 | 40.0% |

## Part 1 聊天意愿

### 聊天意愿评分

- 均值: **3.73**, 中位数: 4, 标准差: 1.07
- 有效数字回答: 60 条

| 取值 | 频数 | 占比 |
|---|---|---|
| 4 | 30 | 50.0% |
| 3 | 18 | 30.0% |
| 2 | 12 | 20.0% |

**按「好友数量」分组（量表均值对比）**

| 画像取值 | 样本量 | 均值 | 中位数 | 标准差 |
|---|---|---|---|---|
| 1~5 | 36 | 3.50 | 4 | 1.10 |
| 6~10 | 24 | 4.10 | 4 | 0.80 |
"""
        report_md = """# 报告

## Part 1 聊天意愿

**本节总结：**

1. **总体积极**：多数玩家愿意聊天。

**使用现状与人群分层**

现有主题分析继续保留。
"""

        blocks = render_qualitative_stats_by_part(stats_md)
        result = inject_qualitative_stats(report_md, stats_md)

        self.assertEqual(blocks["Part 1 聊天意愿"].count("### 辅助统计"), 1)
        self.assertEqual(result.count("### 辅助统计"), 1)
        self.assertIn("**好友数量**", result)
        self.assertIn("**聊天意愿评分**", result)
        self.assertIn("| 6~10 | 24 | 4.10 | 4 | 0.80 |", result)
        self.assertIn("**基础解读：**", result)
        self.assertNotIn("stats-chart", result)
        self.assertLess(result.index("### 辅助统计"), result.index("**使用现状与人群分层**"))


if __name__ == "__main__":
    unittest.main()
