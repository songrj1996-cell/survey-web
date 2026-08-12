from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
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

    def test_failed_replace_keeps_previous_bundle_and_removes_temporary_file(self):
        self.storage.save_bundle(self.owner_ref, self.bundle)
        replacement = ResearchAssetBundle(
            self.bundle.snapshot.model_copy(update={"title": "replacement"}),
            self.bundle.collection,
        )

        with patch(
            "app.storage.research_assets.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaisesRegex(ResearchAssetStorageError, "原子保存失败"):
                self.storage.save_bundle(self.owner_ref, replacement)

        self.assertEqual(
            self.storage.load_bundle(
                self.owner_ref,
                self.bundle.snapshot.snapshot_id,
            ),
            self.bundle,
        )
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

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


if __name__ == "__main__":
    unittest.main()
