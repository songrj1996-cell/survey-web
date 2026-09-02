import unittest

from pydantic import ValidationError

from app.routers.interview_v2 import router
from app.schemas.interview_v2_report import InterviewV2ReportCreateRequest


class InterviewV2ReportRouteTests(unittest.TestCase):
    def test_routes_are_registered(self):
        paths = {route.path for route in router.routes}
        self.assertIn("/api/v1/interview-projects/{project_id}/reports", paths)
        self.assertIn("/api/v1/interview-reports/{report_version_id}", paths)
        self.assertIn("/api/v1/interview-reports/{report_version_id}/claims/{claim_id}", paths)

    def test_create_contract_forbids_unfrozen_or_extra_input(self):
        with self.assertRaises(ValidationError):
            InterviewV2ReportCreateRequest.model_validate({"freeze_current": False})
        with self.assertRaises(ValidationError):
            InterviewV2ReportCreateRequest.model_validate({"freeze_current": True, "report": "model text"})
