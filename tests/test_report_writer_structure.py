import unittest

from app.core.config import DEFAULT_WRITER_REQUIREMENTS
from app.services.report_engine import (
    _build_qa_context,
    _build_qa_seed_query,
    _build_writer_action_query,
    _build_writer_context,
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
        self.assertNotIn("`**代表性玩家反馈：**`", query)
        self.assertNotIn("`#### 正面观点`", query)

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

    def test_qa_seed_query_restores_previous_qa_and_current_question(self):
        query = _build_qa_seed_query(
            "<qa_context><report>报告正文</report></qa_context>",
            [
                {"role": "user", "content": "之前的问题"},
                {"role": "ai", "content": "之前的回答"},
            ],
            "为什么得出这个结论？",
        )

        self.assertIn("报告正文", query)
        self.assertIn("用户：之前的问题", query)
        self.assertIn("AI：之前的回答", query)
        self.assertIn("用户问题：为什么得出这个结论？", query)


if __name__ == "__main__":
    unittest.main()
