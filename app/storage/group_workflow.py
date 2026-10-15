"""Durable group-workflow documents; storage only, with process-safe atomic writes."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Callable

from app.core import config
from app.core.file_lock import acquire_exclusive_file_lock, release_file_lock

_LOCK = threading.RLock()
_LOCAL = threading.local()
_KINDS = {"tasks", "sources", "snapshots", "previews", "other_rewards", "ledger", "members", "imports"}


def _root() -> Path:
    return Path(config.GROUP_WORKFLOW_DATA_DIR)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("无效的记录标识")
    return value


def _path(kind: str, identifier: str) -> Path:
    if kind not in _KINDS:
        raise ValueError("无效的存储类型")
    return _root() / kind / f"{_identifier(identifier)}.json"


@contextmanager
def locked():
    """One lock order for all documents, including batch/claim commits."""
    with _LOCK:
        if getattr(_LOCAL, "depth", 0):
            _LOCAL.depth += 1
            try:
                yield
            finally:
                _LOCAL.depth -= 1
            return
        directory = _root()
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "workflow.lock").open("a+b") as handle:
            acquire_exclusive_file_lock(handle.fileno())
            _LOCAL.depth = 1
            try:
                yield
            finally:
                _LOCAL.depth = 0
                release_file_lock(handle.fileno())


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("存储记录结构异常，请联系管理员恢复，未覆盖原文件")
    return value


def _write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".save-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read(kind: str, identifier: str) -> dict | None:
    try:
        _identifier(identifier)
    except ValueError:
        return None
    with locked():
        return _read(_path(kind, identifier))


def list_records(kind: str) -> list[dict]:
    if kind not in _KINDS:
        raise ValueError("无效的存储类型")
    with locked():
        directory = _root() / kind
        return [_read(path) for path in sorted(directory.glob("*.json"))]


def create(kind: str, identifier: str, document: dict) -> dict:
    with locked():
        path = _path(kind, identifier)
        if path.exists():
            raise FileExistsError(identifier)
        _write(path, document)
        return deepcopy(document)


def mutate(kind: str, identifier: str, change: Callable[[dict], object], *, default=None):
    with locked():
        path = _path(kind, identifier)
        document = _read(path)
        if document is None:
            if default is None:
                raise FileNotFoundError(identifier)
            document = deepcopy(default)
        result = change(document)
        _write(path, document)
        return deepcopy(result)


def save_blob(identifier: str, content: bytes) -> str:
    """Immutable originals. Duplicate identical saves are harmless."""
    with locked():
        path = _root() / "uploads" / f"{_identifier(identifier)}.bin"
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content).hexdigest()
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise FileExistsError("原始文件已存在且内容不同")
            return digest
        with path.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        return digest


def read_blob(identifier: str) -> bytes:
    with locked():
        return (_root() / "uploads" / f"{_identifier(identifier)}.bin").read_bytes()


@contextmanager
def runner_lease():
    """Exactly one scheduler process per data directory; never hold the document lock."""
    directory = _root()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "runner.lock").open("a+b") as handle:
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(handle.fileno()).st_size < 1:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                release_file_lock(handle.fileno())
