from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.core import security
from app.routers import export as export_router
from app.routers import history as history_router
from app.services import export_download, export_history, history_service, report_history
from app.services.report_versions import (
    append_report_version,
    sync_active_report_version,
)
from app.storage import history as history_storage


def _snapshot(title: str, created_at: str) -> dict:
    return {
        "report_md": f"# {title}\n\n{title}正文",
        "title": title,
        "qa_context_md": f"<report>{title}</report>",
        "qa_messages": [{"role": "user", "content": f"{title}问题"}],
        "qa_provider": "direct_llm",
        "qa_model": f"qa-{title}",
        "report_writer_provider": "direct_llm",
        "report_writer_model": f"writer-{title}",
        "analyst_conv_id": f"conv-{title}",
        "analyst_app": "standard",
        "created_at": created_at,
    }


def _versioned_session(session_id: str = "version-history-id") -> dict:
    source = {
        "id": session_id,
        "filename": "responses.xlsx",
        "mode": "",
        "plan": {"parts": [{"name": "发现"}]},
        "stats_md": "有效样本(总计):总体=2",
        "rows": [["id", "feedback"], ["1", "a"], ["2", "b"]],
        **_snapshot("第一版", "2026-08-01T10:00:00"),
    }
    sync_active_report_version(source)
    append_report_version(
        source,
        _snapshot("第二版", "2026-08-02T10:00:00"),
        instruction="聚焦流失原因",
    )
    source["next_report_version"] = 6
    return source


class TemporaryHistoryMixin:
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="version-history-integration-")
        self.history_file = os.path.join(self.temp_dir.name, "history.json")
        self.history_path_patch = patch.object(
            history_storage,
            "HISTORY_FILE",
            self.history_file,
        )
        self.login_patch = patch.object(
            security,
            "FEISHU_LOGIN_REQUIRED",
            False,
        )
        self.history_path_patch.start()
        self.login_patch.start()

    def tearDown(self):
        self.login_patch.stop()
        self.history_path_patch.stop()
        self.temp_dir.cleanup()


class HistoryVersionIntegrationTests(TemporaryHistoryMixin, unittest.TestCase):
    def test_history_list_and_detail_return_selected_metadata_without_other_bodies(self):
        sess = _versioned_session()
        saved = report_history.save_to_history(sess["id"], sess)
        self.assertEqual(saved["active_report_version"], 2)
        self.assertEqual(saved["next_report_version"], 6)

        with (
            patch.object(history_service, "get_session", return_value=sess),
            patch.object(history_service, "MAX_REPORT_VERSIONS", 2),
        ):
            listed = history_service.get_history_list(None)
            selected = history_service.get_history_entry(sess["id"], None, 1)

        self.assertEqual(len(listed), 1)
        item = listed[0]
        self.assertEqual(item["version_count"], 2)
        self.assertEqual(item["active_report_version"], 2)
        self.assertEqual(item["next_version"], 6)
        self.assertEqual(item["max_versions"], 2)
        self.assertFalse(item["can_generate_version"])
        self.assertEqual([v["version"] for v in item["report_versions"]], [1, 2])
        self.assertNotIn("report_md", item["report_versions"][0])
        self.assertNotIn("qa_context_md", item["report_versions"][0])

        self.assertEqual(selected["report_md"], "# 第一版\n\n第一版正文")
        self.assertEqual(selected["title"], "第一版")
        self.assertEqual(selected["version"], 1)
        self.assertEqual(selected["selected_version"], 1)
        self.assertEqual(selected["active_version"], 2)
        self.assertEqual(selected["next_version"], 6)
        self.assertEqual(selected["version_created_at"], "2026-08-01T10:00:00")

        with patch.object(history_service, "get_session", return_value=sess):
            with self.assertRaisesRegex(ValueError, "V9 不存在"):
                history_service.get_history_entry(sess["id"], None, 9)

    def test_comment_and_interview_history_keep_non_versioned_behavior(self):
        comment = {
            "filename": "comments.xlsx",
            "mode": "comment",
            "report_md": "# 评论简报\n\n正文",
            "comment_report_title": "评论简报",
            "rows": [["comment"], ["text"]],
        }
        interview = {
            "filename": "interview.xlsx",
            "mode": "interview",
            "report_md": "# 访谈报告\n\n正文",
            "rows": [["player"], ["p1"]],
            "interview_workbook": {"sheets": [{"name": "S1"}]},
        }
        comment_entry = report_history.save_to_history("comment-id", comment)
        interview_entry = report_history.save_to_history("interview-id", interview)

        self.assertNotIn("report_versions", comment_entry)
        self.assertNotIn("active_report_version", comment_entry)
        self.assertNotIn("report_versions", interview_entry)
        self.assertNotIn("active_report_version", interview_entry)

        with patch.object(
            history_service,
            "get_session",
            side_effect=HTTPException(status_code=404),
        ):
            listed = history_service.get_history_list(None)
        by_id = {item["id"]: item for item in listed}
        self.assertEqual(by_id["comment-id"]["version_count"], 0)
        self.assertEqual(by_id["interview-id"]["version_count"], 0)
        self.assertFalse(by_id["comment-id"]["can_generate_version"])
        self.assertFalse(by_id["interview-id"]["can_generate_version"])

    def test_title_rename_updates_every_history_and_live_session_snapshot(self):
        sess = _versioned_session()
        report_history.save_to_history(sess["id"], sess)
        live_session = deepcopy(sess)

        with (
            patch.object(report_history, "get_session", return_value=live_session),
            patch.object(report_history, "save_session") as save_session,
        ):
            result = report_history._update_history_title_by_id(
                sess["id"],
                "统一标题",
                None,
            )

        self.assertEqual(result["title"], "统一标题")
        stored = history_storage._load_history()[0]
        for source in (stored, live_session):
            self.assertEqual(source["title"], "统一标题")
            self.assertTrue(source["report_md"].startswith("# 统一标题\n"))
            self.assertEqual(source["next_report_version"], 6)
            for snapshot in source["report_versions"]:
                self.assertEqual(snapshot["title"], "统一标题")
                self.assertTrue(snapshot["report_md"].startswith("# 统一标题\n"))
        save_session.assert_called_once_with(sess["id"], live_session)

    def test_concurrent_interview_issue_confirmations_preserve_every_update(self):
        issue_count = 12
        entry = {
            "id": "interview-history",
            "filename": "interview.xlsx",
            "title": "访谈报告",
            "created_at": "2026-08-01T10:00:00",
            "report_md": "# 访谈报告",
            "mode": "interview",
            "interview_audit": {
                "issues": [
                    {"id": index, "review_status": "pending"}
                    for index in range(issue_count)
                ],
            },
        }
        unrelated = {
            "id": "survey-history",
            "report_no": "R-010",
            "qa_messages": [{"role": "user", "content": "保留 QA"}],
            "report_versions": [{"version": 1, "report_md": "# V1"}],
        }
        history_storage._save_history([entry, unrelated])

        def confirm(index: int) -> dict:
            return report_history.confirm_interview_audit_issue(
                "interview-history",
                index,
                {"email": "reviewer@example.com"},
            )

        with patch.object(
            report_history,
            "get_session",
            side_effect=HTTPException(status_code=404),
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(confirm, range(issue_count)))

        self.assertEqual(len(results), issue_count)
        stored = history_storage._load_history()
        interview = next(item for item in stored if item["id"] == "interview-history")
        statuses = [
            item["review_status"]
            for item in interview["interview_audit"]["issues"]
        ]
        self.assertEqual(statuses, ["confirmed"] * issue_count)
        self.assertTrue(interview["report_no"].startswith("R-"))
        kept = next(item for item in stored if item["id"] == "survey-history")
        self.assertEqual(kept["qa_messages"][0]["content"], "保留 QA")
        self.assertEqual(len(kept["report_versions"]), 1)

    def test_concurrent_annotate_archives_keep_all_entries_and_unique_numbers(self):
        existing = {
            "id": "survey-history",
            "report_no": "R-001",
            "qa_messages": [{"role": "user", "content": "保留 QA"}],
            "report_versions": [{"version": 1, "report_md": "# V1"}],
        }
        history_storage._save_history([existing])

        def archive(index: int) -> None:
            sess = {
                "filename": f"annotate-{index}.xlsx",
                "rows": [["id"], [str(index)]],
                "tasks": {"ai_detect": True, "quality": index % 2 == 0},
                "ai_results": [{"id": index}],
                "confirmed_ai_ids": [index],
                "quality_results": [{"id": index}],
            }
            return report_history.save_annotate_to_history(
                f"annotate-{index}",
                sess,
                f"result-{index}.xlsx",
                f"download-{index}.xlsx",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(archive, range(12)))

        self.assertEqual(results, [None] * 12)
        stored = history_storage._load_history()
        ids = {item["id"] for item in stored}
        self.assertIn("survey-history", ids)
        self.assertTrue(
            {f"annotate-{index}" for index in range(12)}.issubset(ids)
        )
        report_numbers = [item["report_no"] for item in stored]
        self.assertEqual(len(report_numbers), len(set(report_numbers)))
        kept = next(item for item in stored if item["id"] == "survey-history")
        self.assertEqual(kept["qa_messages"][0]["content"], "保留 QA")
        self.assertEqual(len(kept["report_versions"]), 1)


class VersionExportTests(TemporaryHistoryMixin, unittest.IsolatedAsyncioTestCase):
    async def test_session_word_pdf_markdown_and_feishu_data_select_version(self):
        sess = _versioned_session()
        identity = lambda report_md, mode: report_md
        with (
            patch.object(export_download, "get_session", return_value=sess),
            patch.object(export_download, "_prep_export_md", side_effect=identity),
            patch.object(
                export_download,
                "markdown_to_docx",
                side_effect=lambda report_md: f"docx:{report_md}".encode(),
            ),
            patch.object(
                export_download,
                "report_markdown_to_pdf",
                side_effect=lambda report_md, mode: f"pdf:{report_md}".encode(),
            ),
        ):
            word, word_safe, word_title = await export_download.prepare_word_download(
                sess["id"],
                1,
            )
            markdown, md_safe, md_title = await export_download.prepare_markdown_download(
                sess["id"],
                "1",
            )
            pdf, pdf_safe, pdf_title = await export_download.prepare_pdf_download(
                sess["id"],
                1,
            )
            feishu_md, mode = export_download.get_session_export_data(sess["id"], 1)

        expected = "# 第一版\n\n第一版正文"
        self.assertEqual(word, f"docx:{expected}".encode())
        self.assertEqual(markdown, expected.encode("utf-8"))
        self.assertEqual(pdf, f"pdf:{expected}".encode())
        self.assertEqual(feishu_md, expected)
        self.assertEqual(mode, "")
        self.assertEqual(
            (word_safe, word_title, md_safe, md_title, pdf_safe, pdf_title),
            ("第一版", "第一版", "第一版", "第一版", "第一版", "第一版"),
        )

        with patch.object(export_download, "get_session", return_value=sess):
            with self.assertRaises(HTTPException) as caught:
                export_download.get_session_export_data(sess["id"], 9)
        self.assertEqual(caught.exception.status_code, 404)

    async def test_history_word_pdf_and_markdown_select_version(self):
        sess = _versioned_session()
        report_history.save_to_history(sess["id"], sess)
        identity = lambda report_md, mode: report_md
        missing_session = HTTPException(status_code=404)
        with (
            patch.object(history_service, "get_session", side_effect=missing_session),
            patch.object(export_history, "_prep_export_md", side_effect=identity),
            patch.object(
                export_history,
                "markdown_to_docx",
                side_effect=lambda report_md: f"docx:{report_md}".encode(),
            ),
            patch.object(
                export_history,
                "report_markdown_to_pdf",
                side_effect=lambda report_md, mode: f"pdf:{report_md}".encode(),
            ),
        ):
            word, _, word_title = await export_history.prepare_word_history_download(
                sess["id"],
                None,
                1,
            )
            markdown, _, md_title = await export_history.prepare_markdown_history_download(
                sess["id"],
                None,
                "1",
            )
            pdf, _, pdf_title = await export_history.prepare_pdf_history_download(
                sess["id"],
                None,
                1,
            )

        expected = "# 第一版\n\n第一版正文"
        self.assertEqual(word, f"docx:{expected}".encode())
        self.assertEqual(markdown, expected.encode("utf-8"))
        self.assertEqual(pdf, f"pdf:{expected}".encode())
        self.assertEqual((word_title, md_title, pdf_title), ("第一版",) * 3)

    async def test_feishu_routers_forward_requested_session_and_history_versions(self):
        request = object()
        login = {"email": "user@example.com"}
        with (
            patch.object(export_router, "require_feishu_configured"),
            patch.object(
                export_router,
                "_current_login",
                new=AsyncMock(return_value=login),
            ),
            patch.object(
                export_router,
                "get_session_export_data",
                return_value=("# 第一版", ""),
            ) as session_data,
            patch.object(
                export_router,
                "get_history_export_entry",
                return_value={
                    "report_md": "# 历史第一版",
                    "title": "历史第一版",
                    "mode": "",
                    "plan": {},
                },
            ) as history_data,
            patch.object(
                export_router,
                "_export_to_feishu",
                new=AsyncMock(side_effect=["https://doc/1", "https://doc/2"]),
            ) as export_to_feishu,
            patch.object(export_router, "audit_log", new=AsyncMock()),
        ):
            session_result = await export_router.export_feishu(
                "session-id",
                request,
                "1",
            )
            history_result = await export_router.export_feishu_history(
                "history-id",
                request,
                "2",
            )

        self.assertEqual(session_result["url"], "https://doc/1")
        self.assertEqual(history_result["url"], "https://doc/2")
        session_data.assert_called_once_with("session-id", "1")
        history_data.assert_called_once_with("history-id", login, "2")
        self.assertEqual(export_to_feishu.await_args_list[0].args[0], "# 第一版")
        self.assertEqual(export_to_feishu.await_args_list[1].args[0], "# 历史第一版")

    async def test_download_routers_forward_version_query(self):
        request = object()
        with (
            patch.object(
                export_router,
                "_current_login",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                export_router,
                "prepare_markdown_download",
                new=AsyncMock(return_value=(b"session", "session", "Session")),
            ) as session_markdown,
            patch.object(
                export_router,
                "prepare_markdown_history_download",
                new=AsyncMock(return_value=(b"history", "history", "History")),
            ) as history_markdown,
            patch.object(
                export_router,
                "_make_download_response",
                side_effect=lambda content, media_type, filename: {
                    "content": content,
                    "media_type": media_type,
                    "filename": filename,
                },
            ),
            patch.object(export_router, "audit_log", new=AsyncMock()),
        ):
            session_result = await export_router.export_markdown(
                "session-id",
                request,
                "1",
            )
            history_result = await export_router.export_markdown_history(
                "history-id",
                request,
                "2",
            )

        session_markdown.assert_awaited_once_with("session-id", "1")
        history_markdown.assert_awaited_once_with("history-id", None, "2")
        self.assertEqual(session_result["filename"], "session.md")
        self.assertEqual(history_result["filename"], "history.md")


class HistoryRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_detail_forwards_version_and_maps_missing_version(self):
        request = object()
        entry = {"id": "history-id", "title": "V1", "version": 1}
        with (
            patch.object(
                history_router,
                "_current_login",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                history_router,
                "get_history_entry",
                return_value=entry,
            ) as get_entry,
            patch.object(history_router, "audit_log", new=AsyncMock()),
        ):
            result = await history_router.get_history_item(
                "history-id",
                request,
                "1",
            )
        self.assertEqual(result, entry)
        get_entry.assert_called_once_with("history-id", None, "1")

        with (
            patch.object(
                history_router,
                "_current_login",
                new=AsyncMock(return_value=None),
            ),
            patch.object(
                history_router,
                "get_history_entry",
                side_effect=ValueError("报告版本 V9 不存在"),
            ),
        ):
            with self.assertRaises(HTTPException) as caught:
                await history_router.get_history_item(
                    "history-id",
                    request,
                    "9",
                )
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
