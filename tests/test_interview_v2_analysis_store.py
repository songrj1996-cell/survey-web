import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
PARTICIPANT = "participant_" + "2" * 32
DOSSIER = "dossier_" + "3" * 32
ANALYSIS = "analysis_" + "4" * 32
EVIDENCE = "evidence_" + "5" * 32
BOUNDARY = "boundary_" + "6" * 32
COVERAGE = "coverage_" + "7" * 32
STRUCTURE = "structure_" + "a" * 32
BOUNDARY_SHA = "b" * 64
COVERAGE_SHA = "c" * 64


class InterviewV2AnalysisStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iv2-analysis-")
        self.patch = patch.object(store.config, "INTERVIEW_V2_DATA_DIR", Path(self.temp.name))
        self.patch.start()
        self.project_dir = Path(self.temp.name) / "projects" / PROJECT
        self.project_dir.mkdir(parents=True)
        self.source = {
            "structure_revision_id": STRUCTURE,
            "evidence_revision_id": EVIDENCE,
            "boundary_revision_id": BOUNDARY,
            "boundary_payload_sha256": BOUNDARY_SHA,
            "coverage_revision_id": COVERAGE,
            "coverage_payload_sha256": COVERAGE_SHA,
            "dossier_versions": [{"participant_id": PARTICIPANT, "dossier_version_id": DOSSIER,
                                  "revision_payload_sha256": "0" * 64}],
        }
        store._atomic_write_json(store._analysis_boundary_state_path(PROJECT), {
            "current_structure_revision_id": STRUCTURE,
            "current_evidence_revision_id": EVIDENCE,
            "current_boundary_revision_id": BOUNDARY,
            "current_boundary_payload_sha256": BOUNDARY_SHA,
            "current_coverage_revision_id": COVERAGE,
            "current_coverage_payload_sha256": COVERAGE_SHA,
            "is_stale": False,
        })
        saved_dossier = store.save_participant_dossier_cas(
            project_id=PROJECT, participant_id=PARTICIPANT,
            base_dossier_version_id=None,
            revision={"dossier_version_id": DOSSIER, "source": {}, "status": "approved",
                      "attributes": {}, "dossier": {}, "created_at": "2026-08-31T00:00:00Z"},
        )
        self.source["dossier_versions"][0]["revision_payload_sha256"] = (
            saved_dossier["revision"]["revision_payload_sha256"]
        )

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def revision(self, analysis_run_id=ANALYSIS):
        return {
            "analysis_run_id": analysis_run_id,
            "source": self.source,
            "status": "completed",
            "findings": [],
            "stat_facts": [],
            "created_at": "2026-08-31T01:00:00Z",
        }

    def test_save_load_and_compare_and_swap(self):
        saved = store.save_analysis_run_cas(
            project_id=PROJECT, base_analysis_run_id=None, revision=self.revision()
        )
        self.assertEqual(1, saved["state"]["current_version_number"])
        loaded = store.load_current_analysis_run(PROJECT)
        self.assertEqual(ANALYSIS, loaded["revision"]["analysis_run_id"])
        with self.assertRaises(ValueError):
            store.save_analysis_run_cas(
                project_id=PROJECT, base_analysis_run_id=None,
                revision=self.revision("analysis_" + "8" * 32),
            )

    def test_changed_dossier_head_blocks_publish(self):
        next_dossier = "dossier_" + "9" * 32
        store.save_participant_dossier_cas(
            project_id=PROJECT, participant_id=PARTICIPANT,
            base_dossier_version_id=DOSSIER,
            revision={"dossier_version_id": next_dossier, "source": {}, "status": "approved",
                      "attributes": {}, "dossier": {}, "created_at": "2026-08-31T00:30:00Z"},
        )
        with self.assertRaisesRegex(ValueError, "analysis input changed"):
            store.save_analysis_run_cas(
                project_id=PROJECT, base_analysis_run_id=None, revision=self.revision()
            )

    def test_tampered_dossier_payload_blocks_publish_even_when_head_is_same(self):
        dossier_path = (
            self.project_dir / "participant_dossiers" / PARTICIPANT
            / "versions" / f"{DOSSIER}.json"
        )
        payload = store._read_json(dossier_path)
        payload["status"] = "needs_changes"
        store._atomic_write_json(dossier_path, payload)
        with self.assertRaisesRegex(ValueError, "analysis input changed"):
            store.save_analysis_run_cas(
                project_id=PROJECT, base_analysis_run_id=None, revision=self.revision()
            )


if __name__ == "__main__":
    unittest.main()
