"""routers/feishu:飞书 OAuth 登录 / 回调 / 当前身份 / 登出（HTTP 壳）。

OAuth state 管理、code 兑换、web_logins 读写全部在 services/feishu_auth。
"""
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import COOKIE_NAME, FEISHU_LOGIN_REQUIRED, FEISHU_SESSION_SECONDS
from app.core.security import _is_admin, _login_denied_reason, _login_url, _safe_next_path
from app.integrations import feishu_client as feishu_export
from app.services.audit import _audit_log_from_login
from app.services.auth import _current_login, _get_user_perms, _login_allowed
from app.services.feishu_auth import do_logout, new_oauth_state, process_oauth_callback, require_feishu_configured

router = APIRouter()


@router.get("/api/feishu/login")
async def feishu_login(next: str = "/"):
    require_feishu_configured()
    state = new_oauth_state(_safe_next_path(next))
    return RedirectResponse(feishu_export.build_authorize_url(state))


@router.get("/api/feishu/callback")
async def feishu_callback(request: Request, code: str = "", state: str = ""):
    result = await process_oauth_callback(code, state, request)
    if not result["success"]:
        return RedirectResponse(_login_url(result["next_path"], result.get("error", "")))
    resp = RedirectResponse(_safe_next_path(result["next_path"]))
    resp.set_cookie(
        COOKIE_NAME, result["sid"],
        httponly=True, samesite="lax",
        secure=result["secure"],
        max_age=FEISHU_SESSION_SECONDS,
    )
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
    do_logout(sid)
    _audit_log_from_login(request, login, "auth", "退出登录", "用户主动退出飞书登录")
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp
