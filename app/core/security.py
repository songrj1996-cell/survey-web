"""core/security:认证与权限的纯逻辑 + 登录态解析 + 鉴权响应构造。

边界:本模块不直接读写数据文件——白名单经 storage.whitelist、登录态经 storage.logins;
外部令牌刷新经 feishu_export(集成层)。不在此读写 JSON 文件,保持 core 不反向依赖。
"""
import time
from urllib.parse import quote

from fastapi import HTTPException, Request

from app.integrations import feishu_client as feishu_export
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import (
    COOKIE_NAME,
    FEISHU_ADMIN_EMAILS,
    FEISHU_ALLOWED_EMAILS,
    FEISHU_LOGIN_REQUIRED,
    MAX_HISTORY,
)
from app.storage.logins import (
    _save_web_logins,
    _sync_web_logins_from_disk,
    web_logins,
)
from app.storage.whitelist import _load_whitelist

# 权限项全集（顺序即管理页展示顺序）
ALL_PERMS = ["survey", "annotate", "comment"]


def _email(login: dict | None) -> str:
    return str((login or {}).get("email", "")).strip().lower()


def _open_id(login: dict | None) -> str:
    return str((login or {}).get("open_id", "")).strip()


def _whitelist_match(u: dict, login: dict) -> bool:
    """whitelist 条目按 email 或 open_id 匹配。"""
    if not u.get("enabled", True):
        return False
    e = _email(login)
    oid = _open_id(login)
    uid = u.get("email", "").strip().lower()  # 字段复用，可存 email 或 open_id
    return bool((e and uid == e) or (oid and uid == oid))


def _is_admin(login: dict | None) -> bool:
    e = _email(login)
    if not e:
        return False
    if FEISHU_ADMIN_EMAILS and e in FEISHU_ADMIN_EMAILS:
        return True
    # FEISHU_ALLOWED_EMAILS 里的人也视为管理员（向下兼容）
    if FEISHU_ALLOWED_EMAILS and e in FEISHU_ALLOWED_EMAILS:
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
    # 没有任何白名单配置时，允许所有已登录用户（开放模式）
    if not FEISHU_ADMIN_EMAILS and not FEISHU_ALLOWED_EMAILS and not _load_whitelist():
        return True
    return False


def _login_denied_reason(login: dict | None = None) -> str:
    login = login or {}
    name = login.get("name", "")
    email = login.get("email", "").strip()
    open_id = login.get("open_id", "").strip()
    if not email and not open_id:
        return "未能识别账号，请联系管理员（飞书邮箱或 contact 权限可能未授权）"
    id_str = email or open_id
    name_str = f"（{name}）" if name else ""
    hint = f"Open ID: {open_id}" if not email else ""
    return f"账号 {id_str}{name_str} 无访问权限，请联系管理员添加。{hint}".strip()


def _safe_next_path(raw_next: str | None) -> str:
    if not raw_next:
        return "/"
    raw_next = str(raw_next).strip()
    if not raw_next.startswith("/") or raw_next.startswith("//"):
        return "/"
    if "\r" in raw_next or "\n" in raw_next:
        return "/"
    if raw_next.startswith("/api/feishu/callback"):
        return "/"
    return raw_next


def _login_url(next_path: str = "/", error: str = "") -> str:
    url = f"/login?next={quote(_safe_next_path(next_path), safe='')}"
    if error:
        url += f"&error={quote(error, safe='')}"
    return url


def _is_public_path(path: str) -> bool:
    if path in {"/login", "/favicon.ico"}:
        return True
    return path.startswith("/static/") or path.startswith("/api/feishu/")


def _wants_api_response(request: Request) -> bool:
    path = request.url.path
    accept = request.headers.get("accept", "")
    return path.startswith("/api/") or "text/event-stream" in accept or "application/json" in accept


def _unauthorized_response(request: Request):
    next_path = _safe_next_path(str(request.url.path))
    if request.url.query:
        next_path = _safe_next_path(f"{next_path}?{request.url.query}")
    if _wants_api_response(request):
        return JSONResponse(
            {"detail": "请先登录飞书", "login_url": _login_url(next_path)},
            status_code=401,
        )
    return RedirectResponse(_login_url(next_path))


def _forbidden_response(request: Request, login: dict | None = None):
    msg = _login_denied_reason(login)
    if _wants_api_response(request):
        return JSONResponse({"detail": msg}, status_code=403)
    return RedirectResponse(_login_url("/", msg))


def _owner_from_login(login: dict | None) -> dict:
    login = login or {}
    email = str(login.get("email", "")).strip().lower()
    open_id = str(login.get("open_id", "")).strip()
    if email:
        owner_key = f"email:{email}"
    elif open_id:
        owner_key = f"open_id:{open_id}"
    else:
        owner_key = ""
    return {
        "owner_key": owner_key,
        "owner_email": email,
        "owner_open_id": open_id,
        "owner_name": str(login.get("name", "")).strip(),
    }


def _history_owner_key(entry: dict | None) -> str:
    entry = entry or {}
    owner_key = str(entry.get("owner_key", "")).strip()
    if owner_key:
        return owner_key
    email = str(entry.get("owner_email", "")).strip().lower()
    if email:
        return f"email:{email}"
    open_id = str(entry.get("owner_open_id", "")).strip()
    if open_id:
        return f"open_id:{open_id}"
    return ""


def _visible_to_owner(item: dict | None, login: dict | None) -> bool:
    if not FEISHU_LOGIN_REQUIRED:
        return True
    viewer_key = _owner_from_login(login).get("owner_key", "")
    item_key = _history_owner_key(item)
    return bool(viewer_key and item_key and viewer_key == item_key)


def _assign_session_owner(sess: dict, login: dict | None) -> None:
    if not sess.get("owner_key"):
        sess.update(_owner_from_login(login))


def _find_history_for_login(history: list, hist_id: str, login: dict | None) -> dict | None:
    entry = next((h for h in history if h.get("id") == hist_id), None)
    if not entry or not _visible_to_owner(entry, login):
        return None
    return entry


def _trim_history_for_owner(history: list, owner_key: str) -> list:
    if not owner_key:
        return history[:MAX_HISTORY]
    seen = 0
    kept = []
    for entry in history:
        if _history_owner_key(entry) == owner_key:
            seen += 1
            if seen > MAX_HISTORY:
                continue
        kept.append(entry)
    return kept


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
            fresh = await feishu_export.refresh_token(login["refresh"])
            login.update(fresh)
            _save_web_logins()
        except Exception:
            web_logins.pop(sid, None)
            _save_web_logins()
            return None
    return login


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
