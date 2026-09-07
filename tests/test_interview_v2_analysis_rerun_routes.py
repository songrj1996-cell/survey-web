from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
from pydantic import ValidationError

from app.routers import interview_v2 as router
from app.schemas.interview_v2_rerun import RERUN_REQUEST_ADAPTER
from app.services.interview_v2_import_service import InterviewV2ImportError
from tests import test_interview_v2_analysis_rerun as f
from tests.test_interview_v2_analysis_rerun_service import payload


class AnalysisModuleRerunRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(router, "INTERVIEW_V2_ENABLED", True))
        self.stack.enter_context(patch.object(router, "_require_feature", new=AsyncMock(return_value=f.LOGIN)))
        self.read = self.stack.enter_context(patch.object(router, "_read_structure_json", new=AsyncMock(return_value=payload())))
        self.access = self.stack.enter_context(patch.object(router, "validate_analysis_rerun_access"))
        self.key = self.stack.enter_context(patch.object(router, "require_request_llm_api_key", new=AsyncMock(return_value="mock-key")))
        self.service = self.stack.enter_context(patch.object(router, "create_analysis_module_rerun", new=AsyncMock(return_value={
            "project_id": f.PROJECT, "analysis_run_id": f.NEXT, "analysis_version_number": 2,
            "status": "completed", "is_current_version": True, "rerun": {"reused": False},
        })))
        self.audit = self.stack.enter_context(patch.object(router, "audit_log", new=AsyncMock()))
        self.wrapper = self.stack.enter_context(patch.object(router, "run_with_llm_api_key", new=AsyncMock(side_effect=self.wrap)))

    async def wrap(self, awaitable, _api_key, **_kwargs):
        return await awaitable

    async def invoke(self, key="route-key"):
        return await router.rerun_interview_v2_report_section(f.PROJECT, SimpleNamespace(headers={"Idempotency-Key": key}))

    def test_discriminated_schema_keeps_report_contract_and_rejects_bad_module_requests(self):
        result = RERUN_REQUEST_ADAPTER.validate_python(payload())
        self.assertEqual(f.MODULE_A, result.module_id)
        report = {"from_stage": "report_section", "base_report_version_id": "report_" + "1" * 32, "section_id": "section_" + "2" * 32, "base_section_revision": 1}
        self.assertEqual("report_section", RERUN_REQUEST_ADAPTER.validate_python(report).from_stage)
        for change in (
            {"module_id": "../outside"}, {"module_id": [f.MODULE_A, f.MODULE_B]},
            {"base_analysis_run_id": None}, {"from_stage": "participant_dossier"},
            {"force": True}, {"force": 0}, {"reuse_unchanged_artifacts": 1},
            {"preserve_manual_report_edits": False}, {"section_id": "section_" + "1" * 32},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RERUN_REQUEST_ADAPTER.validate_python({**payload(), **change})

    async def test_owner_check_precedes_key_and_usage_context(self):
        order = []
        self.access.side_effect = lambda *_args: order.append("access")

        async def key(_request):
            order.append("key")
            return "mock-key"

        async def wrap(awaitable, api_key, **kwargs):
            order.append("wrapper")
            self.assertEqual("mock-key", api_key)
            self.assertEqual("interview", kwargs["category"])
            self.assertEqual("V2 单模块分析重跑", kwargs["action"])
            self.assertEqual(f.MODULE_A, kwargs["reference_id"])
            return await awaitable

        self.key.side_effect = key
        self.wrapper.side_effect = wrap
        result = await self.invoke()
        self.assertEqual(["access", "key", "wrapper"], order)
        self.assertEqual(f.NEXT, result["analysis_run_id"])
        self.access.assert_called_once_with(f.PROJECT, payload(), f.LOGIN)
        self.service.assert_awaited_once_with(f.PROJECT, payload(), f.LOGIN, "route-key")
        self.audit.assert_awaited_once()

    async def test_owner_denial_is_404_without_key_lookup(self):
        self.access.side_effect = InterviewV2ImportError(status_code=404, code="INTERVIEW_ANALYSIS_NOT_FOUND", message="未找到")
        result = await self.invoke()
        self.assertEqual(404, result.status_code)
        self.key.assert_not_awaited()
        self.service.assert_not_awaited()

    async def test_bad_contract_or_key_fails_before_model(self):
        self.read.return_value = {**payload(), "force": True}
        result = await self.invoke()
        self.assertEqual(400, result.status_code)
        self.assertIn("ANALYSIS_RERUN_REQUEST_INVALID", result.body.decode())
        self.read.return_value = payload()
        for key in ("", "invalid key"):
            result = await self.invoke(key)
            self.assertEqual(400, result.status_code)
        self.key.assert_not_awaited()
        self.service.assert_not_awaited()

    async def test_service_conflict_remains_structured_and_does_not_log_success(self):
        self.service.side_effect = InterviewV2ImportError(status_code=409, code="ANALYSIS_INPUT_CHANGED", message="输入已变化")
        result = await self.invoke()
        self.assertEqual(409, result.status_code)
        self.assertEqual("ANALYSIS_INPUT_CHANGED", json.loads(result.body)["error"]["code"])
        self.audit.assert_not_awaited()

    async def test_shared_endpoint_serializes_analysis_response_without_report_fields(self):
        app = FastAPI()
        app.include_router(router.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/interview-projects/{f.PROJECT}/reruns", headers={"Idempotency-Key": "route-key"}, json=payload())
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(f.NEXT, response.json()["analysis_run_id"])
        self.assertTrue(response.json()["is_current_version"])
        self.assertNotIn("report_version_id", response.json())

    async def test_disabled_feature_does_not_reach_key_or_service(self):
        with patch.object(router, "INTERVIEW_V2_ENABLED", False):
            result = await self.invoke()
        self.assertEqual(503, result.status_code)
        self.key.assert_not_awaited()
        self.service.assert_not_awaited()
