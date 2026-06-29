"""core/audit:审计事件的纯工具——特征列表、IP 取值、用户信息拼装。

落盘入口(audit_log / _audit_log_from_login)需要 storage + 登录态,已移至 services/audit。
"""
from fastapi import Request

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
