import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core import config
from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
ANALYSIS = "analysis_" + "2" * 32
REPORT = "report_" + "3" * 32


class InterviewV2ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="interview-v2-report-store-")
        self.patch = patch.object(config, "INTERVIEW_V2_DATA_DIR", self.temp.name)
        self.patch.start()
        analysis = {
            "analysis_run_id": ANALYSIS, "project_id": PROJECT,
            "status": "completed", "source": {}, "created_at": "2026-09-02T00:00:00Z",
        }
        analysis["revision_payload_sha256"] = store._analysis_digest(analysis)
        directory = Path(self.temp.name) / "projects" / PROJECT / "analysis_runs"
        store._atomic_write_json(directory / "versions" / f"{ANALYSIS}.json", analysis)
        store._atomic_write_json(directory / "state.json", {
            "project_id": PROJECT, "current_analysis_run_id": ANALYSIS,
            "current_version_number": 1, "history": [],
        })

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def _revision(self):
        analysis = store.load_current_analysis_run(PROJECT)["revision"]
        return {
            "report_version_id": REPORT,
            "source": {
                "analysis_run_id": ANALYSIS,
                "analysis_revision_payload_sha256": analysis["revision_payload_sha256"],
            },
            "status": "draft", "audit_status": "audited",
            "sections": [], "claims": [], "audit_issues": [],
            "created_at": "2026-09-02T00:01:00Z",
        }

    def test_saves_immutable_report_and_loads_by_global_id(self):
        saved = store.save_report_version_cas(
            project_id=PROJECT, base_report_version_id=None, revision=self._revision()
        )
        loaded = store.load_report_version(REPORT)
        self.assertEqual(1, saved["revision"]["version_number"])
        self.assertEqual(PROJECT, loaded["project_id"])
        self.assertEqual(REPORT, loaded["revision"]["report_version_id"])

    def test_rejects_when_analysis_head_changed(self):
        revision = self._revision()
        state_path = Path(self.temp.name) / "projects" / PROJECT / "analysis_runs" / "state.json"
        store._atomic_write_json(state_path, {
            "project_id": PROJECT,
            "current_analysis_run_id": "analysis_" + "9" * 32,
            "current_version_number": 2, "history": [],
        })
        with self.assertRaisesRegex(ValueError, "report input changed"):
            store.save_report_version_cas(
                project_id=PROJECT, base_report_version_id=None, revision=revision
            )
