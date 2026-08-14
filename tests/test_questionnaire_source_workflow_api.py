from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException, Request, Response
import httpx

from app.core.research_assets import content_sha256
from app.routers import questionnaire_source_workflows as workflow_router_module
from app.routers.questionnaire_source_workflows import (
    create_questionnaire_source_workflows_router,
)
from app.schemas.questionnaire import (
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireMergeCandidate,
    QuestionnaireSourceFailureReason,
    QuestionnaireSourceNextAction,
    QuestionnaireSourceWorkflowStatus,
    questionnaire_source_priority,
)
from app.schemas.research_assets import (
    ImportErrorCode,
    ImportIssue,
    MediaType,
    ProcessingStatus,
    ResearchAssetCollection,
    SourceLocator,
)
from app.services.questionnaire_source_materialization import (
    QuestionnaireMaterializedCandidate,
    QuestionnaireSourceMaterializedStep,
)
from app.services import questionnaire_source_materialization as materialization_module
from app.services.questionnaire_source_workflow import (
    QuestionnaireSourceAcquisitionError,
)
from app.services.questionnaire_source_workflow_api import (
    HmacQuestionnaireSourceSelectionTokenCodec,
    QuestionnaireSourceWorkflowApi,
    QuestionnaireSourceWorkflowConflictError,
    QuestionnaireSourceWorkflowInternalError,
    QuestionnaireSourceWorkflowInvalidError,
    QuestionnaireSourceWorkflowNotFoundError,
    QuestionnaireSourceWorkflowPlan,
)
from app.storage.research_assets import (
    SnapshotConflictError,
    SnapshotPackage,
    SnapshotPackageError,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"
LOGIN = {"email": "workflow-user@example.com", "name": "Workflow User"}
OWNER_REF = "email:workflow-user@example.com"
OTHER_LOGIN = {"email": "other-workflow-user@example.com"}
OTHER_OWNER_REF = "email:other-workflow-user@example.com"
WORKFLOW_REF = "workflow-safe-resolved"
TOKEN_SECRET = b"questionnaire-workflow-api-test-key" * 2
PRIVATE_MARKER = "PRIVATE_WORKFLOW_SHOULD_NOT_LEAK"

RESPONSE_FIELDS = {
    "schema_version",
    "status",
    "route",
    "snapshot",
    "attempts",
    "next_actions",
    "selection_options",
    "selection_token",
    "conflicts",
    "failure_reason",
    "response_only_confirmed",
    "selected_source_ids",
    "partial_success",
}
SNAPSHOT_SUMMARY_FIELDS = {
    "schema_version",
    "snapshot_id",
    "provider",
    "source_mode",
    "collection_state",
    "mapping_status",
    "item_count",
    "question_count",
    "asset_count",
    "image_asset_count",
    "asset_reference_count",
}
ATTEMPT_SUMMARY_FIELDS = {
    "acquisition_route",
    "source_id",
    "source_mode",
    "priority",
    "status",
    "snapshot_id",
    "failure_reason",
    "warning_codes",
    "issue_codes",
    "retryable",
}
SELECTION_OPTION_FIELDS = {
    "source_id",
    "source_mode",
    "priority",
    "status",
    "snapshot_id",
}
CONFLICT_SUMMARY_FIELDS = {
    "conflict_id",
    "field_path",
    "candidate_source_ids",
    "suggested_source_id",
    "blocking",
}


class _MemoryStorage:
    """Thread-safe owner-scoped immutable storage used only by these tests."""

    def __init__(self) -> None:
        self.packages: dict[tuple[str, str], SnapshotPackage] = {}
        self.save_calls: list[tuple[str, SnapshotPackage]] = []
        self.load_calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def load_snapshot_package(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> SnapshotPackage | None:
        with self._lock:
            self.load_calls.append((owner_ref, snapshot_id))
            return self.packages.get((owner_ref, snapshot_id))

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        if package.bundle.collection.owner_ref != owner_ref:
            raise SnapshotPackageError("private owner mismatch")
        key = (owner_ref, package.bundle.snapshot.snapshot_id)
        with self._lock:
            existing = self.packages.get(key)
            if existing is not None and existing != package:
                raise SnapshotConflictError("private immutable conflict")
            self.save_calls.append((owner_ref, package))
            self.packages[key] = package

    def reset(self) -> None:
        with self._lock:
            self.packages.clear()
            self.save_calls.clear()
            self.load_calls.clear()


class _PlanProvider:
    def __init__(self) -> None:
        self.plans: dict[
            tuple[str, str], QuestionnaireSourceWorkflowPlan
        ] = {}
        self.calls: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(
        self,
        owner_ref: str,
        workflow_ref: str,
    ) -> QuestionnaireSourceWorkflowPlan | None:
        with self._lock:
            self.calls.append((owner_ref, workflow_ref))
            return self.plans.get((owner_ref, workflow_ref))

    def add(self, plan: QuestionnaireSourceWorkflowPlan) -> None:
        self.plans[(plan.owner_ref, plan.workflow_ref)] = plan


class _ChunkedReceive:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls = 0

    async def __call__(self) -> dict:
        index = self.calls
        self.calls += 1
        if index >= len(self.chunks):
            return {"type": "http.disconnect"}
        return {
            "type": "http.request",
            "body": self.chunks[index],
            "more_body": index < len(self.chunks) - 1,
        }


class _DisconnectingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        return {"type": "http.disconnect"}


class _StallingReceive:
    def __init__(self, first_chunk: bytes) -> None:
        self.first_chunk = first_chunk
        self.calls = 0
        self.first_sent = asyncio.Event()
        self.never = asyncio.Event()

    async def __call__(self) -> dict:
        self.calls += 1
        if self.calls == 1:
            self.first_sent.set()
            return {
                "type": "http.request",
                "body": self.first_chunk,
                "more_body": True,
            }
        await self.never.wait()
        raise AssertionError("unreachable")


def _materialized(
    name: str,
    *,
    owner_ref: str = OWNER_REF,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
    raw_marker: str | None = None,
    title: str | None = None,
) -> QuestionnaireMaterializedCandidate:
    payload = json.loads(
        (FIXTURE_DIR / name).read_text(encoding="utf-8")
    )
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    if raw_marker is not None:
        raw_definition = dict(snapshot.provider_raw_definition)
        raw_definition["test_private_marker"] = raw_marker
        snapshot = snapshot.model_copy(update={
            "provider_raw_definition": raw_definition,
        })
    if title is not None:
        snapshot = snapshot.model_copy(update={"title": title})

    collection = ResearchAssetCollection.model_validate(
        payload["collection"]
    )
    sources = []
    for index, source in enumerate(collection.sources):
        update = {"owner_ref": owner_ref}
        if index == 0 and raw_marker is not None:
            update["original_name"] = raw_marker
        sources.append(source.model_copy(update=update))

    media: dict[str, bytes] = {}
    assets = []
    for asset in collection.assets:
        if asset.media_type == MediaType.IMAGE:
            content = f"workflow-api:{name}:{asset.asset_id}".encode()
            content_hash = content_sha256(content)
            media[content_hash] = content
            asset = asset.model_copy(update={
                "content_hash": content_hash,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = collection.model_copy(update={
        "owner_ref": owner_ref,
        "sources": sources,
        "assets": assets,
    })
    primary_source_id = collection.sources[0].source_id
    candidate = QuestionnaireMergeCandidate(
        source_id=primary_source_id,
        source_mode=snapshot.source_mode,
        priority=questionnaire_source_priority(snapshot.source_mode),
        snapshot=snapshot,
        collection=collection,
        status=status,
    )
    return QuestionnaireMaterializedCandidate(candidate, media)


def _failed_materialized(
    *,
    source_id: str,
    source_mode: QuestionnaireSourceMode,
    error_code: ImportErrorCode,
    message: str = "safe failure",
    retryable: bool = False,
    source_locator: SourceLocator | None = None,
) -> QuestionnaireMaterializedCandidate:
    return QuestionnaireMaterializedCandidate(
        QuestionnaireMergeCandidate(
            source_id=source_id,
            source_mode=source_mode,
            priority=questionnaire_source_priority(source_mode),
            status=ProcessingStatus.FAILED,
            issues=[ImportIssue(
                code=error_code,
                message=message,
                retryable=retryable,
                source_locator=source_locator,
            )],
        ),
        {},
    )


def _processing_materialized(
    *,
    source_id: str,
) -> QuestionnaireMaterializedCandidate:
    return QuestionnaireMaterializedCandidate(
        QuestionnaireMergeCandidate(
            source_id=source_id,
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=questionnaire_source_priority(
                QuestionnaireSourceMode.OFFICIAL_API
            ),
            status=ProcessingStatus.PROCESSING,
        ),
        {},
    )


def _step(
    materialized: QuestionnaireMaterializedCandidate,
    *,
    route: QuestionnaireAcquisitionRoute,
    owner_ref: str = OWNER_REF,
    load=None,
) -> QuestionnaireSourceMaterializedStep:
    return QuestionnaireSourceMaterializedStep(
        route=route,
        source_id=materialized.candidate.source_id,
        source_mode=materialized.candidate.source_mode,
        owner_ref=owner_ref,
        load=load or (lambda: materialized),
    )


def _plan(
    workflow_ref: str,
    steps: list[QuestionnaireSourceMaterializedStep] | tuple[
        QuestionnaireSourceMaterializedStep, ...
    ],
    *,
    owner_ref: str = OWNER_REF,
    actions: tuple[QuestionnaireSourceNextAction, ...] = (),
) -> QuestionnaireSourceWorkflowPlan:
    return QuestionnaireSourceWorkflowPlan(
        workflow_ref=workflow_ref,
        owner_ref=owner_ref,
        steps=tuple(steps),
        available_actions=actions,
    )


def _request_object(
    receive,
    *,
    content_type: bytes = b"application/json",
) -> Request:
    return Request({
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"content-type", content_type)],
        "client": ("test", 1),
        "server": ("test", 80),
    }, receive)


async def _call_asgi(
    app: FastAPI,
    path: str,
    receive,
    *,
    content_type: bytes = b"application/json",
) -> tuple[int, dict, dict[bytes, bytes]]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [(b"content-type", content_type)],
            "client": ("test", 1),
            "server": ("test", 80),
        },
        receive,
        send,
    )
    start = next(
        message for message in sent
        if message["type"] == "http.response.start"
    )
    content = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    headers = dict(start.get("headers", []))
    return start["status"], json.loads(content), headers


class QuestionnaireSourceWorkflowApiTests(
    unittest.IsolatedAsyncioTestCase,
):
    async def asyncSetUp(self) -> None:
        self.storage = _MemoryStorage()
        self.provider = _PlanProvider()
        self.codec = HmacQuestionnaireSourceSelectionTokenCodec(
            TOKEN_SECRET
        )
        self.api = QuestionnaireSourceWorkflowApi(
            self.storage,
            self.provider,
            self.codec,
        )
        self.router = create_questionnaire_source_workflows_router(
            self.api
        )
        self.app = FastAPI()
        self.app.include_router(self.router)

    async def _request(
        self,
        workflow_ref: str,
        *,
        login: dict | None = LOGIN,
        **kwargs,
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            with patch(
                "app.routers.questionnaire_source_workflows._require_feature",
                new=AsyncMock(return_value=login),
            ) as require_feature:
                response = await client.post(
                    "/api/questionnaire-sources/workflows/"
                    f"{workflow_ref}/resolve",
                    **kwargs,
                )
        require_feature.assert_awaited_once()
        self.assertEqual(require_feature.await_args.args[1], "survey")
        return response

    def _endpoint(self):
        return next(route.endpoint for route in self.router.routes)

    async def _retry_endpoint_until_available(
        self,
        endpoint,
        workflow_ref: str,
    ):
        for _ in range(150):
            receive = _ChunkedReceive([b"{}"])
            request = _request_object(receive)
            with patch(
                "app.routers.questionnaire_source_workflows._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ):
                try:
                    return await endpoint(
                        workflow_ref,
                        request,
                        Response(),
                    )
                except HTTPException as error:
                    if error.status_code != 429:
                        raise
            await asyncio.sleep(0.01)
        self.fail("workflow admission gate was not released")

    def _add_resolved_plan(
        self,
        workflow_ref: str = WORKFLOW_REF,
        *,
        owner_ref: str = OWNER_REF,
        raw_marker: str | None = None,
    ) -> QuestionnaireMaterializedCandidate:
        materialized = _materialized(
            "google_forms.json",
            owner_ref=owner_ref,
            raw_marker=raw_marker,
        )
        self.provider.add(_plan(
            workflow_ref,
            [_step(
                materialized,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                owner_ref=owner_ref,
            )],
            owner_ref=owner_ref,
        ))
        return materialized

    async def test_server_plan_resolves_and_response_is_safe_allowlist(self):
        materialized = self._add_resolved_plan(
            raw_marker=PRIVATE_MARKER,
        )

        response = await self._request(WORKFLOW_REF, json={})

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(set(payload), RESPONSE_FIELDS)
        self.assertEqual(payload["status"], "resolved")
        self.assertEqual(set(payload["snapshot"]), SNAPSHOT_SUMMARY_FIELDS)
        self.assertEqual(len(payload["attempts"]), 1)
        self.assertEqual(set(payload["attempts"][0]), ATTEMPT_SUMMARY_FIELDS)
        self.assertEqual(
            payload["selected_source_ids"],
            [materialized.candidate.source_id],
        )
        self.assertEqual(
            response.headers["cache-control"],
            "private, no-store",
        )
        self.assertEqual(
            self.provider.calls,
            [(OWNER_REF, WORKFLOW_REF)],
        )
        self.assertEqual(len(self.storage.save_calls), 1)
        saved_owner, saved = self.storage.save_calls[0]
        self.assertEqual(saved_owner, OWNER_REF)
        self.assertEqual(saved.media, dict(materialized.media))

        serialized = response.text.casefold()
        for forbidden in (
            PRIVATE_MARKER,
            OWNER_REF,
            "owner_ref",
            "provider_raw_definition",
            "original_name",
            "original_url",
            "source_locator",
            "content_hash",
            "filename",
            "media",
        ):
            self.assertNotIn(forbidden.casefold(), serialized)

    async def test_request_contract_is_strict_bounded_and_server_owned(self):
        self._add_resolved_plan()
        cases = (
            ({"owner_ref": OWNER_REF}, 422),
            ({"steps": []}, 422),
            ({"available_actions": ["continue_response_only"]}, 422),
            ({"route": "saved_snapshot"}, 422),
            ({"source_mode": "official_api"}, 422),
            ({"priority": 1}, 422),
            ({"selected_source_id": "source-only"}, 422),
            ({"selection_token": "token-only"}, 422),
            ({
                "selected_source_id": "source",
                "selection_token": "token",
                "response_only": True,
            }, 422),
            ({"response_only": 1}, 422),
            ({
                "selected_source_id": "source",
                "selection_token": "x" * 513,
            }, 422),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                response = await self._request(
                    WORKFLOW_REF,
                    json=payload,
                )
                self.assertEqual(response.status_code, expected)

        duplicate = await self._request(
            WORKFLOW_REF,
            content=(
                b'{"response_only":false,"response_only":true}'
            ),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(duplicate.status_code, 422)
        malformed = await self._request(
            WORKFLOW_REF,
            content=b"{",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(malformed.status_code, 422)
        empty = await self._request(
            WORKFLOW_REF,
            content=b"",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(empty.status_code, 422)
        wrong_type = await self._request(
            WORKFLOW_REF,
            content=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(wrong_type.status_code, 415)
        oversized = await self._request(
            WORKFLOW_REF,
            content=b"{" + b" " * 5000 + b"}",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(oversized.status_code, 413)

        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.storage.save_calls, [])

    async def test_authentication_and_empty_owner_precede_body_and_plan(self):
        self._add_resolved_plan()
        endpoint = self._endpoint()
        path_body = b"{}"

        for login, expected in (
            (None, 401),
            ({}, 401),
        ):
            with self.subTest(login=login):
                receive = _ChunkedReceive([path_body])
                request = _request_object(receive)
                with patch(
                    "app.routers.questionnaire_source_workflows._require_feature",
                    new=AsyncMock(return_value=login),
                ):
                    with self.assertRaises(HTTPException) as caught:
                        await endpoint(
                            WORKFLOW_REF,
                            request,
                            Response(),
                        )
                self.assertEqual(caught.exception.status_code, expected)
                self.assertEqual(receive.calls, 0)

        receive = _ChunkedReceive([path_body])
        request = _request_object(receive)
        with patch(
            "app.routers.questionnaire_source_workflows._require_feature",
            new=AsyncMock(side_effect=HTTPException(
                status_code=403,
                detail="denied",
            )),
        ):
            with self.assertRaises(HTTPException) as caught:
                await endpoint(WORKFLOW_REF, request, Response())
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(receive.calls, 0)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.storage.save_calls, [])

    async def test_all_eight_domain_states_have_stable_safe_http_shapes(self):
        resolved = _materialized("google_forms.json")
        self.provider.add(_plan(
            "state-resolved",
            [_step(
                resolved,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            )],
        ))

        missing_saved = _failed_materialized(
            source_id=resolved.candidate.source_id,
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            error_code=ImportErrorCode.NOT_FOUND,
        )
        self.provider.add(_plan(
            "state-resolved-partial",
            [
                _step(
                    missing_saved,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                ),
                _step(
                    resolved,
                    route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                ),
            ],
        ))

        processing = _processing_materialized(source_id="processing-source")
        self.provider.add(_plan(
            "state-in-progress",
            [_step(
                processing,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            )],
        ))

        login_failed = _failed_materialized(
            source_id="authorization-required",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            error_code=ImportErrorCode.LOGIN_REQUIRED,
            message=PRIVATE_MARKER,
            source_locator=SourceLocator(
                test_private_marker=PRIVATE_MARKER,
            ),
        )
        self.provider.add(_plan(
            "state-awaiting-action",
            [_step(
                login_failed,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
            )],
            actions=(QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,),
        ))

        closed_placeholder = _failed_materialized(
            source_id="closed-published-page",
            source_mode=QuestionnaireSourceMode.PUBLISHED_PAGE,
            error_code=ImportErrorCode.PROVIDER_ERROR,
        )

        def load_closed_page():
            raise QuestionnaireSourceAcquisitionError(
                ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message=PRIVATE_MARKER,
                    retryable=False,
                ),
                reason=(
                    QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
                ),
            )

        self.provider.add(_plan(
            "state-closed-public-page",
            [_step(
                closed_placeholder,
                route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                load=load_closed_page,
            )],
            actions=(
                QuestionnaireSourceNextAction.RETRY_PUBLISHED_PAGE,
                QuestionnaireSourceNextAction.TEMPORARILY_REOPEN_AND_RETRY,
            ),
        ))

        first = _materialized("google_forms.json")
        second = _materialized(
            "bested.json",
            title=PRIVATE_MARKER,
        )
        self.provider.add(_plan(
            "state-selection-required",
            [
                _step(
                    first,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                ),
                _step(
                    second,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                ),
            ],
            actions=(QuestionnaireSourceNextAction.SELECT_SOURCE,),
        ))

        self.provider.add(_plan(
            "state-skipped",
            [],
            actions=(
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ),
        ))
        self.provider.add(_plan(
            "state-failed",
            [_step(
                login_failed,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
            )],
        ))

        cases = (
            ("state-resolved", "resolved", {}, 1),
            ("state-resolved-partial", "resolved_partial", {}, 1),
            ("state-in-progress", "in_progress", {}, 0),
            ("state-awaiting-action", "awaiting_action", {}, 0),
            ("state-closed-public-page", "closed_public_page", {}, 0),
            ("state-selection-required", "selection_required", {}, 0),
            ("state-skipped", "skipped", {"response_only": True}, 0),
            ("state-failed", "failed", {}, 0),
        )
        payloads: dict[str, dict] = {}
        for workflow_ref, expected_status, request_body, expected_writes in cases:
            with self.subTest(status=expected_status):
                self.storage.reset()
                response = await self._request(
                    workflow_ref,
                    json=request_body,
                )
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                payloads[expected_status] = payload
                self.assertEqual(set(payload), RESPONSE_FIELDS)
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(
                    len(self.storage.save_calls),
                    expected_writes,
                )
                if expected_status in {"resolved", "resolved_partial"}:
                    self.assertEqual(
                        set(payload["snapshot"]),
                        SNAPSHOT_SUMMARY_FIELDS,
                    )
                else:
                    self.assertIsNone(payload["snapshot"])

        awaiting = payloads["awaiting_action"]
        self.assertEqual(
            awaiting["attempts"][0]["issue_codes"],
            ["login_required"],
        )
        self.assertEqual(
            set(awaiting["attempts"][0]),
            ATTEMPT_SUMMARY_FIELDS,
        )
        self.assertNotIn(PRIVATE_MARKER, json.dumps(awaiting))

        selection = payloads["selection_required"]
        self.assertEqual(len(selection["selection_options"]), 2)
        self.assertTrue(all(
            set(option) == SELECTION_OPTION_FIELDS
            for option in selection["selection_options"]
        ))
        self.assertIsInstance(selection["selection_token"], str)
        self.assertTrue(selection["selection_token"].startswith("v1."))
        self.assertTrue(selection["conflicts"])
        self.assertTrue(all(
            set(conflict) == CONFLICT_SUMMARY_FIELDS
            for conflict in selection["conflicts"]
        ))
        self.assertNotIn(PRIVATE_MARKER, json.dumps(selection))

        skipped = payloads["skipped"]
        self.assertTrue(skipped["response_only_confirmed"])
        self.assertEqual(skipped["attempts"], [])
        self.assertEqual(skipped["selected_source_ids"], [])

    async def test_response_only_is_enabled_only_by_the_server_plan(self):
        materialized = _materialized("google_forms.json")
        loader_called = False

        def must_not_load():
            nonlocal loader_called
            loader_called = True
            raise AssertionError("response-only must remain lazy")

        self.provider.add(_plan(
            "response-only-allowed",
            [_step(
                materialized,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                load=must_not_load,
            )],
            actions=(
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ),
        ))
        self.provider.add(_plan(
            "response-only-denied",
            [_step(
                materialized,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                load=must_not_load,
            )],
        ))

        allowed = await self._request(
            "response-only-allowed",
            json={"response_only": True},
        )
        denied = await self._request(
            "response-only-denied",
            json={"response_only": True},
        )

        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["status"], "skipped")
        self.assertEqual(denied.status_code, 409)
        self.assertFalse(loader_called)
        self.assertEqual(self.storage.save_calls, [])

    async def test_selection_token_is_bound_to_owner_and_workflow_ref(self):
        first = _materialized("google_forms.json")
        second = _materialized("bested.json")

        def add_selection_plan(
            workflow_ref: str,
            *,
            owner_ref: str,
        ) -> None:
            owner_first = (
                first
                if owner_ref == OWNER_REF
                else _materialized(
                    "google_forms.json",
                    owner_ref=owner_ref,
                )
            )
            owner_second = (
                second
                if owner_ref == OWNER_REF
                else _materialized(
                    "bested.json",
                    owner_ref=owner_ref,
                )
            )
            self.provider.add(_plan(
                workflow_ref,
                [
                    _step(
                        owner_first,
                        route=(
                            QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT
                        ),
                        owner_ref=owner_ref,
                    ),
                    _step(
                        owner_second,
                        route=(
                            QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT
                        ),
                        owner_ref=owner_ref,
                    ),
                ],
                owner_ref=owner_ref,
                actions=(QuestionnaireSourceNextAction.SELECT_SOURCE,),
            ))

        add_selection_plan("selection-owner-a", owner_ref=OWNER_REF)
        add_selection_plan("selection-other-ref", owner_ref=OWNER_REF)
        add_selection_plan(
            "selection-owner-a",
            owner_ref=OTHER_OWNER_REF,
        )

        waiting = await self._request(
            "selection-owner-a",
            json={},
        )
        self.assertEqual(waiting.status_code, 200)
        waiting_payload = waiting.json()
        self.assertEqual(waiting_payload["status"], "selection_required")
        token = waiting_payload["selection_token"]
        selected_source_id = second.candidate.source_id
        decision = {
            "selected_source_id": selected_source_id,
            "selection_token": token,
        }

        wrong_workflow = await self._request(
            "selection-other-ref",
            json=decision,
        )
        wrong_owner = await self._request(
            "selection-owner-a",
            login=OTHER_LOGIN,
            json=decision,
        )
        tampered_token = token[:-1] + ("A" if token[-1] != "A" else "B")
        tampered = await self._request(
            "selection-owner-a",
            json={
                "selected_source_id": selected_source_id,
                "selection_token": tampered_token,
            },
        )

        self.assertEqual(wrong_workflow.status_code, 409)
        self.assertEqual(wrong_owner.status_code, 409)
        self.assertEqual(tampered.status_code, 409)
        self.assertEqual(self.storage.save_calls, [])

        selected = await self._request(
            "selection-owner-a",
            json=decision,
        )

        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["status"], "resolved_partial")
        self.assertEqual(
            selected.json()["selected_source_ids"],
            [selected_source_id],
        )
        self.assertEqual(len(self.storage.save_calls), 1)
        saved_owner, saved_package = self.storage.save_calls[0]
        self.assertEqual(saved_owner, OWNER_REF)
        self.assertEqual(saved_package.media, dict(second.media))
        self.assertTrue(
            set(saved_package.media).isdisjoint(set(first.media))
        )

    async def test_selection_rejects_changed_candidates_without_writing(self):
        first = _materialized("google_forms.json")
        second = _materialized("bested.json")
        current_second = second

        def load_second():
            return current_second

        self.provider.add(_plan(
            "selection-stale-candidate",
            [
                _step(
                    first,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                ),
                _step(
                    second,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    load=load_second,
                ),
            ],
            actions=(QuestionnaireSourceNextAction.SELECT_SOURCE,),
        ))
        waiting = await self._request(
            "selection-stale-candidate",
            json={},
        )
        self.assertEqual(waiting.status_code, 200)

        current_second = _materialized(
            "bested.json",
            title="candidate changed after user confirmation",
        )
        selected = await self._request(
            "selection-stale-candidate",
            json={
                "selected_source_id": second.candidate.source_id,
                "selection_token": waiting.json()["selection_token"],
            },
        )

        self.assertEqual(selected.status_code, 409)
        self.assertEqual(
            selected.json(),
            {"detail": "问卷来源状态已变化，请重新确认"},
        )
        self.assertEqual(self.storage.save_calls, [])

    async def test_repeated_resolve_is_idempotent_and_stable(self):
        self._add_resolved_plan()

        first = await self._request(WORKFLOW_REF, json={})
        second = await self._request(WORKFLOW_REF, json={})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), first.json())
        self.assertEqual(len(self.storage.packages), 1)
        self.assertEqual(len(self.storage.save_calls), 2)
        self.assertEqual(
            self.storage.save_calls[0],
            self.storage.save_calls[1],
        )

    async def test_missing_and_cross_owner_plans_are_indistinguishable(self):
        foreign = self._add_resolved_plan(
            "foreign-plan",
            owner_ref=OTHER_OWNER_REF,
        )
        mismatched = _plan(
            "mismatched-plan",
            [_step(
                foreign,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                owner_ref=OTHER_OWNER_REF,
            )],
            owner_ref=OTHER_OWNER_REF,
        )
        self.provider.plans[(OWNER_REF, "mismatched-plan")] = mismatched

        missing = await self._request("missing-plan", json={})
        foreign_response = await self._request("foreign-plan", json={})
        mismatched_response = await self._request(
            "mismatched-plan",
            json={},
        )

        for response in (missing, foreign_response, mismatched_response):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                response.json(),
                {"detail": "问卷来源计划不存在"},
            )
            self.assertNotIn(OTHER_OWNER_REF, response.text)
        self.assertEqual(self.storage.save_calls, [])

    async def test_api_errors_have_stable_redacted_http_mappings(self):
        self._add_resolved_plan()
        cases = (
            (
                QuestionnaireSourceWorkflowInvalidError(),
                422,
                "问卷来源工作流请求无效",
            ),
            (
                QuestionnaireSourceWorkflowNotFoundError(),
                404,
                "问卷来源计划不存在",
            ),
            (
                QuestionnaireSourceWorkflowConflictError(),
                409,
                "问卷来源状态已变化，请重新确认",
            ),
            (
                QuestionnaireSourceWorkflowInternalError(),
                500,
                "问卷来源工作流暂时不可用",
            ),
            (
                RuntimeError(PRIVATE_MARKER),
                500,
                "问卷来源工作流暂时不可用",
            ),
        )
        for error, expected_status, expected_detail in cases:
            with self.subTest(error=type(error).__name__):
                async def fail_run(api, owner_ref, workflow_ref, request):
                    raise error

                with patch.object(
                    QuestionnaireSourceWorkflowApi,
                    "run",
                    new=fail_run,
                ):
                    response = await self._request(
                        WORKFLOW_REF,
                        json={},
                    )
                self.assertEqual(response.status_code, expected_status)
                self.assertEqual(
                    response.json(),
                    {"detail": expected_detail},
                )
                self.assertNotIn(PRIVATE_MARKER, response.text)

    async def test_storage_failure_is_redacted_and_prevents_success(self):
        self._add_resolved_plan(raw_marker=PRIVATE_MARKER)

        with patch.object(
            self.storage,
            "save_snapshot_package",
            side_effect=RuntimeError(PRIVATE_MARKER),
        ):
            response = await self._request(WORKFLOW_REF, json={})

        self.assertEqual(response.status_code, 500)
        self.assertEqual(
            response.json(),
            {"detail": "问卷来源工作流暂时不可用"},
        )
        self.assertNotIn(PRIVATE_MARKER, response.text)
        self.assertEqual(self.storage.packages, {})

    async def test_http_success_waits_for_validation_and_atomic_persistence(self):
        self._add_resolved_plan()
        started = threading.Event()
        release = threading.Event()
        original = materialization_module._validate_and_save

        def blocking_validate_and_save(*args, **kwargs):
            started.set()
            release.wait(2)
            return original(*args, **kwargs)

        with patch.object(
            materialization_module,
            "_validate_and_save",
            side_effect=blocking_validate_and_save,
        ):
            request_task = asyncio.create_task(
                self._request(WORKFLOW_REF, json={})
            )
            try:
                self.assertTrue(
                    await asyncio.to_thread(started.wait, 1),
                    "persistence worker did not start",
                )
                await asyncio.sleep(0.02)
                self.assertFalse(request_task.done())
                self.assertEqual(self.storage.save_calls, [])
            finally:
                release.set()
            response = await request_task

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "resolved")
        self.assertEqual(len(self.storage.save_calls), 1)

    async def test_body_limits_disconnect_and_timeout_release_admission(self):
        self._add_resolved_plan()
        path = (
            "/api/questionnaire-sources/workflows/"
            f"{WORKFLOW_REF}/resolve"
        )
        with patch(
            "app.routers.questionnaire_source_workflows._require_feature",
            new=AsyncMock(return_value=LOGIN),
        ):
            oversized_receive = _ChunkedReceive([
                b"{" + b" " * 3000,
                b" " * 3000,
                b"}",
            ])
            oversized_status, oversized, _ = await _call_asgi(
                self.app,
                path,
                oversized_receive,
            )
            self.assertEqual(oversized_status, 413)
            self.assertEqual(
                oversized,
                {"detail": "问卷来源工作流请求超过大小限制"},
            )
            self.assertEqual(oversized_receive.calls, 2)

            disconnected_status, disconnected, _ = await _call_asgi(
                self.app,
                path,
                _DisconnectingReceive(b"{"),
            )
            self.assertEqual(disconnected_status, 400)
            self.assertEqual(
                disconnected,
                {"detail": "问卷来源工作流请求未完整发送"},
            )

            stalling = _StallingReceive(b"{")
            with patch.object(
                workflow_router_module,
                "_WORKFLOW_REQUEST_TIMEOUT_SECONDS",
                0.03,
            ):
                timeout_status, timed_out, _ = await _call_asgi(
                    self.app,
                    path,
                    stalling,
                )
            self.assertEqual(timeout_status, 408)
            self.assertEqual(
                timed_out,
                {"detail": "问卷来源工作流请求发送超时，请重试"},
            )

        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.storage.save_calls, [])
        retry = await self._request(WORKFLOW_REF, json={})
        self.assertEqual(retry.status_code, 200)

    async def test_busy_request_is_rejected_before_consuming_body(self):
        self._add_resolved_plan()
        endpoint = self._endpoint()
        started = asyncio.Event()
        release = asyncio.Event()
        original_run = QuestionnaireSourceWorkflowApi.run

        async def slow_run(api, owner_ref, workflow_ref, request):
            started.set()
            await release.wait()
            return await original_run(
                api,
                owner_ref,
                workflow_ref,
                request,
            )

        first_receive = _ChunkedReceive([b"{}"])
        first_request = _request_object(first_receive)
        second_receive = _ChunkedReceive([b"{}"])
        second_request = _request_object(second_receive)
        with (
            patch.object(
                QuestionnaireSourceWorkflowApi,
                "run",
                new=slow_run,
            ),
            patch(
                "app.routers.questionnaire_source_workflows._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first_task = asyncio.create_task(endpoint(
                WORKFLOW_REF,
                first_request,
                Response(),
            ))
            await asyncio.wait_for(started.wait(), timeout=1)
            try:
                with self.assertRaises(HTTPException) as busy:
                    await endpoint(
                        WORKFLOW_REF,
                        second_request,
                        Response(),
                    )
                self.assertEqual(busy.exception.status_code, 429)
                self.assertEqual(second_receive.calls, 0)
            finally:
                release.set()
            result = await first_task

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED,
        )
        self.assertEqual(len(self.storage.save_calls), 1)

    async def test_processing_timeout_holds_gate_until_save_thread_finishes(self):
        self._add_resolved_plan()
        endpoint = self._endpoint()
        started = threading.Event()
        release = threading.Event()
        original = materialization_module._validate_and_save
        call_count = 0

        def block_first_validate_and_save(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                started.set()
                release.wait(2)
            return original(*args, **kwargs)

        with (
            patch.object(
                materialization_module,
                "_validate_and_save",
                side_effect=block_first_validate_and_save,
            ),
            patch.object(
                workflow_router_module,
                "_WORKFLOW_PROCESSING_TIMEOUT_SECONDS",
                0.03,
            ),
        ):
            first = await self._request(WORKFLOW_REF, json={})
            self.assertTrue(started.is_set())
            self.assertEqual(first.status_code, 504)
            self.assertEqual(
                first.json(),
                {"detail": "问卷来源工作流处理超时，请稍后重试"},
            )

            second_receive = _ChunkedReceive([b"{}"])
            with patch(
                "app.routers.questionnaire_source_workflows._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ):
                with self.assertRaises(HTTPException) as busy:
                    await endpoint(
                        WORKFLOW_REF,
                        _request_object(second_receive),
                        Response(),
                    )
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)
            self.assertEqual(self.storage.save_calls, [])

            release.set()
            result = await self._retry_endpoint_until_available(
                endpoint,
                WORKFLOW_REF,
            )

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED,
        )
        self.assertGreaterEqual(len(self.storage.save_calls), 2)
        self.assertEqual(len(self.storage.packages), 1)

    async def test_cancelled_request_holds_gate_until_save_thread_finishes(self):
        self._add_resolved_plan()
        endpoint = self._endpoint()
        started = threading.Event()
        release = threading.Event()
        original = materialization_module._validate_and_save
        call_count = 0

        def block_first_validate_and_save(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                started.set()
                release.wait(2)
            return original(*args, **kwargs)

        with (
            patch.object(
                materialization_module,
                "_validate_and_save",
                side_effect=block_first_validate_and_save,
            ),
            patch(
                "app.routers.questionnaire_source_workflows._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
        ):
            first_task = asyncio.create_task(endpoint(
                WORKFLOW_REF,
                _request_object(_ChunkedReceive([b"{}"])),
                Response(),
            ))
            self.assertTrue(
                await asyncio.to_thread(started.wait, 1),
                "persistence worker did not start",
            )
            first_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first_task

            second_receive = _ChunkedReceive([b"{}"])
            with self.assertRaises(HTTPException) as busy:
                await endpoint(
                    WORKFLOW_REF,
                    _request_object(second_receive),
                    Response(),
                )
            self.assertEqual(busy.exception.status_code, 429)
            self.assertEqual(second_receive.calls, 0)
            self.assertEqual(self.storage.save_calls, [])

            release.set()
            result = await self._retry_endpoint_until_available(
                endpoint,
                WORKFLOW_REF,
            )

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED,
        )
        self.assertGreaterEqual(len(self.storage.save_calls), 2)
        self.assertEqual(len(self.storage.packages), 1)


if __name__ == "__main__":
    unittest.main()
