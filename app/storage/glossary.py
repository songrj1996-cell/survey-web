"""storage/glossary: multilingual glossary JSON persistence.

This module owns only filesystem concerns.  Missing files are represented by an
in-memory empty glossary; reads never create or repair the runtime file.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
import tempfile
import threading
import time
from typing import Any

from app.core.config import GLOSSARY_FILE


GLOSSARY_SCHEMA_VERSION = 1
_GLOSSARY_LOCK = threading.RLock()


class GlossaryStorageError(RuntimeError):
    """The persisted glossary cannot be read safely."""


class GlossaryRevisionConflict(RuntimeError):
    """The glossary changed after the caller loaded it."""


def empty_glossary() -> dict[str, Any]:
    return {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "revision": 0,
        "items": [],
    }


def _validate_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GlossaryStorageError("术语库存储格式无效")
    revision = value.get("revision", 0)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise GlossaryStorageError("术语库版本格式无效")
    items = value.get("items", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise GlossaryStorageError("术语库条目格式无效")
    schema_version = value.get("schema_version", GLOSSARY_SCHEMA_VERSION)
    if schema_version != GLOSSARY_SCHEMA_VERSION:
        raise GlossaryStorageError("术语库版本不受支持")
    return {
        "schema_version": GLOSSARY_SCHEMA_VERSION,
        "revision": revision,
        "items": deepcopy(items),
    }


def _load_unlocked() -> dict[str, Any]:
    if not os.path.exists(GLOSSARY_FILE):
        return empty_glossary()
    try:
        with open(GLOSSARY_FILE, "r", encoding="utf-8") as source:
            value = json.load(source)
    except json.JSONDecodeError as exc:
        raise GlossaryStorageError("术语库文件已损坏，请先恢复或修复该文件") from exc
    except OSError as exc:
        raise GlossaryStorageError("术语库文件读取失败") from exc
    return _validate_document(value)


def load_glossary() -> dict[str, Any]:
    """Load a defensive copy without creating a missing glossary file."""
    with _GLOSSARY_LOCK:
        return _load_unlocked()


def _atomic_write_json(value: dict[str, Any]) -> None:
    directory = os.path.dirname(GLOSSARY_FILE) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=directory,
            prefix=f".{os.path.basename(GLOSSARY_FILE)}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = temp_file.name
            json.dump(value, temp_file, ensure_ascii=False, indent=2)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, GLOSSARY_FILE)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.01 * (attempt + 1))
        temp_path = ""
    except OSError as exc:
        raise GlossaryStorageError("术语库文件保存失败") from exc
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except FileNotFoundError:
                pass


def save_glossary(value: dict[str, Any], *, expected_revision: int) -> dict[str, Any]:
    """Atomically save ``value`` when the persisted revision still matches."""
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise TypeError("expected_revision must be an integer")
    with _GLOSSARY_LOCK:
        current = _load_unlocked()
        if current["revision"] != expected_revision:
            raise GlossaryRevisionConflict(
                f"expected revision {expected_revision}, current revision {current['revision']}"
            )
        candidate = _validate_document({
            "schema_version": GLOSSARY_SCHEMA_VERSION,
            "revision": current["revision"] + 1,
            "items": value.get("items", []),
        })
        _atomic_write_json(candidate)
        return deepcopy(candidate)


def glossary_file_signature() -> tuple[str, int | None, int | None]:
    """Return a cheap cache signature while keeping the file as source of truth."""
    try:
        stat = os.stat(GLOSSARY_FILE)
    except FileNotFoundError:
        return (os.path.abspath(GLOSSARY_FILE), None, None)
    except OSError:
        return (os.path.abspath(GLOSSARY_FILE), -1, -1)
    return (os.path.abspath(GLOSSARY_FILE), stat.st_mtime_ns, stat.st_size)
