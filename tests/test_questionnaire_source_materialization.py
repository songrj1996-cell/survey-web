from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from app.core.research_assets import content_sha256
from app.schemas.questionnaire import (
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireAcquisitionRoute,
    QuestionnaireMergeCandidate,
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
)
from app.services.questionnaire_source_materialization import (
    QuestionnaireMaterializedCandidate,
    QuestionnaireSourceMaterializedStep,
    run_and_persist_questionnaire_source_workflow,
)
from app.services.questionnaire_source_service import (
    QuestionnaireSourceScopeError,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetBundle,
    SnapshotPackage,
    SnapshotPackageError,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"
OWNER_REF = "fixture-user"


def _materialized(
    name: str,
    *,
    source_id: str,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
) -> QuestionnaireMaterializedCandidate:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    media: dict[str, bytes] = {}
    assets = []
    for asset in collection.assets:
        if asset.media_type == MediaType.IMAGE:
            content = f"{name}:{asset.asset_id}".encode("utf-8")
            content_hash = content_sha256(content)
            media[content_hash] = content
            asset = asset.model_copy(update={
                "content_hash": content_hash,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = collection.model_copy(update={"assets": assets})
    candidate = QuestionnaireMergeCandidate(
        source_id=source_id,
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
    issue: ImportIssue,
    media=None,
) -> QuestionnaireMaterializedCandidate:
    return QuestionnaireMaterializedCandidate(
        QuestionnaireMergeCandidate(
            source_id=source_id,
            source_mode=source_mode,
            priority=questionnaire_source_priority(source_mode),
            status=ProcessingStatus.FAILED,
            issues=[issue],
        ),
        media or {},
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


class _RecordingStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, SnapshotPackage]] = []

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        return None

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        self.calls.append((owner_ref, package))


class _StorageFailure(RuntimeError):
    pass


class _FailingStorage(_RecordingStorage):
    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        self.calls.append((owner_ref, package))
        raise _StorageFailure("simulated storage failure")


class _SlowStorage(_RecordingStorage):
    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        time.sleep(0.2)
        super().save_snapshot_package(owner_ref, package)


class QuestionnaireSourceMaterializationTests(
    unittest.IsolatedAsyncioTestCase,
):
    async def test_resolved_result_is_persisted_with_complete_media(self):
        materialized = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        with tempfile.TemporaryDirectory(
            prefix="questionnaire-materialization-test-",
        ) as temporary:
            storage = FileResearchAssetStorage(temporary)

            workflow = await run_and_persist_questionnaire_source_workflow(
                owner_ref=OWNER_REF,
                steps=[_step(
                    materialized,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                )],
                storage=storage,
            )

            saved = storage.load_snapshot_package(
                OWNER_REF,
                workflow.result.snapshot.snapshot_id,
            )

        self.assertEqual(
            workflow.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED,
        )
        self.assertIsNotNone(saved)
        self.assertEqual(saved.media, dict(materialized.media))
        self.assertEqual(saved.bundle.snapshot, workflow.result.snapshot)
        self.assertEqual(saved.bundle.collection, workflow.result.collection)

    async def test_fallback_partial_result_is_saved_after_async_loader(self):
        failed = _failed_materialized(
            source_id="authorized",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            issue=ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="需要重新授权",
                retryable=False,
            ),
        )
        uploaded = _materialized(
            "bested.json",
            source_id="src_bested_demo",
        )

        async def load_uploaded():
            return uploaded

        storage = _RecordingStorage()
        workflow = await run_and_persist_questionnaire_source_workflow(
            owner_ref=OWNER_REF,
            steps=[
                _step(
                    failed,
                    route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                ),
                _step(
                    uploaded,
                    route=(
                        QuestionnaireAcquisitionRoute
                        .ORIGINAL_QUESTIONNAIRE_UPLOAD
                    ),
                    load=load_uploaded,
                ),
            ],
            storage=storage,
        )

        self.assertEqual(
            workflow.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )
        self.assertEqual(len(storage.calls), 1)
        self.assertEqual(storage.calls[0][0], OWNER_REF)
        self.assertEqual(storage.calls[0][1].media, dict(uploaded.media))
        self.assertEqual(
            storage.calls[0][1].bundle.snapshot,
            uploaded.candidate.snapshot,
        )

    async def test_non_resolved_states_never_write(self):
        in_progress = QuestionnaireMaterializedCandidate(
            QuestionnaireMergeCandidate(
                source_id="processing",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                status=ProcessingStatus.PROCESSING,
            ),
            {},
        )
        failed = _failed_materialized(
            source_id="authorized",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            issue=ImportIssue(
                code=ImportErrorCode.LOGIN_REQUIRED,
                message="需要授权",
                retryable=False,
            ),
        )
        google = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        google = QuestionnaireMaterializedCandidate(
            google.candidate,
            {"invalid-unselected-media": b"must-not-be-persisted"},
        )
        bested = _materialized(
            "bested.json",
            source_id="src_bested_demo",
        )

        cases = [
            (
                QuestionnaireSourceWorkflowStatus.IN_PROGRESS,
                [_step(
                    in_progress,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                )],
                [],
                False,
            ),
            (
                QuestionnaireSourceWorkflowStatus.AWAITING_ACTION,
                [_step(
                    failed,
                    route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                )],
                [QuestionnaireSourceNextAction.AUTHORIZE_CONNECTION],
                False,
            ),
            (
                QuestionnaireSourceWorkflowStatus.FAILED,
                [_step(
                    failed,
                    route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                )],
                [],
                False,
            ),
            (
                QuestionnaireSourceWorkflowStatus.SELECTION_REQUIRED,
                [
                    _step(
                        google,
                        route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    ),
                    _step(
                        bested,
                        route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    ),
                ],
                [QuestionnaireSourceNextAction.SELECT_SOURCE],
                False,
            ),
            (
                QuestionnaireSourceWorkflowStatus.SKIPPED,
                [],
                [QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY],
                True,
            ),
        ]
        for expected_status, steps, actions, response_only in cases:
            with self.subTest(status=expected_status):
                storage = _RecordingStorage()
                workflow = (
                    await run_and_persist_questionnaire_source_workflow(
                        owner_ref=OWNER_REF,
                        steps=steps,
                        storage=storage,
                        available_actions=actions,
                        response_only=response_only,
                    )
                )
                self.assertEqual(workflow.status, expected_status)
                self.assertEqual(storage.calls, [])

    async def test_selection_persists_only_explicitly_selected_media(self):
        google = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        google = QuestionnaireMaterializedCandidate(
            google.candidate,
            {"invalid-unselected-media": b"must-not-be-persisted"},
        )
        bested = _materialized(
            "bested.json",
            source_id="src_bested_demo",
        )
        steps = [
            _step(
                google,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            ),
            _step(
                bested,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            ),
        ]
        storage = _RecordingStorage()

        waiting = await run_and_persist_questionnaire_source_workflow(
            owner_ref=OWNER_REF,
            steps=steps,
            storage=storage,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
        )
        self.assertEqual(storage.calls, [])

        resolved = await run_and_persist_questionnaire_source_workflow(
            owner_ref=OWNER_REF,
            steps=steps,
            storage=storage,
            available_actions=[QuestionnaireSourceNextAction.SELECT_SOURCE],
            selected_source_id=bested.candidate.source_id,
            selection_token=waiting.selection_token,
        )

        self.assertEqual(
            resolved.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED_PARTIAL,
        )
        self.assertEqual(len(storage.calls), 1)
        package = storage.calls[0][1]
        self.assertEqual(package.media, dict(bested.media))
        self.assertEqual(package.bundle.snapshot, bested.candidate.snapshot)
        self.assertTrue(set(package.media).isdisjoint(set(google.media)))

    async def test_same_source_id_across_routes_uses_final_route_media(self):
        source_id = "src_google_demo"
        missing = _failed_materialized(
            source_id=source_id,
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            issue=ImportIssue(
                code=ImportErrorCode.NOT_FOUND,
                message="未找到已存快照",
                retryable=False,
            ),
            media={"invalid-media-key": b"must-not-be-selected"},
        )
        authorized = _materialized(
            "google_forms.json",
            source_id=source_id,
        )
        storage = _RecordingStorage()

        workflow = await run_and_persist_questionnaire_source_workflow(
            owner_ref=OWNER_REF,
            steps=[
                _step(
                    missing,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                ),
                _step(
                    authorized,
                    route=(
                        QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION
                    ),
                ),
            ],
            storage=storage,
        )

        self.assertEqual(
            workflow.route,
            QuestionnaireAcquisitionRoute.AUTHORIZED_CONNECTION,
        )
        self.assertEqual(len(storage.calls), 1)
        self.assertEqual(
            storage.calls[0][1].media,
            dict(authorized.media),
        )

    async def test_route_source_mode_source_id_and_owner_are_validated(self):
        google = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        with self.assertRaisesRegex(ValueError, "不兼容"):
            QuestionnaireSourceMaterializedStep(
                route=QuestionnaireAcquisitionRoute.PUBLISHED_PAGE,
                source_id=google.candidate.source_id,
                source_mode=google.candidate.source_mode,
                owner_ref=OWNER_REF,
                load=lambda: google,
            )

        storage = _RecordingStorage()
        mismatched_step = QuestionnaireSourceMaterializedStep(
            route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
            source_id="another-source-id",
            source_mode=google.candidate.source_mode,
            owner_ref=OWNER_REF,
            load=lambda: google,
        )
        with self.assertRaisesRegex(ValueError, "身份"):
            await run_and_persist_questionnaire_source_workflow(
                owner_ref=OWNER_REF,
                steps=[mismatched_step],
                storage=storage,
            )
        self.assertEqual(storage.calls, [])

        with self.assertRaises(QuestionnaireSourceScopeError):
            await run_and_persist_questionnaire_source_workflow(
                owner_ref=OWNER_REF,
                steps=[_step(
                    google,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    owner_ref="foreign-owner",
                )],
                storage=storage,
            )
        self.assertEqual(storage.calls, [])

        foreign_collection = google.candidate.collection.model_copy(update={
            "owner_ref": "foreign-owner",
            "sources": [
                source.model_copy(update={"owner_ref": "foreign-owner"})
                for source in google.candidate.collection.sources
            ],
        })
        foreign = QuestionnaireMaterializedCandidate(
            google.candidate.model_copy(update={
                "collection": foreign_collection,
            }),
            google.media,
        )
        with self.assertRaises(QuestionnaireSourceScopeError):
            await run_and_persist_questionnaire_source_workflow(
                owner_ref=OWNER_REF,
                steps=[_step(
                    foreign,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                )],
                storage=storage,
            )
        self.assertEqual(storage.calls, [])

    async def test_missing_or_tampered_selected_media_is_not_written(self):
        valid = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        missing_media = dict(valid.media)
        missing_media.pop(next(iter(missing_media)))
        first_hash = next(iter(valid.media))
        tampered_media = dict(valid.media)
        tampered_media[first_hash] = b"tampered"

        for label, media in (
            ("missing", missing_media),
            ("tampered", tampered_media),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory(
                prefix="questionnaire-materialization-invalid-test-",
            ) as temporary:
                storage = FileResearchAssetStorage(temporary)
                materialized = QuestionnaireMaterializedCandidate(
                    valid.candidate,
                    media,
                )
                with self.assertRaises(SnapshotPackageError):
                    await run_and_persist_questionnaire_source_workflow(
                        owner_ref=OWNER_REF,
                        steps=[_step(
                            materialized,
                            route=(
                                QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT
                            ),
                        )],
                        storage=storage,
                    )
                self.assertEqual(list(Path(temporary).iterdir()), [])

    async def test_storage_failure_propagates_before_success_is_returned(self):
        materialized = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        storage = _FailingStorage()

        with self.assertRaisesRegex(_StorageFailure, "simulated"):
            await run_and_persist_questionnaire_source_workflow(
                owner_ref=OWNER_REF,
                steps=[_step(
                    materialized,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                )],
                storage=storage,
            )

        self.assertEqual(len(storage.calls), 1)
        self.assertEqual(
            storage.calls[0],
            (
                OWNER_REF,
                SnapshotPackage(
                    bundle=ResearchAssetBundle(
                        materialized.candidate.snapshot,
                        materialized.candidate.collection,
                    ),
                    media=dict(materialized.media),
                ),
            ),
        )

    async def test_synchronous_storage_does_not_block_the_event_loop(self):
        materialized = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        storage = _SlowStorage()

        task = asyncio.create_task(
            run_and_persist_questionnaire_source_workflow(
                owner_ref=OWNER_REF,
                steps=[_step(
                    materialized,
                    route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                )],
                storage=storage,
            )
        )
        await asyncio.sleep(0.03)

        self.assertFalse(task.done())
        workflow = await task
        self.assertEqual(
            workflow.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED,
        )
        self.assertEqual(len(storage.calls), 1)

    async def test_media_validation_does_not_block_the_event_loop(self):
        materialized = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        storage = _RecordingStorage()

        from app.services import questionnaire_source_materialization

        original_build = (
            questionnaire_source_materialization.build_snapshot_package
        )

        def slow_build(*args, **kwargs):
            time.sleep(0.2)
            return original_build(*args, **kwargs)

        with patch.object(
            questionnaire_source_materialization,
            "build_snapshot_package",
            side_effect=slow_build,
        ):
            task = asyncio.create_task(
                run_and_persist_questionnaire_source_workflow(
                    owner_ref=OWNER_REF,
                    steps=[_step(
                        materialized,
                        route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                    )],
                    storage=storage,
                )
            )
            await asyncio.sleep(0.03)

            self.assertFalse(task.done())
            workflow = await task

        self.assertEqual(
            workflow.status,
            QuestionnaireSourceWorkflowStatus.RESOLVED,
        )
        self.assertEqual(len(storage.calls), 1)

    async def test_response_only_short_circuits_loader_and_storage(self):
        materialized = _materialized(
            "google_forms.json",
            source_id="src_google_demo",
        )
        loader_called = False

        def must_not_load():
            nonlocal loader_called
            loader_called = True
            raise AssertionError("response-only must not load a source")

        storage = _RecordingStorage()
        workflow = await run_and_persist_questionnaire_source_workflow(
            owner_ref=OWNER_REF,
            steps=[_step(
                materialized,
                route=QuestionnaireAcquisitionRoute.SAVED_SNAPSHOT,
                load=must_not_load,
            )],
            storage=storage,
            available_actions=[
                QuestionnaireSourceNextAction.CONTINUE_RESPONSE_ONLY,
            ],
            response_only=True,
        )

        self.assertEqual(
            workflow.status,
            QuestionnaireSourceWorkflowStatus.SKIPPED,
        )
        self.assertFalse(loader_called)
        self.assertEqual(storage.calls, [])


if __name__ == "__main__":
    unittest.main()
