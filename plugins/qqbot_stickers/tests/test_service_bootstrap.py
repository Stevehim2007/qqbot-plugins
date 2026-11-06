from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from sticker_service import StickerService
from sticker_store import CollectedStickerStore, normalize_category_name
from tests.image_factory import png


class LegacyBootstrapTests(unittest.TestCase):
    def test_legacy_categories_above_limit_are_all_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "collected_stickers"
            root.mkdir(parents=True)
            entries = []
            for index in range(5):
                data = png(4, 4, tag=index + 1)
                digest = hashlib.sha256(data).hexdigest()
                category = f"分类{index}"
                path = root / category / f"{digest}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                entries.append(
                    {
                        "sha256": digest,
                        "filename": f"{digest}.png",
                        "category": category,
                        "path": str(path),
                        "size": len(data),
                        "mime": "image/png",
                        "width": 4,
                        "height": 4,
                    }
                )
            (root / "index.json").write_text(
                json.dumps({"version": 1, "stickers": entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            store = CollectedStickerStore(
                root,
                max_file_bytes=100_000,
                category_normalizer=lambda value: normalize_category_name(value, max_chars=32),
                max_count=50,
                max_dimension=512,
                max_pixels=500_000,
            )
            service = StickerService(root, store=store, max_categories=2)
            try:
                asyncio.run(service.initialize(owner_epoch=1))
                state = json.loads(service.db_path.read_text(encoding="utf-8"))
                categories = [
                    row
                    for row in state["categories"].values()
                    if not row.get("tombstone")
                ]
                collections = [
                    row
                    for row in state["collections"].values()
                    if not row.get("tombstone")
                ]
                self.assertEqual(len(categories), 5)
                self.assertEqual(len(collections), 5)
            finally:
                service.close()

    def test_new_category_still_rejected_above_limit_after_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "collected_stickers"
            root.mkdir(parents=True)
            store = CollectedStickerStore(
                root,
                max_file_bytes=100_000,
                category_normalizer=lambda value: normalize_category_name(value, max_chars=32),
                max_count=50,
                max_dimension=512,
                max_pixels=500_000,
            )
            service = StickerService(root, store=store, max_categories=1)
            try:
                asyncio.run(service.initialize(owner_epoch=1))
                with self.assertRaises(Exception):
                    asyncio.run(
                        service.category_create(
                            _context(), {"name": "新分类"}
                        )
                    )
            finally:
                service.close()


def _context():
    from contracts import RequestContext

    return RequestContext(
        instance_id="bot-1",
        actor_id="admin",
        scope=("admin", "global"),
        owner_epoch=1,
    )


if __name__ == "__main__":
    unittest.main()
