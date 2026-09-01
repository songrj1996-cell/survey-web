import unittest

from app.routers.interview_v2 import router


class InterviewV2AnalysisRouteContractTests(unittest.TestCase):
    def test_batch_5a_routes_are_registered(self):
        paths = {route.path for route in router.routes}
        self.assertIn(
            "/api/v1/interview-projects/{project_id}/analysis-runs", paths
        )
        self.assertIn(
            "/api/v1/interview-projects/{project_id}/analysis-runs/current", paths
        )


if __name__ == "__main__":
    unittest.main()
