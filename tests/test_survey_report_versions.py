from __future__ import annotations

from contextlib import ExitStack, contextmanager
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.services import report_history, report_versions, survey_service
from app.storage import history as history_storage


def _base_session() -> dict:
    return {
        "filename": "responses.xlsx",
        "owner_key": "email:owner@example.com",
        "rows": [["题目"], ["回答"]],
        "plan": {
            "columns": [],
            "parts": [],
            "cross_tabs": [],
            "open_questions": [],
            "branch_rules": [],
        },
        "branch_rules": [],
        "stats_md": "有效样本(总计):总体=1",
        "open_text": {},
        "qualitative_context": {},
    }


def _snapshot(title: str, *, qa_message: str = "") -> dict:
    messages = []
    if qa_message:
        messages.append({"role": "user", "content": qa_message})
    return {
        "report_md": f"# {title}\n\n{title}正文",
        "title": title,
        "qa_context_md": (
            f"<qa_context><report>{title}正文</report><rows>（无数据）</rows></qa_context>"
        ),
        "qa_messages": messages,
        "qa_provider": "",
        "qa_model": "",
        "report_writer_provider": "direct_llm",
        "report_writer_model": f"writer-{title}",
        "analyst_conv_id": "",
        "analyst_app": "standard",
    }


def _versioned_session(version_count: int = 3) -> dict:
    sess = _base_session()
    for version in range(1, version_count + 1):
        report_versions.append_report_version(
            sess,
            _snapshot(f"V{version} 报告", qa_message=f"V{version} 旧问题"),
            kind="initial" if version == 1 else "regenerate",
            base_version=None if version == 1 else version - 1,
            instruction="" if version == 1 else f"生成 V{version}",
            created_at=f"2026-08-0{version}T10:00:00",
        )
    return sess


def _writer(title: str) -> AsyncMock:
    return AsyncMock(
        side_effect=[
            (f"# {title}", "writer-model"),
            ("NONE", "writer-model"),
            (
                "<!--CORE_START-->\n## 核心结论\n"
                f"{title}核心。\n<!--CORE_END-->",
                "writer-model",
            ),
            ("## 行动建议\n\n**继续验证**", "writer-model"),
        ]
    )


def _event_payloads(events: list[str]) -> list[dict]:
    payloads = []
    for event in events:
        if event.startswith("data: "):
            payloads.append(json.loads(event.removeprefix("data: ").strip()))
    return payloads


class _TemporaryHistoryPathMixin:
    """Fail-safe every test in this module away from the runtime history file."""

    def setUp(self):
        super().setUp()
        self._history_temp = tempfile.TemporaryDirectory(
            prefix="survey-report-version-history-"
        )
        self.history_dir = Path(self._history_temp.name)
        self.history_file = self.history_dir / "history.json"
        self._history_path_patch = patch.object(
            history_storage,
            "HISTORY_FILE",
            str(self.history_file),
        )
        self._history_path_patch.start()

    def tearDown(self):
        self._history_path_patch.stop()
        self._history_temp.cleanup()
        super().tearDown()


@contextmanager
def _isolated_report_runtime(sess: dict, writer: AsyncMock):
    """Mock every report-stream dependency that could write data or call a model."""
    with ExitStack() as stack:
        save_session = stack.enter_context(
            patch.object(survey_service, "save_session")
        )
        save_history = stack.enter_context(
            patch.object(survey_service, "save_to_history")
        )
        stack.enter_context(patch.object(survey_service, "get_session", return_value=sess))
        stack.enter_context(
            patch.object(
                survey_service,
                "_current_login",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            )
        )
        stack.enter_context(patch.object(survey_service, "_assign_session_owner"))
        stack.enter_context(patch.object(survey_service, "_ensure_branch_rules"))
        stack.enter_context(
            patch.object(
                survey_service,
                "_get_report_writer_system_prompt",
                return_value="writer system",
            )
        )
        stack.enter_context(
            patch.object(survey_service, "_direct_writer_round", new=writer)
        )
        stack.enter_context(
            patch.object(survey_service, "normalize_glossary_terms", side_effect=lambda text: text)
        )
        stack.enter_context(
            patch.object(
                survey_service,
                "inject_qualitative_stats",
                side_effect=lambda report, _stats, _plan: report,
            )
        )
        stack.enter_context(
            patch.object(
                survey_service,
                "_inject_disclaimer",
                side_effect=lambda report, mode="": report,
            )
        )
        stack.enter_context(
            patch.object(
                survey_service,
                "_inject_research_background",
                side_effect=lambda report, _context: report,
            )
        )
        stack.enter_context(
            patch.object(
                survey_service,
                "_build_qa_context",
                side_effect=lambda _source, report="": (
                    f"<qa_context><report>{report}</report>"
                    "<rows>（无数据）</rows></qa_context>"
                ),
            )
        )
        stack.enter_context(
            patch.object(
                survey_service.survey_stats,
                "find_numbers_not_in_stats",
                return_value=[],
            )
        )
        stack.enter_context(
            patch.object(survey_service, "audit_log", new=AsyncMock())
        )
        stack.enter_context(patch("traceback.print_exc"))
        yield {
            "save_session": save_session,
            "save_history": save_history,
            "writer": writer,
        }


class SurveyReportVersionGenerationTests(
    _TemporaryHistoryPathMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_first_successful_generation_creates_numeric_v1(self):
        sess = _base_session()
        writer = _writer("首版报告")

        with _isolated_report_runtime(sess, writer) as runtime:
            events = [
                event
                async for event in survey_service.report_stream(
                    "initial-version-session",
                    object(),
                    generation_kind="initial",
                )
            ]

        done = next(
            payload for payload in _event_payloads(events)
            if payload.get("type") == "report_done"
        )
        self.assertIs(type(done["version"]), int)
        self.assertEqual(done["version"], 1)
        self.assertNotIn("version_data", done)
        self.assertNotIn("qa_context_md", done)
        self.assertEqual(sess["active_report_version"], 1)
        self.assertEqual(sess["next_report_version"], 2)
        self.assertEqual(len(sess["report_versions"]), 1)
        self.assertEqual(sess["report_versions"][0]["kind"], "initial")
        self.assertIsNone(sess["report_versions"][0]["base_version"])
        runtime["save_session"].assert_called_once_with(
            "initial-version-session", sess
        )
        runtime["save_history"].assert_called_once_with(
            "initial-version-session", sess
        )

    async def test_regeneration_can_use_any_existing_base_and_saves_instruction(self):
        sess = _versioned_session(3)
        previous_versions = {
            version: report_versions.resolve_report_version(sess, version)
            for version in (1, 2, 3)
        }
        instruction = "只聚焦首次流失的触发原因"
        writer = _writer("第四版报告")

        with _isolated_report_runtime(sess, writer):
            events = [
                event
                async for event in survey_service.report_stream(
                    "regenerate-from-old-session",
                    object(),
                    instruction=instruction,
                    base_version=1,
                    generation_kind="regenerate",
                )
            ]

        done = next(
            payload for payload in _event_payloads(events)
            if payload.get("type") == "report_done"
        )
        self.assertEqual(done["version"], 4)
        committed = report_versions.resolve_report_version(sess, 4)
        self.assertEqual(committed["base_version"], 1)
        self.assertEqual(committed["instruction"], instruction)
        self.assertEqual(committed["kind"], "regenerate")
        self.assertEqual(sess["active_report_version"], 4)
        for version, snapshot in previous_versions.items():
            self.assertEqual(
                report_versions.resolve_report_version(sess, version),
                snapshot,
            )
        first_query = writer.await_args_list[0].args[1]
        self.assertIn(instruction, first_query)

    async def test_writer_failure_does_not_append_or_overwrite_versions(self):
        sess = _versioned_session(2)
        before = deepcopy(sess)
        writer = AsyncMock(side_effect=RuntimeError("writer unavailable"))

        with _isolated_report_runtime(sess, writer) as runtime:
            events = [
                event
                async for event in survey_service.report_stream(
                    "failed-version-session",
                    object(),
                    instruction="这次不会成功",
                    base_version=1,
                    generation_kind="regenerate",
                )
            ]

        self.assertEqual(sess, before)
        self.assertFalse(
            any(
                payload.get("type") == "report_done"
                for payload in _event_payloads(events)
            )
        )
        error = next(
            payload for payload in _event_payloads(events)
            if payload.get("type") == "error"
        )
        self.assertIn("writer unavailable", error["message"])
        runtime["save_session"].assert_not_called()
        runtime["save_history"].assert_not_called()

    async def test_history_persistence_failure_rolls_back_new_version_number(self):
        sess = _versioned_session(1)
        before = deepcopy(sess)
        writer = _writer("不会提交的第二版")

        with _isolated_report_runtime(sess, writer) as runtime:
            runtime["save_history"].side_effect = OSError("history unavailable")
            events = [
                event
                async for event in survey_service.report_stream(
                    "history-write-failure-session",
                    object(),
                    instruction="本次持久化会失败",
                    base_version=1,
                    generation_kind="regenerate",
                )
            ]

        self.assertEqual(sess, before)
        self.assertFalse(
            any(
                payload.get("type") == "report_done"
                for payload in _event_payloads(events)
            )
        )
        self.assertEqual(
            next(
                payload for payload in _event_payloads(events)
                if payload.get("type") == "error"
            )["message"],
            "history unavailable",
        )
        self.assertEqual(runtime["save_session"].call_count, 2)


class SurveyReportVersionQaTests(
    _TemporaryHistoryPathMixin,
    unittest.IsolatedAsyncioTestCase,
):
    async def test_session_qa_updates_only_the_explicit_version(self):
        sess = _versioned_session(2)
        first_before = report_versions.resolve_report_version(sess, 1)
        second_before = report_versions.resolve_report_version(sess, 2)
        active_messages_before = deepcopy(sess["qa_messages"])
        answer = AsyncMock(
            return_value=(
                "只回答第一版的问题",
                "qa-model",
                "<qa_context><report>V1</report><rows>（无数据）</rows></qa_context>",
            )
        )

        with (
            patch.object(survey_service, "get_session", return_value=sess),
            patch.object(
                survey_service,
                "_current_login",
                new=AsyncMock(return_value={"email": "owner@example.com"}),
            ),
            patch.object(survey_service, "_assign_session_owner"),
            patch.object(survey_service, "_answer_qa_direct", new=answer),
            patch.object(survey_service, "save_session") as save_session,
            patch.object(survey_service, "save_to_history") as save_history,
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch("traceback.print_exc"),
        ):
            events = [
                event
                async for event in survey_service.qa_stream(
                    "session-qa-version",
                    "第一版依据是什么？",
                    object(),
                    version=1,
                )
            ]

        first_after = report_versions.resolve_report_version(sess, 1)
        second_after = report_versions.resolve_report_version(sess, 2)
        self.assertEqual(first_after["qa_messages"][:-2], first_before["qa_messages"])
        self.assertEqual(first_after["qa_messages"][-2]["role"], "user")
        self.assertEqual(first_after["qa_messages"][-2]["content"], "第一版依据是什么？")
        self.assertEqual(first_after["qa_messages"][-1]["role"], "ai")
        self.assertEqual(first_after["qa_messages"][-1]["content"], "只回答第一版的问题")
        self.assertEqual(second_after, second_before)
        self.assertEqual(sess["active_report_version"], 2)
        self.assertEqual(sess["qa_messages"], active_messages_before)
        self.assertEqual(answer.await_args.args[0]["report_md"], first_before["report_md"])
        done = next(
            payload for payload in _event_payloads(events)
            if payload.get("type") == "qa_done"
        )
        self.assertEqual(done["version"], 1)
        save_session.assert_called_once_with("session-qa-version", sess)
        save_history.assert_called_once_with("session-qa-version", sess)

    async def test_history_qa_updates_only_explicit_version_in_temporary_file(self):
        entry = _versioned_session(2)
        entry["id"] = "history-version-id"
        first_before = report_versions.resolve_report_version(entry, 1)
        second_before = report_versions.resolve_report_version(entry, 2)
        active_messages_before = deepcopy(entry["qa_messages"])
        answer = AsyncMock(
            return_value=(
                "历史第一版回答",
                "qa-history-model",
                "<qa_context><report>历史 V1</report><rows>（无数据）</rows></qa_context>",
            )
        )

        history_storage._save_history([entry])
        supplied_history = history_storage._load_history()
        with (
            patch.object(survey_service, "_answer_qa_direct", new=answer),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch("traceback.print_exc"),
        ):
            events = [
                event
                async for event in survey_service.history_qa_stream(
                    "history-version-id",
                    "历史第一版依据是什么？",
                    supplied_history,
                    object(),
                    version=1,
                )
            ]
        stored_entry = history_storage._load_history()[0]
        self.assertEqual(
            sorted(path.name for path in self.history_dir.iterdir()),
            ["history.json"],
        )

        first_after = report_versions.resolve_report_version(stored_entry, 1)
        second_after = report_versions.resolve_report_version(stored_entry, 2)
        self.assertEqual(first_after["qa_messages"][:-2], first_before["qa_messages"])
        self.assertEqual(first_after["qa_messages"][-2]["content"], "历史第一版依据是什么？")
        self.assertEqual(first_after["qa_messages"][-1]["content"], "历史第一版回答")
        self.assertEqual(second_after, second_before)
        self.assertEqual(stored_entry["active_report_version"], 2)
        self.assertEqual(stored_entry["qa_messages"], active_messages_before)
        self.assertEqual(answer.await_args.args[0]["report_md"], first_before["report_md"])
        done = next(
            payload for payload in _event_payloads(events)
            if payload.get("type") == "qa_done"
        )
        self.assertEqual(done["version"], 1)

    async def test_history_qa_syncs_live_session_before_later_history_save(self):
        history_id = "history-live-sync-id"
        live_session = _versioned_session(2)
        history_entry = deepcopy(live_session)
        history_entry["id"] = history_id
        history_storage._save_history([history_entry])
        answer = AsyncMock(
            return_value=(
                "从历史页得到的新回答",
                "qa-history-model",
                "<qa_context><report>历史 V1</report><rows>（无数据）</rows></qa_context>",
            )
        )

        with (
            patch.object(survey_service, "get_session", return_value=live_session),
            patch.object(survey_service, "save_session") as save_session,
            patch.object(survey_service, "_answer_qa_direct", new=answer),
            patch.object(survey_service, "audit_log", new=AsyncMock()),
            patch("traceback.print_exc"),
        ):
            events = [
                event
                async for event in survey_service.history_qa_stream(
                    history_id,
                    "从历史页追问第一版",
                    history_storage._load_history(),
                    object(),
                    version=1,
                )
            ]

        self.assertTrue(
            any(payload.get("type") == "qa_done" for payload in _event_payloads(events))
        )
        save_session.assert_called_once_with(history_id, live_session)
        live_first = report_versions.resolve_report_version(live_session, 1)
        self.assertEqual(live_first["qa_messages"][-1]["content"], "从历史页得到的新回答")

        # 模拟随后从当前会话再次保存历史；刚才的历史追问不能被旧 session 覆盖掉。
        report_history.save_to_history(history_id, live_session)
        stored_first = report_versions.resolve_report_version(
            history_storage._load_history()[0],
            1,
        )
        self.assertEqual(stored_first["qa_messages"][-1]["content"], "从历史页得到的新回答")


class SurveyReportVersionHistoryPersistenceTests(
    _TemporaryHistoryPathMixin,
    unittest.TestCase,
):
    def test_non_versioned_session_is_rejected_by_version_endpoints(self):
        session = {
            "mode": "comment",
            "report_md": "# 评论报告",
            "title": "评论报告",
        }
        with patch.object(survey_service, "get_session", return_value=session):
            for call in (
                lambda: survey_service.get_session_report_versions("comment-id"),
                lambda: survey_service.get_session_report_version("comment-id", 1),
                lambda: survey_service.delete_session_report_version("comment-id", 1),
            ):
                with self.subTest(call=call):
                    with self.assertRaises(HTTPException) as caught:
                        call()
                    self.assertEqual(caught.exception.status_code, 400)

    def test_save_to_history_persists_all_versions_and_active_mirror(self):
        sess = _versioned_session(3)
        sess.update({
            "filename": "versioned.xlsx",
            "owner_email": "owner@example.com",
            "owner_open_id": "",
            "owner_name": "Owner",
        })

        saved = report_history.save_to_history("versioned-history-id", sess)
        stored = history_storage._load_history()[0]
        self.assertEqual(
            sorted(path.name for path in self.history_dir.iterdir()),
            ["history.json"],
        )

        self.assertEqual(saved["id"], "versioned-history-id")
        self.assertEqual(stored["active_report_version"], 3)
        self.assertEqual(stored["next_report_version"], 4)
        self.assertEqual(
            [item["version"] for item in stored["report_versions"]],
            [1, 2, 3],
        )
        for version in (1, 2, 3):
            self.assertEqual(
                report_versions.resolve_report_version(stored, version),
                report_versions.resolve_report_version(sess, version),
            )
        active = report_versions.resolve_report_version(stored, 3)
        self.assertEqual(stored["report_md"], active["report_md"])
        self.assertEqual(stored["qa_messages"], active["qa_messages"])


if __name__ == "__main__":
    unittest.main()
