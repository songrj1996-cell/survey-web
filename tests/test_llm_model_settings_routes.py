"""A/B HTTP checks without importing app.main or touching real data."""
import asyncio
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from app.core import config, security
from app.routers import settings_api
from app.services import auth


def make_model_settings_ui_app():
    """Local browser fixture, only settings routes; never app.main or real logins."""
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    root = Path(__file__).resolve().parents[1]
    security.FEISHU_ADMIN_EMAILS = {"ui-admin@example.test"}
    security.FEISHU_ALLOWED_EMAILS = set()
    auth._current_login = AsyncMock(return_value={"email": "ui-admin@example.test", "name": "界面验证管理员"})
    settings_api.audit_log = AsyncMock()
    app = FastAPI()
    app.include_router(settings_api.router)
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def page():
        return '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head><body style="overflow:auto;height:auto"><div style="padding:16px;max-width:1024px;margin:auto"><div id="model-settings-root"></div></div><script src="/static/js/core/feature-permissions.js"></script><script src="/static/js/features/model-settings.js"></script><script>ModelSettings.load()</script></body></html>'''

    return app


class ModelSettingsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="model-routes-", dir=config.DATA_DIR)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "models.json"
        self.login = {"email": "admin@example.test", "name": "测试管理员"}
        for target, attribute, value in ((config, "LLM_MODEL_SETTINGS_FILE", str(self.path)),
                                        (security, "FEISHU_ADMIN_EMAILS", {"admin@example.test"}),
                                        (security, "FEISHU_ALLOWED_EMAILS", set())):
            patcher = patch.object(target, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(auth, "_current_login", AsyncMock(side_effect=lambda _: self.login))
        patcher.start(); self.addCleanup(patcher.stop)
        self.audit = AsyncMock()
        patcher = patch.object(settings_api, "audit_log", self.audit)
        patcher.start(); self.addCleanup(patcher.stop)
        self.model_call = AsyncMock(side_effect=AssertionError("No live model calls permitted"))
        patcher = patch("app.integrations.llm_client.collect_chat_completion", self.model_call)
        patcher.start(); self.addCleanup(patcher.stop)
        app = FastAPI(); app.include_router(settings_api.router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://isolated.test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_admin_check_publish_restore_and_no_model_calls(self):
        response = await self.client.get("/api/model-settings")
        self.assertEqual(response.status_code, 200)
        state = response.json()
        self.assertFalse(self.path.exists())
        cfg = deepcopy(state["configuration"])
        cfg["models"][0]["name"] += " 一"
        body = {"expected_revision": state["revision"], "configuration": cfg}
        checked = await self.client.post("/api/model-settings/check", json=body)
        self.assertTrue(checked.json()["valid"])
        self.assertFalse(self.path.exists())
        first = await self.client.post("/api/model-settings/publish", json=body)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["version"], 1)
        body["expected_revision"] = first.json()["revision"]
        cfg["models"][0]["name"] += " 二"
        second = await self.client.post("/api/model-settings/publish", json=body)
        self.assertEqual(second.status_code, 200, second.text)
        restored = await self.client.post("/api/model-settings/versions/1/restore", json={"expected_revision": second.json()["revision"]})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()["restored_from"], 1)
        self.assertEqual((await self.client.get("/api/model-settings")).json()["version"], 2)
        self.assertEqual(self.audit.await_count, 2)
        for version, recorded in enumerate(self.audit.await_args_list, start=1):
            self.assertEqual(recorded.args[1:3], ("settings", "保存模型配置版本"))
            self.assertEqual(
                recorded.args[3],
                f"v{version}；配置已发布，新任务使用此版本，进行中及恢复的任务沿用原配置",
            )
        self.assertEqual(first.json()["history"][0]["actor_name"], "测试管理员")
        self.model_call.assert_not_called()

    async def test_non_admin_and_anonymous_cannot_use_any_endpoint(self):
        state = (await self.client.get("/api/model-settings")).json()
        body = {"expected_revision": state["revision"], "configuration": state["configuration"]}
        for login in (None, {"email": "member@example.test", "name": "普通成员", "perms": ["model_settings"]}):
            self.login = login
            self.assertEqual((await self.client.get("/api/model-settings")).status_code, 403)
            for path, payload in (("/check", body), ("/publish", body), ("/versions/1/restore", {"expected_revision": state["revision"]})):
                self.assertEqual((await self.client.post("/api/model-settings" + path, json=payload)).status_code, 403)
        self.assertFalse(self.path.exists())
        self.audit.assert_not_called()

    async def test_conflict_invalid_fields_and_corruption_are_clear(self):
        state = (await self.client.get("/api/model-settings")).json()
        body = {"expected_revision": "0" * 64, "configuration": state["configuration"]}
        conflict = await self.client.post("/api/model-settings/check", json=body)
        self.assertEqual(conflict.status_code, 409)
        self.assertIn("草稿", conflict.json()["detail"])
        body["expected_revision"] = state["revision"]
        body["configuration"]["secret"] = "disallowed"
        self.assertEqual((await self.client.post("/api/model-settings/publish", json=body)).status_code, 422)
        self.path.write_text("{broken", encoding="utf-8")
        result = await self.client.get("/api/model-settings")
        self.assertEqual(result.status_code, 503)
        self.assertIn("未覆盖", result.json()["detail"])
        self.assertEqual(self.path.read_text(), "{broken")
