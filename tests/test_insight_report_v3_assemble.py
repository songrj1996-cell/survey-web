from copy import deepcopy
import re
import unittest

from app.services.insight_report_v3.assemble import (
    assemble_report,
    render_actions_section,
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
    def test_sample_distribution_dedup_preserves_distinct_data_and_original_tables(self):
        profile = _table("MLBB客户端语言")
        duplicate = _table("MLBB客户端语言", "Part 1 用户语言画像")
        duplicate["table_markdown"] = duplicate["table_markdown"].replace("取值", "选项").replace(" | ", "|")
        changed = _table("MLBB客户端语言", "Part 2 后续调查")
        changed["table_markdown"] = changed["table_markdown"].replace("60", "55").replace("40", "45")
        blocks = [profile, duplicate, changed, _table("性别")]
        original = deepcopy(blocks)
        selected = select_sample_tables(blocks, analysis_focus={"report_organization": "MLBB客户端语言"})
        self.assertEqual(selected, [profile, blocks[3], changed])
        self.assertEqual(blocks, original)
        self.assertEqual(len(select_sample_tables([profile, deepcopy(profile)], analysis_focus=None)), 1)

    def test_sample_dedup_does_not_infer_equal_scopes_or_merge_by_title(self):
        first = _table("满意度", "Part 1")
        second = _table("满意度", "Part 2")
        self.assertEqual(len(select_sample_tables(
            [first, second], analysis_focus={"report_organization": "满意度"},
        )), 2)
        profile = _table("性别")
        different_scope = {**_table("性别", "Part 1"), "audience": "新玩家"}
        self.assertEqual(len(select_sample_tables(
            [profile, different_scope], analysis_focus={"report_organization": "性别"},
        )), 2)

    def test_action_required_data_prefixes_and_real_profile_dimension(self):
        for prefix in ("需补充", "需要补充", ""):
            with self.subTest(prefix=prefix):
                action = {
                    "title": "优化配置入口",
                    "target_group": {"profile_name": "MLBB客户端语言", "profile_value": "英语"},
                    "evidence_gap": "尚无上线后的行为数据。",
                    "required_data": prefix + "上线后的使用数据。",
                }
                original = deepcopy(action)
                rendered = render_actions_section(actions=[action], chapter_note="", section_index=5)
                self.assertIn("仍需补充上线后的使用数据。", rendered)
                self.assertNotIn("补充需", rendered)
                self.assertIn("“MLBB客户端语言”为“英语”的群体", rendered)
                self.assertIn("依据该人群的定性材料", rendered)
                self.assertNotIn("专章", rendered)
                self.assertNotIn("母语", rendered)
                self.assertEqual(action, original)

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
        self.assertNotIn("未交给模型", markdown)
        self.assertLessEqual(len(prose), 500)

    def test_assembly_uses_evidence_points_narrative_actions_and_deduplicated_appendix(self):
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
                "evidence": [{
                    "response_id": "global-1",
                    "evidence_point": "失败后被归责且缺少队友支援",
                    "translation_zh": "输了以后经常只怪打野。\n队友也没有来支援。",
                    "translation_status": "source_zh",
                }],
            }],
            chapters=[{
                "id": "c01",
                "title": "责任与协作压力",
                "blocks": [{
                    "id": "b1",
                    "heading": "失败归因集中",
                    "body": "反馈反复指向队友协作不足。",
                    "finding_ids": ["2/f1"],
                    "evidence": [{
                        "response_id": "global-1",
                        "evidence_point": "队友未支援却把失败责任归给打野",
                        "translation_zh": "输了以后经常只怪打野。\n队友也没有来支援。",
                        "translation_status": "source_zh",
                    }],
                }],
            }],
            findings_by_id={"2/f1": finding},
            actions=[{
                "id": "a01",
                "title": "强化协作状态反馈，降低单人归责",
                "action_scope": "general",
                "target_group": None,
                "problem": "失败责任容易集中到单一职责。",
                "objective": "让队伍更容易理解支援与责任来源。",
                "design_directions": ["补充关键支援状态反馈", "提供便于复盘的协作信息"],
                "validation_plan": "可通过结合试用反馈核对玩家能否理解责任来源。",
                "finding_ids": ["2/f1"],
                "evidence_gap": "还缺少上线后的行为证据。",
                "required_data": "需要补充改版后的任务完成数据。",
            }],
        )
        self.assertTrue(report.startswith("# 体验洞察报告"))
        self.assertLess(report.index("## 一、调研背景"), report.index("## 二、样本概况"))
        self.assertLess(report.index("## 二、样本概况"), report.index("## 三、核心发现"))
        self.assertIn("## 四、责任与协作压力", report)
        self.assertIn("## 五、行动建议", report)
        self.assertNotIn("**提及情况：**", report)
        self.assertNotIn("**证据要点：**", report)
        self.assertIn("**建议一：强化协作状态反馈，降低单人归责**", report)
        self.assertIn("可考虑的设计方向：\n1. 补充关键支援状态反馈", report)
        self.assertIn("实施前后可通过结合试用反馈", report)
        self.assertNotIn("核对玩家能否理解责任来源来验证", report)
        self.assertNotIn("可通过可通过", report)
        self.assertNotIn("可通过通过", report)
        self.assertIn("仍需补充改版后的任务完成数据", report)
        self.assertNotIn("补充需要补充", report)
        self.assertNotIn("。。", report)
        self.assertNotIn("。；", report)
        self.assertIn("## 附录 A：完整证据索引", report)
        self.assertEqual(report.count("### E-001"), 1)
        self.assertEqual(report.count("输了以后经常只怪打野。"), 1)
        self.assertIn("三、核心发现 / 责任压力影响意愿", report)
        self.assertIn("四、责任与协作压力 / 失败归因集中", report)
        self.assertNotIn("代表反馈", report)
        self.assertNotIn("design_directions", report)
        self.assertNotIn("finding_ids", report)
        self.assertNotIn("玩家ID | 画像信息", report)

    def test_missing_evidence_summary_points_to_full_index_without_truncation(self):
        translation = "这是一段需要完整保留、不能截短的多句反馈。\n第二句仍然属于同一条来源。"
        report = assemble_report(
            report_title="证据回退测试",
            background_md="## 一、调研背景\n\n背景。",
            sample_md="## 二、样本概况\n\n概况。",
            core_blocks=[{
                "id": "core-1",
                "heading": "待核对发现",
                "body": "正文只保留索引入口。",
                "finding_ids": ["1/f1"],
                "evidence": [{
                    "response_id": "global-2",
                    "evidence_point": "",
                    "translation_zh": translation,
                    "translation_status": "source_zh",
                }],
            }],
            chapters=[],
            findings_by_id={"1/f1": {"id": "1/f1", "frequency_tier": "有玩家"}},
            actions=[],
            action_note="当前证据不足以形成建议。",
        )
        self.assertNotIn("**证据要点：**", report)
        self.assertIn("### E-001", report)
        self.assertEqual(report.count("这是一段需要完整保留、不能截短的多句反馈。"), 1)
        self.assertIn("> 第二句仍然属于同一条来源。", report)

    def test_validation_plan_with_existing_validation_semantics_has_no_duplicate_suffix(self):
        action = {
            "title": "保留挽回空间",
            "problem": "失败后缺少挽回空间",
            "objective": "让玩家更容易感知局势变化",
            "design_directions": [],
            "validation_plan": "通过可用性测试并辅以定性访谈验证玩家对挽回空间的感受变化",
            "evidence_gap": "",
            "required_data": "",
        }
        rendered = render_actions_section(actions=[action], chapter_note="", section_index=4)
        self.assertIn("定性访谈验证玩家对挽回空间的感受变化。", rendered)
        self.assertNotIn("验证玩家对挽回空间的感受变化来验证", rendered)

    def test_unresolved_audit_items_are_listed_immediately_after_title(self):
        report = assemble_report(
            report_title="审校提示测试",
            background_md="## 一、调研背景\n\n背景。",
            sample_md="## 二、样本概况\n\n概况。",
            core_blocks=[],
            chapters=[],
            findings_by_id={},
            actions=[],
            audit_unresolved=[{
                "section": "核心发现",
                "heading": "压力来源",
                "target": "正文结论",
                "problem": "修复后仍无法确认因果表述",
            }],
        )
        self.assertLess(report.index("语义审校提示"), report.index("## 一、调研背景"))
        self.assertIn("1 项未能由自动审校完整确认", report)
        self.assertIn("核心发现 / 压力来源（正文结论）", report)
        self.assertIn("不代表一定错误", report)

    def test_unmet_research_task_is_visible_before_report_body(self):
        report = assemble_report(
            report_title="缺口测试", background_md="## 一、调研背景\n背景",
            sample_md="## 二、样本概况\n样本", core_blocks=[], chapters=[],
            findings_by_id={}, actions=[],
            research_gaps=[{"expected_deliverable": "各偏好选项的原因", "gap": "未向无偏好选项追问原因，不能代为解释"}],
        )
        self.assertLess(report.index("研究任务缺口"), report.index("## 一、调研背景"))
        self.assertIn("未向无偏好选项追问原因，不能代为解释", report)


if __name__ == "__main__":
    unittest.main()
