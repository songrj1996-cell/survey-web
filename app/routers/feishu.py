"""routers/feishu:飞书 OAuth 登录 / 回调 / 当前身份 / 登出。"""
import secrets
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.audit import _audit_log_from_login
from app.core.config import COOKIE_NAME, FEISHU_LOGIN_REQUIRED, FEISHU_SESSION_SECONDS
from app.core.security import (
    _current_login,
    _get_user_perms,
    _is_admin,
    _login_allowed,
    _login_denied_reason,
    _login_url,
    _safe_next_path,
)
from app.integrations import feishu_client as feishu_export
from app.storage.logins import _save_web_logins, _sync_web_logins_from_disk, web_logins

router = APIRouter()

# OAuth state 暂存(进程内内存,短期有效;仅登录/回调使用)
oauth_states: dict[str, dict] = {}


@router.get("/api/feishu/login")
async def feishu_login(next: str = "/"):
    if not feishu_export.is_configured():
        raise HTTPException(status_code=500, detail="未配置飞书应用（FEISHU_APP_ID/SECRET/REDIRECT_URI）")
    state = secrets.token_urlsafe(16)
    oauth_states[state] = {"next": _safe_next_path(next), "ts": time.time()}
    # 清理过期 state
    now = time.time()
    for k in list(oauth_states.keys()):
        if oauth_states[k]["ts"] + 600 < now:
            del oauth_states[k]
    return RedirectResponse(feishu_export.build_authorize_url(state))


@router.get("/api/feishu/callback")
async def feishu_callback(request: Request, code: str = "", state: str = ""):
    st = oauth_states.pop(state, None)
    if not st:
        raise HTTPException(status_code=400, detail="state 无效或已过期，请重新登录")
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权 code")
    try:
        login = await feishu_export.exchange_code(code)
    except Exception as e:
        return RedirectResponse(_login_url(st.get("next") or "/", f"飞书授权失败：{e}"))
    if FEISHU_LOGIN_REQUIRED and not _login_allowed(login):
        return RedirectResponse(_login_url(st.get("next") or "/", _login_denied_reason(login)))
    now = time.time()
    login["created_at"] = now
    login["expires_at"] = now + FEISHU_SESSION_SECONDS
    sid = secrets.token_urlsafe(24)
    _sync_web_logins_from_disk()
    web_logins[sid] = login
    _save_web_logins()
    _audit_log_from_login(request, login, "auth", "飞书登录", "飞书授权登录成功")
    resp = RedirectResponse(_safe_next_path(st.get("next") or "/"))
    secure = feishu_export.FEISHU_REDIRECT_URI.startswith("https://")
    resp.set_cookie(COOKIE_NAME, sid, httponly=True, samesite="lax", secure=secure, max_age=FEISHU_SESSION_SECONDS)
    return resp


@router.get("/api/feishu/me")
async def feishu_me(request: Request):
    login = await _current_login(request)
    allowed = _login_allowed(login)
    return {
        "configured": feishu_export.is_configured(),
        "login_required": FEISHU_LOGIN_REQUIRED,
        "logged_in": bool(login),
        "allowed": allowed,
        "name": (login or {}).get("name", ""),
        "email": (login or {}).get("email", ""),
        "open_id": (login or {}).get("open_id", ""),
        "login_url": _login_url("/"),
        "error": "" if (not login or allowed) else _login_denied_reason(login),
        "perms": _get_user_perms(login),
        "is_admin": _is_admin(login),
    }


@router.post("/api/feishu/logout")
async def feishu_logout(request: Request):
    login = await _current_login(request)
    sid = request.cookies.get(COOKIE_NAME, "")
    _sync_web_logins_from_disk()
    web_logins.pop(sid, None)
    _save_web_logins()
    _audit_log_from_login(request, login, "auth", "退出登录", "用户主动退出飞书登录")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp
