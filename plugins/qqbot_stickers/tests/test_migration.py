from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from migration import MigrationResult, migrate_legacy_stickers
from sticker_store import CollectedStickerStore, normalize_category_name
from tests.image_factory import gif, png


def _normalizer(value):
    return normalize_category_name(value, max_chars=32)


def _new_store(root: Path, **overrides) -> CollectedStickerStore:
    options = dict(
        max_file_bytes=100_000,
        category_normalizer=_normalizer,
        max_count=50,
        max_dimension=512,
        max_pixels=500_000,
    )
    options.update(overrides)
    return CollectedStickerStore(root, **options)


def _write_index(root: Path, entries: list[dict]) -> None:
    (root / "index.json").write_text(
        json.dumps({"version": 1, "stickers": entries}, ensure_ascii=False),
        encoding="utf-8",
    )


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.legacy_parent = self.base / "legacy" / "qqbot_enhancer"
        self.legacy = self.legacy_parent / "collected_stickers"
        self.legacy.mkdir(parents=True)
        self.new_root = self.base / "new" / "collected_stickers"
        self.store = _new_store(self.new_root)

    def tearDown(self):
        self.temp.cleanup()

    def _legacy_file(self, category: str, data: bytes, name: str | None = None) -> Path:
        directory = self.legacy / category
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (name or (hashlib.sha256(data).hexdigest() + ".png"))
        path.write_bytes(data)
        return path

    def test_imports_valid_image_and_metadata(self):
        data = png(20, 20, tag=1)
        path = self._legacy_file("开心", data)
        _write_index(
            self.legacy,
            [
                {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "filename": path.name,
                    "category": "开心",
                    "categories": ["开心", "日常"],
                    "tags": ["happy", "笑"],
                    "meaning": "表示开心",
                    "tone": "expressive",
                }
            ],
        )
        result = migrate_legacy_stickers(self.legacy_parent, self.store)
        self.assertIsInstance(result, MigrationResult)
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.errors, 0)
        entries = self.store.load_index()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "开心")
        self.assertEqual(entries[0]["categories"], ["开心", "日常"])
        self.assertEqual(entries[0]["tags"], ["happy", "笑"])
        self.assertEqual(entries[0]["meaning"], "表示开心")
        self.assertEqual(entries[0]["tone"], "expressive")

    def test_malformed_index_falls_back_to_safe_scan(self):
        data = png(tag=2)
        self._legacy_file("扫描", data)
        (self.legacy / "index.json").write_text("{broken", encoding="utf-8")
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertTrue(result.malformed_index)
        self.assertEqual(result.imported, 1)
        self.assertEqual(self.store.load_index()[0]["category"], "扫描")

    def test_idempotent_by_sha(self):
        data = png(tag=3)
        self._legacy_file("A", data)
        first = migrate_legacy_stickers(self.legacy, self.store)
        second = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual((first.imported, first.duplicates), (1, 0))
        self.assertEqual((second.imported, second.duplicates), (0, 1))
        self.assertEqual(len(self.store.load_index()), 1)
        images = [
            item
            for item in self.new_root.rglob("*")
            if item.is_file()
            and item.name != "index.json"
            and not item.name.startswith(".")
        ]
        self.assertEqual(len(images), 1)

    def test_legacy_tree_is_not_modified(self):
        data = png(tag=4)
        path = self._legacy_file("保留", data)
        _write_index(self.legacy, [])
        os.utime(path, (1_600_000_000, 1_600_000_000))
        before_stat = path.stat()
        before_bytes = path.read_bytes()
        before_index = (self.legacy / "index.json").read_bytes()
        migrate_legacy_stickers(self.legacy, self.store)
        after_stat = path.stat()
        self.assertEqual(after_stat.st_mtime_ns, before_stat.st_mtime_ns)
        self.assertEqual(after_stat.st_mode, before_stat.st_mode)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual((self.legacy / "index.json").read_bytes(), before_index)

    def test_existing_new_entry_not_overwritten(self):
        data = png(tag=5)
        self._legacy_file("旧分类", data)
        _write_index(
            self.legacy,
            [
                {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "filename": hashlib.sha256(data).hexdigest() + ".png",
                    "category": "旧分类",
                    "tags": ["legacy"],
                }
            ],
        )
        status, original = self.store.add_sticker(
            data, category="新分类", tags=["new"], meaning="新含义"
        )
        self.assertEqual(status, "saved")
        original_path = Path(original["path"])
        original_mtime = original_path.stat().st_mtime_ns
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(result.imported, 0)
        loaded = self.store.load_index()[0]
        self.assertEqual(loaded["category"], "新分类")
        self.assertEqual(loaded["tags"], ["new"])
        self.assertEqual(loaded["meaning"], "新含义")
        self.assertEqual(original_path.stat().st_mtime_ns, original_mtime)

    def test_symlink_files_and_directories_are_skipped(self):
        data = png(tag=6)
        outside = self.base / "outside.png"
        outside.write_bytes(data)
        symlink_dir = self.legacy / "链接目录"
        symlink_dir.symlink_to(self.base / "outside-dir", target_is_directory=True)
        (self.legacy / "正常").mkdir()
        (self.legacy / "正常" / "link.png").symlink_to(outside)
        valid = png(tag=7)
        self._legacy_file("正常", valid)
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual(result.imported, 1)
        self.assertEqual(len(self.store.load_index()), 1)

    def test_invalid_oversize_and_nonimage_skipped(self):
        (self.legacy / "A").mkdir()
        (self.legacy / "A" / "bad.png").write_bytes(b"not image")
        (self.legacy / "A" / "large.png").write_bytes(png(200, 200, tag=8))
        (self.legacy / "A" / "good.png").write_bytes(png(tag=9))
        store = _new_store(self.new_root, max_file_bytes=200, max_dimension=100)
        result = migrate_legacy_stickers(
            self.legacy, store, max_file_bytes=200, max_dimension=100
        )
        self.assertEqual(result.imported, 1)
        self.assertGreaterEqual(result.skipped, 2)

    def test_legacy_index_traversal_does_not_escape(self):
        safe = png(tag=10)
        self._legacy_file("安全", safe)
        outside_dir = self.base / "outside"
        outside_dir.mkdir()
        (outside_dir / ("e" * 64 + ".png")).write_bytes(png(tag=11))
        _write_index(
            self.legacy,
            [
                {
                    "sha256": "e" * 64,
                    "filename": "e" * 64 + ".png",
                    "category": "../outside",
                }
            ],
        )
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual(result.imported, 1)
        shas = {entry["sha256"] for entry in self.store.load_index()}
        self.assertNotIn("e" * 64, shas)

    def test_bad_legacy_metadata_is_sanitized_not_fatal(self):
        data = png(tag=121)
        path = self._legacy_file("元数据", data)
        _write_index(
            self.legacy,
            [
                {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "filename": path.name,
                    "category": "元数据",
                    "categories": ["元数据", "bad/path", "B", "C", "D"],
                    "tags": ["good", "bad tag"] + ["t%d" % index for index in range(30)],
                    "meaning": "x" * 500,
                    "tone": "bad tone",
                }
            ],
        )
        store = _new_store(self.new_root, max_categories=3, max_tags=2)
        result = migrate_legacy_stickers(self.legacy, store)
        self.assertEqual(result.imported, 1)
        entry = store.load_index()[0]
        self.assertEqual(entry["category"], "元数据")
        self.assertEqual(entry["categories"], ["元数据", "B", "C"])
        self.assertEqual(entry["tags"], ["good", "t0"])
        self.assertLessEqual(len(entry["meaning"]), store.max_meaning_chars)
        self.assertEqual(entry["tone"], "")

    def test_extensionless_file_detected_by_magic(self):
        data = png(tag=12)
        path = self._legacy_file("裸文件", data, name="sticker-no-extension")
        _write_index(
            self.legacy,
            [
                {
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "filename": path.name,
                    "category": "裸文件",
                    "tags": ["magic"],
                }
            ],
        )
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual(result.imported, 1)
        self.assertTrue(self.store.load_index()[0]["filename"].endswith(".png"))

    def test_same_sha_in_two_legacy_categories_counts_duplicate(self):
        data = png(tag=13)
        self._legacy_file("A", data)
        self._legacy_file("B", data)
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual(result.imported, 1)
        self.assertEqual(result.duplicates, 1)
        self.assertEqual(len(self.store.load_index()), 1)

    def test_gif_import_uses_magic_extension(self):
        data = gif(tag=14)
        self._legacy_file("GIF", data, name="legacy-anything.gif")
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertEqual(result.imported, 1)
        self.assertTrue(self.store.load_index()[0]["filename"].endswith(".gif"))

    def test_destination_limit_does_not_exceed_max_count(self):
        self._legacy_file("A", png(tag=15))
        self._legacy_file("A", png(tag=16))
        store = _new_store(self.new_root, max_count=1)
        result = migrate_legacy_stickers(self.legacy, store, max_count=1)
        self.assertEqual(result.imported, 1)
        self.assertEqual(len(store.load_index()), 1)

    def test_missing_legacy_root_is_noop(self):
        result = migrate_legacy_stickers(self.base / "missing", self.store)
        self.assertEqual(result.imported, 0)
        self.assertEqual(result.errors, 0)

    def test_legacy_index_symlink_marks_malformed_and_scans_files(self):
        data = png(tag=17)
        self._legacy_file("A", data)
        outside = self.base / "outside-index.json"
        outside.write_text("not json", encoding="utf-8")
        (self.legacy / "index.json").symlink_to(outside)
        result = migrate_legacy_stickers(self.legacy, self.store)
        self.assertTrue(result.malformed_index)
        self.assertEqual(result.imported, 1)

    def test_result_supports_mapping_access(self):
        result = MigrationResult(imported=2)
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result.get("duplicates"), 0)
        self.assertIn("legacy_preserved", result)

    def test_nested_destination_inside_legacy_refused(self):
        data = png(tag=181)
        self._legacy_file("A", data)
        nested_store = _new_store(self.legacy / "new-data")
        result = migrate_legacy_stickers(self.legacy, nested_store)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.imported, 0)
        self.assertFalse((self.legacy / "new-data" / "index.json").exists())

    def test_same_source_and_destination_refused(self):
        data = png(tag=18)
        self._legacy_file("A", data)
        legacy_store = CollectedStickerStore(
            self.legacy,
            max_file_bytes=100_000,
            category_normalizer=_normalizer,
        )
        result = migrate_legacy_stickers(self.legacy, legacy_store)
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.imported, 0)


if __name__ == "__main__":
    unittest.main()
