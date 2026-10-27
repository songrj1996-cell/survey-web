"""Settings navigation acceptance with synthetic identities and isolated storage."""
from contextlib import ExitStack
from copy import deepcopy
import json
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from app.core import config, security
from app.core.feature_permissions import ALL_PERMS, feature_catalog, permission_levels
from app.routers import settings_api
from app.services import auth
from app.storage import glossary, prompts, settings
from app.storage.ui_texts import DEFAULT_UI_TEXTS


def make_settings_navigation_ui_app(data_root=None):
    """Only settings routes, real drawer/scripts, fake identities; never app.main."""
    project = Path(__file__).resolve().parents[1]
    folder = Path(data_root or config.DATA_DIR).resolve()
    normal = lambda p: Path(str(p).removeprefix("\\\\?\\"))
    assert normal(folder).is_relative_to(project / ".test-artifacts" / "settings-navigation")
    folder.mkdir(parents=True, exist_ok=True)
    roles = {
        "admin": {"email": "admin@offline.invalid", "name": "离线管理员"},
        "admin_b": {"email": "admin-b@offline.invalid", "name": "另一位离线管理员"},
        "member": {"email": "member@offline.invalid", "name": "离线成员", "perms": ["platform", "prompts", "glossary", "audit"]},
        "readonly": {"email": "readonly@offline.invalid", "name": "只读成员", "perms": {"platform": "read", "glossary": "read", "prompts": "read"}},
        "none": {"email": "none@offline.invalid", "name": "未授权成员", "perms": []},
        "retired": {"email": "retired@offline.invalid", "name": "旧文案权限成员", "perms": ["ui_texts"]},
    }
    for row in roles.values():
        row.update(enabled=True, perms_v=4)
    state = {"role": "admin", "fail_settings": False}
    stack = ExitStack()
    for target, key, value in (
        (config, "DATA_DIR", str(folder)),
        (config, "GROUP_WORKFLOW_DATA_DIR", folder / "workflow"),
        (config, "LLM_MODEL_SETTINGS_FILE", str(folder / "models.json")),
        (config, "FEISHU_LOGIN_REQUIRED", True),
        (auth, "FEISHU_LOGIN_REQUIRED", True),
        (security, "FEISHU_LOGIN_REQUIRED", True),
        (security, "FEISHU_ADMIN_EMAILS", {roles["admin"]["email"], roles["admin_b"]["email"]}),
        (security, "FEISHU_ALLOWED_EMAILS", set()),
        (settings, "APP_SETTINGS_FILE", str(folder / "app_settings.json")),
        (prompts, "PROMPTS_FILE", str(folder / "prompts.json")),
        (glossary, "GLOSSARY_FILE", str(folder / "glossary.json")),
    ):
        stack.enter_context(patch.object(target, key, value))
    stack.enter_context(patch.object(auth, "_current_login", AsyncMock(side_effect=lambda _: roles.get(state["role"]))))
    stack.enter_context(patch.object(auth, "_load_whitelist", lambda: list(roles.values())))
    audit = stack.enter_context(patch.object(settings_api, "audit_log", AsyncMock()))
    settings._save_app_settings({**settings.DEFAULT_APP_SETTINGS, "report_postwrite_enabled": False, "unrelated_fixture": "preserved"})
    document = glossary.empty_glossary()
    document["items"] = [{"id": f"term-{i}", "category": "合成术语", "ch": f"合成词{i:03}",
                          "terms_by_lang": {"en": [f"synthetic-{i}"]}, "enabled": True,
                          "priority": 0, "note": "仅用于离线验收"} for i in range(300)]
    (folder / "glossary.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    app = FastAPI()
    app.include_router(settings_api.router)
    app.mount("/static", StaticFiles(directory=project / "static"), name="static")
    app.state.fixture = state
    app.state.fixture_roles = roles
    app.state.fixture_audit = audit
    app.state.fixture_stack = stack
    app.state.fixture_folder = folder

    @app.middleware("http")
    async def synthetic_failure(request, call_next):
        if state["fail_settings"] and request.method == "PATCH" and request.url.path == "/api/app-settings":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "合成保存失败，未修改设置"}, status_code=503)
        return await call_next(request)

    def identity():
        login = roles.get(state["role"])
        levels = auth._get_user_permission_levels(login)
        return {**(login or {}), "is_admin": security._is_admin(login), "feature_catalog": feature_catalog(),
                "permission_levels": levels, "perms": list(levels)}

    @app.get("/fixture/identity")
    async def get_identity():
        return identity()

    @app.post("/fixture/identity/{role}")
    async def select_identity(role: str):
        if role not in roles and role != "anonymous":
            raise HTTPException(404, "未知合成身份")
        state["role"] = role
        return identity()

    @app.post("/fixture/fail-settings/{enabled}")
    async def fail_settings(enabled: bool):
        state["fail_settings"] = enabled
        return {"ok": True}

    @app.get("/api/admin/users")
    async def get_users():
        # This fixture reads synthetic users; actual member persistence is outside this change.
        return {"users": [{**deepcopy(row), "revision": 1, "is_admin": key.startswith("admin")}
                          for key, row in roles.items()]}

    @app.get("/api/admin/audit-logs")
    async def get_audit():
        return {"logs": [], "users": [], "features": []}

    @app.get("/fixture/baseline/{asset}")
    async def baseline_asset(asset: str):
        files = {"style.css": "static/style.css", "settings.js": "static/js/features/settings.js",
                 "feature-permissions.js": "static/js/core/feature-permissions.js"}
        if asset not in files:
            raise HTTPException(404, "未知对照资源")
        content = subprocess.check_output(["git", "show", "65144bc:" + files[asset]], cwd=project)
        return Response(content, media_type="text/css" if asset.endswith(".css") else "text/javascript")

    @app.get("/", response_class=HTMLResponse)
    async def page(baseline: bool = False):
        source = (subprocess.check_output(["git", "show", "65144bc:static/index.html"], cwd=project).decode("utf-8")
                  if baseline else (project / "static/index.html").read_text(encoding="utf-8"))
        start = source.index('  <div class="drawer" id="settings-drawer">')
        end = source.index("  <!-- Report audit review", start)
        drawer = source[start:end]
        html = '''<!doctype html><html lang="zh-CN" data-theme="light"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"></head>
        <body><button id="btn-theme" hidden>主题</button><div class="sidebar__nav" hidden></div><main style="padding:32px"><h1>设置离线验收</h1>
        <p>合成身份与合成配置，不发起模型请求。</p><button id="nav-settings">打开设置</button></main>''' + drawer + '''
        <div class="toast-container" id="toast-container" aria-live="polite"></div>
        <script>window.marked={setOptions(){},parse:s=>s};</script>
        <script src="/static/js/core/core.js"></script><script src="/static/js/core/feature-permissions.js"></script>
        <script src="/static/js/features/model-settings.js"></script><script src="/static/js/features/settings.js"></script>
        <script>
        window.refreshFeishuStatus=async()=>{};
        window.applyFixtureRole=async role=>{const r=await fetch('/fixture/identity/'+role,{method:'POST'});const i=await r.json();PlatformPermissions.apply(i);return i;};
        document.getElementById('nav-settings').onclick=()=>{openDrawer('settings-drawer');loadActiveSettingsTab();};
        fetch('/fixture/identity').then(r=>r.json()).then(i=>{PlatformPermissions.apply(i);openDrawer('settings-drawer');return loadActiveSettingsTab();});
        </script></body></html>'''
        if baseline:
            html = html.replace('/static/style.css', '/fixture/baseline/style.css')
            html = html.replace('/static/js/features/settings.js', '/fixture/baseline/settings.js')
            html = html.replace('/static/js/core/feature-permissions.js', '/fixture/baseline/feature-permissions.js')
        return html

    return app


class SettingsNavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="settings-case-", dir=config.DATA_DIR)
        self.addCleanup(self.directory.cleanup)
        self.app = make_settings_navigation_ui_app(self.directory.name)
        self.addCleanup(self.app.state.fixture_stack.close)
        self.folder = Path(self.directory.name)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://isolated.test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_catalog_shares_permission_and_retires_copy_without_rewriting_grants(self):
        children = feature_catalog()[-1]["children"]
        platform = [row for row in children if row["key"] == "platform"]
        self.assertEqual(len(platform), 1)
        self.assertEqual(platform[0]["tabs"], ["functions", "system"])
        self.assertNotIn("ui_texts", ALL_PERMS)
        legacy = {"perms": ["ui_texts", "platform"], "permission_levels": {"platform": "read"}}
        saved = deepcopy(legacy)
        self.assertEqual(permission_levels(legacy), {"platform": "read"})
        self.assertEqual(legacy, saved)

    async def test_fixed_reads_ignore_legacy_content_corruption_and_never_create_files(self):
        old = self.folder / "ui_texts.json"
        for content in (None, '{"upload_guide":{"current":"OLD_CUSTOM"}}', "{broken"):
            if content is not None:
                old.write_text(content, encoding="utf-8")
            before = old.read_bytes() if old.exists() else None
            texts = await self.client.get("/api/ui-texts")
            self.assertEqual(texts.status_code, 200)
            self.assertEqual(texts.json(), DEFAULT_UI_TEXTS)
            guide = await self.client.get("/api/upload-guide")
            self.assertEqual(guide.json()["content"], DEFAULT_UI_TEXTS["upload_guide"]["current"])
            self.assertEqual(old.read_bytes() if old.exists() else None, before)

    async def test_retired_put_cannot_write_or_record_success_for_any_identity(self):
        old = self.folder / "ui_texts.json"
        old.write_text("preserve legacy bytes", encoding="utf-8")
        for role in ("admin", "member", "readonly", "none", "retired", "anonymous"):
            self.app.state.fixture["role"] = role
            response = await self.client.put("/api/ui-texts/upload_guide", json={"content": "MUST_NOT_BE_SAVED"})
            self.assertEqual(response.status_code, 410 if role == "admin" else 403)
            if role == "admin":
                self.assertIn("请刷新页面", response.json()["detail"])
            self.assertEqual(old.read_text(encoding="utf-8"), "preserve legacy bytes")
        self.app.state.fixture_audit.assert_not_called()

    async def test_each_function_patch_preserves_other_fields_and_records_exact_change(self):
        path = self.folder / "app_settings.json"
        keys = ["survey_duplicate_reminder_enabled", "comment_duplicate_reminder_enabled", "google_forms_entry_enabled",
                "report_quick_mode_enabled", "report_postwrite_enabled"]
        for key in keys:
            before = json.loads(path.read_text(encoding="utf-8"))
            requested = not before[key]
            response = await self.client.patch("/api/app-settings", json={key: requested})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {**before, key: requested})
            self.assertEqual(self.app.state.fixture_audit.await_args.args[1:3], ("settings", "修改功能设置"))

    async def test_existing_platform_grant_and_readonly_restriction_remain_effective(self):
        path = self.folder / "app_settings.json"
        for role, expected in (("member", 200), ("readonly", 403), ("none", 403), ("retired", 403), ("anonymous", 403)):
            self.app.state.fixture["role"] = role
            before = path.read_bytes()
            response = await self.client.patch("/api/app-settings", json={"report_quick_mode_enabled": True})
            self.assertEqual(response.status_code, expected)
            if expected == 403:
                self.assertEqual(path.read_bytes(), before)
            read = await self.client.get("/api/app-settings")
            self.assertEqual(read.status_code, 200 if role in ("member", "readonly") else 403)

    async def test_personal_preference_is_separate_from_functions_and_other_admin(self):
        path = self.folder / "app_settings.json"
        before = path.read_bytes()
        response = await self.client.patch("/api/group-management/preferences", json={"only_self": True, "expected_revision": 0})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((await self.client.get("/api/app-settings")).json()["group_management_preferences"]["only_self"])
        self.app.state.fixture["role"] = "admin_b"
        self.assertFalse((await self.client.get("/api/app-settings")).json()["group_management_preferences"]["only_self"])
        self.assertEqual(path.read_bytes(), before)
        self.app.state.fixture["role"] = "member"
        self.assertEqual((await self.client.patch("/api/group-management/preferences", json={"only_self": True, "expected_revision": 0})).status_code, 403)
        self.assertNotIn("group_management_preferences", (await self.client.get("/api/app-settings")).json())

    async def test_model_administration_remains_admin_only_after_menu_split(self):
        for role in ("member", "readonly", "none", "retired", "anonymous"):
            self.app.state.fixture["role"] = role
            self.assertEqual((await self.client.get("/api/model-settings")).status_code, 403)
        self.assertFalse((self.folder / "models.json").exists())

    async def test_invalid_setting_request_never_overwrites_saved_settings(self):
        path = self.folder / "app_settings.json"
        before = path.read_bytes()
        response = await self.client.patch("/api/app-settings", json={"report_quick_mode_enabled": {"invalid": True}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
