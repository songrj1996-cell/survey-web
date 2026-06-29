"""routers/settings_api:上传说明 / 提示词 / 页面文案 / 系统设置接口（HTTP 壳）。

读写逻辑全部在 services/settings_service。
"""
from fastapi import APIRouter, Request

from app.core.text import _short_text
from app.schemas.requests import AppSettingsPatch, PromptUpdateRequest, UiTextUpdateRequest
from app.services.audit import audit_log
from app.services.auth import _require_admin
from app.services.settings_service import (
    get_all_prompts,
    get_all_ui_texts,
    get_app_settings,
    get_upload_guide,
    update_app_settings,
    update_prompt,
    update_ui_text,
)

router = APIRouter()


@router.get("/api/upload-guide")
async def get_upload_guide_endpoint():
    return {"content": get_upload_guide()}


@router.get("/api/prompts")
async def get_prompts():
    return get_all_prompts()


@router.put("/api/prompts/{key}")
async def update_prompt_endpoint(key: str, req: PromptUpdateRequest, request: Request):
    update_prompt(key, req.content, req.note or "")
    await audit_log(request, "settings", "修改 Prompt", f"{key}；备注：{_short_text(req.note or '')}")
    return {"ok": True, "key": key}


@router.get("/api/ui-texts")
async def get_ui_texts():
    return get_all_ui_texts()


@router.put("/api/ui-texts/{key}")
async def update_ui_text_endpoint(key: str, req: UiTextUpdateRequest, request: Request):
    update_ui_text(key, req.content)
    await audit_log(request, "settings", "修改页面文案", f"{key}；内容：{_short_text(req.content)}")
    return {"ok": True, "key": key}


@router.get("/api/app-settings")
async def get_app_settings_endpoint(request: Request):
    await _require_admin(request)
    return get_app_settings()


@router.patch("/api/app-settings")
async def update_app_settings_endpoint(req: AppSettingsPatch, request: Request):
    await _require_admin(request)
    settings, detail = update_app_settings(req)
    await audit_log(request, "settings", "修改平台设置", detail)
    return settings
