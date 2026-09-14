"""Report-focus persistence, authority and access boundary regressions."""
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from app.schemas.requests import SurveyAnalysisSettingsRequest
from app.services import survey_service
from app.routers import survey


class SurveyEntrySettingsTests(unittest.TestCase):
    def setUp(self):
        self.session = {
            "rows": [["反馈"], ["希望改进"]],
            "source_type": "google",
            "columns_detected": [{"name_zh": "反馈", "role": "open_text", "column_indexes": [0]}],
            "qualitative_context": {"problem": "提高满意度"},
        }
        self.loader = patch.object(survey_service, "get_session", return_value=self.session)
        self.saver = patch.object(survey_service, "save_session")
        self.loader.start()
        self.save = self.saver.start()
        self.addCleanup(self.loader.stop)
        self.addCleanup(self.saver.stop)

    def test_statistics_focus_uses_python_and_preserves_answers_and_provenance(self):
        before = deepcopy(self.session)
        result = survey_service.set_survey_analysis_settings("entry", "statistics")
        self.assertEqual(result, {
            "report_mode": "statistics", "pending_report_style": "full",
            "report_focus": "statistics", "analysis_mode": "quantitative",
            "mode": "quantitative", "stats_source": "python",
        })
        for key, value in before.items():
            self.assertEqual(self.session[key], value)
        self.save.assert_called_once()

    def test_can_change_focus_before_planning_without_reupload(self):
        survey_service.set_survey_analysis_settings("entry", "statistics")
        result = survey_service.set_survey_analysis_settings("entry", "insight")
        self.assertEqual(result["mode"], "standard")
        self.assertEqual(result["analysis_mode"], "qualitative")
        self.assertEqual(self.session["rows"], [["反馈"], ["希望改进"]])

    def test_external_stats_reject_insight_without_any_mutation(self):
        for metadata in [{"mode": "crosstab"}, {"stats_source": "external_crosstab"}]:
            with self.subTest(metadata=metadata):
                self.session.update(metadata)
                before = deepcopy(self.session)
                with self.assertRaises(HTTPException) as caught:
                    survey_service.set_survey_analysis_settings("entry", "insight")
                self.assertEqual(caught.exception.status_code, 409)
                self.assertEqual(self.session, before)
        self.save.assert_not_called()

    def test_external_statistics_remain_authoritative(self):
        self.session.update({"mode": "crosstab", "crosstab_parsed": {"questions": ["Q1"]}, "crosstab_md": "official"})
        result = survey_service.set_survey_analysis_settings("entry", "statistics")
        self.assertEqual(result["stats_source"], "external_crosstab")
        self.assertEqual(result["mode"], "crosstab")
        self.assertEqual(self.session["crosstab_md"], "official")
        self.assertEqual(self.session["crosstab_parsed"], {"questions": ["Q1"]})

    def test_rejects_mode_change_after_approval_or_report_exists(self):
        for key in ["stats_md", "plan_approved_at", "report_md", "report_versions"]:
            with self.subTest(key=key):
                self.session[key] = {"existing": True}
                with self.assertRaises(HTTPException) as caught:
                    survey_service.set_survey_analysis_settings("entry", "statistics")
                self.assertEqual(caught.exception.status_code, 409)
                self.session.pop(key)
        self.save.assert_not_called()

    def test_focus_change_invalidates_unapproved_plan_only(self):
        self.session["plan"] = {"parts": ["old"]}
        self.session["plan_revision_texts"] = ["old focus"]
        result = survey_service.set_survey_analysis_settings("entry", "statistics")
        self.assertEqual(result["mode"], "quantitative")
        self.assertNotIn("plan", self.session)
        self.assertNotIn("plan_revision_texts", self.session)
        self.assertEqual(self.session["qualitative_context"], {"problem": "提高满意度"})

    def test_retry_same_focus_is_idempotent(self):
        survey_service.set_survey_analysis_settings("entry", "statistics")
        self.session["plan"] = {"parts": []}
        result = survey_service.set_survey_analysis_settings("entry", "statistics")
        self.assertEqual(result["mode"], "quantitative")
        self.assertEqual(self.session["plan"], {"parts": []})

    def test_empty_session_and_invalid_value_fail_before_save(self):
        self.session["rows"] = []
        with self.assertRaises(HTTPException):
            survey_service.set_survey_analysis_settings("entry", "insight")
        with self.assertRaises(ValidationError):
            SurveyAnalysisSettingsRequest(report_focus="anything")
        with self.assertRaises(HTTPException):
            survey_service.set_survey_analysis_settings("entry", "anything")
        self.save.assert_not_called()

    def test_other_workflows_cannot_be_converted_to_questionnaire_sessions(self):
        for mode in ("comment", "interview", "annotate"):
            self.session["mode"] = mode
            before = deepcopy(self.session)
            with self.assertRaises(HTTPException):
                survey_service.set_survey_analysis_settings("entry", "statistics")
            self.assertEqual(self.session, before)
        self.save.assert_not_called()


class SurveyEntryRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_route_denies_before_saving_other_users_session(self):
        with (
            patch.object(survey, "require_session_request_access", AsyncMock(side_effect=HTTPException(403, "denied"))) as access,
            patch.object(survey, "set_survey_analysis_settings") as save,
        ):
            with self.assertRaises(HTTPException):
                await survey.update_analysis_settings("someone-else", SurveyAnalysisSettingsRequest(report_focus="statistics"), object())
            access.assert_awaited_once()
            save.assert_not_called()

    async def test_authorized_route_passes_validated_preference(self):
        with (
            patch.object(survey, "require_session_request_access", AsyncMock()),
            patch.object(survey, "set_survey_analysis_settings", return_value={"mode": "quantitative"}) as save,
        ):
            result = await survey.update_analysis_settings("owned", SurveyAnalysisSettingsRequest(report_focus="statistics"), object())
            save.assert_called_once_with("owned", "statistics", report_mode=None)
            self.assertEqual(result["mode"], "quantitative")


if __name__ == "__main__":
    unittest.main()
