"""storage/history:历史记录文件(history.json)读写 + 报告编号(R-NNN)维护。

只负责读写与编号;归属人过滤、可见性判断等逻辑在 core/security。
"""
import json
import os
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from app.core.config import HISTORY_FILE


_HISTORY_LOCK = threading.RLock()
_MutationResult = TypeVar("_MutationResult")


def _load_history_unlocked() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_history() -> list:
    with _HISTORY_LOCK:
        return _load_history_unlocked()


def _save_history_unlocked(history: list) -> None:
    target = Path(HISTORY_FILE)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, target)
    finally:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass


def _save_history(history: list) -> None:
    with _HISTORY_LOCK:
        _save_history_unlocked(history)


def mutate_history(callback: Callable[[list], _MutationResult]) -> _MutationResult:
    """Run one history read-modify-write transaction under the process lock.

    ``callback`` receives the loaded list and may mutate it in place. Its return
    value is passed back to the caller. If it raises, history is not written.
    """
    with _HISTORY_LOCK:
        history = _load_history_unlocked()
        result = callback(history)
        _save_history_unlocked(history)
        return result


def _history_no_value(report_no: str) -> int:
    m = re.match(r"^R-(\d+)$", str(report_no or "").strip())
    return int(m.group(1)) if m else 0


def _assign_history_report_numbers(history: list) -> bool:
    if not history:
        return False
    dirty = False
    used = {_history_no_value(h.get("report_no", "")) for h in history}
    used.discard(0)
    next_no = max(used or {0}) + 1
    missing = [h for h in history if not h.get("report_no")]
    missing.sort(key=lambda h: h.get("created_at", ""))
    for h in missing:
        while next_no in used:
            next_no += 1
        h["report_no"] = f"R-{next_no:03d}"
        used.add(next_no)
        dirty = True
    return dirty


def _load_history_with_report_numbers(*, seed_if_missing: list | None = None) -> list:
    """Load current history and persist missing report numbers under one lock."""
    with _HISTORY_LOCK:
        exists = os.path.exists(HISTORY_FILE)
        history = _load_history_unlocked() if exists else list(seed_if_missing or [])
        dirty = _assign_history_report_numbers(history)
        if dirty or (not exists and history):
            _save_history_unlocked(history)
        return history


def _ensure_history_report_numbers(history: list, *, save: bool = True) -> list:
    if not history or not any(not item.get("report_no") for item in history):
        return history
    if save:
        current = _load_history_with_report_numbers(seed_if_missing=history)
        history[:] = current
    else:
        _assign_history_report_numbers(history)
    return history


def _next_history_report_no(history: list) -> str:
    _ensure_history_report_numbers(history, save=False)
    max_no = max((_history_no_value(h.get("report_no", "")) for h in history), default=0)
    return f"R-{max_no + 1:03d}"
