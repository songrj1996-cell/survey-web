"""调研素材聚合的本地原子存储与可移植快照包。

所有文件根目录都必须由调用方显式注入；本模块没有 ``data/`` 默认路径。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, BinaryIO, NamedTuple, Protocol, runtime_checkable
import zipfile

from pydantic import ValidationError

from app.core.file_lock import acquire_exclusive_file_lock, release_file_lock
from app.core.research_assets import (
    ResearchContractError,
    canonical_json,
    validate_research_contract,
)
from app.schemas.questionnaire import (
    CollectionState,
    MappingStatus,
    QuestionnaireSnapshot,
    QuestionnaireSourceMode,
)
from app.schemas.research_assets import (
    MediaType,
    Provider,
    ResearchAssetCollection,
)


_STORAGE_SCHEMA_VERSION = 1
SNAPSHOT_PACKAGE_SCHEMA_VERSION = 1
SNAPSHOT_PACKAGE_MAX_MEMBERS = 512
SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
SNAPSHOT_PACKAGE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
SNAPSHOT_PACKAGE_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_STORED_BUNDLE_BYTES = 64 * 1024 * 1024
_SNAPSHOT_CATALOG_MAX_LIMIT = 50
_SNAPSHOT_CATALOG_MAX_PAGE_METADATA_BYTES = 128 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SNAPSHOT_PACKAGE_FILENAME_PATTERN = re.compile(r"^([0-9a-f]{64})\.zip$")
_MANIFEST_PATH = "manifest.json"

_DirectoryHandle = int | Path


class ResearchAssetStorageError(RuntimeError):
    """快照持久化内容无效、损坏或无法安全读写。"""


class SnapshotConflictError(ResearchAssetStorageError):
    """同一 owner 与 snapshot_id 已存在不同的不可变快照内容。"""


class SnapshotPackageError(ResearchAssetStorageError):
    """API JSON 与媒体快照包不满足完整性或安全约束。"""


class ResearchAssetBundle(NamedTuple):
    """必须作为一个事务整体读取和保存的问卷快照与素材集合。"""

    snapshot: QuestionnaireSnapshot
    collection: ResearchAssetCollection


class SnapshotPackage(NamedTuple):
    """已校验的快照聚合与按内容哈希索引的媒体字节。"""

    bundle: ResearchAssetBundle
    media: dict[str, bytes]


@dataclass(frozen=True, slots=True)
class StoredSnapshotPackage:
    """从同一份持久化 ZIP 字节得到的快照内容与不可变身份。"""

    package: SnapshotPackage
    package_sha256: str
    archive_size_bytes: int


class SnapshotCatalogEntry(NamedTuple):
    """不持有 bundle 或媒体内容的 owner-scoped 快照目录元数据。"""

    owner_ref: str
    storage_key: str
    snapshot_id: str
    provider: Provider
    source_mode: QuestionnaireSourceMode
    collection_state: CollectionState
    mapping_status: MappingStatus
    item_count: int
    question_count: int
    asset_count: int
    image_asset_count: int
    asset_reference_count: int


class SnapshotCatalogPage(NamedTuple):
    """按不透明存储键分页、只持有轻量条目的快照目录。"""

    entries: tuple[SnapshotCatalogEntry, ...]
    next_cursor: str | None

    @property
    def snapshot_ids(self) -> tuple[str, ...]:
        return tuple(entry.snapshot_id for entry in self.entries)


@runtime_checkable
class ResearchAssetStorage(Protocol):
    """按用户隔离、以聚合为原子边界的最小同步存储端口。

    实现必须拒绝空 ``owner_ref``，并在保存前确认它与
    ``bundle.collection.owner_ref`` 一致；快照与素材集合不得分步提交。
    """

    def load_bundle(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> ResearchAssetBundle | None:
        ...

    def save_bundle(
        self,
        owner_ref: str,
        bundle: ResearchAssetBundle,
    ) -> None:
        ...


@runtime_checkable
class ResearchSnapshotStorage(Protocol):
    """按用户隔离、以确定性 ZIP 单文件保存完整离线快照的端口。"""

    def load_snapshot_package(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> SnapshotPackage | None:
        ...

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        ...


@runtime_checkable
class ResearchSnapshotIdentityStorage(Protocol):
    """读取实际持久化 ZIP 内容及其字节级身份的独立端口。"""

    def load_snapshot_package_with_identity(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> StoredSnapshotPackage | None:
        ...


@runtime_checkable
class ResearchSnapshotCatalogStorage(ResearchSnapshotStorage, Protocol):
    """在旧快照端口上增加 owner 隔离的只读目录能力。"""

    def list_snapshot_catalog(
        self,
        owner_ref: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SnapshotCatalogPage:
        ...


def _require_nonblank(
    value: Any,
    label: str,
    error_type: type[ResearchAssetStorageError],
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{label} 不能为空")
    return value.strip()


def _require_stable_id(
    value: Any,
    label: str,
    error_type: type[ResearchAssetStorageError],
) -> str:
    normalized = _require_nonblank(value, label, error_type)
    if value != normalized:
        raise error_type(f"{label} 不能包含首尾空白")
    return normalized


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _bundle_value(bundle: ResearchAssetBundle) -> dict[str, Any]:
    return {
        "snapshot": bundle.snapshot.model_dump(mode="json"),
        "collection": bundle.collection.model_dump(mode="json"),
    }


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


def _load_json_bytes(
    content: bytes,
    label: str,
    error_type: type[ResearchAssetStorageError],
) -> Any:
    try:
        return json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise error_type(f"{label} 不是有效且无重复字段的 UTF-8 JSON") from error


def _require_exact_keys(
    value: Any,
    expected: set[str],
    label: str,
    error_type: type[ResearchAssetStorageError],
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise error_type(f"{label} 字段不完整或包含未知字段")
    return value


def _validated_bundle(
    owner_ref: str,
    bundle: ResearchAssetBundle,
    error_type: type[ResearchAssetStorageError],
) -> str:
    owner = _require_nonblank(owner_ref, "owner_ref", error_type)
    if not isinstance(bundle, ResearchAssetBundle):
        raise error_type("bundle 类型无效")
    if not isinstance(bundle.snapshot, QuestionnaireSnapshot):
        raise error_type("bundle.snapshot 类型无效")
    if not isinstance(bundle.collection, ResearchAssetCollection):
        raise error_type("bundle.collection 类型无效")
    if bundle.collection.owner_ref != owner:
        raise error_type("owner_ref 与 bundle.collection.owner_ref 不一致")
    _require_stable_id(bundle.snapshot.snapshot_id, "snapshot_id", error_type)
    try:
        validate_research_contract(bundle.snapshot, bundle.collection)
    except (ResearchContractError, ValidationError, ValueError) as error:
        raise error_type(f"快照聚合校验失败：{error}") from error
    return owner


def _bundle_from_value(
    value: Any,
    label: str,
    error_type: type[ResearchAssetStorageError],
) -> ResearchAssetBundle:
    document = _require_exact_keys(
        value,
        {"snapshot", "collection"},
        label,
        error_type,
    )
    try:
        snapshot = QuestionnaireSnapshot.model_validate(document["snapshot"])
        collection = ResearchAssetCollection.model_validate(document["collection"])
    except ValidationError as error:
        raise error_type(f"{label} 不符合快照契约") from error
    return ResearchAssetBundle(snapshot=snapshot, collection=collection)


def _identity_hash(value: str) -> str:
    return _sha256(value.encode("utf-8"))


class FileResearchAssetStorage:
    """显式根目录下、按 owner 隔离的不可变 Bundle 与 ZIP 快照存储。"""

    _locks_guard = threading.Lock()
    _root_locks: dict[Path, threading.RLock] = {}

    def __init__(self, root: str | os.PathLike[str]):
        if root is None:
            raise TypeError("root 必须显式提供")
        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValueError("root 必须是非空路径")
        self._root = Path(raw_root).absolute()
        with self._locks_guard:
            self._lock = self._root_locks.setdefault(
                self._root,
                threading.RLock(),
            )

    @property
    def root(self) -> Path:
        return self._root

    def _bundle_path(self, owner_ref: str, snapshot_id: str) -> Path:
        owner = _require_nonblank(
            owner_ref,
            "owner_ref",
            ResearchAssetStorageError,
        )
        snapshot = _require_stable_id(
            snapshot_id,
            "snapshot_id",
            ResearchAssetStorageError,
        )
        return self._root / _identity_hash(owner) / f"{_identity_hash(snapshot)}.json"

    def _package_path(self, owner_ref: str, snapshot_id: str) -> Path:
        owner = _require_nonblank(
            owner_ref,
            "owner_ref",
            ResearchAssetStorageError,
        )
        snapshot = _require_stable_id(
            snapshot_id,
            "snapshot_id",
            ResearchAssetStorageError,
        )
        return self._root / _identity_hash(owner) / f"{_identity_hash(snapshot)}.zip"

    def _lock_path(self, owner_ref: str, snapshot_id: str) -> Path:
        owner = _require_nonblank(
            owner_ref,
            "owner_ref",
            ResearchAssetStorageError,
        )
        snapshot = _require_stable_id(
            snapshot_id,
            "snapshot_id",
            ResearchAssetStorageError,
        )
        return (
            self._root
            / _identity_hash(owner)
            / f".{_identity_hash(snapshot)}.lock"
        )

    @staticmethod
    def _bundle_identity(bundle: ResearchAssetBundle) -> bytes:
        return _canonical_bytes(_bundle_value(bundle))

    @classmethod
    def _same_bundle(
        cls,
        first: ResearchAssetBundle,
        second: ResearchAssetBundle,
    ) -> bool:
        return cls._bundle_identity(first) == cls._bundle_identity(second)

    @classmethod
    def _same_package(
        cls,
        first: SnapshotPackage,
        second: SnapshotPackage,
    ) -> bool:
        return cls._same_bundle(first.bundle, second.bundle) and first.media == second.media

    @staticmethod
    def _read_bytes(
        target: Path,
        *,
        max_bytes: int,
        label: str,
        error_type: type[ResearchAssetStorageError] = ResearchAssetStorageError,
    ) -> bytes | None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            descriptor = os.open(target, flags)
        except FileNotFoundError:
            return None
        except OSError as error:
            raise error_type(f"{label}打开失败") from error
        try:
            try:
                initial_status = os.fstat(descriptor)
            except OSError as error:
                raise error_type(f"{label}状态读取失败") from error
            if not stat.S_ISREG(initial_status.st_mode):
                raise error_type(f"{label}必须是普通文件")
            if initial_status.st_nlink != 1:
                raise error_type(f"{label}不能是硬链接")
            if initial_status.st_size > max_bytes:
                raise error_type(f"{label}超过安全读取上限")

            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = os.read(
                        descriptor,
                        min(1024 * 1024, max_bytes - total + 1),
                    )
                except OSError as error:
                    raise error_type(f"{label}读取失败") from error
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise error_type(f"{label}超过安全读取上限")
                chunks.append(chunk)

            try:
                final_status = os.fstat(descriptor)
            except OSError as error:
                raise error_type(f"{label}状态读取失败") from error
            try:
                path_status = os.stat(target, follow_symlinks=False)
            except OSError as error:
                raise error_type(f"{label}在读取期间发生变化") from error
            if (
                total != initial_status.st_size
                or final_status.st_dev != initial_status.st_dev
                or final_status.st_ino != initial_status.st_ino
                or final_status.st_mode != initial_status.st_mode
                or final_status.st_size != initial_status.st_size
                or final_status.st_mtime_ns != initial_status.st_mtime_ns
                or final_status.st_ctime_ns != initial_status.st_ctime_ns
                or final_status.st_nlink != 1
                or path_status.st_dev != initial_status.st_dev
                or path_status.st_ino != initial_status.st_ino
                or path_status.st_mode != initial_status.st_mode
                or path_status.st_size != initial_status.st_size
                or path_status.st_mtime_ns != initial_status.st_mtime_ns
                or (
                    os.name != "nt"
                    and path_status.st_ctime_ns != initial_status.st_ctime_ns
                )
            ):
                raise error_type(f"{label}在读取期间发生变化")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _load_bundle_file(
        self,
        owner: str,
        snapshot_id: str,
    ) -> ResearchAssetBundle | None:
        content = self._read_bytes(
            self._bundle_path(owner, snapshot_id),
            max_bytes=_MAX_STORED_BUNDLE_BYTES,
            label="快照文件",
        )
        if content is None:
            return None

        return self._bundle_from_stored_content(owner, snapshot_id, content)

    @staticmethod
    def _bundle_from_stored_content(
        owner: str,
        snapshot_id: str,
        content: bytes,
    ) -> ResearchAssetBundle:
        envelope = _require_exact_keys(
            _load_json_bytes(
                content,
                "快照文件",
                ResearchAssetStorageError,
            ),
            {
                "schema_version",
                "owner_ref",
                "snapshot_id",
                "bundle_sha256",
                "bundle",
            },
            "快照文件",
            ResearchAssetStorageError,
        )
        if (
            isinstance(envelope["schema_version"], bool)
            or envelope["schema_version"] != _STORAGE_SCHEMA_VERSION
        ):
            raise ResearchAssetStorageError("快照存储版本不受支持")
        if envelope["owner_ref"] != owner:
            raise ResearchAssetStorageError("快照 owner_ref 与读取范围不一致")
        if envelope["snapshot_id"] != snapshot_id:
            raise ResearchAssetStorageError("快照 ID 与读取路径不一致")
        bundle_content = _canonical_bytes(envelope["bundle"])
        if envelope["bundle_sha256"] != _sha256(bundle_content):
            raise ResearchAssetStorageError("快照内容哈希校验失败")

        bundle = _bundle_from_value(
            envelope["bundle"],
            "快照 bundle",
            ResearchAssetStorageError,
        )
        _validated_bundle(owner, bundle, ResearchAssetStorageError)
        if bundle.snapshot.snapshot_id != snapshot_id:
            raise ResearchAssetStorageError("bundle.snapshot_id 与读取范围不一致")
        return bundle

    def _load_package_file(
        self,
        owner: str,
        snapshot_id: str,
    ) -> SnapshotPackage | None:
        stored_package = self._load_package_file_with_identity(owner, snapshot_id)
        return None if stored_package is None else stored_package.package

    def _load_package_file_with_identity(
        self,
        owner: str,
        snapshot_id: str,
    ) -> StoredSnapshotPackage | None:
        content = self._read_bytes(
            self._package_path(owner, snapshot_id),
            max_bytes=SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES,
            label="快照包文件",
            error_type=SnapshotPackageError,
        )
        if content is None:
            return None
        return self._stored_package_from_content(owner, snapshot_id, content)

    @staticmethod
    def _stored_package_from_content(
        owner: str,
        snapshot_id: str,
        content: bytes,
    ) -> StoredSnapshotPackage:
        package = parse_snapshot_package(owner, content)
        if package.bundle.snapshot.snapshot_id != snapshot_id:
            raise SnapshotPackageError("快照包 snapshot_id 与读取路径不一致")
        return StoredSnapshotPackage(
            package=package,
            package_sha256=_sha256(content),
            archive_size_bytes=len(content),
        )

    def load_bundle(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> ResearchAssetBundle | None:
        owner = _require_nonblank(
            owner_ref,
            "owner_ref",
            ResearchAssetStorageError,
        )
        requested_snapshot_id = _require_stable_id(
            snapshot_id,
            "snapshot_id",
            ResearchAssetStorageError,
        )
        with self._lock, self._exclusive_snapshot(owner, requested_snapshot_id):
            bundle = self._load_bundle_file(owner, requested_snapshot_id)
            package = self._load_package_file(owner, requested_snapshot_id)
            if (
                bundle is not None
                and package is not None
                and not self._same_bundle(bundle, package.bundle)
            ):
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 的 bundle 和快照包内容冲突"
                )
            if bundle is not None:
                return bundle
            if package is not None:
                return package.bundle
            return None

    def save_bundle(
        self,
        owner_ref: str,
        bundle: ResearchAssetBundle,
    ) -> None:
        owner = _validated_bundle(
            owner_ref,
            bundle,
            ResearchAssetStorageError,
        )
        snapshot_id = bundle.snapshot.snapshot_id
        target = self._bundle_path(owner, snapshot_id)
        bundle_value = _bundle_value(bundle)
        bundle_content = _canonical_bytes(bundle_value)
        envelope = {
            "schema_version": _STORAGE_SCHEMA_VERSION,
            "owner_ref": owner,
            "snapshot_id": snapshot_id,
            "bundle_sha256": _sha256(bundle_content),
            "bundle": bundle_value,
        }
        content = _canonical_bytes(envelope)
        if len(content) > _MAX_STORED_BUNDLE_BYTES:
            raise ResearchAssetStorageError("快照文件超过安全保存上限")

        with self._lock, self._exclusive_snapshot(owner, snapshot_id):
            package = self._load_package_file(owner, snapshot_id)
            if package is not None and not self._same_bundle(bundle, package.bundle):
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 已存在不同 bundle 的快照包"
                )
            stored_bundle = self._load_bundle_file(owner, snapshot_id)
            if stored_bundle is not None:
                if self._same_bundle(bundle, stored_bundle):
                    return
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 已存在不同 bundle"
                )
            self._atomic_write(target, content, "快照")

    def load_snapshot_package(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> SnapshotPackage | None:
        stored_package = self.load_snapshot_package_with_identity(
            owner_ref,
            snapshot_id,
        )
        return None if stored_package is None else stored_package.package

    def load_snapshot_package_with_identity(
        self,
        owner_ref: str,
        snapshot_id: str,
    ) -> StoredSnapshotPackage | None:
        owner = _require_nonblank(
            owner_ref,
            "owner_ref",
            ResearchAssetStorageError,
        )
        requested_snapshot_id = _require_stable_id(
            snapshot_id,
            "snapshot_id",
            ResearchAssetStorageError,
        )
        storage_key = _identity_hash(requested_snapshot_id)
        # The descriptor-relative per-snapshot ``flock`` below is the actual
        # serialization boundary.  Do not hold the root-wide in-process lock
        # while reading and validating a potentially large package: unrelated
        # owners must remain independent.
        with self._open_owner_directory(owner) as directory:
            if directory is None:
                return None
            package_name = f"{storage_key}.zip"
            try:
                if isinstance(directory, Path):
                    package_status = os.stat(
                        directory / package_name,
                        follow_symlinks=False,
                    )
                else:
                    package_status = os.stat(
                        package_name,
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise SnapshotPackageError("快照包文件状态读取失败") from error
            if not stat.S_ISREG(package_status.st_mode):
                raise SnapshotPackageError("快照包文件必须是普通文件")
            if package_status.st_nlink != 1:
                raise SnapshotPackageError("快照包文件不能是硬链接")
            if package_status.st_size > SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES:
                raise SnapshotPackageError("快照包文件超过安全读取上限")
            with self._exclusive_snapshot_in_directory(
                directory,
                requested_snapshot_id,
            ):
                content = self._read_directory_file(
                    directory,
                    package_name,
                    max_bytes=SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES,
                    label="快照包文件",
                    required=False,
                    error_type=SnapshotPackageError,
                )
                if content is None:
                    return None
                stored_package = self._stored_package_from_content(
                    owner,
                    requested_snapshot_id,
                    content,
                )
                bundle_content = self._read_directory_file(
                    directory,
                    f"{storage_key}.json",
                    max_bytes=_MAX_STORED_BUNDLE_BYTES,
                    label="快照文件",
                    required=False,
                )
            if bundle_content is not None:
                bundle = self._bundle_from_stored_content(
                    owner,
                    requested_snapshot_id,
                    bundle_content,
                )
            else:
                bundle = None
            package = stored_package.package
            if bundle is not None and not self._same_bundle(bundle, package.bundle):
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 的 bundle 和快照包内容冲突"
                )
            return stored_package

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        if not isinstance(package, SnapshotPackage):
            raise SnapshotPackageError("package 类型无效")
        content = build_snapshot_package(owner_ref, package.bundle, package.media)
        normalized = parse_snapshot_package(owner_ref, content)
        owner = normalized.bundle.collection.owner_ref
        snapshot_id = normalized.bundle.snapshot.snapshot_id
        target = self._package_path(owner, snapshot_id)

        with self._lock, self._exclusive_snapshot(owner, snapshot_id):
            bundle = self._load_bundle_file(owner, snapshot_id)
            if bundle is not None and not self._same_bundle(bundle, normalized.bundle):
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 已存在不同 bundle"
                )
            stored_package = self._load_package_file(owner, snapshot_id)
            if stored_package is not None:
                if self._same_package(normalized, stored_package):
                    return
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 已存在不同快照包内容"
                )
            self._atomic_write(target, content, "快照包")

    def list_snapshot_catalog(
        self,
        owner_ref: str,
        *,
        cursor: str | None = None,
        limit: int = 20,
    ) -> SnapshotCatalogPage:
        """按哈希存储键稳定枚举 owner 范围内的轻量快照元数据。

        方法只读取最终 ``.zip`` / 可选 ``.json`` 文件，不创建
        owner 目录或读锁文件。扫描时只保留 ``limit + 1`` 个存储键；
        当前页逐项只读取 central directory、manifest 与 bundle，并在投影为
        轻量条目后立即释放完整 bundle；媒体成员内容留给显式加载/下载校验。
        """
        owner = _require_nonblank(
            owner_ref,
            "owner_ref",
            ResearchAssetStorageError,
        )
        if cursor is not None:
            cursor = _require_sha256(
                cursor,
                "cursor",
                ResearchAssetStorageError,
            )
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > _SNAPSHOT_CATALOG_MAX_LIMIT
        ):
            raise ResearchAssetStorageError("limit 必须是 1 到 50 的整数")

        selected_keys: list[str] = []
        selection_capacity = limit + 1
        with self._open_owner_directory(owner) as directory:
            if directory is None:
                return SnapshotCatalogPage(entries=(), next_cursor=None)
            try:
                entries = os.scandir(directory)
            except OSError as error:
                raise ResearchAssetStorageError("快照包目录枚举失败") from error
            with entries:
                for entry in entries:
                    name = entry.name
                    if not name.endswith(".zip"):
                        continue
                    match = _SNAPSHOT_PACKAGE_FILENAME_PATTERN.fullmatch(name)
                    if match is None:
                        raise SnapshotPackageError("快照包未按小写哈希存储键命名")
                    try:
                        mode = entry.stat(follow_symlinks=False).st_mode
                    except OSError as error:
                        raise ResearchAssetStorageError(
                            "快照包目录项状态读取失败"
                        ) from error
                    if not stat.S_ISREG(mode):
                        raise SnapshotPackageError("快照包目录项必须是普通文件")

                    storage_key = match.group(1)
                    if cursor is not None and storage_key <= cursor:
                        continue
                    if (
                        len(selected_keys) == selection_capacity
                        and storage_key >= selected_keys[-1]
                    ):
                        continue
                    position = 0
                    while (
                        position < len(selected_keys)
                        and selected_keys[position] < storage_key
                    ):
                        position += 1
                    selected_keys.insert(position, storage_key)
                    if len(selected_keys) > selection_capacity:
                        selected_keys.pop()

            has_more = len(selected_keys) > limit
            page_keys = selected_keys[:limit]
            catalog_entries: list[SnapshotCatalogEntry] = []
            metadata_bytes = 0
            for storage_key in page_keys:
                entry, consumed_bytes = self._catalog_entry_from_package(
                    owner,
                    storage_key,
                    directory,
                    metadata_budget=(
                        _SNAPSHOT_CATALOG_MAX_PAGE_METADATA_BYTES
                        - metadata_bytes
                    ),
                )
                metadata_bytes += consumed_bytes
                catalog_entries.append(entry)

        return SnapshotCatalogPage(
            entries=tuple(catalog_entries),
            next_cursor=(page_keys[-1] if has_more else None),
        )

    @contextmanager
    def _open_owner_directory(self, owner_ref: str):
        directory_path = self._root / _identity_hash(owner_ref)
        if os.name == "nt":
            try:
                root_status = self._root.lstat()
            except FileNotFoundError:
                yield None
                return
            except OSError as error:
                raise ResearchAssetStorageError(
                    "快照包存储根目录打开失败"
                ) from error
            if stat.S_ISLNK(root_status.st_mode) or not stat.S_ISDIR(
                root_status.st_mode
            ):
                raise ResearchAssetStorageError("快照包存储根路径必须是目录")
            try:
                status = directory_path.lstat()
            except FileNotFoundError:
                yield None
                return
            except OSError as error:
                raise ResearchAssetStorageError(
                    "快照包 owner 目录打开失败"
                ) from error
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                raise ResearchAssetStorageError("快照包 owner 路径必须是目录")
            yield directory_path
            return
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(directory_path, flags)
        except FileNotFoundError:
            yield None
            return
        except OSError as error:
            raise ResearchAssetStorageError("快照包 owner 目录打开失败") from error
        try:
            try:
                mode = os.fstat(descriptor).st_mode
            except OSError as error:
                raise ResearchAssetStorageError(
                    "快照包 owner 目录状态读取失败"
                ) from error
            if not stat.S_ISDIR(mode):
                raise ResearchAssetStorageError("快照包 owner 路径必须是目录")
            yield descriptor
        finally:
            os.close(descriptor)

    @classmethod
    def _catalog_entry_from_package(
        cls,
        owner: str,
        storage_key: str,
        directory: _DirectoryHandle,
        *,
        metadata_budget: int,
    ) -> tuple[SnapshotCatalogEntry, int]:
        with cls._open_directory_file(
            directory,
            f"{storage_key}.zip",
            max_bytes=SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES,
            label="快照包文件",
            required=True,
        ) as package_file:
            assert package_file is not None
            package_bundle, metadata_bytes = _read_snapshot_package_bundle_metadata(
                owner,
                package_file,
                metadata_budget=metadata_budget,
            )
        snapshot = package_bundle.snapshot
        collection = package_bundle.collection
        snapshot_id = snapshot.snapshot_id
        if _identity_hash(snapshot_id) != storage_key:
            raise SnapshotPackageError("快照包 snapshot_id 与存储键不一致")

        bundle_content = cls._read_directory_file(
            directory,
            f"{storage_key}.json",
            max_bytes=min(
                _MAX_STORED_BUNDLE_BYTES,
                metadata_budget - metadata_bytes,
            ),
            label="快照文件",
            required=False,
        )
        if bundle_content is not None:
            metadata_bytes += len(bundle_content)
            stored_bundle = cls._bundle_from_stored_content(
                owner,
                snapshot_id,
                bundle_content,
            )
            if not cls._same_bundle(stored_bundle, package_bundle):
                raise SnapshotConflictError(
                    "同一 owner_ref 与 snapshot_id 的 bundle 和快照包内容冲突"
                )
        entry = SnapshotCatalogEntry(
            owner_ref=owner,
            storage_key=storage_key,
            snapshot_id=snapshot_id,
            provider=snapshot.provider,
            source_mode=snapshot.source_mode,
            collection_state=snapshot.collection_state,
            mapping_status=snapshot.mapping_status,
            item_count=snapshot.item_count,
            question_count=snapshot.question_count,
            asset_count=snapshot.asset_count,
            image_asset_count=sum(
                asset.media_type == MediaType.IMAGE
                for asset in collection.assets
            ),
            asset_reference_count=snapshot.asset_reference_count,
        )
        return entry, metadata_bytes

    @staticmethod
    @contextmanager
    def _open_directory_file(
        directory: _DirectoryHandle,
        name: str,
        *,
        max_bytes: int,
        label: str,
        required: bool,
    ):
        if isinstance(directory, Path):
            content = FileResearchAssetStorage._read_directory_file(
                directory,
                name,
                max_bytes=max_bytes,
                label=label,
                required=required,
            )
            if content is None:
                yield None
                return
            with io.BytesIO(content) as source:
                yield source
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            if isinstance(directory, Path):
                descriptor = os.open(directory / name, flags)
            else:
                descriptor = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError as error:
            if not required:
                yield None
                return
            raise ResearchAssetStorageError(f"{label}在枚举期间消失") from error
        except OSError as error:
            raise ResearchAssetStorageError(f"{label}打开失败") from error
        try:
            try:
                file_status = os.fstat(descriptor)
            except OSError as error:
                raise ResearchAssetStorageError(f"{label}状态读取失败") from error
            if not stat.S_ISREG(file_status.st_mode):
                raise SnapshotPackageError(f"{label}必须是普通文件")
            if file_status.st_size > max_bytes:
                raise ResearchAssetStorageError(f"{label}超过安全读取上限")
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                yield source
            try:
                current_status = os.fstat(descriptor)
            except OSError as error:
                raise ResearchAssetStorageError(f"{label}状态读取失败") from error
            if (
                current_status.st_dev != file_status.st_dev
                or current_status.st_ino != file_status.st_ino
                or current_status.st_size != file_status.st_size
                or current_status.st_mtime_ns != file_status.st_mtime_ns
            ):
                raise ResearchAssetStorageError(f"{label}在读取期间发生变化")
        finally:
            os.close(descriptor)

    @staticmethod
    def _read_directory_file(
        directory: _DirectoryHandle,
        name: str,
        *,
        max_bytes: int,
        label: str,
        required: bool,
        error_type: type[ResearchAssetStorageError] = ResearchAssetStorageError,
    ) -> bytes | None:
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        try:
            if isinstance(directory, Path):
                descriptor = os.open(directory / name, flags)
            else:
                descriptor = os.open(name, flags, dir_fd=directory)
        except FileNotFoundError as error:
            if not required:
                return None
            raise error_type(f"{label}在枚举期间消失") from error
        except OSError as error:
            raise error_type(f"{label}打开失败") from error
        try:
            try:
                file_status = os.fstat(descriptor)
            except OSError as error:
                raise error_type(f"{label}状态读取失败") from error
            if not stat.S_ISREG(file_status.st_mode):
                raise error_type(f"{label}必须是普通文件")
            if file_status.st_nlink != 1:
                raise error_type(f"{label}不能是硬链接")
            if file_status.st_size > max_bytes:
                raise error_type(f"{label}超过安全读取上限")

            chunks: list[bytes] = []
            total = 0
            while True:
                try:
                    chunk = os.read(
                        descriptor,
                        min(1024 * 1024, max_bytes - total + 1),
                    )
                except OSError as error:
                    raise error_type(f"{label}读取失败") from error
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise error_type(f"{label}超过安全读取上限")
                chunks.append(chunk)
            try:
                final_status = os.fstat(descriptor)
                if isinstance(directory, Path):
                    path_status = os.stat(
                        directory / name,
                        follow_symlinks=False,
                    )
                else:
                    path_status = os.stat(
                        name,
                        dir_fd=directory,
                        follow_symlinks=False,
                    )
            except OSError as error:
                raise error_type(f"{label}在读取期间发生变化") from error
            if (
                total != file_status.st_size
                or final_status.st_dev != file_status.st_dev
                or final_status.st_ino != file_status.st_ino
                or final_status.st_mode != file_status.st_mode
                or final_status.st_size != file_status.st_size
                or final_status.st_mtime_ns != file_status.st_mtime_ns
                or final_status.st_ctime_ns != file_status.st_ctime_ns
                or final_status.st_nlink != 1
                or path_status.st_dev != file_status.st_dev
                or path_status.st_ino != file_status.st_ino
                or path_status.st_mode != file_status.st_mode
                or path_status.st_size != file_status.st_size
                or path_status.st_mtime_ns != file_status.st_mtime_ns
                or (
                    os.name != "nt"
                    and path_status.st_ctime_ns != file_status.st_ctime_ns
                )
            ):
                raise error_type(f"{label}在读取期间发生变化")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    @staticmethod
    @contextmanager
    def _exclusive_snapshot_in_directory(
        directory: _DirectoryHandle,
        snapshot_id: str,
    ):
        lock_name = f".{_identity_hash(snapshot_id)}.lock"
        flags = (
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            if isinstance(directory, Path):
                descriptor = os.open(directory / lock_name, flags, 0o600)
            else:
                descriptor = os.open(lock_name, flags, 0o600, dir_fd=directory)
        except OSError as error:
            raise ResearchAssetStorageError("快照进程锁创建失败") from error
        try:
            try:
                lock_status = os.fstat(descriptor)
            except OSError as error:
                raise ResearchAssetStorageError("快照进程锁状态读取失败") from error
            if not stat.S_ISREG(lock_status.st_mode) or lock_status.st_nlink != 1:
                raise ResearchAssetStorageError("快照进程锁必须是单链接普通文件")
            try:
                acquire_exclusive_file_lock(descriptor)
            except OSError as error:
                raise ResearchAssetStorageError("快照进程锁获取失败") from error
            try:
                yield
            finally:
                try:
                    release_file_lock(descriptor)
                except OSError:
                    pass
        finally:
            os.close(descriptor)

    @contextmanager
    def _exclusive_snapshot(self, owner_ref: str, snapshot_id: str):
        lock_path = self._lock_path(owner_ref, snapshot_id)
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
        except OSError as error:
            raise ResearchAssetStorageError("快照进程锁创建失败") from error
        try:
            acquire_exclusive_file_lock(descriptor)
        except OSError as error:
            os.close(descriptor)
            raise ResearchAssetStorageError("快照进程锁获取失败") from error
        try:
            yield
        finally:
            try:
                release_file_lock(descriptor)
            except OSError:
                pass
            finally:
                os.close(descriptor)

    @classmethod
    def _atomic_write(cls, target: Path, content: bytes, label: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise ResearchAssetStorageError(f"{label}目录创建失败") from error
        try:
            descriptor, temporary_path = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
        except OSError as error:
            raise ResearchAssetStorageError(f"{label}临时文件创建失败") from error
        try:
            with os.fdopen(descriptor, "wb") as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            temporary_path = ""
            cls._fsync_directory(target.parent)
        except OSError as error:
            raise ResearchAssetStorageError(f"{label}原子保存失败") from error
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


def _require_sha256(
    value: Any,
    label: str,
    error_type: type[ResearchAssetStorageError] = SnapshotPackageError,
) -> str:
    if not isinstance(value, str) or not _SHA256_PATTERN.fullmatch(value):
        raise error_type(f"{label} 必须是小写 SHA-256")
    return value


_SNAPSHOT_MEDIA_TYPES = frozenset({MediaType.IMAGE, MediaType.DOCUMENT})


def _snapshot_media_labels(media_types: set[MediaType]) -> tuple[str, str]:
    if media_types == {MediaType.IMAGE}:
        return "图片素材", "图片"
    if media_types == {MediaType.DOCUMENT}:
        return "文档素材", "文档"
    return "图片或文档素材", "媒体"


def _snapshot_media_requirements(
    collection: ResearchAssetCollection,
) -> tuple[dict[str, set[int]], str]:
    requirements: dict[str, set[int]] = {}
    media_types: set[MediaType] = set()
    for asset in collection.assets:
        if asset.media_type not in _SNAPSHOT_MEDIA_TYPES:
            continue
        media_types.add(asset.media_type)
        asset_label = (
            "图片素材"
            if asset.media_type == MediaType.IMAGE
            else "文档素材"
        )
        if asset.content_hash is None:
            raise SnapshotPackageError(
                f"{asset_label} {asset.asset_id} 缺少 content_hash"
            )
        content_hash = _require_sha256(
            asset.content_hash,
            f"{asset_label} {asset.asset_id} 的 content_hash",
        )
        sizes = requirements.setdefault(content_hash, set())
        if asset.size_bytes is not None:
            sizes.add(asset.size_bytes)
    media_label, hash_label = _snapshot_media_labels(media_types)
    for content_hash, sizes in requirements.items():
        if len(sizes) > 1:
            raise SnapshotPackageError(
                f"同一{hash_label}哈希 {content_hash} 的 size_bytes 不一致"
            )
    return requirements, media_label


def _validated_media(
    collection: ResearchAssetCollection,
    media: Mapping[str, bytes],
) -> dict[str, bytes]:
    if not isinstance(media, Mapping):
        raise SnapshotPackageError("media 必须是按内容哈希索引的映射")
    requirements, media_label = _snapshot_media_requirements(collection)
    normalized: dict[str, bytes] = {}
    for raw_hash, raw_content in media.items():
        content_hash = _require_sha256(raw_hash, "media key")
        if content_hash in normalized:
            raise SnapshotPackageError(f"媒体哈希重复：{content_hash}")
        if not isinstance(raw_content, bytes):
            raise SnapshotPackageError(f"媒体 {content_hash} 必须是 bytes")
        if len(raw_content) > SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES:
            raise SnapshotPackageError(f"媒体 {content_hash} 超过单项大小上限")
        if _sha256(raw_content) != content_hash:
            raise SnapshotPackageError(f"媒体 {content_hash} 内容哈希不一致")
        normalized[content_hash] = raw_content

    provided_hashes = set(normalized)
    expected_hashes = set(requirements)
    unexpected = provided_hashes - expected_hashes
    if unexpected:
        raise SnapshotPackageError(
            f"快照包含未被{media_label}引用的媒体："
            + "、".join(sorted(unexpected))
        )
    missing = expected_hashes - provided_hashes
    if missing:
        raise SnapshotPackageError(
            f"快照缺少{media_label}媒体：" + "、".join(sorted(missing))
        )
    for content_hash, sizes in requirements.items():
        if sizes and len(normalized[content_hash]) not in sizes:
            raise SnapshotPackageError(
                f"媒体 {content_hash} 与素材 size_bytes 不一致"
            )
    total_size = sum(len(content) for content in normalized.values())
    if total_size > SNAPSHOT_PACKAGE_MAX_TOTAL_BYTES:
        raise SnapshotPackageError("媒体总大小超过快照包安全上限")
    return normalized


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100600 << 16
    return info


def build_snapshot_package(
    owner_ref: str,
    bundle: ResearchAssetBundle,
    media: Mapping[str, bytes],
) -> bytes:
    """创建内容哈希命名的 API JSON + 媒体 ZIP 快照包。"""
    owner = _validated_bundle(owner_ref, bundle, SnapshotPackageError)
    normalized_media = _validated_media(bundle.collection, media)
    bundle_content = _canonical_bytes(_bundle_value(bundle))
    if len(bundle_content) > SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES:
        raise SnapshotPackageError("bundle JSON 超过单项大小上限")
    bundle_hash = _sha256(bundle_content)
    bundle_path = f"bundle/{bundle_hash}.json"
    media_entries = [
        {
            "path": f"media/{content_hash}",
            "sha256": content_hash,
            "size": len(content),
        }
        for content_hash, content in sorted(normalized_media.items())
    ]
    manifest = {
        "schema_version": SNAPSHOT_PACKAGE_SCHEMA_VERSION,
        "owner_ref": owner,
        "snapshot_id": bundle.snapshot.snapshot_id,
        "bundle": {
            "path": bundle_path,
            "sha256": bundle_hash,
            "size": len(bundle_content),
        },
        "media": media_entries,
    }
    manifest_content = _canonical_bytes(manifest)
    if len(manifest_content) > SNAPSHOT_PACKAGE_MAX_MANIFEST_BYTES:
        raise SnapshotPackageError("manifest 超过大小上限")
    total_size = len(manifest_content) + len(bundle_content) + sum(
        len(content) for content in normalized_media.values()
    )
    if total_size > SNAPSHOT_PACKAGE_MAX_TOTAL_BYTES:
        raise SnapshotPackageError("快照包解压总大小超过安全上限")
    if 2 + len(normalized_media) > SNAPSHOT_PACKAGE_MAX_MEMBERS:
        raise SnapshotPackageError("快照包成员数量超过安全上限")

    output = io.BytesIO()
    try:
        with zipfile.ZipFile(output, "w", allowZip64=False) as archive:
            archive.writestr(_zip_info(_MANIFEST_PATH), manifest_content)
            archive.writestr(_zip_info(bundle_path), bundle_content)
            for content_hash, content in sorted(normalized_media.items()):
                archive.writestr(_zip_info(f"media/{content_hash}"), content)
    except (OSError, RuntimeError, zipfile.LargeZipFile) as error:
        raise SnapshotPackageError("快照包创建失败") from error
    result = output.getvalue()
    if len(result) > SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES:
        raise SnapshotPackageError("快照包压缩文件超过安全上限")
    return result


def _validate_zip_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        raise SnapshotPackageError("快照包包含非法成员路径")
    path = PurePosixPath(name)
    if path.is_absolute() or any(
        part in {"", ".", ".."} for part in name.split("/")
    ):
        raise SnapshotPackageError(f"快照包包含不安全路径：{name}")
    if info.is_dir():
        raise SnapshotPackageError(f"快照包不允许目录成员：{name}")
    if info.flag_bits & 0x1:
        raise SnapshotPackageError(f"快照包不允许加密成员：{name}")
    mode = info.external_attr >> 16
    if stat.S_IFMT(mode) == stat.S_IFLNK:
        raise SnapshotPackageError(f"快照包不允许符号链接：{name}")
    if info.file_size < 0 or info.file_size > SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES:
        raise SnapshotPackageError(f"快照包成员超过大小上限：{name}")


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    limit: int,
) -> bytes:
    if info.file_size > limit:
        raise SnapshotPackageError(f"快照包成员超过读取上限：{info.filename}")
    chunks: list[bytes] = []
    total = 0
    try:
        with archive.open(info, "r") as source:
            while True:
                chunk = source.read(min(1024 * 1024, limit - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise SnapshotPackageError(
                        f"快照包成员实际内容超过读取上限：{info.filename}"
                    )
                chunks.append(chunk)
    except SnapshotPackageError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise SnapshotPackageError(f"快照包成员读取失败：{info.filename}") from error
    content = b"".join(chunks)
    if len(content) != info.file_size:
        raise SnapshotPackageError(f"快照包成员声明大小不一致：{info.filename}")
    return content


def _manifest_size(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SnapshotPackageError(f"{label} 必须是非负整数")
    if value > SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES:
        raise SnapshotPackageError(f"{label} 超过单项大小上限")
    return value


def _manifest_entry(value: Any, label: str) -> tuple[str, str, int]:
    entry = _require_exact_keys(
        value,
        {"path", "sha256", "size"},
        label,
        SnapshotPackageError,
    )
    path = entry["path"]
    if not isinstance(path, str):
        raise SnapshotPackageError(f"{label}.path 类型无效")
    digest = _require_sha256(entry["sha256"], f"{label}.sha256")
    size = _manifest_size(entry["size"], f"{label}.size")
    return path, digest, size


class _SnapshotArchiveMetadata(NamedTuple):
    bundle: ResearchAssetBundle
    media_descriptors: dict[str, tuple[str, int]]
    info_by_name: dict[str, zipfile.ZipInfo]
    metadata_bytes: int


def _validate_media_descriptor_closure(
    collection: ResearchAssetCollection,
    media_descriptors: Mapping[str, tuple[str, int]],
) -> None:
    requirements, media_label = _snapshot_media_requirements(collection)
    provided_hashes = set(media_descriptors)
    expected_hashes = set(requirements)
    unexpected = provided_hashes - expected_hashes
    if unexpected:
        raise SnapshotPackageError(
            f"快照包含未被{media_label}引用的媒体："
            + "、".join(sorted(unexpected))
        )
    missing = expected_hashes - provided_hashes
    if missing:
        raise SnapshotPackageError(
            f"快照缺少{media_label}媒体：" + "、".join(sorted(missing))
        )
    for content_hash, sizes in requirements.items():
        declared_size = media_descriptors[content_hash][1]
        if sizes and declared_size not in sizes:
            raise SnapshotPackageError(
                f"媒体 {content_hash} 与素材 size_bytes 不一致"
            )


def _snapshot_archive_metadata(
    owner: str,
    archive: zipfile.ZipFile,
    *,
    metadata_budget: int | None = None,
) -> _SnapshotArchiveMetadata:
    infos = archive.infolist()
    if not infos or len(infos) > SNAPSHOT_PACKAGE_MAX_MEMBERS:
        raise SnapshotPackageError("快照包成员数量无效")
    names = [info.filename for info in infos]
    if len(names) != len(set(names)):
        raise SnapshotPackageError("快照包包含重复成员")
    total_declared_size = 0
    info_by_name: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        _validate_zip_member(info)
        total_declared_size += info.file_size
        if total_declared_size > SNAPSHOT_PACKAGE_MAX_TOTAL_BYTES:
            raise SnapshotPackageError("快照包声明的解压总大小超过安全上限")
        info_by_name[info.filename] = info
    manifest_info = info_by_name.get(_MANIFEST_PATH)
    if manifest_info is None:
        raise SnapshotPackageError("快照包缺少 manifest.json")
    if (
        metadata_budget is not None
        and manifest_info.file_size > metadata_budget
    ):
        raise SnapshotPackageError("快照目录页元数据超过安全读取预算")
    manifest = _require_exact_keys(
        _load_json_bytes(
            _read_zip_member(
                archive,
                manifest_info,
                SNAPSHOT_PACKAGE_MAX_MANIFEST_BYTES,
            ),
            "manifest.json",
            SnapshotPackageError,
        ),
        {"schema_version", "owner_ref", "snapshot_id", "bundle", "media"},
        "manifest.json",
        SnapshotPackageError,
    )
    if (
        isinstance(manifest["schema_version"], bool)
        or manifest["schema_version"] != SNAPSHOT_PACKAGE_SCHEMA_VERSION
    ):
        raise SnapshotPackageError("快照包版本不受支持")
    if manifest["owner_ref"] != owner:
        raise SnapshotPackageError("快照包 owner_ref 与导入范围不一致")
    manifest_snapshot_id = _require_stable_id(
        manifest["snapshot_id"],
        "manifest.snapshot_id",
        SnapshotPackageError,
    )

    bundle_path, bundle_hash, bundle_size = _manifest_entry(
        manifest["bundle"],
        "manifest.bundle",
    )
    if bundle_path != f"bundle/{bundle_hash}.json":
        raise SnapshotPackageError("bundle 未按内容哈希命名")
    raw_media_entries = manifest["media"]
    if not isinstance(raw_media_entries, list):
        raise SnapshotPackageError("manifest.media 必须是列表")
    if len(raw_media_entries) > SNAPSHOT_PACKAGE_MAX_MEMBERS - 2:
        raise SnapshotPackageError("manifest.media 成员数量超过安全上限")
    media_descriptors: dict[str, tuple[str, int]] = {}
    media_paths: set[str] = set()
    for index, raw_entry in enumerate(raw_media_entries):
        path, digest, size = _manifest_entry(
            raw_entry,
            f"manifest.media[{index}]",
        )
        if path != f"media/{digest}":
            raise SnapshotPackageError("媒体未按内容哈希命名")
        if digest in media_descriptors or path in media_paths:
            raise SnapshotPackageError("manifest.media 包含重复媒体")
        media_descriptors[digest] = (path, size)
        media_paths.add(path)

    expected_members = {_MANIFEST_PATH, bundle_path, *media_paths}
    if set(info_by_name) != expected_members:
        raise SnapshotPackageError("快照包包含缺失或未声明成员")
    bundle_info = info_by_name[bundle_path]
    if bundle_info.file_size != bundle_size:
        raise SnapshotPackageError("bundle JSON 大小或内容哈希不一致")
    metadata_bytes = manifest_info.file_size + bundle_size
    if metadata_budget is not None and metadata_bytes > metadata_budget:
        raise SnapshotPackageError("快照目录页元数据超过安全读取预算")
    for digest, (path, declared_size) in media_descriptors.items():
        if info_by_name[path].file_size != declared_size:
            raise SnapshotPackageError(
                f"媒体 {digest} 大小或内容哈希不一致"
            )

    bundle_content = _read_zip_member(
        archive,
        bundle_info,
        SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES,
    )
    if _sha256(bundle_content) != bundle_hash:
        raise SnapshotPackageError("bundle JSON 大小或内容哈希不一致")
    bundle = _bundle_from_value(
        _load_json_bytes(
            bundle_content,
            "bundle JSON",
            SnapshotPackageError,
        ),
        "bundle JSON",
        SnapshotPackageError,
    )
    _validated_bundle(owner, bundle, SnapshotPackageError)
    if bundle.snapshot.snapshot_id != manifest_snapshot_id:
        raise SnapshotPackageError("bundle.snapshot_id 与 manifest 不一致")
    _validate_media_descriptor_closure(bundle.collection, media_descriptors)
    return _SnapshotArchiveMetadata(
        bundle=bundle,
        media_descriptors=media_descriptors,
        info_by_name=info_by_name,
        metadata_bytes=metadata_bytes,
    )


def _read_snapshot_package_bundle_metadata(
    owner_ref: str,
    package_file: BinaryIO,
    *,
    metadata_budget: int,
) -> tuple[ResearchAssetBundle, int]:
    """只读取 manifest 与 bundle，媒体内容留给显式快照加载。"""
    owner = _require_nonblank(owner_ref, "owner_ref", SnapshotPackageError)
    if (
        isinstance(metadata_budget, bool)
        or not isinstance(metadata_budget, int)
        or metadata_budget < 0
    ):
        raise SnapshotPackageError("快照目录页元数据预算无效")
    try:
        with zipfile.ZipFile(package_file, "r") as archive:
            metadata = _snapshot_archive_metadata(
                owner,
                archive,
                metadata_budget=metadata_budget,
            )
    except SnapshotPackageError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise SnapshotPackageError("快照包不是有效 ZIP") from error
    return metadata.bundle, metadata.metadata_bytes


def parse_snapshot_package(
    owner_ref: str,
    package: bytes,
) -> SnapshotPackage:
    """安全解析并校验 API JSON + 媒体 ZIP 快照包。"""
    owner = _require_nonblank(owner_ref, "owner_ref", SnapshotPackageError)
    if not isinstance(package, bytes):
        raise SnapshotPackageError("package 必须是 bytes")
    if len(package) > SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES:
        raise SnapshotPackageError("快照包压缩文件超过安全上限")

    try:
        archive = zipfile.ZipFile(io.BytesIO(package), "r")
    except (OSError, zipfile.BadZipFile) as error:
        raise SnapshotPackageError("快照包不是有效 ZIP") from error

    with archive:
        metadata = _snapshot_archive_metadata(owner, archive)
        media: dict[str, bytes] = {}
        for digest, (path, declared_size) in metadata.media_descriptors.items():
            content = _read_zip_member(
                archive,
                metadata.info_by_name[path],
                SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES,
            )
            if len(content) != declared_size or _sha256(content) != digest:
                raise SnapshotPackageError(
                    f"媒体 {digest} 大小或内容哈希不一致"
                )
            media[digest] = content

    normalized_media = _validated_media(metadata.bundle.collection, media)
    return SnapshotPackage(bundle=metadata.bundle, media=normalized_media)
