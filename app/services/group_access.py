"""Account-specific viewing scope and record access for group management only."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib

from fastapi import HTTPException

from app.core import security
from app.services.feature_access import owner_identity
from app.storage import group_workflow as store

_continuing_run = ContextVar("group_continuing_authorized_run", default=False)


def _preference_id(login):
    return hashlib.sha256(owner_identity(login)["owner_key"].encode()).hexdigest()


def admin_preferences(login):
    if not security._is_admin(login):
        return None
    try:
        saved = store.read("preferences", _preference_id(login)) or {}
    except (OSError, ValueError):
        raise HTTPException(503, "暂时无法读取个人查看范围，请稍后重试；本次未返回跨账号记录") from None
    if saved and (type(saved.get("only_self")) is not bool or type(saved.get("revision")) is not int):
        raise HTTPException(503, "个人查看范围记录异常，请联系管理员检查；本次未返回跨账号记录")
    return {"only_self": saved.get("only_self") is True, "revision": saved.get("revision", 0)}


def save_admin_preferences(login, request):
    if not security._is_admin(login):
        raise HTTPException(403, "仅管理员可以设置群组管理查看范围")
    def update(saved):
        if saved.get("revision", 0) != request.expected_revision:
            raise HTTPException(409, "查看范围已在其他页面更新，请重新打开设置后再试")
        saved.update(only_self=request.only_self, revision=request.expected_revision + 1,
                     updated_at=datetime.now(timezone.utc).isoformat())
        return {"only_self": saved["only_self"], "revision": saved["revision"]}
    return store.mutate("preferences", _preference_id(login), update, default={})


@dataclass(frozen=True)
class RecordScope:
    owner_key: str
    all_members: bool

    def can_read(self, record):
        return bool(record and (security._history_owner_key(record) == self.owner_key or self.all_members))

    def require_record(self, record, *, write=False):
        if not self.can_read(record):
            raise HTTPException(404, "记录不存在或无权访问")
        return record


def record_scope(login):
    owner = owner_identity(login)["owner_key"]
    admin = security._is_admin(login)
    # A display preference must not interrupt an already admitted background run.
    preferences = admin_preferences(login) if admin and not _continuing_run.get() else None
    return RecordScope(owner, admin and not (preferences or {}).get("only_self", False))


def can_read(record, login):
    return record_scope(login).can_read(record)


def require_record(record, login, *, write=False):
    return record_scope(login).require_record(record, write=write)


@contextmanager
def continue_authorized_run():
    token = _continuing_run.set(True)
    try:
        yield
    finally:
        _continuing_run.reset(token)
