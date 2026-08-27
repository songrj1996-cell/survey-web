import unittest
from unittest.mock import AsyncMock, patch

from app.services.report_engine import _batch_qualitative_analysis


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


if __name__ == "__main__":
    unittest.main()
