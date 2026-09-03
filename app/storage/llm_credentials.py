"""用户 LLM 凭据密文的 JSON 持久化。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable

from app.core.config import USER_LLM_CREDENTIALS_FILE


_credentials_lock = threading.RLock()


def load_llm_credentials() -> dict[str, dict]:
    with _credentials_lock:
        if not os.path.exists(USER_LLM_CREDENTIALS_FILE):
            return {}
        try:
            with open(USER_LLM_CREDENTIALS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(owner): dict(record)
            for owner, record in data.items()
            if isinstance(record, dict)
        }


def mutate_llm_credentials(mutator: Callable[[dict[str, dict]], None]) -> None:
    """在进程锁内重新读取、修改并原子替换密文文件。"""
    with _credentials_lock:
        records = load_llm_credentials()
        mutator(records)
        parent = os.path.dirname(USER_LLM_CREDENTIALS_FILE) or "."
        os.makedirs(parent, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=".user_llm_credentials.",
            suffix=".tmp",
            dir=parent,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(records, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, USER_LLM_CREDENTIALS_FILE)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
