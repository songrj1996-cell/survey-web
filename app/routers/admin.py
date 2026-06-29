"""routers/admin:白名单用户 CRUD + 审计日志查询（HTTP 壳）。

白名单读写在 services/admin_users，审计日志过滤在 services/admin_audit。
"""
from fastapi import APIRouter, Request

from app.schemas.requests import AdminUserPatch, AdminUserRequest
from app.services.admin_audit import query_audit_logs
from app.services.admin_users import add_whitelist_user, delete_whitelist_user, update_whitelist_user
from app.services.audit import audit_log
from app.services.auth import _admin_user_rows, _require_admin

router = APIRouter()


@router.get("/api/admin/users")
async def admin_list_users(request: Request):
    await _require_admin(request)
    return {"users": _admin_user_rows()}


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
    return query_audit_logs(start, end, user, feature, limit)


@router.post("/api/admin/users")
async def admin_add_user(req: AdminUserRequest, request: Request):
    await _require_admin(request)
    detail = add_whitelist_user(req.email.strip().lower(), req.perms, req.enabled)
    await audit_log(request, "admin", "添加用户", detail)
    return {"ok": True}


@router.patch("/api/admin/users/{email}")
async def admin_update_user(email: str, req: AdminUserPatch, request: Request):
    await _require_admin(request)
    detail = update_whitelist_user(email.strip().lower(), req.perms, req.enabled)
    await audit_log(request, "admin", "更新用户", detail)
    return {"ok": True}


@router.delete("/api/admin/users/{email}")
async def admin_delete_user(email: str, request: Request):
    await _require_admin(request)
    delete_whitelist_user(email.strip().lower())
    await audit_log(request, "admin", "删除用户", email.strip().lower())
    return {"ok": True}
