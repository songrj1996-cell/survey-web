"""services/feishu_auth:飞书 OAuth 登录态管理——state 生成、回调处理、登出。"""
import secrets
import time

from fastapi import HTTPException, Request

from app.core.config import FEISHU_LOGIN_REQUIRED, FEISHU_SESSION_SECONDS
from app.core.security import _login_denied_reason
from app.integrations import feishu_client
from app.services.audit import _audit_log_from_login
from app.services.auth import _login_allowed
from app.storage.logins import _save_web_logins, _sync_web_logins_from_disk, web_logins

# OAuth state 暂存（进程内内存，短期有效；仅登录/回调使用）
oauth_states: dict[str, dict] = {}


def new_oauth_state(next_path: str) -> str:
    """生成并存储 OAuth state token，清理过期 state，返回 state 字符串。"""
    state = secrets.token_urlsafe(16)
    oauth_states[state] = {"next": next_path, "ts": time.time()}
    now = time.time()
    for k in list(oauth_states.keys()):
        if oauth_states[k]["ts"] + 600 < now:
            del oauth_states[k]
    return state


async def process_oauth_callback(code: str, state: str, request: Request) -> dict:
    """处理飞书 OAuth 回调。

    返回 dict：
    - success=True: {"success": True, "login": ..., "sid": ..., "next_path": ..., "secure": ...}
    - success=False: {"success": False, "next_path": ..., "error": ...}
    - 硬错误（state 无效、code 缺失）直接 raise HTTPException。
    """
    st = oauth_states.pop(state, None)
    if not st:
        raise HTTPException(status_code=400, detail="state 无效或已过期，请重新登录")
    if not code:
        raise HTTPException(status_code=400, detail="缺少授权 code")
    next_path = st.get("next") or "/"
    try:
        login = await feishu_client.exchange_code(code)
    except Exception as e:
        return {"success": False, "next_path": next_path, "error": f"飞书授权失败：{e}"}
    if FEISHU_LOGIN_REQUIRED and not _login_allowed(login):
        return {"success": False, "next_path": next_path, "error": _login_denied_reason(login)}

    now = time.time()
    login["created_at"] = now
    login["expires_at"] = now + FEISHU_SESSION_SECONDS
    sid = secrets.token_urlsafe(24)
    _sync_web_logins_from_disk()
    web_logins[sid] = login
    _save_web_logins()
    _audit_log_from_login(request, login, "auth", "飞书登录", "飞书授权登录成功")
    secure = feishu_client.FEISHU_REDIRECT_URI.startswith("https://")
    return {"success": True, "login": login, "sid": sid, "next_path": next_path, "secure": secure}


def do_logout(sid: str) -> None:
    """从 web_logins 中移除 session。"""
    _sync_web_logins_from_disk()
    web_logins.pop(sid, None)
    _save_web_logins()
