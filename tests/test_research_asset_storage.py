from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings
import zipfile

from app.storage import research_assets as research_assets_storage
from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import MediaType, ResearchAssetCollection
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetBundle,
    ResearchAssetStorageError,
    ResearchSnapshotCatalogStorage,
    ResearchSnapshotIdentityStorage,
    ResearchSnapshotStorage,
    SnapshotCatalogEntry,
    SnapshotCatalogPage,
    SnapshotConflictError,
    SnapshotPackage,
    SnapshotPackageError,
    StoredSnapshotPackage,
    build_snapshot_package,
    parse_snapshot_package,
)


FIXTURE = Path(__file__).parent / "fixtures" / "research_assets" / "google_forms.json"


def _bundle_with_media() -> tuple[ResearchAssetBundle, dict[str, bytes]]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    snapshot = QuestionnaireSnapshot.model_validate(payload["snapshot"])
    collection = ResearchAssetCollection.model_validate(payload["collection"])
    media: dict[str, bytes] = {}
    assets = []
    for index, asset in enumerate(collection.assets):
        if asset.media_type == MediaType.IMAGE:
            content = f"fixture-image-{index}".encode("utf-8")
            content_hash = hashlib.sha256(content).hexdigest()
            media[content_hash] = content
            asset = asset.model_copy(update={
                "content_hash": content_hash,
                "size_bytes": len(content),
            })
        assets.append(asset)
    collection = collection.model_copy(update={"assets": assets})
    return ResearchAssetBundle(snapshot, collection), media


def _bundle_with_image_and_document_media() -> tuple[
    ResearchAssetBundle,
    dict[str, bytes],
    str,
]:
    bundle, media = _bundle_with_media()
    document_content = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )
    document_hash = hashlib.sha256(document_content).hexdigest()
    image_asset = next(
        asset
        for asset in bundle.collection.assets
        if asset.media_type == MediaType.IMAGE
    )
    document_asset = image_asset.model_copy(update={
        "asset_id": "asset_offline_research_document",
        "media_type": MediaType.DOCUMENT,
        "mime_type": "application/pdf",
        "filename": "research-material.pdf",
        "display_name": "离线研究文档",
        "provider_resource_id": "fixture-research-document",
        "size_bytes": len(document_content),
        "content_hash": document_hash,
    })
    assets = [*bundle.collection.assets, document_asset]
    collection = bundle.collection.model_copy(update={"assets": assets})
    snapshot = bundle.snapshot.model_copy(update={"asset_count": len(assets)})
    media[document_hash] = document_content
    return ResearchAssetBundle(snapshot, collection), media, document_hash


def _rewrite_zip(
    package: bytes,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: list[tuple[str, bytes]] | None = None,
    removals: set[str] | None = None,
) -> bytes:
    replacements = replacements or {}
    removals = removals or set()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package), "r") as source:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                if info.filename in removals:
                    continue
                target.writestr(info, replacements.get(info.filename, source.read(info)))
            for name, content in additions or []:
                target.writestr(name, content)
    return output.getvalue()


def _package_with_different_media(
    bundle: ResearchAssetBundle,
    media: dict[str, bytes],
) -> SnapshotPackage:
    original_hash = next(iter(media))
    replacement_content = b"different-offline-media"
    replacement_hash = hashlib.sha256(replacement_content).hexdigest()
    assets = [
        asset.model_copy(update={
            "content_hash": replacement_hash,
            "size_bytes": len(replacement_content),
        })
        if asset.content_hash == original_hash
        else asset
        for asset in bundle.collection.assets
    ]
    collection = bundle.collection.model_copy(update={"assets": assets})
    replacement_media = dict(media)
    replacement_media.pop(original_hash)
    replacement_media[replacement_hash] = replacement_content
    return SnapshotPackage(
        ResearchAssetBundle(bundle.snapshot, collection),
        replacement_media,
    )


def _package_with_different_document_media(
    bundle: ResearchAssetBundle,
    media: dict[str, bytes],
) -> SnapshotPackage:
    document_asset = next(
        asset
        for asset in bundle.collection.assets
        if asset.media_type == MediaType.DOCUMENT
    )
    assert document_asset.content_hash is not None
    replacement_content = b"%PDF-1.4\n%different-document\n%%EOF\n"
    replacement_hash = hashlib.sha256(replacement_content).hexdigest()
    assets = [
        asset.model_copy(update={
            "content_hash": replacement_hash,
            "size_bytes": len(replacement_content),
        })
        if asset.asset_id == document_asset.asset_id
        else asset
        for asset in bundle.collection.assets
    ]
    collection = bundle.collection.model_copy(update={"assets": assets})
    replacement_media = dict(media)
    replacement_media.pop(document_asset.content_hash)
    replacement_media[replacement_hash] = replacement_content
    return SnapshotPackage(
        ResearchAssetBundle(bundle.snapshot, collection),
        replacement_media,
    )


def _package_for_snapshot_id(
    bundle: ResearchAssetBundle,
    media: dict[str, bytes],
    snapshot_id: str,
) -> SnapshotPackage:
    return SnapshotPackage(
        ResearchAssetBundle(
            bundle.snapshot.model_copy(update={"snapshot_id": snapshot_id}),
            bundle.collection,
        ),
        dict(media),
    )


def _package_for_owner(
    bundle: ResearchAssetBundle,
    media: dict[str, bytes],
    owner_ref: str,
) -> SnapshotPackage:
    collection = bundle.collection.model_copy(update={
        "owner_ref": owner_ref,
        "sources": [
            source.model_copy(update={"owner_ref": owner_ref})
            for source in bundle.collection.sources
        ],
    })
    return SnapshotPackage(
        ResearchAssetBundle(bundle.snapshot, collection),
        dict(media),
    )


class _LegacySnapshotStorage:
    """只实现增加 catalog 前的快照存储端口。"""

    def load_snapshot_package(self, owner_ref: str, snapshot_id: str):
        return None

    def save_snapshot_package(
        self,
        owner_ref: str,
        package: SnapshotPackage,
    ) -> None:
        return None


def _save_bundle_in_process(
    root: str,
    owner_ref: str,
    bundle: ResearchAssetBundle,
    start_event,
    result_queue,
    write_barrier=None,
) -> None:
    storage = FileResearchAssetStorage(root)
    original_atomic_write = FileResearchAssetStorage._atomic_write

    def delayed_atomic_write(target: Path, content: bytes, label: str) -> None:
        try:
            write_barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        original_atomic_write(target, content, label)

    try:
        start_event.wait(timeout=5)
        if write_barrier is None:
            storage.save_bundle(owner_ref, bundle)
        else:
            with patch.object(
                FileResearchAssetStorage,
                "_atomic_write",
                new=staticmethod(delayed_atomic_write),
            ):
                storage.save_bundle(owner_ref, bundle)
    except SnapshotConflictError:
        result_queue.put("conflict")
    except Exception as error:  # pragma: no cover - only reported by parent assertion
        result_queue.put(f"error:{type(error).__name__}:{error}")
    else:
        result_queue.put("ok")


class FileResearchAssetStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="research-asset-storage-test-"
        )
        self.root = Path(self.temporary.name)
        self.storage = FileResearchAssetStorage(self.root)
        self.bundle, _ = _bundle_with_media()
        self.owner_ref = self.bundle.collection.owner_ref

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _run_concurrent_bundle_saves(
        self,
        bundles: list[ResearchAssetBundle],
        *,
        synchronize_writes: bool = False,
    ) -> list[str]:
        context = multiprocessing.get_context(
            "spawn" if os.name == "nt" else "fork"
        )
        start_event = context.Event()
        result_queue = context.Queue()
        write_barrier = (
            context.Barrier(len(bundles)) if synchronize_writes else None
        )
        processes = [
            context.Process(
                target=_save_bundle_in_process,
                args=(
                    str(self.root),
                    self.owner_ref,
                    bundle,
                    start_event,
                    result_queue,
                    write_barrier,
                ),
            )
            for bundle in bundles
        ]
        try:
            for process in processes:
                process.start()
            start_event.set()
            for process in processes:
                process.join(timeout=10)
            self.assertFalse(
                any(process.is_alive() for process in processes),
                "并发保存子进程未在超时内退出",
            )
            self.assertEqual(
                [process.exitcode for process in processes],
                [0] * len(processes),
            )
            return sorted(
                result_queue.get(timeout=2) for _ in processes
            )
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
            result_queue.close()
            result_queue.join_thread()

    def test_root_is_required_and_bundle_round_trips_in_owner_scope(self):
        with self.assertRaises(TypeError):
            FileResearchAssetStorage()  # type: ignore[call-arg]

        self.storage.save_bundle(self.owner_ref, self.bundle)

        self.assertEqual(
            self.storage.load_bundle(
                self.owner_ref,
                self.bundle.snapshot.snapshot_id,
            ),
            self.bundle,
        )
        self.assertIsNone(
            self.storage.load_bundle(
                "another-owner",
                self.bundle.snapshot.snapshot_id,
            )
        )
        files = list(self.root.rglob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertNotIn(self.owner_ref, str(files[0]))
        self.assertNotIn(self.bundle.snapshot.snapshot_id, str(files[0]))

    def test_save_rejects_owner_mismatch_before_creating_files(self):
        with self.assertRaisesRegex(ResearchAssetStorageError, "owner_ref"):
            self.storage.save_bundle("another-owner", self.bundle)
        self.assertEqual(list(self.root.rglob("*")), [])

        mismatched_collection = self.bundle.collection.model_copy(
            update={"owner_ref": "another-owner"}
        )
        mismatched_bundle = ResearchAssetBundle(
            self.bundle.snapshot,
            mismatched_collection,
        )
        with self.assertRaisesRegex(ResearchAssetStorageError, "owner_ref"):
            self.storage.save_bundle("another-owner", mismatched_bundle)
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_failed_replace_does_not_create_bundle_and_removes_temporary_file(self):
        with patch(
            "app.storage.research_assets.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaisesRegex(ResearchAssetStorageError, "原子保存失败"):
                self.storage.save_bundle(self.owner_ref, self.bundle)

        self.assertIsNone(
            self.storage.load_bundle(
                self.owner_ref, self.bundle.snapshot.snapshot_id
            )
        )
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
        self.assertEqual(list(self.root.rglob("*.json")), [])

    def test_same_bundle_is_idempotent_and_different_bundle_conflicts(self):
        self.storage.save_bundle(self.owner_ref, self.bundle)
        with patch("app.storage.research_assets.os.replace") as replace:
            self.storage.save_bundle(self.owner_ref, self.bundle)
        replace.assert_not_called()

        different = ResearchAssetBundle(
            self.bundle.snapshot.model_copy(update={"title": "different"}),
            self.bundle.collection,
        )
        with self.assertRaises(SnapshotConflictError):
            self.storage.save_bundle(self.owner_ref, different)
        self.assertEqual(
            self.storage.load_bundle(
                self.owner_ref, self.bundle.snapshot.snapshot_id
            ),
            self.bundle,
        )

    def test_cross_process_different_bundle_writes_have_exactly_one_winner(self):
        different = ResearchAssetBundle(
            self.bundle.snapshot.model_copy(update={"title": "different"}),
            self.bundle.collection,
        )

        results = self._run_concurrent_bundle_saves(
            [self.bundle, different],
            synchronize_writes=True,
        )

        self.assertEqual(results, ["conflict", "ok"])
        restored = self.storage.load_bundle(
            self.owner_ref,
            self.bundle.snapshot.snapshot_id,
        )
        self.assertTrue(restored == self.bundle or restored == different)
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_cross_process_identical_bundle_writes_are_both_idempotent(self):
        results = self._run_concurrent_bundle_saves(
            [self.bundle, self.bundle],
        )

        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(
            self.storage.load_bundle(
                self.owner_ref,
                self.bundle.snapshot.snapshot_id,
            ),
            self.bundle,
        )
        self.assertEqual(len(list(self.root.rglob("*.json"))), 1)

    def test_load_rejects_tampered_persisted_bundle(self):
        self.storage.save_bundle(self.owner_ref, self.bundle)
        stored_path = next(self.root.rglob("*.json"))
        envelope = json.loads(stored_path.read_text(encoding="utf-8"))
        envelope["bundle"]["snapshot"]["title"] = "tampered"
        stored_path.write_text(
            json.dumps(envelope, ensure_ascii=False),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ResearchAssetStorageError, "哈希校验失败"):
            self.storage.load_bundle(
                self.owner_ref,
                self.bundle.snapshot.snapshot_id,
            )


class SnapshotPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bundle, self.media = _bundle_with_media()
        self.owner_ref = self.bundle.collection.owner_ref
        self.package = build_snapshot_package(
            self.owner_ref,
            self.bundle,
            self.media,
        )

    def test_legacy_image_only_package_round_trip_uses_content_hash_member_names(self):
        self.assertFalse(
            any(
                asset.media_type == MediaType.DOCUMENT
                for asset in self.bundle.collection.assets
            )
        )
        self.assertEqual(
            build_snapshot_package(self.owner_ref, self.bundle, self.media),
            self.package,
        )
        restored = parse_snapshot_package(self.owner_ref, self.package)
        self.assertEqual(restored.bundle, self.bundle)
        self.assertEqual(restored.media, self.media)

        with zipfile.ZipFile(io.BytesIO(self.package), "r") as archive:
            names = set(archive.namelist())
            manifest = json.loads(archive.read("manifest.json"))
            bundle_hash = manifest["bundle"]["sha256"]
            self.assertIn(f"bundle/{bundle_hash}.json", names)
            self.assertEqual(
                {f"media/{content_hash}" for content_hash in self.media},
                names - {"manifest.json", f"bundle/{bundle_hash}.json"},
            )

    def test_image_and_document_media_round_trip_in_one_package(self):
        bundle, media, document_hash = _bundle_with_image_and_document_media()

        package = build_snapshot_package(self.owner_ref, bundle, media)
        restored = parse_snapshot_package(self.owner_ref, package)

        self.assertEqual(restored.bundle, bundle)
        self.assertEqual(restored.media, media)
        self.assertEqual(restored.media[document_hash], media[document_hash])
        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            self.assertIn(f"media/{document_hash}", archive.namelist())

    def test_build_requires_exact_document_media_closure(self):
        bundle, media, document_hash = _bundle_with_image_and_document_media()
        missing = dict(media)
        missing.pop(document_hash)
        with self.assertRaisesRegex(
            SnapshotPackageError,
            "缺少图片或文档素材媒体",
        ):
            build_snapshot_package(self.owner_ref, bundle, missing)

        unrelated_content = b"unreferenced-document-bytes"
        unrelated_hash = hashlib.sha256(unrelated_content).hexdigest()
        with self.assertRaisesRegex(
            SnapshotPackageError,
            "未被图片或文档素材引用",
        ):
            build_snapshot_package(
                self.owner_ref,
                bundle,
                {**media, unrelated_hash: unrelated_content},
            )

    def test_build_rejects_document_hash_or_declared_size_mismatch(self):
        bundle, media, document_hash = _bundle_with_image_and_document_media()
        with self.assertRaisesRegex(SnapshotPackageError, "内容哈希不一致"):
            build_snapshot_package(
                self.owner_ref,
                bundle,
                {**media, document_hash: b"tampered-document"},
            )

        assets = [
            asset.model_copy(update={"size_bytes": asset.size_bytes + 1})
            if asset.media_type == MediaType.DOCUMENT
            and asset.size_bytes is not None
            else asset
            for asset in bundle.collection.assets
        ]
        wrong_size_bundle = ResearchAssetBundle(
            bundle.snapshot,
            bundle.collection.model_copy(update={"assets": assets}),
        )
        with self.assertRaisesRegex(SnapshotPackageError, "size_bytes 不一致"):
            build_snapshot_package(self.owner_ref, wrong_size_bundle, media)

    def test_parse_rejects_document_content_and_manifest_size_tampering(self):
        bundle, media, document_hash = _bundle_with_image_and_document_media()
        package = build_snapshot_package(self.owner_ref, bundle, media)
        media_path = f"media/{document_hash}"
        content_tampered = _rewrite_zip(
            package,
            replacements={media_path: b"tampered-document"},
        )
        with self.assertRaisesRegex(SnapshotPackageError, "哈希不一致"):
            parse_snapshot_package(self.owner_ref, content_tampered)

        with zipfile.ZipFile(io.BytesIO(package), "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
        document_entry = next(
            entry
            for entry in manifest["media"]
            if entry["sha256"] == document_hash
        )
        document_entry["size"] += 1
        manifest_tampered = _rewrite_zip(
            package,
            replacements={
                "manifest.json": json.dumps(
                    manifest,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            },
        )
        with self.assertRaisesRegex(SnapshotPackageError, "大小或内容哈希不一致"):
            parse_snapshot_package(self.owner_ref, manifest_tampered)

    def test_build_rejects_missing_unreferenced_or_mismatched_media(self):
        missing = dict(self.media)
        missing.pop(next(iter(missing)))
        with self.assertRaisesRegex(SnapshotPackageError, "缺少图片素材媒体"):
            build_snapshot_package(self.owner_ref, self.bundle, missing)

        unrelated_content = b"unrelated"
        unrelated_hash = hashlib.sha256(unrelated_content).hexdigest()
        with self.assertRaisesRegex(SnapshotPackageError, "未被图片素材引用"):
            build_snapshot_package(
                self.owner_ref,
                self.bundle,
                {**self.media, unrelated_hash: unrelated_content},
            )

        first_hash = next(iter(self.media))
        mismatched = {**self.media, first_hash: b"tampered"}
        with self.assertRaisesRegex(SnapshotPackageError, "内容哈希不一致"):
            build_snapshot_package(self.owner_ref, self.bundle, mismatched)

    def test_parse_rejects_wrong_owner_and_tampered_media(self):
        with self.assertRaisesRegex(SnapshotPackageError, "owner_ref"):
            parse_snapshot_package("another-owner", self.package)

        media_path = f"media/{next(iter(self.media))}"
        tampered = _rewrite_zip(
            self.package,
            replacements={media_path: b"tampered"},
        )
        with self.assertRaisesRegex(SnapshotPackageError, "哈希不一致"):
            parse_snapshot_package(self.owner_ref, tampered)

    def test_parse_rejects_path_traversal_unknown_and_duplicate_members(self):
        traversal = _rewrite_zip(
            self.package,
            additions=[("../escape", b"must-not-extract")],
        )
        with self.assertRaisesRegex(SnapshotPackageError, "不安全路径"):
            parse_snapshot_package(self.owner_ref, traversal)

        unknown = _rewrite_zip(
            self.package,
            additions=[("undeclared.bin", b"unknown")],
        )
        with self.assertRaisesRegex(SnapshotPackageError, "未声明成员"):
            parse_snapshot_package(self.owner_ref, unknown)

        with zipfile.ZipFile(io.BytesIO(self.package), "r") as archive:
            duplicate_manifest = archive.read("manifest.json")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            duplicate = _rewrite_zip(
                self.package,
                additions=[("manifest.json", duplicate_manifest)],
            )
        with self.assertRaisesRegex(SnapshotPackageError, "重复成员"):
            parse_snapshot_package(self.owner_ref, duplicate)

    def test_parse_enforces_member_count_and_total_uncompressed_limits(self):
        with patch(
            "app.storage.research_assets.SNAPSHOT_PACKAGE_MAX_MEMBERS",
            1,
        ):
            with self.assertRaisesRegex(SnapshotPackageError, "成员数量"):
                parse_snapshot_package(self.owner_ref, self.package)

        with patch(
            "app.storage.research_assets.SNAPSHOT_PACKAGE_MAX_TOTAL_BYTES",
            1,
        ):
            with self.assertRaisesRegex(SnapshotPackageError, "解压总大小"):
                parse_snapshot_package(self.owner_ref, self.package)


class FileResearchSnapshotStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="research-snapshot-storage-test-"
        )
        self.root = Path(self.temporary.name)
        self.storage = FileResearchAssetStorage(self.root)
        self.bundle, self.media = _bundle_with_media()
        self.owner_ref = self.bundle.collection.owner_ref
        self.snapshot_id = self.bundle.snapshot.snapshot_id
        self.package = SnapshotPackage(self.bundle, self.media)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_protocol_and_package_round_trip_preserve_offline_media(self):
        self.assertIsInstance(self.storage, ResearchSnapshotStorage)

        self.storage.save_snapshot_package(self.owner_ref, self.package)

        restored = self.storage.load_snapshot_package(
            self.owner_ref,
            self.snapshot_id,
        )
        self.assertEqual(restored, self.package)
        assert restored is not None
        for content_hash, content in restored.media.items():
            self.assertEqual(hashlib.sha256(content).hexdigest(), content_hash)
            self.assertEqual(content, self.media[content_hash])
        self.assertEqual(
            self.storage.load_bundle(self.owner_ref, self.snapshot_id),
            self.bundle,
        )

        files = list(self.root.rglob("*.zip"))
        self.assertEqual(len(files), 1)
        self.assertEqual(
            files[0].read_bytes(),
            build_snapshot_package(self.owner_ref, self.bundle, self.media),
        )
        self.assertNotIn(self.owner_ref, str(files[0]))
        self.assertNotIn(self.snapshot_id, str(files[0]))

    def test_catalog_protocol_keeps_legacy_snapshot_protocol_compatible(self):
        legacy = _LegacySnapshotStorage()

        self.assertIsInstance(legacy, ResearchSnapshotStorage)
        self.assertNotIsInstance(legacy, ResearchSnapshotIdentityStorage)
        self.assertNotIsInstance(legacy, ResearchSnapshotCatalogStorage)
        self.assertIsInstance(self.storage, ResearchSnapshotStorage)
        self.assertIsInstance(self.storage, ResearchSnapshotIdentityStorage)
        self.assertIsInstance(self.storage, ResearchSnapshotCatalogStorage)

    def test_identity_load_uses_exact_persisted_zip_bytes(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        persisted_bytes = package_path.read_bytes()

        stored = self.storage.load_snapshot_package_with_identity(
            self.owner_ref,
            self.snapshot_id,
        )

        self.assertEqual(
            stored,
            StoredSnapshotPackage(
                package=self.package,
                package_sha256=hashlib.sha256(persisted_bytes).hexdigest(),
                archive_size_bytes=len(persisted_bytes),
            ),
        )
        assert stored is not None
        self.assertEqual(
            self.storage.load_snapshot_package(
                self.owner_ref,
                self.snapshot_id,
            ),
            stored.package,
        )

    def test_identity_load_never_rebuilds_persisted_package(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)

        with patch(
            "app.storage.research_assets.build_snapshot_package",
            side_effect=AssertionError("load must not rebuild package identity"),
        ):
            stored = self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )
            restored = self.storage.load_snapshot_package(
                self.owner_ref,
                self.snapshot_id,
            )

        assert stored is not None
        self.assertEqual(stored.package, self.package)
        self.assertEqual(restored, self.package)

    def test_identity_load_missing_package_returns_none(self):
        before = set(self.root.rglob("*"))
        self.assertIsNone(
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                "missing-snapshot",
            )
        )
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_identity_load_missing_ids_do_not_leave_locks_in_existing_owner(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        before = {
            path.relative_to(self.root)
            for path in self.root.rglob("*")
        }

        for index in range(20):
            self.assertIsNone(
                self.storage.load_snapshot_package_with_identity(
                    self.owner_ref,
                    f"missing-snapshot-{index}",
                )
            )

        self.assertEqual(
            {
                path.relative_to(self.root)
                for path in self.root.rglob("*")
            },
            before,
        )

    def test_identity_load_rejects_symlink_fifo_and_oversize_package(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        persisted_bytes = package_path.read_bytes()
        safe_target = package_path.with_suffix(".safe")
        safe_target.write_bytes(persisted_bytes)

        if os.name != "nt":
            package_path.unlink()
            package_path.symlink_to(safe_target.name)
            with self.assertRaisesRegex(SnapshotPackageError, "普通文件"):
                self.storage.load_snapshot_package_with_identity(
                    self.owner_ref,
                    self.snapshot_id,
                )

            package_path.unlink()
            os.mkfifo(package_path)
            with self.assertRaisesRegex(SnapshotPackageError, "普通文件"):
                self.storage.load_snapshot_package_with_identity(
                    self.owner_ref,
                    self.snapshot_id,
                )

        package_path.unlink()
        package_path.write_bytes(persisted_bytes)
        with (
            patch.object(
                research_assets_storage,
                "SNAPSHOT_PACKAGE_MAX_ARCHIVE_BYTES",
                len(persisted_bytes) - 1,
            ),
            self.assertRaisesRegex(SnapshotPackageError, "安全读取上限"),
        ):
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )

    def test_identity_load_rejects_hardlinked_package_and_paired_json(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        self.storage.save_bundle(self.owner_ref, self.bundle)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        package_hardlink = package_path.with_suffix(".hardlink")
        os.link(package_path, package_hardlink)

        with self.assertRaisesRegex(SnapshotPackageError, "硬链接"):
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )

        package_hardlink.unlink()
        bundle_path = self.storage._bundle_path(
            self.owner_ref,
            self.snapshot_id,
        )
        bundle_hardlink = bundle_path.with_suffix(".hardlink")
        os.link(bundle_path, bundle_hardlink)
        with self.assertRaisesRegex(ResearchAssetStorageError, "硬链接"):
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )

    @unittest.skipIf(
        os.name == "nt",
        "Windows symlink creation requires an optional developer privilege",
    )
    def test_identity_load_rejects_symlinked_owner_directory(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        owner_directory = package_path.parent
        moved_directory = self.root / "moved-owner-directory"
        owner_directory.rename(moved_directory)
        owner_directory.symlink_to(moved_directory.name, target_is_directory=True)

        with self.assertRaisesRegex(
            ResearchAssetStorageError,
            "owner 目录打开失败",
        ):
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )

    def test_identity_load_rejects_package_changed_while_reading(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        original_read = os.read
        changed = False

        def racing_read(descriptor: int, size: int) -> bytes:
            nonlocal changed
            chunk = original_read(descriptor, size)
            if chunk and not changed:
                changed = True
                package_path.write_bytes(package_path.read_bytes() + b"changed")
            return chunk

        with (
            patch("app.storage.research_assets.os.read", side_effect=racing_read),
            self.assertRaisesRegex(SnapshotPackageError, "读取期间发生变化"),
        ):
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )

    def test_identity_load_rejects_same_size_change_with_restored_mtime(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        original_status = package_path.stat()
        original_read = os.read
        changed = False

        def racing_read(descriptor: int, size: int) -> bytes:
            nonlocal changed
            chunk = original_read(descriptor, size)
            if chunk and not changed:
                changed = True
                with package_path.open("r+b") as target:
                    first_byte = target.read(1)
                    target.seek(0)
                    target.write(bytes([first_byte[0] ^ 0x01]))
                    target.flush()
                    os.fsync(target.fileno())
                os.utime(
                    package_path,
                    ns=(
                        original_status.st_atime_ns,
                        original_status.st_mtime_ns,
                    ),
                )
            return chunk

        with (
            patch("app.storage.research_assets.os.read", side_effect=racing_read),
            self.assertRaisesRegex(SnapshotPackageError, "读取期间发生变化"),
        ):
            self.storage.load_snapshot_package_with_identity(
                self.owner_ref,
                self.snapshot_id,
            )

    def test_blocked_identity_parse_does_not_block_another_owner(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        other_owner = "identity-other-owner"
        other_package = _package_for_owner(
            self.bundle,
            self.media,
            other_owner,
        )
        self.storage.save_snapshot_package(other_owner, other_package)

        parse_started = threading.Event()
        release_parse = threading.Event()
        other_finished = threading.Event()
        results: dict[str, StoredSnapshotPackage | None] = {}
        errors: list[Exception] = []
        original_parser = FileResearchAssetStorage._stored_package_from_content

        def blocking_parser(owner_ref, snapshot_id, content):
            if owner_ref == self.owner_ref:
                parse_started.set()
                if not release_parse.wait(timeout=5):
                    raise TimeoutError("identity parse release timed out")
            return original_parser(owner_ref, snapshot_id, content)

        def load_owner(label, owner_ref, snapshot_id):
            try:
                storage = FileResearchAssetStorage(self.root)
                results[label] = storage.load_snapshot_package_with_identity(
                    owner_ref,
                    snapshot_id,
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)
            finally:
                if label == "other":
                    other_finished.set()

        with patch.object(
            FileResearchAssetStorage,
            "_stored_package_from_content",
            new=staticmethod(blocking_parser),
        ):
            blocked_thread = threading.Thread(
                target=load_owner,
                args=("blocked", self.owner_ref, self.snapshot_id),
            )
            other_thread = threading.Thread(
                target=load_owner,
                args=(
                    "other",
                    other_owner,
                    other_package.bundle.snapshot.snapshot_id,
                ),
            )
            blocked_thread.start()
            try:
                self.assertTrue(parse_started.wait(timeout=2))
                other_thread.start()
                self.assertTrue(
                    other_finished.wait(timeout=2),
                    "owner A 的 identity parse 阻塞了 owner B",
                )
                self.assertFalse(other_thread.is_alive())
            finally:
                release_parse.set()
                blocked_thread.join(timeout=5)
                if other_thread.is_alive():
                    other_thread.join(timeout=5)

        self.assertFalse(blocked_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results["blocked"].package, self.package)
        self.assertEqual(results["other"].package, other_package)

    def test_catalog_empty_and_other_owner_scopes_are_read_only(self):
        before = set(self.root.rglob("*"))

        empty = self.storage.list_snapshot_catalog(self.owner_ref)

        self.assertEqual(empty, SnapshotCatalogPage((), None))
        self.assertEqual(set(self.root.rglob("*")), before)

        self.storage.save_snapshot_package(self.owner_ref, self.package)
        owner_files_before = {
            path.relative_to(self.root) for path in self.root.rglob("*")
        }
        other_owner_page = self.storage.list_snapshot_catalog("another-owner")
        self.assertEqual(other_owner_page, SnapshotCatalogPage((), None))
        self.assertEqual(
            {path.relative_to(self.root) for path in self.root.rglob("*")},
            owner_files_before,
        )

    def test_catalog_has_stable_two_page_hash_order_and_opaque_cursor(self):
        snapshot_ids = [
            "catalog-snapshot-a",
            "catalog-snapshot-b",
            "catalog-snapshot-c",
            "catalog-snapshot-d",
            "catalog-snapshot-e",
        ]
        for snapshot_id in reversed(snapshot_ids):
            self.storage.save_snapshot_package(
                self.owner_ref,
                _package_for_snapshot_id(self.bundle, self.media, snapshot_id),
            )
        expected_ids = sorted(
            snapshot_ids,
            key=lambda value: hashlib.sha256(value.encode("utf-8")).hexdigest(),
        )

        first = self.storage.list_snapshot_catalog(self.owner_ref, limit=2)
        repeated_first = self.storage.list_snapshot_catalog(
            self.owner_ref,
            limit=2,
        )

        self.assertEqual(first, repeated_first)
        self.assertEqual(
            list(first.snapshot_ids),
            expected_ids[:2],
        )
        self.assertIsNotNone(first.next_cursor)
        assert first.next_cursor is not None
        self.assertRegex(first.next_cursor, r"^[0-9a-f]{64}$")
        self.assertNotIn(self.owner_ref, first.next_cursor)
        self.assertNotIn(expected_ids[1], first.next_cursor)

        second = self.storage.list_snapshot_catalog(
            self.owner_ref,
            cursor=first.next_cursor,
            limit=2,
        )
        self.assertEqual(
            list(second.snapshot_ids),
            expected_ids[2:4],
        )
        self.assertIsNotNone(second.next_cursor)
        third = self.storage.list_snapshot_catalog(
            self.owner_ref,
            cursor=second.next_cursor,
            limit=2,
        )
        self.assertEqual(
            list(third.snapshot_ids),
            expected_ids[4:],
        )
        self.assertIsNone(third.next_cursor)

    def test_catalog_does_not_parse_or_decompress_real_large_media(self):
        large_media = b"large-catalog-media" * (8 * 1024 * 1024 // 19)
        large_hash = hashlib.sha256(large_media).hexdigest()
        assets = [
            asset.model_copy(update={
                "content_hash": large_hash,
                "size_bytes": len(large_media),
            })
            if asset.media_type == MediaType.IMAGE
            else asset
            for asset in self.bundle.collection.assets
        ]
        package = SnapshotPackage(
            ResearchAssetBundle(
                self.bundle.snapshot,
                self.bundle.collection.model_copy(update={"assets": assets}),
            ),
            {large_hash: large_media},
        )
        self.storage.save_snapshot_package(self.owner_ref, package)
        original_member_read = research_assets_storage._read_zip_member
        read_names: list[str] = []

        def record_member_read(archive, info, limit):
            read_names.append(info.filename)
            return original_member_read(archive, info, limit)

        with (
            patch(
                "app.storage.research_assets.parse_snapshot_package"
            ) as full_parser,
            patch(
                "app.storage.research_assets._read_zip_member",
                side_effect=record_member_read,
            ),
        ):
            page = self.storage.list_snapshot_catalog(self.owner_ref, limit=20)

        full_parser.assert_not_called()
        self.assertEqual(page.snapshot_ids, (self.snapshot_id,))
        self.assertEqual(page._fields, ("entries", "next_cursor"))
        self.assertIsInstance(page.entries[0], SnapshotCatalogEntry)
        self.assertEqual(page.entries[0].title, self.bundle.snapshot.title)
        self.assertFalse(hasattr(page.entries[0], "bundle"))
        self.assertFalse(hasattr(page.entries[0], "media"))
        self.assertEqual(read_names[0], "manifest.json")
        self.assertEqual(len(read_names), 2)
        self.assertFalse(any(name.startswith("media/") for name in read_names))

    def test_catalog_page_metadata_budget_includes_optional_sidecar(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        self.storage.save_bundle(self.owner_ref, self.bundle)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        sidecar_path = self.storage._bundle_path(
            self.owner_ref,
            self.snapshot_id,
        )
        with zipfile.ZipFile(package_path, "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
            package_metadata_bytes = (
                archive.getinfo("manifest.json").file_size
                + archive.getinfo(manifest["bundle"]["path"]).file_size
            )
        budget_without_last_sidecar_byte = (
            package_metadata_bytes + sidecar_path.stat().st_size - 1
        )

        with (
            patch.object(
                research_assets_storage,
                "_SNAPSHOT_CATALOG_MAX_PAGE_METADATA_BYTES",
                budget_without_last_sidecar_byte,
            ),
            self.assertRaisesRegex(
                ResearchAssetStorageError,
                "安全读取上限",
            ),
        ):
            self.storage.list_snapshot_catalog(self.owner_ref)

    def test_blocked_catalog_metadata_does_not_block_other_owner_save_and_load(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        other_owner = "catalog-other-owner"
        other_package = _package_for_owner(
            self.bundle,
            self.media,
            other_owner,
        )
        metadata_started = threading.Event()
        release_metadata = threading.Event()
        catalog_results: list[SnapshotCatalogPage] = []
        other_results: list[SnapshotPackage | None] = []
        errors: list[Exception] = []

        original_metadata_reader = (
            research_assets_storage._read_snapshot_package_bundle_metadata
        )

        def blocking_metadata(owner_ref, package_file, *, metadata_budget):
            if owner_ref == self.owner_ref:
                metadata_started.set()
                if not release_metadata.wait(timeout=5):
                    raise TimeoutError("catalog metadata release timed out")
            return original_metadata_reader(
                owner_ref,
                package_file,
                metadata_budget=metadata_budget,
            )

        def list_catalog() -> None:
            try:
                catalog_results.append(
                    self.storage.list_snapshot_catalog(self.owner_ref, limit=1)
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        def save_and_load_other_owner() -> None:
            try:
                storage = FileResearchAssetStorage(self.root)
                storage.save_snapshot_package(other_owner, other_package)
                other_results.append(
                    storage.load_snapshot_package(
                        other_owner,
                        other_package.bundle.snapshot.snapshot_id,
                    )
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        with patch(
            "app.storage.research_assets._read_snapshot_package_bundle_metadata",
            side_effect=blocking_metadata,
        ):
            catalog_thread = threading.Thread(target=list_catalog)
            other_thread = threading.Thread(target=save_and_load_other_owner)
            catalog_thread.start()
            try:
                self.assertTrue(metadata_started.wait(timeout=2))
                other_thread.start()
                other_thread.join(timeout=2)
                self.assertFalse(
                    other_thread.is_alive(),
                    "另一 owner 的保存/读取被 catalog metadata 阻塞",
                )
                self.assertEqual(other_results, [other_package])
            finally:
                release_metadata.set()
                catalog_thread.join(timeout=5)
                if other_thread.is_alive():
                    other_thread.join(timeout=5)

        self.assertFalse(catalog_thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(catalog_results), 1)
        self.assertEqual(catalog_results[0].snapshot_ids, (self.snapshot_id,))

    def test_catalog_reads_complete_version_during_same_owner_atomic_replace(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        target = self.storage._package_path(self.owner_ref, self.snapshot_id)
        alternate = SnapshotPackage(
            ResearchAssetBundle(
                self.bundle.snapshot.model_copy(update={"title": "alternate"}),
                self.bundle.collection,
            ),
            self.media,
        )
        alternate_content = build_snapshot_package(
            self.owner_ref,
            alternate.bundle,
            alternate.media,
        )
        read_started = threading.Event()
        replace_finished = threading.Event()
        replace_errors: list[Exception] = []
        original_metadata_reader = (
            research_assets_storage._read_snapshot_package_bundle_metadata
        )

        def pause_metadata(owner_ref, package_file, *, metadata_budget):
            read_started.set()
            if not replace_finished.wait(timeout=5):
                raise TimeoutError("atomic replace timed out")
            return original_metadata_reader(
                owner_ref,
                package_file,
                metadata_budget=metadata_budget,
            )

        def replace_complete_package() -> None:
            try:
                if not read_started.wait(timeout=2):
                    raise TimeoutError("catalog read did not start")
                temporary = target.parent / f".{target.name}.replacement.tmp"
                temporary.write_bytes(alternate_content)
                os.replace(temporary, target)
            except Exception as error:  # pragma: no cover - asserted below
                replace_errors.append(error)
            finally:
                replace_finished.set()

        replace_thread = threading.Thread(target=replace_complete_package)
        replace_thread.start()
        with patch(
            "app.storage.research_assets._read_snapshot_package_bundle_metadata",
            side_effect=pause_metadata,
        ):
            page = self.storage.list_snapshot_catalog(self.owner_ref, limit=1)
        replace_thread.join(timeout=5)

        self.assertFalse(replace_thread.is_alive())
        self.assertEqual(replace_errors, [])
        self.assertEqual(page.snapshot_ids, (self.snapshot_id,))
        self.assertEqual(
            self.storage.load_snapshot_package(
                self.owner_ref,
                self.snapshot_id,
            ),
            alternate,
        )

    def test_catalog_parses_only_current_page_not_cursor_before_or_page_after(self):
        packages = [
            _package_for_snapshot_id(
                self.bundle,
                self.media,
                f"lazy-catalog-{index}",
            )
            for index in range(3)
        ]
        for package in packages:
            self.storage.save_snapshot_package(self.owner_ref, package)
        ordered = sorted(
            (
                hashlib.sha256(
                    package.bundle.snapshot.snapshot_id.encode("utf-8")
                ).hexdigest(),
                package.bundle.snapshot.snapshot_id,
            )
            for package in packages
        )

        first_key, first_snapshot_id = ordered[0]
        first_path = self.storage._package_path(
            self.owner_ref,
            first_snapshot_id,
        )
        first_content = first_path.read_bytes()
        first_path.write_bytes(b"corrupt-before-cursor")

        after_corrupt_cursor = self.storage.list_snapshot_catalog(
            self.owner_ref,
            cursor=first_key,
            limit=2,
        )
        self.assertEqual(
            after_corrupt_cursor.snapshot_ids,
            tuple(snapshot_id for _, snapshot_id in ordered[1:]),
        )
        with self.assertRaises(SnapshotPackageError):
            self.storage.list_snapshot_catalog(self.owner_ref, limit=1)

        first_path.write_bytes(first_content)
        last_key, _ = ordered[-1]
        last_path = first_path.parent / f"{last_key}.zip"
        last_path.write_bytes(b"corrupt-after-page")

        first_page = self.storage.list_snapshot_catalog(self.owner_ref, limit=1)
        self.assertEqual(first_page.snapshot_ids, (ordered[0][1],))
        second_page = self.storage.list_snapshot_catalog(
            self.owner_ref,
            cursor=first_page.next_cursor,
            limit=1,
        )
        self.assertEqual(second_page.snapshot_ids, (ordered[1][1],))
        with self.assertRaises(SnapshotPackageError):
            self.storage.list_snapshot_catalog(
                self.owner_ref,
                cursor=second_page.next_cursor,
                limit=1,
            )

    def test_catalog_validates_limit_and_cursor_before_storage_access(self):
        for invalid_limit in (0, 51, True, 1.5, "20"):
            with self.subTest(limit=invalid_limit):
                with self.assertRaisesRegex(ResearchAssetStorageError, "limit"):
                    self.storage.list_snapshot_catalog(
                        self.owner_ref,
                        limit=invalid_limit,  # type: ignore[arg-type]
                    )
        for invalid_cursor in (
            "",
            "a" * 63,
            "A" * 64,
            "g" * 64,
            1,
        ):
            with self.subTest(cursor=invalid_cursor):
                with self.assertRaisesRegex(ResearchAssetStorageError, "cursor"):
                    self.storage.list_snapshot_catalog(
                        self.owner_ref,
                        cursor=invalid_cursor,  # type: ignore[arg-type]
                    )
        self.assertEqual(list(self.root.rglob("*")), [])

    def test_catalog_ignores_normal_sidecars_and_returns_no_extra_cursor(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        owner_directory = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        ).parent
        (owner_directory / "normal.json").write_text("{}", encoding="utf-8")
        (owner_directory / ".catalog.lock").write_text("", encoding="utf-8")
        (owner_directory / ".catalog.tmp").write_text("", encoding="utf-8")

        page = self.storage.list_snapshot_catalog(self.owner_ref, limit=1)

        self.assertEqual(page.snapshot_ids, (self.snapshot_id,))
        self.assertIsNone(page.next_cursor)

    @unittest.skipIf(
        os.name == "nt",
        "Windows symlink creation requires an optional developer privilege",
    )
    def test_catalog_fails_closed_for_corrupt_symlink_or_nonregular_zip(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        valid_content = package_path.read_bytes()
        package_path.write_bytes(b"corrupt-package")
        with self.assertRaises(SnapshotPackageError):
            self.storage.list_snapshot_catalog(self.owner_ref)

        package_path.write_bytes(valid_content)
        unsafe_path = package_path.parent / f"{'0' * 64}.zip"
        unsafe_path.symlink_to(package_path.name)
        with self.assertRaisesRegex(SnapshotPackageError, "普通文件"):
            self.storage.list_snapshot_catalog(self.owner_ref)

        unsafe_path.unlink()
        unsafe_path.mkdir()
        with self.assertRaisesRegex(SnapshotPackageError, "普通文件"):
            self.storage.list_snapshot_catalog(self.owner_ref)

    def test_catalog_revalidates_media_closure_and_split_brain_bundle(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        package_path = self.storage._package_path(
            self.owner_ref,
            self.snapshot_id,
        )
        media_path = f"media/{next(iter(self.media))}"
        with zipfile.ZipFile(io.BytesIO(package_path.read_bytes()), "r") as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["media"] = [
            entry for entry in manifest["media"] if entry["path"] != media_path
        ]
        package_path.write_bytes(
            _rewrite_zip(
                package_path.read_bytes(),
                replacements={
                    "manifest.json": json.dumps(
                        manifest,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                },
                removals={media_path},
            )
        )
        with self.assertRaisesRegex(SnapshotPackageError, "缺少图片素材媒体"):
            self.storage.list_snapshot_catalog(self.owner_ref)

        package_path.unlink()
        self.storage.save_bundle(self.owner_ref, self.bundle)
        different_package = SnapshotPackage(
            ResearchAssetBundle(
                self.bundle.snapshot.model_copy(update={"title": "different"}),
                self.bundle.collection,
            ),
            self.media,
        )
        package_path.write_bytes(
            build_snapshot_package(
                self.owner_ref,
                different_package.bundle,
                different_package.media,
            )
        )
        with self.assertRaises(SnapshotConflictError):
            self.storage.list_snapshot_catalog(self.owner_ref)

    def test_catalog_after_concurrent_saves_contains_every_complete_package(self):
        packages = [
            _package_for_snapshot_id(
                self.bundle,
                self.media,
                f"concurrent-catalog-{index}",
            )
            for index in range(8)
        ]
        barrier = threading.Barrier(len(packages))
        errors: list[Exception] = []

        def save(package: SnapshotPackage) -> None:
            try:
                barrier.wait(timeout=2)
                FileResearchAssetStorage(self.root).save_snapshot_package(
                    self.owner_ref,
                    package,
                )
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        threads = [
            threading.Thread(target=save, args=(package,))
            for package in packages
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        page = self.storage.list_snapshot_catalog(self.owner_ref, limit=50)
        self.assertCountEqual(
            page.snapshot_ids,
            [package.bundle.snapshot.snapshot_id for package in packages],
        )
        self.assertIsNone(page.next_cursor)

    def test_document_package_save_and_load_preserve_all_media(self):
        bundle, media, document_hash = _bundle_with_image_and_document_media()
        package = SnapshotPackage(bundle, media)

        self.storage.save_snapshot_package(self.owner_ref, package)
        restored = self.storage.load_snapshot_package(
            self.owner_ref,
            bundle.snapshot.snapshot_id,
        )

        self.assertEqual(restored, package)
        assert restored is not None
        self.assertEqual(restored.media[document_hash], media[document_hash])

    def test_document_package_owner_isolation_and_immutable_conflict(self):
        bundle, media, _ = _bundle_with_image_and_document_media()
        package = SnapshotPackage(bundle, media)
        self.storage.save_snapshot_package(self.owner_ref, package)

        self.assertIsNone(
            self.storage.load_snapshot_package(
                "another-owner",
                bundle.snapshot.snapshot_id,
            )
        )
        with self.assertRaisesRegex(SnapshotPackageError, "owner_ref"):
            self.storage.save_snapshot_package("another-owner", package)

        different = _package_with_different_document_media(bundle, media)
        with self.assertRaises(SnapshotConflictError):
            self.storage.save_snapshot_package(self.owner_ref, different)
        self.assertEqual(
            self.storage.load_snapshot_package(
                self.owner_ref,
                bundle.snapshot.snapshot_id,
            ),
            package,
        )

    def test_same_package_is_idempotent_without_replacing_file(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        stored_path = next(self.root.rglob("*.zip"))
        original_bytes = stored_path.read_bytes()

        with patch("app.storage.research_assets.os.replace") as replace:
            self.storage.save_snapshot_package(self.owner_ref, self.package)

        replace.assert_not_called()
        self.assertEqual(stored_path.read_bytes(), original_bytes)

    def test_same_id_different_bundle_or_media_conflicts_and_preserves_old_data(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        stored_path = next(self.root.rglob("*.zip"))
        original_bytes = stored_path.read_bytes()

        different_bundle = SnapshotPackage(
            ResearchAssetBundle(
                self.bundle.snapshot.model_copy(update={"title": "different"}),
                self.bundle.collection,
            ),
            self.media,
        )
        with self.assertRaises(SnapshotConflictError):
            self.storage.save_snapshot_package(self.owner_ref, different_bundle)

        different_media = _package_with_different_media(self.bundle, self.media)
        with self.assertRaises(SnapshotConflictError):
            self.storage.save_snapshot_package(self.owner_ref, different_media)

        self.assertEqual(stored_path.read_bytes(), original_bytes)
        self.assertEqual(
            self.storage.load_snapshot_package(self.owner_ref, self.snapshot_id),
            self.package,
        )

    def test_owner_scope_and_requested_snapshot_id_are_both_checked(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        self.assertIsNone(
            self.storage.load_snapshot_package("another-owner", self.snapshot_id)
        )
        with self.assertRaisesRegex(SnapshotPackageError, "owner_ref"):
            self.storage.save_snapshot_package("another-owner", self.package)

        stored_content = next(self.root.rglob("*.zip")).read_bytes()
        wrong_snapshot_id = "different-snapshot-id"
        wrong_target = self.storage._package_path(
            self.owner_ref,
            wrong_snapshot_id,
        )
        wrong_target.write_bytes(stored_content)
        with self.assertRaisesRegex(SnapshotPackageError, "snapshot_id"):
            self.storage.load_snapshot_package(
                self.owner_ref,
                wrong_snapshot_id,
            )

    def test_corrupt_existing_package_cannot_be_loaded_or_overwritten(self):
        self.storage.save_snapshot_package(self.owner_ref, self.package)
        stored_path = next(self.root.rglob("*.zip"))
        stored_path.write_bytes(b"corrupt-package")

        with self.assertRaises(SnapshotPackageError):
            self.storage.load_snapshot_package(self.owner_ref, self.snapshot_id)
        with self.assertRaises(SnapshotPackageError):
            self.storage.save_snapshot_package(self.owner_ref, self.package)
        self.assertEqual(stored_path.read_bytes(), b"corrupt-package")

    def test_bundle_and_package_identity_are_cross_checked(self):
        self.storage.save_bundle(self.owner_ref, self.bundle)
        different = SnapshotPackage(
            ResearchAssetBundle(
                self.bundle.snapshot.model_copy(update={"title": "different"}),
                self.bundle.collection,
            ),
            self.media,
        )
        different_content = build_snapshot_package(
            self.owner_ref,
            different.bundle,
            different.media,
        )
        package_path = self.storage._package_path(self.owner_ref, self.snapshot_id)
        package_path.write_bytes(different_content)

        with self.assertRaises(SnapshotConflictError):
            self.storage.load_bundle(self.owner_ref, self.snapshot_id)
        with self.assertRaises(SnapshotConflictError):
            self.storage.load_snapshot_package(self.owner_ref, self.snapshot_id)
        with self.assertRaises(SnapshotConflictError):
            self.storage.save_bundle(self.owner_ref, self.bundle)

    def test_failed_package_replace_cleans_up_without_creating_target(self):
        with patch(
            "app.storage.research_assets.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaisesRegex(
                ResearchAssetStorageError,
                "快照包原子保存失败",
            ):
                self.storage.save_snapshot_package(self.owner_ref, self.package)

        self.assertIsNone(
            self.storage.load_snapshot_package(self.owner_ref, self.snapshot_id)
        )
        self.assertEqual(list(self.root.rglob("*.tmp")), [])
        self.assertEqual(list(self.root.rglob("*.zip")), [])


if __name__ == "__main__":
    unittest.main()
