"""Runtime toggle acceptance using isolated settings, mock sessions and writers."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core.config import DEFAULT_QUICK_WRITER_REQUIREMENTS
from app.routers import settings_api, survey as survey_router
from app.schemas.requests import AppSettingsPatch
from app.services import settings_service, survey_service
from app.storage import settings as settings_storage
from tests.test_report_quick_mode_flow import _draft
from tests.test_survey_report_versions import _base_session, _event_payloads, _isolated_report_runtime


class _IsolatedSettings:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="quick-platform-setting-")
        self.addCleanup(temporary.cleanup)
        self.settings_file = Path(temporary.name) / "app_settings.json"
        path_patch = patch.object(settings_storage, "APP_SETTINGS_FILE", str(self.settings_file))
        path_patch.start()
        self.addCleanup(path_patch.stop)

    def set_enabled(self, enabled):
        return settings_service.update_app_settings(
            AppSettingsPatch(report_quick_mode_enabled=enabled)
        )


class QuickReportSettingTests(_IsolatedSettings, unittest.TestCase):
    def test_default_preserves_existing_settings_and_persists_disabled(self):
        existing = {"comment_duplicate_reminder_enabled": False,
                    "google_forms_entry_enabled": True, "custom_setting": "keep"}
        self.settings_file.write_text(json.dumps(existing), encoding="utf-8")
        result = settings_service.get_app_settings()
        self.assertFalse(result["report_quick_mode_enabled"])
        self.assertEqual({key: result[key] for key in existing}, existing)
        self.assertEqual(json.loads(self.settings_file.read_text(encoding="utf-8")), result)

    def test_platform_setting_controls_options_and_confirmation_without_restart(self):
        sess = _base_session()
        with patch.object(survey_service, "get_session", return_value=sess):
            for env_value, enabled in (("true", False), ("false", True), ("true", False)):
                with self.subTest(env=env_value, enabled=enabled), patch.dict(
                    os.environ, {"REPORT_QUICK_MODE_ENABLED": env_value}
                ):
                    settings, detail = self.set_enabled(enabled)
                    self.assertEqual(settings["report_quick_mode_enabled"], enabled)
                    self.assertEqual(detail, "快速报告模式：" + ("开启" if enabled else "关闭"))
                    self.assertEqual(survey_service.report_style_options("session")["quick_enabled"], enabled)
                    if enabled:
                        self.assertEqual(survey_service._validate_report_style(sess, "quick"), "quick")
                    else:
                        with self.assertRaises(HTTPException) as caught:
                            survey_service._validate_report_style(sess, "quick")
                        self.assertEqual(caught.exception.status_code, 400)
                    self.assertEqual(survey_service._validate_report_style(sess, "full"), "full")

    def test_enabled_does_not_expand_supported_report_types(self):
        self.set_enabled(True)
        for fields in ({"analysis_mode": "quantitative"}, {"mode": "crosstab"},
                       {"mode": "comment"}, {"mode": "interview"}, {"mode": "annotate"}):
            sess = {**_base_session(), **fields}
            with self.subTest(fields=fields), patch.object(survey_service, "get_session", return_value=sess):
                self.assertFalse(survey_service.report_style_options("session")["quick_enabled"])
                with self.assertRaises(HTTPException):
                    survey_service._validate_report_style(sess, "quick")

    def test_admin_save_persists_audits_and_immediately_changes_report_options(self):
        app = FastAPI()
        app.include_router(settings_api.router)
        app.include_router(survey_router.router)
        with TestClient(app) as client, ExitStack() as stack:
            stack.enter_context(patch.object(settings_api, "_require_admin", new=AsyncMock()))
            audit = stack.enter_context(patch.object(settings_api, "audit_log", new=AsyncMock()))
            stack.enter_context(patch.object(survey_router, "require_session_request_access", new=AsyncMock()))
            stack.enter_context(patch.object(survey_service, "get_session", return_value=_base_session()))
            self.assertFalse(client.get("/api/app-settings").json()["report_quick_mode_enabled"])
            for enabled in (True, False):
                response = client.patch("/api/app-settings", json={"report_quick_mode_enabled": enabled})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(client.get("/api/app-settings").json()["report_quick_mode_enabled"], enabled)
                self.assertEqual(client.get("/api/report/session/options").json()["quick_enabled"], enabled)
                self.assertEqual(audit.await_args.args[3], "快速报告模式：" + ("开启" if enabled else "关闭"))
            self.assertEqual(audit.await_count, 2)

    def test_non_admin_cannot_read_or_change_switch(self):
        self.set_enabled(False)
        before = self.settings_file.read_bytes()
        app = FastAPI()
        app.include_router(settings_api.router)
        denied = AsyncMock(side_effect=HTTPException(status_code=403, detail="需要管理员权限"))
        with TestClient(app) as client, patch.object(settings_api, "_require_admin", new=denied), patch.object(
            settings_api, "audit_log", new=AsyncMock()
        ) as audit:
            self.assertEqual(client.get("/api/app-settings").status_code, 403)
            self.assertEqual(client.patch("/api/app-settings", json={"report_quick_mode_enabled": True}).status_code, 403)
            audit.assert_not_awaited()
        self.assertEqual(self.settings_file.read_bytes(), before)

    def test_disabling_before_confirmation_does_not_save_requested_quick_mode(self):
        sess = _base_session()
        self.set_enabled(False)
        with patch.object(survey_service, "get_session", return_value=sess), patch.object(
            survey_service, "save_session"
        ) as save:
            with self.assertRaises(HTTPException):
                survey_service.confirm_survey_plan("session", {"email": "owner@example.com"}, report_style="quick")
            save.assert_not_called()
        self.assertNotIn("pending_report_style", sess)


class QuickReportRuntimeToggleTests(_IsolatedSettings, unittest.IsolatedAsyncioTestCase):
    async def test_disabling_before_generation_rejects_without_model_call_or_full_fallback(self):
        sess = {**_base_session(), "pending_report_style": "quick"}
        self.set_enabled(False)
        writer = AsyncMock()
        with _isolated_report_runtime(sess, writer):
            events = _event_payloads([event async for event in survey_service.report_stream("quick-disabled", None)])
        writer.assert_not_awaited()
        self.assertTrue(any(event["type"] == "error" for event in events))
        self.assertFalse(any(event["type"] == "report_done" for event in events))
        self.assertNotIn("report_versions", sess)

    async def test_disabling_during_writing_does_not_change_inflight_report_mode(self):
        sess = {**_base_session(), "pending_report_style": "quick"}
        self.set_enabled(True)

        async def write_then_disable(*args, **kwargs):
            self.set_enabled(False)
            return _draft(), "mock-model"

        writer = AsyncMock(side_effect=write_then_disable)
        with _isolated_report_runtime(sess, writer), patch.object(
            survey_service, "_get_prompt_text", return_value=DEFAULT_QUICK_WRITER_REQUIREMENTS
        ):
            events = _event_payloads([event async for event in survey_service.report_stream("quick-inflight", None)])
        self.assertFalse([event for event in events if event["type"] == "error"], events)
        self.assertTrue(any(event["type"] == "report_done" and event["report_style"] == "quick" for event in events))
        self.assertEqual(writer.await_count, 1)
        self.assertFalse(settings_service.is_quick_report_enabled())


if __name__ == "__main__":
    unittest.main()
