import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from app.routers import interview_v2 as router_module
from app.schemas.interview_v2_report import InterviewV2ReportRerunRequest
from app.services.interview_v2_import_service import InterviewV2ImportError


PROJECT = "project_" + "1" * 32
REPORT = "report_" + "2" * 32
SECTION = "section_" + "3" * 32
LOGIN = {"email": "owner@example.com"}
KEY = "rerun-route-01"


def _payload():
    return {
        "from_stage": "report_section",
        "base_report_version_id": REPORT,
        "section_id": SECTION,
        "base_section_revision": 2,
        "instruction": "",
        "preserve_manual_report_edits": True,
        "reuse_unchanged_artifacts": True,
        "force": False,
    }


class InterviewV2ReportRerunRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_route_and_strict_request_contract_are_registered(self):
        paths = {route.path for route in router_module.router.routes}
        self.assertIn(
            "/api/v1/interview-projects/{project_id}/reruns", paths
        )
        request = InterviewV2ReportRerunRequest.model_validate(_payload())
        self.assertEqual("report_section", request.from_stage)
        self.assertEqual(2, request.base_section_revision)

        invalid_payloads = (
            {**_payload(), "from_stage": "report"},
            {**_payload(), "base_section_revision": True},
            {**_payload(), "preserve_manual_report_edits": False},
            {**_payload(), "reuse_unchanged_artifacts": False},
            {**_payload(), "force": True},
            {**_payload(), "extra": "forbidden"},
        )
        for invalid in invalid_payloads:
            with self.subTest(payload=invalid), self.assertRaises(ValidationError):
                InterviewV2ReportRerunRequest.model_validate(invalid)

    async def test_owner_access_precedes_key_lookup_and_usage_wrapper(self):
        order = []
        request = SimpleNamespace(headers={"Idempotency-Key": KEY})
        expected = {
            "report_version_id": "report_" + "4" * 32,
            "rerun": {"rerun_id": "rerun_" + "5" * 32, "reused": False},
        }

        def authorize(*_args):
            order.append("access")

        async def key_lookup(_request):
            order.append("key")
            return "personal-key"

        async def rerun(*_args):
            order.append("service")
            return expected

        async def wrapper(awaitable, api_key, **kwargs):
            order.append("wrapper")
            self.assertEqual("personal-key", api_key)
            self.assertEqual("interview", kwargs["category"])
            self.assertEqual("V2 报告单章节重生成", kwargs["action"])
            self.assertEqual(SECTION, kwargs["reference_id"])
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
                new=AsyncMock(return_value=_payload()),
            ),
            patch.object(
                router_module,
                "validate_report_rerun_access",
                side_effect=authorize,
            ) as access_mock,
            patch.object(
                router_module,
                "require_request_llm_api_key",
                new=AsyncMock(side_effect=key_lookup),
            ),
            patch.object(
                router_module,
                "create_report_section_rerun",
                new=AsyncMock(side_effect=rerun),
            ) as service_mock,
            patch.object(
                router_module,
                "run_with_llm_api_key",
                new=AsyncMock(side_effect=wrapper),
            ),
            patch.object(router_module, "audit_log", new=AsyncMock()) as audit,
        ):
            result = await router_module.rerun_interview_v2_report_section(
                PROJECT, request
            )

        self.assertEqual(expected, result)
        access_mock.assert_called_once_with(PROJECT, _payload(), LOGIN)
        service_mock.assert_awaited_once_with(PROJECT, _payload(), LOGIN, KEY)
        audit.assert_awaited_once()
        self.assertEqual(["access", "key", "wrapper", "service"], order)

    async def test_owner_failure_is_hidden_before_key_lookup(self):
        request = SimpleNamespace(headers={"Idempotency-Key": KEY})
        not_found = InterviewV2ImportError(
            status_code=404,
            code="INTERVIEW_REPORT_NOT_FOUND",
            message="未找到该报告版本。",
        )
        key_mock = AsyncMock(return_value="personal-key")
        service_mock = AsyncMock()
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
                new=AsyncMock(return_value=_payload()),
            ),
            patch.object(
                router_module,
                "validate_report_rerun_access",
                side_effect=not_found,
            ),
            patch.object(
                router_module,
                "require_request_llm_api_key",
                new=key_mock,
            ),
            patch.object(
                router_module,
                "create_report_section_rerun",
                new=service_mock,
            ),
        ):
            response = await router_module.rerun_interview_v2_report_section(
                PROJECT, request
            )

        self.assertEqual(404, response.status_code)
        self.assertEqual(
            "INTERVIEW_REPORT_NOT_FOUND",
            json.loads(response.body)["error"]["code"],
        )
        key_mock.assert_not_awaited()
        service_mock.assert_not_awaited()

    async def test_missing_idempotency_key_stops_before_access_and_model(self):
        request = SimpleNamespace(headers={})
        access_mock = MagicMock()
        key_mock = AsyncMock()
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
                new=AsyncMock(return_value=_payload()),
            ),
            patch.object(
                router_module,
                "validate_report_rerun_access",
                new=access_mock,
            ),
            patch.object(
                router_module,
                "require_request_llm_api_key",
                new=key_mock,
            ),
        ):
            response = await router_module.rerun_interview_v2_report_section(
                PROJECT, request
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "RERUN_IDEMPOTENCY_KEY_INVALID",
            json.loads(response.body)["error"]["code"],
        )
        access_mock.assert_not_called()
        key_mock.assert_not_awaited()

    async def test_invalid_nonempty_key_is_rejected_after_owner_before_key_lookup(self):
        request = SimpleNamespace(headers={"Idempotency-Key": "bad key"})
        key_mock = AsyncMock()
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
                new=AsyncMock(return_value=_payload()),
            ),
            patch.object(
                router_module,
                "validate_report_rerun_access",
                return_value=None,
            ) as access_mock,
            patch.object(
                router_module,
                "require_request_llm_api_key",
                new=key_mock,
            ),
        ):
            response = await router_module.rerun_interview_v2_report_section(
                PROJECT, request
            )

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            "RERUN_IDEMPOTENCY_KEY_INVALID",
            json.loads(response.body)["error"]["code"],
        )
        access_mock.assert_called_once_with(PROJECT, _payload(), LOGIN)
        key_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
