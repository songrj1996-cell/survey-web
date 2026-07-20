import unittest
from unittest.mock import AsyncMock, patch

from app.services import report_history
from app.services import survey_service


class DirectReportServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_standard_report_uses_direct_writer_and_builds_qa_context(self):
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
            ("## 行动建议\n\n**修复消息丢失**", "model-a"),
        ])

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(survey_service, "_direct_writer_round", new=direct),
            patch.object(survey_service, "save_session") as save_session,
            patch.object(survey_service, "save_to_history") as save_history,
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
            patch.object(
                survey_service,
                "sse_dify_stream",
                side_effect=AssertionError("report writer must not call Dify"),
            ),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertEqual(direct.await_count, 5)
        self.assertTrue(any('"type": "report_done"' in event for event in events))
        self.assertEqual(sess["report_writer_provider"], "direct_llm")
        self.assertEqual(sess["report_writer_model"], "model-a")
        self.assertEqual(sess["analyst_conv_id"], "")
        self.assertFalse(sess["rows_fed"])
        self.assertIn("<report>", sess["qa_context_md"])
        self.assertIn("聊天功能调研", sess["qa_context_md"])
        self.assertIn("消息会消失", sess["qa_context_md"])
        save_session.assert_called_once()
        save_history.assert_called_once()

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
            ("无标题的旧建议", "model-a"),
            ("### 行动建议（修正版）\n\n**修复消息丢失**", "model-a"),
        ])

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
            patch.object(survey_service, "_direct_writer_round", new=direct),
            patch.object(survey_service, "save_session"),
            patch.object(survey_service, "save_to_history"),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch.object(survey_service.survey_stats, "find_numbers_not_in_stats", return_value=[]),
        ):
            events = [event async for event in survey_service.report_stream("sid", object())]

        self.assertEqual(direct.await_count, 6)
        self.assertIn("不要改变建议", direct.await_args_list[-1].args[1])
        self.assertIn("## 行动建议\n\n**修复消息丢失**", sess["report_md"])
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
            ("## 行动建议\n\n**修复消息丢失**", "model-a"),
        ])

        async def slow_writer(*_args):
            import asyncio
            await asyncio.sleep(0.01)
            return next(answers)

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(survey_service, "_current_login", new=AsyncMock(return_value=None)),
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

    async def test_qa_failure_rebuilds_dify_conversation_from_full_context(self):
        source = {
            "report_md": "# 报告\n\n## 核心结论\n消息丢失需要优先处理。",
            "stats_md": "有效样本(总计):总体=2",
            "plan": {"columns": [], "parts": []},
            "rows": [["玩家ID", "反馈"], ["p-1", "消息丢失"]],
            "rows_fed": True,
            "qa_messages": [{"role": "user", "content": "上一个问题"}],
        }
        collect = AsyncMock(side_effect=[
            RuntimeError("stale conversation"),
            ("基于报告和原始反馈的回答", "new-conv"),
        ])

        with patch.object(survey_service, "_collect_dify_answer", new=collect):
            answer, conv_id, context = await survey_service._answer_qa_with_recovery(
                source, "这个结论依据什么？", "sid", "old-conv", "dify-key"
            )

        self.assertEqual(answer, "基于报告和原始反馈的回答")
        self.assertEqual(conv_id, "new-conv")
        self.assertIn("消息丢失需要优先处理", context)
        self.assertEqual(collect.await_args_list[0].args[2], "old-conv")
        recovery_query = collect.await_args_list[1].args[0]
        self.assertEqual(collect.await_args_list[1].args[2], "")
        self.assertIn("<report>", recovery_query)
        self.assertIn("消息丢失", recovery_query)
        self.assertIn("用户：上一个问题", recovery_query)
        self.assertIn("用户问题：这个结论依据什么？", recovery_query)

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
            patch.object(
                survey_service,
                "_analyst_key_for_report",
                return_value=("dify-key", "DIFY_ANALYST_KEY"),
            ),
        ):
            result = survey_service.prepare_history_qa_context("history-1", None)

        self.assertEqual(result, (history, "", "dify-key", "DIFY_ANALYST_KEY"))

    def test_history_archive_persists_direct_writer_and_qa_context(self):
        sess = {
            "filename": "responses.xlsx",
            "report_md": "# 报告",
            "plan": {"columns": [], "parts": []},
            "stats_md": "总体=2",
            "qa_context_md": "<qa_context>完整上下文</qa_context>",
            "report_writer_provider": "direct_llm",
            "report_writer_model": "model-a",
            "rows_fed": False,
            "rows": [["id"], ["1"], ["2"]],
        }
        saved = []
        with (
            patch.object(report_history, "_load_history", return_value=[]),
            patch.object(report_history, "_save_history", side_effect=lambda value: saved.extend(value)),
            patch.object(report_history, "_ensure_history_report_numbers"),
            patch.object(report_history, "_next_history_report_no", return_value="R-001"),
        ):
            report_history.save_to_history("sid", sess)

        self.assertEqual(saved[0]["qa_context_md"], sess["qa_context_md"])
        self.assertEqual(saved[0]["report_writer_provider"], "direct_llm")
        self.assertEqual(saved[0]["report_writer_model"], "model-a")
        self.assertFalse(saved[0]["rows_fed"])


if __name__ == "__main__":
    unittest.main()
