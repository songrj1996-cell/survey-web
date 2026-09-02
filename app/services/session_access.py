"""services/session_access:运行时任务 session 的统一所有权授权入口。"""
from collections.abc import Awaitable, Callable

from fastapi import HTTPException

from app.core import config
from app.core.security import _history_owner_key, _owner_from_login
from app.storage.sessions import get_session


SESSION_ACCESS_NOT_FOUND_DETAIL = "任务不存在或已过期，请重新上传文件"
SessionLoader = Callable[[str], dict]
LoginResolver = Callable[[object], Awaitable[dict | None]]


def _require_login_owner_key(login: dict | None) -> str:
    """登录开启时要求当前登录态能映射到稳定 owner_key。"""
    owner_key = str(_owner_from_login(login).get("owner_key") or "").strip()
    if not owner_key:
        raise HTTPException(status_code=401, detail="请先登录飞书")
    return owner_key


def require_loaded_session_access(sess: dict, login: dict | None) -> dict:
    """校验已加载 session 的 owner；登录关闭时保持原本开放行为。"""
    if not config.FEISHU_LOGIN_REQUIRED:
        return sess

    viewer_key = _require_login_owner_key(login)
    session_owner_key = _history_owner_key(sess)
    if not session_owner_key or session_owner_key != viewer_key:
        raise HTTPException(
            status_code=404,
            detail=SESSION_ACCESS_NOT_FOUND_DETAIL,
        )
    return sess


def require_session_access(
    session_id: str,
    login: dict | None,
    *,
    loader: SessionLoader | None = None,
) -> dict:
    """先确认登录身份，再加载并校验 session，避免未登录请求读取任务数据。"""
    if config.FEISHU_LOGIN_REQUIRED:
        _require_login_owner_key(login)

    load = loader or get_session
    try:
        sess = load(session_id)
    except HTTPException as exc:
        if config.FEISHU_LOGIN_REQUIRED and exc.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail=SESSION_ACCESS_NOT_FOUND_DETAIL,
            ) from None
        raise
    return require_loaded_session_access(sess, login)


async def require_session_request_access(
    request: object,
    session_id: str,
    *,
    login_resolver: LoginResolver,
    loader: SessionLoader | None = None,
) -> dict | None:
    """Router 前置授权；登录关闭时不解析登录态、不提前读取 session。"""
    if not config.FEISHU_LOGIN_REQUIRED:
        return None
    login = await login_resolver(request)
    require_session_access(session_id, login, loader=loader)
    return login
