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


def quick_completion_fixture():
    from app.services.report_quick_mode import render_quick_report, fill_question_evidence
    from app.services.report_modes import quick_qa_context
    from app.services.report_versions import resolve_report_version
    rows = [{"response_id": "r1", "text": "合成完整回答一", "profile": {"段位": "Gold", "局数": 0}},
            {"response_id": "r2", "text": "合成完整回答二", "profile": {"段位": "Silver", "新玩家": False}}]
    originals = [{"question_key": str(i), "question": f"合成题{i}", "source_order": i, "sources": deepcopy(rows)} for i in (1, 2)]
    finding = {"text": "合成观点", "frequency": "部分提及", "risk": False, "evidence_ids": ["r1", "r2"]}
    questions = [{**deepcopy(q), "status": "complete" if q["question_key"] == "1" else "failed",
                  "findings": fill_question_evidence([finding], rows) if q["question_key"] == "1" else []} for q in originals]
    frozen = {"source_questions": originals, "objective_stats": {"sections": []}}
    summary = {"questions": questions, "objective_stats": {"sections": []}}
    def usage(count):
        values = {"input_tokens": count, "output_tokens": 0, "total_tokens": count, "call_count": 1,
                  "usage_reported_call_count": 1, "usage_missing_call_count": 0, "models_used": ["synthetic"]}
        return {"schema_version": 1, "phases": {"themes": deepcopy(values)}, "totals": values}
    initial = {"report_mode": "quick", "report_style": "quick", "report_status": "partial", "title": "合成报告",
               "input_snapshot": frozen, "quick_summary": summary, "quick_checkpoint": {"questions": [deepcopy(questions[0])]},
               "report_duration_seconds": 2, "qa_messages": [{"role": "user", "content": "原有追问"}],
               "report_llm_usage": usage(7), "quick_report_diagnostics": {"initial": True}}
    initial["report_md"] = render_quick_report(summary, title=initial["title"])
    initial["qa_context_md"] = quick_qa_context(initial["report_md"], frozen)
    entry = {"id": "completion-history", "owner_key": "email:owner@example.com", "mode": "standard", "filename": "synthetic.csv"}
    append_report_version(entry, initial)
    base = resolve_report_version(entry, 1)
    result = deepcopy(initial)
    result["quick_summary"]["questions"][1].update(status="complete", findings=fill_question_evidence([finding], rows))
    result.update(report_status="complete", report_duration_seconds=3, report_llm_usage=usage(11), qa_messages=[])
    result["quick_checkpoint"]["questions"] = deepcopy(result["quick_summary"]["questions"])
    result["report_md"] = render_quick_report(result["quick_summary"], title=result["title"])
    for _ in range(4):
        append_report_version(entry, result, base_version=1)
    return entry, base, result


class QuickCompletionProtectionTests(unittest.TestCase):
    def test_only_failed_question_changes_and_metadata_is_preserved_at_limit(self):
        from app.services.report_versions import complete_quick_report_version, resolve_report_version
        entry, base, result = quick_completion_fixture()
        other = deepcopy(entry["report_versions"][1:])
        entry["report_versions"][0]["qa_messages"].append({"role": "assistant", "content": "更新期间已有追问"})
        saved = complete_quick_report_version(entry, result, expected_base=base)
        self.assertEqual(saved["version"], 1)
        self.assertEqual(len(entry["report_versions"]), 5)
        self.assertEqual(entry["report_versions"][1:], other)
        self.assertEqual(entry["active_report_version"], 5)
        self.assertEqual(entry["next_report_version"], 6)
        self.assertEqual(saved["quick_summary"]["questions"][0], base["quick_summary"]["questions"][0])
        self.assertEqual(saved["input_snapshot"], base["input_snapshot"])
        self.assertEqual(len(saved["qa_messages"]), 2)
        self.assertEqual(saved["report_llm_usage"]["totals"]["total_tokens"], 18)
        self.assertEqual(saved["report_duration_seconds"], 5)
        self.assertEqual(saved["created_at"], base["created_at"])
        self.assertEqual(saved["quick_completion_revision"], 1)
        self.assertEqual(resolve_report_version(entry, 1), saved)

    def test_rejects_corruption_without_any_mutation(self):
        from app.services.report_versions import complete_quick_report_version
        changes = [
            lambda r: r["quick_summary"]["questions"][0].update(findings=[]),
            lambda r: r["input_snapshot"]["source_questions"][1]["sources"][0]["profile"].update(段位="Changed"),
            lambda r: r["quick_summary"]["questions"][1]["sources"][0].update(text="错配原文"),
            lambda r: r["quick_summary"]["questions"][1]["findings"][0]["evidence"][0].update(text="错配引用"),
            lambda r: r["quick_summary"]["questions"].pop(),
            lambda r: r.update(report_md="错误正文"),
            lambda r: r["quick_summary"].update(objective_stats={"changed": True}),
            lambda r: r["quick_checkpoint"].update(questions=[]),
        ]
        for change in changes:
            entry, base, result = quick_completion_fixture()
            before = deepcopy(entry)
            change(result)
            with self.assertRaises(ValueError):
                complete_quick_report_version(entry, result, expected_base=base)
            self.assertEqual(entry, before)

    def test_stale_result_and_missing_success_or_profile_are_rejected(self):
        from app.services.report_versions import complete_quick_report_version, validate_quick_completion_base
        entry, base, result = quick_completion_fixture()
        entry["report_versions"][0]["quick_completion_revision"] = 1
        before = deepcopy(entry)
        with self.assertRaisesRegex(ValueError, "已更新"):
            complete_quick_report_version(entry, result, expected_base=base)
        self.assertEqual(entry, before)
        broken = deepcopy(base)
        broken["quick_checkpoint"]["questions"] = []
        with self.assertRaisesRegex(ValueError, "成功题目缓存"):
            validate_quick_completion_base(broken)
        broken = deepcopy(base)
        for collection in (broken["input_snapshot"]["source_questions"], broken["quick_summary"]["questions"]):
            collection[1]["sources"][0].pop("profile")
        with self.assertRaisesRegex(ValueError, "画像"):
            validate_quick_completion_base(broken)


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
        "comparison_validation": {
            "status": "passed",
            "version_marker": title,
            "changes": [],
            "unresolved": [],
        },
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


class QuickCompletionPersistenceTests(unittest.TestCase):
    def test_atomic_history_failure_concurrency_and_stale_session(self):
        from pathlib import Path
        from app.services.report_versions import resolve_report_version
        from app.services import survey_service
        entry, base, result = quick_completion_fixture()
        login = {"email": "owner@example.com"}
        with tempfile.TemporaryDirectory(prefix="quick-completion-") as folder, patch.object(history_storage, "HISTORY_FILE", os.path.join(folder, "history.json")):
            history_storage._save_history([entry])
            original = Path(history_storage.HISTORY_FILE).read_bytes()
            with patch.object(history_storage.os, "replace", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError):
                    report_history.complete_quick_report_in_history(entry["id"], result, expected_base=base, login=login)
            self.assertEqual(Path(history_storage.HISTORY_FILE).read_bytes(), original)
            def complete():
                try:
                    report_history.complete_quick_report_in_history(entry["id"], result, expected_base=base, login=login)
                    return True
                except ValueError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: complete(), range(2))), [False, True])
            saved = history_storage._load_history()[0]
            self.assertEqual(len(saved["report_versions"]), 5)
            self.assertEqual(resolve_report_version(saved, 1)["quick_completion_revision"], 1)
            # A stale live reader must not undo the completed version during a later history save.
            merged = report_history._history_version_source(entry, saved, title=entry["title"], created_at=entry.get("created_at", ""))
            self.assertEqual(resolve_report_version(merged, 1), resolve_report_version(saved, 1))
            # Version viewing recovers from authoritative history after a failed session-copy save.
            with patch.object(survey_service, "get_session", return_value=entry):
                view = survey_service.get_session_report_version(entry["id"], 1)
            self.assertEqual(view["report_status"], "complete")
            from app.services.report_modes import prepare_report_markdown
            exported = export_history.get_history_export_entry(entry["id"], login, 1)
            self.assertEqual(exported["report_md"], prepare_report_markdown(resolve_report_version(saved, 1)))


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
        self.assertEqual(
            selected["comparison_validation"]["version_marker"],
            "第一版",
        )

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
    async def test_outline_uses_selected_version_across_history_and_all_export_formats(self):
        from pathlib import Path
        from tests.test_report_quick_outline import outline_fixture
        from app.services.report_modes import prepare_report_markdown
        from app.services.report_quick_outline import build_quick_outline
        snapshot = outline_fixture()
        entry = {'id': 'outline-history', 'report_no': 'R-001', 'owner_key': 'email:owner@example.com', 'mode': 'standard', 'filename': 'synthetic.csv'}
        append_report_version(entry, snapshot)
        newer = deepcopy(snapshot)
        newer['input_snapshot']['branch_rules'][0]['allowed_options'] = ['另一个版本的人群']
        append_report_version(entry, newer)
        history_storage._save_history([entry])
        before = Path(self.history_file).read_bytes()
        for version, expected_snapshot in ((1, snapshot), (2, newer)):
            selected = history_service.get_history_entry(entry['id'], {'email': 'owner@example.com'}, version)
            self.assertEqual(selected['quick_outline'], build_quick_outline(expected_snapshot))
            self.assertEqual(selected['report_md'], expected_snapshot['report_md'])
            with (
                patch.object(export_download, 'get_session', return_value=entry),
                patch.object(export_download, 'markdown_to_docx', side_effect=lambda md: md.encode()),
                patch.object(export_download, 'report_markdown_to_pdf', side_effect=lambda md, mode: md.encode()),
                patch.object(export_history, 'markdown_to_docx', side_effect=lambda md: md.encode()),
                patch.object(export_history, 'report_markdown_to_pdf', side_effect=lambda md, mode: md.encode()),
            ):
                for scope in ('body', 'evidence'):
                    expected = prepare_report_markdown(expected_snapshot, scope)
                    prepared = export_download._prep_export_md(expected, mode='standard')
                    self.assertEqual((await export_download.prepare_word_download(entry['id'], version, scope))[0], prepared.encode())
                    self.assertEqual((await export_download.prepare_markdown_download(entry['id'], version, scope))[0], prepared.encode())
                    self.assertEqual((await export_download.prepare_pdf_download(entry['id'], version, scope))[0], expected.encode())
                    self.assertEqual(export_download.get_session_export_data(entry['id'], version, scope)[0], expected)
                    exported = export_history.get_history_export_entry(entry['id'], {'email': 'owner@example.com'}, version, scope)
                    self.assertEqual(exported['report_md'], expected)
                    self.assertEqual((await export_history.prepare_word_history_download(entry['id'], {'email': 'owner@example.com'}, version, scope))[0], prepared.encode())
                    self.assertEqual((await export_history.prepare_markdown_history_download(entry['id'], {'email': 'owner@example.com'}, version, scope))[0], prepared.encode())
                    self.assertEqual((await export_history.prepare_pdf_history_download(entry['id'], {'email': 'owner@example.com'}, version, scope))[0], expected.encode())
        self.assertEqual(Path(self.history_file).read_bytes(), before)

    async def test_quick_exports_recover_completed_selected_version_without_writing(self):
        from pathlib import Path
        from app.services.report_modes import prepare_report_markdown
        from app.services.report_versions import resolve_report_version, update_report_version
        sess, base, result = quick_completion_fixture()
        update_report_version(sess, 5, title='另一个版本', report_md='# 另一个版本\n\n版本五专有正文')
        history_storage._save_history([sess])
        _, completed = report_history.complete_quick_report_in_history(
            sess['id'], result, expected_base=base, login={'email': 'owner@example.com'})
        before = deepcopy(sess)
        history_bytes = Path(self.history_file).read_bytes()
        # The session still has partial V1; the archive has completed V1 and active V5.
        self.assertEqual(resolve_report_version(sess, 1)['report_status'], 'partial')
        self.assertEqual(completed['report_status'], 'complete')
        with (
            patch.object(export_download, 'get_session', return_value=sess),
            patch.object(export_download, 'markdown_to_docx', side_effect=lambda md: ('docx:' + md).encode('utf-8')),
            patch.object(export_download, 'report_markdown_to_pdf', side_effect=lambda md, mode: ('pdf:' + md).encode('utf-8')),
        ):
            for scope in ('body', 'evidence'):
                with self.subTest(scope=scope):
                    expected = prepare_report_markdown(completed, scope)
                    prepared = export_download._prep_export_md(expected, mode=sess['mode'])
                    word = await export_download.prepare_word_download(sess['id'], '1', scope)
                    markdown = await export_download.prepare_markdown_download(sess['id'], 1, scope)
                    pdf = await export_download.prepare_pdf_download(sess['id'], 1, scope)
                    feishu_md, _ = export_download.get_session_export_data(sess['id'], 1, scope)
                    self.assertEqual(word[0], ('docx:' + prepared).encode('utf-8'))
                    self.assertEqual(markdown[0], prepared.encode('utf-8'))
                    self.assertEqual(pdf[0], ('pdf:' + expected).encode('utf-8'))
                    self.assertEqual(feishu_md, expected)
                    self.assertEqual([item[2] for item in (word, markdown, pdf)], ['合成报告'] * 3)
            selected = export_download._get_session_report_source(sess['id'], 1)
            self.assertEqual(selected['quick_completion_revision'], 1)
            self.assertEqual(selected['input_snapshot'], base['input_snapshot'])
            self.assertEqual(selected['qa_messages'], base['qa_messages'])
            self.assertEqual(export_download._get_session_report_source(sess['id'])['version'], 5)
            self.assertIn('版本五专有正文', export_download.get_session_export_data(sess['id'], 5)[0])
        # A restored job resolves the original history ID, not its temporary session ID.
        restored = {**deepcopy(sess), 'rerun_target_history_id': sess['id']}
        with patch.object(export_download, 'get_session', return_value=restored):
            self.assertEqual(export_download._get_session_report_source('restored-job', 1)['report_md'], completed['report_md'])
        self.assertEqual(sess, before)
        self.assertEqual(Path(self.history_file).read_bytes(), history_bytes)
        self.assertEqual(len(history_storage._load_history()[0]['report_versions']), 5)

    async def test_quick_export_recovery_preserves_owner_and_legacy_boundaries(self):
        sess, base, _ = quick_completion_fixture()
        foreign = deepcopy(sess)
        foreign['owner_key'] = 'email:someone-else@example.com'
        for archive in ([], [foreign]):
            with self.subTest(archive=bool(archive)), patch.object(export_download, 'get_session', return_value=sess), patch.object(report_history, '_load_history', return_value=archive):
                selected = export_download._get_session_report_source(sess['id'], 1)
                self.assertEqual(selected['report_md'], base['report_md'])
                self.assertEqual(selected['report_status'], 'partial')
        with patch.object(export_download, 'get_session', return_value=sess), patch.object(report_history, '_load_history', side_effect=OSError('synthetic unreadable history')):
            with self.assertRaisesRegex(OSError, 'unreadable history'):
                export_download.get_session_export_data(sess['id'], 1)
        legacy = _versioned_session()
        with patch.object(export_download, 'get_session', return_value=legacy), patch.object(report_history, '_load_history', side_effect=AssertionError('ordinary export must not load history')):
            self.assertEqual(export_download._get_session_report_source(legacy['id'], 1)['title'], '第一版')

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
        session_data.assert_called_once_with("session-id", "1", scope="body")
        history_data.assert_called_once_with("history-id", login, "2", scope="body")
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

        session_markdown.assert_awaited_once_with("session-id", "1", scope="body")
        history_markdown.assert_awaited_once_with("history-id", None, "2", scope="body")
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
