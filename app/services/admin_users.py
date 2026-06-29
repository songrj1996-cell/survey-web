"""services/admin_users:白名单用户 CRUD 业务逻辑。"""
from fastapi import HTTPException

from app.core.config import FEISHU_ADMIN_EMAILS, FEISHU_ALLOWED_EMAILS
from app.core.security import ALL_PERMS
from app.storage.whitelist import _PERMS_SCHEMA_VERSION, _load_whitelist, _save_whitelist


def add_whitelist_user(identifier: str, perms: list[str], enabled: bool) -> str:
    """添加白名单用户。返回审计详情字符串，已存在则 raise 409。"""
    users = _load_whitelist()
    if any(u.get("email", "").lower() == identifier for u in users):
        raise HTTPException(status_code=409, detail="该账号已存在")
    valid_perms = [p for p in perms if p in ALL_PERMS] or ["survey"]
    users.append({
        "email": identifier,
        "perms": valid_perms,
        "enabled": enabled,
        "perms_v": _PERMS_SCHEMA_VERSION,
    })
    _save_whitelist(users)
    return f"{identifier}，权限：{', '.join(valid_perms)}，状态：{'启用' if enabled else '禁用'}"


def update_whitelist_user(email: str, perms: list[str] | None, enabled: bool | None) -> str:
    """更新白名单用户权限或启用状态。返回审计详情，不存在则 raise 404。"""
    users = _load_whitelist()
    for u in users:
        if u.get("email", "").lower() == email:
            if perms is not None:
                u["perms"] = [p for p in perms if p in ALL_PERMS]
                u["perms_v"] = _PERMS_SCHEMA_VERSION
            if enabled is not None:
                u["enabled"] = enabled
            _save_whitelist(users)
            parts = []
            if perms is not None:
                parts.append(f"权限：{', '.join(u.get('perms', []))}")
            if enabled is not None:
                parts.append(f"状态：{'启用' if enabled else '禁用'}")
            return f"{email}；" + "；".join(parts)
    raise HTTPException(status_code=404, detail="用户不存在")


def delete_whitelist_user(email: str) -> None:
    """删除白名单用户，不允许删除管理员账号，不存在则 raise 404。"""
    if email in FEISHU_ADMIN_EMAILS or email in FEISHU_ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="管理员账号不可删除")
    users = _load_whitelist()
    new_users = [u for u in users if u.get("email", "").lower() != email]
    if len(new_users) == len(users):
        raise HTTPException(status_code=404, detail="用户不存在")
    _save_whitelist(new_users)
