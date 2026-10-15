"""Explicit member edits with protected administrators and preserved read restrictions."""
from copy import deepcopy
from datetime import datetime, timezone
import re

from fastapi import HTTPException

from app.core.config import FEISHU_ADMIN_EMAILS, FEISHU_ALLOWED_EMAILS
from app.core.feature_permissions import ALL_PERMS, permission_levels
from app.storage.whitelist import _PERMS_SCHEMA_VERSION, mutate_whitelist


def _identifier(value):
    value = str(value).strip().lower()
    if not re.fullmatch(r"(?:[^\s@]+@[^\s@]+\.[^\s@]+|ou_[a-z0-9_-]+)", value):
        raise HTTPException(422, "请填写飞书邮箱或可靠的 Open ID；姓名不能作为唯一账号")
    return value


def _protected(identifier):
    if identifier in FEISHU_ADMIN_EMAILS or identifier in FEISHU_ALLOWED_EMAILS:
        raise HTTPException(403, "管理员身份由平台配置保护，不能通过成员编辑修改或停用")


def _perms(values, enabled):
    if any(p not in ALL_PERMS or p == "members" for p in values):
        raise HTTPException(422, "包含无效权限；成员与权限仅限既有管理员，不能通过普通成员授权提权")
    valid = list(dict.fromkeys(values))
    if enabled and not valid:
        raise HTTPException(422, "启用成员至少需要一项功能权限")
    return valid


def _history(member, before, actor):
    member.setdefault("permission_history", []).append({
        "at": datetime.now(timezone.utc).isoformat(), "actor_name": (actor or {}).get("name") or "管理员",
        "before": before, "after": {key: deepcopy(member.get(key)) for key in ("name", "perms", "enabled", "permission_levels")},
        "revision": member["revision"],
    })


def add_whitelist_user(identifier, perms, enabled, name="", actor=None):
    identifier = _identifier(identifier)
    _protected(identifier)
    if not name.strip():
        raise HTTPException(422, "请填写显示姓名")
    valid = _perms(perms, enabled)
    def add(users):
        if any(u.get("email", "").lower() == identifier for u in users):
            raise HTTPException(409, "该账号已存在，请编辑现有成员")
        member = {"email": identifier, "name": name.strip(), "perms": valid, "enabled": enabled,
                  "perms_v": _PERMS_SCHEMA_VERSION, "revision": 1}
        _history(member, None, actor)
        users.append(member)
        return f"{name.strip()}；权限：{', '.join(valid)}；状态：{'启用' if enabled else '停用'}"
    return mutate_whitelist(add)


def update_whitelist_user(email, perms, enabled, name=None, expected_revision=None, actor=None):
    email = _identifier(email)
    _protected(email)
    def update(users):
        member = next((u for u in users if u.get("email", "").lower() == email), None)
        if not member:
            raise HTTPException(404, "成员不存在")
        if expected_revision is None or expected_revision != member.get("revision", 1):
            raise HTTPException(409, "成员资料已变化，请刷新后重新保存")
        before = {key: deepcopy(member.get(key)) for key in ("name", "perms", "enabled", "permission_levels")}
        old_levels = permission_levels(member)
        next_enabled = member.get("enabled", True) if enabled is None else enabled
        next_perms = _perms(list(old_levels) if perms is None else perms, next_enabled)
        next_name = member.get("name", "") if name is None else name.strip()
        if not next_name:
            raise HTTPException(422, "请补齐显示姓名后保存")
        saved_read = {key: "read" for key, level in member.get("permission_levels", {}).items() if level == "read"}
        saved_read.update({key: "read" for key, level in old_levels.items() if level == "read"})
        member.update(name=next_name, perms=next_perms, enabled=next_enabled, permission_levels=saved_read,
                      perms_v=_PERMS_SCHEMA_VERSION, revision=member.get("revision", 1) + 1)
        _history(member, before, actor)
        return f"{next_name}；权限：{', '.join(next_perms)}；状态：{'启用' if next_enabled else '停用'}"
    return mutate_whitelist(update)


def delete_whitelist_user(email):
    raise HTTPException(409, "成员记录需要保留，请在成员编辑中停用并保存")
