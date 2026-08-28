import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.services import report_history
from app.services import survey_service
from app.storage import history as history_storage
from app.services.report_engine import (
    _describe_qa_context_scope,
    _resolve_core_coverage_review,
)


def _analysis_focus() -> dict:
    return {
        "core_question": "聊天消息丢失是否需要优先处理",
        "report_organization": "按影响与风险组织，而不是逐题平铺",
        "supporting_analyses": ["确认发生场景"],
        "evidence_role": "玩家反馈用于解释丢失场景",
        "expected_deliverables": ["修复优先级判断"],
        "avoid_structures": ["不要只复述题目"],
    }


def _streamed_chunk_text(events: list[str]) -> str:
    chunks = []
    for event in events:
        if not event.startswith("data: "):
            continue
        payload = json.loads(event.removeprefix("data: ").strip())
        if payload.get("type") == "chunk":
            chunks.append(payload.get("content", ""))
    return "".join(chunks)


def _event_payloads(events: list[str]) -> list[dict]:
    return [
        json.loads(event.removeprefix("data: ").strip())
        for event in events
        if event.startswith("data: ")
    ]


VIEWPOINT_STATS_MD = (
    "<subjective_viewpoint_stats>\n"
    "观点：消息会消失；提及情况：1名玩家提及，占相关有效回答玩家的100.0%\n"
    "</subjective_viewpoint_stats>"
)

ACTION_SECTION_MD = (
    "## 行动建议\n\n"
    "1. **修复消息丢失**（优先级：高）\n"
    "   - **核心判断：** 消息丢失需要优先验证。\n"
    "   - **产品动作：** 排查消息链路。\n"
    "   - **验证方式：** 对比修复前后的丢失率。\n"
    "   - **依据：** 玩家反馈消息会消失。\n"
    "   - **不确定性/前提：** 仍需确认发生范围。"
)


def _core_repair(original: str, replacement: str) -> str:
    return (
        "<!--CORE_REPAIRS_START-->\n"
        "<!--CORE_REPAIR_START-->\n"
        f"<original>\n{original}\n</original>\n"
        f"<replacement>\n{replacement}\n</replacement>\n"
        "<!--CORE_REPAIR_END-->\n"
        "<!--CORE_REPAIRS_END-->"
    )


async def _stub_qualitative_analysis(*_args, **_kwargs):
    yield ("result", {1: {"col_name": "聊天反馈", "themes": []}})


async def _stub_report_viewpoint_stats(*_args, **_kwargs):
    yield ("result", [{
        "id": "RVIEW:t01",
        "name": "消息会消失",
        "count": 1,
        "denominator": 1,
        "percentage": 100.0,
        "source_questions": ["聊天反馈"],
    }])


class DirectReportServiceTests(unittest.IsolatedAsyncioTestCase):
    def test_core_coverage_review_pass_and_invalid_outputs_preserve_original(self):
        original = (
            "<!--CORE_START-->\n"
            "## 核心结论\n"
            "原始核心判断。\n\n"
            "需要完整保留的原因与场景。\n"
            "<!--CORE_END-->"
        )

        self.assertEqual(_resolve_core_coverage_review(original, "PASS"), original)
        self.assertEqual(_resolve_core_coverage_review(original, "  PASS\n"), original)
        for invalid in (
            "",
            "PASS\n补充说明",
            "## 核心结论\n缺少完整标记",
            "<!--CORE_START-->\n## 核心结论\n缺少结束标记",
            _core_repair("不存在的原句。", "替换内容。"),
            _core_repair("原始核心判断。", "针对这个问题，改写判断。"),
            _core_repair("原始核心判断。", "## 核心结论\n替换全部内容。"),
            _core_repair("原始核心判断。", ""),
        ):
            with self.subTest(invalid=invalid):
                self.assertEqual(
                    _resolve_core_coverage_review(original, invalid),
                    original,
                )

    def test_core_coverage_review_applies_only_unique_local_repairs(self):
        original = (
            "<!--CORE_START-->\n"
            "## 核心结论\n"
            "### 观看渠道与产品机会\n"
            "1. **外部观看占主导**：51名玩家选择官方赛事直播。\n\n"
            "2. **游戏内场景仍有价值**：玩家提到学英雄、等好友和支持好友。\n\n"
            "### 体验问题\n"
            "聊天区存在广告和诈骗内容，影响观看体验。\n"
            "<!--CORE_END-->"
        )
        review = _core_repair(
            "1. **外部观看占主导**：51名玩家选择官方赛事直播。",
            (
                "1. **外部观看占主导，但产品机会要看具体场景**：51名玩家选择官方赛事直播。\n"
                "   - 玩家同时提到学英雄、等好友和支持好友等游戏内观看场景。\n"
                "   - **分析推断**：游戏内观看更适合承接与游戏行为紧密相连的短时、社交场景；"
                "仍需进一步验证使用频次。"
            ),
        )

        resolved = _resolve_core_coverage_review(original, review)

        self.assertIn("产品机会要看具体场景", resolved)
        self.assertIn("**分析推断**", resolved)
        self.assertIn("2. **游戏内场景仍有价值**", resolved)
        self.assertIn("聊天区存在广告和诈骗内容", resolved)
        self.assertEqual(resolved.count("<!--CORE_START-->"), 1)
        self.assertEqual(resolved.count("<!--CORE_END-->"), 1)

    def test_core_coverage_review_can_promote_decision_rule_without_rewriting_details(self):
        original = (
            "<!--CORE_START-->\n"
            "## 核心结论\n"
            "本次调研共收集52份有效回复。\n"
            "### 总体判断\n"
            "<u>方案1获得最多第一名，方案3满意度最高。</u>\n\n"
            "玩家还会结合切换聊天对象是否方便、新功能是否真正有用来权衡方案。\n\n"
            "### 其他体验问题\n"
            "部分玩家反馈聊天入口不够明显。\n"
            "<!--CORE_END-->"
        )
        review = _core_repair(
            "<u>方案1获得最多第一名，方案3满意度最高。</u>",
            (
                "三个方案的选择首先取决于 **是否保留旧习惯、聊天是否清晰、找队友是否更快、操作是否更少**，"
                "而不是单看某一项排名。方案1获得最多第一名，方案3满意度最高。"
            ),
        )

        resolved = _resolve_core_coverage_review(original, review)

        first_judgment = resolved.index("三个方案的选择首先取决于")
        supporting_detail = resolved.index("玩家还会结合切换聊天对象是否方便")
        self.assertLess(first_judgment, supporting_detail)
        self.assertNotIn("<u>方案1获得最多第一名", resolved)
        self.assertIn("**是否保留旧习惯、聊天是否清晰、找队友是否更快、操作是否更少**", resolved)
        self.assertIn("### 其他体验问题\n部分玩家反馈聊天入口不够明显。", resolved)

    def test_core_coverage_review_rejects_ambiguous_or_overbroad_repairs(self):
        repeated = (
            "<!--CORE_START-->\n## 核心结论\n"
            "相同句子。\n\n相同句子。\n<!--CORE_END-->"
        )
        self.assertEqual(
            _resolve_core_coverage_review(repeated, _core_repair("相同句子。", "新句子。")),
            repeated,
        )

        long_paragraph = "需保留的长段落。" * 220
        overbroad = f"<!--CORE_START-->\n## 核心结论\n{long_paragraph}\n<!--CORE_END-->"
        self.assertEqual(
            _resolve_core_coverage_review(
                overbroad,
                _core_repair(long_paragraph, "被过度概括。"),
            ),
            overbroad,
        )

    def test_qa_context_scope_reports_full_sampled_and_missing_feedback(self):
        full_context = '<qa_context><rows>\n{"id": "p-1"}\n{"id": "p-2"}\n</rows></qa_context>'
        sampled_context = (
            '<qa_context><rows>\n# 原始数据共 2418 行，超出上下文上限，已按画像维度分层抽样到 100 行。\n'
            '{"id": "p-1"}\n</rows></qa_context>'
        )
        missing_context = '<qa_context><rows>（无数据）</rows></qa_context>'

        self.assertIn('全部 2 条原始玩家反馈', _describe_qa_context_scope(full_context))
        self.assertIn('抽样的 100 条原始玩家反馈（原始共 2418 条）', _describe_qa_context_scope(sampled_context))
        self.assertIn('未保留可用的原始玩家反馈', _describe_qa_context_scope(missing_context))

    async def test_qa_stream_emits_scope_before_answer(self):
        qa_context = '<qa_context><rows>\n{"id": "p-1"}\n</rows></qa_context>'
        sess = {
            "report_md": "# 报告",
            "qa_context_md": qa_context,
            "analyst_conv_id": "",
            "qa_messages": [],
        }
        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(
                survey_service,
                "_answer_qa_direct",
                new=AsyncMock(return_value=("回答内容", "claude-sonnet-5", qa_context)),
            ),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
        ):
            events = [event async for event in survey_service.qa_stream("sid", "问题", object())]

        self.assertIn('"type": "qa_scope"', events[0])
        self.assertIn('全部 1 条原始玩家反馈', events[0])
        self.assertIn('"type": "chunk"', events[1])
        self.assertEqual(sess["qa_provider"], "direct_llm")
        self.assertEqual(sess["qa_model"], "claude-sonnet-5")

    async def test_valid_core_review_replaces_only_core_and_builds_qa_context(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "消息会消失"]],
            "plan": {
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "branch_rules": [],
                "analysis_focus": _analysis_focus(),
            },
            "branch_rules": [],
            "stats_md": "有效样本(总计):总体=1",
            "open_text": {
                1: [{"ids": {"玩家ID": "p-1"}, "profile": {}, "text": "消息会消失"}],
            },
        }
        direct = AsyncMock(side_effect=[
            ("# 聊天功能调研", "model-a"),
            ("## Part 1 聊天体验\n\n本节总结。", "model-a"),
            ("NONE", "model-a"),
            (
                "<!--CORE_START-->\n## 核心结论\n旧核心判断。\n<!--CORE_END-->",
                "model-a",
            ),
            (
                _core_repair("旧核心判断。", "覆盖审校后的核心判断。"),
                "model-a",
            ),
            (ACTION_SECTION_MD, "model-a"),
        ])

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(
                survey_service,
                "_batch_qualitative_analysis",
                new=_stub_qualitative_analysis,
            ),
            patch.object(
                survey_service,
                "build_report_viewpoint_stats",
                new=_stub_report_viewpoint_stats,
            ),
            patch.object(
                survey_service,
                "render_viewpoint_stats",
                return_value=VIEWPOINT_STATS_MD,
            ),
            patch.object(survey_service, "_direct_writer_round", new=direct),
            patch.object(survey_service, "save_session") as save_session,
            patch.object(survey_service, "save_to_history") as save_history,
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertEqual(direct.await_count, 6)
        self.assertTrue(any('"type": "report_done"' in event for event in events))
        progress = [
            payload
            for payload in _event_payloads(events)
            if payload.get("type") == "analysis_progress"
        ]
        self.assertEqual(
            {payload.get("phase") for payload in progress},
            {"themes", "synthesis", "writing", "finalize"},
        )
        self.assertTrue(any(
            payload.get("phase") == "writing" and payload.get("status") == "active"
            for payload in progress
        ))
        self.assertTrue(any(
            payload.get("phase") == "finalize" and payload.get("status") == "completed"
            for payload in progress
        ))
        self.assertEqual(sess["report_writer_provider"], "direct_llm")
        self.assertEqual(sess["report_writer_model"], "model-a")
        self.assertEqual(sess["analyst_conv_id"], "")
        self.assertFalse(sess["rows_fed"])
        self.assertIn("<report>", sess["qa_context_md"])
        self.assertIn("聊天功能调研", sess["qa_context_md"])
        self.assertIn("消息会消失", sess["qa_context_md"])
        self.assertIn("覆盖审校后的核心判断", sess["report_md"])
        self.assertNotIn("旧核心判断", sess["report_md"])
        streamed_text = _streamed_chunk_text(events)
        self.assertIn("覆盖审校后的核心判断", streamed_text)
        self.assertNotIn("旧核心判断", streamed_text)
        self.assertIn("## Part 1 聊天体验", sess["report_md"])
        self.assertIn("## 行动建议", sess["report_md"])
        save_session.assert_called_once()
        save_history.assert_called_once()

    async def test_core_review_failure_keeps_original_and_continues_report(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "消息会消失"]],
            "plan": {
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "branch_rules": [],
                "analysis_focus": _analysis_focus(),
            },
            "branch_rules": [],
            "stats_md": "有效样本(总计):总体=1",
            "open_text": {
                1: [{"ids": {"玩家ID": "p-1"}, "profile": {}, "text": "消息会消失"}],
            },
        }
        original_core = (
            "<!--CORE_START-->\n## 核心结论\n原始核心判断。\n<!--CORE_END-->"
        )
        direct = AsyncMock(side_effect=[
            ("# 聊天功能调研", "model-a"),
            ("## Part 1 聊天体验\n\n本节总结。", "model-a"),
            ("NONE", "model-a"),
            (original_core, "model-a"),
            RuntimeError("review unavailable"),
            (ACTION_SECTION_MD, "model-a"),
        ])

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(
                survey_service,
                "_batch_qualitative_analysis",
                new=_stub_qualitative_analysis,
            ),
            patch.object(
                survey_service,
                "build_report_viewpoint_stats",
                new=_stub_report_viewpoint_stats,
            ),
            patch.object(
                survey_service,
                "render_viewpoint_stats",
                return_value=VIEWPOINT_STATS_MD,
            ),
            patch.object(survey_service, "_direct_writer_round", new=direct),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertEqual(direct.await_count, 6)
        self.assertTrue(any("核心结论证据复核未完成" in event for event in events))
        self.assertTrue(any('"type": "report_done"' in event for event in events))
        self.assertIn("原始核心判断", sess["report_md"])
        self.assertIn("原始核心判断", _streamed_chunk_text(events))
        self.assertIn("## 行动建议", sess["report_md"])

    async def test_action_section_is_repaired_without_streaming_invalid_attempt(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "消息会消失"]],
            "plan": {
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "branch_rules": [],
            },
            "branch_rules": [],
            "stats_md": "有效样本(总计):总体=1",
            "open_text": {
                1: [{"ids": {"玩家ID": "p-1"}, "profile": {}, "text": "消息会消失"}],
            },
        }
        direct = AsyncMock(side_effect=[
            ("# 聊天功能调研", "model-a"),
            ("## Part 1 聊天体验\n\n本节总结。", "model-a"),
            ("NONE", "model-a"),
            ("<!--CORE_START-->\n## 核心结论\n样本总数 1。\n<!--CORE_END-->", "model-a"),
            ("PASS", "model-a"),
            ("无标题的旧建议", "model-a"),
            (ACTION_SECTION_MD.replace("## 行动建议", "### 行动建议（修正版）"), "model-a"),
        ])

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(
                survey_service,
                "_batch_qualitative_analysis",
                new=_stub_qualitative_analysis,
            ),
            patch.object(
                survey_service,
                "build_report_viewpoint_stats",
                new=_stub_report_viewpoint_stats,
            ),
            patch.object(
                survey_service,
                "render_viewpoint_stats",
                return_value=VIEWPOINT_STATS_MD,
            ),
            patch.object(survey_service, "_direct_writer_round", new=direct),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertEqual(direct.await_count, 7)
        self.assertIn("不要改变建议", direct.await_args_list[-1].args[1])
        self.assertIn("## 行动建议\n\n1. **修复消息丢失**", sess["report_md"])
        self.assertNotIn("| 建议内容 |", sess["report_md"])
        self.assertNotIn("无标题的旧建议", "".join(events))
        self.assertTrue(any("行动建议格式校验中" in event for event in events))

    async def test_slow_direct_writer_sends_heartbeat_without_partial_content(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "消息会消失"]],
            "plan": {
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "branch_rules": [],
            },
            "branch_rules": [],
            "stats_md": "有效样本(总计):总体=1",
            "open_text": {
                1: [{"ids": {"玩家ID": "p-1"}, "profile": {}, "text": "消息会消失"}],
            },
        }
        answers = iter([
            ("# 聊天功能调研", "model-a"),
            ("## Part 1 聊天体验\n\n本节总结。", "model-a"),
            ("NONE", "model-a"),
            ("<!--CORE_START-->\n## 核心结论\n样本总数 1。\n<!--CORE_END-->", "model-a"),
            ("PASS", "model-a"),
            (ACTION_SECTION_MD, "model-a"),
        ])

        async def slow_writer(*_args):
            import asyncio
            await asyncio.sleep(0.01)
            return next(answers)

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(
                survey_service,
                "_batch_qualitative_analysis",
                new=_stub_qualitative_analysis,
            ),
            patch.object(
                survey_service,
                "build_report_viewpoint_stats",
                new=_stub_report_viewpoint_stats,
            ),
            patch.object(
                survey_service,
                "render_viewpoint_stats",
                return_value=VIEWPOINT_STATS_MD,
            ),
            patch.object(survey_service, "_direct_writer_round", side_effect=slow_writer),
            patch.object(survey_service, "LLM_STREAM_HEARTBEAT_SECONDS", 0.001),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertTrue(any('"type": "heartbeat"' in event for event in events))
        self.assertTrue(any('"type": "report_done"' in event for event in events))
        self.assertIn("## 行动建议", sess["report_md"])

    async def test_standard_report_passes_viewpoint_stats_through_final_writer_prompt(self):
        sess = {
            "filename": "responses.xlsx",
            "rows": [["玩家ID", "聊天反馈"], ["p-1", "消息会消失"]],
            "plan": {
                "columns": [{"index": 1, "name": "聊天反馈", "role": "open_text"}],
                "parts": [{"name": "聊天体验", "column_indexes": [1]}],
                "branch_rules": [],
            },
            "branch_rules": [],
            "stats_md": "有效样本(总计):总体=1",
            "open_text": {
                1: [{"ids": {"玩家ID": "p-1"}, "profile": {}, "text": "消息会消失"}],
            },
        }
        answers = iter([
            ("# 聊天功能调研", "model-a"),
            (
                "## Part 1 聊天体验\n\n本节总结。\n\n"
                "**观点：消息会消失**\n\n- **主要发现**：需要处理。",
                "model-a",
            ),
            ("NONE", "model-a"),
            (
                "<!--CORE_START-->\n## 核心结论\n消息丢失需要处理。\n<!--CORE_END-->",
                "model-a",
            ),
            ("PASS", "model-a"),
            (ACTION_SECTION_MD, "model-a"),
        ])
        writer_calls: list[tuple[list[dict], str]] = []

        async def capture_writer(messages, query):
            writer_calls.append((deepcopy(messages), query))
            answer, model = next(answers)
            messages.extend([
                {"role": "user", "content": query},
                {"role": "assistant", "content": answer},
            ])
            return answer, model

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(
                survey_service,
                "_batch_qualitative_analysis",
                new=_stub_qualitative_analysis,
            ),
            patch.object(
                survey_service,
                "build_report_viewpoint_stats",
                new=_stub_report_viewpoint_stats,
            ),
            patch.object(
                survey_service,
                "render_viewpoint_stats",
                return_value=VIEWPOINT_STATS_MD,
            ),
            patch.object(survey_service, "_direct_writer_round", new=capture_writer),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertTrue(any('"type": "report_done"' in event for event in events))
        self.assertEqual(len(writer_calls), 6)
        self.assertIn(VIEWPOINT_STATS_MD, writer_calls[0][1])
        self.assertIn("事实边界也必须逐条复核", writer_calls[-2][1])
        final_messages, final_query = writer_calls[-1]
        final_prompt = "\n".join(
            [*(message["content"] for message in final_messages), final_query]
        )
        self.assertIn(VIEWPOINT_STATS_MD, final_prompt)
        diagnostics = sess["report_versions"][-1]["viewpoint_diagnostics"]
        self.assertEqual(diagnostics["catalog"]["entry_count"], 1)
        self.assertTrue(diagnostics["writer_context"]["included"])
        self.assertEqual(
            diagnostics["writer_output"]["status"], "writer_omission"
        )
        self.assertEqual(
            diagnostics["writer_output"]["viewpoint_block_count"], 1
        )
        self.assertEqual(
            diagnostics["writer_output"]["mention_block_count"], 0
        )

    async def test_direct_qa_uses_context_history_and_configured_model_chain(self):
        source = {
            "report_md": "# 报告\n\n## 核心结论\n消息丢失需要优先处理。",
            "stats_md": "有效样本(总计):总体=2",
            "plan": {"columns": [], "parts": []},
            "rows": [["玩家ID", "反馈"], ["p-1", "消息丢失"]],
            "qa_messages": [
                {"role": "user", "content": "上一个问题"},
                {"role": "ai", "content": "上一个回答"},
            ],
        }
        collect = AsyncMock(return_value=("基于报告和原始反馈的回答", "gpt-5.6-sol"))

        with (
            patch.object(survey_service, "collect_chat_completion", new=collect),
            patch.object(survey_service, "_get_report_qa_system_prompt", return_value="QA rules"),
            patch.object(survey_service, "LLM_QA_MODEL", "claude-sonnet-5"),
            patch.object(survey_service, "LLM_QA_FALLBACK_MODELS", ("gpt-5.6-sol",)),
            patch.object(survey_service, "LLM_QA_MAX_TOKENS", 16000),
            patch.object(survey_service, "LLM_QA_REASONING", "medium"),
        ):
            answer, model, context = await survey_service._answer_qa_direct(
                source, "这个结论依据什么？"
            )

        self.assertEqual(answer, "基于报告和原始反馈的回答")
        self.assertEqual(model, "gpt-5.6-sol")
        self.assertIn("消息丢失需要优先处理", context)
        messages = collect.await_args.args[0]
        self.assertEqual(messages[0], {"role": "system", "content": "QA rules"})
        self.assertIn("<report>", messages[1]["content"])
        self.assertEqual(messages[2], {"role": "user", "content": "上一个问题"})
        self.assertEqual(messages[3], {"role": "assistant", "content": "上一个回答"})
        self.assertEqual(messages[4], {"role": "user", "content": "这个结论依据什么？"})
        self.assertEqual(
            collect.await_args.kwargs,
            {
                "models": ("claude-sonnet-5", "gpt-5.6-sol"),
                "max_tokens": 16000,
                "reasoning_effort": "medium",
            },
        )

    async def test_direct_qa_rebuilds_cached_context_when_raw_rows_are_available(self):
        source = {
            "report_md": "# 报告",
            "stats_md": "总体=1",
            "plan": {
                "columns": [{"index": 1, "name": "概念1整体满意度", "role": "scale"}],
                "parts": [],
            },
            "rows": [["玩家ID", "满意度"], ["p-1", "5"]],
            "qa_context_md": "<qa_context><rows>旧的有损上下文</rows></qa_context>",
        }
        collect = AsyncMock(return_value=("已刷新", "qa-model"))

        with patch.object(survey_service, "collect_chat_completion", new=collect):
            answer, model, context = await survey_service._answer_qa_direct(source, "请核对")

        self.assertEqual(answer, "已刷新")
        self.assertEqual(model, "qa-model")
        self.assertNotIn("旧的有损上下文", context)
        self.assertIn('"满意度": "5"', context)

    async def test_direct_qa_keeps_cached_context_when_raw_rows_are_unavailable(self):
        cached = "<qa_context><report>历史报告</report><rows>历史上下文</rows></qa_context>"
        source = {"qa_context_md": cached}
        collect = AsyncMock(return_value=("历史回答", "qa-model"))

        with patch.object(survey_service, "collect_chat_completion", new=collect):
            _, _, context = await survey_service._answer_qa_direct(source, "请核对")

        self.assertEqual(context, cached)

    async def test_history_qa_allows_direct_report_without_conversation_id(self):
        entry = {
            "id": "history-1",
            "report_md": "# 已归档报告",
            "analyst_conv_id": "",
            "analyst_app": "standard",
        }
        history = [entry]
        with (
            patch.object(survey_service, "_load_history", return_value=history),
            patch.object(survey_service, "_find_history_for_login", return_value=entry),
            patch.object(survey_service, "LLM_API_KEY", "llm-key"),
            patch.object(survey_service, "LLM_QA_MODEL", "claude-sonnet-5"),
        ):
            result = survey_service.prepare_history_qa_context("history-1", None)

        self.assertEqual(result, history)

    def test_history_archive_persists_direct_writer_and_qa_context(self):
        sess = {
            "filename": "responses.xlsx",
            "report_md": "# 报告",
            "plan": {"columns": [], "parts": []},
            "stats_md": "总体=2",
            "qa_context_md": "<qa_context>完整上下文</qa_context>",
            "report_writer_provider": "direct_llm",
            "report_writer_model": "model-a",
            "qa_provider": "direct_llm",
            "qa_model": "claude-sonnet-5",
            "rows_fed": False,
            "rows": [["id"], ["1"], ["2"]],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            history_path = Path(tmpdir) / "history.json"
            with patch.object(history_storage, "HISTORY_FILE", str(history_path)):
                report_history.save_to_history("sid", sess)
            saved = json.loads(history_path.read_text(encoding="utf-8"))

        self.assertEqual(saved[0]["qa_context_md"], sess["qa_context_md"])
        self.assertEqual(saved[0]["report_writer_provider"], "direct_llm")
        self.assertEqual(saved[0]["report_writer_model"], "model-a")
        self.assertEqual(saved[0]["qa_provider"], "direct_llm")
        self.assertEqual(saved[0]["qa_model"], "claude-sonnet-5")
        self.assertFalse(saved[0]["rows_fed"])


if __name__ == "__main__":
    unittest.main()
