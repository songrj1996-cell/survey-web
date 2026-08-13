from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from pydantic import ValidationError

from app.schemas.questionnaire import (
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.questionnaire_sources import (
    QuestionnaireConflictResolution,
    QuestionnaireMergeCandidate,
    QuestionnaireSourceConflict,
    QuestionnaireSourceResult,
    QuestionnaireSourceValue,
)
from app.schemas.research_assets import (
    MediaType,
    ProcessingStatus,
    ResearchAssetCollection,
)
from app.services.questionnaire_source_service import (
    QuestionnaireSourceScopeError,
    QuestionnaireSourceSelectionRequiredError,
    QuestionnaireSourceUnavailableError,
    import_questionnaire_snapshot_package,
    load_questionnaire_source_snapshot,
    resolve_questionnaire_sources,
    save_questionnaire_source_result,
    save_questionnaire_source_snapshot,
)
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetBundle,
    SnapshotConflictError,
    SnapshotPackageError,
    build_snapshot_package,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"


def _bundle(name: str) -> ResearchAssetBundle:
    payload = json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))
    return ResearchAssetBundle(
        QuestionnaireSnapshot.model_validate(payload["snapshot"]),
        ResearchAssetCollection.model_validate(payload["collection"]),
    )


def _candidate(
    name: str,
    *,
    source_id: str,
    priority: int,
    status: ProcessingStatus = ProcessingStatus.COMPLETED,
) -> QuestionnaireMergeCandidate:
    bundle = _bundle(name)
    return QuestionnaireMergeCandidate(
        source_id=source_id,
        source_mode=bundle.snapshot.source_mode,
        priority=priority,
        snapshot=bundle.snapshot,
        collection=bundle.collection,
        status=status,
    )


def _result_with_media() -> tuple[QuestionnaireSourceResult, dict[str, bytes]]:
    bundle = _bundle("google_forms.json")
    media: dict[str, bytes] = {}
    assets = []
    for index, asset in enumerate(bundle.collection.assets):
        if asset.media_type == MediaType.IMAGE:
            content = f"questionnaire-snapshot-image-{index}".encode("utf-8")
            content_hash = hashlib.sha256(content).hexdigest()
            media[content_hash] = content
            asset = asset.model_copy(update={
                "content_hash": content_hash,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = bundle.collection.model_copy(update={"assets": assets})
    candidate = QuestionnaireMergeCandidate(
        source_id="src_google_demo",
        source_mode=bundle.snapshot.source_mode,
        priority=2,
        snapshot=bundle.snapshot,
        collection=collection,
        status=ProcessingStatus.COMPLETED,
    )
    return (
        resolve_questionnaire_sources(
            [candidate],
            owner_ref=collection.owner_ref,
        ),
        media,
    )


def _replace_zip_member(
    archive: bytes,
    member_name: str,
    content: bytes,
) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(archive), "r") as source:
        with zipfile.ZipFile(
            output,
            "w",
            compression=zipfile.ZIP_DEFLATED,
        ) as target:
            for info in source.infolist():
                target.writestr(
                    info,
                    content if info.filename == member_name else source.read(info),
                )
    return output.getvalue()


class _RecordingStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ResearchAssetBundle]] = []

    def load_bundle(self, owner_ref: str, snapshot_id: str):
        return None

    def save_bundle(self, owner_ref: str, bundle: ResearchAssetBundle) -> None:
        self.calls.append((owner_ref, bundle))


class QuestionnaireSourceServiceTests(unittest.TestCase):
    def test_highest_priority_valid_bundle_wins_and_conflicts_are_explicit(self):
        official = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
        )
        upload = _candidate(
            "bested.json",
            source_id="src_bested_demo",
            priority=3,
        )

        result = resolve_questionnaire_sources(
            [upload, official],
            owner_ref="fixture-user",
        )

        self.assertEqual(result.snapshot.snapshot_id, official.snapshot.snapshot_id)
        self.assertEqual(result.selected_source_ids, ["src_google_demo"])
        self.assertTrue(result.partial_success)
        self.assertTrue(result.conflicts)
        self.assertIn(
            "snapshot.title",
            {conflict.field_path for conflict in result.conflicts},
        )
        title_conflict = next(
            conflict for conflict in result.conflicts
            if conflict.field_path == "snapshot.title"
        )
        self.assertEqual(title_conflict.suggested_source_id, "src_google_demo")
        bested_only_presence = next(
            conflict for conflict in result.conflicts
            if conflict.field_path.startswith(
                "snapshot.canonical_questions[bested:"
            )
            and conflict.field_path.endswith(".present")
        )
        self.assertEqual(
            bested_only_presence.suggested_source_id,
            "src_bested_demo",
        )
        self.assertTrue(all(
            conflict.resolution == QuestionnaireConflictResolution.UNRESOLVED
            for conflict in result.conflicts
        ))

    def test_failed_connection_falls_back_to_original_upload(self):
        failed_connection = QuestionnaireMergeCandidate(
            source_id="failed-google-source",
            source_mode=QuestionnaireSourceMode.OFFICIAL_API,
            priority=2,
            status=ProcessingStatus.FAILED,
        )
        upload = _candidate(
            "bested.json",
            source_id="src_bested_demo",
            priority=3,
            status=ProcessingStatus.PARTIAL,
        )

        result = resolve_questionnaire_sources(
            [upload, failed_connection],
            owner_ref="fixture-user",
        )

        self.assertEqual(result.selected_source_ids, ["src_bested_demo"])
        self.assertTrue(result.partial_success)
        self.assertEqual(
            [attempt.status for attempt in result.attempts],
            [ProcessingStatus.FAILED, ProcessingStatus.PARTIAL],
        )

    def test_invalid_higher_priority_bundle_is_not_silently_selected(self):
        official = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
        )
        invalid_snapshot = official.snapshot.model_copy(
            update={"asset_count": official.snapshot.asset_count + 1}
        )
        invalid = official.model_copy(update={"snapshot": invalid_snapshot})
        upload = _candidate(
            "bested.json",
            source_id="src_bested_demo",
            priority=3,
        )

        result = resolve_questionnaire_sources(
            [invalid, upload],
            owner_ref="fixture-user",
        )

        self.assertEqual(result.selected_source_ids, ["src_bested_demo"])
        self.assertTrue(result.partial_success)
        failed_attempt = result.attempts[0]
        self.assertEqual(failed_attempt.status, ProcessingStatus.FAILED)
        self.assertEqual(failed_attempt.issues[0].code.value, "integrity_error")
        self.assertNotIn("different-owner", failed_attempt.issues[0].message)

    def test_all_failed_sources_return_safe_attempts_for_upload_fallback_ui(self):
        candidates = [
            QuestionnaireMergeCandidate(
                source_id="official",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                status=ProcessingStatus.FAILED,
            ),
            QuestionnaireMergeCandidate(
                source_id="published",
                source_mode=QuestionnaireSourceMode.PUBLISHED_PAGE,
                priority=4,
                status=ProcessingStatus.FAILED,
            ),
        ]

        with self.assertRaises(QuestionnaireSourceUnavailableError) as caught:
            resolve_questionnaire_sources(candidates, owner_ref="fixture-user")

        self.assertEqual(
            [attempt.source_id for attempt in caught.exception.attempts],
            ["official", "published"],
        )
        self.assertIn("上传原问卷或快照包", str(caught.exception))

    def test_result_is_saved_only_through_atomic_bundle_port(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
        )
        result = resolve_questionnaire_sources(
            [candidate],
            owner_ref="fixture-user",
        )
        storage = _RecordingStorage()

        save_questionnaire_source_result(result, storage)

        self.assertEqual(storage.calls, [(
            result.collection.owner_ref,
            ResearchAssetBundle(result.snapshot, result.collection),
        )])

    def test_duplicate_source_candidates_are_rejected(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
        )
        duplicate = first.model_copy(update={"priority": 3})

        with self.assertRaisesRegex(ValueError, "重复 source_id"):
            resolve_questionnaire_sources(
                [first, duplicate],
                owner_ref="fixture-user",
            )

    def test_mixed_owner_candidates_are_rejected_before_conflict_values_exist(self):
        owner_a = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
        )
        owner_b = _candidate(
            "bested.json",
            source_id="src_bested_demo",
            priority=3,
        )
        foreign_collection = owner_b.collection.model_copy(update={
            "owner_ref": "foreign-owner",
            "sources": [
                source.model_copy(update={"owner_ref": "foreign-owner"})
                for source in owner_b.collection.sources
            ],
        })
        owner_b = owner_b.model_copy(update={"collection": foreign_collection})

        with self.assertRaises(QuestionnaireSourceScopeError) as caught:
            resolve_questionnaire_sources(
                [owner_a, owner_b],
                owner_ref="fixture-user",
            )

        self.assertFalse(hasattr(caught.exception, "conflicts"))
        self.assertNotIn("foreign-owner", str(caught.exception))
        self.assertNotIn(owner_b.snapshot.title, str(caught.exception))

    def test_same_trust_tier_requires_explicit_selection(self):
        first = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
        )
        second_collection = first.collection.model_copy(update={
            "sources": [
                source.model_copy(update={"source_id": "second-official"})
                if source.source_id == "src_google_demo"
                else source
                for source in first.collection.sources
            ],
            "documents": [
                document.model_copy(update={
                    "source_id": "second-official",
                    "source_locator": (
                        document.source_locator.model_copy(update={
                            "source_id": "second-official",
                        })
                        if document.source_locator is not None
                        and document.source_id == "src_google_demo"
                        else document.source_locator
                    ),
                })
                if document.source_id == "src_google_demo"
                else document
                for document in first.collection.documents
            ],
        })
        second = first.model_copy(update={
            "source_id": "second-official",
            "collection": second_collection,
        })

        with self.assertRaises(
            QuestionnaireSourceSelectionRequiredError
        ) as caught:
            resolve_questionnaire_sources(
                [second, first],
                owner_ref="fixture-user",
            )

        self.assertEqual(
            set(caught.exception.source_ids),
            {"src_google_demo", "second-official"},
        )

    def test_needs_review_selection_is_always_partial(self):
        candidate = _candidate(
            "google_forms.json",
            source_id="src_google_demo",
            priority=2,
            status=ProcessingStatus.NEEDS_REVIEW,
        )

        result = resolve_questionnaire_sources(
            [candidate],
            owner_ref="fixture-user",
        )

        self.assertTrue(result.partial_success)


class QuestionnaireSnapshotPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="questionnaire-snapshot-service-test-"
        )
        self.storage = FileResearchAssetStorage(self.temporary.name)
        self.result, self.media = _result_with_media()
        self.owner_ref = self.result.collection.owner_ref

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _archive(self) -> bytes:
        return build_snapshot_package(
            self.owner_ref,
            ResearchAssetBundle(self.result.snapshot, self.result.collection),
            self.media,
        )

    def test_save_and_load_round_trip_includes_media(self):
        saved = save_questionnaire_source_snapshot(
            self.result,
            self.media,
            self.storage,
        )

        loaded = load_questionnaire_source_snapshot(
            self.owner_ref,
            self.result.snapshot.snapshot_id,
            self.storage,
        )

        self.assertEqual(saved.bundle.snapshot, self.result.snapshot)
        self.assertEqual(saved.media, self.media)
        self.assertEqual(loaded, saved)

    def test_import_keeps_original_source_mode_and_provider_provenance(self):
        original_sources = self.result.collection.sources

        imported = import_questionnaire_snapshot_package(
            self.owner_ref,
            self._archive(),
            self.storage,
        )

        self.assertEqual(
            imported.bundle.snapshot.source_mode,
            QuestionnaireSourceMode.OFFICIAL_API,
        )
        self.assertNotEqual(
            imported.bundle.snapshot.source_mode,
            QuestionnaireSourceMode.PLATFORM_SNAPSHOT,
        )
        self.assertEqual(
            imported.bundle.snapshot.provider,
            self.result.snapshot.provider,
        )
        self.assertEqual(imported.bundle.collection.sources, original_sources)
        self.assertEqual(
            load_questionnaire_source_snapshot(
                self.owner_ref,
                self.result.snapshot.snapshot_id,
                self.storage,
            ),
            imported,
        )

    def test_import_rejects_wrong_owner_and_tampered_media(self):
        archive = self._archive()
        with self.assertRaisesRegex(SnapshotPackageError, "owner_ref"):
            import_questionnaire_snapshot_package(
                "another-owner",
                archive,
                self.storage,
            )

        media_hash = next(iter(self.media))
        tampered = _replace_zip_member(
            archive,
            f"media/{media_hash}",
            b"tampered",
        )
        with self.assertRaisesRegex(SnapshotPackageError, "哈希不一致"):
            import_questionnaire_snapshot_package(
                self.owner_ref,
                tampered,
                self.storage,
            )

        self.assertIsNone(
            load_questionnaire_source_snapshot(
                self.owner_ref,
                self.result.snapshot.snapshot_id,
                self.storage,
            )
        )

    def test_repeated_import_of_identical_package_is_idempotent(self):
        archive = self._archive()

        first = import_questionnaire_snapshot_package(
            self.owner_ref,
            archive,
            self.storage,
        )
        second = import_questionnaire_snapshot_package(
            self.owner_ref,
            archive,
            self.storage,
        )

        self.assertEqual(second, first)
        self.assertEqual(
            load_questionnaire_source_snapshot(
                self.owner_ref,
                self.result.snapshot.snapshot_id,
                self.storage,
            ),
            first,
        )

    def test_same_snapshot_id_with_different_content_propagates_conflict(self):
        save_questionnaire_source_snapshot(
            self.result,
            self.media,
            self.storage,
        )
        changed_result = self.result.model_copy(update={
            "snapshot": self.result.snapshot.model_copy(update={
                "title": "same ID, different content",
            }),
        })

        with self.assertRaises(SnapshotConflictError):
            save_questionnaire_source_snapshot(
                changed_result,
                self.media,
                self.storage,
            )


class QuestionnaireSourceContractTests(unittest.TestCase):
    def test_conflict_accepts_suggestion_or_an_explicit_candidate_only(self):
        candidates = [
            QuestionnaireSourceValue(
                source_id="official",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=2,
                value={"title": "官方标题"},
            ),
            QuestionnaireSourceValue(
                source_id="upload",
                source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
                priority=3,
                value={"title": "上传标题"},
            ),
        ]
        accepted = QuestionnaireSourceConflict(
            conflict_id="conflict-title",
            field_path="snapshot.title",
            candidates=candidates,
            suggested_source_id="official",
            suggested_value={"title": "官方标题"},
            reason="官方定义优先",
            resolution=QuestionnaireConflictResolution.ACCEPT_SUGGESTION,
        )
        self.assertEqual(accepted.selected_source_id, "official")
        self.assertEqual(accepted.selected_value, {"title": "官方标题"})

        with self.assertRaises(ValidationError):
            QuestionnaireSourceConflict(
                conflict_id="conflict-title",
                field_path="snapshot.title",
                candidates=candidates,
                suggested_source_id="official",
                suggested_value={"title": "错误值"},
                reason="官方定义优先",
            )

    def test_merge_candidate_source_mode_must_match_snapshot(self):
        bundle = _bundle("google_forms.json")
        with self.assertRaises(ValidationError):
            QuestionnaireMergeCandidate(
                source_id="src_google_demo",
                source_mode=QuestionnaireSourceMode.MATERIAL_UPLOAD,
                priority=3,
                snapshot=bundle.snapshot,
                collection=bundle.collection,
                status=ProcessingStatus.COMPLETED,
            )

    def test_source_priority_cannot_be_overridden_by_callers(self):
        bundle = _bundle("google_forms.json")
        with self.assertRaisesRegex(ValidationError, "固定级别 2"):
            QuestionnaireMergeCandidate(
                source_id="src_google_demo",
                source_mode=QuestionnaireSourceMode.OFFICIAL_API,
                priority=99,
                snapshot=bundle.snapshot,
                collection=bundle.collection,
                status=ProcessingStatus.COMPLETED,
            )


if __name__ == "__main__":
    unittest.main()
