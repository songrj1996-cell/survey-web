"""storage/logins:web 登录态(web_logins.json)读写 + 内存同步。

web_logins 是进程内共享的 sid->login 字典;_sync_web_logins_from_disk 用磁盘内容覆盖它,
以支持多 worker。core/security 通过本模块读写登录态(security 不直接碰文件)。
"""
import json
import os
import time

from app.core.config import WEB_LOGINS_FILE


def _load_web_logins() -> dict[str, dict]:
    if not os.path.exists(WEB_LOGINS_FILE):
        return {}
    try:
        with open(WEB_LOGINS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        now = time.time()
        return {
            str(k): v for k, v in data.items()
            if isinstance(v, dict) and float(v.get("expires_at") or 0) > now
        }
    except Exception:
        return {}


def _save_web_logins() -> None:
    now = time.time()
    stale = [k for k, v in web_logins.items() if float(v.get("expires_at") or 0) <= now]
    for k in stale:
        web_logins.pop(k, None)
    with open(WEB_LOGINS_FILE, "w", encoding="utf-8") as f:
        json.dump(web_logins, f, ensure_ascii=False, indent=2)


web_logins: dict[str, dict] = _load_web_logins()


def _sync_web_logins_from_disk() -> None:
    """Keep multi-worker processes in sync with the persisted login store."""
    web_logins.clear()
    web_logins.update(_load_web_logins())
