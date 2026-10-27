"""按用户隔离的 LLM 任务用量 JSON 持久化。"""
from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
from collections.abc import Callable
from time import sleep

from app.core.config import USER_LLM_USAGE_FILE
from app.core.runtime_logging import runtime_log


_usage_lock = threading.RLock()
_FILE_RETRY_DELAYS = (0.02, 0.05)


class UsageStorageError(RuntimeError):
    """The usage file could not be safely read or updated."""


def _storage_error(stage: str, error: Exception) -> UsageStorageError:
    # Do not log exception messages: JSON/OS errors may contain private values.
    runtime_log(
        "用量记录", "error",
        "用量记录文件处理失败，原文件未被本次操作覆盖；请管理员检查文件权限、占用和格式。"
        "本次用量可能未完整保存，不能据此判断为零消耗",
        stage=stage, path=USER_LLM_USAGE_FILE, error_type=type(error).__name__,
        **{"系统错误码": getattr(error, "errno", None), "Windows 错误码": getattr(error, "winerror", None)},
    )
    return UsageStorageError("用量记录暂时不可用，原文件已保留")


def _with_file_retry(operation: Callable, stage: str):
    """Retry only permission/sharing failures, never the model or whole task."""
    for attempt in range(len(_FILE_RETRY_DELAYS) + 1):
        try:
            result = operation()
        except OSError as exc:
            retryable = (
                isinstance(exc, PermissionError)
                or exc.errno in {errno.EACCES, errno.EBUSY}
                or getattr(exc, "winerror", None) in {5, 32, 33}
            )
            if not retryable or attempt == len(_FILE_RETRY_DELAYS):
                raise _storage_error(stage, exc) from exc
            sleep(_FILE_RETRY_DELAYS[attempt])
        else:
            if attempt:
                runtime_log("用量记录", "info", "用量记录文件本地重试后恢复",
                            stage=stage, path=USER_LLM_USAGE_FILE, attempt=attempt + 1)
            return result


def load_llm_usage() -> dict[str, list[dict]]:
    with _usage_lock:
        missing = object()

        def read():
            try:
                with open(USER_LLM_USAGE_FILE, "r", encoding="utf-8") as file:
                    return json.load(file)
            except FileNotFoundError:
                return missing

        try:
            payload = _with_file_retry(read, "读取用量文件")
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise _storage_error("校验用量文件", exc) from exc
        if payload is missing:
            return {}
        owners = payload.get("owners") if isinstance(payload, dict) else None
        if not isinstance(owners, dict) or any(
            not isinstance(records, list) or any(not isinstance(item, dict) for item in records)
            for records in owners.values()
        ):
            raise _storage_error("校验用量文件", ValueError("Invalid usage structure"))
        return {owner: [dict(item) for item in records] for owner, records in owners.items()}


def mutate_llm_usage(
    mutator: Callable[[dict[str, list[dict]]], None],
) -> None:
    """在进程锁内重新读取、修改并原子替换用量文件。"""
    with _usage_lock:
        owners = load_llm_usage()
        mutator(owners)
        parent = os.path.dirname(USER_LLM_USAGE_FILE) or "."
        temp_path = None
        try:
            os.makedirs(parent, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=".user_llm_usage.",
                suffix=".tmp",
                dir=parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(
                    {"version": 1, "owners": owners},
                    file,
                    ensure_ascii=False,
                    indent=2,
                )
                file.flush()
                os.fsync(file.fileno())
            _with_file_retry(lambda: os.replace(temp_path, USER_LLM_USAGE_FILE), "保存用量文件")
        except (OSError, TypeError, ValueError) as exc:
            raise _storage_error("准备保存用量文件", exc) from exc
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass  # Successful replacement has consumed this temp file.
                except OSError as exc:
                    runtime_log("用量记录", "warning", "用量临时文件未能清理，请管理员检查文件占用；此提示不改变保存结果",
                                stage="清理用量临时文件", path=temp_path, error_type=type(exc).__name__)
