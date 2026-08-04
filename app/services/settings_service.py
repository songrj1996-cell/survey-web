"""services/settings_service:提示词 / 页面文案 / 系统设置的读取与更新。"""
import hashlib
from datetime import datetime

from fastapi import HTTPException

from app.schemas.requests import AppSettingsPatch
from app.storage.prompts import (
    DEFAULT_PROMPTS,
    _load_prompts,
    _prompt_update_transaction,
)
from app.storage.settings import _load_app_settings, _save_app_settings
from app.storage.ui_texts import _load_ui_texts, _save_ui_texts

_MAX_PROMPT_LENGTH = 100_000


def _prompt_revision(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def get_upload_guide() -> str:
    return _load_ui_texts().get("upload_guide", {}).get("current", "")


def get_all_prompts() -> dict:
    prompts = _load_prompts()
    return {
        key: {
            **prompts[key],
            "revision": _prompt_revision(prompts[key].get("current", "")),
        }
        for key in DEFAULT_PROMPTS
        if key in prompts
    }


def update_prompt(
    key: str,
    content: str,
    note: str,
    expected_revision: str | None = None,
) -> None:
    if key not in DEFAULT_PROMPTS:
        raise HTTPException(status_code=404, detail="提示词不存在")
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=422, detail="提示词内容不能为空")
    if len(content) > _MAX_PROMPT_LENGTH:
        raise HTTPException(status_code=422, detail="提示词内容过长")
    with _prompt_update_transaction() as prompts:
        if key not in prompts:
            raise HTTPException(status_code=404, detail="提示词不存在")
        p = prompts[key]
        if not p.get("editable", False):
            raise HTTPException(status_code=403, detail="提示词不可修改")
        current = p.get("current", "")
        if expected_revision and expected_revision != _prompt_revision(current):
            raise HTTPException(
                status_code=409,
                detail="提示词已被其他管理员更新；你的草稿仍已保留，请加载最新版本后再修改",
            )
        if content == current:
            raise HTTPException(status_code=409, detail="提示词内容没有变化")
        p["history"].insert(0, {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "content": p["current"],
            "note": note or "（未填写修改说明）",
        })
        p["history"] = p["history"][:20]
        p["current"] = content


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
