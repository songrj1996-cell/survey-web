"""storage/settings:系统设置(app_settings.json)的读写与默认值合并。"""
import json
import os

from app.core.config import APP_SETTINGS_FILE

DEFAULT_APP_SETTINGS = {
    "comment_duplicate_reminder_enabled": True,
}


def _load_app_settings() -> dict:
    if not os.path.exists(APP_SETTINGS_FILE):
        _save_app_settings(DEFAULT_APP_SETTINGS)
        return dict(DEFAULT_APP_SETTINGS)
    try:
        with open(APP_SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        data = {}
    dirty = False
    for key, value in DEFAULT_APP_SETTINGS.items():
        if key not in data:
            data[key] = value
            dirty = True
    if dirty:
        _save_app_settings(data)
    return data


def _save_app_settings(settings: dict) -> None:
    with open(APP_SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
