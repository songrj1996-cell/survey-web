from datetime import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import history_service, report_history, survey_service
from app.services.report_versions import (
    append_report_version,
    report_version_summaries,
    resolve_report_version,
)
from app.storage import history as history_storage


class ReportCompletionTimingTests(unittest.TestCase):
    def test_duration_runs_from_plan_approval_to_successful_completion(self):
        timing = survey_service._report_completion_timing(
            {"plan_approved_at": "2026-08-25T10:00:00.000"},
            completed_at=datetime.fromisoformat("2026-08-25T10:12:34.600"),
        )

        self.assertEqual(timing["plan_approved_at"], "2026-08-25T10:00:00.000")
        self.assertEqual(timing["report_completed_at"], "2026-08-25T10:12:34.600")
        self.assertEqual(timing["report_duration_seconds"], 755)

    def test_missing_or_invalid_approval_time_does_not_invent_duration(self):
        completed_at = datetime.fromisoformat("2026-08-25T10:12:34.600")

        for session in ({}, {"plan_approved_at": "not-a-time"}):
            with self.subTest(session=session):
                timing = survey_service._report_completion_timing(
                    session,
                    completed_at=completed_at,
                )
                self.assertEqual(
                    timing,
                    {"report_completed_at": "2026-08-25T10:12:34.600"},
                )


class ReportTimingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="report-timing-history-")
        self.history_file = os.path.join(self.temp_dir.name, "history.json")
        self.history_patch = patch.object(
            history_storage,
            "HISTORY_FILE",
            self.history_file,
        )
        self.history_patch.start()

    def tearDown(self):
        self.history_patch.stop()
        self.temp_dir.cleanup()

    def test_duration_survives_version_summary_history_list_and_detail(self):
        session = {
            "id": "timed-report",
            "filename": "responses.xlsx",
            "mode": "",
            "rows": [["id", "feedback"], ["p1", "useful"]],
            "plan": {"parts": [{"name": "发现"}]},
            "stats_md": "有效样本(总计):总体=1",
        }
        append_report_version(
            session,
            {
                "report_md": "# 定性分析报告\n\n正文",
                "title": "定性分析报告",
                "plan_approved_at": "2026-08-25T10:00:00.000",
                "report_completed_at": "2026-08-25T10:12:34.600",
                "report_duration_seconds": 755,
            },
            kind="initial",
        )

        version = resolve_report_version(session, 1)
        self.assertEqual(version["report_duration_seconds"], 755)

        saved = report_history.save_to_history(session["id"], session)
        with (
            patch.object(history_service, "_visible_to_owner", return_value=True),
            patch.object(history_service, "_find_history_for_login", return_value=saved),
        ):
            listed = history_service.get_history_list(None)
            detail = history_service.get_history_entry(session["id"], None, 1)

        self.assertEqual(listed[0]["report_duration_seconds"], 755)
        self.assertEqual(detail["report_duration_seconds"], 755)
        self.assertEqual(
            detail["report_completed_at"],
            "2026-08-25T10:12:34.600",
        )

    def test_legacy_version_without_timing_remains_compatible(self):
        session = {
            "report_md": "# 旧报告\n\n正文",
            "title": "旧报告",
            "created_at": "2026-08-24T10:00:00",
        }

        summary = report_version_summaries(session)[0]
        version = resolve_report_version(session, 1)

        self.assertNotIn("report_duration_seconds", summary)
        self.assertNotIn("report_duration_seconds", version)
        self.assertNotIn("plan_approved_at", version)
        self.assertNotIn("report_completed_at", version)

    def test_annotate_quality_duration_is_archived_and_listed(self):
        session = {
            "filename": "annotate.xlsx",
            "rows": [["ID", "Q1"], ["P1", "answer"]],
            "tasks": {"ai_detect": False, "quality": True},
            "quality_results": [{"id": "P1"}],
            "quality_started_at": "2026-08-25T10:00:00.000",
            "quality_completed_at": "2026-08-25T10:01:05.600",
            "quality_duration_seconds": 66,
        }

        report_history.save_annotate_to_history(
            "timed-annotate",
            session,
            "isolated-result.xlsx",
            "annotate-result.xlsx",
        )
        with patch.object(history_service, "_visible_to_owner", return_value=True):
            listed = history_service.get_history_list(None, "annotate")
        archived = history_storage._load_history()[0]

        self.assertEqual(archived["annotate_quality_duration_seconds"], 66)
        self.assertEqual(
            archived["annotate_quality_started_at"],
            "2026-08-25T10:00:00.000",
        )
        self.assertEqual(
            archived["annotate_quality_completed_at"],
            "2026-08-25T10:01:05.600",
        )
        self.assertEqual(listed[0]["annotate_quality_duration_seconds"], 66)

    def test_legacy_annotate_history_does_not_invent_quality_duration(self):
        report_history.save_annotate_to_history(
            "legacy-annotate",
            {
                "filename": "legacy.xlsx",
                "rows": [["ID", "Q1"], ["P1", "answer"]],
                "tasks": {"ai_detect": True, "quality": False},
            },
            "isolated-result.xlsx",
            "legacy-result.xlsx",
        )
        with patch.object(history_service, "_visible_to_owner", return_value=True):
            listed = history_service.get_history_list(None, "annotate")
        archived = history_storage._load_history()[0]

        self.assertNotIn("annotate_quality_duration_seconds", archived)
        self.assertIsNone(listed[0]["annotate_quality_duration_seconds"])


if __name__ == "__main__":
    unittest.main()
