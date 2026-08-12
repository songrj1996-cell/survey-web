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
    BestedQuestionnaireHyperlink,
    BestedQuestionnaireImage,
    BestedQuestionnaireMediaIssue,
    BestedQuestionnaireParseResult,
    parse_bested_questionnaire_upload,
)
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

    def test_media_issue_marks_source_and_document_partial(self):
        content = _bested_workbook()
        parsed = parse_bested_questionnaire_upload("questionnaire.xlsx", content)
        partial = BestedQuestionnaireMediaIssue(
            code="image_extraction_failed",
            sheet_name=parsed.sheet_name,
            source_cell="C3",
            source_row=3,
        )
        parsed = BestedQuestionnaireParseResult(
            content_sha256=parsed.content_sha256,
            sheet_name=parsed.sheet_name,
            provider_rows=parsed.provider_rows,
            questions=parsed.questions,
            questionnaire_text=parsed.questionnaire_text,
            images=(),
            hyperlinks=parsed.hyperlinks,
            media_issues=(partial,),
        )

        result = map_bested_questionnaire_upload(
            parsed,
            owner_ref="mapping-user",
            filename="questionnaire.xlsx",
            questionnaire_content=content,
            retrieved_at=RETRIEVED_AT,
        )

        validate_research_contract(result.bundle.snapshot, result.bundle.collection)
        self.assertEqual(
            result.bundle.collection.sources[0].acquisition_status.value,
            "partial",
        )
        self.assertEqual(
            result.bundle.collection.documents[0].parse_status.value,
            "partial",
        )
        self.assertEqual(
            result.bundle.collection.documents[0].warnings[0].code,
            "bested_image_extraction_failed",
        )
        warning = next(
            item for item in result.bundle.snapshot.warnings
            if item.code == "bested_image_extraction_failed"
        )
        self.assertEqual(warning.source_locator.cell, "C3")
        self.assertNotIn("exception", warning.message.casefold())

    def test_rejects_unknown_or_cross_sheet_bested_media_issue(self):
        content = _bested_workbook()
        parsed = parse_bested_questionnaire_upload("questionnaire.xlsx", content)
        base = {
            "content_sha256": parsed.content_sha256,
            "sheet_name": parsed.sheet_name,
            "provider_rows": parsed.provider_rows,
            "questions": parsed.questions,
            "questionnaire_text": parsed.questionnaire_text,
            "images": parsed.images,
            "hyperlinks": parsed.hyperlinks,
        }
        for issue, error in (
            (BestedQuestionnaireMediaIssue(code="unknown"), "未知错误代码"),
            (
                BestedQuestionnaireMediaIssue(
                    code="image_extraction_failed",
                    sheet_name="其他工作表",
                ),
                "其他工作表",
            ),
            (
                BestedQuestionnaireMediaIssue(
                    code="image_extraction_failed",
                    sheet_name=parsed.sheet_name,
                    source_cell="C4",
                    source_row=3,
                ),
                "单元格与来源行",
            ),
        ):
            with self.subTest(issue=issue):
                with self.assertRaisesRegex(ValueError, error):
                    map_bested_questionnaire_upload(
                        BestedQuestionnaireParseResult(
                            **base,
                            media_issues=(issue,),
                        ),
                        owner_ref="mapping-user",
                        filename="questionnaire.xlsx",
                        questionnaire_content=content,
                        retrieved_at=RETRIEVED_AT,
                    )

    def test_rejects_forged_bested_media_closure_and_question_binding(self):
        content = _bested_workbook()
        parsed = parse_bested_questionnaire_upload("questionnaire.xlsx", content)
        base = {
            "content_sha256": parsed.content_sha256,
            "sheet_name": parsed.sheet_name,
            "provider_rows": parsed.provider_rows,
            "questions": parsed.questions,
            "questionnaire_text": parsed.questionnaire_text,
            "hyperlinks": parsed.hyperlinks,
        }

        missing_image = BestedQuestionnaireParseResult(
            **base,
            images=(),
            media_issues=(),
        )
        with self.assertRaisesRegex(ValueError, "OOXML 声明数量"):
            map_bested_questionnaire_upload(
                missing_image,
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=content,
                retrieved_at=RETRIEVED_AT,
            )

        image = parsed.images[0]
        forged_image = BestedQuestionnaireImage(
            content=image.content,
            mime_type=image.mime_type,
            sheet_name=image.sheet_name,
            source_cell=image.source_cell,
            source_row=image.source_row,
            coverage=image.coverage,
            question_qid=2,
        )
        with self.assertRaisesRegex(ValueError, "题目归属与来源行"):
            map_bested_questionnaire_upload(
                BestedQuestionnaireParseResult(
                    **base,
                    images=(forged_image,),
                    media_issues=(),
                ),
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=content,
                retrieved_at=RETRIEVED_AT,
            )

        forged_coverage = BestedQuestionnaireImage(
            content=image.content,
            mime_type=image.mime_type,
            sheet_name=image.sheet_name,
            source_cell=image.source_cell,
            source_row=image.source_row,
            coverage="C3:C999",
            question_qid=image.question_qid,
        )
        with self.assertRaisesRegex(ValueError, "题目归属与来源行"):
            map_bested_questionnaire_upload(
                BestedQuestionnaireParseResult(
                    **base,
                    images=(forged_coverage,),
                    media_issues=(),
                ),
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=content,
                retrieved_at=RETRIEVED_AT,
            )

        workbook_failure = BestedQuestionnaireParseResult(
            **base,
            images=parsed.images,
            media_issues=(BestedQuestionnaireMediaIssue(
                code="media_workbook_load_failed",
                sheet_name=parsed.sheet_name,
            ),),
        )
        with self.assertRaisesRegex(ValueError, "加载失败记录与解析结果矛盾"):
            map_bested_questionnaire_upload(
                workbook_failure,
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=content,
                retrieved_at=RETRIEVED_AT,
            )

        hyperlink = parsed.hyperlinks[0]
        forged_hyperlink = BestedQuestionnaireHyperlink(
            url=hyperlink.url,
            display_text=hyperlink.display_text,
            sheet_name=hyperlink.sheet_name,
            source_cell=hyperlink.source_cell,
            source_row=hyperlink.source_row,
            question_qid=2,
        )
        with self.assertRaisesRegex(ValueError, "超链接的题目归属"):
            map_bested_questionnaire_upload(
                BestedQuestionnaireParseResult(
                    **{
                        **base,
                        "hyperlinks": (
                            forged_hyperlink,
                            *parsed.hyperlinks[1:],
                        ),
                    },
                    images=parsed.images,
                    media_issues=(),
                ),
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=content,
                retrieved_at=RETRIEVED_AT,
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

    def test_mapping_preflights_same_hash_invalid_bested_package(self):
        invalid_content = b"not a zip package"
        parsed = BestedQuestionnaireParseResult(
            content_sha256=hashlib.sha256(invalid_content).hexdigest(),
            sheet_name="问卷内容",
            provider_rows=(("题号", "题目"),),
            questions=(),
            questionnaire_text="题号 | 题目",
        )

        with self.assertRaisesRegex(ValueError, "不是有效"):
            map_bested_questionnaire_upload(
                parsed,
                owner_ref="mapping-user",
                filename="questionnaire.xlsx",
                questionnaire_content=invalid_content,
                retrieved_at=RETRIEVED_AT,
            )


if __name__ == "__main__":
    unittest.main()
