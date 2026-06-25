"""core/audit:审计事件的构造入口。

负责拼装审计事件(取 IP、组装 user、组装 entry)并交 storage.audit_store 落盘;
不负责文件读写本身。audit_log 会解析当前登录态(经 core.security)。
"""
import uuid
from datetime import datetime

from fastapi import Request

from app.core.security import _current_login
from app.storage.audit_store import _append_audit_log

AUDIT_FEATURES = [
    {"key": "auth", "label": "登录与账号"},
    {"key": "survey", "label": "问卷分析"},
    {"key": "quant", "label": "定量分析"},
    {"key": "annotate", "label": "数据标注"},
    {"key": "comment", "label": "评论分析"},
    {"key": "report", "label": "报告导出与追问"},
    {"key": "settings", "label": "平台设置"},
    {"key": "admin", "label": "权限管理"},
]
AUDIT_FEATURE_LABELS = {item["key"]: item["label"] for item in AUDIT_FEATURES}


def _client_ip(request: Request | None) -> str:
    if not request:
        return ""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


def _audit_user(login: dict | None) -> dict:
    login = login or {}
    email = str(login.get("email", "")).strip().lower()
    open_id = str(login.get("open_id", "")).strip()
    user_key = f"email:{email}" if email else (f"open_id:{open_id}" if open_id else "anonymous")
    return {
        "user_key": user_key,
        "user_email": email,
        "user_name": str(login.get("name", "")).strip(),
        "open_id": open_id,
    }


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
