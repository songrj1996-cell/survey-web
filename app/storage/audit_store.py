"""storage/audit_store:审计日志文件(audit_logs.json)的读取 / 追加 / 裁剪。

只负责读写;审计事件的构造(获取 IP、组装 user、组装 entry)在 core/audit。
"""
import json
import os

from app.core.config import AUDIT_LOG_FILE, MAX_AUDIT_LOGS


def _load_audit_logs() -> list[dict]:
    if not os.path.exists(AUDIT_LOG_FILE):
        return []
    try:
        with open(AUDIT_LOG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("logs", [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_audit_logs(logs: list[dict]) -> None:
    with open(AUDIT_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs[:MAX_AUDIT_LOGS], f, ensure_ascii=False, indent=2)


def _append_audit_log(entry: dict) -> None:
    try:
        logs = _load_audit_logs()
        logs.insert(0, entry)
        _save_audit_logs(logs)
    except Exception as exc:
        print(f"[audit] write failed: {exc}", flush=True)
