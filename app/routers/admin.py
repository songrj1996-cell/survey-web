"""routers/admin:白名单用户 CRUD + 审计日志查询（HTTP 壳）。

白名单读写在 services/admin_users，审计日志过滤在 services/admin_audit。
"""
from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas.requests import AdminUserPatch, AdminUserRequest
from app.services.admin_audit import query_audit_logs
from app.services.admin_users import add_whitelist_user, delete_whitelist_user, update_whitelist_user
from app.services.audit import audit_log
from app.services.auth import _admin_user_rows, _require_admin, _require_feature
from app.core.feature_permissions import feature_catalog
from app.core.runtime_logging import runtime_log

router = APIRouter()


@router.get("/api/admin/users")
async def admin_list_users(request: Request):
    await _require_admin(request)
    return {"users": _admin_user_rows(), "feature_catalog": feature_catalog()}


@router.get("/api/admin/audit-logs")
async def admin_audit_logs(
    request: Request,
    start: str = "",
    end: str = "",
    user: str = "",
    feature: str = "",
    limit: int = 300,
):
    await _require_feature(request, "audit", action="read")
    return query_audit_logs(start, end, user, feature, limit)


@router.post("/api/admin/users")
async def admin_add_user(req: AdminUserRequest, request: Request):
    login = await _require_admin(request)
    detail = add_whitelist_user(req.email.strip().lower(), req.perms, req.enabled, req.name, login)
    await audit_log(request, "admin", "添加用户", detail)
    return {"ok": True}


@router.patch("/api/admin/users/{email}")
async def admin_update_user(email: str, req: AdminUserPatch, request: Request):
    login = await _require_admin(request)
    detail = update_whitelist_user(email.strip().lower(), req.perms, req.enabled, req.name, req.expected_revision, login)
    await audit_log(request, "admin", "更新用户", detail)
    return {"ok": True}


@router.delete("/api/admin/users/{email}")
async def admin_delete_user(email: str, request: Request, expected_revision: int = Query(..., ge=1)):
    try:
        await _require_admin(request)
        detail = delete_whitelist_user(email.strip().lower(), expected_revision)
    except HTTPException as exc:
        runtime_log("成员与权限", "warning", f"删除成员未完成：{exc.detail}",
                    http_status=exc.status_code, path="DELETE /api/admin/users/{成员账号}")
        raise
    except OSError as exc:
        runtime_log("成员与权限", "error", "删除成员未完成；请重新读取成员列表核对结果，再决定是否重试",
                    http_status=503, path="DELETE /api/admin/users/{成员账号}")
        raise HTTPException(503, "删除未完成，请刷新成员列表核对结果后重试") from exc
    await audit_log(request, "admin", "删除成员", detail)
    runtime_log("成员与权限", "success", detail, http_status=200, path="DELETE /api/admin/users/{成员账号}")
    return {"ok": True}
