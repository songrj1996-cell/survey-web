from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from app.integrations.google_forms_responses_client import GoogleFormResponsesCapture
from app.routers.google_forms_families import create_google_forms_families_router
from app.schemas.questionnaire import CanonicalQuestionType
from app.services.google_forms_family_api import (
    GoogleFormsFamilyApi,
    GoogleFormsFamilyMappingUnavailableError,
    GoogleFormsFamilyNeedsReviewError,
    translate_family_variants_with_llm,
)
from app.services.google_forms_snapshot_api import GoogleFormsQuestionnaireSnapshotApi
from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    build_questionnaire_family,
)
from app.storage.questionnaire_families import FileQuestionnaireFamilyStorage
from app.storage.research_assets import FileResearchAssetStorage
from tests.test_google_forms_family_binding import _response
from tests.test_google_forms_questionnaire_source_api import (
    FORM_ID as CAPTURE_FORM_ID,
    _capture,
)
from tests.test_questionnaire_family_mapping import (
    OWNER,
    TITLE,
    _question,
    semantics,
    snapshot,
)


LOGIN = {"email": "owner-synthetic@example.test", "name": "Owner"}


class _Client:
    def __init__(self, captures: dict[str, GoogleFormResponsesCapture]) -> None:
        self.captures = captures
        self.response_calls: list[tuple[str, str]] = []

    async def fetch_form(self, owner_ref: str, form_id: str):
        if form_id in {CAPTURE_FORM_ID, "FORM_SECOND"}:
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

    async def test_needs_review_family_never_reads_responses(self):
        family = _family(needs_review=True)
        self.family_storage.save_family(family)
        with self.assertRaises(GoogleFormsFamilyNeedsReviewError):
            await self.api.create_analysis_session(OWNER, family.family_id, LOGIN)
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
            )
        self.assertEqual(completion.await_count, 2)
        self.assertEqual(len(translated), len(translatable))

    async def test_router_get_is_owner_scoped_and_create_body_is_bounded_contract(self):
        family = _family()
        self.family_storage.save_family(family)
        app = FastAPI()
        app.include_router(create_google_forms_families_router(self.api))
        transport = httpx.ASGITransport(app=app)
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
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(
                    f"/api/questionnaire-sources/google-forms/families/{family.family_id}"
                )
                malformed = await client.post(
                    "/api/questionnaire-sources/google-forms/families",
                    content=b'{"title":"x","title":"y","variants":[]}',
                    headers={"Content-Type": "application/json"},
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
        self.assertEqual(malformed.status_code, 422)
        self.assertEqual(mapping_unavailable.status_code, 503)
        self.assertEqual(
            mapping_unavailable.json()["detail"]["code"],
            "google_forms_family_mapping_unavailable",
        )


if __name__ == "__main__":
    unittest.main()
