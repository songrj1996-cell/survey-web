"""services/settings_service:提示词 / 页面文案 / 系统设置的读取与更新。"""
from datetime import datetime

from fastapi import HTTPException

from app.schemas.requests import AppSettingsPatch
from app.storage.prompts import _load_prompts, _save_prompts
from app.storage.settings import _load_app_settings, _save_app_settings
from app.storage.ui_texts import _load_ui_texts, _save_ui_texts


def get_upload_guide() -> str:
    return _load_prompts().get("upload_guide", {}).get("current", "")


def get_all_prompts() -> dict:
    return _load_prompts()


def update_prompt(key: str, content: str, note: str) -> None:
    prompts = _load_prompts()
    if key not in prompts:
        raise HTTPException(status_code=404, detail=f"prompt '{key}' 不存在")
    p = prompts[key]
    if not p.get("editable", False):
        raise HTTPException(status_code=403, detail="该 Prompt 在 Dify 后台管理，不可在此修改")
    p["history"].insert(0, {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "content": p["current"],
        "note": note or "（未填写修改说明）",
    })
    p["history"] = p["history"][:20]
    p["current"] = content
    _save_prompts(prompts)


def get_all_ui_texts() -> dict:
    return _load_ui_texts()


def update_ui_text(key: str, content: str) -> None:
    texts = _load_ui_texts()
    if key not in texts:
        raise HTTPException(status_code=404, detail=f"ui-text '{key}' 不存在")
    texts[key]["current"] = content
    _save_ui_texts(texts)


def get_app_settings() -> dict:
    return _load_app_settings()


def update_app_settings(patch: AppSettingsPatch) -> tuple[dict, str]:
    """更新系统设置，返回 (settings, audit_detail)。"""
    settings = _load_app_settings()
    if patch.comment_duplicate_reminder_enabled is not None:
        settings["comment_duplicate_reminder_enabled"] = bool(patch.comment_duplicate_reminder_enabled)
    _save_app_settings(settings)
    detail = f"评论重复文件提醒：{'开启' if settings.get('comment_duplicate_reminder_enabled') else '关闭'}"
    return settings, detail
