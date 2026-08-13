from __future__ import annotations

import hashlib
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import warnings
import zipfile

from app.schemas.questionnaire import QuestionnaireSnapshot
from app.schemas.research_assets import MediaType, ResearchAssetCollection
from app.storage.research_assets import (
    FileResearchAssetStorage,
    ResearchAssetBundle,
    ResearchAssetStorageError,
    ResearchSnapshotStorage,
    SnapshotConflictError,
    SnapshotPackage,
    SnapshotPackageError,
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


def _rewrite_zip(
    package: bytes,
    *,
    replacements: dict[str, bytes] | None = None,
    additions: list[tuple[str, bytes]] | None = None,
) -> bytes:
    replacements = replacements or {}
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(package), "r") as source:
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
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
        context = multiprocessing.get_context("fork")
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

    def test_package_round_trip_uses_content_hash_member_names(self):
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
