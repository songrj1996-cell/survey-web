from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
import httpx
from pydantic import ValidationError

from app.routers import (
    questionnaire_asset_reviews as asset_review_router_module,
    questionnaire_pdf_materials as pdf_router_module,
    questionnaire_source_runtime as runtime_router_module,
    questionnaire_sources as sources_router_module,
)
from app.routers.questionnaire_source_runtime import (
    create_questionnaire_source_runtime_router,
)
from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.questionnaire_source_runtime import (
    QuestionnaireSourceCapabilities,
)
from app.schemas.research_assets import MediaType, ResearchAssetCollection
from app.services.questionnaire_snapshot_api import (
    QuestionnaireSnapshotApi,
    QuestionnaireSnapshotInternalError,
    QuestionnaireSnapshotNotFoundError,
)
from app.services.questionnaire_snapshot_analysis_api import (
    QuestionnaireSnapshotAnalysisApi,
)
from app.services.questionnaire_asset_review_api import (
    QuestionnaireAssetReviewApi,
)
from app.services.questionnaire_source_runtime import (
    QuestionnaireSourceRuntime,
    create_questionnaire_source_runtime,
)
from app.storage.questionnaire_asset_reviews import (
    FileQuestionnaireAssetReviewStorage,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetBundle,
    SnapshotPackage,
    build_snapshot_package,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"
LOGIN = {"email": "runtime-owner@example.com", "name": "Runtime Owner"}
OWNER_REF = "email:runtime-owner@example.com"
OTHER_OWNER_REF = "email:other-runtime-owner@example.com"

EXPECTED_CAPABILITIES = {
    "schema_version": 1,
    "snapshot_package_upload": True,
    "snapshot_catalog": True,
    "snapshot_analysis_session": True,
    "asset_review_projection": True,
    "asset_review_decisions": True,
    "bested_original_questionnaire_upload": True,
    "screenshot_material_upload": True,
    "pdf_material_upload": True,
    "google_forms_connection": False,
    "source_workflow": False,
}

EXPECTED_ROUTES = {
    ("GET", "/api/questionnaire-sources/capabilities"),
    ("GET", "/api/questionnaire-sources/snapshots"),
    ("POST", "/api/questionnaire-sources/snapshots"),
    (
        "POST",
        "/api/questionnaire-sources/snapshots/{snapshot_id}/analysis-sessions",
    ),
    (
        "GET",
        "/api/questionnaire-sources/snapshots/{snapshot_id}/asset-review",
    ),
    (
        "POST",
        "/api/questionnaire-sources/snapshots/{snapshot_id}"
        "/asset-review/decisions",
    ),
    (
        "GET",
        "/api/questionnaire-sources/snapshots/{snapshot_id}"
        "/asset-review/thumbnails/{asset_token}.png",
    ),
    ("GET", "/api/questionnaire-sources/snapshots/{snapshot_id}"),
    (
        "GET",
        "/api/questionnaire-sources/snapshots/{snapshot_id}/download",
    ),
    ("POST", "/api/questionnaire-sources/bested/snapshots"),
    ("POST", "/api/questionnaire-sources/materials/snapshots"),
    ("POST", "/api/questionnaire-sources/materials/pdf/snapshots"),
}
EXPECTED_ROUTES_WITH_GOOGLE = {
    *EXPECTED_ROUTES,
    ("POST", "/api/questionnaire-sources/google-forms/snapshots"),
}


class _InvalidPathLike:
    def __fspath__(self):
        return object()


class _FakeGoogleFormsCaptureClient:
    async def fetch_form(self, owner_ref: str, form_id: str):
        raise AssertionError("runtime wiring test must not call Google")


def _snapshot_archive(owner_ref: str) -> tuple[bytes, str]:
    payload = json.loads(
        (FIXTURE_DIR / "google_forms.json").read_text(encoding="utf-8")
    )
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    media: dict[str, bytes] = {}
    assets = []
    for index, asset in enumerate(collection.assets):
        if asset.media_type == MediaType.IMAGE:
            content = f"runtime-snapshot-media-{index}".encode("utf-8")
            content_hash = hashlib.sha256(content).hexdigest()
            media[content_hash] = content
            asset = asset.model_copy(update={
                "content_hash": content_hash,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = collection.model_copy(update={
        "owner_ref": owner_ref,
        "sources": [
            source.model_copy(update={"owner_ref": owner_ref})
            for source in collection.sources
        ],
        "assets": assets,
    })
    bundle = ResearchAssetBundle(snapshot, collection)
    package = SnapshotPackage(bundle, media)
    return (
        build_snapshot_package(owner_ref, package.bundle, package.media),
        snapshot.snapshot_id,
    )


class QuestionnaireSourceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-source-runtime-test-",
        )
        self.addCleanup(self.temporary.cleanup)
        self.storage_root = Path(self.temporary.name) / "runtime-storage"
        self.runtime = create_questionnaire_source_runtime(self.storage_root)
        self.router = create_questionnaire_source_runtime_router(self.runtime)
        self.app = FastAPI()
        self.app.include_router(self.router)

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.request(method, path, **kwargs)

    def test_capability_contract_is_exact_literal_locked_and_frozen(self):
        capabilities = QuestionnaireSourceCapabilities()
        self.assertEqual(
            capabilities.model_dump(mode="json"),
            EXPECTED_CAPABILITIES,
        )

        invalid_values = {
            "schema_version": 2,
            "snapshot_package_upload": False,
            "snapshot_catalog": False,
            "snapshot_analysis_session": False,
            "asset_review_projection": False,
            "asset_review_decisions": False,
            "bested_original_questionnaire_upload": False,
            "screenshot_material_upload": False,
            "pdf_material_upload": False,
            "source_workflow": True,
        }
        for field, invalid_value in invalid_values.items():
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    QuestionnaireSourceCapabilities.model_validate({
                        **EXPECTED_CAPABILITIES,
                        field: invalid_value,
                    })

        connected = QuestionnaireSourceCapabilities(
            google_forms_connection=True,
        )
        self.assertTrue(connected.google_forms_connection)
        for invalid_value in (1, 0, "true", None):
            with self.subTest(google_forms_connection=invalid_value):
                with self.assertRaises(ValidationError):
                    QuestionnaireSourceCapabilities.model_validate({
                        **EXPECTED_CAPABILITIES,
                        "google_forms_connection": invalid_value,
                    })

        with self.assertRaises(ValidationError):
            QuestionnaireSourceCapabilities.model_validate({
                **EXPECTED_CAPABILITIES,
                "asset_review_decisions": 1,
            })

        with self.assertRaises(ValidationError):
            QuestionnaireSourceCapabilities.model_validate({
                **EXPECTED_CAPABILITIES,
                "storage_root": "/private/runtime/path",
            })
        with self.assertRaises(ValidationError):
            capabilities.snapshot_package_upload = False

    def test_factory_requires_explicit_root_shares_storage_and_has_no_side_effect(self):
        self.assertFalse(self.storage_root.exists())
        self.assertIsInstance(self.runtime.storage, FileResearchAssetStorage)
        self.assertEqual(
            self.runtime.storage.root,
            self.storage_root.absolute(),
        )
        self.assertIsInstance(
            self.runtime.review_storage,
            FileQuestionnaireAssetReviewStorage,
        )
        self.assertEqual(
            self.runtime.review_storage.root,
            self.runtime.storage.root,
        )
        self.assertIs(
            self.runtime.asset_review_api.review_storage,
            self.runtime.review_storage,
        )
        for api in (
            self.runtime.snapshot_api,
            self.runtime.snapshot_analysis_api,
            self.runtime.asset_review_api,
            self.runtime.bested_api,
            self.runtime.screenshot_material_api,
            self.runtime.pdf_material_api,
        ):
            self.assertIs(api.storage, self.runtime.storage)
        self.assertEqual(
            self.runtime.capabilities.model_dump(mode="json"),
            EXPECTED_CAPABILITIES,
        )
        self.assertFalse(self.storage_root.exists())

        with self.assertRaises(TypeError):
            create_questionnaire_source_runtime()

    def test_invalid_roots_and_runtime_components_fail_closed(self):
        invalid_roots = (
            (None, TypeError),
            ("", ValueError),
            (" \t", ValueError),
            (b"/invalid/bytes-root", ValueError),
            (_InvalidPathLike(), TypeError),
        )
        for root, error_type in invalid_roots:
            with self.subTest(root_type=type(root).__name__):
                with self.assertRaises(error_type):
                    create_questionnaire_source_runtime(root)

        other_storage = FileResearchAssetStorage(
            Path(self.temporary.name) / "other-storage"
        )
        other_review_storage = FileQuestionnaireAssetReviewStorage(
            Path(self.temporary.name) / "other-storage"
        )
        with self.assertRaisesRegex(ValueError, "共享 runtime.storage"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=self.runtime.review_storage,
                snapshot_api=QuestionnaireSnapshotApi(other_storage),
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(TypeError, "snapshot_api"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=self.runtime.review_storage,
                snapshot_api=object(),
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(ValueError, "共享 runtime.storage"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=self.runtime.review_storage,
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=QuestionnaireSnapshotAnalysisApi(
                    other_storage
                ),
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(TypeError, "snapshot_analysis_api"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=self.runtime.review_storage,
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=object(),
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(ValueError, "共享 runtime.storage"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=self.runtime.review_storage,
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=QuestionnaireAssetReviewApi(
                    other_storage,
                    other_review_storage,
                ),
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(TypeError, "asset_review_api"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=self.runtime.review_storage,
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=object(),
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(
            ValueError,
            "review_storage 必须共享 runtime.storage 根目录",
        ):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=other_review_storage,
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaisesRegex(TypeError, "review_storage"):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=object(),
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        same_root_other_review_storage = FileQuestionnaireAssetReviewStorage(
            self.runtime.storage.root,
        )
        with self.assertRaisesRegex(
            ValueError,
            "共享 runtime.review_storage",
        ):
            QuestionnaireSourceRuntime(
                storage=self.runtime.storage,
                review_storage=same_root_other_review_storage,
                snapshot_api=self.runtime.snapshot_api,
                snapshot_analysis_api=self.runtime.snapshot_analysis_api,
                asset_review_api=self.runtime.asset_review_api,
                bested_api=self.runtime.bested_api,
                screenshot_material_api=self.runtime.screenshot_material_api,
                pdf_material_api=self.runtime.pdf_material_api,
            )
        with self.assertRaises(TypeError):
            create_questionnaire_source_runtime_router(object())

    async def test_existing_file_root_fails_closed_without_overwriting_file(self):
        invalid_root = Path(self.temporary.name) / "not-a-directory"
        sentinel = b"runtime-root-sentinel"
        invalid_root.write_bytes(sentinel)
        runtime = create_questionnaire_source_runtime(invalid_root)

        with self.assertRaises(QuestionnaireSnapshotInternalError):
            await runtime.snapshot_api.get_snapshot(OWNER_REF, "missing")

        self.assertEqual(invalid_root.read_bytes(), sentinel)

    def test_aggregate_router_exposes_only_the_supported_runtime_routes(self):
        actual_routes = {
            (method, route.path)
            for route in self.router.routes
            for method in route.methods
        }
        self.assertEqual(actual_routes, EXPECTED_ROUTES)
        self.assertNotIn(
            ("POST", "/api/questionnaire-sources/google-forms/snapshots"),
            actual_routes,
        )

    def test_google_forms_route_is_registered_only_when_client_is_injected(self):
        connected_runtime = create_questionnaire_source_runtime(
            Path(self.temporary.name) / "connected-storage",
            google_forms_client=_FakeGoogleFormsCaptureClient(),
        )
        connected_router = create_questionnaire_source_runtime_router(
            connected_runtime,
        )
        actual_routes = {
            (method, route.path)
            for route in connected_router.routes
            for method in route.methods
        }

        self.assertEqual(actual_routes, EXPECTED_ROUTES_WITH_GOOGLE)
        self.assertIsNotNone(connected_runtime.google_forms_api)
        self.assertTrue(
            connected_runtime.capabilities.google_forms_connection
        )
        self.assertIs(
            connected_runtime.google_forms_api.storage,
            connected_runtime.storage,
        )
        self.assertNotIn(
            ("POST", "/api/questionnaire-sources/workflow/resolve"),
            actual_routes,
        )

    async def test_capabilities_endpoint_authenticates_and_returns_only_safe_fields(
        self,
    ):
        authorize = AsyncMock(return_value=LOGIN)
        with patch.object(
            runtime_router_module,
            "_require_feature",
            new=authorize,
        ):
            response = await self._request(
                "GET",
                "/api/questionnaire-sources/capabilities",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), EXPECTED_CAPABILITIES)
        self.assertNotIn(str(self.storage_root), response.text)
        authorize.assert_awaited_once()
        self.assertEqual(authorize.await_args.args[1], "survey")

        deny = AsyncMock(side_effect=HTTPException(
            status_code=403,
            detail="runtime capability denied",
        ))
        with patch.object(
            runtime_router_module,
            "_require_feature",
            new=deny,
        ):
            denied = await self._request(
                "GET",
                "/api/questionnaire-sources/capabilities",
            )
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json(), {"detail": "runtime capability denied"})

        empty_login = AsyncMock(return_value=None)
        with patch.object(
            runtime_router_module,
            "_require_feature",
            new=empty_login,
        ):
            unauthenticated = await self._request(
                "GET",
                "/api/questionnaire-sources/capabilities",
            )
        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(unauthenticated.json(), {"detail": "请先登录飞书"})

    async def test_aggregated_existing_routes_keep_their_factory_authentication(self):
        deny_sources = AsyncMock(side_effect=HTTPException(
            status_code=403,
            detail="source route denied",
        ))
        source_requests = (
            ("GET", "/api/questionnaire-sources/snapshots"),
            ("POST", "/api/questionnaire-sources/snapshots"),
            ("GET", "/api/questionnaire-sources/snapshots/missing"),
            (
                "GET",
                "/api/questionnaire-sources/snapshots/missing/download",
            ),
            ("POST", "/api/questionnaire-sources/bested/snapshots"),
            ("POST", "/api/questionnaire-sources/materials/snapshots"),
        )
        with patch.object(
            sources_router_module,
            "_require_feature",
            new=deny_sources,
        ):
            source_responses = [
                await self._request(method, path)
                for method, path in source_requests
            ]
        self.assertTrue(all(
            response.status_code == 403
            and response.json() == {"detail": "source route denied"}
            for response in source_responses
        ))
        self.assertEqual(deny_sources.await_count, len(source_requests))
        self.assertTrue(all(
            call.args[1] == "survey"
            for call in deny_sources.await_args_list
        ))

        deny_pdf = AsyncMock(side_effect=HTTPException(
            status_code=403,
            detail="pdf route denied",
        ))
        with patch.object(
            pdf_router_module,
            "_require_feature",
            new=deny_pdf,
        ):
            pdf_response = await self._request(
                "POST",
                "/api/questionnaire-sources/materials/pdf/snapshots",
            )
        self.assertEqual(pdf_response.status_code, 403)
        self.assertEqual(pdf_response.json(), {"detail": "pdf route denied"})
        deny_pdf.assert_awaited_once()
        self.assertEqual(deny_pdf.await_args.args[1], "survey")

        deny_review = AsyncMock(side_effect=HTTPException(
            status_code=403,
            detail="asset review denied",
        ))
        review_requests = (
            (
                "/api/questionnaire-sources/snapshots/missing/asset-review"
            ),
            (
                "/api/questionnaire-sources/snapshots/missing"
                "/asset-review/thumbnails/"
                f"{'0' * 64}.png"
            ),
        )
        with patch.object(
            asset_review_router_module,
            "_require_feature",
            new=deny_review,
        ):
            review_responses = [
                await self._request("GET", path)
                for path in review_requests
            ]
        self.assertTrue(all(
            response.status_code == 403
            and response.json() == {"detail": "asset review denied"}
            for response in review_responses
        ))
        self.assertEqual(deny_review.await_count, len(review_requests))
        self.assertTrue(all(
            call.args[1] == "survey"
            for call in deny_review.await_args_list
        ))

    async def test_snapshot_storage_is_owner_isolated_through_runtime_api(self):
        archive, snapshot_id = _snapshot_archive(OWNER_REF)
        imported = await self.runtime.snapshot_api.import_snapshot(
            OWNER_REF,
            archive,
        )
        self.assertEqual(imported.snapshot_id, snapshot_id)
        self.assertIsNotNone(self.runtime.storage.load_snapshot_package(
            OWNER_REF,
            snapshot_id,
        ))
        self.assertIsNone(self.runtime.storage.load_snapshot_package(
            OTHER_OWNER_REF,
            snapshot_id,
        ))

        with self.assertRaises(QuestionnaireSnapshotNotFoundError):
            await self.runtime.snapshot_api.get_snapshot(
                OTHER_OWNER_REF,
                snapshot_id,
            )
        loaded = await self.runtime.snapshot_api.get_snapshot(
            OWNER_REF,
            snapshot_id,
        )
        self.assertEqual(loaded, imported)

        owner_catalog = await self.runtime.snapshot_api.list_snapshots(
            OWNER_REF,
        )
        other_catalog = await self.runtime.snapshot_api.list_snapshots(
            OTHER_OWNER_REF,
        )
        self.assertEqual(owner_catalog.items, [imported])
        self.assertIsNone(owner_catalog.next_cursor)
        self.assertEqual(other_catalog.items, [])
        self.assertIsNone(other_catalog.next_cursor)


if __name__ == "__main__":
    unittest.main()
