import unittest

from app.routers.interview_v2 import router


class DossierRouteContractTests(unittest.TestCase):
    def test_batch_4a_routes_are_registered(self):
        paths = {route.path for route in router.routes}
        self.assertIn("/api/v1/interview-projects/{project_id}/participants", paths)
        self.assertIn("/api/v1/interview-participants/{participant_id}/dossiers/current", paths)
        self.assertIn("/api/v1/interview-participants/{participant_id}/dossiers:regenerate", paths)
        self.assertIn("/api/v1/interview-participants/{participant_id}/dossiers:review", paths)


if __name__ == "__main__":
    unittest.main()
