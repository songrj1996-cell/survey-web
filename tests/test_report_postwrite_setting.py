"""Saved postwrite switch: isolated files, real settings routes, synthetic reports."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.core import config
from app.routers import settings_api
from app.schemas.requests import AppSettingsPatch
from app.services import auth, settings_service, survey_service
from app.services.report_modes import prepare_report_markdown
from app.services.report_postwrite_inputs import freeze_inputs
from app.storage import settings as settings_storage
from tests.test_report_postwrite_audit import session
from tests.test_report_postwrite_flow import REPORT, events, fake_collect, runtime


class IsolatedSettings:
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="postwrite-setting-")
        self.addCleanup(temporary.cleanup)
        self.settings_file = Path(temporary.name) / "app_settings.json"
        self.original = {**settings_storage.DEFAULT_APP_SETTINGS, "custom_setting": {"keep": 7}}
        self.settings_file.write_text(json.dumps(self.original), encoding="utf-8")
        for manager in (
            patch.object(settings_storage, "APP_SETTINGS_FILE", str(self.settings_file)),
            patch.object(config, "REPORT_POSTWRITE_ENABLED", False),
        ):
            manager.start()
            self.addCleanup(manager.stop)

    def set_enabled(self, enabled):
        return settings_service.update_app_settings(AppSettingsPatch(report_postwrite_enabled=enabled))


class PostwriteSettingTests(IsolatedSettings, unittest.TestCase):
    def test_read_uses_legacy_default_without_persisting_or_enabling(self):
        before = self.settings_file.read_bytes()
        for legacy in (False, True, False):
            with self.subTest(legacy=legacy), patch.object(config, "REPORT_POSTWRITE_ENABLED", legacy):
                self.assertEqual(settings_service.get_app_settings_for_display()["report_postwrite_enabled"], legacy)
                self.assertEqual(settings_service.is_postwrite_report_enabled(), legacy)
        self.assertEqual(self.settings_file.read_bytes(), before)
        self.assertNotIn("report_postwrite_enabled", settings_service.get_app_settings())

    def test_saved_true_and_false_override_environment_and_survive_fresh_reads(self):
        for enabled in (True, False, True):
            with self.subTest(enabled=enabled), patch.object(config, "REPORT_POSTWRITE_ENABLED", not enabled):
                result, detail = self.set_enabled(enabled)
                self.assertEqual(result["report_postwrite_enabled"], enabled)
                self.assertEqual(detail, "先写作后核验：" + ("开启" if enabled else "关闭"))
                self.assertEqual(settings_service.is_postwrite_report_enabled(), enabled)
                saved = json.loads(self.settings_file.read_text(encoding="utf-8"))
                self.assertEqual(saved.pop("report_postwrite_enabled"), enabled)
                self.assertEqual(saved, self.original)

    def test_other_setting_save_does_not_materialize_postwrite_default(self):
        settings_service.update_app_settings(AppSettingsPatch(report_quick_mode_enabled=True))
        self.assertNotIn("report_postwrite_enabled", json.loads(self.settings_file.read_text(encoding="utf-8")))
        self.assertFalse(settings_service.get_app_settings_for_display()["report_postwrite_enabled"])

    def test_fresh_entry_immediately_switches_but_existing_engines_remain_bound(self):
        for enabled in (False, True, False):
            self.set_enabled(enabled)
            self.assertEqual(survey_service._use_postwrite_engine(session()), enabled)
            for changes in ({"report_md": "old"}, {"report_mode": "quick"},
                            {"report_mode": "statistics"}, {"mode": "crosstab"},
                            {"mode": "quantitative"}, {"mode": "comment"}, {"mode": "interview"}):
                with self.subTest(enabled=enabled, changes=changes):
                    self.assertFalse(survey_service._use_postwrite_engine({**session(), **changes}))
        self.assertTrue(survey_service._use_postwrite_engine({"input_snapshot": freeze_inputs(session())}))

    def test_history_rerun_requires_switch_and_never_rewrites_the_old_version(self):
        snapshot = {"version": 1, "report_md": REPORT, "report_mode": "insight",
                    "input_snapshot": freeze_inputs(session())}
        owner = {"id": "synthetic-history", "report_versions": [deepcopy(snapshot)]}
        original = deepcopy(owner)
        with patch.object(survey_service, "_load_history", return_value=[]), \
             patch.object(survey_service, "_find_history_for_login", return_value=owner), \
             patch.object(survey_service, "resolve_report_version", return_value=snapshot), \
             patch.object(survey_service, "new_session", return_value="synthetic-session") as create, \
             patch.object(survey_service, "save_session") as save, \
             patch.object(survey_service, "_assign_session_owner"):
            with self.assertRaises(HTTPException) as caught:
                survey_service.prepare_report_history_session("synthetic-history", None, 1)
            self.assertEqual(caught.exception.status_code, 409)
            create.assert_not_called()
            save.assert_not_called()
            self.set_enabled(True)
            self.assertEqual(survey_service.prepare_report_history_session("synthetic-history", None, 1), "synthetic-session")
            save.assert_called_once()
            self.assertEqual(save.call_args.args[1]["rerun_base_version"], 1)
        self.assertEqual(owner, original)
        for enabled in (True, False):
            self.set_enabled(enabled)
            self.assertEqual(prepare_report_markdown(snapshot, "body"), REPORT)

    def test_platform_save_returns_effective_state_audits_and_controls_entry(self):
        app = FastAPI()
        app.include_router(settings_api.router)
        with TestClient(app) as client, ExitStack() as stack:
            stack.enter_context(patch.object(auth, "_current_login", new=AsyncMock(return_value={"name": "合成操作员"})))
            stack.enter_context(patch.object(auth, "_get_user_perms", return_value={"platform"}))
            stack.enter_context(patch.object(auth, "_get_user_permission_levels", return_value={"platform": "use"}))
            audit = stack.enter_context(patch.object(settings_api, "audit_log", new=AsyncMock()))
            before = self.settings_file.read_bytes()
            self.assertFalse(client.get("/api/app-settings").json()["report_postwrite_enabled"])
            self.assertEqual(self.settings_file.read_bytes(), before)
            for enabled in (True, False):
                response = client.patch("/api/app-settings", json={"report_postwrite_enabled": enabled})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["report_postwrite_enabled"], enabled)
                self.assertEqual(client.get("/api/app-settings").json()["report_postwrite_enabled"], enabled)
                self.assertEqual(survey_service._use_postwrite_engine(session()), enabled)
                self.assertEqual(audit.await_args.args[3], "先写作后核验：" + ("开启" if enabled else "关闭"))
            self.assertEqual(audit.await_count, 2)

    def test_read_only_and_missing_platform_permission_cannot_save(self):
        app = FastAPI()
        app.include_router(settings_api.router)
        with TestClient(app) as client, patch.object(auth, "_current_login", new=AsyncMock(return_value={})), \
             patch.object(settings_api, "audit_log", new=AsyncMock()) as audit:
            for perms, levels, read_status in (({"platform"}, {"platform": "read"}, 200), (set(), {}, 403)):
                with self.subTest(perms=perms), patch.object(auth, "_get_user_perms", return_value=perms), \
                     patch.object(auth, "_get_user_permission_levels", return_value=levels):
                    before = self.settings_file.read_bytes()
                    self.assertEqual(client.get("/api/app-settings").status_code, read_status)
                    self.assertEqual(client.patch("/api/app-settings", json={"report_postwrite_enabled": True}).status_code, 403)
                    self.assertEqual(self.settings_file.read_bytes(), before)
            audit.assert_not_awaited()

    def test_failed_save_does_not_report_success_or_change_effective_switch(self):
        with patch.object(settings_service, "_save_app_settings", side_effect=OSError("合成保存失败")):
            with self.assertRaisesRegex(OSError, "合成保存失败"):
                self.set_enabled(True)
        self.assertFalse(settings_service.is_postwrite_report_enabled())


class PostwriteRuntimeToggleTests(IsolatedSettings, unittest.IsolatedAsyncioTestCase):
    async def test_disabling_during_running_report_does_not_interrupt_pipeline(self):
        self.set_enabled(True)
        sess = session()
        calls = []

        async def collect(messages, **kwargs):
            calls.append(messages)
            if len(calls) == 1:
                self.set_enabled(False)
            return await fake_collect(messages, **kwargs)

        with runtime(sess, collect), patch.object(survey_service, "is_postwrite_report_enabled",
                                                   side_effect=settings_service.is_postwrite_report_enabled):
            result = events([e async for e in survey_service.report_stream("inflight-switch", None)])
        self.assertTrue(any(e["type"] == "report_done" for e in result), result)
        self.assertFalse(any(e["type"] == "error" for e in result), result)
        self.assertGreater(len(calls), 1)
        self.assertFalse(settings_service.is_postwrite_report_enabled())

    async def test_saved_report_disabled_does_not_call_model_or_fallback(self):
        sess = session()
        self.set_enabled(True)
        with runtime(sess), patch.object(survey_service, "is_postwrite_report_enabled",
                                        side_effect=settings_service.is_postwrite_report_enabled):
            _ = [e async for e in survey_service.report_stream("saved-switch", None)]
        self.set_enabled(False)
        collect = AsyncMock(side_effect=AssertionError("unexpected model call"))
        with runtime(sess, collect), patch.object(survey_service, "is_postwrite_report_enabled",
                                                   side_effect=settings_service.is_postwrite_report_enabled), \
             patch.object(survey_service, "_full_report_stream") as fallback:
            result = events([e async for e in survey_service.report_stream(
                "saved-switch", None, generation_kind="regenerate", base_version=1)])
        self.assertTrue(any(e["type"] == "error" for e in result), result)
        collect.assert_not_awaited()
        fallback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
