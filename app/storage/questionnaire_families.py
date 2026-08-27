"""Owner-scoped persistence for questionnaire-family structure only."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from app.core.file_lock import acquire_exclusive_file_lock, release_file_lock
from app.schemas.questionnaire_families import QuestionnaireFamily


_MAX_FAMILY_BYTES = 20 * 1024 * 1024


class QuestionnaireFamilyStorageError(RuntimeError):
    pass


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise QuestionnaireFamilyStorageError(f"{label} 无效")
    return value


def _identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FileQuestionnaireFamilyStorage:
    """Atomic JSON storage that never contains response records or answer PII."""

    def __init__(self, root: str | os.PathLike[str]) -> None:
        try:
            path = Path(root).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            raise QuestionnaireFamilyStorageError("family storage root 无效") from None
        self._root = path

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, owner_ref: str, family_id: str) -> Path:
        owner = _required(owner_ref, "owner_ref")
        family = _required(family_id, "family_id")
        return (
            self._root
            / "questionnaire_families"
            / _identity(owner)
            / f"{_identity(family)}.json"
        )

    def _lock_path(self, owner_ref: str, family_id: str) -> Path:
        return self._path(owner_ref, family_id).with_suffix(".lock")

    @contextmanager
    def _lock(self, owner_ref: str, family_id: str) -> Iterator[None]:
        lock_path = self._lock_path(owner_ref, family_id)
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except OSError as error:
            raise QuestionnaireFamilyStorageError("family lock 创建失败") from error
        try:
            acquire_exclusive_file_lock(descriptor)
            yield
        except OSError as error:
            raise QuestionnaireFamilyStorageError("family lock 失败") from error
        finally:
            try:
                release_file_lock(descriptor)
            finally:
                os.close(descriptor)

    def save_family(self, family: QuestionnaireFamily) -> None:
        if not isinstance(family, QuestionnaireFamily):
            raise QuestionnaireFamilyStorageError("family 类型无效")
        try:
            content = family.model_dump_json(indent=2).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise QuestionnaireFamilyStorageError("family 序列化失败") from error
        if len(content) > _MAX_FAMILY_BYTES:
            raise QuestionnaireFamilyStorageError("family 超过存储大小限制")
        target = self._path(family.owner_ref, family.family_id)
        with self._lock(family.owner_ref, family.family_id):
            self._atomic_write(target, content)

    def load_family(
        self,
        owner_ref: str,
        family_id: str,
    ) -> QuestionnaireFamily | None:
        target = self._path(owner_ref, family_id)
        with self._lock(owner_ref, family_id):
            try:
                if not target.exists():
                    return None
                if not target.is_file() or target.stat().st_size > _MAX_FAMILY_BYTES:
                    raise QuestionnaireFamilyStorageError("family 文件无效")
                content = target.read_bytes()
            except QuestionnaireFamilyStorageError:
                raise
            except OSError as error:
                raise QuestionnaireFamilyStorageError("family 读取失败") from error
        try:
            payload = json.loads(content)
            family = QuestionnaireFamily.model_validate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as error:
            raise QuestionnaireFamilyStorageError("family 内容无效") from error
        if family.owner_ref != owner_ref or family.family_id != family_id:
            raise QuestionnaireFamilyStorageError("family scope 无效")
        return family

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        temporary_path = ""
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
            )
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = ""
        except OSError as error:
            raise QuestionnaireFamilyStorageError("family 原子保存失败") from error
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


__all__ = [
    "FileQuestionnaireFamilyStorage",
    "QuestionnaireFamilyStorageError",
]
