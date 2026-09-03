from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
import httpx

from app.integrations.google_forms_responses_client import (
    GoogleFormResponsesCapture,
    GoogleFormsResponsesClientError,
    GoogleFormsResponsesErrorCode,
)
from app.integrations.google_forms_client import (
    GoogleFormCapture,
    GoogleFormImageCapture,
    GoogleImageContext,
)
from app.routers.google_forms_families import create_google_forms_families_router
from app.schemas.questionnaire import CanonicalQuestionType, CollectionState
from app.schemas.questionnaire_families import family_summary
from app.services.google_forms_family_api import (
    GoogleFormsFamilyApi,
    GoogleFormsFamilyInternalError,
    GoogleFormsFamilyInvalidError,
    GoogleFormsFamilyMappingUnavailableError,
    GoogleFormsFamilyNeedsReviewError,
    GoogleFormsFamilyNoResponsesError,
    GoogleFormsFamilyProviderError,
    translate_family_variants_with_llm,
)
from app.services.google_forms_snapshot_api import (
    GoogleFormsQuestionnaireAuthRequiredError,
    GoogleFormsQuestionnaireConflictError,
    GoogleFormsQuestionnaireInternalError,
    GoogleFormsQuestionnaireInvalidError,
    GoogleFormsQuestionnaireNotFoundError,
    GoogleFormsQuestionnairePermissionError,
    GoogleFormsQuestionnaireProviderError,
    GoogleFormsQuestionnaireRetryableError,
    GoogleFormsQuestionnaireSnapshotApi,
)
from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    build_questionnaire_family,
)
from app.storage.questionnaire_families import FileQuestionnaireFamilyStorage
from app.storage.research_assets import FileResearchAssetStorage
from tests.test_google_forms_family_binding import _response
from tests.test_questionnaire_family_mapping import (
    OWNER,
    TITLE,
    _question,
    semantics,
    snapshot,
)


LOGIN = {"email": "owner-synthetic@example.test", "name": "Owner"}
CAPTURE_FORM_ID = "FORM_SYNTHETIC_001"
GOOGLE_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "questionnaire_sources"
    / "google_forms_api.json"
)


def _capture() -> GoogleFormCapture:
    raw_form = json.loads(GOOGLE_FIXTURE.read_text(encoding="utf-8"))
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
                1, "item-choice", "question-choice", ("question-choice",), 0,
            ),
        ),
        (
            ("items", 1, "questionItem", "image"),
            GoogleImageContext(
                1, "item-choice", "question-choice", ("question-choice",), None,
            ),
        ),
        (
            ("items", 2, "questionGroupItem", "image"),
            GoogleImageContext(
                2,
                "item-grid",
                None,
                ("question-grid-usability", "question-grid-art"),
                None,
            ),
        ),
    )
    images = []
    for index, (path, context) in enumerate(definitions):
        content = b"\x89PNG\r\n\x1a\n" + f"google-{index}".encode()
        images.append(GoogleFormImageCapture(
            json_path=path,
            context=context,
            content=content,
            mime_type="image/png",
            sha256=hashlib.sha256(content).hexdigest(),
        ))
    return GoogleFormCapture(CAPTURE_FORM_ID, raw_form, tuple(images), ())


class _Client:
    def __init__(self, captures: dict[str, GoogleFormResponsesCapture]) -> None:
        self.captures = captures
        self.response_errors: dict[str, GoogleFormsResponsesClientError] = {}
        self.structure_calls: list[tuple[str, str]] = []
        self.response_calls: list[tuple[str, str]] = []

    async def fetch_form(self, owner_ref: str, form_id: str):
        self.structure_calls.append((owner_ref, form_id))
        if form_id in {CAPTURE_FORM_ID, "FORM_SECOND", "FORM_EN", "FORM_ID"}:
            capture = _capture()
            raw_form = dict(capture.raw_form)
            raw_form["formId"] = form_id
            return type(capture)(
                form_id,
                raw_form,
                capture.images,
                capture.image_failures,
            )
        raise AssertionError("unexpected form structure request")

    async def fetch_responses(self, owner_ref: str, form_id: str):
        self.response_calls.append((owner_ref, form_id))
        error = self.response_errors.get(form_id)
        if error is not None:
            raise error
        return self.captures[form_id]


def _family(*, needs_review: bool = False):
    en = snapshot("FORM_EN", "en", include_discord=True)
    id_form = snapshot("FORM_ID", "id", omit_open=needs_review, reorder=not needs_review)
    declared = [("en", "FORM_EN", en), ("id", "FORM_ID", id_form)]
    return build_questionnaire_family(
        owner_ref=OWNER,
        title=TITLE,
        variants=[
            FamilyVariantSnapshot(language=language, snapshot=source)
            for language, _, source in declared
        ],
        semantic_questions=semantics(declared),
    )


class GoogleFormsFamilyApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="google-family-api-")
        root = Path(self.temporary.name)
        self.snapshot_storage = FileResearchAssetStorage(root)
        self.family_storage = FileQuestionnaireFamilyStorage(root)
        captures = {
            "FORM_EN": GoogleFormResponsesCapture(
                form_id="FORM_EN",
                responses=(
                    _response("shared", "en", "Ranked", "Original English"),
                    _response("shared", "en", "Ranked", "Duplicate English"),
                ),
                page_count=1,
            ),
            "FORM_ID": GoogleFormResponsesCapture(
                form_id="FORM_ID",
                responses=(_response("shared", "id", "Peringkat", "Jawaban asli"),),
                page_count=1,
            ),
        }
        self.client = _Client(captures)
        self.snapshot_api = GoogleFormsQuestionnaireSnapshotApi(
            self.client,
            self.snapshot_storage,
        )
        self.api = GoogleFormsFamilyApi(
            client=self.client,
            snapshot_api=self.snapshot_api,
            snapshot_storage=self.snapshot_storage,
            family_storage=self.family_storage,
            semantic_translator=AsyncMock(return_value={}),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_ready_family_reads_all_variants_and_creates_one_session(self):
        family = _family()
        self.family_storage.save_family(family)
        upload = AsyncMock(return_value={
            "session_id": "12345678-1234-1234-1234-123456789012",
            "filename": f"google-forms-family-{family.family_id}.json",
            "total_rows": 2,
            "headers": ["Preferred mode", "Why?", "来源语言", "Google 回答来源"],
            "preview": [["排位", "Original English", "en", "en|var|shared"]],
            "source_type": "google",
            "questionnaire_used": True,
            "matched_questions": len(family.canonical_questions),
            "questionnaire_family_id": family.family_id,
            "languages": ["en", "id"],
            "duplicate_response_count": 1,
            "unmatched_answer_count": 0,
            "file_upload_answer_count": 0,
        })
        with patch(
            "app.services.google_forms_family_api.handle_survey_upload",
            new=upload,
        ):
            result = await self.api.create_analysis_session(
                OWNER,
                family.family_id,
                LOGIN,
            )

        self.assertEqual(result.total_rows, 2)
        self.assertEqual(result.languages, ["en", "id"])
        self.assertEqual(result.duplicate_response_count, 1)
        self.assertEqual(
            {form_id for _, form_id in self.client.response_calls},
            {"FORM_EN", "FORM_ID"},
        )
        binding = upload.await_args.kwargs["bound_questionnaire"]
        self.assertEqual(len(binding.rows), 3)
        self.assertEqual(binding.unmatched_answer_count, 0)
        self.assertEqual(
            [item.language for item in binding.response_provenance],
            ["en", "id"],
        )

    async def test_closed_forms_with_historical_responses_still_create_session(self):
        en = snapshot("FORM_EN", "en", include_discord=True).model_copy(
            update={"collection_state": CollectionState.CLOSED},
        )
        id_form = snapshot("FORM_ID", "id", reorder=True).model_copy(
            update={"collection_state": CollectionState.CLOSED},
        )
        declared = [("en", "FORM_EN", en), ("id", "FORM_ID", id_form)]
        family = build_questionnaire_family(
            owner_ref=OWNER,
            title=TITLE,
            variants=[
                FamilyVariantSnapshot(language=language, snapshot=source)
                for language, _, source in declared
            ],
            semantic_questions=semantics(declared),
        )
        self.family_storage.save_family(family)
        upload = AsyncMock(return_value={
            "session_id": "12345678-1234-1234-1234-123456789012",
            "filename": f"google-forms-family-{family.family_id}.json",
            "total_rows": 2,
            "headers": ["偏好模式", "原因", "来源语言", "Google 回答来源"],
            "preview": [["排位", "Original English", "en", "en|var|shared"]],
            "source_type": "google",
            "questionnaire_used": True,
            "matched_questions": len(family.canonical_questions),
            "questionnaire_family_id": family.family_id,
            "languages": ["en", "id"],
            "duplicate_response_count": 1,
            "unmatched_answer_count": 0,
            "file_upload_answer_count": 0,
        })

        with patch(
            "app.services.google_forms_family_api.handle_survey_upload",
            new=upload,
        ):
            result = await self.api.create_analysis_session(
                OWNER,
                family.family_id,
                LOGIN,
            )

        self.assertEqual(result.total_rows, 2)
        upload.assert_awaited_once()

    async def test_single_form_internal_milestone_builds_and_persists_family(self):
        summary = await self.api.create_family(
            OWNER,
            "Single form milestone",
            [("en", CAPTURE_FORM_ID)],
        )
        self.assertEqual(summary.status.value, "ready")
        self.assertEqual(summary.variant_count, 1)
        self.assertGreater(summary.canonical_question_count, 0)
        loaded = self.family_storage.load_family(OWNER, summary.family_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.variants[0].provider_form_id, CAPTURE_FORM_ID)

    async def test_empty_family_has_distinct_no_responses_error(self):
        family = _family()
        self.family_storage.save_family(family)
        self.client.captures = {
            form_id: GoogleFormResponsesCapture(
                form_id=form_id,
                responses=(),
                page_count=1,
            )
            for form_id in ("FORM_EN", "FORM_ID")
        }

        with self.assertRaises(GoogleFormsFamilyNoResponsesError):
            await self.api.create_analysis_session(OWNER, family.family_id, LOGIN)

    async def test_response_provider_errors_have_stable_family_contract(self):
        family = _family()
        self.family_storage.save_family(family)
        cases = (
            (
                GoogleFormsResponsesErrorCode.AUTHORIZATION_FAILED,
                None,
                False,
                401,
                "google_forms_provider_authentication_failed",
            ),
            (
                GoogleFormsResponsesErrorCode.PERMISSION_DENIED,
                403,
                False,
                403,
                "google_forms_provider_permission_denied",
            ),
            (
                GoogleFormsResponsesErrorCode.FORM_NOT_FOUND,
                404,
                False,
                404,
                "google_forms_provider_form_not_found",
            ),
            (
                GoogleFormsResponsesErrorCode.RATE_LIMITED,
                429,
                True,
                429,
                "google_forms_provider_rate_limited",
            ),
            (
                GoogleFormsResponsesErrorCode.PROVIDER_UNAVAILABLE,
                500,
                True,
                503,
                "google_forms_provider_unavailable",
            ),
        )
        for source_code, source_status, retryable, status, code in cases:
            with self.subTest(code=source_code):
                self.client.response_errors = {
                    "FORM_EN": GoogleFormsResponsesClientError(
                        source_code,
                        "provider detail must stay private",
                        retryable=retryable,
                        status_code=source_status,
                    )
                }
                with self.assertRaises(GoogleFormsFamilyProviderError) as raised:
                    await self.api.create_analysis_session(
                        OWNER,
                        family.family_id,
                        LOGIN,
                    )
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(raised.exception.code, code)

    async def test_structure_rate_limit_keeps_distinct_retryable_code(self):
        provider_cause = RuntimeError("private provider detail")
        provider_cause.status_code = 429
        retryable = GoogleFormsQuestionnaireRetryableError()
        retryable.__cause__ = provider_cause
        with patch.object(
            GoogleFormsQuestionnaireSnapshotApi,
            "import_questionnaire",
            new=AsyncMock(side_effect=retryable),
        ):
            with self.assertRaises(GoogleFormsFamilyProviderError) as raised:
                await self.api.create_family(
                    OWNER,
                    "Rate limited structure",
                    [("en", CAPTURE_FORM_ID)],
                )
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(
            raised.exception.code,
            "google_forms_provider_rate_limited",
        )

    async def test_structure_snapshot_errors_keep_family_error_boundary(self):
        cases = (
            (
                GoogleFormsQuestionnaireInvalidError(),
                GoogleFormsFamilyInvalidError,
                None,
            ),
            (
                GoogleFormsQuestionnaireConflictError(),
                GoogleFormsFamilyInternalError,
                None,
            ),
            (
                GoogleFormsQuestionnaireInternalError(),
                GoogleFormsFamilyInternalError,
                None,
            ),
            (
                GoogleFormsQuestionnaireProviderError(),
                GoogleFormsFamilyProviderError,
                "google_forms_provider_error",
            ),
        )
        for source_error, family_error, expected_code in cases:
            with self.subTest(source_error=type(source_error).__name__):
                with patch.object(
                    GoogleFormsQuestionnaireSnapshotApi,
                    "import_questionnaire",
                    new=AsyncMock(side_effect=source_error),
                ):
                    with self.assertRaises(family_error) as raised:
                        await self.api.create_family(
                            OWNER,
                            "Structure boundary",
                            [("en", CAPTURE_FORM_ID)],
                        )
                if expected_code is not None:
                    self.assertEqual(raised.exception.code, expected_code)
                    self.assertEqual(raised.exception.status_code, 502)

    async def test_needs_review_family_never_reads_responses(self):
        family = _family(needs_review=True)
        self.family_storage.save_family(family)
        with self.assertRaises(GoogleFormsFamilyNeedsReviewError):
            await self.api.create_analysis_session(OWNER, family.family_id, LOGIN)
        self.assertEqual(self.client.response_calls, [])

    async def test_catalog_is_owner_scoped_safe_and_sorted(self):
        older = _family()
        newer = older.model_copy(update={
            "family_id": f"{older.family_id}-newer",
            "title": "Newer project",
            "updated_at": datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
        })
        older = older.model_copy(update={
            "updated_at": datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc),
        })
        other_owner = older.model_copy(update={
            "owner_ref": "other-owner",
            "family_id": "other-family",
            "title": "Other owner project",
        })
        for family in (older, newer, other_owner):
            self.family_storage.save_family(family)

        result = await self.api.list_families(OWNER, limit=1)
        self.assertEqual([item.title for item in result.items], ["Newer project"])
        self.assertIsNotNone(result.next_cursor)
        payload = result.model_dump(mode="json")
        serialized = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("owner_ref", "provider_form_id", "snapshot_id", "diagnostics"):
            self.assertNotIn(forbidden, serialized)
        second = await self.api.list_families(
            OWNER,
            cursor=result.next_cursor,
            limit=1,
        )
        self.assertEqual([item.title for item in second.items], [TITLE])

    async def test_refresh_uses_saved_structure_and_never_reads_responses(self):
        summary = await self.api.create_family(
            OWNER,
            "Refreshable survey",
            [("en", CAPTURE_FORM_ID)],
        )
        stored = self.family_storage.load_family(OWNER, summary.family_id)
        self.assertIsNotNone(stored)
        created_at = stored.created_at
        old_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.family_storage.save_family(stored.model_copy(update={"updated_at": old_time}))
        self.client.structure_calls.clear()
        self.api.semantic_translator.reset_mock()

        refreshed = await self.api.refresh_family(
            OWNER,
            summary.family_id,
            llm_api_key="request-scoped-key",
        )

        self.assertEqual(refreshed.family_id, summary.family_id)
        self.assertGreater(refreshed.updated_at, old_time)
        reloaded = self.family_storage.load_family(OWNER, summary.family_id)
        self.assertEqual(reloaded.created_at, created_at)
        self.assertEqual(
            self.client.structure_calls,
            [(OWNER, CAPTURE_FORM_ID)],
        )
        self.assertEqual(self.client.response_calls, [])
        self.api.semantic_translator.assert_awaited_once()
        self.assertEqual(
            self.api.semantic_translator.await_args.args[-1],
            "request-scoped-key",
        )

    async def test_failed_refresh_preserves_previous_family(self):
        family = _family()
        self.family_storage.save_family(family)
        failing_api = GoogleFormsFamilyApi(
            client=self.client,
            snapshot_api=self.snapshot_api,
            snapshot_storage=self.snapshot_storage,
            family_storage=self.family_storage,
            semantic_translator=AsyncMock(
                side_effect=GoogleFormsFamilyMappingUnavailableError()
            ),
        )
        with self.assertRaises(GoogleFormsFamilyMappingUnavailableError):
            await failing_api.refresh_family(OWNER, family.family_id)
        self.assertEqual(
            self.family_storage.load_family(OWNER, family.family_id),
            family,
        )
        self.assertEqual(self.client.response_calls, [])

    async def test_incomplete_multilingual_translation_is_retryable_not_review(self):
        with self.assertRaises(GoogleFormsFamilyMappingUnavailableError):
            await self.api.create_family(
                OWNER,
                "Translation unavailable",
                [("en", CAPTURE_FORM_ID), ("id", "FORM_SECOND")],
            )
        saved = list(
            (Path(self.temporary.name) / "questionnaire_families").rglob("*.json")
        )
        self.assertEqual(saved, [])

    async def test_default_translator_repairs_invalid_json_once(self):
        source = snapshot("FORM_EN", "en")
        source = source.model_copy(update={
            "canonical_questions": [
                *source.canonical_questions,
                _question(
                    "en-section",
                    None,
                    "",
                    CanonicalQuestionType.SECTION,
                ),
            ],
            "question_count": source.question_count + 1,
        })
        translatable = [
            question
            for question in source.canonical_questions
            if question.canonical_type not in {
                CanonicalQuestionType.SECTION,
                CanonicalQuestionType.STATIC_TEXT,
            }
        ]
        repaired = {
            "translations": [
                {
                    "question_id": question.question_id,
                    "name_zh": f"中文题目 {index}",
                    "options_zh": [
                        f"中文选项 {index}-{option_index}"
                        for option_index, _ in enumerate(question.options)
                    ],
                    "rows_zh": [
                        f"中文矩阵行 {index}-{row_index}"
                        for row_index, _ in enumerate(question.rows)
                    ],
                }
                for index, question in enumerate(translatable)
            ]
        }
        completion = AsyncMock(side_effect=[
            ('{"translations": []}', "model-a"),
            (json.dumps(repaired, ensure_ascii=False), "model-b"),
        ])
        with patch(
            "app.services.google_forms_family_api.collect_chat_completion",
            new=completion,
        ):
            translated = await translate_family_variants_with_llm(
                OWNER,
                TITLE,
                [FamilyVariantSnapshot(language="en", snapshot=source)],
                "request-scoped-key",
            )
        self.assertEqual(completion.await_count, 2)
        self.assertEqual(len(translated), len(translatable))
        self.assertTrue(all(
            call.kwargs["api_key"] == "request-scoped-key"
            for call in completion.await_args_list
        ))

    async def test_router_get_is_owner_scoped_and_create_body_is_bounded_contract(self):
        family = _family()
        self.family_storage.save_family(family)
        refreshable = await self.api.create_family(
            OWNER,
            "Router refresh survey",
            [("en", CAPTURE_FORM_ID)],
        )
        app = FastAPI()
        app.include_router(create_google_forms_families_router(self.api))
        transport = httpx.ASGITransport(app=app)
        request_key = AsyncMock(return_value="request-scoped-key")
        with (
            patch(
                "app.routers.google_forms_families._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.google_forms_families.require_request_llm_api_key",
                new=request_key,
            ),
            patch(
                "app.routers.google_forms_families._owner_key",
                return_value=OWNER,
            ),
        ):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    f"/api/questionnaire-sources/google-forms/families/{family.family_id}"
                )
                catalog = await client.get(
                    "/api/questionnaire-sources/google-forms/families?limit=20"
                )
                invalid_catalog = await client.get(
                    "/api/questionnaire-sources/google-forms/families?cursor=***"
                )
                refreshed = await client.post(
                    "/api/questionnaire-sources/google-forms/families/"
                    f"{refreshable.family_id}/refresh"
                )
                malformed = await client.post(
                    "/api/questionnaire-sources/google-forms/families",
                    content=b'{"title":"x","title":"y","variants":[]}',
                    headers={"Content-Type": "application/json"},
                )
                single_form = await client.post(
                    "/api/questionnaire-sources/google-forms/families",
                    json={
                        "title": "Single Form over HTTP",
                        "variants": [
                            {
                                "language": "en",
                                "form_url": (
                                    "https://docs.google.com/forms/d/"
                                    f"{CAPTURE_FORM_ID}/edit"
                                ),
                            },
                        ],
                    },
                )
                mapping_unavailable = await client.post(
                    "/api/questionnaire-sources/google-forms/families",
                    json={
                        "title": "Retryable mapping",
                        "variants": [
                            {
                                "language": "en",
                                "form_url": (
                                    "https://docs.google.com/forms/d/"
                                    f"{CAPTURE_FORM_ID}/edit"
                                ),
                            },
                            {
                                "language": "id",
                                "form_url": (
                                    "https://docs.google.com/forms/d/"
                                    "FORM_SECOND/edit"
                                ),
                            },
                        ],
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["languages"], ["en", "id"])
        self.assertNotIn("owner_ref", response.text)
        self.assertEqual(catalog.status_code, 200)
        self.assertGreaterEqual(len(catalog.json()["items"]), 2)
        self.assertNotIn("provider_form_id", catalog.text)
        self.assertEqual(invalid_catalog.status_code, 422)
        self.assertEqual(
            invalid_catalog.json()["detail"]["code"],
            "google_forms_family_invalid_catalog_query",
        )
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(refreshed.json()["family_id"], refreshable.family_id)
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(
            malformed.json()["detail"]["code"],
            "google_forms_family_invalid_request",
        )
        self.assertEqual(single_form.status_code, 200, single_form.text)
        self.assertEqual(single_form.json()["variant_count"], 1)
        self.assertEqual(mapping_unavailable.status_code, 503)
        self.assertEqual(
            mapping_unavailable.json()["detail"]["code"],
            "google_forms_family_mapping_unavailable",
        )
        request_key.assert_awaited_once()
        self.assertEqual(
            self.api.semantic_translator.await_args.args[-1],
            "request-scoped-key",
        )

    async def test_router_create_request_boundaries_have_stable_error_codes(self):
        app = FastAPI()
        app.include_router(create_google_forms_families_router(self.api))
        transport = httpx.ASGITransport(app=app)
        languages = [
            "en",
            "id",
            "ms",
            "th",
            "vi",
            "tl",
            "my",
            "km",
            "lo",
            "zh-cn",
            "ja",
        ]

        def variants(count: int) -> list[dict[str, str]]:
            return [
                {
                    "language": languages[index],
                    "form_url": (
                        "https://docs.google.com/forms/d/"
                        f"FORM_{index:02d}/edit"
                    ),
                }
                for index in range(count)
            ]

        create = AsyncMock(return_value=family_summary(_family()))
        request_key = AsyncMock(return_value="request-scoped-key")
        with (
            patch(
                "app.routers.google_forms_families._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.google_forms_families.require_request_llm_api_key",
                new=request_key,
            ),
            patch(
                "app.routers.google_forms_families._owner_key",
                return_value=OWNER,
            ),
            patch(
                "app.services.google_forms_family_api."
                "GoogleFormsFamilyApi.create_family",
                new=create,
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                endpoint = "/api/questionnaire-sources/google-forms/families"
                one = await client.post(
                    endpoint,
                    json={"title": "One", "variants": variants(1)},
                )
                ten = await client.post(
                    endpoint,
                    json={"title": "Ten", "variants": variants(10)},
                )
                zero = await client.post(
                    endpoint,
                    json={"title": "Zero", "variants": []},
                )
                eleven = await client.post(
                    endpoint,
                    json={"title": "Eleven", "variants": variants(11)},
                )
                duplicate_language = await client.post(
                    endpoint,
                    json={
                        "title": "Duplicate language",
                        "variants": [
                            variants(2)[0],
                            {**variants(2)[1], "language": "en"},
                        ],
                    },
                )
                duplicate_url = await client.post(
                    endpoint,
                    json={
                        "title": "Duplicate URL",
                        "variants": [
                            variants(2)[0],
                            {**variants(2)[1], "form_url": variants(2)[0]["form_url"]},
                        ],
                    },
                )
                unsupported = await client.post(
                    endpoint,
                    content=b"not json",
                    headers={"Content-Type": "text/plain"},
                )
                oversized = await client.post(
                    endpoint,
                    content=b"x" * (32 * 1024 + 1),
                    headers={"Content-Type": "application/json"},
                )

        self.assertEqual(one.status_code, 200, one.text)
        self.assertEqual(ten.status_code, 200, ten.text)
        self.assertEqual(create.await_count, 2)
        request_key.assert_awaited_once()
        for response in (
            zero,
            eleven,
            duplicate_language,
            duplicate_url,
        ):
            with self.subTest(response=response):
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(
                    response.json()["detail"]["code"],
                    "google_forms_family_invalid_request",
                )
        self.assertEqual(unsupported.status_code, 415, unsupported.text)
        self.assertEqual(
            unsupported.json()["detail"]["code"],
            "google_forms_family_unsupported_media_type",
        )
        self.assertEqual(oversized.status_code, 413, oversized.text)
        self.assertEqual(
            oversized.json()["detail"]["code"],
            "google_forms_family_request_too_large",
        )

    async def test_router_preserves_missing_personal_llm_key_contract(self):
        family = _family()
        self.family_storage.save_family(family)
        app = FastAPI()
        app.include_router(create_google_forms_families_router(self.api))
        transport = httpx.ASGITransport(app=app)
        missing_key = AsyncMock(side_effect=HTTPException(
            status_code=428,
            detail={
                "code": "USER_LLM_KEY_REQUIRED",
                "message": "请先在个人中心填写 LLM API Key",
            },
        ))
        with (
            patch(
                "app.routers.google_forms_families._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.google_forms_families._owner_key",
                return_value=OWNER,
            ),
            patch(
                "app.routers.google_forms_families.require_request_llm_api_key",
                new=missing_key,
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                created = await client.post(
                    "/api/questionnaire-sources/google-forms/families",
                    json={
                        "title": "Needs personal key",
                        "variants": [
                            {
                                "language": "en",
                                "form_url": (
                                    "https://docs.google.com/forms/d/FORM_EN/edit"
                                ),
                            },
                            {
                                "language": "id",
                                "form_url": (
                                    "https://docs.google.com/forms/d/FORM_ID/edit"
                                ),
                            },
                        ],
                    },
                )
                refreshed = await client.post(
                    "/api/questionnaire-sources/google-forms/families/"
                    f"{family.family_id}/refresh"
                )

        for response in (created, refreshed):
            self.assertEqual(response.status_code, 428, response.text)
            self.assertEqual(
                response.json()["detail"]["code"],
                "USER_LLM_KEY_REQUIRED",
            )
        self.assertEqual(missing_key.await_count, 2)

    async def test_router_structure_errors_have_stable_safe_codes(self):
        app = FastAPI()
        app.include_router(create_google_forms_families_router(self.api))
        transport = httpx.ASGITransport(app=app)
        rate_limited = GoogleFormsQuestionnaireRetryableError()
        provider_cause = RuntimeError("private rate limit detail")
        provider_cause.status_code = 429
        rate_limited.__cause__ = provider_cause
        cases = (
            (
                GoogleFormsQuestionnaireInvalidError(),
                422,
                "google_forms_family_invalid",
            ),
            (
                GoogleFormsQuestionnaireAuthRequiredError(),
                401,
                "google_forms_provider_authentication_failed",
            ),
            (
                GoogleFormsQuestionnairePermissionError(),
                403,
                "google_forms_provider_permission_denied",
            ),
            (
                GoogleFormsQuestionnaireNotFoundError(),
                404,
                "google_forms_provider_form_not_found",
            ),
            (
                rate_limited,
                429,
                "google_forms_provider_rate_limited",
            ),
            (
                GoogleFormsQuestionnaireRetryableError(),
                503,
                "google_forms_provider_unavailable",
            ),
            (
                GoogleFormsQuestionnaireProviderError(),
                502,
                "google_forms_provider_error",
            ),
            (
                GoogleFormsQuestionnaireConflictError(),
                500,
                "google_forms_family_internal_error",
            ),
            (
                GoogleFormsQuestionnaireInternalError(),
                500,
                "google_forms_family_internal_error",
            ),
        )
        with (
            patch(
                "app.routers.google_forms_families._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.google_forms_families._owner_key",
                return_value=OWNER,
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                for source_error, status, code in cases:
                    with self.subTest(source_error=type(source_error).__name__):
                        with patch.object(
                            GoogleFormsQuestionnaireSnapshotApi,
                            "import_questionnaire",
                            new=AsyncMock(side_effect=source_error),
                        ):
                            response = await client.post(
                                "/api/questionnaire-sources/google-forms/families",
                                json={
                                    "title": "Structure error",
                                    "variants": [{
                                        "language": "en",
                                        "form_url": (
                                            "https://docs.google.com/forms/d/"
                                            f"{CAPTURE_FORM_ID}/edit"
                                        ),
                                    }],
                                },
                            )
                    self.assertEqual(response.status_code, status, response.text)
                    self.assertEqual(response.json()["detail"]["code"], code)
                    self.assertNotIn("private rate limit detail", response.text)

    async def test_router_analysis_errors_use_safe_stable_codes(self):
        family = _family()
        self.family_storage.save_family(family)
        app = FastAPI()
        app.include_router(create_google_forms_families_router(self.api))
        transport = httpx.ASGITransport(app=app)
        cases = (
            (
                GoogleFormsResponsesErrorCode.PERMISSION_DENIED,
                403,
                False,
                403,
                "google_forms_provider_permission_denied",
            ),
            (
                GoogleFormsResponsesErrorCode.FORM_NOT_FOUND,
                404,
                False,
                404,
                "google_forms_provider_form_not_found",
            ),
            (
                GoogleFormsResponsesErrorCode.RATE_LIMITED,
                429,
                True,
                429,
                "google_forms_provider_rate_limited",
            ),
            (
                GoogleFormsResponsesErrorCode.PROVIDER_UNAVAILABLE,
                503,
                True,
                503,
                "google_forms_provider_unavailable",
            ),
        )
        endpoint = (
            "/api/questionnaire-sources/google-forms/families/"
            f"{family.family_id}/analysis-sessions"
        )
        with (
            patch(
                "app.routers.google_forms_families._require_feature",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch(
                "app.routers.google_forms_families._owner_key",
                return_value=OWNER,
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                for source_code, source_status, retryable, status, code in cases:
                    with self.subTest(code=source_code):
                        self.client.response_errors = {
                            "FORM_EN": GoogleFormsResponsesClientError(
                                source_code,
                                "private provider detail FORM_EN",
                                retryable=retryable,
                                status_code=source_status,
                            )
                        }
                        response = await client.post(endpoint)
                        self.assertEqual(response.status_code, status, response.text)
                        self.assertEqual(response.json()["detail"]["code"], code)
                        self.assertNotIn("private provider detail", response.text)
                        self.assertNotIn("FORM_EN", response.text)

                self.client.response_errors = {}
                self.client.captures = {
                    form_id: GoogleFormResponsesCapture(
                        form_id=form_id,
                        responses=(),
                        page_count=1,
                    )
                    for form_id in ("FORM_EN", "FORM_ID")
                }
                no_responses = await client.post(endpoint)
                self.assertEqual(no_responses.status_code, 409, no_responses.text)
                self.assertEqual(
                    no_responses.json()["detail"]["code"],
                    "google_forms_family_no_responses",
                )

                needs_review = _family(needs_review=True)
                self.family_storage.save_family(needs_review)
                review_response = await client.post(endpoint)
                self.assertEqual(review_response.status_code, 409)
                self.assertEqual(
                    review_response.json()["detail"]["code"],
                    "google_forms_family_needs_review",
                )
                self.assertIn(
                    "修改原 Form",
                    review_response.json()["detail"]["message"],
                )

                not_found = await client.post(
                    "/api/questionnaire-sources/google-forms/families/"
                    "missing-family/analysis-sessions"
                )
                self.assertEqual(not_found.status_code, 404)
                self.assertEqual(
                    not_found.json()["detail"]["code"],
                    "google_forms_family_not_found",
                )


if __name__ == "__main__":
    unittest.main()
