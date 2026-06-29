"""services/admin_audit:审计日志过滤查询。"""
from app.core.audit import AUDIT_FEATURES
from app.services.auth import _admin_user_rows
from app.storage.audit_store import _load_audit_logs


def query_audit_logs(
    start: str = "",
    end: str = "",
    user: str = "",
    feature: str = "",
    limit: int = 300,
) -> dict:
    """过滤审计日志，返回前端所需的完整响应 dict。"""
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
