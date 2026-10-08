import re
import unittest

from app.services.insight_report_v3.assemble import (
    assemble_report,
    render_sample_overview,
    select_sample_tables,
)


def _table(title, part=""):
    return {
        "title": title,
        "part": part,
        "table_markdown": (
            "| 取值 | 频数 | 占比 |\n"
            "|---|---:|---:|\n"
            "| A | 60 | 60.0% |\n"
            "| B | 40 | 40.0% |"
        ),
    }


class InsightV3AssembleTests(unittest.TestCase):
    def test_sample_overview_keeps_profiles_and_only_focus_related_table(self):
        blocks = [
            _table("性别"),
            _table("段位"),
            _table("满意度", "Part 1 体验"),
            _table("无关选择题", "Part 2 其它"),
        ]
        selected = select_sample_tables(
            blocks,
            analysis_focus={"report_organization": "重点解释满意度与体验之间的关系"},
        )
        self.assertEqual([item["title"] for item in selected], ["性别", "段位", "满意度"])

        markdown, selected_again = render_sample_overview(
            "<metadata>总样本: 105, 有效样本: 100, 全空被排除: 5 条</metadata>",
            blocks,
            analysis_focus={"report_organization": "重点解释满意度与体验之间的关系"},
        )
        self.assertEqual(len(selected_again), 3)
        self.assertIn("105 份", markdown)
        self.assertIn("100 份", markdown)
        self.assertEqual(markdown.count("| 取值 | 频数 | 占比 |"), 3)
        self.assertNotIn("无关选择题", markdown)
        prose = re.sub(r"(?m)^\|.*$", "", markdown)
        prose = re.sub(r"[#*\s\d.%—-]", "", prose)
        self.assertGreaterEqual(len(prose), 300)
        self.assertLessEqual(len(prose), 500)

    def test_assembly_uses_fixed_first_three_sections_and_inline_evidence(self):
        finding = {
            "id": "2/f1",
            "question_key": "2",
            "frequency_tier": "部分",
        }
        report = assemble_report(
            report_title="体验洞察报告",
            background_md="## 一、调研背景\n\n- **研究问题：** 为什么流失",
            sample_md="## 二、样本概况\n\n确定性样本说明。",
            core_blocks=[{
                "id": "core-1",
                "heading": "责任压力影响意愿",
                "body": "失败时的归因压力会削弱继续学习意愿。",
                "finding_ids": ["2/f1"],
                "evidence": [{"translation_zh": "输了以后经常只怪打野。\n队友也没有来支援。"}],
            }],
            chapters=[{
                "id": "c01",
                "title": "责任与协作压力",
                "blocks": [{
                    "id": "b1",
                    "heading": "失败归因集中",
                    "body": "反馈反复指向队友协作不足。",
                    "finding_ids": ["2/f1"],
                    "evidence": [{"translation_zh": "队友不来帮忙却怪我。"}],
                }],
            }],
            findings_by_id={"2/f1": finding},
        )
        self.assertTrue(report.startswith("# 体验洞察报告"))
        self.assertLess(report.index("## 一、调研背景"), report.index("## 二、样本概况"))
        self.assertLess(report.index("## 二、样本概况"), report.index("## 三、核心发现"))
        self.assertIn("## 四、责任与协作压力", report)
        self.assertIn("**提及情况：** 部分。", report)
        self.assertIn("> 代表反馈：队友不来帮忙却怪我。", report)
        self.assertIn("> 代表反馈：输了以后经常只怪打野。\n> 队友也没有来支援。", report)
        self.assertNotIn("玩家ID | 画像信息", report)


if __name__ == "__main__":
    unittest.main()
