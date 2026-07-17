import unittest

from app.core.config import DEFAULT_WRITER_REQUIREMENTS
from app.services.report_engine import _build_writer_context, _build_writer_part_query


class ReportWriterStructureTests(unittest.TestCase):
    def test_default_requirements_use_topic_structure_and_translation_only_evidence(self):
        requirements = DEFAULT_WRITER_REQUIREMENTS

        self.assertIn("同一 Topic 下的客观题和相关开放题必须结合分析", requirements)
        self.assertIn("Part 内部**禁止使用任何 `###` 或 `####` 标题**", requirements)
        self.assertIn("表头固定为 `玩家ID`、`画像信息`、`中文翻译`", requirements)
        self.assertIn("不得保留原始语言文本", requirements)
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
        self.assertNotIn("`#### 正面观点`", query)

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


if __name__ == "__main__":
    unittest.main()
