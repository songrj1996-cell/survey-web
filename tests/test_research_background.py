import unittest

from app.services.report_render import (
    _inject_disclaimer,
    _inject_research_background,
    _prep_export_md,
    _prep_feishu_export_md,
)


class ResearchBackgroundTests(unittest.TestCase):
    def test_inserts_filled_context_before_core_conclusion(self):
        report = _inject_disclaimer("# 调研报告\n\n## 核心结论\n\n报告结论。")

        result = _inject_research_background(report, {
            "problem": "解决聊天频道体验问题",
            "key_concerns": "不同频道分别遇到了什么问题",
            "target_users": "使用游戏内聊天的玩家",
            "analysis_approach": "先建立跨案例框架，再把案例作为证据",
            "background": "不应展示的旧字段",
            "report_usage": "不应展示的旧字段",
        })

        self.assertLess(result.index("## 调研背景"), result.index("## 核心结论"))
        self.assertIn("**业务问题/业务痛点/业务规划**：解决聊天频道体验问题", result)
        self.assertIn("**本次调研最关心的问题**：不同频道分别遇到了什么问题", result)
        self.assertIn("**产品/功能的目标用户**：使用游戏内聊天的玩家", result)
        self.assertIn("**分析思路**：先建立跨案例框架，再把案例作为证据", result)
        self.assertNotIn("不应展示的旧字段", result)

    def test_omits_section_when_context_is_empty(self):
        report = "# 调研报告\n\n## 核心结论\n\n报告结论。"

        self.assertEqual(_inject_research_background(report, {}), report)
        analysis_only = _inject_research_background(
            report,
            {"analysis_approach": "按目标用户横向比较三个案例"},
        )
        self.assertIn("## 调研背景", analysis_only)
        self.assertIn("**分析思路**：按目标用户横向比较三个案例", analysis_only)

    def test_export_preparation_keeps_research_background(self):
        report = _inject_research_background(
            _inject_disclaimer("# 调研报告\n\n## 核心结论\n\n报告结论。"),
            {
                "problem": "验证聊天体验问题",
                "analysis_approach": "先比较判断标准，再解释方案数据",
            },
        )

        export_md = _prep_export_md(report)
        feishu_md = _prep_feishu_export_md(report)
        self.assertIn("## 调研背景", export_md)
        self.assertIn("分析思路", export_md)
        self.assertIn("调研背景", feishu_md)
        self.assertIn("分析思路", feishu_md)

    def test_replaces_existing_section_and_escapes_markdown(self):
        report = "# 调研报告\n\n## 调研背景\n\n旧内容\n\n## 核心结论\n\n报告结论。"
        context = {"problem": "<script>alert(1)</script> **规划**"}

        result = _inject_research_background(report, context)
        repeated = _inject_research_background(result, context)

        self.assertNotIn("旧内容", result)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; \\*\\*规划\\*\\*", result)
        self.assertEqual(result, repeated)


if __name__ == "__main__":
    unittest.main()
