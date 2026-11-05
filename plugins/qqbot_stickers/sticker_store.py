"""Crash-safe collected-sticker persistence.

The store is deliberately independent of AstrBot.  It accepts a root path,
config-derived limits and a category normalizer from the plugin composition
root.  Untrusted index data is never allowed to select a path outside the
plugin's ``collected_stickers`` directory.

Safety model
------------
* a process-wide ``fcntl.flock`` lock serializes all mutations, including the
  read/check/write sequence that implements SHA dedupe and bounded counts;
* callers may additionally hold an ``asyncio.Lock`` (the plugin does) so one
  event loop cannot queue an unbounded amount of blocking writer work;
* files are written to a temporary sibling, fsynced, then atomically replaced;
* image files are 0644, while index/lock files are 0600;
* symlinks, non-regular files, traversal components and forged filename/category
  pairs are rejected at both read and write time.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX AstrBot deployments provide fcntl.  Keep imports portable.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]

try:
    from .media import ImageValidationError, detect_image_magic, inspect_image_bytes
except ImportError:  # direct test harness
    from media import ImageValidationError, detect_image_magic, inspect_image_bytes

COLLECTED_STICKER_DIR_NAME = "collected_stickers"
COLLECTED_INDEX_FILENAME = "index.json"
COLLECTED_LOCK_FILENAME = ".write.lock"
COLLECTED_CATEGORY_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]{1,32}$")
COLLECTED_FILENAME_RE = re.compile(
    r"^[a-f0-9]{64}\.(?:jpg|png|gif|webp|bmp|avif)$"
)
COLLECTED_DIR_MODE = 0o755
COLLECTED_FILE_MODE = 0o644
COLLECTED_INDEX_MODE = 0o600
COLLECTED_LOCK_MODE = 0o600
SCHEMA_VERSION = 2
MAX_INDEX_BYTES = 8 * 1024 * 1024
DEFAULT_COLLECTED_CATEGORY = "未分类"
_TAG_RE = re.compile(r"^[0-9A-Za-z_\-\u4e00-\u9fff]{1,64}$")
_SOURCE_RE = re.compile(r"^[0-9A-Za-z_.:\-]{1,64}$")


class StickerStoreError(ValueError):
    """Invalid sticker-store operation; callers should degrade safely."""


def normalize_category_name(
    raw: Any,
    *,
    default_category: str = DEFAULT_COLLECTED_CATEGORY,
    max_chars: int = 32,
) -> str | None:
    """Normalize one category and reject path-like or control input."""

    value = str(raw or "").strip()
    if not value:
        value = default_category
    limit = max(1, min(32, int(max_chars)))
    if len(value) > limit or value in {".", ".."}:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if not COLLECTED_CATEGORY_RE.fullmatch(value):
        return None
    return value


class CollectedStickerStore:
    """Per-plugin bounded sticker store with atomic index updates."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_file_bytes: int,
        category_normalizer: Callable[[Any], str | None] | None = None,
        default_category: str = DEFAULT_COLLECTED_CATEGORY,
        clock: Callable[[], float] = time.time,
        log_warning: Callable[[str], None] | None = None,
        max_tags: int = 12,
        max_tag_chars: int = 24,
        max_meaning_chars: int = 200,
        max_tone_chars: int = 24,
        max_categories: int = 8,
        max_count: int = 200,
        max_dimension: int = 4096,
        max_pixels: int = 16_777_216,
        min_dimension: int = 1,
    ) -> None:
        raw_root = Path(root)
        if raw_root.is_symlink():
            raise StickerStoreError("sticker root must not be a symlink")
        self.root = raw_root
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.category_normalizer = (
            category_normalizer
            or (
                lambda value: normalize_category_name(
                    value, default_category=default_category
                )
            )
        )
        self.default_category = default_category
        self.clock = clock
        self.log_warning = log_warning
        self.max_tags = max(0, int(max_tags))
        self.max_tag_chars = max(1, min(64, int(max_tag_chars)))
        self.max_meaning_chars = max(1, int(max_meaning_chars))
        self.max_tone_chars = max(1, int(max_tone_chars))
        self.max_categories = max(1, min(32, int(max_categories)))
        self.max_count = max(1, int(max_count))
        self.max_dimension = max(1, int(max_dimension))
        self.max_pixels = max(1, int(max_pixels))
        self.min_dimension = max(1, int(min_dimension))

    # ------------------------------------------------------------------
    # Paths, locks and low-level I/O
    # ------------------------------------------------------------------
    def _warn(self, message: str) -> None:
        if self.log_warning is not None:
            try:
                self.log_warning(message)
            except Exception:
                pass

    def _ensure_root_mode(self) -> None:
        if self.root.is_symlink():
            raise StickerStoreError("sticker root must not be a symlink")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=COLLECTED_DIR_MODE)
            if not self.root.is_dir() or self.root.is_symlink():
                raise StickerStoreError("sticker root is not a regular directory")
            try:
                self.root.chmod(COLLECTED_DIR_MODE)
            except OSError as exc:
                self._warn(f"cannot set sticker root permissions: {exc}")
        except StickerStoreError:
            raise
        except OSError as exc:
            raise StickerStoreError(f"cannot prepare sticker root: {exc}") from exc

    @property
    def index_path(self) -> Path:
        return self.root / COLLECTED_INDEX_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.root / COLLECTED_LOCK_FILENAME

    def _relative_resolved(self, path: Path) -> Path:
        base = self.root.resolve()
        if path.is_symlink():
            raise StickerStoreError("symlink path rejected")
        try:
            resolved = path.resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise StickerStoreError("unresolvable sticker path") from exc
        if resolved != base and base not in resolved.parents:
            raise StickerStoreError("sticker path escapes data root")
        return resolved

    def _is_within(self, path: Path, base: Path) -> bool:
        try:
            if path.is_symlink():
                return False
            resolved = path.resolve()
            resolved_base = base.resolve()
            return resolved == resolved_base or resolved_base in resolved.parents
        except (OSError, ValueError):
            return False

    def _chmod_safe(self, path: Path, mode: int, base: Path) -> bool:
        try:
            if path.is_symlink() or not self._is_within(path, base):
                self._warn("skip permission repair outside/through symlink")
                return False
            path.chmod(mode)
            return True
        except OSError:
            self._warn("sticker permission repair failed safely")
            return False

    def _safe_regular_file(self, path: Path) -> Path | None:
        try:
            if path.is_symlink():
                return None
            resolved = self._relative_resolved(path)
            mode = resolved.stat().st_mode
        except (OSError, StickerStoreError, ValueError):
            return None
        if not stat.S_ISREG(mode):
            return None
        return resolved

    @contextlib.contextmanager
    def file_lock(self, *, exclusive: bool = True) -> Iterator[None]:
        """Hold the cross-process store lock.

        A single lock file protects index + sticker-file mutations.  Shared
        locks are used for reads that participate in a later mutation, so a
        reader never observes a half-replaced index.
        """

        self._ensure_root_mode()
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, COLLECTED_LOCK_MODE)
        except OSError as exc:
            raise StickerStoreError(f"cannot open sticker lock: {exc}") from exc
        try:
            try:
                os.fchmod(fd, COLLECTED_LOCK_MODE)
            except OSError:
                pass
            if fcntl is not None:
                try:
                    fcntl.flock(
                        fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    )
                except OSError as exc:
                    raise StickerStoreError(
                        f"cannot acquire sticker lock: {exc}"
                    ) from exc
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            try:
                os.close(fd)
            except OSError:
                pass

    @contextlib.contextmanager
    def mutation_lock(self) -> Iterator[None]:
        """Public alias useful to plugins/tests that need a compound write."""

        with self.file_lock(exclusive=True):
            yield

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(directory, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @classmethod
    def atomic_write_bytes(
        cls,
        path: Path,
        data: bytes,
        *,
        mode: int = COLLECTED_FILE_MODE,
        log_warning: Callable[[str], None] | None = None,
    ) -> None:
        """Write bytes via temp + fsync + os.replace and set a final mode."""

        target = Path(path)
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True, mode=COLLECTED_DIR_MODE)
        if target.is_symlink():
            raise StickerStoreError("refusing to replace symlink target")
        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(
                prefix=".tmp-", suffix=".part", dir=str(parent)
            )
            try:
                if hasattr(os, "fchmod"):
                    os.fchmod(
                        fd,
                        COLLECTED_INDEX_MODE
                        if mode == COLLECTED_INDEX_MODE
                        else 0o600,
                    )
                with os.fdopen(fd, "wb", closefd=True) as temporary:
                    temporary.write(data)
                    temporary.flush()
                    os.fsync(temporary.fileno())
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(temp_path, target)
            temp_path = ""
            try:
                os.chmod(target, mode)
            except OSError as exc:
                if log_warning is not None:
                    log_warning(f"cannot set permissions on {target.name}: {exc}")
            cls._fsync_directory(parent)
        except Exception:
            if temp_path:
                try:
                    Path(temp_path).unlink(missing_ok=True)
                except OSError:
                    pass
            raise

    def _read_prefix_nofollow(self, path: Path, limit: int = 32) -> bytes | None:
        safe = self._safe_regular_file(path)
        if safe is None:
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(safe, flags)
        except OSError:
            return None
        try:
            return os.read(fd, max(1, int(limit)))
        except OSError:
            return None
        finally:
            os.close(fd)

    def _read_bytes_nofollow(self, path: Path, limit: int) -> bytes | None:
        safe = self._safe_regular_file(path)
        if safe is None:
            return None
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(safe, flags)
        except OSError:
            return None
        try:
            chunks: list[bytes] = []
            remaining = max(1, int(limit))
            while remaining > 0:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            if remaining <= 0:
                # One extra byte proves the object exceeds the bound.
                if os.read(fd, 1):
                    return None
            return b"".join(chunks)
        except OSError:
            return None
        finally:
            os.close(fd)

    # ------------------------------------------------------------------
    # Category and filename validation
    # ------------------------------------------------------------------
    def category_dir(self, category: str, *, create: bool = False) -> Path | None:
        raw = str(category or "").strip()
        normalized = self.category_normalizer(raw)
        if normalized is None or normalized != raw:
            return None
        if "/" in raw or "\\" in raw or raw in {".", ".."}:
            return None
        self._ensure_root_mode()
        base = self.root.resolve()
        candidate = base / raw
        try:
            if candidate.is_symlink():
                return None
            if create:
                candidate.mkdir(parents=False, exist_ok=True, mode=COLLECTED_DIR_MODE)
            if not candidate.is_dir() or candidate.is_symlink():
                return None
            resolved = candidate.resolve()
        except (OSError, ValueError):
            return None
        if resolved.parent != base:
            return None
        try:
            resolved.chmod(COLLECTED_DIR_MODE)
        except OSError:
            pass
        return resolved

    def file_path(self, category: str, filename: str) -> Path | None:
        if not COLLECTED_FILENAME_RE.fullmatch(str(filename or "")):
            return None
        category_dir = self.category_dir(category, create=False)
        if category_dir is None:
            return None
        candidate = category_dir / filename
        safe = self._safe_regular_file(candidate)
        if safe is None or safe.parent != category_dir:
            return None
        return safe

    # ------------------------------------------------------------------
    # Metadata sanitization and serialization
    # ------------------------------------------------------------------
    def _sanitize_tags(self, raw: Any) -> list[str] | None:
        if raw is None:
            return []
        if not isinstance(raw, (list, tuple, set)):
            return None
        tags: list[str] = []
        for item in raw:
            tag = str(item or "").strip()
            if not tag or len(tag) > self.max_tag_chars:
                return None
            if not _TAG_RE.fullmatch(tag):
                return None
            if tag not in tags:
                tags.append(tag)
        if len(tags) > self.max_tags:
            return None
        return tags

    def _sanitize_meaning(self, raw: Any) -> str | None:
        meaning = str(raw or "").strip()
        if len(meaning) > self.max_meaning_chars:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in meaning):
            return None
        return re.sub(r"\s+", " ", meaning).strip()

    def _sanitize_tone(self, raw: Any) -> str | None:
        tone = str(raw or "").strip()
        if not tone:
            return ""
        if len(tone) > self.max_tone_chars:
            return None
        if not _TAG_RE.fullmatch(tone):
            return None
        return tone

    def _sanitize_source(self, raw: Any) -> str:
        source = str(raw or "").strip()
        return source if _SOURCE_RE.fullmatch(source) else ""

    def _sanitize_categories(
        self, raw: Any, *, primary: str | None = None
    ) -> list[str] | None:
        if isinstance(raw, (str, bytes)) or not isinstance(
            raw, (list, tuple, set)
        ):
            return None
        categories: list[str] = []
        for item in raw:
            category = self.category_normalizer(item)
            if category is None:
                return None
            if category not in categories:
                categories.append(category)
        if primary is not None:
            primary_norm = self.category_normalizer(primary)
            if primary_norm is None:
                return None
            if primary_norm in categories:
                categories.remove(primary_norm)
            categories.insert(0, primary_norm)
        if not categories or len(categories) > self.max_categories:
            return None
        return categories

    def _serialize_entry(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        sha256 = str(entry.get("sha256", "") or "").lower()
        filename = str(entry.get("filename", "") or "")
        category = self.category_normalizer(entry.get("category"))
        if (
            not re.fullmatch(r"[a-f0-9]{64}", sha256)
            or category is None
            or category != str(entry.get("category", "") or "").strip()
            or not COLLECTED_FILENAME_RE.fullmatch(filename)
            or not filename.startswith(sha256 + ".")
        ):
            return None
        raw_categories = entry.get("categories")
        if not isinstance(raw_categories, (list, tuple, set)):
            raw_categories = [category]
        categories = self._sanitize_categories(raw_categories, primary=category)
        if categories is None:
            categories = [category]
        tags = self._sanitize_tags(entry.get("tags", []))
        meaning = self._sanitize_meaning(entry.get("meaning", ""))
        tone = self._sanitize_tone(entry.get("tone", ""))
        if tags is None or meaning is None or tone is None:
            return None
        created_at = _safe_nonnegative_int(entry.get("created_at", 0))
        updated_at = _safe_nonnegative_int(
            entry.get("updated_at", created_at), default=created_at
        )
        size = _safe_nonnegative_int(entry.get("size", 0))
        width = _safe_nonnegative_int(entry.get("width", 0))
        height = _safe_nonnegative_int(entry.get("height", 0))
        mime = str(entry.get("mime", "") or "")
        if mime not in {
            "image/jpeg",
            "image/png",
            "image/gif",
            "image/webp",
            "image/bmp",
            "image/avif",
        }:
            mime = ""
        return {
            "sha256": sha256,
            "filename": filename,
            "category": category,
            "categories": categories,
            "size": size,
            "width": width,
            "height": height,
            "mime": mime,
            "created_at": created_at,
            "updated_at": updated_at,
            "source": self._sanitize_source(entry.get("source", "")),
            "tags": tags,
            "meaning": meaning,
            "tone": tone,
        }

    # ------------------------------------------------------------------
    # Index I/O
    # ------------------------------------------------------------------
    def _load_index_unlocked(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        if self.index_path.is_symlink():
            self._warn("collected sticker index is a symlink; ignored")
            return []
        raw = self._read_bytes_nofollow(self.index_path, MAX_INDEX_BYTES)
        if raw is None:
            self._warn("collected sticker index is unreadable or too large")
            return []
        try:
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            self._warn("collected sticker index is malformed; treated as empty")
            return []
        if isinstance(payload, list):
            raw_entries = payload
        elif isinstance(payload, dict):
            raw_entries = payload.get("stickers", [])
        else:
            return []
        if not isinstance(raw_entries, list):
            return []
        loaded: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        for item in raw_entries:
            if not isinstance(item, dict):
                continue
            sha256 = str(item.get("sha256", "") or "").lower()
            filename = str(item.get("filename", "") or "")
            category = self.category_normalizer(item.get("category"))
            if (
                not re.fullmatch(r"[a-f0-9]{64}", sha256)
                or category is None
                or category != str(item.get("category", "") or "").strip()
                or not COLLECTED_FILENAME_RE.fullmatch(filename)
                or not filename.startswith(sha256 + ".")
                or sha256 in seen_sha
            ):
                continue
            path = self.file_path(category, filename)
            if path is None:
                continue
            try:
                actual_size = path.stat().st_size
            except OSError:
                continue
            if actual_size <= 0 or actual_size > self.max_file_bytes:
                continue
            prefix = self._read_prefix_nofollow(path, 32)
            detected = detect_image_magic(prefix or b"")
            if detected is None or detected[1] != path.suffix.lower():
                continue
            recorded_size = _safe_nonnegative_int(item.get("size", 0))
            if recorded_size and recorded_size != actual_size:
                continue
            raw_categories = item.get("categories")
            if not isinstance(raw_categories, (list, tuple, set)):
                raw_categories = [category]
            categories = self._sanitize_categories(
                raw_categories, primary=category
            ) or [category]
            tags = self._sanitize_tags(item.get("tags", []))
            meaning = self._sanitize_meaning(item.get("meaning", ""))
            tone = self._sanitize_tone(item.get("tone", ""))
            created_at = _safe_nonnegative_int(item.get("created_at", 0))
            updated_at = _safe_nonnegative_int(
                item.get("updated_at", created_at), default=created_at
            )
            mime = str(item.get("mime", "") or "")
            if mime not in {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
                "image/bmp",
                "image/avif",
            }:
                mime = ""
            width = _safe_nonnegative_int(item.get("width", 0))
            height = _safe_nonnegative_int(item.get("height", 0))
            if width or height:
                if (
                    width < self.min_dimension
                    or height < self.min_dimension
                    or width > self.max_dimension
                    or height > self.max_dimension
                    or width * height > self.max_pixels
                ):
                    continue
            seen_sha.add(sha256)
            loaded.append(
                {
                    "sha256": sha256,
                    "filename": filename,
                    "category": category,
                    "categories": categories,
                    "size": actual_size,
                    "width": width,
                    "height": height,
                    "mime": mime,
                    "created_at": created_at,
                    "updated_at": updated_at,
                    "source": self._sanitize_source(item.get("source", "")),
                    "tags": tags if tags is not None else [],
                    "meaning": meaning if meaning is not None else "",
                    "tone": tone if tone is not None else "",
                    "path": path,
                }
            )
        return loaded

    def load_index(self) -> list[dict[str, Any]]:
        """Read and validate the index under a shared OS lock."""

        try:
            with self.file_lock(exclusive=False):
                return self._load_index_unlocked()
        except Exception:
            self._warn("collected sticker root/index is unavailable")
            return []

    def _save_index_unlocked(self, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        serializable: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            normalized = self._serialize_entry(entry)
            if normalized is None or normalized["sha256"] in seen_sha:
                continue
            seen_sha.add(normalized["sha256"])
            serializable.append(normalized)
        payload = {
            # ``version`` remains 1 for readers of the legacy enhancer index.
            # Extended fields are advertised separately by schema_version.
            "version": 1,
            "schema_version": SCHEMA_VERSION,
            "saved_at": int(self.clock()),
            "stickers": serializable,
        }
        self._ensure_root_mode()
        if self.index_path.is_symlink():
            raise StickerStoreError("index path is a symlink")
        self.atomic_write_bytes(
            self.index_path,
            json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8"),
            mode=COLLECTED_INDEX_MODE,
            log_warning=self.log_warning,
        )
        return serializable

    def save_index(self, entries: list[dict[str, Any]]) -> None:
        with self.file_lock(exclusive=True):
            self._save_index_unlocked(entries)

    def _valid_entries_unlocked(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        valid: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sha256 = str(entry.get("sha256", "") or "")
            category = self.category_normalizer(entry.get("category"))
            filename = str(entry.get("filename", "") or "")
            if (
                category is None
                or sha256 in seen
                or not COLLECTED_FILENAME_RE.fullmatch(filename)
                or not filename.startswith(sha256 + ".")
            ):
                continue
            path = self.file_path(category, filename)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size <= 0 or size > self.max_file_bytes:
                continue
            prefix = self._read_prefix_nofollow(path, 32)
            detected = detect_image_magic(prefix or b"")
            if detected is None or detected[1] != path.suffix.lower():
                continue
            width = _safe_nonnegative_int(entry.get("width", 0))
            height = _safe_nonnegative_int(entry.get("height", 0))
            if width or height:
                if (
                    width < self.min_dimension
                    or height < self.min_dimension
                    or width > self.max_dimension
                    or height > self.max_dimension
                    or width * height > self.max_pixels
                ):
                    continue
            normalized = dict(entry)
            categories = self._sanitize_categories(
                entry.get("categories", [category]), primary=category
            ) or [category]
            normalized.update(
                {
                    "sha256": sha256,
                    "filename": filename,
                    "category": category,
                    "categories": categories,
                    "size": size,
                    "width": width,
                    "height": height,
                    "path": path,
                }
            )
            seen.add(sha256)
            valid.append(normalized)
        return valid

    def valid_entries(
        self, entries: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Re-check entries and their files under a shared store lock."""

        try:
            with self.file_lock(exclusive=False):
                return self._valid_entries_unlocked(entries)
        except Exception:
            self._warn("collected sticker entries are unavailable")
            return []

    def migrate_permissions(self, data_root: Path | str) -> dict[str, int]:
        """Best-effort safe permission repair inside this store only.

        Symlinks and non-regular files are skipped, never followed.  Index and
        lock files are 0600; sticker images and other regular files are 0644;
        directories are 0755.  The method returns a small diagnostic summary.
        """

        summary = {"dirs": 0, "files": 0, "skipped": 0, "errors": 0}
        try:
            base = Path(data_root).resolve()
            root = self._relative_resolved(self.root)
            if root != base and base not in root.parents:
                summary["errors"] += 1
                return summary
            with self.file_lock(exclusive=True):
                for dirpath, dirnames, filenames in os.walk(
                    root,
                    topdown=True,
                    followlinks=False,
                    onerror=lambda _error: None,
                ):
                    directory = Path(dirpath)
                    kept: list[str] = []
                    for dirname in dirnames:
                        child = directory / dirname
                        if child.is_symlink():
                            summary["skipped"] += 1
                            continue
                        kept.append(dirname)
                    dirnames[:] = kept
                    try:
                        directory.chmod(COLLECTED_DIR_MODE)
                        summary["dirs"] += 1
                    except OSError:
                        summary["errors"] += 1
                    for filename in filenames:
                        path = directory / filename
                        try:
                            if path.is_symlink() or not stat.S_ISREG(
                                path.lstat().st_mode
                            ):
                                summary["skipped"] += 1
                                continue
                            mode = (
                                COLLECTED_INDEX_MODE
                                if filename
                                in {
                                    COLLECTED_INDEX_FILENAME,
                                    COLLECTED_LOCK_FILENAME,
                                }
                                else COLLECTED_FILE_MODE
                            )
                            path.chmod(mode)
                            summary["files"] += 1
                        except OSError:
                            summary["errors"] += 1
                return summary
        except Exception:
            self._warn("collected sticker permission repair failed safely")
            summary["errors"] += 1
            return summary

    # ------------------------------------------------------------------
    # Collection / dedupe
    # ------------------------------------------------------------------
    def add_sticker(
        self,
        data: bytes,
        *,
        category: str = DEFAULT_COLLECTED_CATEGORY,
        categories: list[str] | tuple[str, ...] | None = None,
        tags: list[str] | tuple[str, ...] | None = None,
        meaning: str = "",
        tone: str = "",
        source: str = "",
        merge_duplicate: bool = False,
    ) -> tuple[str, dict[str, Any] | None]:
        """Validate and persist one image.

        Returned status is one of ``saved``, ``duplicate``, ``limit``,
        ``invalid``, ``invalid_image``, ``invalid_dimensions``, ``too_large`` or
        ``storage``.  Existing entries are never overwritten when the digest is
        already present.
        """

        primary = self.category_normalizer(category)
        if primary is None:
            return "invalid", None
        if isinstance(categories, (str, bytes)):
            return "invalid", None
        try:
            raw_categories = list(categories) if categories is not None else [primary]
        except TypeError:
            return "invalid", None
        normalized_categories = self._sanitize_categories(
            raw_categories, primary=primary
        )
        if normalized_categories is None:
            return "invalid", None
        normalized_tags = self._sanitize_tags(tags or [])
        normalized_meaning = self._sanitize_meaning(meaning)
        normalized_tone = self._sanitize_tone(tone)
        if (
            normalized_tags is None
            or normalized_meaning is None
            or normalized_tone is None
        ):
            return "invalid", None
        try:
            info = inspect_image_bytes(
                data,
                max_bytes=self.max_file_bytes,
                max_dimension=self.max_dimension,
                max_pixels=self.max_pixels,
                min_dimension=self.min_dimension,
            )
        except ImageValidationError as exc:
            if exc.code == "too_large":
                return "too_large", None
            if exc.code == "invalid_dimensions":
                return "invalid_dimensions", None
            return "invalid_image", None
        digest = hashlib.sha256(info.data).hexdigest()
        filename = digest + info.extension
        now = int(self.clock())
        try:
            with self.file_lock(exclusive=True):
                entries = self._load_index_unlocked()
                duplicate = next(
                    (entry for entry in entries if entry["sha256"] == digest),
                    None,
                )
                if duplicate is not None:
                    changed = False
                    if merge_duplicate:
                        merged_categories = list(duplicate.get("categories", []))
                        for value in normalized_categories:
                            if (
                                value not in merged_categories
                                and len(merged_categories) < self.max_categories
                            ):
                                merged_categories.append(value)
                        merged_tags = list(duplicate.get("tags", []))
                        for value in normalized_tags:
                            if (
                                value not in merged_tags
                                and len(merged_tags) < self.max_tags
                            ):
                                merged_tags.append(value)
                        merged_meaning = duplicate.get("meaning", "") or normalized_meaning
                        merged_tone = duplicate.get("tone", "") or normalized_tone
                        if (
                            merged_categories != duplicate.get("categories", [])
                            or merged_tags != duplicate.get("tags", [])
                            or merged_meaning != duplicate.get("meaning", "")
                            or merged_tone != duplicate.get("tone", "")
                        ):
                            duplicate = dict(duplicate)
                            duplicate.update(
                                {
                                    "categories": merged_categories,
                                    "tags": merged_tags,
                                    "meaning": merged_meaning,
                                    "tone": merged_tone,
                                    "updated_at": now,
                                }
                            )
                            replaced = [
                                duplicate if entry["sha256"] == digest else entry
                                for entry in entries
                            ]
                            try:
                                self._save_index_unlocked(replaced)
                            except Exception:
                                return "storage", None
                            changed = True
                    if changed:
                        duplicate["path"] = self.file_path(
                            duplicate["category"], duplicate["filename"]
                        )
                    return "duplicate", duplicate
                if len(entries) >= self.max_count:
                    return "limit", None
                category_dir = self.category_dir(primary, create=True)
                if category_dir is None:
                    return "storage", None
                final_path = category_dir / filename
                if final_path.is_symlink():
                    return "storage", None
                created_file = False
                if final_path.exists():
                    existing = self._safe_regular_file(final_path)
                    if existing is None:
                        return "storage", None
                    existing_data = self._read_bytes_nofollow(
                        existing, self.max_file_bytes
                    )
                    if (
                        existing_data is None
                        or hashlib.sha256(existing_data).hexdigest() != digest
                    ):
                        return "storage", None
                else:
                    try:
                        self.atomic_write_bytes(
                            final_path,
                            info.data,
                            mode=COLLECTED_FILE_MODE,
                            log_warning=self.log_warning,
                        )
                    except (OSError, StickerStoreError):
                        return "storage", None
                    created_file = True
                entry = {
                    "sha256": digest,
                    "filename": filename,
                    "category": primary,
                    "categories": normalized_categories,
                    "size": info.size,
                    "width": info.width,
                    "height": info.height,
                    "mime": info.mime,
                    "created_at": now,
                    "updated_at": now,
                    "source": self._sanitize_source(source),
                    "tags": normalized_tags,
                    "meaning": normalized_meaning,
                    "tone": normalized_tone,
                    "path": final_path,
                }
                try:
                    self._save_index_unlocked(entries + [entry])
                except Exception:
                    if created_file:
                        try:
                            final_path.unlink(missing_ok=True)
                        except OSError:
                            pass
                    return "storage", None
                return "saved", entry
        except Exception:
            self._warn("collected sticker add failed safely")
            return "storage", None

    save_sticker = add_sticker

    def get_entry(self, sha256: str) -> dict[str, Any] | None:
        digest = str(sha256 or "").lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return None
        for entry in self.load_index():
            if entry.get("sha256") == digest:
                return entry
        return None

    def has_sha(self, sha256: str) -> bool:
        return self.get_entry(sha256) is not None

    # ------------------------------------------------------------------
    # Metadata updates / reclassification
    # ------------------------------------------------------------------
    def _current_entries_for_update(
        self,
        supplied: list[dict[str, Any]],
        sha256: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        disk_entries = self._load_index_unlocked()
        found = next(
            (entry for entry in disk_entries if entry.get("sha256") == sha256),
            None,
        )
        if found is not None:
            return disk_entries, found
        # Compatibility with callers that use the store as a pure metadata
        # helper before the first save.  Only the supplied entries are used;
        # real plugin writes always follow a disk read under the same lock.
        supplied_entries = [
            dict(item) for item in supplied if isinstance(item, dict)
        ]
        found = next(
            (entry for entry in supplied_entries if entry.get("sha256") == sha256),
            None,
        )
        return supplied_entries, found

    def update_metadata(
        self,
        entries: list[dict[str, Any]],
        sha256: str,
        *,
        category: str | None = None,
        categories: Any = None,
        tags: Any = None,
        meaning: Any = None,
        tone: Any = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Atomically update category/tag/meaning/tone for one digest."""

        digest = str(sha256 or "").lower()
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return "invalid", None
        try:
            with self.file_lock(exclusive=True):
                current_entries, entry = self._current_entries_for_update(
                    entries, digest
                )
                if entry is None:
                    return "not_found", None
                old_category = self.category_normalizer(entry.get("category"))
                filename = str(entry.get("filename", "") or "")
                if old_category is None or not COLLECTED_FILENAME_RE.fullmatch(filename):
                    return "invalid", None
                old_path = self.file_path(old_category, filename)
                if old_path is None:
                    return "invalid", None
                existing_categories = self._sanitize_categories(
                    entry.get("categories", [old_category]), primary=old_category
                ) or [old_category]
                if categories is not None:
                    new_categories = self._sanitize_categories(
                        categories, primary=category
                    )
                    if new_categories is None:
                        return "invalid", None
                elif category is not None:
                    normalized = self.category_normalizer(category)
                    if normalized is None:
                        return "invalid", None
                    new_categories = [normalized]
                else:
                    new_categories = list(existing_categories)
                new_category = new_categories[0]
                new_tags = (
                    self._sanitize_tags(entry.get("tags", []))
                    if tags is None
                    else self._sanitize_tags(tags)
                )
                new_meaning = (
                    self._sanitize_meaning(entry.get("meaning", ""))
                    if meaning is None
                    else self._sanitize_meaning(meaning)
                )
                new_tone = (
                    self._sanitize_tone(entry.get("tone", ""))
                    if tone is None
                    else self._sanitize_tone(tone)
                )
                if new_tags is None or new_meaning is None or new_tone is None:
                    return "invalid", None
                old_tags = entry.get("tags", []) or []
                old_meaning = entry.get("meaning", "") or ""
                old_tone = entry.get("tone", "") or ""
                category_changed = old_category != new_category
                metadata_changed = (
                    new_categories != existing_categories
                    or new_tags != old_tags
                    or new_meaning != old_meaning
                    or new_tone != old_tone
                )
                if not category_changed and not metadata_changed:
                    result = dict(entry)
                    result["path"] = old_path
                    return "same", result
                target_path = old_path
                moved = False
                if category_changed:
                    new_dir = self.category_dir(new_category, create=True)
                    if new_dir is None:
                        return "storage", None
                    candidate = new_dir / filename
                    if candidate.is_symlink() or candidate.exists():
                        return "conflict", None
                    try:
                        os.replace(old_path, candidate)
                        os.chmod(candidate, COLLECTED_FILE_MODE)
                    except OSError:
                        return "storage", None
                    moved = True
                    target_path = candidate
                new_entry = dict(entry)
                new_entry.update(
                    {
                        "category": new_category,
                        "categories": new_categories,
                        "tags": new_tags,
                        "meaning": new_meaning,
                        "tone": new_tone,
                        "updated_at": int(self.clock()),
                        "path": target_path,
                    }
                )
                updated_entries = [
                    new_entry if item.get("sha256") == digest else item
                    for item in current_entries
                ]
                try:
                    self._save_index_unlocked(updated_entries)
                except Exception:
                    if moved:
                        try:
                            os.replace(target_path, old_path)
                            os.chmod(old_path, COLLECTED_FILE_MODE)
                        except OSError as exc:
                            self._warn(f"category move rollback failed: {exc}")
                    return "storage", None
                return "updated", new_entry
        except StickerStoreError:
            return "storage", None

    def update_categories(
        self,
        entries: list[dict[str, Any]],
        sha256: str,
        *,
        categories: Any = None,
        add: Any = None,
        remove: Any = None,
    ) -> tuple[str, dict[str, Any] | None]:
        digest = str(sha256 or "").lower()
        try:
            with self.file_lock(exclusive=True):
                current_entries, entry = self._current_entries_for_update(
                    entries, digest
                )
        except StickerStoreError:
            return "storage", None
        if entry is None:
            return "not_found", None
        old_category = self.category_normalizer(entry.get("category"))
        if old_category is None:
            return "invalid", None
        current = self._sanitize_categories(
            entry.get("categories", [old_category]), primary=old_category
        ) or [old_category]
        if categories is not None:
            new_categories = self._sanitize_categories(categories)
        else:
            new_categories = list(current)
            if add is not None:
                add_values = add if isinstance(add, (list, tuple, set)) else [add]
                sanitized_add = self._sanitize_categories(add_values)
                if sanitized_add is None:
                    return "invalid", None
                for value in sanitized_add:
                    if value not in new_categories:
                        new_categories.append(value)
            if remove is not None:
                remove_values = (
                    remove if isinstance(remove, (list, tuple, set)) else [remove]
                )
                sanitized_remove = self._sanitize_categories(remove_values)
                if sanitized_remove is None:
                    return "invalid", None
                new_categories = [
                    value for value in new_categories if value not in sanitized_remove
                ]
        if not new_categories or len(new_categories) > self.max_categories:
            return "invalid", None
        primary = old_category if old_category in new_categories else new_categories[0]
        ordered = [primary] + [
            value for value in new_categories if value != primary
        ]
        return self.update_metadata(
            current_entries,
            digest,
            category=primary,
            categories=ordered,
        )

    def add_categories(
        self, entries: list[dict[str, Any]], sha256: str, add: Any
    ) -> tuple[str, dict[str, Any] | None]:
        return self.update_categories(entries, sha256, add=add)

    def remove_categories(
        self, entries: list[dict[str, Any]], sha256: str, remove: Any
    ) -> tuple[str, dict[str, Any] | None]:
        return self.update_categories(entries, sha256, remove=remove)

    def reclassify(
        self, entries: list[dict[str, Any]], sha256: str, new_category: str
    ) -> tuple[str, dict[str, Any] | None]:
        old_entry = self.find_by_sha(entries, sha256)
        old_category = old_entry.get("category") if old_entry else None
        status, entry = self.update_metadata(
            entries, sha256, category=new_category
        )
        if status == "updated" and old_category != new_category:
            return "moved", entry
        return status, entry

    move_sticker = reclassify

    # ------------------------------------------------------------------
    # Read-only helpers and compatibility
    # ------------------------------------------------------------------
    @staticmethod
    def categories(entries: list[dict[str, Any]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for entry in entries:
            values = entry.get("categories")
            if not isinstance(values, (list, tuple, set)):
                values = [entry.get("category", "")]
            for raw in values:
                value = str(raw or "")
                if value:
                    result[value] = result.get(value, 0) + 1
        return result

    def category_counts(self) -> dict[str, int]:
        return self.categories(self.load_index())

    @staticmethod
    def unique_count(entries: list[dict[str, Any]]) -> int:
        return len(
            {
                str(entry.get("sha256", ""))
                for entry in entries
                if entry.get("sha256")
            }
        )

    @staticmethod
    def list_entries(
        entries: list[dict[str, Any]], category: str | None = None
    ) -> list[dict[str, Any]]:
        if category is None:
            return list(entries)
        return [
            entry
            for entry in entries
            if category
            in (
                entry.get("categories")
                if isinstance(entry.get("categories"), (list, tuple, set))
                else [entry.get("category")]
            )
        ]

    @staticmethod
    def find_by_sha(
        entries: list[dict[str, Any]], sha256: str
    ) -> dict[str, Any] | None:
        digest = str(sha256 or "").lower()
        for entry in entries:
            if str(entry.get("sha256", "")) == digest:
                return entry
        return None

    def write_sticker(self, category: str, filename: str, data: bytes) -> Path:
        """Compatibility helper used by imports/tests; no index update."""

        if not COLLECTED_FILENAME_RE.fullmatch(filename):
            raise StickerStoreError("invalid sticker filename")
        with self.file_lock(exclusive=True):
            category_dir = self.category_dir(category, create=True)
            if category_dir is None:
                raise StickerStoreError("invalid sticker category")
            target = category_dir / filename
            if target.is_symlink():
                raise StickerStoreError("invalid sticker target")
            self.atomic_write_bytes(target, data, mode=COLLECTED_FILE_MODE)
        return target


def _safe_nonnegative_int(value: Any, *, default: int = 0) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, number)
