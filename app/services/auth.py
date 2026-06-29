"""services/auth:登录态解析与访问控制判断。

读写 storage 或调用 integrations 的鉴权函数放在这里;
纯判断逻辑（_whitelist_match / _is_admin / _login_denied_reason 等）仍在 core/security。
"""
import time

from fastapi import HTTPException, Request

from app.core.config import (
    COOKIE_NAME,
    FEISHU_ADMIN_EMAILS,
    FEISHU_ALLOWED_EMAILS,
    FEISHU_LOGIN_REQUIRED,
)
from app.core.security import ALL_PERMS, _is_admin, _whitelist_match
from app.integrations import feishu_client
from app.storage.logins import _save_web_logins, _sync_web_logins_from_disk, web_logins
from app.storage.whitelist import _load_whitelist


async def _current_login(request: Request) -> dict | None:
    """从 cookie 取登录态；token 临过期则尝试刷新。返回 None 表示未登录。"""
    sid = request.cookies.get(COOKIE_NAME, "")
    _sync_web_logins_from_disk()
    login = web_logins.get(sid)
    if not login:
        return None
    now = time.time()
    if login.get("expires_at", 0) and login["expires_at"] < now:
        web_logins.pop(sid, None)
        _save_web_logins()
        return None
    if login.get("exp", 0) < time.time() + 120 and login.get("refresh"):
        try:
            fresh = await feishu_client.refresh_token(login["refresh"])
            login.update(fresh)
            _save_web_logins()
        except Exception:
            web_logins.pop(sid, None)
            _save_web_logins()
            return None
    return login


def _login_allowed(login: dict | None) -> bool:
    if not FEISHU_LOGIN_REQUIRED:
        return True
    if not login:
        return False
    if _is_admin(login):
        return True
    for u in _load_whitelist():
        if _whitelist_match(u, login):
            return True
    if not FEISHU_ADMIN_EMAILS and not FEISHU_ALLOWED_EMAILS and not _load_whitelist():
        return True
    return False


def _get_user_perms(login: dict | None) -> list[str]:
    if not FEISHU_LOGIN_REQUIRED:
        return list(ALL_PERMS)
    if not login:
        return []
    if _is_admin(login):
        return list(ALL_PERMS)
    for u in _load_whitelist():
        if _whitelist_match(u, login):
            return list(u.get("perms", list(ALL_PERMS)))
    return []


async def _require_admin(request: Request):
    login = await _current_login(request)
    if not _is_admin(login):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return login


def _admin_user_rows() -> list[dict]:
    users = _load_whitelist()
    result = []
    for u in users:
        e = u.get("email", "").lower()
        result.append({
            "email": e,
            "perms": u.get("perms", list(ALL_PERMS)),
            "enabled": u.get("enabled", True),
            "is_admin": False,
        })
    admin_emails = set(FEISHU_ADMIN_EMAILS) | set(FEISHU_ALLOWED_EMAILS)
    existing = {u["email"] for u in result}
    for e in sorted(admin_emails):
        if e not in existing:
            result.insert(0, {"email": e, "perms": list(ALL_PERMS), "enabled": True, "is_admin": True})
        else:
            for u in result:
                if u["email"] == e:
                    u["is_admin"] = True
    return result
