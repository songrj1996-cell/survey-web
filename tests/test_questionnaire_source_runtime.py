from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
import httpx

from app.routers.questionnaire_source_runtime import (
    create_questionnaire_source_runtime_router,
)
from app.schemas.questionnaire_source_runtime import QuestionnaireSourceCapabilities
from app.services.questionnaire_source_runtime import (
    QuestionnaireSourceRuntime,
    create_questionnaire_source_runtime,
)


OWNER = "email:runtime-owner@example.test"
LOGIN = {"email": "runtime-owner@example.test", "name": "Runtime Owner"}

EXPECTED_ROUTES = {
    ("GET", "/api/questionnaire-sources/capabilities"),
    ("GET", "/api/questionnaire-sources/google-forms/families"),
    ("POST", "/api/questionnaire-sources/google-forms/families"),
    ("GET", "/api/questionnaire-sources/google-forms/families/{family_id}"),
    ("POST", "/api/questionnaire-sources/google-forms/families/{family_id}/refresh"),
    (
        "POST",
        "/api/questionnaire-sources/google-forms/families/{family_id}"
        "/analysis-sessions",
    ),
}


class _Client:
    async def fetch_form(self, owner_ref: str, form_id: str):
        raise AssertionError("not used")

    async def fetch_responses(self, owner_ref: str, form_id: str):
        raise AssertionError("not used")


def _routes(router) -> set[tuple[str, str]]:
    result = set()
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or set():
            if method not in {"HEAD", "OPTIONS"}:
                result.add((method, route.path))
    return result


class QuestionnaireSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="google-runtime-")
        self.runtime = create_questionnaire_source_runtime(
            Path(self.temporary.name) / "research-assets",
            google_forms_client=_Client(),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_runtime_shares_one_isolated_storage_root(self):
        self.assertIsInstance(self.runtime, QuestionnaireSourceRuntime)
        self.assertEqual(
            self.runtime.family_storage.root,
            self.runtime.storage.root,
        )
        self.assertIs(
            self.runtime.google_forms_api.storage,
            self.runtime.storage,
        )
        self.assertIs(
            self.runtime.google_forms_family_api.snapshot_storage,
            self.runtime.storage,
        )
        self.assertEqual(
            self.runtime.capabilities,
            QuestionnaireSourceCapabilities(
                google_forms_connection=True,
                google_forms_unified_analysis=True,
            ),
        )

    async def test_router_publishes_only_capability_and_five_family_routes(self):
        router = create_questionnaire_source_runtime_router(self.runtime)
        self.assertEqual(_routes(router), EXPECTED_ROUTES)
        serialized = "\n".join(path for _, path in _routes(router))
        for forbidden in (
            "/snapshots",
            "/bested/",
            "/materials/",
            "/asset-review",
            "/workflow/",
        ):
            self.assertNotIn(forbidden, serialized)

    async def test_capabilities_response_is_authenticated_and_minimal(self):
        app = FastAPI()
        app.include_router(create_questionnaire_source_runtime_router(self.runtime))
        transport = httpx.ASGITransport(app=app)
        with (
            patch(
                "app.routers.questionnaire_source_runtime._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.questionnaire_source_runtime._owner_key",
                return_value=OWNER,
            ),
            patch(
                "app.routers.questionnaire_source_runtime.get_app_settings",
                return_value={"google_forms_entry_enabled": True},
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/questionnaire-sources/capabilities"
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {
            "schema_version": 1,
            "google_forms_connection": True,
            "google_forms_unified_analysis": True,
        })

    async def test_disabled_entry_hides_capabilities_and_blocks_family_routes(self):
        app = FastAPI()
        app.include_router(create_questionnaire_source_runtime_router(self.runtime))
        transport = httpx.ASGITransport(app=app)
        with (
            patch(
                "app.routers.questionnaire_source_runtime._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.questionnaire_source_runtime._owner_key",
                return_value=OWNER,
            ),
            patch(
                "app.routers.questionnaire_source_runtime.get_app_settings",
                return_value={"google_forms_entry_enabled": False},
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                capabilities = await client.get(
                    "/api/questionnaire-sources/capabilities"
                )
                families = await client.get(
                    "/api/questionnaire-sources/google-forms/families"
                )
        self.assertEqual(capabilities.status_code, 200, capabilities.text)
        self.assertEqual(capabilities.json(), {
            "schema_version": 1,
            "google_forms_connection": False,
            "google_forms_unified_analysis": False,
        })
        self.assertEqual(families.status_code, 404, families.text)
        self.assertEqual(families.json()["detail"]["code"], "google_forms_entry_disabled")

    async def test_enabled_entry_allows_family_routes(self):
        app = FastAPI()
        app.include_router(create_questionnaire_source_runtime_router(self.runtime))
        transport = httpx.ASGITransport(app=app)
        with (
            patch(
                "app.routers.questionnaire_source_runtime.get_app_settings",
                return_value={"google_forms_entry_enabled": True},
            ),
            patch(
                "app.routers.google_forms_families._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                response = await client.get(
                    "/api/questionnaire-sources/google-forms/families"
                )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"], [])

    async def test_capability_booleans_are_strict(self):
        with self.assertRaises(ValueError):
            QuestionnaireSourceCapabilities(
                google_forms_connection=1,
                google_forms_unified_analysis=True,
            )
        with self.assertRaises(ValueError):
            QuestionnaireSourceCapabilities(
                google_forms_connection=True,
                google_forms_unified_analysis="true",
            )

    async def test_runtime_requires_a_client(self):
        with self.assertRaisesRegex(ValueError, "只读客户端"):
            create_questionnaire_source_runtime(
                Path(self.temporary.name) / "missing-client",
                google_forms_client=None,
            )


if __name__ == "__main__":
    unittest.main()
