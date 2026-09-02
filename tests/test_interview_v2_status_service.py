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
                "current_structure_revision_id": "structure_" + "7" * 32,
                "current_boundary_revision_id": "boundary_" + "8" * 32,
                "current_boundary_payload_sha256": "9" * 64,
                "current_coverage_revision_id": "coverage_" + "a" * 32,
                "current_coverage_payload_sha256": "b" * 64,
            }),
            patch.object(service.store, "load_evidence_revision", return_value={
                "expected_participants": [{"participant_id": participant_id, "group_id": "group_" + "6" * 32}],
            }),
            patch.object(service.store, "load_current_participant_dossier", return_value={
                "revision": {"status": "approved", "source": {
                    "structure_revision_id": "structure_" + "7" * 32,
                    "evidence_revision_id": "evidence_" + "5" * 32,
                    "boundary_revision_id": "boundary_" + "8" * 32,
                    "boundary_payload_sha256": "9" * 64,
                    "coverage_revision_id": "coverage_" + "a" * 32,
                    "coverage_payload_sha256": "b" * 64,
                }, "dossier_version_id": "dossier_" + "c" * 32},
                "state": {"current_dossier_version_id": "dossier_" + "c" * 32},
            }),
            patch.object(service.store, "load_current_analysis_run", return_value={
                "revision": {
                    "analysis_run_id": "analysis_" + "d" * 32,
                    "revision_payload_sha256": "e" * 64,
                    "status": "completed", "findings": [{"finding_id": "finding_" + "f" * 32}],
                    "source": {
                        "structure_revision_id": "structure_" + "7" * 32,
                        "evidence_revision_id": "evidence_" + "5" * 32,
                        "boundary_revision_id": "boundary_" + "8" * 32,
                        "boundary_payload_sha256": "9" * 64,
                        "coverage_revision_id": "coverage_" + "a" * 32,
                        "coverage_payload_sha256": "b" * 64,
                        "dossier_versions": [{
                            "participant_id": participant_id,
                            "dossier_version_id": "dossier_" + "c" * 32,
                        }],
                    },
                }
            }),
            patch.object(service.store, "load_current_report_version", return_value={
                "revision": {
                    "report_version_id": "report_" + "0" * 32,
                    "status": "draft", "audit_status": "audited",
                    "source": {
                        "analysis_run_id": "analysis_" + "d" * 32,
                        "analysis_revision_payload_sha256": "e" * 64,
                    },
                }
            }),
        ):
            result = service.get_interview_import_with_structure_status(IMPORT_ID, LOGIN)

        self.assertEqual(1, result["dossier_summary"]["participant_count"])
        self.assertEqual(1, result["dossier_summary"]["approved_count"])
        self.assertTrue(result["dossier_summary"]["analysis_ready"])
        self.assertEqual("completed", result["analysis_summary"]["status"])
        self.assertEqual(1, result["analysis_summary"]["finding_count"])
        self.assertEqual("draft", result["report_summary"]["status"])
        self.assertEqual("audited", result["report_summary"]["audit_status"])

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
