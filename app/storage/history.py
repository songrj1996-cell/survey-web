"""storage/history:历史记录文件(history.json)读写 + 报告编号(R-NNN)维护。

只负责读写与编号;归属人过滤、可见性判断等逻辑在 core/security。
"""
import json
import os
import re

from app.core.config import HISTORY_FILE


def _load_history() -> list:
    if not os.path.exists(HISTORY_FILE):
        return []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_history(history: list) -> None:
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def _history_no_value(report_no: str) -> int:
    m = re.match(r"^R-(\d+)$", str(report_no or "").strip())
    return int(m.group(1)) if m else 0


def _ensure_history_report_numbers(history: list, *, save: bool = True) -> list:
    if not history:
        return history
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
    if dirty and save:
        _save_history(history)
    return history


def _next_history_report_no(history: list) -> str:
    _ensure_history_report_numbers(history, save=False)
    max_no = max((_history_no_value(h.get("report_no", "")) for h in history), default=0)
    return f"R-{max_no + 1:03d}"
