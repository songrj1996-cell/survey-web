import unittest

from app.core.config import DEFAULT_WRITER_REQUIREMENTS
from app.services.report_engine import (
    _build_qa_context,
    _build_crosstab_plan_revision_query,
    _build_crosstab_planner_query,
    _build_large_sample_writer_query,
    _build_writer_action_query,
    _build_writer_context,
    _build_writer_core_query,
    _build_writer_part_query,
)


class ReportWriterStructureTests(unittest.TestCase):
    def test_default_requirements_use_topic_structure_and_translation_only_evidence(self):
        requirements = DEFAULT_WRITER_REQUIREMENTS

        self.assertIn("同一 Topic 下的客观题和相关开放题必须结合分析", requirements)
        self.assertIn("Part 内部**禁止使用任何 `###` 或 `####` 标题**", requirements)
        self.assertIn("表头固定为 `玩家ID`、`画像信息`、`中文翻译`", requirements)
        self.assertIn("不得保留原始语言文本", requirements)
        self.assertIn("`**本节总结：**`", requirements)
        self.assertIn("Markdown 编号列表", requirements)
        self.assertIn("`**相关具体信息引用：**`", requirements)
        self.assertIn("不看玩家原文也能立即理解的**大白话**", requirements)
        self.assertIn("优先沿用玩家反馈中文翻译中的具体说法", requirements)
        self.assertIn("不得为了通俗而补造", requirements)
        self.assertIn("未参与调研立项、未看过问卷提纲的读者也能独立理解", requirements)
        self.assertIn("分别回车成短段，不强制编号", requirements)
        self.assertIn("不同范围的观点与风险不得混写", requirements)
        self.assertNotIn("用 1 段话概括本次调研", requirements)
        self.assertIn("`建议内容`、`优先级`、`产品动作`、`验证方式`、`依据`、`不确定性/前提`", requirements)
        self.assertNotIn("用连贯的段落文字（不用列表）", requirements)
        self.assertNotIn("`**代表性玩家反馈：**`", requirements)
        self.assertNotIn("每个具体题目必须使用 `### 题目名`", requirements)
        self.assertNotIn("表格必须同时有 `玩家ID` 和 `MLBBID` 两列", requirements)

    def test_part_query_requires_topic_synthesis_without_nested_headings(self):
        query = _build_writer_part_query({
            "i": 2,
            "name": "公频聊天",
            "col_desc": "使用情况(single_choice); 使用原因(open_text)",
        })

        self.assertIn("客观题与相关开放题必须结合分析", query)
        self.assertIn("不要按问卷题目逐题复述", query)
        self.assertIn("禁止使用任何 `###` 或 `####` 标题", query)
        self.assertIn("玩家ID | 画像信息 | 中文翻译", query)
        self.assertIn("3–6 条带加粗短标题的 Markdown 编号列表", query)
        self.assertIn("`**相关具体信息引用：**`", query)
        self.assertIn("不看后文原文也能理解的大白话", query)
        self.assertIn("确需概括时", query)
        self.assertIn("不得补造原文中没有的例子", query)
        self.assertNotIn("`**代表性玩家反馈：**`", query)
        self.assertNotIn("`#### 正面观点`", query)
        self.assertIn("系统会在本节总结之后确定性插入客观题统计表", query)
        self.assertIn("不要自行复制客观题统计表", query)
        self.assertIn("大致代表什么、有哪些样本或解释限制", query)
        self.assertIn("不要机械复述最高项和最低项", query)

    def test_core_query_requires_plain_language_grounded_in_player_wording(self):
        query = _build_writer_core_query(
            [{"i": 1, "name": "皮肤编号", "col_desc": "编号评价(open_text)"}],
            has_bug=False,
        )

        self.assertIn("不看玩家原文也能立即理解的大白话", query)
        self.assertIn("优先沿用玩家中文翻译中的具体词语", query)
        self.assertIn("功能性增益", query)
        self.assertIn("具体希望增加、取消或改变什么", query)
        self.assertIn("解释和例子只能来自 <open_text> 或已生成章节", query)
        self.assertIn("未参与调研立项、未看过问卷提纲的读者也能独立理解", query)
        self.assertIn("分别回车成短段，不使用 1、2、3 编号", query)
        self.assertIn("不得写成一个超长段落", query)
        self.assertIn("不同范围不得混写", query)
        self.assertIn("不得机械套用标签或补造研究阶段", query)

    def test_quantitative_part_query_prioritizes_objective_statistics(self):
        query = _build_writer_part_query({
            "i": 1,
            "name": "使用情况",
            "col_desc": "使用频率(single_choice); 使用原因(open_text)",
        }, quantitative_first=True)

        self.assertIn("本次为定量优先报告", query)
        self.assertIn("主要分布、最高/最低项和显著差异", query)
        self.assertIn("完整逐题统计表由系统在附录确定性插入", query)
        self.assertNotIn("本节总结之后确定性插入客观题统计表", query)

    def test_large_sample_mode_keeps_qualitative_stats_rule_out_of_quantitative_query(self):
        plan = {"parts": [], "columns": []}
        qualitative = _build_large_sample_writer_query(
            "", {}, plan, [], quantitative_first=False,
        )
        quantitative = _build_large_sample_writer_query(
            "", {}, plan, [], quantitative_first=True,
        )

        self.assertIn("系统会在对应 Part 内确定性插入客观题统计表", qualitative)
        self.assertNotIn("系统会在对应 Part 内确定性插入客观题统计表", quantitative)

    def test_crosstab_planning_uses_business_context(self):
        context = {
            "problem": "判断新剧情是否值得继续投入",
            "key_concerns": "不同熟悉度玩家的认知差异",
            "target_users": "剧情内容玩家",
        }

        initial = _build_crosstab_planner_query("Q1", ["熟悉度"], [], context)
        revised = _build_crosstab_plan_revision_query(
            "Q1", ["熟悉度"], [], [{"name": "认知"}], "突出差异", context,
        )

        for query in (initial, revised):
            self.assertIn("判断新剧情是否值得继续投入", query)
            self.assertIn("不同熟悉度玩家的认知差异", query)
            self.assertIn("剧情内容玩家", query)

    def test_action_query_requires_six_column_markdown_table(self):
        query = _build_writer_action_query(
            [{"i": 1, "name": "聊天体验", "col_desc": "体验反馈(open_text)"}],
            has_bug=False,
        )

        self.assertIn("只使用一张 Markdown 表格", query)
        self.assertIn(
            "建议内容 | 优先级 | 产品动作 | 验证方式 | 依据 | 不确定性/前提",
            query,
        )
        self.assertIn("`优先级` 只能写高/中/低", query)

    def test_writer_context_merges_all_identity_sources_into_player_id(self):
        plan = {
            "parts": [{"name": "聊天体验", "column_indexes": [1]}],
            "columns": [{"index": 1, "name": "体验反馈", "role": "open_text"}],
        }
        open_text = {
            1: [{
                "ids": {"Discord用户ID": "discord-1", "MLBBID": "mlbb-2"},
                "profile": {"好友数量": "1~5"},
                "text": "Too many spam messages.",
            }],
        }

        _, open_text_md, requirements = _build_writer_context("", open_text, plan, ["ID", "体验反馈"])

        self.assertIn("玩家ID=discord-1 / mlbb-2", open_text_md)
        self.assertNotIn("MLBBID=mlbb-2", open_text_md)
        self.assertIn("只能使用一个 `玩家ID` 列", requirements)
        self.assertIn("不得展示原始语言文本", requirements)

    def test_qa_context_contains_report_and_questionnaire_evidence(self):
        source = {
            "report_md": "# 聊天功能报告\n\n## 核心结论\n存在消息丢失反馈。",
            "stats_md": "有效样本(总计):总体=1",
            "questionnaire_text": "Q1：是否遇到聊天问题？",
            "qualitative_context": {"problem": "了解聊天体验"},
            "plan": {
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
            },
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "切换设备后消息消失"]],
        }

        context = _build_qa_context(source)

        self.assertIn("<report>", context)
        self.assertIn("存在消息丢失反馈", context)
        self.assertIn("<analysis_plan>", context)
        self.assertIn("有效样本(总计):总体=1", context)
        self.assertIn("Q1：是否遇到聊天问题？", context)
        self.assertIn("切换设备后消息消失", context)

if __name__ == "__main__":
    unittest.main()
