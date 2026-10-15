"""Feature and record access. Admin inspection never becomes owner mutation."""
from fastapi import HTTPException, Request
import re

from app.core import config
from app.core.security import _history_owner_key, _is_admin, _owner_from_login
from app.services.auth import _get_user_permission_levels


def owner_identity(login: dict | None) -> dict:
    result = _owner_from_login(login)
    if not result["owner_key"]:
        if config.FEISHU_LOGIN_REQUIRED:
            raise HTTPException(401, "请先登录飞书")
        result.update(owner_key="local:anonymous", owner_name="本地操作者")
    return result


def require_permission(login: dict | None, feature: str, *, write: bool = False) -> None:
    if not config.FEISHU_LOGIN_REQUIRED:
        return
    level = _get_user_permission_levels(login).get(feature)
    if not level or (write and level != "use"):
        raise HTTPException(403, "当前账号没有此操作权限")
    if feature == "members" and not _is_admin(login):
        raise HTTPException(403, "成员权限管理需要管理员身份")


def can_read(record: dict, login: dict | None) -> bool:
    return bool(
        _history_owner_key(record) == owner_identity(login)["owner_key"]
        or _is_admin(login)
    )


def require_record(record: dict | None, login: dict | None, *, write: bool = False) -> dict:
    if not record:
        raise HTTPException(404, "记录不存在或无权访问")
    own = _history_owner_key(record) == owner_identity(login)["owner_key"]
    if not own and (write or not _is_admin(login)):
        raise HTTPException(404, "记录不存在或无权访问")
    return record


def history_feature(entry: dict) -> str:
    mode = str(entry.get("mode") or entry.get("kind") or (entry.get("plan") or {}).get("mode") or "")
    if mode.startswith("interview"):
        return "interview"
    if mode in ("annotate", "annotation"):
        return "annotate"
    if mode.startswith("comment"):
        return "comment"
    return "survey"


def legacy_request_is_read(request) -> bool:
    if request.method != "GET":
        return False
    path = request.url.path
    if any(part in path for part in ("/run", "/preprocess", "/api/columns/", "/api/plan/")):
        return False
    if re.fullmatch(r"/api/report/[^/]+", path):
        return bool(request.query_params.get("version"))  # A saved version is read-only; otherwise GET generates.
    return True


async def legacy_route_access(request: Request):
    """One feature/object gate for old route families, including shared QA/exports."""
    from app.services.auth import _current_login
    from app.core.security import admin_history_read_scope
    from app.storage.history import _load_history
    from app.storage.sessions import get_session
    login = await _current_login(request)
    path = request.url.path
    read_only = legacy_request_is_read(request)
    feature = "comment" if path.startswith("/api/comment-analysis") else "annotate" if path.startswith("/api/annotate") else "survey"
    params = request.path_params
    history_id = params.get("history_id") or params.get("hist_id") or request.query_params.get("history_id")
    session_id = params.get("session_id") or params.get("sid")
    if request.method in ("POST", "PATCH") and "application/json" in request.headers.get("content-type", ""):
        body = await request.json()
        if isinstance(body, dict):
            history_id = history_id or body.get("history_id") or (body.get("id") if path == "/api/history-title" else None)
            session_id = session_id or body.get("session_id")
    if history_id:
        entry = next((h for h in _load_history() if h.get("id") == history_id), None)
        if config.FEISHU_LOGIN_REQUIRED:
            require_record(entry, login, write=not read_only)
        elif not entry:
            raise HTTPException(404, "历史记录不存在")
        feature = history_feature(entry)
    elif session_id and not path.startswith("/api/annotate"):
        entry = get_session(session_id)
        if config.FEISHU_LOGIN_REQUIRED:
            require_record(entry, login, write=not read_only)
        feature = history_feature(entry)
    if path != "/api/history":
        require_permission(login, feature, write=not read_only)
    token = admin_history_read_scope.set(read_only)
    try:
        yield
    finally:
        admin_history_read_scope.reset(token)
