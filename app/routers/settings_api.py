"""routers/settings_api:上传说明 / 提示词 / 页面文案 / 系统设置 接口。"""
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from app.services.audit import audit_log
from app.services.auth import _require_admin
from app.core.text import _short_text
from app.schemas.requests import (
    AppSettingsPatch,
    PromptUpdateRequest,
    UiTextUpdateRequest,
)
from app.storage.prompts import _load_prompts, _save_prompts
from app.storage.settings import _load_app_settings, _save_app_settings
from app.storage.ui_texts import _load_ui_texts, _save_ui_texts

router = APIRouter()


@router.get("/api/upload-guide")
async def get_upload_guide():
    prompts = _load_prompts()
    return {"content": prompts.get("upload_guide", {}).get("current", "")}


@router.get("/api/prompts")
async def get_prompts():
    return _load_prompts()


@router.put("/api/prompts/{key}")
async def update_prompt(key: str, req: PromptUpdateRequest, request: Request):
    prompts = _load_prompts()
    if key not in prompts:
        raise HTTPException(status_code=404, detail=f"prompt '{key}' 不存在")
    p = prompts[key]
    if not p.get("editable", False):
        raise HTTPException(status_code=403, detail="该 Prompt 在 Dify 后台管理，不可在此修改")

    # 把当前版本存入历史
    p["history"].insert(0, {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content": p["current"],
        "note": req.note or "（未填写修改说明）",
    })
    p["history"] = p["history"][:20]  # 保留最近 20 条
    p["current"] = req.content
    _save_prompts(prompts)
    await audit_log(request, "settings", "修改 Prompt", f"{key}；备注：{_short_text(req.note or '')}")
    return {"ok": True, "key": key}


@router.get("/api/ui-texts")
async def get_ui_texts():
    return _load_ui_texts()


@router.put("/api/ui-texts/{key}")
async def update_ui_text(key: str, req: UiTextUpdateRequest, request: Request):
    texts = _load_ui_texts()
    if key not in texts:
        raise HTTPException(status_code=404, detail=f"ui-text '{key}' 不存在")
    texts[key]["current"] = req.content
    _save_ui_texts(texts)
    await audit_log(request, "settings", "修改页面文案", f"{key}；内容：{_short_text(req.content)}")
    return {"ok": True, "key": key}


@router.get("/api/app-settings")
async def get_app_settings(request: Request):
    await _require_admin(request)
    return _load_app_settings()


@router.patch("/api/app-settings")
async def update_app_settings(req: AppSettingsPatch, request: Request):
    await _require_admin(request)
    settings = _load_app_settings()
    if req.comment_duplicate_reminder_enabled is not None:
        settings["comment_duplicate_reminder_enabled"] = bool(req.comment_duplicate_reminder_enabled)
    _save_app_settings(settings)
    await audit_log(
        request,
        "settings",
        "修改平台设置",
        f"评论重复文件提醒：{'开启' if settings.get('comment_duplicate_reminder_enabled') else '关闭'}",
    )
    return settings
