"""Owner-scoped append-only sidecar storage for asset review decisions.

The root is always supplied by the caller.  Persisted documents contain only
domain-separated scope keys, opaque tokens, and immutable package identity.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Any, Protocol, runtime_checkable
import unicodedata

from pydantic import ValidationError

from app.core.file_lock import acquire_exclusive_file_lock, release_file_lock
from app.schemas.questionnaire_asset_review_state import (
    MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS as _SCHEMA_MAX_EVENTS,
    QUESTIONNAIRE_ASSET_REVIEW_STATE_SCHEMA_VERSION,
    QuestionnaireAssetReviewCommand,
    QuestionnaireAssetReviewDecision,
    QuestionnaireAssetReviewEvent,
    QuestionnaireAssetReviewState,
)


MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS = _SCHEMA_MAX_EVENTS
MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES = 16 * 1024 * 1024
MAX_QUESTIONNAIRE_ASSET_REVIEW_SCOPE_BYTES = 4096
MAX_QUESTIONNAIRE_ASSET_REVIEW_PACKAGE_BYTES = 128 * 1024 * 1024

_NAMESPACE = ".asset-reviews-v1"
_DirectoryHandle = int | Path
_OWNER_SCOPE_DOMAIN = b"survey-web/questionnaire-asset-review/owner-scope/v1\0"
_COMMAND_HASH_DOMAIN = b"survey-web/questionnaire-asset-review/command/v1\0"
_EVENT_HASH_DOMAIN = b"survey-web/questionnaire-asset-review/event/v1\0"
_SHA256_LENGTH = 64
_STATE_KEYS = {
    "schema_version",
    "owner_scope_key",
    "snapshot_storage_key",
    "base_package_sha256",
    "base_package_size_bytes",
    "revision",
    "head_event_sha256",
    "events",
}
_EVENT_KEYS = {
    "revision",
    "idempotency_key",
    "reference_token",
    "asset_token",
    "decision",
    "reviewer_token",
    "recorded_at",
    "command_sha256",
    "previous_event_sha256",
    "event_sha256",
}


class QuestionnaireAssetReviewStorageError(RuntimeError):
    """Base error for unsafe, invalid, conflicting, or failed sidecar I/O."""


class QuestionnaireAssetReviewInvalidError(QuestionnaireAssetReviewStorageError):
    """Persisted sidecar content or its filesystem representation is invalid."""


class QuestionnaireAssetReviewConflictError(QuestionnaireAssetReviewStorageError):
    """The immutable base, idempotency key, or expected revision conflicts."""


class QuestionnaireAssetReviewInternalError(QuestionnaireAssetReviewStorageError):
    """A trusted clock or durable filesystem operation failed."""


class _ScopeThreadLock:
    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.users = 0


@runtime_checkable
class QuestionnaireAssetReviewStateStorage(Protocol):
    def load_state(
        self,
        owner_ref: str,
        snapshot_id: str,
        *,
        base_package_sha256: str,
        base_package_size_bytes: int,
    ) -> QuestionnaireAssetReviewState:
        ...

    def append(
        self,
        owner_ref: str,
        snapshot_id: str,
        command: QuestionnaireAssetReviewCommand,
    ) -> QuestionnaireAssetReviewState:
        ...


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:  # pragma: no cover - internal types
        raise QuestionnaireAssetReviewInternalError(
            "素材审阅状态无法规范序列化"
        ) from error


def _domain_hash(domain: bytes, value: Any) -> str:
    return _sha256(domain + _canonical_bytes(value))


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _require_scope(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise QuestionnaireAssetReviewStorageError(
            f"{label} 必须是稳定的非空字符串"
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise QuestionnaireAssetReviewStorageError(
            f"{label} 必须是有效 UTF-8"
        ) from error
    if len(encoded) > MAX_QUESTIONNAIRE_ASSET_REVIEW_SCOPE_BYTES:
        raise QuestionnaireAssetReviewStorageError(f"{label} 超过安全长度")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise QuestionnaireAssetReviewStorageError(f"{label} 不能包含控制字符")
    return value


def _require_sha256(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != _SHA256_LENGTH
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise QuestionnaireAssetReviewStorageError(f"{label} 必须是小写 SHA-256")
    return value


def _require_package_size(value: Any) -> int:
    if (
        type(value) is not int
        or value < 1
        or value > MAX_QUESTIONNAIRE_ASSET_REVIEW_PACKAGE_BYTES
    ):
        raise QuestionnaireAssetReviewStorageError(
            "base_package_size_bytes 超出安全范围"
        )
    return value


def _owner_scope_key(owner_ref: str) -> str:
    encoded = owner_ref.encode("utf-8")
    framed = len(encoded).to_bytes(8, "big") + encoded
    return _sha256(_OWNER_SCOPE_DOMAIN + framed)


def _snapshot_storage_key(snapshot_id: str) -> str:
    return _sha256(snapshot_id.encode("utf-8"))


def _command_value(
    *,
    idempotency_key: str,
    reference_token: str,
    asset_token: str,
    decision: QuestionnaireAssetReviewDecision,
    reviewer_token: str,
    base_package_sha256: str,
    base_package_size_bytes: int,
) -> dict[str, Any]:
    return {
        "asset_token": asset_token,
        "base_package_sha256": base_package_sha256,
        "base_package_size_bytes": base_package_size_bytes,
        "decision": decision.value,
        "idempotency_key": idempotency_key,
        "reference_token": reference_token,
        "reviewer_token": reviewer_token,
    }


def _command_sha256(command: QuestionnaireAssetReviewCommand) -> str:
    return _domain_hash(
        _COMMAND_HASH_DOMAIN,
        _command_value(
            idempotency_key=command.idempotency_key,
            reference_token=command.reference_token,
            asset_token=command.asset_token,
            decision=command.decision,
            reviewer_token=command.reviewer_token,
            base_package_sha256=command.base_package_sha256,
            base_package_size_bytes=command.base_package_size_bytes,
        ),
    )


def _event_value(event: QuestionnaireAssetReviewEvent) -> dict[str, Any]:
    value = event.model_dump(mode="json")
    value.pop("event_sha256")
    return value


def _event_sha256(event: QuestionnaireAssetReviewEvent) -> str:
    return _domain_hash(_EVENT_HASH_DOMAIN, _event_value(event))


class FileQuestionnaireAssetReviewStorage:
    """Explicit-root, owner-isolated, CAS-protected append-only JSON sidecar."""

    _scope_locks_guard = threading.Lock()
    _scope_locks: dict[tuple[Path, str, str], _ScopeThreadLock] = {}

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        if root is None:
            raise TypeError("root 必须显式提供")
        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValueError("root 必须是非空路径")
        self._root = Path(raw_root).absolute()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def root(self) -> Path:
        return self._root

    def _scope_keys(self, owner_ref: str, snapshot_id: str) -> tuple[str, str]:
        owner = _require_scope(owner_ref, "owner_ref")
        snapshot = _require_scope(snapshot_id, "snapshot_id")
        return _owner_scope_key(owner), _snapshot_storage_key(snapshot)

    def _state_path(self, owner_ref: str, snapshot_id: str) -> Path:
        owner_key, snapshot_key = self._scope_keys(owner_ref, snapshot_id)
        return self._root / _NAMESPACE / owner_key / f"{snapshot_key}.json"

    def _lock_path(self, owner_ref: str, snapshot_id: str) -> Path:
        owner_key, snapshot_key = self._scope_keys(owner_ref, snapshot_id)
        return self._root / _NAMESPACE / owner_key / f".{snapshot_key}.lock"

    @classmethod
    @contextmanager
    def _hold_thread_lock(
        cls,
        root: Path,
        owner_key: str,
        snapshot_key: str,
    ):
        key = (root, owner_key, snapshot_key)
        with cls._scope_locks_guard:
            entry = cls._scope_locks.get(key)
            if entry is None:
                entry = _ScopeThreadLock()
                cls._scope_locks[key] = entry
            entry.users += 1
        acquired = False
        try:
            entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with cls._scope_locks_guard:
                entry.users -= 1
                if entry.users == 0 and cls._scope_locks.get(key) is entry:
                    del cls._scope_locks[key]

    def _empty_state(
        self,
        owner_key: str,
        snapshot_key: str,
        base_sha256: str,
        base_size: int,
    ) -> QuestionnaireAssetReviewState:
        return QuestionnaireAssetReviewState(
            schema_version=QUESTIONNAIRE_ASSET_REVIEW_STATE_SCHEMA_VERSION,
            owner_scope_key=owner_key,
            snapshot_storage_key=snapshot_key,
            base_package_sha256=base_sha256,
            base_package_size_bytes=base_size,
            revision=0,
            head_event_sha256=None,
            events=(),
        )

    def load_state(
        self,
        owner_ref: str,
        snapshot_id: str,
        *,
        base_package_sha256: str,
        base_package_size_bytes: int,
    ) -> QuestionnaireAssetReviewState:
        owner_key, snapshot_key = self._scope_keys(owner_ref, snapshot_id)
        base_sha = _require_sha256(base_package_sha256, "base_package_sha256")
        base_size = _require_package_size(base_package_size_bytes)
        owner_directory = self._open_existing_owner_directory(owner_key)
        if owner_directory is None:
            return self._empty_state(owner_key, snapshot_key, base_sha, base_size)
        try:
            content = self._read_state_content(owner_directory, snapshot_key)
        finally:
            self._close_directory(owner_directory)
        if content is None:
            return self._empty_state(owner_key, snapshot_key, base_sha, base_size)
        state = self._decode_state(content, owner_key, snapshot_key)
        self._require_matching_base(state, base_sha, base_size)
        return state

    def _load_existing_state(
        self,
        owner_key: str,
        snapshot_key: str,
    ) -> QuestionnaireAssetReviewState | None:
        owner_directory = self._open_existing_owner_directory(owner_key)
        if owner_directory is None:
            return None
        try:
            content = self._read_state_content(owner_directory, snapshot_key)
        finally:
            self._close_directory(owner_directory)
        if content is None:
            return None
        return self._decode_state(content, owner_key, snapshot_key)

    def append(
        self,
        owner_ref: str,
        snapshot_id: str,
        command: QuestionnaireAssetReviewCommand,
    ) -> QuestionnaireAssetReviewState:
        owner_key, snapshot_key = self._scope_keys(owner_ref, snapshot_id)
        command = self._validated_command(command)
        incoming_sha = _command_sha256(command)
        preflight_state = self._load_existing_state(owner_key, snapshot_key)
        recorded_at: datetime | None = None
        if preflight_state is None:
            # A new scope must validate its trusted clock before creating even
            # the namespace or lock file.
            recorded_at = self._recorded_at()
        else:
            self._require_matching_base(
                preflight_state,
                command.base_package_sha256,
                command.base_package_size_bytes,
            )
        with self._hold_thread_lock(self._root, owner_key, snapshot_key):
            with self._exclusive_owner_directory(owner_key, snapshot_key) as directory:
                return self._append_locked(
                    directory,
                    owner_key,
                    snapshot_key,
                    command,
                    incoming_sha,
                    recorded_at,
                )

    def _append_locked(
        self,
        directory: _DirectoryHandle,
        owner_key: str,
        snapshot_key: str,
        command: QuestionnaireAssetReviewCommand,
        incoming_sha: str,
        recorded_at: datetime | None,
    ) -> QuestionnaireAssetReviewState:
        old_content = self._read_state_content(directory, snapshot_key)
        if old_content is None:
            state = self._empty_state(
                owner_key,
                snapshot_key,
                command.base_package_sha256,
                command.base_package_size_bytes,
            )
        else:
            state = self._decode_state(old_content, owner_key, snapshot_key)
        self._require_matching_base(
            state,
            command.base_package_sha256,
            command.base_package_size_bytes,
        )
        for event in state.events:
            if event.idempotency_key != command.idempotency_key:
                continue
            if event.command_sha256 == incoming_sha:
                # A previous attempt may have committed the event but failed
                # while syncing rollback-temp cleanup.  Re-syncing the owner
                # directory makes the retry a durability recovery point without
                # changing state bytes or mtime.
                try:
                    self._fsync_directory(directory)
                except OSError as error:
                    raise QuestionnaireAssetReviewInternalError(
                        "素材审阅幂等重放目录同步失败"
                    ) from error
                return state
            raise QuestionnaireAssetReviewConflictError(
                "幂等键已绑定到不同素材审阅命令"
            )
        if command.expected_revision != state.revision:
            raise QuestionnaireAssetReviewConflictError(
                "素材审阅 revision 已变化"
            )
        if state.revision >= MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS:
            raise QuestionnaireAssetReviewStorageError(
                "素材审阅事件数量达到安全上限"
            )
        if recorded_at is None or old_content is not None:
            recorded_at = self._recorded_at()
        if state.events and recorded_at < state.events[-1].recorded_at:
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅时钟早于上一条事件"
            )
        previous_sha = state.head_event_sha256 or state.base_package_sha256
        event_without_hash = QuestionnaireAssetReviewEvent(
            revision=state.revision + 1,
            idempotency_key=command.idempotency_key,
            reference_token=command.reference_token,
            asset_token=command.asset_token,
            decision=command.decision,
            reviewer_token=command.reviewer_token,
            recorded_at=recorded_at,
            command_sha256=incoming_sha,
            previous_event_sha256=previous_sha,
            event_sha256="0" * 64,
        )
        event = event_without_hash.model_copy(
            update={"event_sha256": _event_sha256(event_without_hash)}
        )
        new_state = QuestionnaireAssetReviewState(
            schema_version=QUESTIONNAIRE_ASSET_REVIEW_STATE_SCHEMA_VERSION,
            owner_scope_key=owner_key,
            snapshot_storage_key=snapshot_key,
            base_package_sha256=state.base_package_sha256,
            base_package_size_bytes=state.base_package_size_bytes,
            revision=event.revision,
            head_event_sha256=event.event_sha256,
            events=(*state.events, event),
        )
        new_content = _canonical_bytes(new_state.model_dump(mode="json"))
        if len(new_content) > MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES:
            raise QuestionnaireAssetReviewStorageError(
                "素材审阅状态超过安全保存上限"
            )
        self._atomic_replace_state(
            directory,
            f"{snapshot_key}.json",
            new_content,
            old_content,
        )
        return new_state

    @staticmethod
    def _validated_command(
        command: QuestionnaireAssetReviewCommand,
    ) -> QuestionnaireAssetReviewCommand:
        if not isinstance(command, QuestionnaireAssetReviewCommand):
            raise QuestionnaireAssetReviewStorageError("command 类型无效")
        try:
            return QuestionnaireAssetReviewCommand.model_validate(
                command.model_dump()
            )
        except ValidationError as error:
            raise QuestionnaireAssetReviewStorageError(
                "command 不符合严格契约"
            ) from error

    def _recorded_at(self) -> datetime:
        try:
            value = self._clock()
        except Exception as error:
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅时钟读取失败"
            ) from error
        if type(value) is not datetime or value.tzinfo is None:
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅时钟必须返回带时区的 datetime"
            )
        try:
            if value.utcoffset() is None:
                raise ValueError("missing UTC offset")
            return value.astimezone(timezone.utc)
        except Exception as error:
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅时钟必须返回有效时区"
            ) from error

    @staticmethod
    def _require_matching_base(
        state: QuestionnaireAssetReviewState,
        base_package_sha256: Any,
        base_package_size_bytes: Any,
    ) -> None:
        base_sha = _require_sha256(base_package_sha256, "base_package_sha256")
        base_size = _require_package_size(base_package_size_bytes)
        if (
            state.base_package_sha256 != base_sha
            or state.base_package_size_bytes != base_size
        ):
            raise QuestionnaireAssetReviewConflictError(
                "素材审阅基础快照身份不一致"
            )

    @classmethod
    def _decode_state(
        cls,
        content: bytes,
        owner_key: str,
        snapshot_key: str,
    ) -> QuestionnaireAssetReviewState:
        try:
            document = json.loads(
                content.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as error:
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅状态不是有效且无重复字段的 UTF-8 JSON"
            ) from error
        if not isinstance(document, dict) or set(document) != _STATE_KEYS:
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅状态字段不完整或包含未知字段"
            )
        events = document.get("events")
        if not isinstance(events, list):
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅 events 必须是数组"
            )
        if len(events) > MAX_QUESTIONNAIRE_ASSET_REVIEW_EVENTS:
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅事件数量超过安全上限"
            )
        for event in events:
            if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅事件字段不完整或包含未知字段"
                )
        try:
            state = QuestionnaireAssetReviewState.model_validate_json(content)
        except ValidationError as error:
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅状态不符合严格契约"
            ) from error
        if content != _canonical_bytes(state.model_dump(mode="json")):
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅状态不是规范 JSON"
            )
        if state.revision == 0:
            raise QuestionnaireAssetReviewInvalidError(
                "revision 0 状态只能由缺失 sidecar 在内存中表示"
            )
        if state.owner_scope_key != owner_key:
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅 owner scope 与读取路径不一致"
            )
        if state.snapshot_storage_key != snapshot_key:
            raise QuestionnaireAssetReviewInvalidError(
                "素材审阅 snapshot scope 与读取路径不一致"
            )
        cls._validate_hash_chain(state)
        return state

    @staticmethod
    def _validate_hash_chain(state: QuestionnaireAssetReviewState) -> None:
        previous_sha = state.base_package_sha256
        previous_recorded_at: datetime | None = None
        for event in state.events:
            expected_command_sha = _domain_hash(
                _COMMAND_HASH_DOMAIN,
                _command_value(
                    idempotency_key=event.idempotency_key,
                    reference_token=event.reference_token,
                    asset_token=event.asset_token,
                    decision=event.decision,
                    reviewer_token=event.reviewer_token,
                    base_package_sha256=state.base_package_sha256,
                    base_package_size_bytes=state.base_package_size_bytes,
                ),
            )
            if event.command_sha256 != expected_command_sha:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅命令哈希校验失败"
                )
            if event.previous_event_sha256 != previous_sha:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅事件哈希链断裂"
                )
            if event.event_sha256 != _event_sha256(event):
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅事件哈希校验失败"
                )
            if (
                previous_recorded_at is not None
                and event.recorded_at < previous_recorded_at
            ):
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅事件时间必须按 revision 单调递增"
                )
            previous_sha = event.event_sha256
            previous_recorded_at = event.recorded_at

    @staticmethod
    def _directory_flags() -> int:
        return (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

    @classmethod
    def _verify_directory(cls, descriptor: _DirectoryHandle, label: str) -> None:
        try:
            status = (
                descriptor.lstat()
                if isinstance(descriptor, Path)
                else os.fstat(descriptor)
            )
        except OSError as error:
            raise QuestionnaireAssetReviewStorageError(
                f"{label}状态读取失败"
            ) from error
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise QuestionnaireAssetReviewInvalidError(f"{label}必须是目录")

    @staticmethod
    def _close_directory(directory: _DirectoryHandle) -> None:
        if not isinstance(directory, Path):
            os.close(directory)

    def _open_root_directory(self, *, create: bool) -> _DirectoryHandle | None:
        if os.name == "nt":
            if create:
                try:
                    self._root.mkdir(parents=False, exist_ok=True)
                except OSError as error:
                    raise QuestionnaireAssetReviewInternalError(
                        "素材审阅根目录创建失败"
                    ) from error
            if not self._root.exists():
                return None
            self._verify_directory(self._root, "素材审阅根目录")
            return self._root
        if create:
            try:
                os.mkdir(self._root, 0o700)
            except FileExistsError:
                pass
            except OSError as error:
                raise QuestionnaireAssetReviewInternalError(
                    "素材审阅根目录创建失败"
                ) from error
        try:
            descriptor = os.open(self._root, self._directory_flags())
        except FileNotFoundError:
            if not create:
                return None
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅根目录不存在"
            )
        except OSError as error:
            raise QuestionnaireAssetReviewStorageError(
                "素材审阅根目录无法安全打开"
            ) from error
        try:
            self._verify_directory(descriptor, "素材审阅根目录")
            if create:
                self._fsync_root_parent()
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _fsync_root_parent(self) -> None:
        if os.name == "nt":
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            descriptor = os.open(self._root.parent, flags)
        except OSError as error:
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅根目录的父目录无法打开"
            ) from error
        try:
            self._verify_directory(descriptor, "素材审阅根目录的父目录")
            self._fsync_directory(descriptor)
        except QuestionnaireAssetReviewStorageError:
            raise
        except OSError as error:
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅根目录项同步失败"
            ) from error
        finally:
            os.close(descriptor)

    @classmethod
    def _open_child_directory(
        cls,
        parent: _DirectoryHandle,
        name: str,
        *,
        create: bool,
        label: str,
    ) -> _DirectoryHandle | None:
        if isinstance(parent, Path):
            child = parent / name
            if create:
                try:
                    child.mkdir(exist_ok=True)
                except OSError as error:
                    raise QuestionnaireAssetReviewInternalError(
                        f"{label}创建失败"
                    ) from error
            if not child.exists():
                return None
            cls._verify_directory(child, label)
            return child
        if create:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
            except OSError as error:
                raise QuestionnaireAssetReviewInternalError(
                    f"{label}创建失败"
                ) from error
        try:
            descriptor = os.open(name, cls._directory_flags(), dir_fd=parent)
        except FileNotFoundError:
            if not create:
                return None
            raise QuestionnaireAssetReviewInternalError(f"{label}不存在")
        except OSError as error:
            raise QuestionnaireAssetReviewStorageError(
                f"{label}无法安全打开"
            ) from error
        try:
            cls._verify_directory(descriptor, label)
            if create:
                cls._fsync_directory(parent)
        except OSError as error:
            os.close(descriptor)
            raise QuestionnaireAssetReviewInternalError(
                f"{label}的父目录项同步失败"
            ) from error
        except Exception:
            os.close(descriptor)
            raise
        return descriptor

    def _open_existing_owner_directory(
        self,
        owner_key: str,
    ) -> _DirectoryHandle | None:
        root = self._open_root_directory(create=False)
        if root is None:
            return None
        try:
            namespace = self._open_child_directory(
                root,
                _NAMESPACE,
                create=False,
                label="素材审阅命名空间目录",
            )
        finally:
            self._close_directory(root)
        if namespace is None:
            return None
        try:
            owner = self._open_child_directory(
                namespace,
                owner_key,
                create=False,
                label="素材审阅 owner 目录",
            )
        finally:
            self._close_directory(namespace)
        return owner

    def _open_or_create_owner_directory(self, owner_key: str) -> _DirectoryHandle:
        root = self._open_root_directory(create=True)
        assert root is not None
        try:
            namespace = self._open_child_directory(
                root,
                _NAMESPACE,
                create=True,
                label="素材审阅命名空间目录",
            )
        finally:
            self._close_directory(root)
        assert namespace is not None
        try:
            owner = self._open_child_directory(
                namespace,
                owner_key,
                create=True,
                label="素材审阅 owner 目录",
            )
        finally:
            self._close_directory(namespace)
        assert owner is not None
        return owner

    @contextmanager
    def _exclusive_owner_directory(self, owner_key: str, snapshot_key: str):
        directory = self._open_or_create_owner_directory(owner_key)
        lock_name = f".{snapshot_key}.lock"
        flags = (
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        lock_descriptor: int | None = None
        last_missing_error: FileNotFoundError | None = None
        for _ in range(8):
            try:
                if isinstance(directory, Path):
                    lock_descriptor = os.open(
                        directory / lock_name,
                        flags,
                        0o600,
                    )
                else:
                    lock_descriptor = os.open(
                        lock_name,
                        flags,
                        0o600,
                        dir_fd=directory,
                    )
                break
            except FileNotFoundError as error:
                # macOS may transiently return ENOENT when two processes both
                # perform the first O_CREAT|O_NOFOLLOW open.  Retrying the same
                # descriptor-relative, no-follow operation remains fail-closed.
                last_missing_error = error
            except OSError as error:
                self._close_directory(directory)
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅进程锁无法安全打开"
                ) from error
        if lock_descriptor is None:
            self._close_directory(directory)
            raise QuestionnaireAssetReviewStorageError(
                "素材审阅进程锁无法安全打开"
            ) from last_missing_error
        try:
            try:
                lock_status = os.fstat(lock_descriptor)
            except OSError as error:
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅进程锁状态读取失败"
                ) from error
            if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅进程锁必须是单链接普通文件"
                )
            lock_mode = stat.S_IMODE(lock_status.st_mode)
            if os.name != "nt" and lock_mode & 0o077:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅进程锁不能允许 owner 之外的访问"
                )
            try:
                if os.name != "nt" and lock_mode != 0o600:
                    os.fchmod(lock_descriptor, 0o600)
                acquire_exclusive_file_lock(lock_descriptor)
            except OSError as error:
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅进程锁获取失败"
                ) from error
            yield directory
        finally:
            try:
                release_file_lock(lock_descriptor)
            except OSError:
                pass
            os.close(lock_descriptor)
            self._close_directory(directory)

    @staticmethod
    def _read_state_content(
        directory: _DirectoryHandle,
        snapshot_key: str,
    ) -> bytes | None:
        name = f"{snapshot_key}.json"
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            if isinstance(directory, Path):
                descriptor = os.open(directory / name, flags)
            else:
                descriptor = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise QuestionnaireAssetReviewStorageError(
                "素材审阅状态无法安全打开"
            ) from error
        try:
            try:
                initial = os.fstat(descriptor)
            except OSError as error:
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅状态读取失败"
                ) from error
            if not stat.S_ISREG(initial.st_mode) or initial.st_nlink != 1:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅状态必须是单链接普通文件"
                )
            if os.name != "nt" and stat.S_IMODE(initial.st_mode) & 0o077:
                raise QuestionnaireAssetReviewInvalidError(
                    "素材审阅状态不能允许 owner 之外的访问"
                )
            if initial.st_size > MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES:
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅状态超过安全读取上限"
                )
            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = os.read(
                        descriptor,
                        min(
                            1024 * 1024,
                            MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES - total + 1,
                        ),
                    )
                except OSError as error:
                    raise QuestionnaireAssetReviewStorageError(
                        "素材审阅状态读取失败"
                    ) from error
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_QUESTIONNAIRE_ASSET_REVIEW_STATE_BYTES:
                    raise QuestionnaireAssetReviewStorageError(
                        "素材审阅状态超过安全读取上限"
                    )
                chunks.append(chunk)
            try:
                current = os.fstat(descriptor)
            except OSError as error:
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅状态读取失败"
                ) from error
            identity_fields = (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
            if total != initial.st_size or any(
                getattr(initial, field) != getattr(current, field)
                for field in identity_fields
            ):
                raise QuestionnaireAssetReviewStorageError(
                    "素材审阅状态在读取期间发生变化"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_all(descriptor: int, content: bytes) -> None:
        view = memoryview(content)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:  # pragma: no cover - defensive OS contract
                raise OSError("short write")
            written += count

    @staticmethod
    def _fsync_file(descriptor: int) -> None:
        os.fsync(descriptor)

    @staticmethod
    def _replace_file(
        source: str,
        target: str,
        directory: _DirectoryHandle,
    ) -> None:
        if isinstance(directory, Path):
            os.replace(directory / source, directory / target)
        else:
            os.replace(
                source,
                target,
                src_dir_fd=directory,
                dst_dir_fd=directory,
            )

    @staticmethod
    def _unlink_file(name: str, directory: _DirectoryHandle) -> None:
        if isinstance(directory, Path):
            os.unlink(directory / name)
        else:
            os.unlink(name, dir_fd=directory)

    @staticmethod
    def _fsync_directory(directory: _DirectoryHandle) -> None:
        if isinstance(directory, Path):
            return
        os.fsync(directory)

    @classmethod
    def _create_temporary_file(
        cls,
        directory: _DirectoryHandle,
        target_name: str,
    ) -> tuple[int, str]:
        flags = (
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_BINARY", 0)
        )
        for _ in range(32):
            name = (
                f".{secrets.token_hex(12)}.tmp"
                if isinstance(directory, Path)
                else f".{target_name}.{secrets.token_hex(16)}.tmp"
            )
            try:
                if isinstance(directory, Path):
                    descriptor = os.open(directory / name, flags, 0o600)
                else:
                    descriptor = os.open(name, flags, 0o600, dir_fd=directory)
            except FileExistsError:
                continue
            try:
                status = os.fstat(descriptor)
                if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
                    raise QuestionnaireAssetReviewInvalidError(
                        "素材审阅临时文件必须是单链接普通文件"
                    )
                if os.name != "nt":
                    os.fchmod(descriptor, 0o600)
            except Exception:
                os.close(descriptor)
                try:
                    cls._unlink_file(name, directory)
                except OSError:
                    pass
                raise
            return descriptor, name
        raise QuestionnaireAssetReviewInternalError("素材审阅临时文件名冲突")

    @classmethod
    def _write_temporary_file(
        cls,
        directory: _DirectoryHandle,
        target_name: str,
        content: bytes,
    ) -> str:
        descriptor, name = cls._create_temporary_file(directory, target_name)
        try:
            cls._write_all(descriptor, content)
            cls._fsync_file(descriptor)
        except Exception:
            os.close(descriptor)
            try:
                cls._unlink_file(name, directory)
            except OSError:
                pass
            raise
        os.close(descriptor)
        return name

    @classmethod
    def _atomic_replace_state(
        cls,
        directory: _DirectoryHandle,
        target_name: str,
        content: bytes,
        previous_content: bytes | None,
    ) -> None:
        new_temporary: str | None = None
        rollback_temporary: str | None = None
        replaced = False
        committed = False
        try:
            if previous_content is not None:
                rollback_temporary = cls._write_temporary_file(
                    directory,
                    target_name,
                    previous_content,
                )
            new_temporary = cls._write_temporary_file(
                directory,
                target_name,
                content,
            )
            cls._replace_file(new_temporary, target_name, directory)
            new_temporary = None
            replaced = True
            try:
                cls._fsync_directory(directory)
            except Exception as sync_error:
                try:
                    if rollback_temporary is not None:
                        cls._replace_file(
                            rollback_temporary,
                            target_name,
                            directory,
                        )
                        rollback_temporary = None
                    else:
                        cls._unlink_file(target_name, directory)
                    replaced = False
                    cls._fsync_directory(directory)
                except OSError as rollback_error:
                    raise QuestionnaireAssetReviewInternalError(
                        "素材审阅状态原子保存失败且无法回滚"
                    ) from rollback_error
                raise sync_error
            committed = True
            if rollback_temporary is not None:
                cls._unlink_file(rollback_temporary, directory)
                rollback_temporary = None
                cls._fsync_directory(directory)
        except Exception as error:
            if replaced and not committed:
                try:
                    if rollback_temporary is not None:
                        cls._replace_file(
                            rollback_temporary,
                            target_name,
                            directory,
                        )
                        rollback_temporary = None
                    else:
                        cls._unlink_file(target_name, directory)
                    try:
                        cls._fsync_directory(directory)
                    except OSError:
                        pass
                except OSError as rollback_error:
                    raise QuestionnaireAssetReviewInternalError(
                        "素材审阅状态原子保存失败且无法回滚"
                    ) from rollback_error
            cleanup_error: Exception | None = None
            cleaned_temporary = False
            for temporary in (new_temporary, rollback_temporary):
                if temporary is None:
                    continue
                try:
                    cls._unlink_file(temporary, directory)
                    cleaned_temporary = True
                except FileNotFoundError:
                    pass
                except Exception as temporary_error:
                    cleanup_error = temporary_error
            if cleaned_temporary and cleanup_error is None:
                try:
                    cls._fsync_directory(directory)
                except Exception as cleanup_sync_error:
                    cleanup_error = cleanup_sync_error
            if cleanup_error is not None:
                if committed:
                    raise QuestionnaireAssetReviewInternalError(
                        "素材审阅状态已提交，但临时文件清理失败"
                    ) from cleanup_error
                raise QuestionnaireAssetReviewInternalError(
                    "素材审阅状态原子保存失败且临时文件清理失败"
                ) from cleanup_error
            if committed:
                raise QuestionnaireAssetReviewInternalError(
                    "素材审阅状态已提交，但目录清理同步失败"
                ) from error
            if isinstance(error, QuestionnaireAssetReviewStorageError):
                raise
            raise QuestionnaireAssetReviewInternalError(
                "素材审阅状态原子保存失败"
            ) from error
