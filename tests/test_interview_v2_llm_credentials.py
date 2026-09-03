import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.routers import interview_v2 as router_module
from app.services.interview_v2_import_service import InterviewV2ImportError


PROJECT_ID = "project_" + "1" * 32
PARTICIPANT_ID = "participant_" + "2" * 32
SECTION_ID = "section_" + "3" * 32
LOGIN = {"open_id": "viewer"}


class InterviewV2LLMCredentialRouteTests(unittest.IsolatedAsyncioTestCase):
    def _cases(self):
        return (
            {
                "name": "dossier",
                "endpoint": router_module.regenerate_interview_v2_dossier,
                "resource_id": PARTICIPANT_ID,
                "payload": {
                    "project_id": PROJECT_ID,
                    "base_dossier_version_id": None,
                },
                "access_name": "get_current_dossier",
                "access_args": (PROJECT_ID, PARTICIPANT_ID, LOGIN),
                "service_name": "regenerate_dossier",
                "action": "V2 玩家档案生成",
                "reference_id": PARTICIPANT_ID,
            },
            {
                "name": "analysis",
                "endpoint": router_module.create_interview_v2_analysis_run,
                "resource_id": PROJECT_ID,
                "payload": {
                    "base_analysis_run_id": None,
                    "freeze_current": True,
                },
                "access_name": "get_current_analysis",
                "access_args": (PROJECT_ID, LOGIN),
                "service_name": "create_analysis_run",
                "action": "V2 跨玩家分析",
                "reference_id": PROJECT_ID,
            },
            {
                "name": "report",
                "endpoint": router_module.create_interview_v2_report,
                "resource_id": PROJECT_ID,
                "payload": {
                    "base_report_version_id": None,
                    "freeze_current": True,
                },
                "access_name": "get_current_analysis",
                "access_args": (PROJECT_ID, LOGIN),
                "service_name": "create_report",
                "action": "V2 访谈报告生成",
                "reference_id": PROJECT_ID,
            },
            {
                "name": "reaudit",
                "endpoint": router_module.reaudit_interview_v2_report_section,
                "resource_id": SECTION_ID,
                "payload": {
                    "base_section_revision": 1,
                    "reaudit_job_id": "job_" + "4" * 32,
                },
                "access_name": "validate_report_section_access",
                "access_args": (SECTION_ID, LOGIN),
                "service_name": "reaudit_report_section",
                "action": "V2 报告章节重审",
                "reference_id": SECTION_ID,
            },
        )

    async def test_ai_routes_authorize_before_key_and_record_usage_context(self):
        for case in self._cases():
            with self.subTest(case=case["name"]):
                order = []
                request = SimpleNamespace(headers={})
                expected_result = {"operation": case["name"]}

                def access_side_effect(*_args):
                    order.append("access")
                    return {"status": "authorized"}

                async def key_side_effect(_request):
                    order.append("key")
                    return "personal-api-key"

                async def service_side_effect(*_args, **_kwargs):
                    order.append("service")
                    return expected_result

                async def runner_side_effect(awaitable, api_key, **kwargs):
                    order.append("runner")
                    self.assertEqual("personal-api-key", api_key)
                    self.assertIs(request, kwargs["request"])
                    self.assertEqual("interview", kwargs["category"])
                    self.assertEqual(case["action"], kwargs["action"])
                    self.assertEqual(case["reference_id"], kwargs["reference_id"])
                    return await awaitable

                with (
                    patch.object(router_module, "INTERVIEW_V2_ENABLED", True),
                    patch.object(
                        router_module,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ),
                    patch.object(
                        router_module,
                        "_read_structure_json",
                        new=AsyncMock(return_value=case["payload"]),
                    ),
                    patch.object(
                        router_module,
                        case["access_name"],
                        side_effect=access_side_effect,
                    ) as access_mock,
                    patch.object(
                        router_module,
                        "require_request_llm_api_key",
                        new=AsyncMock(side_effect=key_side_effect),
                    ),
                    patch.object(
                        router_module,
                        case["service_name"],
                        new=AsyncMock(side_effect=service_side_effect),
                    ),
                    patch.object(
                        router_module,
                        "run_with_llm_api_key",
                        new=AsyncMock(side_effect=runner_side_effect),
                    ),
                    patch.object(router_module, "audit_log", new=AsyncMock()),
                ):
                    result = await case["endpoint"](
                        case["resource_id"], request
                    )

                self.assertEqual(expected_result, result)
                access_mock.assert_called_once_with(*case["access_args"])
                self.assertEqual(["access", "key", "runner", "service"], order)

    async def test_owner_failure_is_hidden_before_key_lookup(self):
        for case in self._cases():
            with self.subTest(case=case["name"]):
                request = SimpleNamespace(headers={})
                not_found = InterviewV2ImportError(
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                    message="未找到资源。",
                )
                key_mock = AsyncMock(return_value="personal-api-key")
                service_mock = AsyncMock(return_value={})

                with (
                    patch.object(router_module, "INTERVIEW_V2_ENABLED", True),
                    patch.object(
                        router_module,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ),
                    patch.object(
                        router_module,
                        "_read_structure_json",
                        new=AsyncMock(return_value=case["payload"]),
                    ),
                    patch.object(
                        router_module,
                        case["access_name"],
                        side_effect=not_found,
                    ),
                    patch.object(
                        router_module,
                        "require_request_llm_api_key",
                        new=key_mock,
                    ),
                    patch.object(
                        router_module,
                        case["service_name"],
                        new=service_mock,
                    ),
                    patch.object(router_module, "audit_log", new=AsyncMock()),
                ):
                    response = await case["endpoint"](
                        case["resource_id"], request
                    )

                self.assertEqual(404, response.status_code)
                self.assertEqual(
                    "RESOURCE_NOT_FOUND",
                    json.loads(response.body)["error"]["code"],
                )
                key_mock.assert_not_awaited()
                service_mock.assert_not_awaited()

    async def test_missing_key_stops_each_ai_route_after_authorization(self):
        for case in self._cases():
            with self.subTest(case=case["name"]):
                request = SimpleNamespace(headers={})
                service_mock = AsyncMock(return_value={})
                key_required = HTTPException(
                    status_code=428,
                    detail={
                        "code": "USER_LLM_KEY_REQUIRED",
                        "message": "请先在个人中心填写 LLM API Key",
                    },
                )

                with (
                    patch.object(router_module, "INTERVIEW_V2_ENABLED", True),
                    patch.object(
                        router_module,
                        "_require_feature",
                        new=AsyncMock(return_value=LOGIN),
                    ),
                    patch.object(
                        router_module,
                        "_read_structure_json",
                        new=AsyncMock(return_value=case["payload"]),
                    ),
                    patch.object(
                        router_module,
                        case["access_name"],
                        return_value={"status": "authorized"},
                    ) as access_mock,
                    patch.object(
                        router_module,
                        "require_request_llm_api_key",
                        new=AsyncMock(side_effect=key_required),
                    ),
                    patch.object(
                        router_module,
                        case["service_name"],
                        new=service_mock,
                    ),
                    patch.object(router_module, "audit_log", new=AsyncMock()),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await case["endpoint"](case["resource_id"], request)

                self.assertEqual(428, raised.exception.status_code)
                access_mock.assert_called_once_with(*case["access_args"])
                service_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
