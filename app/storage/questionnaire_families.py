"""Owner-scoped persistence for questionnaire-family structure only."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, NamedTuple

from pydantic import ValidationError

from app.core.file_lock import acquire_exclusive_file_lock, release_file_lock
from app.schemas.questionnaire_families import QuestionnaireFamily


_MAX_FAMILY_BYTES = 20 * 1024 * 1024
_MAX_LIST_LIMIT = 50
_FAMILY_FILENAME_PATTERN = re.compile(r"^([0-9a-f]{64})\.json$")


class QuestionnaireFamilyStorageError(RuntimeError):
    pass


class QuestionnaireFamilyCatalogInvalidError(QuestionnaireFamilyStorageError):
    pass


class QuestionnaireFamilyPage(NamedTuple):
    families: tuple[QuestionnaireFamily, ...]
    next_cursor: str | None


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

    def _owner_directory(self, owner_ref: str) -> Path:
        owner = _required(owner_ref, "owner_ref")
        return self._root / "questionnaire_families" / _identity(owner)

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

    def list_families(
        self,
        owner_ref: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> QuestionnaireFamilyPage:
        owner = _required(owner_ref, "owner_ref")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _MAX_LIST_LIMIT
        ):
            raise QuestionnaireFamilyCatalogInvalidError(
                "limit 必须是 1 到 50 的整数"
            )
        cursor_key = self._decode_cursor(cursor) if cursor is not None else None
        directory = self._owner_directory(owner)
        try:
            entries = os.scandir(directory)
        except FileNotFoundError:
            return QuestionnaireFamilyPage(families=(), next_cursor=None)
        except OSError as error:
            raise QuestionnaireFamilyStorageError("family 目录枚举失败") from error

        families: list[QuestionnaireFamily] = []
        with entries:
            for entry in entries:
                match = _FAMILY_FILENAME_PATTERN.fullmatch(entry.name)
                if match is None:
                    if entry.name.endswith(".json"):
                        raise QuestionnaireFamilyStorageError("family 文件名无效")
                    continue
                try:
                    entry_status = entry.stat(follow_symlinks=False)
                    mode = entry_status.st_mode
                    if not stat.S_ISREG(mode):
                        raise QuestionnaireFamilyStorageError("family 目录项无效")
                    if entry_status.st_size > _MAX_FAMILY_BYTES:
                        raise QuestionnaireFamilyStorageError("family 文件无效")
                    content = Path(entry.path).read_bytes()
                except QuestionnaireFamilyStorageError:
                    raise
                except OSError as error:
                    raise QuestionnaireFamilyStorageError("family 读取失败") from error
                try:
                    payload = json.loads(content)
                    family = QuestionnaireFamily.model_validate(payload)
                except (
                    UnicodeDecodeError,
                    json.JSONDecodeError,
                    ValidationError,
                    ValueError,
                ) as error:
                    raise QuestionnaireFamilyStorageError("family 内容无效") from error
                if (
                    family.owner_ref != owner
                    or _identity(family.family_id) != match.group(1)
                    or family.updated_at.utcoffset() is None
                ):
                    raise QuestionnaireFamilyStorageError("family scope 无效")
                families.append(family)

        families.sort(key=self._sort_key, reverse=True)
        if cursor_key is not None:
            families = [
                family
                for family in families
                if self._sort_key(family) < cursor_key
            ]
        has_more = len(families) > limit
        page = families[:limit]
        return QuestionnaireFamilyPage(
            families=tuple(page),
            next_cursor=(self._encode_cursor(page[-1]) if has_more else None),
        )

    @staticmethod
    def _sort_key(family: QuestionnaireFamily) -> tuple[datetime, str]:
        return family.updated_at, family.family_id

    @staticmethod
    def _encode_cursor(family: QuestionnaireFamily) -> str:
        payload = json.dumps(
            [family.updated_at.isoformat(), family.family_id],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> tuple[datetime, str]:
        if (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > 512
            or re.fullmatch(r"[A-Za-z0-9_-]+", cursor) is None
        ):
            raise QuestionnaireFamilyCatalogInvalidError("cursor 无效")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            decoded = base64.b64decode(
                padded.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(decoded.decode("ascii"))
            if (
                not isinstance(payload, list)
                or len(payload) != 2
                or not all(isinstance(value, str) for value in payload)
                or not payload[1]
                or len(payload[1]) > 128
            ):
                raise ValueError
            updated_at = datetime.fromisoformat(payload[0])
            if updated_at.utcoffset() is None:
                raise ValueError
            return updated_at, payload[1]
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            raise QuestionnaireFamilyCatalogInvalidError("cursor 无效") from None

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
    "QuestionnaireFamilyCatalogInvalidError",
    "QuestionnaireFamilyPage",
    "QuestionnaireFamilyStorageError",
]
