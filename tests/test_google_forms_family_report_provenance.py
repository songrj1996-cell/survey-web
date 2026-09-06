import unittest
from unittest.mock import AsyncMock, patch

import survey_stats

from app.integrations.google_forms_responses_client import GoogleFormResponsesCapture
from app.services.google_forms_family_binding import bind_google_forms_family_responses
from app.services.report_engine import _batch_qualitative_analysis
from tests.test_google_forms_family_api import _family
from tests.test_google_forms_family_binding import _response


class GoogleFormsFamilyReportProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_cluster_quotes_keep_original_text_language_and_response_ref(self):
        original = "The original English response"
        entries = [{
            "text": original,
            "ids": {"Google 回答来源": "en|variant-en|response-1"},
            "profile": {"来源语言": "en"},
            "respondent_key": "Google 回答来源=en|variant-en|response-1",
        }]
        plan = {
            "columns": [{"index": 0, "name": "Reason", "role": "open_text"}],
            "parts": [{"name": "Feedback", "column_indexes": [0]}],
        }
        direct = AsyncMock(side_effect=[
            {
                "data": {"themes": [{
                    "id": "theme-1",
                    "name": "Experience",
                    "description": "Experience feedback",
                    "representative_quotes": [original],
                }]},
                "model": "synthetic",
            },
            {
                "data": {"themes": [{
                    "id": "theme-1",
                    "name": "Experience",
                    "description": "Experience feedback",
                    "representative_quotes": [original],
                    "positive_summary": "",
                    "negative_summary": "",
                }]},
                "model": "synthetic",
            },
        ])
        classify = AsyncMock(return_value={
            "classifications": [{
                "response_id": "0",
                "assignments": [{"theme_id": "theme-1", "sentiment": "neutral"}],
            }],
            "model": "synthetic",
            "repair_model": "",
            "repaired_count": 0,
            "fallback_count": 0,
        })
        result = None
        with (
            patch("app.services.report_engine._direct_json_call", new=direct),
            patch("app.services.report_engine._classify_batch_direct", new=classify),
        ):
            async for event, payload in _batch_qualitative_analysis(
                {0: entries},
                plan,
                ["Reason"],
                "synthetic-session",
                deduplicate_respondents=True,
            ):
                if event == "result":
                    result = payload

        self.assertIsNotNone(result)
        theme = result[0]["themes"][0]
        self.assertEqual(theme["quotes"], [original])
        self.assertEqual(theme["quote_evidence"][0]["quote"], original)
        self.assertIn("response-1", theme["quote_evidence"][0]["source"])
        self.assertIn("来源语言=en", theme["quote_evidence"][0]["source"])

    async def test_binding_rows_flow_through_open_text_collection_into_evidence(self):
        family = _family()
        binding = bind_google_forms_family_responses(family, [
            GoogleFormResponsesCapture(
                form_id="FORM_EN",
                responses=(
                    _response("shared-response", "en", "Ranked", "Original English"),
                ),
                page_count=1,
            ),
            GoogleFormResponsesCapture(
                form_id="FORM_ID",
                responses=(
                    _response("shared-response", "id", "Campuran", "Asli Indonesia"),
                ),
                page_count=1,
            ),
        ])
        plan = {
            "columns": [
                {"index": 0, "name": "偏好模式", "role": "single_choice"},
                {"index": 1, "name": "原因", "role": "open_text"},
                {"index": 5, "name": "来源语言", "role": "profile_dim"},
                {"index": 6, "name": "Google 回答来源", "role": "id"},
            ],
            "parts": [{"name": "Feedback", "column_indexes": [1]}],
        }

        open_text = survey_stats.collect_open_text(binding.rows, plan)
        entries = open_text[1]
        self.assertEqual([item["text"] for item in entries], [
            "Original English",
            "Asli Indonesia",
        ])
        self.assertEqual([item["profile"]["来源语言"] for item in entries], [
            "en",
            "id",
        ])
        self.assertTrue(all(
            "shared-response" in item["ids"]["Google 回答来源"]
            for item in entries
        ))
        self.assertNotEqual(
            entries[0]["ids"]["Google 回答来源"],
            entries[1]["ids"]["Google 回答来源"],
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
        result = None
        with (
            patch("app.services.report_engine._direct_json_call", new=direct),
            patch("app.services.report_engine._classify_batch_direct", new=classify),
        ):
            async for event, payload in _batch_qualitative_analysis(
                open_text,
                plan,
                list(binding.rows[0]),
                "binding-provenance-session",
                deduplicate_respondents=True,
            ):
                if event == "result":
                    result = payload

        self.assertIsNotNone(result)
        evidence = result[1]["themes"][0]["quote_evidence"][0]
        self.assertEqual(evidence["quote"], "Original English")
        self.assertIn("shared-response", evidence["source"])
        self.assertIn("来源语言=en", evidence["source"])


if __name__ == "__main__":
    unittest.main()
