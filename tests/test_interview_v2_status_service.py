import unittest
from unittest.mock import patch

from app.services import interview_v2_status_service as service
from app.services.interview_v2_import_service import InterviewV2ImportError


IMPORT_ID = "import_" + "2" * 32
PROJECT_ID = "project_" + "1" * 32
LOGIN = {"email": "owner@example.com"}


def _mapping_status(status: str) -> dict:
    return {
        "import_id": IMPORT_ID,
        "project_id": PROJECT_ID,
        "workbook_revision_id": "workbook_" + "3" * 32,
        "status": status,
    }


class InterviewV2StatusServiceTests(unittest.TestCase):
    def test_unconfirmed_mapping_never_exposes_structure_checkpoint(self):
        public = _mapping_status("GROUP_CONFIRMATION_REQUIRED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(service.store, "load_structure_state") as load_state,
        ):
            result = service.get_interview_import_with_structure_status(
                IMPORT_ID, LOGIN
            )

        self.assertEqual(result["status"], "GROUP_CONFIRMATION_REQUIRED")
        load_state.assert_not_called()

    def test_ready_structure_without_boundary_requires_analysis_boundary(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(
                service.store,
                "load_structure_state",
                return_value={
                    "is_stale": False,
                    "derived_status": "READY_FOR_DOSSIERS",
                },
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_state",
                return_value=None,
            ),
        ):
            result = service.get_interview_import_with_structure_status(
                IMPORT_ID, LOGIN
            )

        self.assertEqual(result["status"], "ANALYSIS_BOUNDARY_REQUIRED")

    def test_analysis_boundary_review_checkpoint_overlays_ready_structure(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(
                service.store,
                "load_structure_state",
                return_value={
                    "is_stale": False,
                    "derived_status": "READY_FOR_DOSSIERS",
                },
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_state",
                return_value={
                    "is_stale": False,
                    "derived_status": "ANALYSIS_BOUNDARY_REVIEW_REQUIRED",
                },
            ),
        ):
            result = service.get_interview_import_with_structure_status(
                IMPORT_ID, LOGIN
            )

        self.assertEqual(result["status"], "ANALYSIS_BOUNDARY_REVIEW_REQUIRED")

    def test_confirmed_analysis_boundary_reaches_ready_for_dossiers(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(
                service.store,
                "load_structure_state",
                return_value={
                    "is_stale": False,
                    "derived_status": "READY_FOR_DOSSIERS",
                },
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_state",
                return_value={
                    "is_stale": False,
                    "derived_status": "READY_FOR_DOSSIERS",
                },
            ),
        ):
            result = service.get_interview_import_with_structure_status(
                IMPORT_ID, LOGIN
            )

        self.assertEqual(result["status"], "READY_FOR_DOSSIERS")

    def test_stale_analysis_boundary_returns_required_checkpoint(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(
                service.store,
                "load_structure_state",
                return_value={
                    "is_stale": False,
                    "derived_status": "READY_FOR_DOSSIERS",
                },
            ),
            patch.object(
                service.store,
                "load_analysis_boundary_state",
                return_value={
                    "is_stale": True,
                    "derived_status": "READY_FOR_DOSSIERS",
                },
            ),
        ):
            result = service.get_interview_import_with_structure_status(
                IMPORT_ID, LOGIN
            )

        self.assertEqual(result["status"], "ANALYSIS_BOUNDARY_REQUIRED")

    def test_ready_checkpoint_includes_dossier_progress_summary(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        participant_id = "participant_" + "4" * 32
        with (
            patch.object(service, "get_interview_import_with_mapping_status", return_value=public),
            patch.object(service.store, "load_structure_state", return_value={
                "is_stale": False, "derived_status": "READY_FOR_DOSSIERS",
            }),
            patch.object(service.store, "load_analysis_boundary_state", return_value={
                "is_stale": False, "derived_status": "READY_FOR_DOSSIERS",
                "current_evidence_revision_id": "evidence_" + "5" * 32,
            }),
            patch.object(service.store, "load_evidence_revision", return_value={
                "expected_participants": [{"participant_id": participant_id, "group_id": "group_" + "6" * 32}],
            }),
            patch.object(service.store, "load_current_participant_dossier", return_value={
                "revision": {"status": "approved"}, "state": {},
            }),
        ):
            result = service.get_interview_import_with_structure_status(IMPORT_ID, LOGIN)

        self.assertEqual(1, result["dossier_summary"]["participant_count"])
        self.assertEqual(1, result["dossier_summary"]["approved_count"])

    def test_stale_structure_does_not_override_current_mapping_checkpoint(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(
                service.store,
                "load_structure_state",
                return_value={
                    "is_stale": True,
                    "derived_status": "STALE",
                    "effective_status": "READY_FOR_DOSSIERS",
                },
            ),
        ):
            result = service.get_interview_import_with_structure_status(
                IMPORT_ID, LOGIN
            )

        self.assertEqual(result["status"], "GROUP_MAPPING_CONFIRMED")

    def test_corrupt_structure_state_uses_stable_service_error(self):
        public = _mapping_status("GROUP_MAPPING_CONFIRMED")
        with (
            patch.object(
                service,
                "get_interview_import_with_mapping_status",
                return_value=public,
            ),
            patch.object(
                service.store,
                "load_structure_state",
                side_effect=ValueError("digest mismatch"),
            ),
        ):
            with self.assertRaises(InterviewV2ImportError) as caught:
                service.get_interview_import_with_structure_status(
                    IMPORT_ID, LOGIN
                )

        self.assertEqual(caught.exception.status_code, 500)
        self.assertEqual(caught.exception.code, "STRUCTURE_PERSISTENCE_FAILED")


if __name__ == "__main__":
    unittest.main()
