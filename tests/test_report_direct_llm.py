import unittest
from unittest.mock import AsyncMock, patch

from app.services import report_history
from app.services import survey_service
from app.services.report_engine import _describe_qa_context_scope


class DirectReportServiceTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(saved[0]["qa_provider"], "direct_llm")
        self.assertEqual(saved[0]["qa_model"], "claude-sonnet-5")
        self.assertFalse(saved[0]["rows_fed"])


if __name__ == "__main__":
    unittest.main()
