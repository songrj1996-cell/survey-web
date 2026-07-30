"""storage/whitelist:白名单(whitelist.json)读写 + 旧权限结构迁移。"""
import json

from app.core.config import WHITELIST_FILE

_PERMS_SCHEMA_VERSION = 3


def _migrate_whitelist_perms(users: list[dict]) -> bool:
    """逐版本升级历史白名单，保留已有用户对新增功能的访问能力。
    v2 给已有访问权限的用户补 comment；v3 给 survey 用户补 interview。
    用 perms_v 标记已迁移，迁移后管理员再取消权限也不会被重新加上。
    返回是否发生改动（需要回写）。"""
    changed = False
    for u in users:
        version = u.get("perms_v", 1)
        if version < _PERMS_SCHEMA_VERSION:
            perms = list(u.get("perms", ["survey", "annotate"]))
            if version < 2 and perms and "comment" not in perms:
                perms.append("comment")
            if version < 3 and "survey" in perms and "interview" not in perms:
                perms.append("interview")
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
