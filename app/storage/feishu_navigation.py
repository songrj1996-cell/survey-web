"""Persistent, process-locked jobs for explicitly registered Feishu documents.

Identifiers, URLs, export title, notification recipient and task status are stored;
never report text or credentials. Recipients come only from the authenticated export.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import uuid

from app.core.config import (
    FEISHU_NAVIGATION_DATA_DIR,
    FEISHU_NAVIGATION_MAX_DOCUMENTS,
    FEISHU_NAVIGATION_COOLDOWN_SECONDS,
)
from app.core.file_lock import acquire_exclusive_file_lock, release_file_lock

_LOCK = threading.RLock()
_TERMINAL = {"completed", "completed_with_skips"}


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("invalid navigation identifier")
    return value


@contextmanager
def _registry():
    directory = Path(FEISHU_NAVIGATION_DATA_DIR)
    directory.mkdir(parents=True, exist_ok=True)
    with _LOCK, (directory / "registry.lock").open("a+b") as lock:
        acquire_exclusive_file_lock(lock.fileno())
        try:
            path = directory / "registry.json"
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "documents": {}}
            if state.get("version") != 1 or not isinstance(state.get("documents"), dict):
                raise ValueError("invalid navigation registry")
            before = deepcopy(state)
            yield state["documents"]
            if state != before:
                descriptor, name = tempfile.mkstemp(prefix=".registry-", suffix=".tmp", dir=directory)
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                        json.dump(state, output, ensure_ascii=False, separators=(",", ":"))
                        output.flush()
                        os.fsync(output.fileno())
                    os.replace(name, path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
        finally:
            release_file_lock(lock.fileno())


def register_document(doc_token: str, doc_url: str, *, recipient_open_id: str = "", title: str = "", now: float | None = None) -> bool:
    _identifier(doc_token)
    if recipient_open_id:
        _identifier(recipient_open_id)
        if not recipient_open_id.startswith("ou_"):
            raise ValueError("invalid navigation notification recipient")
    current = time.time() if now is None else now
    with _registry() as documents:
        if doc_token in documents:
            # Never reset completed jobs or change a registered document's origin.
            if documents[doc_token]["doc_url"] != doc_url:
                raise ValueError("navigation document origin changed")
            return False
        if len(documents) >= FEISHU_NAVIGATION_MAX_DOCUMENTS:
            raise ValueError("navigation registry capacity reached")
        documents[doc_token] = {
            "doc_token": doc_token, "doc_url": doc_url,
            "status": "pending", "subscribed": False, "attempts": 0,
            "next_at": current, "created_at": current, "updated_at": current,
            "lease_until": 0, "claim_id": "", "event_ids": [],
            "last_started_at": 0, "updated_blocks": 0,
            "rerun_requested": False,
            "recipient_open_id": recipient_open_id,
            "title": re.sub(r"[\r\n\t]+", " ", str(title))[:120],
            "notifications": {},
        }
        return True


def enqueue_event(doc_token: str, event_id: str, *, cooldown: float, now: float | None = None) -> bool:
    _identifier(doc_token)
    _identifier(event_id)
    current = time.time() if now is None else now
    with _registry() as documents:
        record = documents.get(doc_token)
        if not record or record["status"] in _TERMINAL or event_id in record["event_ids"]:
            return False
        record["event_ids"] = (record["event_ids"] + [event_id])[-32:]
        # Remember an event racing the current Wiki lookup. Only a successful
        # "not yet moved" lookup may schedule a follow-up; failures keep their
        # retry budget, and completed jobs ignore their own editing events.
        if record["status"] == "processing":
            record["rerun_requested"] = True
            return False
        if record["status"] == "pending":
            return False
        if current - record["last_started_at"] < cooldown:
            # Coalesce into one delayed check instead of losing a real move
            # that occurred immediately after the initial registration check.
            due = record["last_started_at"] + cooldown
        else:
            due = current
        record.update(status="pending", attempts=0, next_at=due, updated_at=current)
        return True


def claim_job(*, lease_seconds: float, max_attempts: int, notify: bool = False, now: float | None = None) -> dict | None:
    current = time.time() if now is None else now
    with _registry() as documents:
        for record in documents.values():
            expired = record["status"] == "processing" and record["lease_until"] <= current
            pending = record["status"] == "pending" and record["next_at"] <= current
            if not (expired or pending):
                continue
            if record["attempts"] >= max_attempts:
                record.update(status="failed", last_error="attempts_exhausted", updated_at=current)
                if notify:
                    _queue_notification(record, "failed", current)
                continue
            record.update(
                status="processing", claim_id=uuid.uuid4().hex,
                lease_until=current + lease_seconds, last_started_at=current,
                attempts=record["attempts"] + 1, updated_at=current,
                rerun_requested=False,
            )
            return deepcopy(record)
    return None


def finish_job(
    doc_token: str, claim_id: str, result: dict, *,
    followup_delay: float = FEISHU_NAVIGATION_COOLDOWN_SECONDS, now: float | None = None,
    notify: bool = False,
) -> bool:
    current = time.time() if now is None else now
    allowed = {"status", "subscribed", "next_at", "last_error", "wiki_url", "updated_blocks", "skipped_links"}
    if set(result) - allowed:
        raise ValueError("invalid navigation result")
    with _registry() as documents:
        record = documents.get(doc_token)
        if not record or record["status"] != "processing" or record["claim_id"] != claim_id:
            return False
        followup = result["status"] == "watching" and record.get("rerun_requested", False)
        record.update(result)
        if followup:
            record.update(
                status="pending", next_at=max(current, record["last_started_at"] + followup_delay),
                attempts=0,
            )
        record.update(claim_id="", lease_until=0, updated_at=current)
        if notify and record["status"] in _TERMINAL | {"failed"}:
            _queue_notification(record, record["status"], current)
        return True


def _queue_notification(record: dict, stage: str, current: float):
    if not record.get("recipient_open_id"):
        return  # Old/seed records have no known recipient; never infer one from readers.
    notices = record.setdefault("notifications", {})
    if stage in notices:
        return
    if stage != "started":
        # If repair finished before delivery, send the result instead of stale progress.
        for earlier in notices.values():
            if earlier["state"] == "pending":
                earlier["state"] = "superseded"
    notices[stage] = {
        "stage": stage, "id": uuid.uuid4().hex, "state": "pending",
        "created_at": current, "next_at": current, "attempts": 0,
        "claim_id": "", "lease_until": 0,
        "url": record.get("wiki_url") or record["doc_url"],
        "skipped_links": record.get("skipped_links", 0),
    }


def mark_navigation_started(doc_token: str, claim_id: str, wiki_url: str, *, notify: bool, now=None) -> bool:
    current = time.time() if now is None else now
    with _registry() as documents:
        record = documents.get(doc_token)
        if not record or record["status"] != "processing" or record["claim_id"] != claim_id:
            return False
        record["wiki_url"] = wiki_url
        if notify:
            _queue_notification(record, "started", current)
        return True


def claim_notification(*, lease_seconds: float, max_attempts: int, ttl_seconds: float, now=None) -> dict | None:
    current = time.time() if now is None else now
    with _registry() as documents:
        for record in documents.values():
            if not record.get("recipient_open_id"):
                continue
            notices = list(record.get("notifications", {}).values())
            # Do not race a start message still being sent by another worker.
            if any(n["state"] == "processing" and n["lease_until"] > current for n in notices):
                continue
            for notice in notices:
                if notice["state"] not in {"pending", "processing"}:
                    continue
                if current - notice["created_at"] >= ttl_seconds or notice["attempts"] >= max_attempts:
                    notice.update(state="failed", last_error="notification_budget_exhausted")
                    continue
                if notice["next_at"] > current:
                    continue
                notice.update(state="processing", claim_id=uuid.uuid4().hex,
                              lease_until=current + lease_seconds, attempts=notice["attempts"] + 1)
                return deepcopy({**notice, "doc_token": record["doc_token"],
                                 "recipient_open_id": record["recipient_open_id"], "title": record.get("title", "")})
    return None


def finish_notification(job: dict, *, sent: bool, retryable: bool, max_attempts: int,
                        delay: float, error: str = "", now=None) -> bool:
    current = time.time() if now is None else now
    with _registry() as documents:
        record = documents.get(job["doc_token"], {})
        notice = record.get("notifications", {}).get(job["stage"])
        if not notice or notice["state"] != "processing" or notice["claim_id"] != job["claim_id"]:
            return False
        retry = retryable and notice["attempts"] < max_attempts
        state = "sent" if sent else ("pending" if retry else "failed")
        # A newer outcome supersedes a failed/ambiguous attempt at an earlier progress message.
        stages = list(record["notifications"])
        if state == "pending" and stages.index(job["stage"]) < len(stages) - 1:
            state = "superseded"
        notice.update(state=state, next_at=current + delay, lease_until=0, claim_id="", last_error=error)
        return True


def list_records() -> list[dict]:
    with _registry() as documents:
        return deepcopy(list(documents.values()))
