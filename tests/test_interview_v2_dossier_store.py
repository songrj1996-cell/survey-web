import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage import interview_v2_store as store


PROJECT = "project_" + "1" * 32
PARTICIPANT = "participant_" + "2" * 32
DOSSIER1 = "dossier_" + "3" * 32


class DossierStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="iv2-dossier-")
        self.patch = patch.object(store.config, "INTERVIEW_V2_DATA_DIR", Path(self.temp.name))
        self.patch.start()
        (Path(self.temp.name) / "projects" / PROJECT).mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def revision(self):
        return {"dossier_version_id": DOSSIER1, "source": {"evidence_revision_id": "evidence_" + "4" * 32},
                "attributes": {}, "dossier": {}, "status": "generated", "created_at": "2026-08-24T00:00:00Z"}

    def test_save_load_and_cas(self):
        saved = store.save_participant_dossier_cas(
            project_id=PROJECT, participant_id=PARTICIPANT,
            base_dossier_version_id=None, revision=self.revision()
        )
        self.assertEqual(1, saved["state"]["current_version_number"])
        loaded = store.load_current_participant_dossier(PROJECT, PARTICIPANT)
        self.assertEqual(DOSSIER1, loaded["revision"]["dossier_version_id"])
        with self.assertRaises(ValueError):
            store.save_participant_dossier_cas(
                project_id=PROJECT, participant_id=PARTICIPANT,
                base_dossier_version_id=None,
                revision={**self.revision(), "dossier_version_id": "dossier_" + "5" * 32},
            )

    def test_review_creates_new_immutable_version(self):
        store.save_participant_dossier_cas(project_id=PROJECT, participant_id=PARTICIPANT,
                                           base_dossier_version_id=None, revision=self.revision())
        reviewed = store.review_participant_dossier_cas(
            project_id=PROJECT, participant_id=PARTICIPANT,
            base_dossier_version_id=DOSSIER1, decision="approved", note="ok",
            actor="tester", reviewed_at="2026-08-24T01:00:00Z"
        )
        self.assertEqual(2, reviewed["state"]["current_version_number"])
        self.assertEqual("approved", reviewed["revision"]["status"])
        self.assertTrue((Path(self.temp.name) / "projects" / PROJECT / "participant_dossiers" / PARTICIPANT / "versions" / f"{DOSSIER1}.json").exists())


if __name__ == "__main__":
    unittest.main()
