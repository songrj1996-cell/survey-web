"""按用户隔离的 LLM 任务用量 JSON 持久化。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable

from app.core.config import USER_LLM_USAGE_FILE


_usage_lock = threading.RLock()


def load_llm_usage() -> dict[str, list[dict]]:
    with _usage_lock:
        if not os.path.exists(USER_LLM_USAGE_FILE):
            return {}
        try:
            with open(USER_LLM_USAGE_FILE, "r", encoding="utf-8") as file:
                payload = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        owners = payload.get("owners") if isinstance(payload, dict) else None
        if not isinstance(owners, dict):
            return {}
        return {
            str(owner): [dict(item) for item in records if isinstance(item, dict)]
            for owner, records in owners.items()
            if isinstance(records, list)
        }


def mutate_llm_usage(
    mutator: Callable[[dict[str, list[dict]]], None],
) -> None:
    """在进程锁内重新读取、修改并原子替换用量文件。"""
    with _usage_lock:
        owners = load_llm_usage()
        mutator(owners)
        parent = os.path.dirname(USER_LLM_USAGE_FILE) or "."
        os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".user_llm_usage.",
            suffix=".tmp",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(
                    {"version": 1, "owners": owners},
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, USER_LLM_USAGE_FILE)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
