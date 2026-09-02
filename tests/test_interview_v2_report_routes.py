import unittest

from pydantic import ValidationError

from app.routers.interview_v2 import router
from app.schemas.interview_v2 import InterviewV2ImportResponse
from app.schemas.interview_v2_report import (
    InterviewV2ReportApproveRequest,
    InterviewV2ReportCreateRequest,
    InterviewV2ReportSectionPatchRequest,
    InterviewV2ReportSectionReauditRequest,
)


class InterviewV2ReportRouteTests(unittest.TestCase):
    def test_routes_are_registered(self):
        paths = {route.path for route in router.routes}
        self.assertIn("/api/v1/interview-projects/{project_id}/reports", paths)
        self.assertIn("/api/v1/interview-reports/{report_version_id}", paths)
        self.assertIn("/api/v1/interview-reports/{report_version_id}/claims/{claim_id}", paths)
        self.assertIn("/api/v1/interview-report-sections/{section_id}", paths)
        self.assertIn("/api/v1/interview-report-sections/{section_id}:reaudit", paths)
        self.assertIn("/api/v1/interview-reports/{report_version_id}:approve", paths)

    def test_create_contract_forbids_unfrozen_or_extra_input(self):
        with self.assertRaises(ValidationError):
            InterviewV2ReportCreateRequest.model_validate({"freeze_current": False})
        with self.assertRaises(ValidationError):
            InterviewV2ReportCreateRequest.model_validate({"freeze_current": True, "report": "model text"})

    def test_review_contracts_require_exact_revision_and_lock(self):
        with self.assertRaises(ValidationError):
            InterviewV2ReportSectionPatchRequest.model_validate({
                "base_section_revision": 1, "content": "正文", "locked": False,
            })
        with self.assertRaises(ValidationError):
            InterviewV2ReportSectionReauditRequest.model_validate({
                "base_section_revision": 1, "reaudit_job_id": "job_invalid",
            })
        approved = InterviewV2ReportApproveRequest.model_validate({
            "base_report_version_id": "report_" + "a" * 32,
            "decision": "approved",
        })
        self.assertEqual("approved", approved.decision)

    def test_import_response_preserves_v2_progress_summaries(self):
        response = InterviewV2ImportResponse.model_validate({
            "import_id": "import_" + "1" * 32,
            "project_id": "project_" + "2" * 32,
            "workbook_revision_id": "workbook_" + "3" * 32,
            "status": "READY_FOR_DOSSIERS",
            "dossier_summary": {
                "participant_count": 1,
                "blocking_participant_ids": ["participant_" + "4" * 32],
                "analysis_ready": False,
            },
            "analysis_summary": {
                "analysis_run_id": "analysis_" + "5" * 32,
                "report_ready": True,
            },
            "report_summary": {
                "report_version_id": "report_" + "6" * 32,
                "approval_ready": False,
            },
        }).model_dump(mode="json")

        self.assertEqual(
            ["participant_" + "4" * 32],
            response["dossier_summary"]["blocking_participant_ids"],
        )
        self.assertFalse(response["dossier_summary"]["analysis_ready"])
        self.assertTrue(response["analysis_summary"]["report_ready"])
        self.assertFalse(response["report_summary"]["approval_ready"])
