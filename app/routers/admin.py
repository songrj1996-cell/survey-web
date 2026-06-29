"""routers/admin:白名单用户 CRUD + 审计日志查询。"""
from fastapi import APIRouter, HTTPException, Request

from app.core.audit import AUDIT_FEATURES
from app.core.config import FEISHU_ADMIN_EMAILS, FEISHU_ALLOWED_EMAILS
from app.core.security import ALL_PERMS
from app.services.audit import audit_log
from app.services.auth import _admin_user_rows, _require_admin
from app.schemas.requests import AdminUserPatch, AdminUserRequest
from app.storage.audit_store import _load_audit_logs
from app.storage.whitelist import _PERMS_SCHEMA_VERSION, _load_whitelist, _save_whitelist

router = APIRouter()


@router.get("/api/admin/users")
async def admin_list_users(request: Request):
    await _require_admin(request)
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
    # 追加管理员条目（仅展示，不可通过 UI 删除）
    admin_emails = set(FEISHU_ADMIN_EMAILS) | set(FEISHU_ALLOWED_EMAILS)
    existing = {u["email"] for u in result}
    for e in sorted(admin_emails):
        if e not in existing:
            result.insert(0, {"email": e, "perms": list(ALL_PERMS), "enabled": True, "is_admin": True})
        else:
            for u in result:
                if u["email"] == e:
                    u["is_admin"] = True
    return {"users": result}


@router.get("/api/admin/audit-logs")
async def admin_audit_logs(
    request: Request,
    start: str = "",
    end: str = "",
    user: str = "",
    feature: str = "",
    limit: int = 300,
):
    await _require_admin(request)
    limit = max(20, min(int(limit or 300), 1000))
    user = (user or "").strip().lower()
    feature = (feature or "").strip()
    start = (start or "").strip()
    end = (end or "").strip()
    if len(start) == 16:
        start = f"{start}:00"
    if len(end) == 16:
        end = f"{end}:59"

    logs = _load_audit_logs()
    filtered = []
    for item in logs:
        ts = str(item.get("ts", ""))
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        if user and str(item.get("user_email", "")).strip().lower() != user:
            continue
        if feature and item.get("feature") != feature:
            continue
        filtered.append(item)

    return {
        "logs": filtered[:limit],
        "users": _admin_user_rows(),
        "features": AUDIT_FEATURES,
        "total": len(filtered),
        "limit": limit,
    }


@router.post("/api/admin/users")
async def admin_add_user(req: AdminUserRequest, request: Request):
    await _require_admin(request)
    identifier = req.email.strip().lower()
    if not identifier:
        raise HTTPException(status_code=400, detail="邮箱或 Open ID 不能为空")
    users = _load_whitelist()
    if any(u.get("email", "").lower() == identifier for u in users):
        raise HTTPException(status_code=409, detail="该账号已存在")
    valid_perms = [p for p in req.perms if p in ALL_PERMS]
    users.append({
        "email": identifier,
        "perms": valid_perms or ["survey"],
        "enabled": req.enabled,
        "perms_v": _PERMS_SCHEMA_VERSION,  # 新增用户已是新结构，无需再迁移
    })
    _save_whitelist(users)
    await audit_log(
        request,
        "admin",
        "添加用户",
        f"{identifier}，权限：{', '.join(valid_perms or ['survey'])}，状态：{'启用' if req.enabled else '禁用'}",
    )
    return {"ok": True}


@router.patch("/api/admin/users/{email}")
async def admin_update_user(email: str, req: AdminUserPatch, request: Request):
    await _require_admin(request)
    email = email.strip().lower()
    users = _load_whitelist()
    for u in users:
        if u.get("email", "").lower() == email:
            if req.perms is not None:
                u["perms"] = [p for p in req.perms if p in ALL_PERMS]
                u["perms_v"] = _PERMS_SCHEMA_VERSION  # 管理员显式设置后锁定结构版本
            if req.enabled is not None:
                u["enabled"] = req.enabled
            _save_whitelist(users)
            parts = []
            if req.perms is not None:
                parts.append(f"权限：{', '.join(u.get('perms', []))}")
            if req.enabled is not None:
                parts.append(f"状态：{'启用' if req.enabled else '禁用'}")
            await audit_log(request, "admin", "更新用户", f"{email}；" + "；".join(parts))
            return {"ok": True}
    raise HTTPException(status_code=404, detail="用户不存在")


@router.delete("/api/admin/users/{email}")
async def admin_delete_user(email: str, request: Request):
    await _require_admin(request)
    email = email.strip().lower()
    # 不允许删除 admin 邮箱
    if email in FEISHU_ADMIN_EMAILS or email in FEISHU_ALLOWED_EMAILS:
        raise HTTPException(status_code=403, detail="管理员账号不可删除")
    users = _load_whitelist()
    new_users = [u for u in users if u.get("email", "").lower() != email]
    if len(new_users) == len(users):
        raise HTTPException(status_code=404, detail="用户不存在")
    _save_whitelist(new_users)
    await audit_log(request, "admin", "删除用户", email)
    return {"ok": True}
