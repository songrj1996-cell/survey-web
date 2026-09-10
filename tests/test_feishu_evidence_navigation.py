"""Offline acceptance for Feishu quick-report links; no real service calls."""
import asyncio
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import httpx
import markdown

from app.integrations import feishu_client as feishu
from app.services.export_service import _export_to_feishu
from app.services.feishu_evidence_navigation import prepare_evidence_navigation
from app.services.report_quick_mode import render_quick_report
from app.services.report_render import _prep_feishu_export_md


DOC = "doc-test"
URL = "https://example.invalid/docx/doc-test?from=export#old"
FULL = "## 核心结论\n\n正文。\n\n## 详细分析\n\n### [E1] 普通标题\n\n普通内容。"


def sample_report():
    draft = {
        "title": "导航验收",
        "core": [{"text": "设计 A 的总体评价更高 🧭。", "evidence_ids": ["E1", "E2"]}],
        "risks": [{"text": "少数反馈需核实，不能据此证明因果。", "evidence_ids": ["E2"]}],
        "actions": [{"text": "在不同屏幕尺寸中验证。", "evidence_ids": ["E1"]}],
        "findings": [{
            "title": title, "summary": "主要发现。", "reason": "场景。",
            "exceptions": "少数玩家持不同意见。", "implication": "分析推断。",
            "limitations": "定性材料不能外推。", "evidence_ids": ["E1", "E2"],
        } for title in ("辨识度", "菜单位置")],
    }
    catalog = [
        {"id": "E1", "kind": "statistics", "title": "评分与样本口径", "scope": "问卷 Q1",
         "statistics": "| 方案 | 人数 |\n| --- | --- |\n| A | 12 |\n| B | 8 |", "player_quotes": []},
        {"id": "E2", "kind": "theme", "title": "可见性 *细节* & 尺寸", "scope": "问卷 Q2",
         "statistics": "2 人提及。", "quality": "degraded", "description": "保留风险边界。",
         "player_quotes": [{"quote": "原话 [E1] **不是导航** <内容> &", "source": "匿名样本 S1"}]},
        {"id": "E3", "kind": "theme", "title": "未在正文展开的发现", "statistics": "1 人提及。",
         "player_quotes": [{"quote": "补充反馈。", "source": "匿名样本 S2"}]},
    ]
    return render_quick_report(draft, catalog)[0]


def imported_blocks(md):
    """A separate HTML-based import fixture, including actual list/table nesting.

    IDs deliberately have no relation to evidence numbers or source line numbers.
    This models the API boundary, not Feishu browser acceptance.
    """
    html = markdown.markdown(md, extensions=["tables", "fenced_code"])
    root = ET.fromstring(f"<root>{html}</root>")
    blocks = []
    children = []

    def runs(element, inherited=None):
        style = dict(inherited or {})
        if element.tag == "strong":
            style["bold"] = True
        if element.tag == "em":
            style["italic"] = True
        if element.tag == "code":
            style["inline_code"] = True
        result = []
        if element.text:
            result.append({"text_run": {"content": element.text, "text_element_style": dict(style)}})
        for child in element:
            result.extend(runs(child, style))
            if child.tail:
                result.append({"text_run": {"content": child.tail, "text_element_style": dict(style)}})
        return result

    def add(kind, elements, parent=DOC):
        block_id = f"block-{400 + len(blocks) * 7}"
        blocks.append({"block_id": block_id, "parent_id": parent, kind: {"elements": elements}})
        if parent == DOC:
            children.append(block_id)
        return block_id

    for element in root:
        if element.tag in {"ul", "ol"}:
            for item in element:
                add("ordered" if element.tag == "ol" else "bullet", runs(item))
        elif element.tag == "table":
            table_id = add("table", [])
            for cell in element.iter("td"):
                add("text", runs(cell), table_id)
        elif element.tag == "blockquote":
            for paragraph in element:
                add("quote", runs(paragraph))
        else:
            kind = f"heading{element.tag[1:]}" if element.tag.startswith("h") else "text"
            add(kind, runs(element))
    return [{"block_id": DOC, "children": children}, *blocks]


def plain(block):
    return feishu._block_text(block)


def updated_links(updates):
    return [
        (update["block_id"], element["text_run"]["content"], unquote(element["text_run"]["text_element_style"]["link"]["url"]))
        for update in updates for element in update["update_text_elements"]["elements"]
        if element["text_run"].get("text_element_style", {}).get("link")
    ]


class EvidencePreparationTests(unittest.TestCase):
    def test_full_reports_and_partial_layouts_are_unchanged(self):
        for original in (FULL, "", "## 核心判断\n\n### [E1] 证据", sample_report().replace("## 关键发现", "## 其他")):
            with self.subTest(original=original[:30]):
                self.assertEqual(prepare_evidence_navigation(original), (original, None))

    def test_only_evidence_titles_change_and_every_source_line_survives(self):
        original = _prep_feishu_export_md(sample_report())
        prepared, plan = prepare_evidence_navigation(original)
        self.assertIsNotNone(plan)
        original_lines = [line for line in original.splitlines() if line]
        retained = [line for line in prepared.splitlines() if line and not line.startswith("返回引用（")]
        expected = [f"**{line[4:]}**" if line.startswith("### [E") else line for line in original_lines]
        self.assertEqual(retained, expected)
        self.assertNotIn("### [E", prepared)
        self.assertIn("### 辨识度", prepared)
        self.assertIn("**分析限制：", prepared)
        self.assertIn("返回引用（E3）：返回完整发现目录", prepared)
        self.assertIn("核心判断 1 · 行动建议 1 · 关键发现 1 · 关键发现 2", prepared)
        self.assertIn("少数但需优先核实的反馈 1", prepared)
        self.assertNotIn("https://", prepared)
        self.assertEqual(len([node for node in plan["nodes"] if node["key"].startswith("evidence:")]), 3)

    def test_invalid_ids_and_duplicate_targets_disable_navigation(self):
        report = sample_report()
        for original in (
            report.replace("### [E3]", "### [E2]"),
            report.replace("[E1] [E2]", "[E1] [E99]", 1),
            report.replace("### 完整发现目录", "### 其他目录"),
        ):
            with self.subTest(original=original[-100:]):
                self.assertEqual(prepare_evidence_navigation(original), (original, None))

    def test_quote_literals_and_code_headings_do_not_create_navigation_nodes(self):
        original = sample_report() + "\n\n```\n### [E999] 代码中的标题\n```\n"
        prepared, plan = prepare_evidence_navigation(original)
        self.assertIn("### [E999] 代码中的标题", prepared)
        self.assertFalse(any("E999" in node["key"] for node in plan["nodes"]))
        self.assertFalse(any("原话" in node["text"] for node in plan["nodes"]))


class BlockNavigationTests(unittest.TestCase):
    def setUp(self):
        self.prepared, self.plan = prepare_evidence_navigation(_prep_feishu_export_md(sample_report()))
        self.blocks = imported_blocks(self.prepared)

    def test_round_trip_links_land_on_exact_evidence_and_all_return_locations(self):
        original = deepcopy(self.blocks)
        updates = feishu._block_navigation_updates(self.blocks, DOC, URL, self.plan)
        by_id = {block["block_id"]: block for block in self.blocks}
        links = updated_links(updates)
        self.assertTrue(links)
        for source_id, text, target_url in links:
            self.assertTrue(target_url.startswith("https://example.invalid/docx/doc-test?from=export#block-"))
            target = by_id[target_url.split("#")[1]]
            if text.startswith("[E"):
                self.assertTrue(plain(target).startswith(text + " "))
                self.assertIn("text", target)
            elif text == "返回完整发现目录":
                self.assertIn("bullet", target)
            else:
                self.assertIn("[E", plain(target))
            self.assertNotIn("quote", by_id[source_id])
        # Identical citation lines under two findings must be two distinct blocks.
        finding_sources = {source_id for source_id, text, _ in links if plain(by_id[source_id]) == "证据：[E1] [E2]"}
        self.assertEqual(len(finding_sources), 2)
        self.assertEqual(self.blocks, original)
        for update in updates:
            block = by_id[update["block_id"]]
            text = "".join(element["text_run"]["content"] for element in update["update_text_elements"]["elements"])
            self.assertEqual(text, plain(block))

    def test_root_children_order_not_api_page_order_drives_sections(self):
        expected = feishu._block_navigation_updates(self.blocks, DOC, URL, self.plan)
        shuffled = [self.blocks[0], *reversed(self.blocks[1:])]
        self.assertEqual(feishu._block_navigation_updates(shuffled, DOC, URL, self.plan), expected)

    def test_replay_does_not_duplicate_links_or_change_text(self):
        updates = feishu._block_navigation_updates(self.blocks, DOC, URL, self.plan)
        by_id = {block["block_id"]: block for block in self.blocks}
        for update in updates:
            block = by_id[update["block_id"]]
            kind = next(kind for kind in ("text", "bullet", "ordered") if kind in block)
            block[kind]["elements"] = update["update_text_elements"]["elements"]
        self.assertEqual(feishu._block_navigation_updates(self.blocks, DOC, URL, self.plan), [])

    def test_sixty_evidence_ids_remain_distinct_and_all_links_resolve(self):
        report = sample_report()
        inventory = "\n\n".join(f"- [E{i}] 补充证据 {i}（正文已引用）" for i in range(4, 61))
        appendix = "\n\n".join(f"### [E{i}] 补充证据 {i}\n\n原始材料 {i}。" for i in range(4, 61))
        report = report.replace("### [E1]", inventory + "\n\n### [E1]", 1)
        report = report.replace("### 阅读与统计口径", appendix + "\n\n### 阅读与统计口径")
        report = report.replace("[E1] [E2]", " ".join(f"[E{i}]" for i in range(1, 61)), 1)
        prepared, plan = prepare_evidence_navigation(_prep_feishu_export_md(report))
        blocks = imported_blocks(prepared)
        updates = feishu._block_navigation_updates(blocks, DOC, URL, plan)
        by_id = {block["block_id"]: block for block in blocks}
        core_id = next(block["block_id"] for block in blocks if plain(block).startswith("设计 A"))
        links = [(text, target) for source, text, target in updated_links(updates) if source == core_id]
        self.assertEqual(len(links), 60)
        self.assertEqual(len({target for _, target in links}), 60)
        for text, target in links:
            self.assertTrue(plain(by_id[target.split("#")[1]]).startswith(text + " "))

    def test_existing_equivalent_decoded_link_is_kept(self):
        elements = [{"text_run": {"content": "[E1]", "text_element_style": {"link": {"url": "https://example.invalid/doc#block"}}}}]
        linked = feishu._link_text_elements(elements, [{"start": 0, "end": 4, "target": "target"}], {"target": "https%3A%2F%2Fexample.invalid%2Fdoc%23block"})
        self.assertEqual(linked, elements)

    def test_missing_ambiguous_changed_or_incomplete_blocks_fail_before_writes(self):
        evidence = next(block for block in self.blocks if plain(block).startswith("[E2]"))
        changed = deepcopy(self.blocks)
        altered = next(block for block in changed if block["block_id"] == evidence["block_id"])
        kind = next(key for key in ("text", "bullet") if key in altered)
        altered[kind]["elements"] = [{"text_run": {"content": "changed"}}]
        missing = [block for block in self.blocks if block is not evidence]
        duplicate = deepcopy(evidence)
        duplicate["block_id"] = "duplicated-target"
        ambiguous = deepcopy(self.blocks) + [duplicate]
        original_position = ambiguous[0]["children"].index(evidence["block_id"])
        ambiguous[0]["children"].insert(original_position + 1, duplicate["block_id"])
        for blocks in (missing, ambiguous, changed):
            with self.assertRaises(ValueError):
                feishu._block_navigation_updates(blocks, DOC, URL, self.plan)
        altered_plan = deepcopy(self.plan)
        altered_plan["headings"][0]["text"] = "changed heading"
        with self.assertRaises(ValueError):
            feishu._block_navigation_updates(self.blocks, DOC, URL, altered_plan)

    def test_split_runs_unicode_styles_and_existing_links_are_preserved(self):
        elements = [
            {"text_run": {"content": "🧭前文 [", "text_element_style": {"bold": True, "comment_ids": ["comment"]}}},
            {"text_run": {"content": "E1]", "text_element_style": {"italic": True}}},
            {"text_run": {"content": " 既有链接", "text_element_style": {"link": {"url": "existing"}}}},
        ]
        before = deepcopy(elements)
        linked = feishu._link_text_elements(elements, [{"start": 4, "end": 8, "target": "target"}], {"target": "new"})
        self.assertEqual("".join(item["text_run"]["content"] for item in linked), "🧭前文 [E1] 既有链接")
        self.assertEqual(linked[-1], elements[-1])
        self.assertEqual(linked[1]["text_run"]["text_element_style"]["comment_ids"], ["comment"])
        self.assertTrue(linked[2]["text_run"]["text_element_style"]["italic"])
        self.assertEqual(elements, before)
        with self.assertRaises(ValueError):
            feishu._link_text_elements(elements, [{"start": 8, "end": 10, "target": "target"}], {"target": "new"})


class NavigationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_paginated_read_batch_update_and_rate_limit_retry(self):
        prepared, plan = prepare_evidence_navigation(_prep_feishu_export_md(sample_report()))
        blocks = imported_blocks(prepared)
        requests = []
        attempts = 0

        def handler(request):
            nonlocal attempts
            requests.append(request)
            if request.method == "GET":
                second = "page_token" in request.url.params
                return httpx.Response(200, json={"code": 0, "data": {
                    "items": blocks[4:] if second else blocks[:4], "has_more": not second, "page_token": "next" if not second else "",
                }})
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, json={"code": 99991400})
            return httpx.Response(200, json={"code": 0})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(feishu, "_app_access_token", AsyncMock(return_value="fake-token")), patch.object(feishu.httpx, "AsyncClient", return_value=client), patch.object(feishu.asyncio, "sleep", AsyncMock()):
            self.assertTrue(await feishu.apply_block_navigation(DOC, URL, plan))
        self.assertEqual([request.method for request in requests], ["GET", "GET", "PATCH", "PATCH"])
        self.assertEqual(json.loads(requests[-1].content), json.loads(requests[-2].content))

    async def test_api_failures_and_timeout_keep_text_and_stop_processing(self):
        prepared, plan = prepare_evidence_navigation(_prep_feishu_export_md(sample_report()))
        blocks = imported_blocks(prepared)
        for failure in ("read", "write", "timeout"):
            requests = []

            async def handler(request):
                requests.append(request.method)
                if failure == "timeout":
                    await asyncio.sleep(0.05)
                if request.method == "GET":
                    return httpx.Response(200, json={"code": 403 if failure == "read" else 0, "data": {"items": blocks, "has_more": False}})
                return httpx.Response(403, json={"code": 1770032})

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            started = time.monotonic()
            with patch.object(feishu, "_app_access_token", AsyncMock(return_value="fake-token")), patch.object(feishu.httpx, "AsyncClient", return_value=client):
                self.assertFalse(await feishu.apply_block_navigation(DOC, URL, plan, timeout_seconds=0.01 if failure == "timeout" else 1))
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertFalse(any(method in ("POST", "DELETE") for method in requests))
            self.assertEqual(requests, ["GET", "PATCH"] if failure == "write" else ["GET"])

    async def test_more_than_one_batch_stops_on_partial_failure(self):
        blocks = [{"block_id": "target", "parent_id": DOC, "text": {"elements": [{"text_run": {"content": "destination"}}]}}]
        plan = {"headings": [], "nodes": [{"key": "target", "section": -1, "kind": "text", "text": "destination", "links": []}]}
        for index in range(205):
            text = f"source {index}"
            blocks.append({"block_id": f"source-{index}", "parent_id": DOC, "text": {"elements": [{"text_run": {"content": text}}]}})
            plan["nodes"].append({"key": text, "section": -1, "kind": "text", "text": text, "links": [{"start": 0, "end": len(text), "target": "target"}]})
        writes = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"code": 0, "data": {"items": blocks}})
            writes.append(json.loads(request.content))
            return httpx.Response(200, json={"code": 0 if len(writes) == 1 else 1770032})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with patch.object(feishu, "_app_access_token", AsyncMock(return_value="fake-token")), patch.object(feishu.httpx, "AsyncClient", return_value=client):
            self.assertFalse(await feishu.apply_block_navigation(DOC, URL, plan))
        self.assertEqual([len(write["requests"]) for write in writes], [100, 100])

    async def test_import_formats_then_links_before_owner_transfer_even_on_link_failure(self):
        events = []

        def handler(request):
            if request.url.path.endswith("upload_all"):
                data = {"file_token": "uploaded"}
            elif request.url.path.endswith("import_tasks"):
                data = {"ticket": "ticket"}
            elif "/import_tasks/" in request.url.path:
                data = {"result": {"job_status": 0, "token": DOC, "type": "docx", "url": URL}}
            else:
                events.append("owner")
                data = {}
            return httpx.Response(200, json={"code": 0, "data": data})

        real_client = httpx.AsyncClient
        async def format_doc(*args):
            events.append("format")
        async def link_doc(*args):
            events.append("links")
            return False
        with patch.object(feishu, "_app_access_token", AsyncMock(return_value="fake-token")), patch.object(feishu.httpx, "AsyncClient", side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(handler))), patch.object(feishu, "apply_report_styles", side_effect=format_doc), patch.object(feishu, "apply_block_navigation", side_effect=link_doc):
            result = await feishu.create_doc_via_bot("测试", "正文", "fake-user", apply_report_format=True, block_navigation={"headings": [], "nodes": []})
        self.assertEqual(result, (URL, DOC, "docx"))
        self.assertEqual(events, ["format", "links", "owner"])

    async def test_export_service_only_enables_navigation_for_quick_reports(self):
        for report in (sample_report(), FULL):
            create = AsyncMock(return_value=(URL, DOC, "docx"))
            with patch.object(feishu, "create_doc_via_bot", create):
                self.assertEqual(await _export_to_feishu(report, {}, title="导出测试"), URL)
            kwargs = create.call_args.kwargs
            self.assertEqual("block_navigation" in kwargs, report != FULL)
            if report != FULL:
                self.assertNotIn("### [E", create.call_args.args[1])
            else:
                self.assertIn("### [E1] 普通标题", create.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
