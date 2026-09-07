from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app.routers import interview_v2 as router
from app.schemas.interview_v2_rerun import RERUN_REQUEST_ADAPTER
from app.services.interview_v2_import_service import InterviewV2ImportError


PROJECT = "project_" + "1" * 32
PARTICIPANT = "participant_" + "2" * 32
BASE = "dossier_" + "3" * 32
NEXT = "dossier_" + "4" * 32
LOGIN = {"email": "owner@example.com"}


def payload():
    return {
        "from_stage": "participant_dossier",
        "participant_id": PARTICIPANT,
        "base_dossier_version_id": BASE,
        "preserve_manual_report_edits": True,
        "reuse_unchanged_artifacts": True,
        "force": False,
    }


class DossierRerunRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(router, "INTERVIEW_V2_ENABLED", True))
        self.stack.enter_context(patch.object(router, "_require_feature", new=AsyncMock(return_value=LOGIN)))
        self.read = self.stack.enter_context(patch.object(router, "_read_structure_json", new=AsyncMock(return_value=payload())))
        self.access = self.stack.enter_context(patch.object(router, "validate_dossier_rerun_access"))
        self.key = self.stack.enter_context(patch.object(router, "require_request_llm_api_key", new=AsyncMock(return_value="mock-key")))
        self.service = self.stack.enter_context(patch.object(router, "create_participant_dossier_rerun", new=AsyncMock(return_value={
            "project_id": PROJECT, "import_id": "import_" + "5" * 32,
            "participant_id": PARTICIPANT, "status": "generated",
            "dossier_version_id": NEXT, "dossier_version_number": 2,
            "is_current_version": True, "rerun": {"reused": False},
        })))
        self.audit = self.stack.enter_context(patch.object(router, "audit_log", new=AsyncMock()))
        self.wrapper = self.stack.enter_context(patch.object(router, "run_with_llm_api_key", new=AsyncMock(side_effect=self.wrap)))

    async def wrap(self, awaitable, _api_key, **kwargs):
        self.assertEqual("V2 单玩家档案重跑", kwargs["action"])
        self.assertEqual(PARTICIPANT, kwargs["reference_id"])
        return await awaitable

    async def invoke(self, key="dossier-key"):
        return await router.rerun_interview_v2_report_section(
            PROJECT, SimpleNamespace(headers={"Idempotency-Key": key})
        )

    def test_strict_single_participant_contract(self):
        parsed = RERUN_REQUEST_ADAPTER.validate_python(payload())
        self.assertEqual(PARTICIPANT, parsed.participant_id)
        for change in (
            {"participant_id": [PARTICIPANT]}, {"participant_id": "../other"},
            {"base_dossier_version_id": None}, {"instruction": "rewrite"},
            {"force": True}, {"force": 0}, {"reuse_unchanged_artifacts": 1},
            {"preserve_manual_report_edits": False},
        ):
            with self.subTest(change=change), self.assertRaises(ValidationError):
                RERUN_REQUEST_ADAPTER.validate_python({**payload(), **change})

    async def test_access_precedes_api_key_and_service(self):
        order = []
        self.access.side_effect = lambda *_args: order.append("access")

        async def key(_request):
            order.append("key")
            return "mock-key"

        async def service(*_args):
            order.append("service")
            return {
                "project_id": PROJECT, "import_id": "import_" + "5" * 32,
                "participant_id": PARTICIPANT, "status": "generated",
                "dossier_version_id": NEXT, "dossier_version_number": 2,
                "is_current_version": True, "rerun": {"reused": False},
            }

        self.key.side_effect = key
        self.service.side_effect = service
        result = await self.invoke()
        self.assertEqual(["access", "key", "service"], order)
        self.assertEqual(NEXT, result["dossier_version_id"])
        self.audit.assert_awaited_once()

    async def test_denied_access_never_reads_api_key(self):
        self.access.side_effect = InterviewV2ImportError(
            status_code=404, code="INTERVIEW_DOSSIER_NOT_FOUND", message="未找到"
        )
        result = await self.invoke()
        self.assertEqual(404, result.status_code)
        self.assertEqual("INTERVIEW_DOSSIER_NOT_FOUND", json.loads(result.body)["error"]["code"])
        self.key.assert_not_awaited()
        self.service.assert_not_awaited()

    async def test_invalid_contract_and_key_stop_before_api_key(self):
        self.read.return_value = {**payload(), "force": True}
        result = await self.invoke()
        self.assertEqual(400, result.status_code)
        self.assertIn("DOSSIER_RERUN_REQUEST_INVALID", result.body.decode())
        self.read.return_value = payload()
        result = await self.invoke("bad key")
        self.assertEqual(400, result.status_code)
        self.key.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
