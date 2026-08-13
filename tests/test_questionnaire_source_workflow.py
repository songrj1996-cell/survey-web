from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from pathlib import Path
import threading
import unittest

from pydantic import ValidationError

from app.schemas.questionnaire import (
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireMergeCandidate,
    QuestionnaireSourceAttempt,
    QuestionnaireSourceFailureReason,
    QuestionnaireSourceNextAction,
    QuestionnaireSourceResult,
    QuestionnaireSourceWorkflowResult,
    QuestionnaireSourceWorkflowStatus,
    questionnaire_source_priority,
)
from app.schemas.research_assets import (
    ImportErrorCode,
    ImportIssue,
    ProcessingStatus,
    ResearchAssetCollection,
)
from app.services.questionnaire_source_service import (
    QuestionnaireSourceScopeError,
)
from app.services.questionnaire_source_workflow import (
    QuestionnaireSourceAcquisitionError,
    QuestionnaireSourceStep,
    run_questionnaire_source_workflow,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"


def _bundle(name: str):
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return (
        QuestionnaireSnapshot.model_validate(payload["snapshot"]),
        ResearchAssetCollection.model_validate(payload["collection"]),
    )


def _candidate(
    name: str,
    *,
    source_id: str,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
) -> QuestionnaireMergeCandidate:
    snapshot, collection = _bundle(name)
    return QuestionnaireMergeCandidate(
        source_id=source_id,
        source_mode=snapshot.source_mode,
        priority=(
            2
            if snapshot.source_mode == QuestionnaireSourceMode.OFFICIAL_API
            else 3
        ),
        snapshot=snapshot,
        collection=collection,
        status=status,
    )


def _failed(
    *,
    source_id: str,
    source_mode: QuestionnaireSourceMode,
    issue: ImportIssue,
) -> QuestionnaireMergeCandidate:
    return QuestionnaireMergeCandidate(
        source_id=source_id,
        source_mode=source_mode,
        priority=questionnaire_source_priority(source_mode),
        status=ProcessingStatus.FAILED,
        issues=[issue],
    )


def _second_official_candidate() -> QuestionnaireMergeCandidate:
    first = _candidate(
        "google_forms.json",
        source_id="src_google_demo",
    )
    second_source_id = "second-official"
    collection = first.collection.model_copy(update={
        "sources": [
            source.model_copy(update={"source_id": second_source_id})
            if source.source_id == first.source_id
            else source
            for source in first.collection.sources
        ],
        "documents": [
            document.model_copy(update={
                "source_id": second_source_id,
                "source_locator": (
                    document.source_locator.model_copy(update={
                        "source_id": second_source_id,
                    })
                    if document.source_locator is not None
                    and document.source_id == first.source_id
                    else document.source_locator
                ),
            })
            if document.source_id == first.source_id
            else document
            for document in first.collection.documents
        ],
    })
    return first.model_copy(update={
        "source_id": second_source_id,
        "collection": collection,
    })


def _step(
    *,
    route: QuestionnaireAcquisitionRoute,
    source_id: str,
    source_mode: QuestionnaireSourceMode,
    load,
    owner_ref: str = "fixture-user",
) -> QuestionnaireSourceStep:
    return QuestionnaireSourceStep(
        route=route,
        source_id=source_id,
        source_mode=source_mode,
        owner_ref=owner_ref,
        load=load,
    )


class QuestionnaireSourceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_closed_snapshot_wins_without_touching_later_steps(self):
        calls: list[str] = []
        saved = _candidate(
            "google_forms_closed.json",
            source_id="src_google_demo",
        )

        def load_saved():
            calls.append("saved")
            return saved

        def must_not_run():
            calls.append("later")
            raise AssertionError("later source must remain lazy")

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                    source_id="published",
                    source_mode=QuestionnaireSourceMode.PUBLISHED_PAGE,
                    load=must_not_run,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id="src_google_demo",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=load_saved,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id="authorized",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=must_not_run,
                ),
            ],
            available_actions=[
                QuestionnaireSourceNextAction.TEMPORARILY_REOPEN_AND_RETRY,
            ],
        )

        self.assertEqual(calls, ["saved"])
        self.assertEqual(result.status, QuestionnaireSourceWorkflowStatus.RESOLVED)
        self.assertEqual(result.route, QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT)
        self.assertEqual(
            result.result.snapshot.source_mode,
            QuestionnaireSourceMode.OFFICIAL_API,
        )
        self.assertEqual(result.result.snapshot.collection_state.value, "closed")
        self.assertEqual(result.next_actions, [])

    async def test_sync_failure_falls_back_to_async_upload_and_is_partial(self):
        calls: list[str] = []
        upload = _candidate(
            "bested.json",
            source_id="src_bested_demo",
        )

        def authorized_failure():
            calls.append("authorized")
            raise QuestionnaireSourceAcquisitionError(ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="需要重新授权 Google Forms",
                retryable=False,
                suggested_action="重新连接或上传问卷",
            ))

        async def uploaded_questionnaire():
            calls.append("upload")
            return upload

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id="authorized",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=authorized_failure,
                ),
                _step(
                    route=(
                        QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
                    ),
                    source_id="src_bested_demo",
                    source_mode=(
                        QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD
                    ),
                    load=uploaded_questionnaire,
                ),
            ],
        )

        self.assertEqual(calls, ["authorized", "upload"])
        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )
        self.assertEqual(
            result.route,
            QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD,
        )
        self.assertEqual(
            [attempt.acquisition_route for attempt in result.attempts],
            [
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD,
            ],
        )
        self.assertEqual(
            result.attempts[0].failure_reason,
            QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
        )

    async def test_same_provider_source_id_can_fallback_across_routes(self):
        source_id = "src_google_demo"
        authorized = _candidate(
            "google_forms.json",
            source_id=source_id,
        )

        def missing_saved_snapshot():
            raise QuestionnaireSourceAcquisitionError(ImportIssue(
                code=ImportErrorCode.NOT_FOUND,
                message="未找到已存快照",
                retryable=False,
            ))

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id=source_id,
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=missing_saved_snapshot,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=source_id,
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=lambda: authorized,
                ),
            ],
        )

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )
        self.assertEqual(
            result.route,
            QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
        )
        self.assertEqual(
            [attempt.source_id for attempt in result.attempts],
            [source_id, source_id],
        )
        self.assertEqual(
            [attempt.acquisition_route for attempt in result.attempts],
            [
                QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
            ],
        )
        self.assertEqual(result.result.selected_source_ids, [source_id])

    async def test_bundleless_partial_can_fallback_with_same_source_id(self):
        source_id = "src_google_demo"
        partial_without_bundle = QuestionnaireMergeCandidate(
            source_id=source_id,
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            status=ProcessingStatus.PARTIAL,
            warnings=[],
        )
        authorized = _candidate(
            "google_forms.json",
            source_id=source_id,
        )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id=source_id,
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=lambda: partial_without_bundle,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=source_id,
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=lambda: authorized,
                ),
            ],
        )

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )
        self.assertIsNone(result.attempts[0].snapshot_id)
        self.assertEqual(
            result.attempts[1].snapshot_id,
            authorized.snapshot.snapshot_id,
        )
        self.assertEqual(result.result.selected_source_ids, [source_id])

    async def test_inconsistent_safe_error_degrades_to_unknown_and_falls_back(self):
        upload = _candidate(
            "bested.json",
            source_id="src_bested_demo",
        )

        def inconsistent_error():
            raise QuestionnaireSourceAcquisitionError(
                ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="adapter supplied an inconsistent classification",
                    retryable=False,
                ),
                reason=QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id="authorized",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=inconsistent_error,
                ),
                _step(
                    route=(
                        QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
                    ),
                    source_id=upload.source_id,
                    source_mode=upload.source_mode,
                    load=lambda: upload,
                ),
            ],
        )

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )
        self.assertEqual(
            result.attempts[0].failure_reason,
            QuestionnaireSourceFailureReason.UNKNOWN,
        )
        self.assertEqual(
            result.attempts[0].issues[0].message,
            "问卷来源获取失败，未使用不完整结果",
        )

    async def test_sync_loader_does_not_block_the_event_loop(self):
        release = threading.Event()
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )

        def blocking_loader():
            release.wait(timeout=0.5)
            return candidate

        task = asyncio.create_task(run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[_step(
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                source_id=candidate.source_id,
                source_mode=candidate.source_mode,
                load=blocking_loader,
            )],
        ))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        release.set()
        result = await task
        self.assertEqual(result.status, QuestionnaireSourceWorkflowStatus.RESOLVED)

    async def test_higher_route_in_progress_blocks_lower_completed_source(self):
        calls: list[str] = []
        pending = QuestionnaireMergeCandidate(
            source_id="saved",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            status=ProcessingStatus.PROCESSING,
        )

        def lower():
            calls.append("lower")
            return _candidate(
                "bested.json",
                source_id="src_bested_demo",
            )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id="saved",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=lambda: pending,
                ),
                _step(
                    route=(
                        QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
                    ),
                    source_id="src_bested_demo",
                    source_mode=(
                        QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD
                    ),
                    load=lower,
                ),
            ],
        )

        self.assertEqual(result.status, QuestionnaireSourceWorkflowStatus.IN_PROGRESS)
        self.assertEqual(calls, [])
        self.assertEqual(len(result.attempts), 1)
        self.assertEqual(result.attempts[0].status, ProcessingStatus.PROCESSING)

    async def test_response_only_requires_capability_and_short_circuits(self):
        calls: list[str] = []
        step = _step(
            route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
            source_id="authorized",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            load=lambda: calls.append("called"),
        )

        with self.assertRaisesRegex(ValueError, "未允许 response-only"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=[step],
                response_only=True,
            )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[step],
            available_actions=[
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ],
            response_only=True,
        )

        self.assertEqual(calls, [])
        self.assertEqual(result.status, QuestionnaireSourceWorkflowStatus.SKIPPED)
        self.assertTrue(result.response_only_confirmed)
        self.assertIsNone(result.result)
        self.assertEqual(result.attempts, [])

    async def test_login_and_permission_actions_are_allowlisted(self):
        for error_code, reason in (
            (
                ImportErrorCode.LOGIN_REQUIRED,
                QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            ),
            (
                ImportErrorCode.PERMISSION_REQUIRED,
                QuestionnaireSourceFailureReason.PERMISSION_REQUIRED,
            ),
        ):
            with self.subTest(error_code=error_code):
                candidate = _failed(
                    source_id="authorized",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    issue=ImportIssue(
                        code=error_code,
                        message="需要用户处理授权",
                        retryable=False,
                    ),
                )
                result = await run_questionnaire_source_workflow(
                    owner_ref="fixture-user",
                    steps=[_step(
                        route=(
                            QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                        ),
                        source_id="authorized",
                        source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                        load=lambda candidate=candidate: candidate,
                    )],
                    available_actions=[
                        QuestionnaireSourceNextAction.RETRY_SOURCE,
                        QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
                        QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
                        QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
                    ],
                )

                self.assertEqual(
                    result.status,
                    QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                )
                self.assertEqual(result.failure_reason, reason)
                self.assertNotIn(
                    QuestionnaireSourceNextAction.RETRY_SOURCE,
                    result.next_actions,
                )
                self.assertIn(
                    QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
                    result.next_actions,
                )

    async def test_retry_action_requires_a_retryable_issue(self):
        retryable = _failed(
            source_id="authorized",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            issue=ImportIssue(
                code=ImportErrorCode.PROVIDER_ERROR,
                message="外部服务暂时不可用",
                retryable=True,
            ),
        )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[_step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id="authorized",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                load=lambda: retryable,
            )],
            available_actions=[
                QuestionnaireSourceNextAction.RETRY_SOURCE,
                QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
            ],
        )

        self.assertEqual(
            result.failure_reason,
            QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER,
        )
        self.assertEqual(
            result.next_actions,
            [
                QuestionnaireSourceNextAction.RETRY_SOURCE,
                QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
            ],
        )

    async def test_same_route_failure_actions_cover_every_failed_attempt(self):
        login = _failed(
            source_id="login-source",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            issue=ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="需要重新授权",
                retryable=False,
            ),
        )
        retryable = _failed(
            source_id="retryable-source",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            issue=ImportIssue(
                code=ImportErrorCode.PROVIDER_ERROR,
                message="Google Forms 暂时不可用",
                retryable=True,
            ),
        )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=login.source_id,
                    source_mode=login.source_mode,
                    load=lambda: login,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=retryable.source_id,
                    source_mode=retryable.source_mode,
                    load=lambda: retryable,
                ),
            ],
            available_actions=[
                QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
                QuestionnaireSourceNextAction.RETRY_SOURCE,
            ],
        )

        self.assertEqual(
            result.failure_reason,
            QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
        )
        self.assertEqual(
            result.next_actions,
            [
                QuestionnaireSourceNextAction.RETRY_SOURCE,
                QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
            ],
        )

    async def test_unknown_exception_is_redacted_and_not_marked_retryable(self):
        secret = "Bearer synthetic-secret https://private.invalid/form?id=secret"

        def explode():
            raise RuntimeError(secret)

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[_step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id="authorized",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                load=explode,
            )],
            available_actions=[
                QuestionnaireSourceNextAction.RETRY_SOURCE,
                QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
            ],
        )

        serialized = result.model_dump_json()
        self.assertNotIn(secret, serialized)
        self.assertNotIn("synthetic-secret", serialized)
        self.assertEqual(
            result.failure_reason,
            QuestionnaireSourceFailureReason.UNKNOWN,
        )
        self.assertEqual(
            result.next_actions,
            [QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT],
        )
        self.assertFalse(result.attempts[0].issues[0].retryable)

    async def test_closed_public_page_only_suggests_explicit_user_actions(self):
        calls = 0

        def closed_page():
            nonlocal calls
            calls += 1
            raise QuestionnaireSourceAcquisitionError(
                ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="发布页未展示问卷内容",
                    retryable=False,
                ),
                reason=QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE,
            )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[_step(
                route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                source_id="published",
                source_mode=QuestionnaireSourceMode.PUBLISHED_PAGE,
                load=closed_page,
            )],
            available_actions=[
                QuestionnaireSourceNextAction.RETRY_SOURCE,
                QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
                QuestionnaireSourceNextAction.RETRY_PUBLISHED_PAGE,
                QuestionnaireSourceNextAction.TEMPORARILY_REOPEN_AND_RETRY,
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ],
        )

        self.assertEqual(calls, 1)
        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.CLOSED_PUBLIC_PAGE,
        )
        self.assertNotIn(
            QuestionnaireSourceNextAction.RETRY_SOURCE,
            result.next_actions,
        )
        self.assertIn(
            QuestionnaireSourceNextAction.TEMPORARILY_REOPEN_AND_RETRY,
            result.next_actions,
        )

    async def test_same_route_sources_require_and_apply_explicit_selection(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        later_calls: list[str] = []
        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=first.source_id,
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                load=lambda: first,
            ),
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=second.source_id,
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                load=lambda: second,
            ),
            _step(
                route=(
                    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
                ),
                source_id="src_bested_demo",
                source_mode=(
                    QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD
                ),
                load=lambda: later_calls.append("later"),
            ),
        ]

        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )
        self.assertEqual(
            waiting.status,
            QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED,
        )
        self.assertEqual(
            set(waiting.selection_source_ids),
            {first.source_id, second.source_id},
        )
        self.assertEqual(later_calls, [])

        selected = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
            selected_source_id=second.source_id,
            selection_token=waiting.selection_token,
        )
        self.assertEqual(
            selected.result.selected_source_ids,
            [second.source_id],
        )
        skipped = next(
            attempt for attempt in selected.attempts
            if attempt.source_id == first.source_id
        )
        self.assertEqual(skipped.status, ProcessingStatus.SKIPPED)
        self.assertEqual(later_calls, [])

    async def test_bundleless_partial_is_not_a_selectable_source(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        bundleless = QuestionnaireMergeCandidate(
            source_id="bundleless",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            status=ProcessingStatus.PARTIAL,
        )

        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=bundleless.source_id,
                    source_mode=bundleless.source_mode,
                    load=lambda: bundleless,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=first.source_id,
                    source_mode=first.source_mode,
                    load=lambda: first,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=second.source_id,
                    source_mode=second.source_mode,
                    load=lambda: second,
                ),
            ],
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )

        self.assertEqual(
            waiting.status,
            QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED,
        )
        self.assertEqual(
            set(waiting.selection_source_ids),
            {first.source_id, second.source_id},
        )
        self.assertNotIn(bundleless.source_id, waiting.selection_source_ids)

    async def test_bundleless_partial_does_not_fake_a_user_selection(self):
        complete = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        bundleless = QuestionnaireMergeCandidate(
            source_id="bundleless",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            status=ProcessingStatus.PARTIAL,
        )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=bundleless.source_id,
                    source_mode=bundleless.source_mode,
                    load=lambda: bundleless,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    source_id=complete.source_id,
                    source_mode=complete.source_mode,
                    load=lambda: complete,
                ),
            ],
        )

        bundleless_attempt = next(
            attempt for attempt in result.attempts
            if attempt.source_id == bundleless.source_id
        )
        self.assertEqual(bundleless_attempt.status, ProcessingStatus.PARTIAL)
        self.assertFalse(any(
            warning.code == "source_not_selected"
            for warning in bundleless_attempt.warnings
        ))
        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )

    async def test_same_saved_route_ignores_provenance_priority_for_selection(self):
        google = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        bested = _candidate(
            "bested.json",
            source_id="src_bested_demo",
        )
        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                source_id=google.source_id,
                source_mode=google.source_mode,
                load=lambda: google,
            ),
            _step(
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                source_id=bested.source_id,
                source_mode=bested.source_mode,
                load=lambda: bested,
            ),
        ]

        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )
        self.assertEqual(
            waiting.status,
            QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED,
        )
        self.assertEqual(
            set(waiting.selection_source_ids),
            {google.source_id, bested.source_id},
        )

        selected = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
            selected_source_id=bested.source_id,
            selection_token=waiting.selection_token,
        )
        self.assertEqual(
            selected.result.selected_source_ids,
            [bested.source_id],
        )
        self.assertTrue(selected.result.conflicts)
        self.assertTrue(all(
            conflict.resolution.value == "user_selected"
            and conflict.selected_source_id == bested.source_id
            for conflict in selected.result.conflicts
        ))

    async def test_route_selection_conflicts_include_lower_provenance(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        second = second.model_copy(update={
            "snapshot": second.snapshot.model_copy(update={
                "title": "Google Forms alternate title",
            }),
        })
        bested = _candidate(
            "bested.json",
            source_id="src_bested_demo",
        )
        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                source_id=candidate.source_id,
                source_mode=candidate.source_mode,
                load=(lambda item=candidate: item),
            )
            for candidate in (first, second, bested)
        ]

        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )
        self.assertTrue(waiting.conflicts)
        self.assertTrue(all(
            bested.source_id
            in {candidate.source_id for candidate in conflict.candidates}
            for conflict in waiting.conflicts
        ))

        selected = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
            selected_source_id=bested.source_id,
            selection_token=waiting.selection_token,
        )
        self.assertEqual(
            selected.result.selected_source_ids,
            [bested.source_id],
        )
        self.assertTrue(all(
            conflict.selected_source_id == bested.source_id
            for conflict in selected.result.conflicts
        ))

    async def test_selection_audit_is_stable_when_steps_are_reordered(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        second = second.model_copy(update={
            "snapshot": second.snapshot.model_copy(update={
                "title": "Google Forms alternate title",
            }),
        })

        def steps(candidates):
            return [
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id=candidate.source_id,
                    source_mode=candidate.source_mode,
                    load=(lambda item=candidate: item),
                )
                for candidate in candidates
            ]

        normal = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps([first, second]),
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )
        reversed_result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps([second, first]),
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )

        self.assertEqual(normal.selection_token, reversed_result.selection_token)
        self.assertEqual(
            normal.selection_source_ids,
            reversed_result.selection_source_ids,
        )
        self.assertEqual(normal.conflicts, reversed_result.conflicts)

    async def test_selection_suggestion_prefers_higher_provenance(self):
        google = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        bested = _candidate(
            "bested.json",
            source_id="src_bested_demo",
        )

        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id=bested.source_id,
                    source_mode=bested.source_mode,
                    load=lambda: bested,
                ),
                _step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id=google.source_id,
                    source_mode=google.source_mode,
                    load=lambda: google,
                ),
            ],
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )

        self.assertTrue(waiting.conflicts)
        shared_conflicts = [
            conflict for conflict in waiting.conflicts
            if not conflict.field_path.endswith(".present")
            and all(
                candidate.value is not None
                and candidate.value != ""
                and candidate.value != []
                and candidate.value != {}
                for candidate in conflict.candidates
            )
        ]
        self.assertTrue(shared_conflicts)
        self.assertTrue(all(
            conflict.suggested_source_id == google.source_id
            for conflict in shared_conflicts
        ))

    async def test_selection_token_rejects_changed_candidates(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        second_available = True

        def load_second():
            if second_available:
                return second
            return QuestionnaireMergeCandidate(
                source_id=second.source_id,
                source_mode=second.source_mode,
                priority=second.priority,
                status=ProcessingStatus.FAILED,
                issues=[ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="来源状态已变化",
                    retryable=False,
                )],
            )

        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=first.source_id,
                source_mode=first.source_mode,
                load=lambda: first,
            ),
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=second.source_id,
                source_mode=second.source_mode,
                load=load_second,
            ),
        ]
        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )
        second_available = False

        with self.assertRaisesRegex(ValueError, "待选来源已变化"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=steps,
                available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
                selected_source_id=first.source_id,
                selection_token=waiting.selection_token,
            )

    async def test_selection_requires_current_server_capability(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=first.source_id,
                source_mode=first.source_mode,
                load=lambda: first,
            ),
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=second.source_id,
                source_mode=second.source_mode,
                load=lambda: second,
            ),
        ]
        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )

        with self.assertRaisesRegex(ValueError, "当前能力未允许"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=steps,
                selected_source_id=first.source_id,
                selection_token=waiting.selection_token,
            )

    async def test_selection_token_ignores_acquisition_timestamps(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        current_second = second

        def load_second():
            return current_second

        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=first.source_id,
                source_mode=first.source_mode,
                load=lambda: first,
            ),
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=second.source_id,
                source_mode=second.source_mode,
                load=load_second,
            ),
        ]
        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )

        shifted_collection = second.collection.model_copy(update={
            "sources": [
                source.model_copy(update={
                    "created_at": source.created_at + timedelta(seconds=1),
                })
                for source in second.collection.sources
            ],
            "documents": [
                document.model_copy(update={
                    "retrieved_at": (
                        document.retrieved_at + timedelta(seconds=1)
                    ),
                })
                for document in second.collection.documents
            ],
            "derivatives": [
                derivative.model_copy(update={
                    "created_at": (
                        derivative.created_at + timedelta(seconds=1)
                    ),
                })
                for derivative in second.collection.derivatives
            ],
        })
        current_second = second.model_copy(update={
            "snapshot": second.snapshot.model_copy(update={
                "retrieved_at": (
                    second.snapshot.retrieved_at + timedelta(seconds=1)
                ),
            }),
            "collection": shifted_collection,
        })

        selected = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
            selected_source_id=first.source_id,
            selection_token=waiting.selection_token,
        )
        self.assertEqual(
            selected.result.selected_source_ids,
            [first.source_id],
        )

    async def test_selection_token_keeps_raw_timestamp_named_fields(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        second = _second_official_candidate()
        current_second = second

        def load_second():
            return current_second

        steps = [
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=first.source_id,
                source_mode=first.source_mode,
                load=lambda: first,
            ),
            _step(
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                source_id=second.source_id,
                source_mode=second.source_mode,
                load=load_second,
            ),
        ]
        waiting = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=steps,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )

        raw_definition = dict(second.snapshot.provider_raw_definition)
        raw_definition["created_at"] = "provider-semantic-value"
        current_second = second.model_copy(update={
            "snapshot": second.snapshot.model_copy(update={
                "provider_raw_definition": raw_definition,
            }),
        })

        with self.assertRaisesRegex(ValueError, "待选来源已变化"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=steps,
                available_actions=[
                    QuestionnaireSourceNextAction.SELECT_SOURCE,
                ],
                selected_source_id=first.source_id,
                selection_token=waiting.selection_token,
            )

    async def test_integrity_invalid_candidate_is_reported_as_failed(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        candidate = candidate.model_copy(update={
            "snapshot": candidate.snapshot.model_copy(update={
                "asset_count": candidate.snapshot.asset_count + 1,
            }),
        })

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[_step(
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                source_id=candidate.source_id,
                source_mode=candidate.source_mode,
                load=lambda: candidate,
            )],
            available_actions=[
                QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
            ],
        )

        self.assertEqual(
            result.status,
            QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
        )
        self.assertEqual(
            result.failure_reason,
            QuestionnaireSourceFailureReason.INVALID_INPUT,
        )
        self.assertEqual(result.attempts[0].status, ProcessingStatus.FAILED)
        self.assertEqual(
            result.attempts[0].issues[0].code,
            ImportErrorCode.INTEGRITY_ERROR,
        )

    async def test_bested_actions_are_limited_by_server_capabilities(self):
        failed = _failed(
            source_id="src_bested_demo",
            source_mode=(
                QuestionnaireSourceMode.ORIGINAL_QUESTIONNAIRE_UPLOAD
            ),
            issue=ImportIssue(
                code=ImportErrorCode.PARSE_FAILED,
                message="原问卷格式无法解析",
                retryable=False,
            ),
        )

        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[_step(
                route=(
                    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
                ),
                source_id=failed.source_id,
                source_mode=failed.source_mode,
                load=lambda: failed,
            )],
            available_actions=[
                QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ],
        )

        self.assertEqual(
            result.next_actions,
            [
                QuestionnaireSourceNextAction.UPLOAD_ORIGINAL_QUESTIONNAIRE,
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ],
        )
        self.assertNotIn(
            QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION,
            result.next_actions,
        )
        self.assertNotIn(
            QuestionnaireSourceNextAction.RETRY_PUBLISHED_PAGE,
            result.next_actions,
        )

    async def test_owner_and_candidate_identity_are_checked_before_fallback(self):
        calls: list[str] = []
        with self.assertRaises(QuestionnaireSourceScopeError):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=[_step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id="saved",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    owner_ref="foreign-owner",
                    load=lambda: calls.append("called"),
                )],
            )
        self.assertEqual(calls, [])

        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        with self.assertRaisesRegex(ValueError, "身份"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=[_step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id="declared-other",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=lambda: candidate,
                )],
            )

    async def test_no_applicable_action_finishes_as_failed(self):
        result = await run_questionnaire_source_workflow(
            owner_ref="fixture-user",
            steps=[],
            available_actions=[QuestionnaireSourceNextAction.RETRY_SOURCE],
        )

        self.assertEqual(result.status, QuestionnaireSourceWorkflowStatus.FAILED)
        self.assertEqual(result.next_actions, [])
        self.assertIsNone(result.result)

    async def test_duplicate_capabilities_and_unused_selection_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "available_actions 不能重复"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=[],
                available_actions=[
                    QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
                    QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT,
                ],
            )

        with self.assertRaisesRegex(ValueError, "必须同时提供"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=[_step(
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    source_id="src_google_demo",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    load=lambda: _candidate(
                        "google_forms.json",
                        source_id="src_google_demo",
                    ),
                )],
                selected_source_id="src_google_demo",
            )

        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        with self.assertRaisesRegex(ValueError, "同一获取路径"):
            await run_questionnaire_source_workflow(
                owner_ref="fixture-user",
                steps=[
                    _step(
                        route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                        source_id=candidate.source_id,
                        source_mode=candidate.source_mode,
                        load=lambda: candidate,
                    ),
                    _step(
                        route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                        source_id=candidate.source_id,
                        source_mode=candidate.source_mode,
                        load=lambda: candidate,
                    ),
                ],
            )


class QuestionnaireSourceWorkflowContractTests(unittest.TestCase):
    def test_selected_attempt_must_match_final_snapshot_identity(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        valid_attempt = QuestionnaireSourceAttempt(
            source_id=candidate.source_id,
            source_mode=candidate.source_mode,
            priority=candidate.priority,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.COMPLETED,
            snapshot_id=candidate.snapshot.snapshot_id,
        )
        base = {
            "snapshot": candidate.snapshot,
            "collection": candidate.collection,
            "selected_source_ids": [candidate.source_id],
            "attempts": [valid_attempt],
        }

        for update in (
            {"snapshot_id": "forged-snapshot"},
            {"source_mode": QuestionnaireSourceMode.AUTHORIZED_EDIT},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                QuestionnaireSourceResult(
                    **{
                        **base,
                        "attempts": [valid_attempt.model_copy(update=update)],
                    },
                )

    def test_partial_success_cannot_hide_a_failed_fallback_attempt(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        failed = QuestionnaireSourceAttempt(
            source_id="saved",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.FAILED,
            failure_reason=QuestionnaireSourceFailureReason.NOT_FOUND,
            issues=[ImportIssue(
                code=ImportErrorCode.NOT_FOUND,
                message="saved snapshot not found",
                retryable=False,
            )],
        )
        selected = QuestionnaireSourceAttempt(
            source_id=candidate.source_id,
            source_mode=candidate.source_mode,
            priority=candidate.priority,
            acquisition_route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
            status=ProcessingStatus.COMPLETED,
            snapshot_id=candidate.snapshot.snapshot_id,
        )

        with self.assertRaises(ValidationError):
            QuestionnaireSourceResult(
                snapshot=candidate.snapshot,
                collection=candidate.collection,
                selected_source_ids=[candidate.source_id],
                attempts=[failed, selected],
                partial_success=False,
            )

    def test_route_and_source_mode_must_have_truthful_provenance(self):
        with self.assertRaisesRegex(ValueError, "source_mode 不兼容"):
            _step(
                route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                source_id="source",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                load=lambda: None,
            )
        with self.assertRaisesRegex(ValueError, "source_mode 不兼容"):
            _step(
                route=(
                    QuestionnaireAcquisitionRoute.ORIGINAL_QUESTIONNAIRE_UPLOAD
                ),
                source_id="source",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                load=lambda: None,
            )

        saved = _step(
            route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            source_id="source",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            load=lambda: None,
        )
        self.assertEqual(
            saved.source_mode,
            QuestionnaireSourceMode.OFFICIAL_API,
        )

        with self.assertRaisesRegex(ValueError, "source_mode 不兼容"):
            QuestionnaireSourceAttempt(
                source_id="source",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                acquisition_route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                status=ProcessingStatus.FAILED,
                failure_reason=QuestionnaireSourceFailureReason.UNKNOWN,
                issues=[ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="published page failed",
                    retryable=False,
                )],
            )
        with self.assertRaisesRegex(ValueError, "response_only"):
            QuestionnaireSourceAttempt(
                source_id="source",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                acquisition_route=QuestionnaireAcquisitionRoute.RESPONSE_ONLY,
                status=ProcessingStatus.FAILED,
                failure_reason=QuestionnaireSourceFailureReason.UNKNOWN,
                issues=[ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="response only is not an acquisition attempt",
                    retryable=False,
                )],
            )

    def test_impossible_workflow_states_are_rejected(self):
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
            )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.SKIPPED,
                route=QuestionnaireAcquisitionRoute.RESPONSE_ONLY,
                response_only_confirmed=False,
            )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.FAILED,
                next_actions=[QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT],
            )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.FAILED,
                route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                failure_reason=QuestionnaireSourceFailureReason.UNKNOWN,
            )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                next_actions=[QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT],
                failure_reason=QuestionnaireSourceFailureReason.UNKNOWN,
            )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.SKIPPED,
                route=QuestionnaireAcquisitionRoute.RESPONSE_ONLY,
                attempts=[QuestionnaireSourceAttempt(
                    source_id="source",
                    source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                    priority=2,
                    acquisition_route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                    status=ProcessingStatus.PROCESSING,
                )],
                response_only_confirmed=True,
            )
        login_attempt = QuestionnaireSourceAttempt(
            source_id="source",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            acquisition_route=(
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
            ),
            status=ProcessingStatus.FAILED,
            failure_reason=QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            issues=[ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="login required",
                retryable=False,
            )],
        )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                attempts=[login_attempt],
                next_actions=[QuestionnaireSourceNextAction.RETRY_SOURCE],
                failure_reason=QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                attempts=[login_attempt],
                next_actions=[QuestionnaireSourceNextAction.UPLOAD_SNAPSHOT],
                failure_reason=QuestionnaireSourceFailureReason.UNKNOWN,
            )

    def test_failure_reason_contract_rejects_semantic_mismatch(self):
        with self.assertRaises(ValidationError):
            QuestionnaireSourceAttempt(
                source_id="source",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                acquisition_route=(
                    QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                ),
                status=ProcessingStatus.FAILED,
                failure_reason=(
                    QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER
                ),
                issues=[ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="not retryable",
                    retryable=False,
                )],
            )

        mismatches = [
            (
                QuestionnaireSourceFailureReason.UNKNOWN,
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                [ImportIssue(
                    code=ImportErrorCode.LOGIN_REQUIRED,
                    message="login required",
                    retryable=False,
                )],
            ),
            (
                QuestionnaireSourceFailureReason.RETRYABLE_PROVIDER,
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                [
                    ImportIssue(
                        code=ImportErrorCode.LOGIN_REQUIRED,
                        message="login required",
                        retryable=False,
                    ),
                    ImportIssue(
                        code=ImportErrorCode.PROVIDER_ERROR,
                        message="retryable provider error",
                        retryable=True,
                    ),
                ],
            ),
            (
                QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE,
                QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                [ImportIssue(
                    code=ImportErrorCode.PROVIDER_ERROR,
                    message="retryable provider error",
                    retryable=True,
                )],
            ),
        ]
        for reason, route, issues in mismatches:
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                QuestionnaireSourceAttempt(
                    source_id="source",
                    source_mode=(
                        QuestionnaireSourceMode.PUBLISHED_PAGE
                        if route == QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
                        else QuestionnaireSourceMode.OFFICIAL_API
                    ),
                    priority=(
                        4
                        if route == QuestionnaireAcquisitionRoute.PUBLISHED_PAGE
                        else 2
                    ),
                    acquisition_route=route,
                    status=ProcessingStatus.FAILED,
                    failure_reason=reason,
                    issues=issues,
                )

    def test_active_attempts_only_appear_in_current_in_progress_route(self):
        processing = QuestionnaireSourceAttempt(
            source_id="source",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.PROCESSING,
        )
        login_failed = QuestionnaireSourceAttempt(
            source_id="login",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            acquisition_route=(
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
            ),
            status=ProcessingStatus.FAILED,
            failure_reason=QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            issues=[ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="login required",
                retryable=False,
            )],
        )

        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.IN_PROGRESS,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                attempts=[processing],
            )
        for status, next_actions, reason in (
            (
                QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                [QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION],
                QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            ),
            (
                QuestionnaireSourceWorkflowStatus.FAILED,
                [],
                QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            ),
        ):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                QuestionnaireSourceWorkflowResult(
                    status=status,
                    route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                    attempts=[processing, login_failed],
                    next_actions=next_actions,
                    failure_reason=reason,
                )

        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.IN_PROGRESS,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                attempts=[processing, processing],
            )

        complete = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        complete_attempt = QuestionnaireSourceAttempt(
            source_id=complete.source_id,
            source_mode=complete.source_mode,
            priority=complete.priority,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.COMPLETED,
            snapshot_id=complete.snapshot.snapshot_id,
        )
        processing_other = QuestionnaireSourceAttempt(
            source_id="processing-other",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.PROCESSING,
        )
        resolved_result = QuestionnaireSourceResult(
            snapshot=complete.snapshot,
            collection=complete.collection,
            selected_source_ids=[complete.source_id],
            attempts=[complete_attempt, processing_other],
            partial_success=False,
        )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.RESOLVED,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                result=resolved_result,
                attempts=resolved_result.attempts,
            )

    def test_resolved_and_closed_routes_are_auditable(self):
        complete = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        selected_attempt = QuestionnaireSourceAttempt(
            source_id=complete.source_id,
            source_mode=complete.source_mode,
            priority=complete.priority,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.COMPLETED,
            snapshot_id=complete.snapshot.snapshot_id,
        )
        later_failure = QuestionnaireSourceAttempt(
            source_id="published",
            source_mode=QuestionnaireSourceMode.PUBLISHED_PAGE,
            priority=4,
            acquisition_route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
            status=ProcessingStatus.FAILED,
            failure_reason=QuestionnaireSourceFailureReason.UNKNOWN,
            issues=[ImportIssue(
                code=ImportErrorCode.PROVIDER_ERROR,
                message="published failed",
                retryable=False,
            )],
        )
        result = QuestionnaireSourceResult(
            snapshot=complete.snapshot,
            collection=complete.collection,
            selected_source_ids=[complete.source_id],
            attempts=[selected_attempt, later_failure],
            partial_success=True,
        )
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                result=result,
                attempts=result.attempts,
            )

        closed = later_failure.model_copy(update={
            "failure_reason": (
                QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
            ),
        })
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                attempts=[closed],
                next_actions=[
                    QuestionnaireSourceNextAction.RETRY_PUBLISHED_PAGE,
                ],
                failure_reason=(
                    QuestionnaireSourceFailureReason.CLOSED_PUBLIC_PAGE
                ),
            )

    def test_usable_higher_route_prevents_lower_route_audit(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        saved = QuestionnaireSourceAttempt(
            source_id=candidate.source_id,
            source_mode=candidate.source_mode,
            priority=candidate.priority,
            acquisition_route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            status=ProcessingStatus.COMPLETED,
            snapshot_id=candidate.snapshot.snapshot_id,
        )
        authorized = saved.model_copy(update={
            "acquisition_route": (
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
            ),
        })
        result = QuestionnaireSourceResult(
            snapshot=candidate.snapshot,
            collection=candidate.collection,
            selected_source_ids=[candidate.source_id],
            attempts=[saved, authorized],
            partial_success=False,
        )

        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=QuestionnaireSourceWorkflowStatus.RESOLVED,
                route=QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
                result=result,
                attempts=result.attempts,
            )

    def test_usable_current_route_cannot_wait_or_fail(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
        )
        complete = QuestionnaireSourceAttempt(
            source_id=candidate.source_id,
            source_mode=candidate.source_mode,
            priority=candidate.priority,
            acquisition_route=(
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
            ),
            status=ProcessingStatus.COMPLETED,
            snapshot_id=candidate.snapshot.snapshot_id,
        )
        login = QuestionnaireSourceAttempt(
            source_id="login",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            acquisition_route=(
                QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
            ),
            status=ProcessingStatus.FAILED,
            failure_reason=QuestionnaireSourceFailureReason.LOGIN_REQUIRED,
            issues=[ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="login required",
                retryable=False,
            )],
        )

        for status, actions in (
            (
                QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                [QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION],
            ),
            (QuestionnaireSourceWorkflowStatus.FAILED, []),
        ):
            with self.subTest(status=status), self.assertRaises(ValidationError):
                QuestionnaireSourceWorkflowResult(
                    status=status,
                    route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                    attempts=[complete, login],
                    next_actions=actions,
                    failure_reason=(
                        QuestionnaireSourceFailureReason.LOGIN_REQUIRED
                    ),
                )

        with self.assertRaises(ValidationError):
            QuestionnaireSourceAttempt(
                source_id="source",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                acquisition_route=(
                    QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                ),
                status=ProcessingStatus.FAILED,
                failure_reason=QuestionnaireSourceFailureReason.NOT_FOUND,
                issues=[ImportIssue(
                    code=ImportErrorCode.LOGIN_REQUIRED,
                    message="login required",
                    retryable=False,
                )],
            )

    def test_selection_contract_rejects_ghost_sources(self):
        with self.assertRaises(ValidationError):
            QuestionnaireSourceWorkflowResult(
                status=(
                    QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED
                ),
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                next_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
                selection_source_ids=["ghost-a", "ghost-b"],
                selection_token="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
