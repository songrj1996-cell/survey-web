from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx
import survey_stats

from app.integrations.google_forms_responses_client import GoogleFormResponsesCapture
from app.routers import survey
from app.routers.google_forms_families import create_google_forms_families_router
from app.services import report_history
from app.services.google_forms_family_api import GoogleFormsFamilyApi
from app.services.google_forms_snapshot_api import GoogleFormsQuestionnaireSnapshotApi
from app.services.questionnaire_family_mapping import (
    FamilyVariantSnapshot,
    build_questionnaire_family,
)
from app.services.report_engine import _batch_qualitative_analysis
from app.services.survey_service import columns_require_llm
from app.storage.questionnaire_families import FileQuestionnaireFamilyStorage
from app.storage.research_assets import FileResearchAssetStorage
from app.storage.sessions import get_session
from tests.test_google_forms_family_api import LOGIN, OWNER, _Client
from tests.test_google_forms_family_binding import _response
from tests.test_questionnaire_family_mapping import TITLE, semantics, snapshot


class GoogleFormsFamilyAnalysisFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="google-family-analysis-flow-",
        )
        root = Path(self.temporary.name)
        self.session_dir = root / "sessions"
        self.snapshot_storage = FileResearchAssetStorage(root / "research-assets")
        self.family_storage = FileQuestionnaireFamilyStorage(
            self.snapshot_storage.root,
        )
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
                responses=(
                    _response("shared", "id", "Mode khusus", "Jawaban asli"),
                ),
                page_count=1,
            ),
        }
        self.client = _Client(captures)
        snapshot_api = GoogleFormsQuestionnaireSnapshotApi(
            self.client,
            self.snapshot_storage,
        )
        self.api = GoogleFormsFamilyApi(
            client=self.client,
            snapshot_api=snapshot_api,
            snapshot_storage=self.snapshot_storage,
            family_storage=self.family_storage,
            semantic_translator=AsyncMock(return_value={}),
        )
        en = snapshot("FORM_EN", "en", include_discord=True, include_other=True)
        id_form = snapshot(
            "FORM_ID", "id", reorder=True, include_other=True
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
        self.family = family

        self.app = FastAPI()
        self.app.include_router(create_google_forms_families_router(self.api))
        self.app.include_router(survey.router)

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_family_endpoint_persists_real_deterministic_survey_session(self):
        transport = httpx.ASGITransport(app=self.app)
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
                "app.routers.survey._current_login",
                new=AsyncMock(return_value=LOGIN),
            ),
            patch("app.storage.sessions._SESSION_DIR", self.session_dir),
            patch(
                "app.services.survey_service.audit_log",
                new=AsyncMock(),
            ),
            patch(
                "app.routers.survey.require_request_llm_api_key",
                new=AsyncMock(return_value="synthetic-test-key"),
            ),
        ):
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://test",
            ) as client:
                created = await client.post(
                    "/api/questionnaire-sources/google-forms/families/"
                    f"{self.family.family_id}/analysis-sessions",
                )

                self.assertEqual(created.status_code, 200, created.text)
                payload = created.json()
                session_id = payload["session_id"]
                stored = get_session(session_id)

                columns = await client.get(f"/api/columns/{session_id}")
                requires_llm = columns_require_llm(session_id)

        self.assertEqual(columns.status_code, 200, columns.text)
        events = [
            json.loads(line.removeprefix("data: "))
            for line in columns.text.splitlines()
            if line.startswith("data: ")
        ]
        ready = next(item for item in events if item.get("type") == "columns_ready")

        self.assertEqual(payload["total_rows"], 2)
        self.assertEqual(payload["languages"], ["en", "id"])
        self.assertEqual(payload["duplicate_response_count"], 1)
        self.assertEqual(stored["column_provider"], "questionnaire")
        self.assertEqual(ready["columns"], stored["columns_detected"])
        self.assertFalse(requires_llm)
        self.assertEqual(
            stored["questionnaire_family_ref"]["family_id"],
            self.family.family_id,
        )
        self.assertEqual(
            stored["questionnaire_family_ref"]["languages"],
            ["en", "id"],
        )
        provenance = stored["google_forms_response_provenance"]
        self.assertEqual(len(provenance), 2)
        self.assertEqual([item["language"] for item in provenance], ["en", "id"])
        self.assertEqual(
            {item["response_id"] for item in provenance},
            {"shared"},
        )
        self.assertEqual(
            {item["provider_form_id"] for item in provenance},
            {"FORM_EN", "FORM_ID"},
        )
        self.assertTrue(
            all(item["answers"] for item in provenance),
        )
        self.assertTrue(
            all(
                answer["original_question_id"]
                for item in provenance
                for answer in item["answers"]
            ),
        )

        choice_column = next(
            item
            for item in stored["columns_detected"]
            if item["role"] == "single_choice"
        )
        self.assertEqual(choice_column["other_text"]["count"], 1)
        self.assertEqual(choice_column["other_text"]["values"], ["Mode khusus"])
        choice_index = choice_column["column_indexes"][0]
        choice_plan = {
            "columns": [{
                "index": choice_index,
                "name": choice_column["name_zh"],
                "role": choice_column["role"],
                "options": choice_column["options"],
                "other_text": choice_column["other_text"],
            }],
            "parts": [{"name": "Choice", "column_indexes": [choice_index]}],
        }
        choice_stats, choice_open_text = survey_stats.compute(
            stored["rows"], choice_plan
        )
        self.assertIn("Other / 其他", choice_stats)
        self.assertEqual(
            [item["text"] for item in choice_open_text[choice_index]],
            ["Mode khusus"],
        )
        self.assertEqual(
            choice_open_text[choice_index][0]["source"],
            "choice_other_text",
        )

        plan = {
            "columns": [
                {"index": 1, "name": "原因", "role": "open_text"},
                {"index": 5, "name": "来源语言", "role": "profile_dim"},
                {"index": 6, "name": "Google 回答来源", "role": "id"},
            ],
            "parts": [{"name": "Feedback", "column_indexes": [1]}],
        }
        open_text = survey_stats.collect_open_text(stored["rows"], plan)
        self.assertEqual(
            [item["text"] for item in open_text[1]],
            ["Original English", "Jawaban asli"],
        )

        direct = AsyncMock(side_effect=[
            {
                "data": {"themes": [{
                    "id": "theme-1",
                    "name": "Experience",
                    "description": "Experience feedback",
                    "representative_quotes": ["Original English"],
                }]},
                "model": "synthetic",
            },
            {
                "data": {"themes": [{
                    "id": "theme-1",
                    "name": "Experience",
                    "description": "Experience feedback",
                    "representative_quotes": ["Original English"],
                    "positive_summary": "",
                    "negative_summary": "",
                }]},
                "model": "synthetic",
            },
        ])
        classify = AsyncMock(return_value={
            "classifications": [
                {
                    "response_id": "0",
                    "assignments": [{"theme_id": "theme-1", "sentiment": "neutral"}],
                },
                {
                    "response_id": "1",
                    "assignments": [{"theme_id": "theme-1", "sentiment": "neutral"}],
                },
            ],
            "model": "synthetic",
            "repair_model": "",
            "repaired_count": 0,
            "fallback_count": 0,
        })
        qualitative = None
        with (
            patch("app.services.report_engine._direct_json_call", new=direct),
            patch("app.services.report_engine._classify_batch_direct", new=classify),
        ):
            async for event, event_payload in _batch_qualitative_analysis(
                open_text,
                plan,
                stored["rows"][0],
                session_id,
                deduplicate_respondents=True,
            ):
                if event == "result":
                    qualitative = event_payload

        self.assertIsNotNone(qualitative)
        evidence = qualitative[1]["themes"][0]["quote_evidence"][0]
        self.assertEqual(evidence["quote"], "Original English")
        self.assertIn("shared", evidence["source"])
        self.assertIn("来源语言=en", evidence["source"])

        archive_session = deepcopy(stored)
        archive_session.update({
            "id": session_id,
            "report_md": "# Google Forms family report\n\nSafe aggregate findings.",
            "plan": plan,
            "qualitative_context": {},
        })
        with patch(
            "app.services.report_history.mutate_history",
            side_effect=lambda mutate: mutate([]),
        ):
            archived = report_history.save_to_history(
                session_id,
                archive_session,
            )

        self.assertIsNotNone(archived)
        self.assertEqual(archived["row_count"], 2)
        self.assertEqual(
            archived["questionnaire_family_ref"]["family_id"],
            self.family.family_id,
        )
        self.assertNotIn("rows", archived)
        self.assertNotIn("google_forms_response_provenance", archived)
        serialized = json.dumps(archived, ensure_ascii=False)
        self.assertNotIn("Original English", serialized)
        self.assertNotIn("Jawaban asli", serialized)
        self.assertNotIn("shared", serialized)


if __name__ == "__main__":
    unittest.main()
