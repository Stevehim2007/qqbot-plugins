"""One-way, idempotent import of legacy qqbot_enhancer stickers.

The legacy tree is treated as read-only input.  Nothing is renamed, chmodded,
deleted or rewritten there.  The destination store remains authoritative and
an existing digest is never overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:
    from .media import IMAGE_PATH_EXTENSIONS, ImageValidationError, inspect_image_bytes
    from .sticker_store import (
        COLLECTED_INDEX_FILENAME,
        CollectedStickerStore,
        DEFAULT_COLLECTED_CATEGORY,
        normalize_category_name,
    )
except ImportError:  # direct test harness
    from media import IMAGE_PATH_EXTENSIONS, ImageValidationError, inspect_image_bytes
    from sticker_store import (
        COLLECTED_INDEX_FILENAME,
        CollectedStickerStore,
        DEFAULT_COLLECTED_CATEGORY,
        normalize_category_name,
    )

MAX_LEGACY_INDEX_BYTES = 8 * 1024 * 1024
_TAG_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]{1,64}$")


@dataclass
class MigrationResult:
    imported: int = 0
    duplicates: int = 0
    skipped: int = 0
    errors: int = 0
    scanned: int = 0
    malformed_index: bool = False
    legacy_preserved: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def get(self, key: str, default: Any = None) -> Any:
        return self.as_dict().get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __contains__(self, key: object) -> bool:
        return key in self.as_dict()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _read_bounded_regular(path: Path, limit: int) -> bytes | None:
    """Read a regular non-symlink file without following a final symlink."""

    try:
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            return None
    except OSError:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        data = os.read(fd, max(1, int(limit)))
        if not data:
            return b""
        if len(data) >= limit and os.read(fd, 1):
            return None
        return data
    except OSError:
        return None
    finally:
        os.close(fd)


def _within(path: Path, root: Path) -> bool:
    try:
        if path.is_symlink():
            return False
        resolved = path.resolve()
        base = root.resolve()
        return resolved == base or base in resolved.parents
    except (OSError, ValueError):
        return False


def _legacy_index_metadata(
    legacy_root: Path,
    result: MigrationResult,
    category_normalizer: Callable[[Any], str | None],
) -> dict[str, dict[str, Any]]:
    index_path = legacy_root / COLLECTED_INDEX_FILENAME
    if not index_path.exists():
        return {}
    if index_path.is_symlink():
        result.malformed_index = True
        result.skipped += 1
        return {}
    raw = _read_bounded_regular(index_path, MAX_LEGACY_INDEX_BYTES)
    if raw is None:
        result.malformed_index = True
        result.skipped += 1
        return {}
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        result.malformed_index = True
        result.skipped += 1
        return {}
    if isinstance(payload, list):
        raw_entries = payload
    elif isinstance(payload, dict):
        raw_entries = payload.get("stickers", [])
    else:
        result.malformed_index = True
        result.skipped += 1
        return {}
    if not isinstance(raw_entries, list):
        result.malformed_index = True
        result.skipped += 1
        return {}

    metadata: dict[str, dict[str, Any]] = {}
    for item in raw_entries:
        if not isinstance(item, dict):
            result.skipped += 1
            continue
        category = category_normalizer(item.get("category"))
        filename = str(item.get("filename", "") or "")
        if category is None or not filename or "/" in filename or "\\" in filename:
            result.skipped += 1
            continue
        candidate = legacy_root / category / filename
        if not _within(candidate, legacy_root):
            result.skipped += 1
            continue
        raw_categories = item.get("categories")
        categories: list[str] = []
        if isinstance(raw_categories, (list, tuple, set)):
            for raw_category in raw_categories:
                normalized = category_normalizer(raw_category)
                if normalized and normalized not in categories:
                    categories.append(normalized)
        if category not in categories:
            categories.insert(0, category)
        else:
            categories.remove(category)
            categories.insert(0, category)
        raw_tags = item.get("tags", [])
        tags: list[str] = []
        if isinstance(raw_tags, (list, tuple, set)):
            for raw_tag in raw_tags:
                tag = str(raw_tag or "").strip()
                if (
                    tag
                    and len(tag) <= 64
                    and _TAG_RE.fullmatch(tag)
                    and tag not in tags
                ):
                    tags.append(tag)
        meaning = str(item.get("meaning", "") or "").strip()
        if len(meaning) > 200 or any(
            ord(char) < 32 or ord(char) == 127 for char in meaning
        ):
            meaning = ""
        tone = str(item.get("tone", "") or "").strip()
        if len(tone) > 64 or not _TAG_RE.fullmatch(tone):
            tone = ""
        digest = str(item.get("sha256", "") or "").lower()
        metadata[str(candidate)] = {
            "category": category,
            "categories": categories,
            "tags": tags,
            "meaning": meaning,
            "tone": tone,
            "indexed_sha256": digest if re.fullmatch(r"[a-f0-9]{64}", digest) else "",
        }
    return metadata


def _iter_legacy_regular_files(
    legacy_root: Path,
    *,
    max_scan_files: int,
) -> Iterator[Path]:
    """Yield regular candidate files without following any symlink."""

    if not legacy_root.is_dir() or legacy_root.is_symlink():
        return
    yielded = 0
    for dirpath, dirnames, filenames in os.walk(
        legacy_root,
        topdown=True,
        followlinks=False,
        onerror=lambda _error: None,
    ):
        directory = Path(dirpath)
        kept: list[str] = []
        for dirname in dirnames:
            child = directory / dirname
            if child.is_symlink():
                continue
            kept.append(dirname)
        dirnames[:] = kept
        for filename in filenames:
            path = directory / filename
            if path.is_symlink() or not _within(path, legacy_root):
                continue
            try:
                if not stat.S_ISREG(path.lstat().st_mode):
                    continue
            except OSError:
                continue
            if path.name == COLLECTED_INDEX_FILENAME:
                continue
            lowered = path.name.lower()
            if lowered.startswith(".tmp-") or lowered.endswith(
                (".tmp", ".part", ".lock")
            ):
                continue
            if path.suffix.lower() not in IMAGE_PATH_EXTENSIONS:
                # Legacy filenames might be extensionless.  Their bytes are
                # still magic-checked by the caller, so keep them as candidates.
                pass
            yielded += 1
            if yielded > max(1, int(max_scan_files)):
                return
            yield path


def migrate_legacy_stickers(
    legacy_root: Path | str,
    new_root: Path | str | CollectedStickerStore,
    *,
    max_file_bytes: int = 8 * 1024 * 1024,
    max_dimension: int = 4096,
    max_pixels: int = 16_777_216,
    max_count: int = 200,
    max_scan_files: int = 5000,
    default_category: str = DEFAULT_COLLECTED_CATEGORY,
    category_normalizer: Callable[[Any], str | None] | None = None,
) -> MigrationResult:
    """Import valid legacy images into ``new_root`` without modifying legacy.

    The operation is idempotent by the SHA-256 of image bytes.  A malformed or
    forged legacy index does not prevent a best-effort safe directory scan.
    Existing destination entries are never overwritten.
    """

    result = MigrationResult()
    legacy = Path(legacy_root)
    # Accept either the qqbot_enhancer data directory or its
    # collected_stickers child, which keeps the API convenient in tests.
    if (
        legacy.name != "collected_stickers"
        and not legacy.is_symlink()
        and legacy.is_dir()
    ):
        child = legacy / "collected_stickers"
        if child.is_dir() and not child.is_symlink():
            legacy = child
    try:
        if not legacy.exists() or not legacy.is_dir() or legacy.is_symlink():
            return result
        legacy_resolved = legacy.resolve()
    except (OSError, ValueError):
        result.errors += 1
        return result

    normalizer = category_normalizer or (
        lambda value: normalize_category_name(
            value, default_category=default_category
        )
    )
    if isinstance(new_root, CollectedStickerStore):
        store = new_root
    else:
        try:
            store = CollectedStickerStore(
                Path(new_root),
                max_file_bytes=max_file_bytes,
                category_normalizer=normalizer,
                default_category=default_category,
                max_count=max_count,
                max_dimension=max_dimension,
                max_pixels=max_pixels,
            )
        except Exception:
            result.errors += 1
            return result
    try:
        store_resolved = store.root.resolve()
        if (
            store_resolved == legacy_resolved
            or legacy_resolved in store_resolved.parents
            or store_resolved in legacy_resolved.parents
        ):
            # Never migrate a directory into itself or a nested overlap; this
            # also guarantees the legacy tree cannot be modified by destination
            # writes.
            result.errors += 1
            return result
    except (OSError, ValueError):
        pass

    metadata_by_path = _legacy_index_metadata(
        legacy_resolved, result, normalizer
    )
    seen_legacy_digests: set[str] = set()
    for path in _iter_legacy_regular_files(
        legacy_resolved, max_scan_files=max_scan_files
    ):
        result.scanned += 1
        try:
            # Pass one extra byte to detect oversize files without reading them
            # fully.  The destination store repeats all checks under its lock.
            data = _read_bounded_regular(
                path, max(1, int(max_file_bytes)) + 1
            )
            if data is None or len(data) > max_file_bytes:
                result.skipped += 1
                continue
            try:
                inspect_image_bytes(
                    data,
                    max_bytes=max_file_bytes,
                    max_dimension=max_dimension,
                    max_pixels=max_pixels,
                )
            except ImageValidationError:
                result.skipped += 1
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_legacy_digests:
                result.duplicates += 1
                continue

            relative = path.relative_to(legacy_resolved)
            if relative.parent == Path("."):
                scan_category = normalizer(default_category)
            else:
                scan_category = normalizer(relative.parent.name)
                if scan_category is None:
                    scan_category = normalizer(default_category)
            if scan_category is None:
                result.skipped += 1
                continue
            metadata = metadata_by_path.get(str(path), {})
            metadata_category = (
                normalizer(metadata.get("category"))
                if metadata.get("category")
                else None
            )
            category = metadata_category or scan_category
            raw_categories = metadata.get("categories")
            categories: list[str] = []
            if isinstance(raw_categories, (list, tuple, set)):
                for value in raw_categories:
                    normalized = normalizer(value)
                    if normalized and normalized not in categories:
                        categories.append(normalized)
            if category in categories:
                categories.remove(category)
            categories.insert(0, category)
            categories = categories[: max(1, store.max_categories)]
            raw_tags = metadata.get("tags", [])
            tags: list[str] = []
            if store.max_tags <= 0:
                raw_tags = []
            if isinstance(raw_tags, (list, tuple, set)):
                for raw_tag in raw_tags:
                    tag = str(raw_tag or "").strip()
                    if (
                        tag
                        and len(tag) <= store.max_tag_chars
                        and _TAG_RE.fullmatch(tag)
                        and tag not in tags
                    ):
                        tags.append(tag)
                    if len(tags) >= store.max_tags:
                        break
            meaning = re.sub(
                r"[\x00-\x1f\x7f]+",
                " ",
                str(metadata.get("meaning", "") or ""),
            ).strip()
            meaning = re.sub(r"\s+", " ", meaning)[: store.max_meaning_chars]
            raw_tone = str(metadata.get("tone", "") or "").strip()
            tone = (
                raw_tone
                if raw_tone
                and len(raw_tone) <= store.max_tone_chars
                and _TAG_RE.fullmatch(raw_tone)
                else ""
            )
            status, _entry = store.add_sticker(
                data,
                category=category,
                categories=categories,
                tags=tags,
                meaning=meaning,
                tone=tone,
                source="legacy-migration",
                merge_duplicate=False,
            )
            seen_legacy_digests.add(digest)
            if status == "saved":
                result.imported += 1
            elif status == "duplicate":
                result.duplicates += 1
            elif status == "limit":
                result.skipped += 1
            else:
                result.skipped += 1
        except Exception:
            result.errors += 1
    return result


# Stable descriptive aliases for plugin lifecycle code and external callers.
import_legacy_stickers = migrate_legacy_stickers
migrate_legacy_collected_stickers = migrate_legacy_stickers
migrate_from_legacy = migrate_legacy_stickers
