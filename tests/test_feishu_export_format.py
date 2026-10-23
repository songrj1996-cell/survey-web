import unittest
from unittest.mock import AsyncMock, patch

from app.core.config import CORE_END, CORE_START, QUALITATIVE_DISCLAIMER, REPORT_DISCLAIMER
from app.integrations.feishu_client import (
    _BT_CALLOUT,
    _BT_ORDERED,
    _BT_QUOTE,
    _FONT_ORANGE,
    _FONT_PURPLE,
    _core_callout_children,
    _divider_block,
    _markdown_inline_elements,
    _replace_section_with_callout,
)
from app.services.report_render import (
    _extract_feishu_callout_sections,
    _prep_feishu_export_md,
    _player_case_markdown,
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
1. **样本特征**：玩家整体聊天意愿较高，<u>该判断需要优先关注</u>。
    1. *证据口径*：仅针对有效回答玩家。
> *补充说明*：这里是辅助证据，不是新的玩家观点。
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
        self.assertTrue(any(block["block_type"] == _BT_ORDERED for block in children))
        self.assertTrue(any(block["block_type"] == _BT_QUOTE for block in children))
        nested = next(
            block for block in children
            if block["block_type"] == _BT_ORDERED
            and "证据口径" in "".join(
                element["text_run"]["content"]
                for element in block["ordered"]["elements"]
            )
        )
        nested_text = "".join(
            element["text_run"]["content"]
            for element in nested["ordered"]["elements"]
        )
        self.assertTrue(nested_text.startswith("　↳ "))
        self.assertEqual(_BT_CALLOUT, 19)

    def test_inline_styles_preserve_bold_italic_underline_and_combinations(self):
        elements = _markdown_inline_elements(
            "普通 **加粗** *斜体* <u>下划线</u> <u>**组合强调**</u>"
        )
        runs = {
            element["text_run"]["content"]: element["text_run"].get(
                "text_element_style", {}
            )
            for element in elements
            if element["text_run"]["content"].strip()
        }

        self.assertTrue(runs["加粗"]["bold"])
        self.assertTrue(runs["斜体"]["italic"])
        self.assertTrue(runs["下划线"]["underline"])
        self.assertTrue(runs["组合强调"]["underline"])
        self.assertTrue(runs["组合强调"]["bold"])

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


class PlayerCaseFormattingTests(unittest.TestCase):
    def test_inline_case_is_bounded_and_keeps_ids_links_and_words(self):
        md = "## 体验\n\n典型玩家案例｜R73：**合成案例**，见[证据](https://example.invalid/source)。\n\n普通分析仍在外面。"
        formatted, texts = _player_case_markdown(md)
        self.assertEqual(texts, ["典型玩家案例｜\nR73：合成案例，见证据 (https://example.invalid/source)。"])
        self.assertIn("```\n\n普通分析仍在外面。", formatted)
        self.assertEqual(_player_case_markdown(formatted), (formatted, []))

    def test_standalone_labels_collect_only_explicit_player_paragraphs_and_lists(self):
        for label in ["**典型玩家案例**", "#### 典型玩家案例", "典型玩家案例：", "典型玩家案例\n---"]:
            md = label + "\n\nR73：合成经历。\n\n1. R74：另一位玩家。\n2. R75 玩家选择放弃。\n\n分析边界。\n\nR90：未标记，不属于案例。"
            formatted, texts = _player_case_markdown(md)
            self.assertEqual(len(texts), 1, label)
            for player in ["R73", "R74", "R75"]:
                self.assertIn(player, texts[0])
            self.assertNotIn("分析边界", texts[0])
            self.assertNotIn("R90", texts[0])
            self.assertIn("分析边界", formatted)

    def test_negative_controls_do_not_become_cases(self):
        samples = [
            "下面通过典型玩家案例说明差异。\n\nR73：原文。",
            "R73：没有标签的玩家段落。",
            "典型玩家案例\n\n总体而言需要改进。\n\nR73：不跨越分析段落。",
            "典型玩家案例\n\n- R73：案例。\n- 这是分析，不是玩家。",
            "典型玩家案例\n\n## 下个章节\n\nR73：原文。",
            "```text\n典型玩家案例｜R73：已存在的代码块。\n```",
            "> 典型玩家案例｜R73：证据引用中的标签。",
            "    典型玩家案例｜R73：缩进代码。",
            "| 典型玩家案例 | 其他 |\n| --- | --- |\n| R73 | 表格 |",
        ]
        for md in samples:
            with self.subTest(md=md):
                self.assertEqual(_player_case_markdown(md), (md, []))

    def test_separate_cases_line_breaks_and_safe_fences(self):
        md = "典型玩家案例\nR73：第一位。\n\n普通结论。\n\n典型玩家案例：R74：第二位含 `代码`。"
        formatted, texts = _player_case_markdown(md)
        self.assertEqual(len(texts), 2)
        self.assertIn("R73", texts[0])
        self.assertIn("R74", texts[1])
        self.assertIn("代码", texts[1])
        self.assertIn("普通结论", formatted)

    def test_core_rebuild_creates_plain_code_only_when_opted_in(self):
        md = f"{CORE_START}\n## 核心结论\n\n### 主题\n\n典型玩家案例\n\nR73：合成案例。\n\n普通结论。\n{CORE_END}"
        original = _extract_feishu_callout_sections(md)
        self.assertNotIn("code_blocks", original[0])
        section = _extract_feishu_callout_sections(md, player_cases=True)[0]
        children = _core_callout_children(section["lines"], code_blocks=section["code_blocks"])
        codes = [b["code"] for b in children if b.get("block_type") == 14]
        self.assertEqual(len(codes), 1)
        self.assertEqual(codes[0]["style"], {"language": 1, "wrap": True})
        self.assertIn("R73：合成案例。", codes[0]["elements"][0]["text_run"]["content"])
        self.assertNotIn("普通结论", codes[0]["elements"][0]["text_run"]["content"])

    def test_case_support_does_not_reformat_other_core_code(self):
        md = f"{CORE_START}\n## 核心结论\n\n```text\n普通代码块\n```\n\n典型玩家案例｜R73：合成案例。\n{CORE_END}"
        section = _extract_feishu_callout_sections(md, player_cases=True)[0]
        children = _core_callout_children(section["lines"], code_blocks=section["code_blocks"])
        codes = [block for block in children if block.get("block_type") == 14]
        self.assertEqual(len(codes), 1)
        self.assertNotIn("普通代码块", repr(codes))
        original = _core_callout_children(["```text", "普通代码块", "```"])
        self.assertEqual(children[:len(original)], original)


class PlayerCaseExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_transforms_body_only_and_never_exports_audit_prompts(self):
        from app.services import export_service as service
        md = "# 合成报告\n\n典型玩家案例｜R73：报告案例。"
        annex = "\n\n## 原文附录\n\n典型玩家案例｜R90：原始证据，不转换。"
        for enabled in [False, True]:
            with self.subTest(enabled=enabled), patch.object(service.feishu_export, "create_doc_via_bot", new=AsyncMock(return_value=("synthetic-url", "doc", "docx"))) as create:
                await service._export_to_feishu(md, {}, player_cases=enabled, evidence_md=annex)
                args, options = create.call_args
                self.assertTrue(args[1].endswith(annex))
                self.assertEqual("player_case_texts" in options, enabled)
                self.assertEqual("```text" in args[1], enabled)
                self.assertNotIn("R90", " ".join(options.get("player_case_texts", [])))

    async def test_case_preparation_failure_retains_body_and_logs_no_source(self):
        from app.services import export_service as service
        md = "# 合成报告\n\n典型玩家案例｜R73：敏感合成内容。"
        with patch.object(service, "_player_case_markdown", side_effect=ValueError("secret-source")), patch.object(service, "runtime_log") as log, patch.object(service.feishu_export, "create_doc_via_bot", new=AsyncMock(return_value=("synthetic-url", "doc", "docx"))) as create:
            await service._export_to_feishu(md, {}, player_cases=True)
        self.assertEqual(create.call_args.args[1], _prep_feishu_export_md(md))
        self.assertNotIn("player_case_texts", create.call_args.kwargs)
        warnings = [call for call in log.call_args_list if "warning" in call.args]
        self.assertEqual(len(warnings), 1)
        self.assertNotIn("secret-source", repr(warnings))
        self.assertNotIn("敏感合成内容", repr(warnings))

    async def test_exact_code_updates_are_batched_and_do_not_rewrite_content(self):
        from app.integrations import feishu_client as client_module
        text = "典型玩家案例\nR73：合成内容。"
        blocks = [{"block_id": "case", "code": {"elements": [{"text_run": {"content": text}}]}},
                  {"block_id": "unrelated", "code": {"elements": [{"text_run": {"content": "unrelated"}}]}}]
        with patch.object(client_module, "_list_doc_blocks", new=AsyncMock(return_value=blocks)), patch.object(client_module, "_doc_edit", new=AsyncMock(return_value={"code": 0})) as edit:
            self.assertTrue(await client_module._apply_player_case_styles(AsyncMock(), {}, "doc", [text]))
        request = edit.call_args.args[4]["requests"]
        self.assertEqual(request, [{"block_id": "case", "update_text_style": {"style": {"language": 1, "wrap": True}}}])
        self.assertNotIn("elements", repr(request))

    async def test_missing_or_rejected_code_keeps_original_text_and_warns(self):
        from app.integrations import feishu_client as client_module
        text = "典型玩家案例\nR73：秘密合成原文。"
        block = {"block_id": "case", "code": {"elements": [{"text_run": {"content": text}}]}}
        for blocks, response in [([], {"code": 0}), ([block], {"code": 403})]:
            with patch.object(client_module, "_list_doc_blocks", new=AsyncMock(return_value=blocks)), patch.object(client_module, "_doc_edit", new=AsyncMock(return_value=response)), patch.object(client_module, "runtime_log") as log:
                self.assertFalse(await client_module._apply_player_case_styles(AsyncMock(), {}, "doc", [text]))
                self.assertTrue(any("warning" in c.args for c in log.call_args_list))
                self.assertNotIn("秘密合成原文", repr(log.call_args_list))

    async def test_large_case_lists_use_batches_and_already_wrapped_codes_need_no_write(self):
        from app.integrations import feishu_client as client_module
        text = "典型玩家案例\nR73：合成案例。"
        blocks = [{"block_id": f"case-{i}", "code": {"elements": [{"text_run": {"content": text}}]}} for i in range(101)]
        with patch.object(client_module, "_list_doc_blocks", new=AsyncMock(return_value=blocks)), patch.object(client_module, "_doc_edit", new=AsyncMock(return_value={"code": 0})) as edit:
            self.assertTrue(await client_module._apply_player_case_styles(AsyncMock(), {}, "doc", [text]))
            self.assertEqual([len(c.args[4]["requests"]) for c in edit.call_args_list], [50, 50, 1])
        blocks[0]["code"]["style"] = {"language": 1, "wrap": True}
        with patch.object(client_module, "_list_doc_blocks", new=AsyncMock(return_value=blocks[:1])), patch.object(client_module, "_doc_edit", new=AsyncMock()) as edit:
            self.assertTrue(await client_module._apply_player_case_styles(AsyncMock(), {}, "doc", [text]))
            edit.assert_not_called()

    async def test_import_passes_case_styles_before_navigation_and_ownership(self):
        from app.integrations import feishu_client as client_module
        calls = []
        client = AsyncMock()
        client.__aenter__.return_value = client
        async def post(url, **kwargs):
            if url.endswith('/files/upload_all'):
                return _FakeResponse({"code": 0, "data": {"file_token": "file"}})
            if url.endswith('/import_tasks'):
                return _FakeResponse({"code": 0, "data": {"ticket": "ticket"}})
            calls.append('owner')
            return _FakeResponse({"code": 0})
        client.post.side_effect = post
        client.get.return_value = _FakeResponse({"code": 0, "data": {"result": {"job_status": 0, "token": "doc", "type": "docx", "url": "synthetic-url"}}})
        async def styles(*args, **kwargs):
            calls.append('styles')
            self.assertEqual(kwargs['player_case_texts'], ['case'])
        async def navigation(*args):
            calls.append('navigation')
        with patch.object(client_module, "_app_access_token", new=AsyncMock(return_value="synthetic")), patch.object(client_module.httpx, "AsyncClient", return_value=client), patch.object(client_module, "apply_report_styles", new=styles), patch.object(client_module, "apply_block_navigation", new=navigation):
            await client_module.create_doc_via_bot('合成报告', '正文', 'synthetic-owner', apply_report_format=True, player_case_texts=['case'], block_navigation={"nodes": []})
        self.assertEqual(calls, ['styles', 'navigation', 'owner'])

    async def test_style_timeout_is_best_effort_and_does_not_retry_per_case(self):
        from app.integrations import feishu_client as client_module
        with patch.object(client_module, "_app_access_token", new=AsyncMock(return_value="synthetic")), patch.object(client_module, "_apply_player_case_styles", new=AsyncMock(side_effect=TimeoutError("secret"))) as apply, patch.object(client_module, "_replace_report_divider", new=AsyncMock(return_value=True)), patch.object(client_module, "runtime_log") as log:
            self.assertFalse(await client_module.apply_report_styles("doc", player_case_texts=["case"]))
        apply.assert_awaited_once()
        self.assertNotIn("secret", repr(log.call_args_list))


if __name__ == "__main__":
    unittest.main()
