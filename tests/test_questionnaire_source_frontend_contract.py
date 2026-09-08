from __future__ import annotations

from pathlib import Path
import re
import tempfile
import unittest

from fastapi import Request

from app.main import app as browser_app
from app.storage import sessions as session_storage


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INDEX = (PROJECT_ROOT / "static" / "index.html").read_text(encoding="utf-8")
JAVASCRIPT = (
    PROJECT_ROOT / "static" / "js" / "features" / "questionnaire-sources.js"
).read_text(encoding="utf-8")
SURVEY_JAVASCRIPT = (
    PROJECT_ROOT / "static" / "js" / "features" / "survey.js"
).read_text(encoding="utf-8")
STYLESHEET = (
    PROJECT_ROOT / "static" / "questionnaire-sources.css"
).read_text(encoding="utf-8")
SETTINGS_JAVASCRIPT = (
    PROJECT_ROOT / "static" / "js" / "features" / "settings.js"
).read_text(encoding="utf-8")


_BROWSER_SESSION_TEMP = tempfile.TemporaryDirectory(
    prefix="survey-web-browser-family-",
)
session_storage._SESSION_DIR = Path(_BROWSER_SESSION_TEMP.name)


_BROWSER_FAMILY = {
    "schema_version": 1,
    "family_id": "qfam_browser_synthetic",
    "title": "浏览器合成项目",
    "status": "ready",
    "variant_count": 2,
    "languages": ["en", "id"],
    "canonical_question_count": 1,
    "blocking_issue_count": 0,
    "warning_count": 0,
    "diagnostics": [],
    "updated_at": "2026-09-03T00:00:00Z",
}


@browser_app.get("/api/questionnaire-sources/capabilities")
async def _browser_capabilities():
    return {
        "schema_version": 1,
        "google_forms_connection": True,
        "google_forms_unified_analysis": True,
    }


@browser_app.get("/api/questionnaire-sources/google-forms/families")
async def _browser_families():
    return {
        "schema_version": 1,
        "items": [_BROWSER_FAMILY],
        "next_cursor": None,
    }


@browser_app.post("/api/questionnaire-sources/google-forms/families")
async def _browser_create_family(request: Request):
    payload = await request.json()
    variants = payload.get("variants") or []
    return {
        **_BROWSER_FAMILY,
        "title": payload.get("title") or _BROWSER_FAMILY["title"],
        "variant_count": len(variants),
        "languages": [item.get("language") for item in variants],
    }


@browser_app.get(
    "/api/questionnaire-sources/google-forms/families/{family_id}"
)
async def _browser_get_family(family_id: str):
    return _BROWSER_FAMILY


@browser_app.post(
    "/api/questionnaire-sources/google-forms/families/{family_id}/refresh"
)
async def _browser_refresh_family(family_id: str):
    return _BROWSER_FAMILY


@browser_app.post(
    "/api/questionnaire-sources/google-forms/families/{family_id}/analysis-sessions"
)
async def _browser_analysis_session(family_id: str):
    session_id = session_storage.new_session()
    session_storage.save_session(session_id, {
        "id": session_id,
        "filename": "google-forms-family-browser.json",
        "source_type": "google",
        "rows": [["反馈", "来源语言", "Google 回答来源"], ["很好", "en", "en|v1|r1"]],
        "columns_detected": [
            {"name_zh": "反馈", "role": "open_text", "column_indexes": [0]},
            {"name_zh": "来源语言", "role": "profile_dim", "column_indexes": [1]},
            {"name_zh": "Google 回答来源", "role": "id", "column_indexes": [2]},
        ],
        "column_provider": "questionnaire",
        "questionnaire_translation_status": "translated",
        "questionnaire_used": True,
    })
    return {
        "session_id": session_id,
        "filename": "google-forms-family-browser.json",
        "total_rows": 1,
        "headers": ["反馈", "来源语言", "Google 回答来源"],
        "preview": [["很好", "en", "en|v1|r1"]],
        "source_type": "google",
        "questionnaire_used": True,
        "matched_questions": 1,
        "questionnaire_family_id": family_id,
        "languages": ["en", "id"],
        "duplicate_response_count": 0,
        "unmatched_answer_count": 0,
        "file_upload_answer_count": 0,
    }


class QuestionnaireSourceFrontendContractTests(unittest.TestCase):
    def test_assets_are_loaded_once_after_survey_ingress(self):
        self.assertEqual(
            INDEX.count('/static/questionnaire-sources.css?v=1'),
            1,
        )
        self.assertEqual(
            INDEX.count('/static/js/features/questionnaire-sources.js?v=1'),
            1,
        )
        self.assertLess(
            INDEX.index('/static/js/features/survey.js?v=38'),
            INDEX.index('/static/js/features/questionnaire-sources.js?v=1'),
        )

    def test_only_google_capability_and_family_endpoints_are_present(self):
        endpoints = set(re.findall(
            r"['\"](/api/questionnaire-sources[^'\"]*)['\"]",
            JAVASCRIPT,
        ))
        self.assertEqual(endpoints, {
            '/api/questionnaire-sources/capabilities',
            '/api/questionnaire-sources/google-forms/families',
        })
        lowered = JAVASCRIPT.casefold()
        for forbidden in (
            'bested',
            '/snapshots',
            '/materials',
            'asset-review',
            'screenshot',
            'pdf',
        ):
            self.assertNotIn(forbidden, lowered)

    def test_one_to_ten_variant_controls_and_last_item_guard_are_explicit(self):
        self.assertIn('const MAX_VARIANTS = 10', JAVASCRIPT)
        self.assertIn("variants: [{ language: '', form_url: '' }]", JAVASCRIPT)
        self.assertIn('state.variants.length >= MAX_VARIANTS', JAVASCRIPT)
        self.assertIn('state.variants.length <= 1', JAVASCRIPT)
        self.assertIn('已达到 10 个版本上限', JAVASCRIPT)
        self.assertIn('添加语言版本（${state.variants.length}/10）', JAVASCRIPT)

    def test_saved_projects_refresh_and_continue_analysis(self):
        self.assertIn('?limit=20', JAVASCRIPT)
        self.assertIn('/refresh`', JAVASCRIPT)
        self.assertIn('/analysis-sessions`', JAVASCRIPT)
        self.assertIn('已保存调研项目', JAVASCRIPT)
        self.assertIn('刷新结构', JAVASCRIPT)
        self.assertIn('继续分析', JAVASCRIPT)

    def test_structured_errors_and_safe_rendering_are_used(self):
        self.assertIn("typeof payload.detail === 'object'", JAVASCRIPT)
        self.assertIn('detail?.message', JAVASCRIPT)
        self.assertIn('detail?.code', JAVASCRIPT)
        self.assertIn('textContent', JAVASCRIPT)
        self.assertNotIn('.innerHTML', JAVASCRIPT)

    def test_deduplication_copy_matches_backend_contract(self):
        self.assertIn(
            '每个 Form 内按 responseId 防止 API 重复读取',
            JAVASCRIPT,
        )
        self.assertIn('不做跨 Form 的回答内容去重', JAVASCRIPT)

    def test_family_session_enters_existing_data_confirmation_flow(self):
        self.assertIn('window.surveySessionIngress', JAVASCRIPT)
        self.assertIn('acceptGoogleFormsFamilySession(session)', JAVASCRIPT)
        self.assertIn(
            "Object.defineProperty(window, 'surveySessionIngress'",
            SURVEY_JAVASCRIPT,
        )
        self.assertIn("goStep(2)", SURVEY_JAVASCRIPT)
        self.assertIn("loadColumns()", SURVEY_JAVASCRIPT)
        self.assertIn('已连接多语言 Google Forms 回答', SURVEY_JAVASCRIPT)
        self.assertIn('data.file_upload_answer_count', SURVEY_JAVASCRIPT)
        self.assertIn(
            '文件上传回答仅保留 Drive 元数据',
            SURVEY_JAVASCRIPT,
        )

    def test_responsive_styles_cover_variant_and_project_layout(self):
        self.assertIn('.qsrc-variant', STYLESHEET)
        self.assertIn('.qsrc-project', STYLESHEET)
        self.assertIn('@media (max-width: 720px)', STYLESHEET)

    def test_admin_settings_exposes_google_link_entry_toggle(self):
        self.assertIn('setting-google-forms-entry', SETTINGS_JAVASCRIPT)
        self.assertIn('google_forms_entry_enabled', SETTINGS_JAVASCRIPT)
        self.assertIn('问卷分析·Google Link 入口', SETTINGS_JAVASCRIPT)


if __name__ == '__main__':
    unittest.main()
