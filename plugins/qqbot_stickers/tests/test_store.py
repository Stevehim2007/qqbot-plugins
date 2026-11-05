from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path

from media import detect_image_magic
from sticker_store import (
    COLLECTED_FILE_MODE,
    COLLECTED_INDEX_MODE,
    COLLECTED_LOCK_MODE,
    CollectedStickerStore,
    StickerStoreError,
    normalize_category_name,
)
from tests.image_factory import gif, png


def _normalizer(value):
    return normalize_category_name(value, max_chars=32)


def _make_store(root: Path, **overrides) -> CollectedStickerStore:
    options = dict(
        max_file_bytes=100_000,
        category_normalizer=_normalizer,
        max_tags=8,
        max_tag_chars=16,
        max_meaning_chars=80,
        max_tone_chars=16,
        max_categories=4,
        max_count=50,
        max_dimension=256,
        max_pixels=65536,
        min_dimension=1,
    )
    options.update(overrides)
    return CollectedStickerStore(root, **options)


def _concurrent_add_same(args):
    root, data = args
    store = _make_store(Path(root), max_count=100)
    status, entry = store.add_sticker(data, category="并发去重")
    return (status, entry["sha256"] if entry else "")


def _concurrent_add(args):
    root, index = args
    store = _make_store(Path(root), max_count=100)
    for offset in range(8):
        value = index * 100 + offset
        status, _entry = store.add_sticker(
            png(8 + value % 30, 8 + value % 17, tag=value),
            category="并发",
            source="worker",
        )
        if status not in {"saved", "duplicate"}:
            return status
    return "ok"


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "collected_stickers"
        self.store = _make_store(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_add_persists_file_and_index(self):
        status, entry = self.store.add_sticker(
            png(20, 20, tag=1), category="开心", tags=["happy"], meaning="开心", tone="expressive"
        )
        self.assertEqual(status, "saved")
        self.assertIsNotNone(entry)
        self.assertTrue(Path(entry["path"]).is_file())
        payload = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["schema_version"], 2)
        self.assertEqual(payload["stickers"][0]["category"], "开心")
        self.assertEqual(payload["stickers"][0]["tags"], ["happy"])

    def test_safe_permissions(self):
        self.store.add_sticker(png(tag=2), category="权限")
        self.assertEqual(self.store.index_path.stat().st_mode & 0o777, COLLECTED_INDEX_MODE)
        self.assertEqual(self.store.lock_path.stat().st_mode & 0o777, COLLECTED_LOCK_MODE)
        entries = self.store.load_index()
        self.assertEqual(Path(entries[0]["path"]).stat().st_mode & 0o777, COLLECTED_FILE_MODE)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o755)

    def test_sha256_dedupe_does_not_add_second_file(self):
        data = png(tag=3)
        first = self.store.add_sticker(data, category="A")
        second = self.store.add_sticker(data, category="B")
        self.assertEqual(first[0], "saved")
        self.assertEqual(second[0], "duplicate")
        self.assertEqual(len(self.store.load_index()), 1)
        file_count = sum(
            1
            for item in self.root.rglob("*")
            if item.is_file()
            and item.name != "index.json"
            and not item.name.startswith(".")
        )
        self.assertEqual(file_count, 1)

    def test_duplicate_can_merge_multi_categories_and_tags(self):
        data = png(tag=4)
        self.store.add_sticker(data, category="A", tags=["one"])
        status, entry = self.store.add_sticker(
            data,
            category="B",
            categories=["B", "C"],
            tags=["two"],
            merge_duplicate=True,
        )
        self.assertEqual(status, "duplicate")
        self.assertEqual(entry["category"], "A")
        self.assertEqual(entry["categories"], ["A", "B", "C"])
        self.assertEqual(entry["tags"], ["one", "two"])

    def test_duplicate_default_does_not_mutate_metadata(self):
        data = png(tag=405)
        self.store.add_sticker(data, category="A", tags=["one"])
        status, entry = self.store.add_sticker(
            data, category="B", categories=["B"], tags=["two"], meaning="new"
        )
        self.assertEqual(status, "duplicate")
        self.assertEqual(entry["category"], "A")
        self.assertEqual(entry["categories"], ["A"])
        self.assertEqual(entry["tags"], ["one"])
        self.assertEqual(entry["meaning"], "")

    def test_max_count_is_enforced_atomically(self):
        store = _make_store(self.root, max_count=2)
        self.assertEqual(store.add_sticker(png(tag=10), category="A")[0], "saved")
        self.assertEqual(store.add_sticker(png(tag=11), category="A")[0], "saved")
        self.assertEqual(store.add_sticker(png(tag=12), category="A")[0], "limit")
        self.assertEqual(len(store.load_index()), 2)

    def test_oversize_rejected(self):
        store = _make_store(self.root, max_file_bytes=40)
        status, entry = store.add_sticker(png(30, 30), category="A")
        self.assertEqual(status, "too_large")
        self.assertIsNone(entry)
        self.assertFalse(store.index_path.exists())

    def test_wrong_magic_rejected(self):
        status, _ = self.store.add_sticker(b"\x89PNG\r\n\x1a\nnot-enough", category="A")
        self.assertEqual(status, "invalid_image")

    def test_dimension_bounds_rejected(self):
        store = _make_store(self.root, max_dimension=32)
        status, _ = store.add_sticker(png(64, 10), category="A")
        self.assertEqual(status, "invalid_dimensions")

    def test_pixel_bounds_rejected(self):
        store = _make_store(self.root, max_pixels=100)
        status, _ = store.add_sticker(png(20, 20), category="A")
        self.assertEqual(status, "invalid_dimensions")

    def test_path_traversal_category_rejected(self):
        for value in ("../escape", "a/b", "a\\b", "..", "."):
            status, entry = self.store.add_sticker(png(tag=20), category=value)
            self.assertEqual(status, "invalid")
            self.assertIsNone(entry)
        self.assertFalse((self.root.parent / "escape").exists())

    def test_category_symlink_rejected(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.root.mkdir(parents=True)
        (self.root / "链接").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(self.store.category_dir("链接"))
        status, _ = self.store.add_sticker(png(tag=21), category="链接")
        self.assertEqual(status, "storage")

    def test_file_symlink_rejected(self):
        data = png(tag=22)
        digest_file = self._filename_for(data)
        category = self.store.category_dir("A", create=True)
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(data)
        (category / digest_file).symlink_to(outside)
        status, _ = self.store.add_sticker(data, category="A")
        self.assertEqual(status, "storage")
        self.assertTrue(outside.exists())

    def _filename_for(self, data: bytes) -> str:
        import hashlib

        mime, ext = detect_image_magic(data)
        return hashlib.sha256(data).hexdigest() + ext

    def test_index_symlink_ignored_and_refused(self):
        self.root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside-index.json"
        outside.write_text('{"stickers": []}', encoding="utf-8")
        self.store.index_path.symlink_to(outside)
        self.assertEqual(self.store.load_index(), [])
        with self.assertRaises(StickerStoreError):
            self.store.save_index([])

    def test_malformed_index_is_safe(self):
        self.root.mkdir(parents=True)
        self.store.index_path.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.store.load_index(), [])

    def test_forged_index_paths_are_skipped(self):
        self.root.mkdir(parents=True)
        fake_sha = "a" * 64
        payload = {
            "version": 2,
            "stickers": [
                {
                    "sha256": fake_sha,
                    "filename": fake_sha + ".png",
                    "category": "../escape",
                },
                {
                    "sha256": fake_sha,
                    "filename": fake_sha + ".png",
                    "category": "safe",
                },
            ],
        }
        self.store.index_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.load_index(), [])

    def test_filename_sha_mismatch_skipped(self):
        status, entry = self.store.add_sticker(png(tag=23), category="A")
        self.assertEqual(status, "saved")
        payload = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        payload["stickers"][0]["filename"] = "b" * 64 + ".png"
        self.store.index_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.load_index(), [])

    def test_recorded_oversize_dimensions_are_skipped(self):
        self.store.add_sticker(png(tag=241), category="A")
        payload = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        payload["stickers"][0]["width"] = 10_000
        self.store.index_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.load_index(), [])

    def test_recorded_size_mismatch_skipped(self):
        self.store.add_sticker(png(tag=24), category="A")
        payload = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        payload["stickers"][0]["size"] += 1
        self.store.index_path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(self.store.load_index(), [])

    def test_multi_category_membership_and_counts(self):
        data = png(tag=25)
        status, entry = self.store.add_sticker(
            data, category="A", categories=["A", "B", "C"]
        )
        self.assertEqual(status, "saved")
        self.assertEqual(entry["categories"], ["A", "B", "C"])
        counts = self.store.category_counts()
        self.assertEqual(counts, {"A": 1, "B": 1, "C": 1})

    def test_semantic_metadata_round_trip(self):
        data = png(tag=26)
        self.store.add_sticker(
            data,
            category="语义",
            tags=["开心", "happy"],
            meaning="表示  开心 的笑容",
            tone="expressive",
        )
        entry = self.store.load_index()[0]
        self.assertEqual(entry["tags"], ["开心", "happy"])
        self.assertEqual(entry["meaning"], "表示 开心 的笑容")
        self.assertEqual(entry["tone"], "expressive")
        self.assertEqual((entry["width"], entry["height"], entry["mime"]), (16, 16, "image/png"))

    def test_invalid_tag_rejected(self):
        status, entry = self.store.add_sticker(
            png(tag=27), category="A", tags=["bad tag"]
        )
        self.assertEqual(status, "invalid")
        self.assertIsNone(entry)

    def test_categories_string_rejected_not_split_into_characters(self):
        status, entry = self.store.add_sticker(
            png(tag=281), category="A", categories="ABC"
        )
        self.assertEqual(status, "invalid")
        self.assertIsNone(entry)

    def test_too_many_categories_rejected(self):
        status, _ = self.store.add_sticker(
            png(tag=28), category="A", categories=["A", "B", "C", "D", "E"]
        )
        self.assertEqual(status, "invalid")

    def test_reclassify_moves_file_and_updates_index(self):
        status, original = self.store.add_sticker(png(tag=29), category="旧")
        self.assertEqual(status, "saved")
        entries = self.store.load_index()
        status, updated = self.store.reclassify(entries, original["sha256"], "新")
        self.assertEqual(status, "moved")
        self.assertEqual(updated["category"], "新")
        self.assertFalse(Path(original["path"]).exists())
        self.assertTrue(Path(updated["path"]).is_file())
        loaded = self.store.load_index()[0]
        self.assertEqual(loaded["category"], "新")
        self.assertEqual(loaded["categories"], ["新"])

    def test_add_remove_categories_preserve_file(self):
        status, entry = self.store.add_sticker(png(tag=30), category="A")
        self.assertEqual(status, "saved")
        entries = self.store.load_index()
        status, updated = self.store.add_categories(entries, entry["sha256"], ["B", "C"])
        self.assertEqual(status, "updated")
        self.assertEqual(updated["category"], "A")
        self.assertEqual(updated["categories"], ["A", "B", "C"])
        status, updated = self.store.remove_categories(
            self.store.load_index(), entry["sha256"], ["B"]
        )
        self.assertEqual(status, "updated")
        self.assertEqual(updated["categories"], ["A", "C"])
        self.assertTrue(Path(updated["path"]).is_file())

    def test_update_metadata_same_status(self):
        self.store.add_sticker(png(tag=31), category="A", tags=["x"])
        entry = self.store.load_index()[0]
        status, same = self.store.update_metadata(
            [entry], entry["sha256"], tags=["x"]
        )
        self.assertEqual(status, "same")
        self.assertEqual(same["tags"], ["x"])

    def test_write_sticker_rejects_bad_filename(self):
        with self.assertRaises(StickerStoreError):
            self.store.write_sticker("A", "../../evil.png", png())
        with self.assertRaises(StickerStoreError):
            self.store.write_sticker("../A", "a" * 64 + ".png", png())

    def test_index_write_failure_rolls_back_new_file(self):
        store = _make_store(self.root)
        original = store.atomic_write_bytes

        def failing_write(path, data, **kwargs):
            if Path(path).name == "index.json":
                raise OSError("simulated index replace failure")
            return original(path, data, **kwargs)

        store.atomic_write_bytes = failing_write
        status, entry = store.add_sticker(png(tag=330), category="A")
        self.assertEqual(status, "storage")
        self.assertIsNone(entry)
        image_files = [
            item
            for item in self.root.rglob("*")
            if item.is_file() and item.suffix in {".png", ".gif", ".jpg"}
        ]
        self.assertEqual(image_files, [])
        self.assertFalse(store.index_path.exists())

    def test_failed_second_write_preserves_previous_index_and_file(self):
        self.store.add_sticker(png(tag=331), category="A")
        before_payload = self.store.index_path.read_bytes()
        before_entries = self.store.load_index()
        original = self.store.atomic_write_bytes

        def failing_write(path, data, **kwargs):
            if Path(path).name == "index.json":
                raise OSError("simulated failure")
            return original(path, data, **kwargs)

        self.store.atomic_write_bytes = failing_write
        status, _entry = self.store.add_sticker(png(tag=332), category="A")
        self.assertEqual(status, "storage")
        self.assertEqual(self.store.index_path.read_bytes(), before_payload)
        after_entries = self.store.load_index()
        self.assertEqual(len(after_entries), len(before_entries))
        images = [
            item
            for item in self.root.rglob("*")
            if item.is_file() and item.suffix in {".png", ".gif", ".jpg"}
        ]
        self.assertEqual(len(images), 1)

    def test_existing_corrupt_digest_file_is_rejected(self):
        data = png(tag=333)
        filename = self._filename_for(data)
        category = self.store.category_dir("A", create=True)
        (category / filename).write_bytes(b"not the same content")
        status, entry = self.store.add_sticker(data, category="A")
        self.assertEqual(status, "storage")
        self.assertIsNone(entry)

    def test_index_entry_with_wrong_magic_is_ignored(self):
        status, entry = self.store.add_sticker(png(tag=334), category="A")
        self.assertEqual(status, "saved")
        Path(entry["path"]).write_bytes(b"BM" + b"\x00" * 40)
        self.assertEqual(self.store.load_index(), [])

    def test_atomic_write_leaves_no_temp_files(self):
        self.store.add_sticker(png(tag=32), category="A")
        leftovers = [
            item
            for item in self.root.rglob("*")
            if item.name.startswith(".tmp-") or item.name.endswith(".part")
        ]
        self.assertEqual(leftovers, [])

    def test_valid_entries_drops_deleted_file(self):
        self.store.add_sticker(png(tag=33), category="A")
        entries = self.store.load_index()
        Path(entries[0]["path"]).unlink()
        self.assertEqual(self.store.valid_entries(entries), [])

    def test_shared_lock_read_is_safe(self):
        self.store.add_sticker(png(tag=34), category="A")
        with self.store.file_lock(exclusive=False):
            self.assertEqual(len(self.store._load_index_unlocked()), 1)

    def test_concurrent_multiwriter_serialization(self):
        self.root.mkdir(parents=True)
        jobs = [(str(self.root), index) for index in range(2)]
        context = multiprocessing.get_context("fork")
        with context.Pool(2) as pool:
            statuses = pool.map(_concurrent_add, jobs)
        self.assertEqual(statuses, ["ok", "ok"])
        entries = self.store.load_index()
        self.assertEqual(len(entries), 16)
        payload = json.loads(self.store.index_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["stickers"]), 16)
        shas = [item["sha256"] for item in payload["stickers"]]
        self.assertEqual(len(shas), len(set(shas)))

    def test_concurrent_same_sha_saves_once(self):
        self.root.mkdir(parents=True)
        data = png(24, 24, tag=440)
        context = multiprocessing.get_context("fork")
        with context.Pool(2) as pool:
            results = pool.map(
                _concurrent_add_same,
                [(str(self.root), data), (str(self.root), data)],
            )
        statuses = sorted(status for status, _sha in results)
        self.assertEqual(statuses, ["duplicate", "saved"])
        entries = self.store.load_index()
        self.assertEqual(len(entries), 1)
        images = [
            item
            for item in self.root.rglob("*")
            if item.is_file() and item.name != "index.json" and not item.name.startswith(".")
        ]
        self.assertEqual(len(images), 1)

    def test_permission_repair_skips_symlinks_and_sets_modes(self):
        self.store.add_sticker(png(tag=441), category="A")
        self.store.lock_path.chmod(0o666)
        self.store.index_path.chmod(0o666)
        image = self.store.load_index()[0]["path"]
        Path(image).chmod(0o666)
        outside = Path(self.temp.name) / "outside-mode"
        outside.write_text("outside", encoding="utf-8")
        outside.chmod(0o600)
        (self.root / "A" / "link").symlink_to(outside)
        summary = self.store.migrate_permissions(self.root.parent)
        self.assertGreaterEqual(summary["dirs"], 1)
        self.assertGreaterEqual(summary["skipped"], 1)
        self.assertEqual(self.store.index_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(Path(image).stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.store.lock_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o600)

    def test_gif_extension_selected_from_magic(self):
        status, entry = self.store.add_sticker(gif(tag=35), category="GIF")
        self.assertEqual(status, "saved")
        self.assertTrue(entry["filename"].endswith(".gif"))
        self.assertEqual(entry["mime"], "image/gif")


if __name__ == "__main__":
    unittest.main()
