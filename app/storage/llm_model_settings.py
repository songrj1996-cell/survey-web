"""Atomic model-settings JSON storage. Reads never create or repair files."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time
import uuid

from app.core import config


class ModelSettingsStorageError(Exception):
    pass


_thread_lock = threading.RLock()


def read_document() -> dict | None:
    path = Path(config.LLM_MODEL_SETTINGS_FILE)
    try:
        with path.open("rb") as source:
            data = source.read(config.LLM_MODEL_SETTINGS_MAX_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ModelSettingsStorageError("模型配置暂时无法读取，原文件未改动") from exc
    if len(data) > config.LLM_MODEL_SETTINGS_MAX_BYTES:
        raise ModelSettingsStorageError("模型配置文件超过允许大小，原文件未改动")
    try:
        value = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ModelSettingsStorageError("模型配置文件无法解析，请联系管理员核对备份；未覆盖原文件") from exc
    if not isinstance(value, dict):
        raise ModelSettingsStorageError("模型配置结构异常，未覆盖原文件")
    return value


@contextmanager
def _write_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        lock.seek(0, os.SEEK_END)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        started = time.monotonic()
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() - started >= config.LLM_MODEL_SETTINGS_LOCK_SECONDS:
                    raise ModelSettingsStorageError("模型配置正在被更新，请稍后重试；未覆盖原文件") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _update_document(update):
    path = Path(config.LLM_MODEL_SETTINGS_FILE)
    with _write_lock(path):
        value = update(read_document())
        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
        if len(data) > config.LLM_MODEL_SETTINGS_MAX_BYTES:
            raise ModelSettingsStorageError("配置历史超过允许大小，未覆盖原文件")
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise ModelSettingsStorageError("模型配置保存失败，请重试；原版本仍保留") from exc
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # A cleanup error must not hide the publication outcome.
        return value


def update_document(update):
    try:
        return _update_document(update)
    except OSError as exc:
        raise ModelSettingsStorageError("配置保存未能完成，请重新加载核对当前版本后再试") from exc
