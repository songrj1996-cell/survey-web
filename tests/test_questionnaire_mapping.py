from __future__ import annotations

import base64
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

import openpyxl
from openpyxl.drawing.image import Image

from app.core.research_assets import validate_research_contract
from app.integrations.bested_questionnaire_client import (
    parse_bested_questionnaire_upload,
)
from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormImageCapture,
    GoogleImageContext,
)
from app.schemas.questionnaire import CollectionState, MappingStatus
from app.schemas.research_assets import (
    AssetContextType,
    BindingStatus,
    MediaType,
    Provider,
)
from app.services.questionnaire_mapping import (
    map_bested_questionnaire_upload,
    map_google_form_capture,
)


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


def _bested_workbook() -> bytes:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
        "AAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "问卷内容"
    for row in (
        ("题号", "题目"),
        ("Q1[单选题]", "更喜欢哪种方案"),
        ("选项", ""),
        ("1", "方案 A"),
        ("2", "方案 B"),
        ("媒体链接", "演示视频"),
        ("Q2[矩阵单选题]", "功能评价"),
        ("选项", ""),
        ("1", "满意"),
        ("2", "不满意"),
        ("矩阵行", ""),
        ("1", "易用性"),
        ("2", "美术表现"),
        ("外部文件", "演示文件"),
    ):
        worksheet.append(row)
    worksheet["B6"].hyperlink = (
        "https://www.youtube.com/watch?v=SYNTHETIC_VIDEO_01"
    )
    worksheet["B14"].hyperlink = (
        "https://drive.google.com/file/d/SYNTHETIC_DRIVE_01/view"
    )
    worksheet.add_image(Image(io.BytesIO(png)), "C3")
    stream = io.BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


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
        with self.assertRaisesRegex(ValueError, "捕获集合"):
            map_google_form_capture(
                GoogleFormCapture(
                    capture.form_id,
                    capture.raw_form,
                    capture.images[1:],
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


class BestedQuestionnaireMappingTests(unittest.TestCase):
    def test_original_upload_produces_partial_snapshot_and_discovers_media(self):
        content = _bested_workbook()
        parsed = parse_bested_questionnaire_upload(
            "synthetic-questionnaire.xlsx",
            content,
        )
        result = map_bested_questionnaire_upload(
            parsed,
            owner_ref="mapping-user",
            filename="synthetic-questionnaire.xlsx",
            questionnaire_content=content,
            retrieved_at=RETRIEVED_AT,
        )
        snapshot = result.bundle.snapshot
        collection = result.bundle.collection

        validate_research_contract(snapshot, collection)
        self.assertEqual(snapshot.mapping_status, MappingStatus.PARTIAL)
        self.assertEqual(snapshot.item_count, 2)
        self.assertEqual(snapshot.question_count, 2)
        self.assertEqual(snapshot.asset_count, 3)
        self.assertEqual(len(result.media), 1)
        self.assertTrue(all(
            not mapping.bindings
            and mapping.mapping_status == MappingStatus.NEEDS_REVIEW
            for mapping in snapshot.response_column_mappings
        ))
        self.assertEqual(
            {asset.provider for asset in collection.assets},
            {Provider.BESTED, Provider.YOUTUBE, Provider.GOOGLE_DRIVE},
        )
        image_reference = next(
            reference for reference in collection.references
            if collection.assets[[
                asset.asset_id for asset in collection.assets
            ].index(reference.asset_id)].media_type == MediaType.IMAGE
        )
        self.assertEqual(
            image_reference.context_type,
            AssetContextType.SURVEY_QUESTION,
        )
        self.assertEqual(
            image_reference.binding_status,
            BindingStatus.NEEDS_REVIEW,
        )
        first_question = snapshot.canonical_questions[0]
        self.assertEqual(
            first_question.options[0].asset_reference_ids,
            [],
            "Excel 行区间证据不能伪造成选项级图片绑定",
        )
        self.assertIn(
            ["Q1[单选题]", "更喜欢哪种方案"],
            snapshot.provider_raw_definition["rows"],
        )

    def test_bested_mapping_is_reproducible_for_same_file_and_scope(self):
        content = _bested_workbook()
        parsed = parse_bested_questionnaire_upload("questionnaire.xlsx", content)
        kwargs = {
            "owner_ref": "mapping-user",
            "filename": "questionnaire.xlsx",
            "questionnaire_content": content,
            "retrieved_at": RETRIEVED_AT,
        }

        self.assertEqual(
            map_bested_questionnaire_upload(parsed, **kwargs),
            map_bested_questionnaire_upload(parsed, **kwargs),
        )

    def test_rejects_parse_result_from_a_different_upload(self):
        content = _bested_workbook()
        parsed = parse_bested_questionnaire_upload("questionnaire.xlsx", content)

        with self.assertRaisesRegex(ValueError, "解析结果与原问卷文件"):
            map_bested_questionnaire_upload(
                parsed,
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=content + b"different-upload",
                retrieved_at=RETRIEVED_AT,
            )


if __name__ == "__main__":
    unittest.main()
