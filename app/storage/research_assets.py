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
from typing import Any, NamedTuple, Protocol, runtime_checkable
import zipfile

from pydantic import ValidationError

from app.core.research_assets import (
    ResearchContractError,
    canonical_json,
    validate_research_contract,
)
from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import MediaType, ResearchAssetCollection


_STORAGE_SCHEMA_VERSION = 1
SNAPSHOT_PACKAGE_SCHEMA_VERSION = 1
SNAPSHOT_PACKAGE_MAX_MEMBERS = 512
SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES = 64 * 1024 * 1024
SNAPSHOT_PACKAGE_MAX_TOTAL_BYTES = 256 * 1024 * 1024
SNAPSHOT_PACKAGE_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_STORED_BUNDLE_BYTES = 64 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_PATH = "manifest.json"


class ResearchAssetStorageError(RuntimeError):
    """快照持久化内容无效、损坏或无法安全读写。"""


class SnapshotPackageError(ResearchAssetStorageError):
    """API JSON 与媒体快照包不满足完整性或安全约束。"""


class ResearchAssetBundle(NamedTuple):
    """必须作为一个事务整体读取和保存的问卷快照与素材集合。"""

    snapshot: QuestionnaireSnapshot
    collection: ResearchAssetCollection


class SnapshotPackage(NamedTuple):
    """已校验的快照聚合与按内容哈希索引的图片字节。"""

    bundle: ResearchAssetBundle
    media: dict[str, bytes]


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
    """显式根目录下、按 owner 隔离的单文件原子 Bundle 存储。"""

    def __init__(self, root: str | os.PathLike[str]):
        if root is None:
            raise TypeError("root 必须显式提供")
        raw_root = os.fspath(root)
        if not isinstance(raw_root, str) or not raw_root.strip():
            raise ValueError("root 必须是非空路径")
        self._root = Path(raw_root).absolute()
        self._lock = threading.RLock()

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
        target = self._bundle_path(owner, requested_snapshot_id)
        with self._lock:
            try:
                size = target.stat().st_size
            except FileNotFoundError:
                return None
            except OSError as error:
                raise ResearchAssetStorageError("快照文件状态读取失败") from error
            if size > _MAX_STORED_BUNDLE_BYTES:
                raise ResearchAssetStorageError("快照文件超过安全读取上限")
            try:
                content = target.read_bytes()
            except OSError as error:
                raise ResearchAssetStorageError("快照文件读取失败") from error

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
        if envelope["snapshot_id"] != requested_snapshot_id:
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
        if bundle.snapshot.snapshot_id != requested_snapshot_id:
            raise ResearchAssetStorageError("bundle.snapshot_id 与读取范围不一致")
        return bundle

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

        with self._lock:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                raise ResearchAssetStorageError("快照目录创建失败") from error
            try:
                descriptor, temporary_path = tempfile.mkstemp(
                    dir=target.parent,
                    prefix=f".{target.name}.",
                    suffix=".tmp",
                )
            except OSError as error:
                raise ResearchAssetStorageError("快照临时文件创建失败") from error
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(content)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, target)
                temporary_path = ""
                self._fsync_directory(target.parent)
            except OSError as error:
                raise ResearchAssetStorageError("快照原子保存失败") from error
            finally:
                if temporary_path:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
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


def _image_media_requirements(
    collection: ResearchAssetCollection,
) -> dict[str, set[int]]:
    requirements: dict[str, set[int]] = {}
    for asset in collection.assets:
        if asset.media_type != MediaType.IMAGE:
            continue
        if asset.content_hash is None:
            raise SnapshotPackageError(
                f"图片素材 {asset.asset_id} 缺少 content_hash"
            )
        content_hash = _require_sha256(
            asset.content_hash,
            f"图片素材 {asset.asset_id} 的 content_hash",
        )
        sizes = requirements.setdefault(content_hash, set())
        if asset.size_bytes is not None:
            sizes.add(asset.size_bytes)
    for content_hash, sizes in requirements.items():
        if len(sizes) > 1:
            raise SnapshotPackageError(
                f"同一图片哈希 {content_hash} 的 size_bytes 不一致"
            )
    return requirements


def _validated_media(
    collection: ResearchAssetCollection,
    media: Mapping[str, bytes],
) -> dict[str, bytes]:
    if not isinstance(media, Mapping):
        raise SnapshotPackageError("media 必须是按内容哈希索引的映射")
    requirements = _image_media_requirements(collection)
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
            "快照包含未被图片素材引用的媒体：" + "、".join(sorted(unexpected))
        )
    missing = expected_hashes - provided_hashes
    if missing:
        raise SnapshotPackageError(
            "快照缺少图片素材媒体：" + "、".join(sorted(missing))
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
    """创建内容哈希命名的 API JSON + 图片 ZIP 快照包。"""
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


def parse_snapshot_package(
    owner_ref: str,
    package: bytes,
) -> SnapshotPackage:
    """安全解析并校验 API JSON + 图片 ZIP 快照包。"""
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
        bundle_content = _read_zip_member(
            archive,
            bundle_info,
            SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES,
        )
        if len(bundle_content) != bundle_size or _sha256(bundle_content) != bundle_hash:
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

        media: dict[str, bytes] = {}
        for digest, (path, declared_size) in media_descriptors.items():
            content = _read_zip_member(
                archive,
                info_by_name[path],
                SNAPSHOT_PACKAGE_MAX_MEMBER_BYTES,
            )
            if len(content) != declared_size or _sha256(content) != digest:
                raise SnapshotPackageError(
                    f"媒体 {digest} 大小或内容哈希不一致"
                )
            media[digest] = content

    normalized_media = _validated_media(bundle.collection, media)
    return SnapshotPackage(bundle=bundle, media=normalized_media)
