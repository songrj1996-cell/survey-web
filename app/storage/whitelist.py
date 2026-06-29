"""storage/whitelist:白名单(whitelist.json)读写 + 旧权限结构迁移。"""
import json

from app.core.config import WHITELIST_FILE

_PERMS_SCHEMA_VERSION = 2


def _migrate_whitelist_perms(users: list[dict]) -> bool:
    """一次性升级历史白名单：给已有访问权限(survey/annotate)的用户补 comment。
    用 perms_v 标记已迁移，迁移后管理员再取消 comment 也不会被重新加上。
    返回是否发生改动（需要回写）。"""
    changed = False
    for u in users:
        if u.get("perms_v", 1) < _PERMS_SCHEMA_VERSION:
            perms = list(u.get("perms", ["survey", "annotate"]))
            if perms and "comment" not in perms:
                perms.append("comment")
            u["perms"] = perms
            u["perms_v"] = _PERMS_SCHEMA_VERSION
            changed = True
    return changed


def _load_whitelist() -> list[dict]:
    try:
        with open(WHITELIST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        users = data.get("users", []) if isinstance(data, dict) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    if _migrate_whitelist_perms(users):
        _save_whitelist(users)
    return users


def _save_whitelist(users: list[dict]) -> None:
    with open(WHITELIST_FILE, "w", encoding="utf-8") as f:
        json.dump({"users": users}, f, ensure_ascii=False, indent=2)
