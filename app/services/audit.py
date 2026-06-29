"""services/audit:审计事件写入——需要 storage + 登录态解析。

纯工具（AUDIT_FEATURES / AUDIT_FEATURE_LABELS / _client_ip / _audit_user）仍在 core/audit。
"""
import uuid
from datetime import datetime

from fastapi import Request

from app.core.audit import AUDIT_FEATURE_LABELS, _audit_user, _client_ip
from app.services.auth import _current_login
from app.storage.audit_store import _append_audit_log


def _audit_log_from_login(
    request: Request | None,
    login: dict | None,
    feature: str,
    action: str,
    detail: str = "",
    *,
    status: str = "success",
    metadata: dict | None = None,
) -> None:
    entry = {
        "id": str(uuid.uuid4()),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "feature": feature,
        "feature_label": AUDIT_FEATURE_LABELS.get(feature, feature),
        "action": action,
        "detail": str(detail or "")[:1000],
        "status": status,
        "ip": _client_ip(request),
        "metadata": metadata or {},
        **_audit_user(login),
    }
    _append_audit_log(entry)


async def audit_log(
    request: Request | None,
    feature: str,
    action: str,
    detail: str = "",
    *,
    status: str = "success",
    metadata: dict | None = None,
) -> None:
    try:
        login = await _current_login(request) if request else None
        _audit_log_from_login(request, login, feature, action, detail, status=status, metadata=metadata)
    except Exception as exc:
        print(f"[audit] collect failed: {exc}", flush=True)
