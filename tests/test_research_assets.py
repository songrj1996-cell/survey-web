from __future__ import annotations

from copy import deepcopy
import inspect
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from app.core import research_assets as research_assets_module
from app.core.research_assets import (
    ResearchContractError,
    build_asset_dedupe_key,
    build_import_idempotency_key,
    canonical_json,
    content_sha256,
    provider_definition_sha256,
    sanitize_provider_payload,
    structured_sha256,
    validate_research_asset_collection,
    validate_research_contract,
)
from app.schemas.questionnaire import (
    BranchRule,
    QuestionnaireSnapshot,
    ResponseColumnBinding,
)
from app.schemas.research_assets import (
    AssetDerivative,
    ResearchAssetCollection,
    SourceLocator,
)
from app.storage.research_assets import (
    ResearchAssetBundle,
    ResearchAssetStorage,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "research_assets"


def _fixture(name: str) -> dict:
    with open(FIXTURE_DIR / name, "r", encoding="utf-8") as source:
        return json.load(source)


def _contracts(name: str) -> tuple[
    QuestionnaireSnapshot,
    ResearchAssetCollection,
]:
    payload = _fixture(name)
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    return snapshot, collection


def _collection(name: str) -> ResearchAssetCollection:
    return ResearchAssetCollection.model_validate(_fixture(name)["collection"])


def _item(snapshot: QuestionnaireSnapshot, item_id: str):
    return next(
        item
        for item in snapshot.provider_items
        if item.provider_item_id == item_id
    )


def _question(snapshot: QuestionnaireSnapshot, question_id: str):
    return next(
        question
        for question in snapshot.canonical_questions
        if question.question_id == question_id
    )


def _reference(collection: ResearchAssetCollection, reference_id: str):
    return next(
        reference
        for reference in collection.references
        if reference.reference_id == reference_id
    )


def _asset(collection: ResearchAssetCollection, asset_id: str):
    return next(
        asset for asset in collection.assets if asset.asset_id == asset_id
    )


class _CountingList(list):
    """Track yielded elements so reference lookup complexity is testable."""

    def __init__(self, values) -> None:
        super().__init__(values)
        self.yield_count = 0

    def __iter__(self):
        for value in super().__iter__():
            self.yield_count += 1
            yield value


class ResearchAssetFixtureTests(unittest.TestCase):
    def test_google_provider_and_canonical_round_trip_is_lossless(self):
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
            snapshot.provider_raw_definition["custom_provider_field"][
                "must_survive_round_trip"
            ]
        )
        provider_raw_items = {
            item["itemId"]: item
            for item in snapshot.provider_raw_definition["items"]
        }
        self.assertEqual(
            {
                item.provider_item_id: item.raw_definition
                for item in snapshot.provider_items
            },
            provider_raw_items,
        )

        choice = _item(snapshot, "item-choice").raw_definition
        self.assertIn("image", choice["questionItem"])
        self.assertIn(
            "image",
            choice["questionItem"]["question"]["choiceQuestion"][
                "options"
            ][0],
        )
        self.assertEqual(
            sanitize_provider_payload(snapshot.provider_raw_definition),
            snapshot.provider_raw_definition,
        )
        self.assertTrue(all(
            sanitize_provider_payload(item.raw_definition)
            == item.raw_definition
            for item in snapshot.provider_items
        ))

    def test_google_counts_distinguish_items_questions_and_assets(self):
        snapshot, collection = _contracts("google_forms.json")

        self.assertEqual(snapshot.item_count, 10)
        self.assertEqual(snapshot.item_count, len(snapshot.provider_items))
        self.assertEqual(snapshot.question_count, 4)
        self.assertEqual(len(snapshot.canonical_questions), 10)
        self.assertEqual(snapshot.asset_count, 6)
        self.assertEqual(snapshot.asset_count, len(collection.assets))
        self.assertEqual(snapshot.asset_reference_count, 7)
        self.assertEqual(
            sum(
                question.canonical_type.value
                not in {"section", "static_text"}
                for question in snapshot.canonical_questions
            ),
            snapshot.question_count,
        )

    def test_google_open_and_closed_fixtures_only_change_state(self):
        opened = _fixture("google_forms.json")
        closed = _fixture("google_forms_closed.json")

        self.assertEqual(opened["collection"], closed["collection"])
        self.assertEqual(
            opened["snapshot"]["provider_raw_definition"]["items"],
            closed["snapshot"]["provider_raw_definition"]["items"],
        )
        self.assertEqual(
            opened["snapshot"]["canonical_questions"],
            closed["snapshot"]["canonical_questions"],
        )
        self.assertEqual(
            opened["snapshot"]["provider_items"],
            closed["snapshot"]["provider_items"],
        )

        normalized_open = deepcopy(opened["snapshot"])
        normalized_closed = deepcopy(closed["snapshot"])
        normalized_open.pop("snapshot_id")
        normalized_closed.pop("snapshot_id")
        normalized_open.pop("collection_state")
        normalized_closed.pop("collection_state")
        normalized_open["provider_raw_definition"]["publishSettings"][
            "publishState"
        ].pop("isAcceptingResponses")
        normalized_closed["provider_raw_definition"]["publishSettings"][
            "publishState"
        ].pop("isAcceptingResponses", None)

        self.assertEqual(normalized_open, normalized_closed)
        self.assertEqual(opened["snapshot"]["collection_state"], "open")
        self.assertEqual(closed["snapshot"]["collection_state"], "closed")
        self.assertTrue(
            opened["snapshot"]["provider_raw_definition"][
                "publishSettings"
            ]["publishState"]["isAcceptingResponses"]
        )
        closed_publish_state = closed["snapshot"]["provider_raw_definition"][
            "publishSettings"
        ]["publishState"]
        self.assertNotIn("isAcceptingResponses", closed_publish_state)
        self.assertFalse(closed_publish_state.get("isAcceptingResponses", False))

    def test_google_fixture_expresses_all_verified_provider_item_shapes(self):
        snapshot, _ = _contracts("google_forms.json")
        item_types = {item.provider_item_type for item in snapshot.provider_items}

        self.assertTrue({
            "pageBreakItem",
            "textItem",
            "imageItem",
            "videoItem",
            "questionItem.choiceQuestion",
            "questionGroupItem.grid",
            "questionItem.textQuestion",
            "questionItem.fileUploadQuestion",
        }.issubset(item_types))

        for item_id in (
            "item-section-intro",
            "item-text-intro",
            "item-image-standalone",
            "item-video",
            "item-section-details",
            "item-text-drive",
        ):
            with self.subTest(item_id=item_id):
                self.assertEqual(_item(snapshot, item_id).provider_question_ids, [])

        upload = _item(snapshot, "item-upload").raw_definition[
            "questionItem"
        ]["question"]["fileUploadQuestion"]
        self.assertEqual(upload["folderId"], "DEMO_INVALID_UPLOAD_FOLDER_ID")
        self.assertEqual(upload["types"], ["IMAGE", "PDF"])
        self.assertEqual(upload["maxFiles"], 2)
        self.assertEqual(upload["maxFileSize"], "10485760")
        self.assertEqual(
            _item(snapshot, "item-video").raw_definition["videoItem"][
                "video"
            ]["youtubeUri"],
            "https://www.youtube.com/watch?v=DEMO_INVALID_VIDEO",
        )

    def test_google_grid_preserves_each_row_question_and_response_binding(self):
        snapshot, _ = _contracts("google_forms.json")
        provider_grid = _item(snapshot, "item-grid")
        canonical_grid = _question(snapshot, "q_grid")
        mapping = next(
            item
            for item in snapshot.response_column_mappings
            if item.question_id == "q_grid"
        )

        self.assertEqual(
            provider_grid.provider_question_ids,
            ["gf-q-grid-usability", "gf-q-grid-art"],
        )
        self.assertEqual(
            [row.provider_question_id for row in canonical_grid.rows],
            ["gf-q-grid-usability", "gf-q-grid-art"],
        )
        self.assertEqual(
            [binding.row_key for binding in mapping.bindings],
            ["row-usability", "row-art"],
        )
        self.assertEqual(
            [binding.provider_question_id for binding in mapping.bindings],
            ["gf-q-grid-usability", "gf-q-grid-art"],
        )
        self.assertEqual(
            [binding.column_index for binding in mapping.bindings],
            [2, 3],
        )

    def test_google_branching_preserves_all_action_combinations(self):
        snapshot, _ = _contracts("google_forms.json")
        question = _question(snapshot, "q_concept_choice")

        self.assertEqual(
            {
                (branch.option_key, branch.action.value, branch.target_section_id)
                for branch in question.branching
            },
            {
                ("A", "go_to_section", "q_section_details"),
                ("B", "next_section", None),
                ("restart", "restart_form", None),
                ("submit", "submit_form", None),
            },
        )
        with self.assertRaises(ValidationError):
            BranchRule.model_validate({
                "option_key": "A",
                "action": "go_to_section",
                "target_section_id": None,
            })
        with self.assertRaises(ValidationError):
            BranchRule.model_validate({
                "option_key": "B",
                "action": "next_section",
                "target_section_id": "q_section_details",
            })

    def test_bested_fixture_preserves_raw_matrix_media_and_unknown_navigation(self):
        snapshot, collection = _contracts("bested.json")

        restored = QuestionnaireSnapshot.model_validate_json(
            snapshot.model_dump_json()
        )
        self.assertEqual(restored, snapshot)
        self.assertEqual(snapshot.provider.value, "bested")
        self.assertEqual(
            _item(snapshot, "bested-item-q1").raw_definition["raw_heading"],
            "Q1[矩阵单选题]",
        )
        self.assertEqual(
            _question(snapshot, "q_bested_matrix").canonical_type.value,
            "matrix_single",
        )
        self.assertEqual(
            [row.provider_question_id for row in _question(
                snapshot, "q_bested_matrix"
            ).rows],
            ["Q1-row-1", "Q1-row-2"],
        )
        self.assertEqual(snapshot.asset_count, 3)
        self.assertEqual(
            {asset.provider.value for asset in collection.assets},
            {"bested", "youtube", "google_drive"},
        )
        self.assertIn(
            "raw_navigation_annotation",
            _item(snapshot, "bested-item-q2").raw_definition,
        )
        self.assertIn(
            "bested_navigation_unverified",
            {warning.code for warning in snapshot.warnings},
        )
        self.assertEqual(
            _question(snapshot, "q_bested_page_2").mapping_status.value,
            "unsupported",
        )
        reused = [
            reference
            for reference in collection.references
            if reference.asset_id == "asset_bested_image"
        ]
        self.assertEqual(len(reused), 2)
        self.assertEqual(
            {reference.context_type.value for reference in reused},
            {"survey_question", "survey_option"},
        )

    def test_source_matrix_covers_every_source_and_access_state(self):
        collection = _collection("source_matrix.json")
        restored = ResearchAssetCollection.model_validate_json(
            collection.model_dump_json()
        )

        self.assertEqual(restored, collection)
        self.assertEqual(
            {source.provider.value for source in collection.sources},
            {
                "excel",
                "google_drive",
                "youtube",
                "local_upload",
            },
        )
        drive_sources = {
            source.source_id: source.access_status.value
            for source in collection.sources
            if source.provider.value == "google_drive"
        }
        self.assertEqual(
            drive_sources,
            {
                "src_drive_accessible": "accessible",
                "src_drive_permission": "permission_required",
                "src_drive_not_found": "not_found",
            },
        )

        excel = _asset(collection, "asset_excel_image")
        self.assertEqual(excel.source_locator.sheet_name, "访谈记录")
        self.assertEqual(excel.source_locator.anchor, "C7")
        self.assertEqual(excel.source_locator.coverage, "C7:F15")
        shared_drive = _asset(
            collection, "asset_drive_accessible"
        ).source_locator.model_dump(mode="json")
        self.assertEqual(shared_drive["shared_drive_id"], "DEMO_SHARED_DRIVE_ID")
        self.assertTrue(shared_drive["supports_all_drives"])
        self.assertEqual(
            _asset(collection, "asset_youtube_matrix").source_locator.video_id,
            "SOURCE_MATRIX_INVALID_VIDEO",
        )
        self.assertEqual(
            _asset(collection, "asset_local_media").source_locator.local_file_id,
            "fixture-audio.wav",
        )
        self.assertEqual(
            len([
                reference
                for reference in collection.references
                if reference.asset_id == "asset_excel_image"
            ]),
            2,
        )
        for document_or_asset in [*collection.documents, *collection.assets]:
            if document_or_asset.content_hash is not None:
                self.assertRegex(document_or_asset.content_hash, r"^[0-9a-f]{64}$")


class ResearchAssetIntegrityTests(unittest.TestCase):
    def test_every_fixture_passes_its_full_integrity_boundary(self):
        for name in (
            "google_forms.json",
            "google_forms_closed.json",
            "bested.json",
        ):
            with self.subTest(name=name):
                snapshot, collection = _contracts(name)
                validate_research_contract(snapshot, collection)

        validate_research_asset_collection(_collection("source_matrix.json"))

    def test_collection_rejects_missing_duplicate_and_cross_provider_links(self):
        _, original = _contracts("google_forms.json")

        missing_source = original.model_copy(deep=True)
        missing_source.documents[0].source_id = "src-missing"
        with self.assertRaisesRegex(ResearchContractError, "不存在的来源"):
            validate_research_asset_collection(missing_source)

        missing_asset = original.model_copy(deep=True)
        missing_asset.references[0].asset_id = "asset-missing"
        with self.assertRaisesRegex(ResearchContractError, "不存在的素材"):
            validate_research_asset_collection(missing_asset)

        duplicate_asset = original.model_copy(deep=True)
        duplicate_asset.assets.append(duplicate_asset.assets[0])
        with self.assertRaisesRegex(ResearchContractError, "素材 存在重复 ID"):
            validate_research_asset_collection(duplicate_asset)

        wrong_provider = original.model_copy(deep=True)
        wrong_provider.assets[0].provider = "excel"
        with self.assertRaisesRegex(ResearchContractError, "来源 Provider 不一致"):
            validate_research_asset_collection(wrong_provider)

    def test_contract_rejects_owner_document_and_asset_count_mismatches(self):
        original_snapshot, original_collection = _contracts("google_forms.json")

        wrong_owner = original_collection.model_copy(deep=True)
        wrong_owner.sources[0].owner_ref = "other-user"
        with self.assertRaisesRegex(ResearchContractError, "owner_ref"):
            validate_research_contract(original_snapshot, wrong_owner)

        wrong_document = original_snapshot.model_copy(
            update={"document_id": "doc-missing"},
            deep=True,
        )
        with self.assertRaisesRegex(ResearchContractError, "不存在的文档"):
            validate_research_contract(wrong_document, original_collection)

        wrong_count = original_snapshot.model_copy(
            update={"asset_count": original_snapshot.asset_count + 1},
            deep=True,
        )
        with self.assertRaisesRegex(ResearchContractError, "asset_count"):
            validate_research_contract(wrong_count, original_collection)

    def test_contract_rejects_question_reference_cross_binding_both_directions(self):
        original_snapshot, original_collection = _contracts("google_forms.json")

        wrong_reference = original_collection.model_copy(deep=True)
        _reference(
            wrong_reference, "aref_question_image"
        ).context_id = "q_grid"
        with self.assertRaisesRegex(ResearchContractError, "声明位置"):
            validate_research_contract(original_snapshot, wrong_reference)

        wrong_canonical = original_snapshot.model_copy(deep=True)
        choice = _question(wrong_canonical, "q_concept_choice")
        grid = _question(wrong_canonical, "q_grid")
        choice.asset_reference_ids.remove("aref_question_image")
        grid.asset_reference_ids.append("aref_question_image")
        with self.assertRaisesRegex(ResearchContractError, "声明位置"):
            validate_research_contract(wrong_canonical, original_collection)

    def test_contract_rejects_option_reference_cross_binding_both_directions(self):
        original_snapshot, original_collection = _contracts("google_forms.json")

        wrong_reference = original_collection.model_copy(deep=True)
        _reference(wrong_reference, "aref_option_a").option_key = "B"
        with self.assertRaisesRegex(ResearchContractError, "声明位置"):
            validate_research_contract(original_snapshot, wrong_reference)

        wrong_canonical = original_snapshot.model_copy(deep=True)
        options = _question(wrong_canonical, "q_concept_choice").options
        options[0].asset_reference_ids.remove("aref_option_a")
        options[1].asset_reference_ids.append("aref_option_a")
        with self.assertRaisesRegex(ResearchContractError, "声明位置"):
            validate_research_contract(wrong_canonical, original_collection)

    def test_contract_rejects_row_reference_cross_binding_both_directions(self):
        original_snapshot, original_collection = _contracts("google_forms.json")

        wrong_reference = original_collection.model_copy(deep=True)
        _reference(
            wrong_reference, "aref_grid_row_usability"
        ).row_key = "row-art"
        with self.assertRaisesRegex(ResearchContractError, "声明位置"):
            validate_research_contract(original_snapshot, wrong_reference)

        wrong_canonical = original_snapshot.model_copy(deep=True)
        rows = _question(wrong_canonical, "q_grid").rows
        rows[0].asset_reference_ids.remove("aref_grid_row_usability")
        rows[1].asset_reference_ids.append("aref_grid_row_usability")
        with self.assertRaisesRegex(ResearchContractError, "声明位置"):
            validate_research_contract(wrong_canonical, original_collection)

    def test_reference_option_and_row_provider_ids_keep_exact_semantics(self):
        original_snapshot, original_collection = _contracts("google_forms.json")

        matching_option_snapshot = original_snapshot.model_copy(deep=True)
        matching_option_collection = original_collection.model_copy(deep=True)
        option = _question(
            matching_option_snapshot,
            "q_concept_choice",
        ).options[0]
        option.provider_option_id = "provider-option-a"
        _reference(
            matching_option_collection,
            "aref_option_a",
        ).source_locator.provider_option_id = "provider-option-a"
        validate_research_contract(
            matching_option_snapshot,
            matching_option_collection,
        )

        wrong_option_collection = matching_option_collection.model_copy(deep=True)
        _reference(
            wrong_option_collection,
            "aref_option_a",
        ).source_locator.provider_option_id = "provider-option-other"
        with self.assertRaisesRegex(ResearchContractError, "Provider 选项不一致"):
            validate_research_contract(
                matching_option_snapshot,
                wrong_option_collection,
            )

        wrong_row_question = original_collection.model_copy(deep=True)
        _reference(
            wrong_row_question,
            "aref_grid_row_usability",
        ).source_locator.provider_question_id = "gf-q-grid-art"
        with self.assertRaisesRegex(ResearchContractError, "Provider 题目不一致"):
            validate_research_contract(original_snapshot, wrong_row_question)

        wrong_question = original_collection.model_copy(deep=True)
        _reference(
            wrong_question,
            "aref_option_a",
        ).source_locator.provider_question_id = "gf-q-grid-art"
        with self.assertRaisesRegex(ResearchContractError, "指向其他题目"):
            validate_research_contract(original_snapshot, wrong_question)

        wrong_item = original_collection.model_copy(deep=True)
        _reference(
            wrong_item,
            "aref_option_a",
        ).source_locator.provider_item_id = "item-grid"
        with self.assertRaisesRegex(ResearchContractError, "Provider Item 不一致"):
            validate_research_contract(original_snapshot, wrong_item)

    def test_option_reference_validation_scans_options_linearly(self):
        snapshot, collection = _contracts("google_forms.json")
        question = _question(snapshot, "q_concept_choice").model_copy(deep=True)
        base_option = question.options[1]
        base_reference = _reference(collection, "aref_option_a")
        option_count = 128
        reference_count = 64
        reference_ids = [
            f"aref-linear-option-{index}"
            for index in range(reference_count)
        ]
        options = [
            base_option.model_copy(update={
                "option_key": f"option-{index}",
                "value": f"选项 {index}",
                "label": f"选项 {index}",
                "asset_reference_ids": [],
                "provider_option_id": f"provider-option-{index}",
            })
            for index in range(option_count - 1)
        ]
        options.append(base_option.model_copy(update={
            "option_key": "target-option",
            "value": "目标选项",
            "label": "目标选项",
            "asset_reference_ids": reference_ids,
            "provider_option_id": "provider-target-option",
        }))
        counted_options = _CountingList(options)
        question.options = counted_options
        question.asset_reference_ids = []
        question.rows = []

        references = []
        for reference_id in reference_ids:
            locator = base_reference.source_locator.model_copy(update={
                "provider_option_id": "provider-target-option",
            })
            references.append(base_reference.model_copy(update={
                "reference_id": reference_id,
                "option_key": "target-option",
                "source_locator": locator,
            }))
        isolated_collection = collection.model_copy(
            update={"references": references},
            deep=True,
        )
        isolated_snapshot = snapshot.model_copy(
            update={"canonical_questions": [question]},
            deep=False,
        )

        research_assets_module._validate_references(
            isolated_snapshot,
            isolated_collection,
            {question.question_id: question},
        )

        self.assertGreaterEqual(counted_options.yield_count, option_count)
        self.assertLessEqual(
            counted_options.yield_count,
            option_count * 2 + 2,
            "每条引用不得重新线性扫描全部选项",
        )

    def test_row_reference_validation_scans_rows_linearly(self):
        snapshot, collection = _contracts("google_forms.json")
        question = _question(snapshot, "q_grid").model_copy(deep=True)
        base_row = question.rows[1]
        base_reference = _reference(collection, "aref_grid_row_usability")
        row_count = 128
        reference_count = 64
        reference_ids = [
            f"aref-linear-row-{index}"
            for index in range(reference_count)
        ]
        rows = [
            base_row.model_copy(update={
                "row_key": f"row-{index}",
                "label": f"行 {index}",
                "provider_question_id": f"provider-row-{index}",
                "asset_reference_ids": [],
            })
            for index in range(row_count - 1)
        ]
        rows.append(base_row.model_copy(update={
            "row_key": "target-row",
            "label": "目标行",
            "provider_question_id": "provider-target-row",
            "asset_reference_ids": reference_ids,
        }))
        counted_rows = _CountingList(rows)
        question.rows = counted_rows
        question.asset_reference_ids = []
        question.options = []

        references = []
        for reference_id in reference_ids:
            locator = base_reference.source_locator.model_copy(update={
                "provider_question_id": "provider-target-row",
            })
            references.append(base_reference.model_copy(update={
                "reference_id": reference_id,
                "row_key": "target-row",
                "source_locator": locator,
            }))
        isolated_collection = collection.model_copy(
            update={"references": references},
            deep=True,
        )
        isolated_snapshot = snapshot.model_copy(
            update={"canonical_questions": [question]},
            deep=False,
        )

        research_assets_module._validate_references(
            isolated_snapshot,
            isolated_collection,
            {question.question_id: question},
        )

        self.assertGreaterEqual(counted_rows.yield_count, row_count)
        self.assertLessEqual(
            counted_rows.yield_count,
            row_count * 2 + 2,
            "每条引用不得重新扫描全部矩阵行或 Provider questionId",
        )

    def test_contract_rejects_provider_item_position_and_question_ownership(self):
        original_snapshot, collection = _contracts("google_forms.json")

        wrong_position = original_snapshot.model_copy(deep=True)
        wrong_position.provider_items[1].provider_position = 99
        with self.assertRaisesRegex(ResearchContractError, "数组位置"):
            validate_research_contract(wrong_position, collection)

        wrong_item = original_snapshot.model_copy(deep=True)
        _question(wrong_item, "q_open").provider_item_id = "item-choice"
        with self.assertRaisesRegex(ResearchContractError, "不属于声明"):
            validate_research_contract(wrong_item, collection)

    def test_contract_rejects_wrong_grid_response_mapping(self):
        original_snapshot, collection = _contracts("google_forms.json")

        wrong_row = original_snapshot.model_copy(deep=True)
        grid_mapping = next(
            mapping
            for mapping in wrong_row.response_column_mappings
            if mapping.question_id == "q_grid"
        )
        grid_mapping.bindings[0].row_key = "row-art"
        with self.assertRaisesRegex(ResearchContractError, "矩阵行"):
            validate_research_contract(wrong_row, collection)

        duplicate_column = original_snapshot.model_copy(deep=True)
        open_mapping = next(
            mapping
            for mapping in duplicate_column.response_column_mappings
            if mapping.question_id == "q_open"
        )
        open_mapping.bindings[0].column_index = 2
        with self.assertRaisesRegex(ResearchContractError, "回答位置重复绑定"):
            validate_research_contract(duplicate_column, collection)

    def test_exact_mapping_requires_every_grid_row_and_non_static_question(self):
        original_snapshot, collection = _contracts("google_forms.json")

        missing_grid_row = original_snapshot.model_copy(deep=True)
        grid_mapping = next(
            mapping
            for mapping in missing_grid_row.response_column_mappings
            if mapping.question_id == "q_grid"
        )
        grid_mapping.bindings.pop()
        with self.assertRaisesRegex(
            ResearchContractError,
            "未覆盖全部 Provider 题目",
        ):
            validate_research_contract(missing_grid_row, collection)

        missing_question_mapping = original_snapshot.model_copy(deep=True)
        missing_question_mapping.response_column_mappings = [
            mapping
            for mapping in missing_question_mapping.response_column_mappings
            if mapping.question_id != "q_open"
        ]
        with self.assertRaisesRegex(
            ResearchContractError,
            "q_open 缺少回答列映射",
        ):
            validate_research_contract(missing_question_mapping, collection)

    def test_google_raw_item_and_question_ids_must_match_declared_ids(self):
        original_snapshot, collection = _contracts("google_forms.json")

        wrong_item_id = original_snapshot.model_copy(deep=True)
        _item(wrong_item_id, "item-choice").raw_definition[
            "itemId"
        ] = "item-contradiction"
        with self.assertRaisesRegex(
            ResearchContractError,
            "Google 原始 itemId 不一致",
        ):
            validate_research_contract(wrong_item_id, collection)

        wrong_question_id = original_snapshot.model_copy(deep=True)
        _item(wrong_question_id, "item-choice").raw_definition[
            "questionItem"
        ]["question"]["questionId"] = "gf-q-contradiction"
        with self.assertRaisesRegex(
            ResearchContractError,
            "questionId",
        ):
            validate_research_contract(wrong_question_id, collection)

    def test_google_exact_root_definition_must_match_provider_items(self):
        original_snapshot, collection = _contracts("google_forms.json")

        missing_snapshot_form_id = original_snapshot.model_copy(deep=True)
        missing_snapshot_form_id.provider_form_id = None
        with self.assertRaisesRegex(
            ResearchContractError,
            "缺少 provider_form_id",
        ):
            validate_research_contract(missing_snapshot_form_id, collection)

        missing_raw_form_id = original_snapshot.model_copy(deep=True)
        missing_raw_form_id.provider_raw_definition.pop("formId")
        with self.assertRaisesRegex(
            ResearchContractError,
            "根原始定义缺少 formId",
        ):
            validate_research_contract(missing_raw_form_id, collection)

        wrong_raw_form_id = original_snapshot.model_copy(deep=True)
        wrong_raw_form_id.provider_raw_definition["formId"] = "OTHER_FORM"
        with self.assertRaisesRegex(
            ResearchContractError,
            "根原始 formId",
        ):
            validate_research_contract(wrong_raw_form_id, collection)

        wrong_root_item_id = original_snapshot.model_copy(deep=True)
        wrong_root_item_id.provider_raw_definition["items"][0][
            "itemId"
        ] = "OTHER_ITEM"
        with self.assertRaisesRegex(
            ResearchContractError,
            r"根原始 items\[0\].*itemId",
        ):
            validate_research_contract(wrong_root_item_id, collection)

        wrong_root_question_id = original_snapshot.model_copy(deep=True)
        wrong_root_question_id.provider_raw_definition["items"][4][
            "questionItem"
        ]["question"]["questionId"] = "OTHER_QUESTION"
        with self.assertRaisesRegex(
            ResearchContractError,
            r"根原始 items\[4\].*questionId",
        ):
            validate_research_contract(wrong_root_question_id, collection)

        incomplete_root_items = original_snapshot.model_copy(deep=True)
        incomplete_root_items.provider_raw_definition["items"].pop()
        with self.assertRaisesRegex(
            ResearchContractError,
            "根原始 items 与 Provider Items 数量",
        ):
            validate_research_contract(incomplete_root_items, collection)

    def test_derivative_parent_and_human_author_are_required(self):
        _, collection = _contracts("google_forms.json")
        revision = next(
            item
            for item in collection.derivatives
            if item.derivative_type.value == "human_revision"
        )

        self.assertEqual(revision.created_by_ref, "fixture-reviewer")
        self.assertEqual(
            revision.revised_from_derivative_id,
            "der_question_understanding",
        )

        no_author = revision.model_dump(mode="json")
        no_author["created_by_ref"] = None
        with self.assertRaises(ValidationError):
            AssetDerivative.model_validate(no_author)

        missing_parent = collection.model_copy(deep=True)
        next(
            item
            for item in missing_parent.derivatives
            if item.derivative_type.value == "human_revision"
        ).revised_from_derivative_id = "der-missing"
        with self.assertRaisesRegex(ResearchContractError, "不存在的父版本"):
            validate_research_asset_collection(missing_parent)


class ResearchAssetSchemaTests(unittest.TestCase):
    def test_future_schema_versions_are_rejected(self):
        payload = _fixture("google_forms.json")
        future_snapshot = deepcopy(payload["snapshot"])
        future_collection = deepcopy(payload["collection"])
        future_snapshot["schema_version"] = 2
        future_collection["schema_version"] = 2

        with self.assertRaises(ValidationError):
            QuestionnaireSnapshot.model_validate(future_snapshot)
        with self.assertRaises(ValidationError):
            ResearchAssetCollection.model_validate(future_collection)

    def test_count_fields_are_schema_invariants(self):
        payload = _fixture("google_forms.json")["snapshot"]
        wrong_items = deepcopy(payload)
        wrong_items["item_count"] += 1
        wrong_questions = deepcopy(payload)
        wrong_questions["question_count"] += 1

        with self.assertRaisesRegex(ValidationError, "item_count"):
            QuestionnaireSnapshot.model_validate(wrong_items)
        with self.assertRaisesRegex(ValidationError, "question_count"):
            QuestionnaireSnapshot.model_validate(wrong_questions)

    def test_owner_ref_rejects_whitespace_only_values(self):
        payload = _fixture("google_forms.json")["collection"]

        blank_collection_owner = deepcopy(payload)
        blank_collection_owner["owner_ref"] = " \t\n "
        with self.assertRaises(ValidationError):
            ResearchAssetCollection.model_validate(blank_collection_owner)

        blank_source_owner = deepcopy(payload)
        blank_source_owner["sources"][0]["owner_ref"] = "   "
        with self.assertRaises(ValidationError):
            ResearchAssetCollection.model_validate(blank_source_owner)

    def test_provider_raw_definition_rejects_non_json_values(self):
        payload = _fixture("google_forms.json")["snapshot"]
        non_json_item_raw = deepcopy(payload)
        non_json_item_raw["provider_items"][0]["raw_definition"][
            "not_json"
        ] = {"set-member"}

        with self.assertRaises(ValidationError):
            QuestionnaireSnapshot.model_validate(non_json_item_raw)

    def test_response_binding_requires_a_key_or_column(self):
        with self.assertRaises(ValidationError):
            ResponseColumnBinding.model_validate({
                "provider_question_id": "provider-q",
                "row_key": None,
                "response_key": None,
                "column_index": None,
                "column_header": "演示列",
                "source_locator": None,
            })

    def test_source_locator_keeps_extensions_and_enforces_provider_identity(self):
        locator = SourceLocator.model_validate({
            "provider": "google_drive",
            "drive_file_id": "DEMO_DRIVE_ID",
            "shared_drive_id": "DEMO_SHARED_DRIVE_ID",
            "supports_all_drives": True,
        })
        dumped = locator.model_dump(mode="json")
        self.assertEqual(dumped["shared_drive_id"], "DEMO_SHARED_DRIVE_ID")
        self.assertTrue(dumped["supports_all_drives"])

        invalid_locators = [
            {"provider": "google_forms"},
            {"provider": "google_drive"},
            {"provider": "youtube"},
            {"provider": "excel"},
            {"provider": "local_upload"},
            {
                "provider": "youtube",
                "video_id": "DEMO_VIDEO",
                "time_start_seconds": 20,
                "time_end_seconds": 10,
            },
        ]
        for invalid in invalid_locators:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                SourceLocator.model_validate(invalid)


class ResearchAssetSanitizationTests(unittest.TestCase):
    def test_provider_payload_removes_transient_urls_tokens_and_query_secrets(self):
        raw = {
            "formId": "DEMO_FORM",
            "image": {
                "contentUri": (
                    "https://media.example.invalid/a.png?token=temporary"
                ),
                "sourceUri": "https://assets.example.invalid/source.png",
                "altText": "演示图",
            },
            "access_token": "secret-value",
            "Authorization": "Bearer secret-value",
            "nested": {
                "refreshToken": "secret-value",
                "cookie": "session=secret-value",
                "safeUrl": (
                    "https://api.example.invalid/file?lang=zh&token=secret"
                    "&X-Goog-Signature=temporary#private-fragment"
                ),
            },
        }
        original = deepcopy(raw)

        sanitized = sanitize_provider_payload(raw)
        serialized = canonical_json(sanitized)

        self.assertEqual(raw, original)
        self.assertNotIn("contentUri", serialized)
        self.assertNotIn("access_token", serialized)
        self.assertNotIn("Authorization", serialized)
        self.assertNotIn("refreshToken", serialized)
        self.assertNotIn("cookie", serialized)
        self.assertNotIn("secret-value", serialized)
        self.assertNotIn("X-Goog-Signature", serialized)
        self.assertNotIn("private-fragment", serialized)
        self.assertEqual(
            sanitized["nested"]["safeUrl"],
            "https://api.example.invalid/file?lang=zh",
        )
        self.assertEqual(
            sanitized["image"]["sourceUri"],
            "https://assets.example.invalid/source.png",
        )

    def test_provider_definition_hash_ignores_transient_secret_differences(self):
        left = {
            "title": "相同定义",
            "contentUri": "https://media.example.invalid/a?token=left",
            "endpoint": "https://api.example.invalid/x?lang=zh&signature=left",
            "token": "left",
        }
        right = {
            "title": "相同定义",
            "contentUri": "https://other.example.invalid/b?token=right",
            "endpoint": "https://api.example.invalid/x?signature=right&lang=zh",
            "token": "right",
        }

        self.assertEqual(
            provider_definition_sha256(left),
            provider_definition_sha256(right),
        )
        self.assertNotEqual(
            provider_definition_sha256(left),
            provider_definition_sha256({**right, "title": "定义已改变"}),
        )

    def test_secret_aliases_are_removed_without_dropping_public_metadata(self):
        raw = {
            "auth": "secret-auth",
            "jwt": "secret-jwt",
            "access_key_id": "secret-access-key-id",
            "access_key": "secret-access-key",
            "secret_access_key": "secret-access-key",
            "secret_key": "secret-key",
            "api_secret": "api-secret",
            "api_token": "api-token",
            "x_auth_token": "x-auth-token",
            "github_token": "github-token",
            "client_assertion": "secret-client-assertion",
            "requiresAuthorization": False,
            "isSecret": False,
            "usesCookie": False,
            "stableToken": "public-enum",
        }

        sanitized = sanitize_provider_payload(raw)

        for secret_field in (
            "auth",
            "jwt",
            "access_key_id",
            "access_key",
            "secret_access_key",
            "secret_key",
            "api_secret",
            "api_token",
            "x_auth_token",
            "github_token",
            "client_assertion",
            "stableToken",
        ):
            self.assertNotIn(secret_field, sanitized)
        self.assertEqual(sanitized["requiresAuthorization"], False)
        self.assertEqual(sanitized["isSecret"], False)
        self.assertEqual(sanitized["usesCookie"], False)

    def test_contract_rejects_unsanitized_provider_definition(self):
        snapshot, collection = _contracts("google_forms.json")
        unsafe = snapshot.model_copy(deep=True)
        unsafe.provider_raw_definition["api_token"] = "must-not-persist"

        with self.assertRaisesRegex(ResearchContractError, "必须先脱敏"):
            validate_research_contract(unsafe, collection)

    def test_contract_rejects_secrets_hidden_in_warning_and_binding_locators(self):
        original_snapshot, collection = _contracts("google_forms.json")

        unsafe_warning = original_snapshot.model_copy(deep=True)
        upload_question = _question(unsafe_warning, "q_upload")
        upload_warning = upload_question.warnings[0]
        upload_warning.source_locator = upload_warning.source_locator.model_copy(
            update={"accessToken": "warning-secret"}
        )
        with self.assertRaisesRegex(ResearchContractError, "必须先脱敏"):
            validate_research_contract(unsafe_warning, collection)

        unsafe_binding = original_snapshot.model_copy(deep=True)
        grid_mapping = next(
            mapping
            for mapping in unsafe_binding.response_column_mappings
            if mapping.question_id == "q_grid"
        )
        binding = grid_mapping.bindings[0]
        binding.source_locator = binding.source_locator.model_copy(
            update={"accessToken": "binding-secret"}
        )
        with self.assertRaisesRegex(ResearchContractError, "必须先脱敏"):
            validate_research_contract(unsafe_binding, collection)


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

    def test_asset_dedupe_identity_is_content_first_and_owner_scoped(self):
        _, collection = _contracts("google_forms.json")
        original = _asset(collection, "asset_question_image")
        renamed = original.model_copy(update={"filename": "renamed.png"})

        owner_key = build_asset_dedupe_key(
            original,
            owner_ref="fixture-user",
        )
        self.assertEqual(
            owner_key,
            build_asset_dedupe_key(renamed, owner_ref="fixture-user"),
        )
        self.assertNotEqual(
            owner_key,
            build_asset_dedupe_key(original, owner_ref="other-user"),
        )
        self.assertNotEqual(
            owner_key,
            build_asset_dedupe_key(
                original,
                owner_ref="fixture-user",
                collection_id="another-collection",
            ),
        )
        self.assertRegex(
            owner_key,
            rf"^asset:v2:[0-9a-f]{{64}}:content:{original.content_hash}$",
        )
        with self.assertRaises(ValueError):
            build_asset_dedupe_key(original, owner_ref=" ")

    def test_import_idempotency_ignores_retrieval_state_but_tracks_owner_content(self):
        _, collection = _contracts("google_forms.json")
        source = next(
            item
            for item in collection.sources
            if item.source_id == "src_google_demo"
        )
        document = next(
            item
            for item in collection.documents
            if item.document_id == "doc_google_demo"
        )
        state_changed = document.model_copy(update={"parse_status": "failed"})
        renamed_source = source.model_copy(update={"original_name": "renamed"})
        content_changed = document.model_copy(update={"content_hash": "0" * 64})
        owner_changed = source.model_copy(update={"owner_ref": "other-user"})

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
        self.assertNotEqual(
            original_key,
            build_import_idempotency_key(owner_changed, document),
        )
        self.assertRegex(original_key, r"^import:v1:[0-9a-f]{64}$")


class ResearchAssetStorageContractTests(unittest.TestCase):
    def test_storage_contract_is_owner_scoped_and_bundle_atomic(self):
        class InMemoryContractStub:
            def load_bundle(self, owner_ref, snapshot_id):
                return None

            def save_bundle(self, owner_ref, bundle):
                return None

        class LegacySplitStub:
            def load_questionnaire_snapshot(self, snapshot_id):
                return None

            def save_asset_collection(self, collection):
                return None

        snapshot, collection = _contracts("google_forms.json")
        bundle = ResearchAssetBundle(snapshot=snapshot, collection=collection)

        self.assertIsInstance(InMemoryContractStub(), ResearchAssetStorage)
        self.assertNotIsInstance(LegacySplitStub(), ResearchAssetStorage)
        self.assertIs(bundle.snapshot, snapshot)
        self.assertIs(bundle.collection, collection)
        self.assertEqual(
            list(inspect.signature(ResearchAssetStorage.load_bundle).parameters),
            ["self", "owner_ref", "snapshot_id"],
        )
        self.assertEqual(
            list(inspect.signature(ResearchAssetStorage.save_bundle).parameters),
            ["self", "owner_ref", "bundle"],
        )


if __name__ == "__main__":
    unittest.main()
