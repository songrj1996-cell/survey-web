from __future__ import annotations

import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from app.core.research_assets import (
    ResearchContractError,
    build_asset_dedupe_key,
    build_import_idempotency_key,
    canonical_json,
    content_sha256,
    structured_sha256,
    validate_research_asset_collection,
    validate_research_contract,
)
from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import (
    ResearchAssetCollection,
    SourceLocator,
)
from app.storage.research_assets import ResearchAssetStorage


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"


def _fixture(name: str) -> dict:
    with open(FIXTURE_DIR / name, "r", encoding="utf-8") as source:
        return json.load(source)


def _contracts(name: str):
    payload = _fixture(name)
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    return snapshot, collection


class ResearchAssetSchemaTests(unittest.TestCase):
    def test_google_fixture_round_trips_without_losing_provider_fields(self):
        snapshot, collection = _contracts("google_forms.json")

        restored_snapshot = QuestionnaireSnapshot.model_validate_json(
            snapshot.model_dump_json()
        )
        restored_collection = ResearchAssetCollection.model_validate_json(
            collection.model_dump_json()
        )

        self.assertEqual(restored_snapshot, snapshot)
        self.assertEqual(restored_collection, collection)
        self.assertTrue(
            restored_snapshot.provider_raw_definition["custom_provider_field"][
                "must_survive_round_trip"
            ]
        )
        self.assertIn(
            "image",
            snapshot.provider_questions[1].raw_definition["questionItem"],
        )
        self.assertIn(
            "image",
            snapshot.provider_questions[1].raw_definition["questionItem"][
                "question"
            ]["choiceQuestion"]["options"][0],
        )
        self.assertEqual(snapshot.question_count, 4)
        self.assertEqual(snapshot.asset_count, 2)
        self.assertEqual(snapshot.asset_reference_count, 3)

    def test_google_contract_supports_one_asset_with_multiple_references(self):
        snapshot, collection = _contracts("google_forms.json")

        validate_research_asset_collection(collection)
        validate_research_contract(
            snapshot,
            collection.assets,
            collection.references,
            collection.derivatives,
        )

        option_asset_references = [
            reference
            for reference in collection.references
            if reference.asset_id == "asset_option_a"
        ]
        self.assertEqual(len(option_asset_references), 2)
        self.assertEqual(
            {reference.context_id for reference in option_asset_references},
            {"q_concept_choice", "q_grid"},
        )

    def test_bested_fixture_preserves_platform_specific_raw_shape(self):
        snapshot, collection = _contracts("bested.json")

        validate_research_asset_collection(collection)
        validate_research_contract(
            snapshot,
            collection.assets,
            collection.references,
            collection.derivatives,
        )

        self.assertEqual(snapshot.provider.value, "bested")
        self.assertEqual(
            snapshot.provider_questions[0].raw_definition["raw_heading"],
            "Q1[矩阵单选题]",
        )
        self.assertTrue(
            snapshot.provider_raw_definition["provider_only_export_metadata"][
                "must_survive_round_trip"
            ]
        )
        self.assertEqual(
            snapshot.canonical_questions[0].canonical_type.value,
            "matrix_single",
        )

    def test_missing_asset_reference_is_rejected(self):
        snapshot, collection = _contracts("google_forms.json")
        broken_reference = collection.references[0].model_copy(
            update={"asset_id": "asset_missing"}
        )

        with self.assertRaisesRegex(ResearchContractError, "不存在的素材"):
            validate_research_contract(
                snapshot,
                collection.assets,
                [broken_reference, *collection.references[1:]],
                collection.derivatives,
            )

    def test_option_stimulus_requires_option_key(self):
        snapshot, collection = _contracts("google_forms.json")
        broken_reference = collection.references[1].model_copy(
            update={"option_key": None}
        )

        with self.assertRaisesRegex(ResearchContractError, "缺少 option_key"):
            validate_research_contract(
                snapshot,
                collection.assets,
                [collection.references[0], broken_reference, collection.references[2]],
                collection.derivatives,
            )

    def test_option_stimulus_requires_an_existing_option(self):
        snapshot, collection = _contracts("google_forms.json")
        broken_reference = collection.references[1].model_copy(
            update={"option_key": "missing-option"}
        )

        with self.assertRaisesRegex(ResearchContractError, "不存在的选项"):
            validate_research_contract(
                snapshot,
                collection.assets,
                [collection.references[0], broken_reference, collection.references[2]],
                collection.derivatives,
            )

    def test_collection_rejects_a_document_with_missing_source(self):
        _, collection = _contracts("google_forms.json")
        broken_document = collection.documents[0].model_copy(
            update={"source_id": "source-missing"}
        )
        broken_collection = collection.model_copy(
            update={"documents": [broken_document]}
        )

        with self.assertRaisesRegex(ResearchContractError, "不存在的来源"):
            validate_research_asset_collection(broken_collection)

    def test_duplicate_ids_are_rejected(self):
        snapshot, collection = _contracts("google_forms.json")

        with self.assertRaisesRegex(ResearchContractError, "素材 存在重复 ID"):
            validate_research_contract(
                snapshot,
                [collection.assets[0], collection.assets[0]],
                collection.references,
                collection.derivatives,
            )

    def test_source_locator_allows_provider_extensions_but_checks_time_range(self):
        locator = SourceLocator.model_validate({
            "provider": "youtube",
            "video_id": "video-demo",
            "time_start_seconds": 10,
            "time_end_seconds": 20,
            "provider_specific_marker": "kept",
        })

        self.assertEqual(
            locator.model_dump(mode="json")["provider_specific_marker"],
            "kept",
        )
        with self.assertRaises(ValidationError):
            SourceLocator(
                provider="youtube",
                video_id="video-demo",
                time_start_seconds=20,
                time_end_seconds=10,
            )


class ResearchAssetIdentityTests(unittest.TestCase):
    def test_canonical_hash_is_independent_of_dict_order(self):
        left = {"provider": "google_forms", "nested": {"b": 2, "a": 1}}
        right = {"nested": {"a": 1, "b": 2}, "provider": "google_forms"}

        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(structured_sha256(left), structured_sha256(right))
        self.assertEqual(
            content_sha256(b"research-assets"),
            "8245f4e2b15797bc6cdbff695cb616b14400d6c7c140c8a9f6c7e02ad5b1984b",
        )

    def test_content_hash_is_the_primary_asset_dedupe_identity(self):
        _, collection = _contracts("google_forms.json")
        original = collection.assets[0]
        renamed = original.model_copy(update={"filename": "renamed.png"})

        self.assertEqual(
            build_asset_dedupe_key(original),
            build_asset_dedupe_key(renamed),
        )
        self.assertEqual(
            build_asset_dedupe_key(original),
            f"content:{original.content_hash}",
        )

    def test_import_idempotency_ignores_retrieval_state_but_tracks_content(self):
        _, collection = _contracts("google_forms.json")
        source = collection.sources[0]
        document = collection.documents[0]
        state_changed = document.model_copy(update={"parse_status": "failed"})
        renamed_source = source.model_copy(update={"original_name": "renamed-form"})
        content_changed = document.model_copy(update={"content_hash": "0" * 64})

        original_key = build_import_idempotency_key(source, document)

        self.assertEqual(
            original_key,
            build_import_idempotency_key(source, state_changed),
        )
        self.assertEqual(
            original_key,
            build_import_idempotency_key(renamed_source, document),
        )
        self.assertNotEqual(
            original_key,
            build_import_idempotency_key(source, content_changed),
        )
        self.assertRegex(original_key, r"^import:v1:[0-9a-f]{64}$")


class ResearchAssetStorageContractTests(unittest.TestCase):
    def test_storage_contract_is_structural_and_has_no_runtime_implementation(self):
        class InMemoryContractStub:
            def load_questionnaire_snapshot(self, snapshot_id):
                return None

            def save_questionnaire_snapshot(self, snapshot):
                return None

            def load_asset_collection(self, document_id):
                return None

            def save_asset_collection(self, collection):
                return None

        self.assertIsInstance(InMemoryContractStub(), ResearchAssetStorage)


if __name__ == "__main__":
    unittest.main()
