from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

from app.core.research_assets import validate_research_contract
from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormImageCapture,
    GoogleFormImageFailure,
    GoogleFormsErrorCode,
    GoogleFormsStage,
    GoogleImageContext,
)
from app.schemas.questionnaire import CollectionState, MappingStatus
from app.schemas.research_assets import (
    AssetContextType,
    BindingStatus,
    MediaType,
    Provider,
)
from app.services.google_forms_questionnaire_mapping import map_google_form_capture


GOOGLE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "questionnaire_sources"
    / "google_forms_api.json"
)
RETRIEVED_AT = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)


def _google_capture(*, closed: bool = False) -> GoogleFormCapture:
    raw_form = json.loads(GOOGLE_FIXTURE.read_text(encoding="utf-8"))
    if closed:
        raw_form["publishSettings"]["publishState"].pop(
            "isAcceptingResponses"
        )
    definitions = (
        (
            ("items", 0, "imageItem", "image"),
            GoogleImageContext(0, "item-standalone-image", None, (), None),
        ),
        (
            (
                "items", 1, "questionItem", "question",
                "choiceQuestion", "options", 0, "image",
            ),
            GoogleImageContext(
                1, "item-choice", "question-choice",
                ("question-choice",), 0,
            ),
        ),
        (
            ("items", 1, "questionItem", "image"),
            GoogleImageContext(
                1, "item-choice", "question-choice",
                ("question-choice",), None,
            ),
        ),
        (
            ("items", 2, "questionGroupItem", "image"),
            GoogleImageContext(
                2, "item-grid", None,
                ("question-grid-usability", "question-grid-art"), None,
            ),
        ),
    )
    images: list[GoogleFormImageCapture] = []
    for index, (path, context) in enumerate(definitions):
        content = b"\x89PNG\r\n\x1a\n" + f"fixture-{index}".encode()
        images.append(GoogleFormImageCapture(
            json_path=path,
            context=context,
            content=content,
            mime_type="image/png",
            sha256=hashlib.sha256(content).hexdigest(),
        ))
    return GoogleFormCapture(
        form_id=raw_form["formId"],
        raw_form=raw_form,
        images=tuple(images),
    )


class GoogleQuestionnaireMappingTests(unittest.TestCase):
    def test_maps_provider_items_questions_images_branches_and_response_ids(self):
        result = map_google_form_capture(
            _google_capture(),
            owner_ref="mapping-user",
            retrieved_at=RETRIEVED_AT,
        )
        snapshot = result.bundle.snapshot
        collection = result.bundle.collection

        validate_research_contract(snapshot, collection)
        self.assertEqual(snapshot.mapping_status, MappingStatus.EXACT)
        self.assertEqual(snapshot.collection_state, CollectionState.OPEN)
        self.assertEqual(snapshot.item_count, 6)
        self.assertEqual(snapshot.question_count, 3)
        self.assertEqual(snapshot.asset_count, 5)
        self.assertEqual(len(result.media), 4)
        self.assertNotIn(
            "contentUri",
            json.dumps(snapshot.provider_raw_definition),
        )

        questions = {
            item.provider_item_id: item
            for item in snapshot.canonical_questions
        }
        choice = questions["item-choice"]
        self.assertEqual(len(choice.asset_reference_ids), 1)
        self.assertEqual(len(choice.options[0].asset_reference_ids), 1)
        self.assertEqual(choice.options[1].asset_reference_ids, [])
        self.assertEqual(
            choice.branching[0].target_section_id,
            questions["item-details"].question_id,
        )
        grid = questions["item-grid"]
        self.assertEqual(len(grid.asset_reference_ids), 1)
        self.assertTrue(all(not row.asset_reference_ids for row in grid.rows))
        grid_mapping = next(
            item for item in snapshot.response_column_mappings
            if item.question_id == grid.question_id
        )
        self.assertEqual(
            {binding.response_key for binding in grid_mapping.bindings},
            {"question-grid-usability", "question-grid-art"},
        )
        video_asset = next(
            asset for asset in collection.assets
            if asset.media_type == MediaType.VIDEO
        )
        self.assertEqual(video_asset.provider, Provider.YOUTUBE)

    def test_closed_default_false_state_and_ids_are_reproducible(self):
        first = map_google_form_capture(
            _google_capture(closed=True),
            owner_ref="mapping-user",
            retrieved_at=RETRIEVED_AT,
        )
        second = map_google_form_capture(
            _google_capture(closed=True),
            owner_ref="mapping-user",
            retrieved_at=RETRIEVED_AT,
        )

        self.assertEqual(
            first.bundle.snapshot.collection_state,
            CollectionState.CLOSED,
        )
        self.assertEqual(first, second)

    def test_rejects_tampered_image_and_unscoped_owner_or_time(self):
        capture = _google_capture()
        bad_image = capture.images[0]
        tampered = GoogleFormImageCapture(
            bad_image.json_path,
            bad_image.context,
            bad_image.content + b"tampered",
            bad_image.mime_type,
            bad_image.sha256,
        )
        with self.assertRaisesRegex(ValueError, "sha256"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    (tampered, *capture.images[1:]),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        with self.assertRaisesRegex(ValueError, "图片成功/失败集合"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    capture.images[1:],
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )

    def test_explicit_image_failure_keeps_structure_and_prevents_cross_binding(self):
        capture = _google_capture()
        failed_option_image = capture.images[1]
        partial_capture = GoogleFormCapture(
            form_id=capture.form_id,
            raw_form=capture.raw_form,
            images=(capture.images[0], *capture.images[2:]),
            image_failures=(GoogleFormImageFailure(
                json_path=failed_option_image.json_path,
                context=failed_option_image.context,
                code=GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
                stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                retryable=True,
                status_code=503,
            ),),
        )

        result = map_google_form_capture(
            partial_capture,
            owner_ref="mapping-user",
            retrieved_at=RETRIEVED_AT,
        )
        snapshot = result.bundle.snapshot
        collection = result.bundle.collection

        validate_research_contract(snapshot, collection)
        self.assertEqual(snapshot.mapping_status, MappingStatus.PARTIAL)
        self.assertEqual(len(result.media), 3)
        self.assertEqual(snapshot.asset_count, 4)
        self.assertEqual(collection.sources[0].acquisition_status.value, "partial")
        self.assertEqual(collection.documents[0].parse_status.value, "partial")
        self.assertEqual(
            collection.documents[0].warnings[0].code,
            "google_forms_image_http_error",
        )
        warning = next(
            item for item in snapshot.warnings
            if item.code == "google_forms_image_http_error"
        )
        self.assertFalse(warning.blocking)
        self.assertIn("可稍后重试", warning.message)
        self.assertEqual(
            warning.source_locator.json_path,
            list(failed_option_image.json_path),
        )
        choice = next(
            item for item in snapshot.canonical_questions
            if item.provider_item_id == "item-choice"
        )
        self.assertEqual(choice.options[0].asset_reference_ids, [])
        self.assertEqual(choice.options[1].asset_reference_ids, [])

        nonretryable_capture = GoogleFormCapture(
            form_id=capture.form_id,
            raw_form=capture.raw_form,
            images=(capture.images[0], *capture.images[2:]),
            image_failures=(GoogleFormImageFailure(
                json_path=failed_option_image.json_path,
                context=failed_option_image.context,
                code=GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
                stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                retryable=False,
                status_code=404,
            ),),
        )
        nonretryable_result = map_google_form_capture(
            nonretryable_capture,
            owner_ref="mapping-user",
            retrieved_at=RETRIEVED_AT,
        )
        self.assertIn(
            "无需自动重试",
            next(
                item for item in nonretryable_result.bundle.snapshot.warnings
                if item.code == "google_forms_image_http_error"
            ).message,
        )

        wrong_context = GoogleImageContext(
            item_position=failed_option_image.context.item_position,
            item_id=failed_option_image.context.item_id,
            question_id=failed_option_image.context.question_id,
            question_ids=failed_option_image.context.question_ids,
            option_index=1,
        )
        with self.assertRaisesRegex(ValueError, "option_index"):
            map_google_form_capture(
                GoogleFormCapture(
                    form_id=capture.form_id,
                    raw_form=capture.raw_form,
                    images=(capture.images[0], *capture.images[2:]),
                    image_failures=(GoogleFormImageFailure(
                        json_path=failed_option_image.json_path,
                        context=wrong_context,
                        code=GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        retryable=True,
                        status_code=503,
                    ),),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )

    def test_rejects_missing_duplicate_or_non_download_image_failure_evidence(self):
        capture = _google_capture()
        image = capture.images[0]
        failure_kwargs = {
            "json_path": image.json_path,
            "context": image.context,
            "code": GoogleFormsErrorCode.IMAGE_TOO_LARGE,
            "stage": GoogleFormsStage.IMAGE_DOWNLOAD,
            "retryable": False,
            "status_code": None,
        }
        with self.assertRaisesRegex(ValueError, "同时标记为成功和失败"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    capture.images,
                    (GoogleFormImageFailure(**failure_kwargs),),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        with self.assertRaisesRegex(ValueError, "stage"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    capture.images[1:],
                    (GoogleFormImageFailure(
                        **{
                            **failure_kwargs,
                            "stage": GoogleFormsStage.FORMS_GET,
                        }
                    ),),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        contradictory_failures = (
            GoogleFormImageFailure(
                **{
                    **failure_kwargs,
                    "code": GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
                    "status_code": 200,
                }
            ),
            GoogleFormImageFailure(
                **{
                    **failure_kwargs,
                    "code": GoogleFormsErrorCode.IMAGE_HTTP_ERROR,
                    "status_code": 302,
                }
            ),
            GoogleFormImageFailure(
                **{
                    **failure_kwargs,
                    "code": GoogleFormsErrorCode.TRANSPORT_ERROR,
                    "status_code": 404,
                }
            ),
            GoogleFormImageFailure(
                **{
                    **failure_kwargs,
                    "code": GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                    "retryable": True,
                }
            ),
        )
        for failure in contradictory_failures:
            with self.subTest(failure=failure):
                with self.assertRaisesRegex(ValueError, "语义|状态码"):
                    map_google_form_capture(
                        GoogleFormCapture(
                            capture.form_id,
                            capture.raw_form,
                            capture.images[1:],
                            (failure,),
                        ),
                        owner_ref="mapping-user",
                        retrieved_at=RETRIEVED_AT,
                    )
        bool_option_context = GoogleImageContext(
            item_position=1,
            item_id="item-choice",
            question_id="question-choice",
            question_ids=("question-choice",),
            option_index=True,
        )
        with self.assertRaisesRegex(ValueError, "option_index 无效"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    (capture.images[0], *capture.images[2:]),
                    (GoogleFormImageFailure(
                        json_path=capture.images[1].json_path,
                        context=bool_option_context,
                        code=GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        retryable=False,
                        status_code=None,
                    ),),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        group_image = capture.images[3]
        forged_group_context = GoogleImageContext(
            item_position=group_image.context.item_position,
            item_id=group_image.context.item_id,
            question_id="question-grid-art",
            question_ids=("question-grid-art",),
            option_index=None,
        )
        with self.assertRaisesRegex(ValueError, "questionId 与 JSON 路径"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    capture.images[:3],
                    (GoogleFormImageFailure(
                        json_path=group_image.json_path,
                        context=forged_group_context,
                        code=GoogleFormsErrorCode.IMAGE_TOO_LARGE,
                        stage=GoogleFormsStage.IMAGE_DOWNLOAD,
                        retryable=False,
                        status_code=None,
                    ),),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        wrong_context = GoogleImageContext(
            item_position=1,
            item_id="item-choice",
            question_id="question-choice",
            question_ids=("question-choice",),
            option_index=None,
        )
        wrong_image = GoogleFormImageCapture(
            capture.images[0].json_path,
            wrong_context,
            capture.images[0].content,
            capture.images[0].mime_type,
            capture.images[0].sha256,
        )
        with self.assertRaisesRegex(ValueError, "路径与 Item 位置"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    (wrong_image, *capture.images[1:]),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        option_image = capture.images[1]
        wrong_option = GoogleFormImageCapture(
            option_image.json_path,
            GoogleImageContext(
                item_position=option_image.context.item_position,
                item_id=option_image.context.item_id,
                question_id=option_image.context.question_id,
                question_ids=option_image.context.question_ids,
                option_index=1,
            ),
            option_image.content,
            option_image.mime_type,
            option_image.sha256,
        )
        with self.assertRaisesRegex(ValueError, "option_index"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    (capture.images[0], wrong_option, *capture.images[2:]),
                ),
                owner_ref="mapping-user",
                retrieved_at=RETRIEVED_AT,
            )
        with self.assertRaisesRegex(ValueError, "owner_ref"):
            map_google_form_capture(
                capture,
                owner_ref=" ",
                retrieved_at=RETRIEVED_AT,
            )
        with self.assertRaisesRegex(ValueError, "带时区"):
            map_google_form_capture(
                capture,
                owner_ref="mapping-user",
                retrieved_at=datetime(2026, 8, 12),
            )

    def test_missing_publish_state_remains_unknown(self):
        capture = _google_capture()
        raw_form = dict(capture.raw_form)
        raw_form.pop("publishSettings")

        result = map_google_form_capture(
            GoogleFormCapture(capture.form_id, raw_form, capture.images),
            owner_ref="mapping-user",
            retrieved_at=RETRIEVED_AT,
        )

        self.assertEqual(
            result.bundle.snapshot.collection_state,
            CollectionState.UNKNOWN,
        )


if __name__ == "__main__":
    unittest.main()
