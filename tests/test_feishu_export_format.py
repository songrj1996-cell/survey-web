import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import CORE_END, CORE_START, QUALITATIVE_DISCLAIMER, REPORT_DISCLAIMER
from app.integrations.feishu_client import (
    _BT_BULLET,
    _BT_CALLOUT,
    _FONT_ORANGE,
    _FONT_PURPLE,
    _core_callout_children,
    _divider_block,
    _replace_section_with_callout,
)
from app.services.report_render import (
    _extract_feishu_callout_sections,
    _prep_feishu_export_md,
)


SAMPLE_REPORT = f"""# 测试报告
{REPORT_DISCLAIMER}
{QUALITATIVE_DISCLAIMER}
{CORE_START}
## 核心结论
本次调研共收集 78 份有效回复。
### 总体判断
这是总体判断，包含**关键结论**。
---
### Part 1 玩家画像与聊天意愿：关键发现
- **样本特征**：玩家整体聊天意愿较高。
### 高信号少数观点与风险
1. **风险信号**：存在需要关注的体验风险。
{CORE_END}
---------------- 以下为详细信息，各位可以按需查看 ----------------
## Part 1 玩家画像与聊天意愿
### 具体题目
正文。
"""


class FeishuExportFormatTests(unittest.TestCase):
    def test_feishu_markdown_uses_quotes_and_cleans_part_titles(self):
        prepared = _prep_feishu_export_md(SAMPLE_REPORT)

        self.assertNotIn("# 测试报告", prepared)
        self.assertIn(f"> *{REPORT_DISCLAIMER.removeprefix('> ')}*", prepared)
        self.assertIn(f"> *{QUALITATIVE_DISCLAIMER.removeprefix('> ')}*", prepared)
        self.assertIn("> 本次调研共收集 78 份有效回复。", prepared)
        self.assertNotIn("### 总体判断", prepared)
        self.assertIn("**玩家画像与聊天意愿**", prepared)
        self.assertIn("## 玩家画像与聊天意愿", prepared)
        self.assertNotIn("Part 1", prepared)

    def test_only_core_body_is_extracted_for_callout(self):
        sections = _extract_feishu_callout_sections(SAMPLE_REPORT)

        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["title"], "核心结论")
        joined = "\n".join(sections[0]["lines"])
        self.assertNotIn("本次调研共收集", joined)
        self.assertNotIn("## 核心结论", joined)
        self.assertIn("### Part 1 玩家画像与聊天意愿：关键发现", joined)

    def test_callout_children_are_body_blocks_with_rich_group_titles(self):
        lines = _extract_feishu_callout_sections(SAMPLE_REPORT)[0]["lines"]
        children = _core_callout_children(lines)

        self.assertTrue(children)
        self.assertTrue(all("heading" not in key for block in children for key in block))
        group = next(
            block for block in children
            if block.get("text", {}).get("elements", [{}])[0]
            .get("text_run", {}).get("content") == "玩家画像与聊天意愿"
        )
        style = group["text"]["elements"][0]["text_run"]["text_element_style"]
        self.assertTrue(style["bold"])
        self.assertEqual(style["text_color"], _FONT_PURPLE)
        self.assertTrue(any(block["block_type"] == _BT_BULLET for block in children))
        self.assertEqual(_BT_CALLOUT, 19)

    def test_detail_divider_is_centered_bold_and_orange(self):
        block = _divider_block("---------------- 以下为详细信息，各位可以按需查看 ----------------")

        self.assertEqual(block["text"]["style"]["align"], 2)
        style = block["text"]["elements"][0]["text_run"]["text_element_style"]
        self.assertTrue(style["bold"])
        self.assertEqual(style["text_color"], _FONT_ORANGE)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = ""

    def json(self):
        return self._payload


class FeishuCalloutApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_heading_and_sample_stay_outside_callout(self):
        blocks = [
            {
                "block_id": "core-heading",
                "parent_id": "doc",
                "heading2": {"elements": [{"text_run": {"content": "核心结论"}}]},
            },
            {
                "block_id": "sample",
                "parent_id": "doc",
                "quote": {"elements": [{"text_run": {"content": "本次调研共收集 78 份有效回复。"}}]},
            },
            {
                "block_id": "old-1",
                "parent_id": "doc",
                "text": {"elements": [{"text_run": {"content": "总体判断正文"}}]},
            },
            {
                "block_id": "old-2",
                "parent_id": "doc",
                "bullet": {"elements": [{"text_run": {"content": "旧要点"}}]},
            },
            {
                "block_id": "divider",
                "parent_id": "doc",
                "text": {"elements": [{"text_run": {"content": "以下为详细信息，各位可以按需查看"}}]},
            },
            {
                "block_id": "detail-heading",
                "parent_id": "doc",
                "heading2": {"elements": [{"text_run": {"content": "玩家画像"}}]},
            },
        ]
        requests = []

        async def request(method, url, headers=None, json=None):
            requests.append((method, url, json))
            if method == "POST" and json.get("children", [{}])[0].get("block_type") == _BT_CALLOUT:
                return _FakeResponse({"code": 0, "data": {"children": [{"block_id": "callout"}]}})
            return _FakeResponse({"code": 0, "data": {"children": []}})

        client = AsyncMock()
        client.request.side_effect = request
        with patch(
            "app.integrations.feishu_client._list_doc_blocks",
            new=AsyncMock(return_value=blocks),
        ):
            changed = await _replace_section_with_callout(
                client,
                {"Authorization": "Bearer test"},
                "doc",
                "核心结论",
                ["### 总体判断", "正文", "### Part 1 玩家画像：关键发现", "- **要点**：内容"],
            )

        self.assertTrue(changed)
        create_callout = requests[0][2]
        self.assertEqual(create_callout["index"], 2)
        self.assertEqual(create_callout["children"][0]["block_type"], _BT_CALLOUT)
        self.assertEqual(create_callout["children"][0]["callout"]["background_color"], 2)
        delete_payload = next(payload for method, _, payload in requests if method == "DELETE")
        self.assertEqual(delete_payload, {"start_index": 3, "end_index": 5})


if __name__ == "__main__":
    unittest.main()
