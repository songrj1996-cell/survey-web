import unittest
from unittest.mock import AsyncMock, patch

from app.core.security import ALL_PERMS
from app.routers import interview
from app.storage.whitelist import _PERMS_SCHEMA_VERSION, _migrate_whitelist_perms


class InterviewPermissionMigrationTests(unittest.TestCase):
    def test_interview_is_an_independent_permission(self):
        self.assertEqual(
            ALL_PERMS,
            ["survey", "interview", "annotate", "comment"],
        )

    def test_v2_survey_user_receives_interview_once(self):
        users = [{
            "email": "legacy@example.com",
            "perms": ["survey", "comment"],
            "perms_v": 2,
        }]

        self.assertTrue(_migrate_whitelist_perms(users))
        self.assertEqual(users[0]["perms"], ["survey", "comment", "interview"])
        self.assertEqual(users[0]["perms_v"], _PERMS_SCHEMA_VERSION)
        self.assertFalse(_migrate_whitelist_perms(users))

    def test_current_user_can_keep_interview_disabled(self):
        users = [{
            "email": "current@example.com",
            "perms": ["survey"],
            "perms_v": _PERMS_SCHEMA_VERSION,
        }]

        self.assertFalse(_migrate_whitelist_perms(users))
        self.assertEqual(users[0]["perms"], ["survey"])


class InterviewRoutePermissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_status_requires_interview_permission(self):
        request = object()
        login = {"email": "researcher@example.com"}
        with (
            patch.object(interview, "_require_feature", new=AsyncMock(return_value=login)) as require,
            patch.object(interview, "get_interview_status", return_value={"status": "ready"}),
        ):
            result = await interview.interview_status("session-1", request)

        self.assertEqual(result, {"status": "ready"})
        require.assert_awaited_once_with(request, "interview")


if __name__ == "__main__":
    unittest.main()

