import unittest
from unittest.mock import patch

from app.services.crosstab_service import handle_crosstab_upload


class QuantitativeOptionalCrosstabTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_crosstab_uses_questionnaire_columns_and_python_stats(self):
        session = {}
        imported = {
            "rows": [["使用频率"], ["每天"], ["偶尔"]],
            "questions": [{
                "name_zh": "使用频率",
                "role": "single_choice",
                "column_indexes": [0],
                "options": ["每天", "偶尔"],
            }],
            "questionnaire_text": "Q1 使用频率",
        }
        with (
            patch("app.services.crosstab_service.parse_bested_qualitative_upload", return_value=imported),
            patch("app.services.crosstab_service.new_session", return_value="sid"),
            patch("app.services.crosstab_service.get_session", return_value=session),
            patch("app.services.crosstab_service.save_session"),
            patch("app.services.crosstab_service._assign_session_owner"),
        ):
            result = await handle_crosstab_upload(
                b"questionnaire", "questionnaire.xls",
                b"responses", "responses.xlsx",
                None, None, None,
            )

        self.assertEqual(result["mode"], "quantitative")
        self.assertEqual(result["stats_source"], "python")
        self.assertTrue(result["requires_column_confirmation"])
        self.assertEqual(session["column_provider"], "questionnaire")
        self.assertNotIn("confirmed_columns", session)

    async def test_uploaded_crosstab_remains_authoritative(self):
        session = {}
        parsed = {
            "segments": [{"label": "总体"}],
            "questions": [{"name": "使用频率", "options": []}],
        }
        with (
            patch("app.services.crosstab_service._parse_file", side_effect=[[["Q1"], ["每天"]], [["Q1", "使用频率"]]]),
            patch("app.services.crosstab_service.crosstab_parser.parse", return_value=parsed),
            patch("app.services.crosstab_service.crosstab_parser.render_to_markdown", return_value="## 使用频率"),
            patch("app.services.crosstab_service.new_session", return_value="sid"),
            patch("app.services.crosstab_service.get_session", return_value=session),
            patch("app.services.crosstab_service.save_session"),
            patch("app.services.crosstab_service._assign_session_owner"),
        ):
            result = await handle_crosstab_upload(
                b"questionnaire", "questionnaire.xlsx",
                b"responses", "responses.xlsx",
                b"crosstab", "crosstab.xlsx", None,
            )

        self.assertEqual(result["mode"], "crosstab")
        self.assertEqual(result["stats_source"], "external_crosstab")
        self.assertFalse(result["requires_column_confirmation"])
        self.assertEqual(session["crosstab_parsed"], parsed)
        self.assertIn("confirmed_columns", session)


if __name__ == "__main__":
    unittest.main()
