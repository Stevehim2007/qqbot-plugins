"""Single-writer sticker service implementing the ABI-v1 business contract.

Architecture:
* ``stickers_db.json`` is the authoritative scoped collection/category ledger.
* ``collected_stickers/index_generations/*.json`` are immutable search indexes.
* ``active_index.json`` is the only atomically switched active pointer.
* assets live once per content hash; collection rows and ACL scope are separate.

Every mutation runs through ``_write_guard()`` (asyncio lock + an OS service
lock) while the service holds a lifetime exclusive ``.writer.lock``.  A second
service instance for the same store fails immediately instead of becoming a
second writer.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import os
import random
import re
import stat
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

try:
    from .contracts import (
        ABI_VERSION,
        CAPABILITY_STICKERS,
        ContractError,
        ConfirmationManager,
        OwnerEpochMismatch,
        RequestContext,
        canonical_scope,
        error_result,
        reply_plan,
        stable_hash,
        success_result,
        tool_result,
    )
    from .media import ImageValidationError, decode_base64_payload, inspect_image_bytes
    from .sticker_store import (
        CollectedStickerStore,
        StickerStoreError,
        normalize_category_name,
    )
except ImportError:  # direct test harness
    from contracts import (
        ABI_VERSION,
        CAPABILITY_STICKERS,
        ContractError,
        ConfirmationManager,
        OwnerEpochMismatch,
        RequestContext,
        canonical_scope,
        error_result,
        reply_plan,
        stable_hash,
        success_result,
        tool_result,
    )
    from media import ImageValidationError, decode_base64_payload, inspect_image_bytes
    from sticker_store import (
        CollectedStickerStore,
        StickerStoreError,
        normalize_category_name,
    )

DB_SCHEMA_VERSION = 3
INDEX_SCHEMA_VERSION = 3
DB_FILENAME = "stickers_db.json"
ACTIVE_INDEX_FILENAME = "active_index.json"
INDEX_GENERATIONS_DIR = "index_generations"
WRITER_LOCK_FILENAME = ".writer.lock"
SERVICE_LOCK_FILENAME = ".service.lock"
ORPHAN_FILENAME = "orphan_assets.json"
ASSET_CATEGORY = "__assets__"
DEFAULT_CATEGORY = "未分类"
LEGACY_SCOPE = "conversation:__legacy_compat__"
MAX_DB_BYTES = 64 * 1024 * 1024
MAX_INDEX_BYTES = 64 * 1024 * 1024
MAX_CHANGE_LOG = 4096
MAX_OPERATIONS = 4096
MAX_DELIVERIES = 4096
MAX_PENDING_PLANS = 256
MAX_SEARCH_LIMIT = 100


class StickerServiceError(RuntimeError):
    error_code = "storage_error"


class SecondWriterActive(StickerServiceError):
    error_code = "permission_denied"


class ServiceNotReady(StickerServiceError):
    error_code = "not_ready"


def _safe_int(value: Any, default: int = 0, minimum: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, number)


def _safe_text(value: Any, limit: int = 200) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    return text[: max(0, int(limit))]


def _scope_key(context: RequestContext) -> str:
    return context.owner_scope


def _copy_dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def _normalise_data(value: Any) -> bytes | None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    return decode_base64_payload(value)


def _exclude_confirmation_fields(args: Mapping[str, Any]) -> dict[str, Any]:
    excluded = {
        "confirmation_ref",
        "confirmation_token",
        "confirm_token",
        "call_id",
        "plan_id",
        "plan_revision",
    }
    return {str(key): value for key, value in args.items() if key not in excluded}


def _asset_ref_safe(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/") or ".." in text.split("/"):
        raise ContractError("unsafe asset_ref")
    if len(text) > 512 or any(ord(char) < 32 for char in text):
        raise ContractError("unsafe asset_ref")
    return text


@contextlib.contextmanager
def _flock_path(path: Path, *, exclusive: bool, blocking: bool = True) -> Iterator[int]:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise StickerServiceError(f"cannot open service lock: {exc}") from exc
    try:
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        try:
            import fcntl

            operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            if not blocking:
                operation |= fcntl.LOCK_NB
            fcntl.flock(fd, operation)
        except BlockingIOError as exc:
            raise SecondWriterActive("another sticker writer is active") from exc
        except ImportError:
            pass
        yield fd
    finally:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        except (ImportError, OSError):
            pass
        try:
            os.close(fd)
        except OSError:
            pass


class StickerService:
    """Authoritative scoped sticker business service and index writer."""

    def __init__(
        self,
        root: Path | str,
        *,
        store: CollectedStickerStore | None = None,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
        category_normalizer: Callable[[Any], str | None] | None = None,
        max_file_bytes: int = 20 * 1024 * 1024,
        max_count: int = 200,
        max_dimension: int = 4096,
        max_pixels: int = 16_777_216,
        max_categories: int = 16,
        max_category_chars: int = 32,
        confirmation_ttl_seconds: int = 300,
        send_cooldown_seconds: int = 0,
        owner_epoch: int = 1,
        acquire_writer_lock: bool = True,
    ) -> None:
        raw_root = Path(root)
        if raw_root.is_symlink():
            raise StickerServiceError("sticker root must not be a symlink")
        raw_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            raw_root.chmod(0o755)
        except OSError:
            pass
        self.root = raw_root.resolve()
        self.clock = clock
        self.rng = rng or random.Random()
        self.owner_epoch = int(owner_epoch)
        self.send_cooldown_seconds = max(0, int(send_cooldown_seconds))
        self.confirmation_ttl_seconds = max(1, min(600, int(confirmation_ttl_seconds)))
        self.max_categories = max(1, min(32, int(max_categories)))
        self.max_category_chars = max(1, min(32, int(max_category_chars)))
        self.max_file_bytes = max(1, int(max_file_bytes))
        self.max_dimension = max(1, int(max_dimension))
        self.max_pixels = max(1, int(max_pixels))
        self._normalizer = category_normalizer or (
            lambda value: normalize_category_name(
                value, max_chars=self.max_category_chars
            )
        )
        self.store = store or CollectedStickerStore(
            self.root,
            max_file_bytes=self.max_file_bytes,
            category_normalizer=self._normalizer,
            default_category=DEFAULT_CATEGORY,
            max_count=max_count,
            max_dimension=self.max_dimension,
            max_pixels=self.max_pixels,
            max_categories=max_categories,
            clock=self.clock,
        )
        if self.store.root.resolve() != self.root:
            raise StickerServiceError("store root does not match service root")
        self.db_path = self.root / DB_FILENAME
        self.active_index_path = self.root / ACTIVE_INDEX_FILENAME
        self.generations_dir = self.root / INDEX_GENERATIONS_DIR
        self.writer_lock_path = self.root / WRITER_LOCK_FILENAME
        self.service_lock_path = self.root / SERVICE_LOCK_FILENAME
        self.orphan_path = self.root / ORPHAN_FILENAME
        self.generations_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
        try:
            self.generations_dir.chmod(0o755)
        except OSError:
            pass
        self.confirmations = ConfirmationManager(
            clock=self.clock,
            default_ttl_seconds=self.confirmation_ttl_seconds,
        )
        self._async_lock = asyncio.Lock()
        self._plan_lock = threading.RLock()
        self._pending_collect: dict[str, dict[str, Any]] = {}
        self._writer_fd: int | None = None
        self._writer_lock_held = False
        self._closed = False
        self._ready = False
        self.rebuild_snapshot_hook: Callable[[dict[str, Any]], Any] | None = None
        self._index_writes = 0
        self._rebuild_count = 0
        self._collect_count = 0
        if acquire_writer_lock:
            self._acquire_writer_lock()

    # ------------------------------------------------------------------
    # Lifecycle / readiness / writer ownership
    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return bool(self._ready and not self._closed and self._writer_lock_held)

    def _acquire_writer_lock(self) -> None:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.writer_lock_path, flags, 0o600)
        except OSError as exc:
            raise StickerServiceError(f"cannot open writer lock: {exc}") from exc
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SecondWriterActive("another sticker writer is active") from exc
            except ImportError:
                pass
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()} started={time.time():.3f}\n".encode())
            os.fsync(fd)
            self._writer_fd = fd
            self._writer_lock_held = True
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ready = False
        fd, self._writer_fd = self._writer_fd, None
        if fd is not None:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except (ImportError, OSError):
                pass
            try:
                os.close(fd)
            except OSError:
                pass
        self._writer_lock_held = False

    def terminate(self) -> None:
        self.close()

    def __del__(self):  # pragma: no cover - defensive resource cleanup
        try:
            self.close()
        except Exception:
            pass

    async def initialize(self, *, owner_epoch: int | None = None) -> None:
        if self._closed:
            raise SecondWriterActive("sticker service is closed")
        async with self._write_guard():
            state = self._load_state_locked(create=True)
            changed = False
            if owner_epoch is not None:
                new_epoch = int(owner_epoch)
                if new_epoch != self.owner_epoch:
                    self.owner_epoch = new_epoch
                    changed = True
            if self._bootstrap_legacy_store_locked(state):
                changed = True
            self._save_state_locked(state)
            if not self._active_index_current_locked(state):
                if not self._publish_index_locked(state):
                    raise StickerServiceError("cannot publish initial sticker index")
            elif changed:
                # Keep the index exactly at the new high-water.
                self._publish_index_locked(state)
            self._ready = True

    def set_owner_epoch(self, owner_epoch: int) -> None:
        self.owner_epoch = int(owner_epoch)

    async def terminate_async(self) -> None:
        self.close()

    @contextlib.contextmanager
    def _service_file_lock(self, *, exclusive: bool) -> Iterator[None]:
        with _flock_path(self.service_lock_path, exclusive=exclusive):
            yield

    @contextlib.asynccontextmanager
    async def _write_guard(self):
        if self._closed:
            raise SecondWriterActive("sticker service is closed")
        async with self._async_lock:
            with self._service_file_lock(exclusive=True):
                yield

    @contextlib.contextmanager
    def _read_guard(self) -> Iterator[None]:
        with self._service_file_lock(exclusive=False):
            yield

    # ------------------------------------------------------------------
    # Plain-dict request/result plumbing
    # ------------------------------------------------------------------
    @staticmethod
    def _raw_error(
        *,
        error_code: str,
        call_id: str = "invalid-context",
        result_type: str = "error",
        data: dict[str, Any] | None = None,
        status: str = "error",
    ) -> dict[str, Any]:
        return {
            "status": status,
            "call_id": call_id,
            "result_type": result_type,
            "data": dict(data or {}),
            "error_code": error_code,
            "retryable": False,
            "side_effect_state": "none",
            "provider_operation_id": "",
        }

    def _prepare(
        self, request_context: Any, args: Mapping[str, Any] | None
    ) -> tuple[RequestContext | None, dict[str, Any] | None, dict[str, Any]]:
        args_map = dict(args or {})
        try:
            context = RequestContext.from_mapping(request_context)
        except ContractError as exc:
            return None, self._raw_error(error_code=exc.error_code), args_map
        except Exception:
            return None, self._raw_error(error_code="invalid_args"), args_map
        if not self.ready:
            return (
                context,
                error_result(context, error_code="not_ready", args=args_map),
                args_map,
            )
        if int(context.owner_epoch) != int(self.owner_epoch):
            return (
                context,
                error_result(
                    context,
                    error_code="permission_denied",
                    data={"reason": "stale_owner_epoch"},
                    args=args_map,
                ),
                args_map,
            )
        try:
            now = float(self.clock())
            if now > float(context.deadline):
                return (
                    context,
                    error_result(
                        context, error_code="deadline_exceeded", args=args_map
                    ),
                    args_map,
                )
        except (TypeError, ValueError, OverflowError):
            return (
                context,
                error_result(context, error_code="invalid_args", args=args_map),
                args_map,
            )
        return context, None, args_map

    def _operation_key(
        self, context: RequestContext, operation: str, args: Mapping[str, Any]
    ) -> str:
        business_key = str(
            args.get("idempotency_key")
            or args.get("source_message_id")
            or context.source_message_id
            or context.turn_id
        ).strip()
        return "|".join(
            (
                context.instance_id,
                context.bot_id,
                context.actor_id,
                context.owner_scope,
                operation,
                business_key or "default",
            )
        )

    def _operation_payload_hash(
        self, operation: str, args: Mapping[str, Any]
    ) -> str:
        return stable_hash(
            {"operation": operation, "args": _exclude_confirmation_fields(args)}
        )

    def _check_operation_replay_locked(
        self,
        state: dict[str, Any],
        context: RequestContext,
        operation: str,
        args: Mapping[str, Any],
    ) -> tuple[bool, dict[str, Any] | None]:
        key = self._operation_key(context, operation, args)
        record = state.get("operations", {}).get(key)
        if not isinstance(record, dict):
            return False, None
        payload_hash = self._operation_payload_hash(operation, args)
        if str(record.get("payload_hash", "")) != payload_hash:
            return True, error_result(
                context,
                error_code="revision_conflict",
                result_type="error",
                data={"reason": "idempotency_payload_changed"},
                operation=operation,
                args=args,
            )
        result = record.get("result")
        if isinstance(result, dict):
            return True, copy.deepcopy(result)
        return True, None

    def _record_operation_locked(
        self,
        state: dict[str, Any],
        context: RequestContext,
        operation: str,
        args: Mapping[str, Any],
        result: Mapping[str, Any],
    ) -> None:
        key = self._operation_key(context, operation, args)
        operations = state.setdefault("operations", {})
        operations[key] = {
            "operation": operation,
            "owner_scope": context.owner_scope,
            "actor_id": context.actor_id,
            "payload_hash": self._operation_payload_hash(operation, args),
            "result": copy.deepcopy(dict(result)),
            "stored_at": float(self.clock()),
        }
        if len(operations) > MAX_OPERATIONS:
            ordered = sorted(
                operations.items(),
                key=lambda pair: float(pair[1].get("stored_at", 0) or 0),
            )
            for old_key, _ in ordered[: len(operations) - MAX_OPERATIONS]:
                operations.pop(old_key, None)

    # ------------------------------------------------------------------
    # State and index persistence
    # ------------------------------------------------------------------
    def _initial_state(self) -> dict[str, Any]:
        now = float(self.clock())
        return {
            "schema_version": DB_SCHEMA_VERSION,
            "revision": 0,
            "high_water": 0,
            "index_generation": 0,
            "created_at": now,
            "updated_at": now,
            "assets": {},
            "collections": {},
            "categories": {},
            "operations": {},
            "deliveries": {},
            "change_log": [],
            "orphans": [],
            "last_rebuild_revision": None,
        }

    def _read_json_nofollow(self, path: Path, limit: int) -> Any:
        raw = self.store._read_bytes_nofollow(path, limit)
        if raw is None:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise StickerServiceError(f"malformed JSON: {path.name}") from exc

    def _load_state_locked(self, *, create: bool = False) -> dict[str, Any]:
        if not self.db_path.exists():
            if not create:
                raise StickerServiceError("sticker database is not initialized")
            return self._initial_state()
        if self.db_path.is_symlink():
            raise StickerServiceError("sticker database must not be a symlink")
        payload = self._read_json_nofollow(self.db_path, MAX_DB_BYTES)
        if not isinstance(payload, dict):
            raise StickerServiceError("sticker database is malformed")
        state = self._initial_state()
        state.update(payload)
        for key in (
            "assets",
            "collections",
            "categories",
            "operations",
            "deliveries",
        ):
            if not isinstance(state.get(key), dict):
                state[key] = {}
        for key in ("change_log", "orphans"):
            if not isinstance(state.get(key), list):
                state[key] = []
        state["revision"] = _safe_int(state.get("revision"), 0)
        state["high_water"] = _safe_int(
            state.get("high_water"), state["revision"]
        )
        state["index_generation"] = _safe_int(state.get("index_generation"), 0)
        state["schema_version"] = DB_SCHEMA_VERSION
        return state

    def _save_state_locked(self, state: dict[str, Any]) -> None:
        state["schema_version"] = DB_SCHEMA_VERSION
        state["updated_at"] = float(self.clock())
        state["revision"] = _safe_int(state.get("revision"), 0)
        state["high_water"] = max(
            _safe_int(state.get("high_water"), 0), state["revision"]
        )
        if self.db_path.is_symlink():
            raise StickerServiceError("sticker database must not be a symlink")
        self.store.atomic_write_bytes(
            self.db_path,
            json.dumps(
                state, ensure_ascii=False, indent=2, sort_keys=True
            ).encode("utf-8"),
            mode=0o600,
        )

    def _bump_revision(
        self, state: dict[str, Any], kind: str, **details: Any
    ) -> int:
        revision = _safe_int(state.get("revision"), 0) + 1
        state["revision"] = revision
        state["high_water"] = revision
        change = {
            "revision": revision,
            "kind": str(kind),
            "at": float(self.clock()),
            **details,
        }
        log = state.setdefault("change_log", [])
        log.append(change)
        if len(log) > MAX_CHANGE_LOG:
            del log[: len(log) - MAX_CHANGE_LOG]
        return revision

    def _category_names(
        self, state: dict[str, Any], category_ids: list[str]
    ) -> list[str]:
        names: list[str] = []
        for category_id in category_ids:
            category = state.get("categories", {}).get(category_id)
            if isinstance(category, dict) and not category.get("tombstone"):
                name = str(category.get("name", "") or "")
                if name and name not in names:
                    names.append(name)
        return names

    def _index_entry_locked(
        self, state: dict[str, Any], row: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        if row.get("tombstone"):
            return None
        category_ids = [
            str(value) for value in row.get("category_ids", []) if str(value)
        ]
        categories = self._category_names(state, category_ids)
        return {
            "sticker_id": str(row.get("sticker_id", "")),
            "content_hash": str(row.get("content_hash", "")),
            "asset_ref": _asset_ref_safe(row.get("asset_ref", "")),
            "mime": str(row.get("mime", "") or ""),
            "bytes": _safe_int(row.get("bytes"), 0),
            "width": _safe_int(row.get("width"), 0),
            "height": _safe_int(row.get("height"), 0),
            "category_ids": category_ids,
            "categories": categories,
            "owner_scope": str(row.get("owner_scope", "")),
            "source_message_id": str(row.get("source_message_id", "")),
            "created_by": str(row.get("created_by", "")),
            "created_at": float(row.get("created_at", 0) or 0),
            "updated_at": float(row.get("updated_at", 0) or 0),
            "revision": _safe_int(row.get("revision"), 0),
            "tags": list(row.get("tags", []) or []),
            "meaning": str(row.get("meaning", "") or ""),
            "tone": str(row.get("tone", "") or ""),
            "tombstone": False,
        }

    def _index_entries_locked(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for row in state.get("collections", {}).values():
            if not isinstance(row, dict):
                continue
            entry = self._index_entry_locked(state, row)
            if entry is not None:
                entries.append(entry)
        entries.sort(key=lambda item: (item.get("created_at", 0), item["sticker_id"]))
        return entries

    def _generation_path(self, generation: int) -> Path:
        return self.generations_dir / f"generation-{int(generation):08d}.json"

    def _write_generation_atomic(
        self,
        generation: int,
        *,
        entries: list[dict[str, Any]],
        high_water: int,
        rebuilt_from: int | None = None,
    ) -> str:
        path = self._generation_path(generation)
        if path.is_symlink():
            raise StickerServiceError("generation path must not be a symlink")
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "generation": int(generation),
            "high_water": int(high_water),
            "saved_at": float(self.clock()),
            "entries": entries,
        }
        if rebuilt_from is not None:
            payload["rebuilt_from"] = int(rebuilt_from)
        self.store.atomic_write_bytes(
            path,
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8"),
            mode=0o600,
        )
        self._index_writes += 1
        return path.relative_to(self.root).as_posix()

    def _switch_active_pointer_atomic(
        self, *, generation: int, relative_path: str, high_water: int
    ) -> None:
        payload = {
            "schema_version": INDEX_SCHEMA_VERSION,
            "generation": int(generation),
            "path": _asset_ref_safe(relative_path),
            "high_water": int(high_water),
            "switched_at": float(self.clock()),
        }
        self.store.atomic_write_bytes(
            self.active_index_path,
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, indent=2
            ).encode("utf-8"),
            mode=0o600,
        )

    def _read_pointer_locked(self) -> dict[str, Any] | None:
        if not self.active_index_path.exists() or self.active_index_path.is_symlink():
            return None
        try:
            raw = self.store._read_bytes_nofollow(
                self.active_index_path, 64 * 1024
            )
            if raw is None:
                return None
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if not isinstance(payload.get("generation"), int):
            return None
        path = str(payload.get("path", "") or "")
        try:
            _asset_ref_safe(path)
        except ContractError:
            return None
        return payload

    def _read_generation_entries(
        self, relative_path: str, expected_high_water: int
    ) -> list[dict[str, Any]] | None:
        try:
            safe_ref = _asset_ref_safe(relative_path)
        except ContractError:
            return None
        path = (self.root / safe_ref).resolve()
        try:
            if self.root not in path.parents or path.is_symlink():
                return None
        except OSError:
            return None
        try:
            raw = self.store._read_bytes_nofollow(path, MAX_INDEX_BYTES)
            if raw is None:
                return None
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                return None
            entries = payload.get("entries")
            if not isinstance(entries, list):
                return None
            if _safe_int(payload.get("high_water"), -1) != int(expected_high_water):
                return None
            return [item for item in entries if isinstance(item, dict)]
        except Exception:
            return None

    def _active_index_current_locked(self, state: dict[str, Any]) -> bool:
        pointer = self._read_pointer_locked()
        if pointer is None:
            return False
        if _safe_int(pointer.get("high_water"), -1) != _safe_int(
            state.get("revision"), -2
        ):
            return False
        return (
            self._read_generation_entries(
                str(pointer.get("path", "")),
                _safe_int(pointer.get("high_water"), -1),
            )
            is not None
        )

    def _publish_index_locked(
        self,
        state: dict[str, Any],
        *,
        entries: list[dict[str, Any]] | None = None,
        generation: int | None = None,
        high_water: int | None = None,
        rebuilt_from: int | None = None,
    ) -> bool:
        try:
            gen = (
                _safe_int(generation, 0)
                if generation is not None
                else _safe_int(state.get("index_generation"), 0) + 1
            )
            if gen <= 0:
                gen = 1
            if entries is None:
                entries = self._index_entries_locked(state)
            water = (
                _safe_int(high_water, 0)
                if high_water is not None
                else _safe_int(state.get("revision"), 0)
            )
            relative = self._write_generation_atomic(
                gen,
                entries=entries,
                high_water=water,
                rebuilt_from=rebuilt_from,
            )
            self._switch_active_pointer_atomic(
                generation=gen, relative_path=relative, high_water=water
            )
            return True
        except Exception:
            return False

    def _commit_state_locked(
        self, state: dict[str, Any], *, publish_index: bool = True
    ) -> bool:
        if publish_index:
            generation = _safe_int(state.get("index_generation"), 0) + 1
            state["index_generation"] = generation
            self._save_state_locked(state)
            return self._publish_index_locked(
                state, generation=generation, high_water=state["revision"]
            )
        self._save_state_locked(state)
        return True

    def _active_entries_locked(
        self, state: dict[str, Any]
    ) -> list[dict[str, Any]]:
        pointer = self._read_pointer_locked()
        if pointer is not None:
            entries = self._read_generation_entries(
                str(pointer.get("path", "")),
                _safe_int(pointer.get("high_water"), -1),
            )
            if entries is not None:
                return entries
        return self._index_entries_locked(state)

    # ------------------------------------------------------------------
    # Categories / collection rows
    # ------------------------------------------------------------------
    def _normalise_category_name(self, value: Any) -> str:
        normalised = self._normalizer(value)
        if normalised is None:
            raise ContractError("invalid category name")
        return normalised

    def _category_identity(self, state: dict[str, Any], value: Any, scope: str) -> str:
        text = str(value or "").strip()
        for category_id, category in state.get("categories", {}).items():
            if not isinstance(category, dict) or category.get("tombstone"):
                continue
            if str(category.get("owner_scope", "")) != scope:
                continue
            if text == category_id or text.casefold() == str(
                category.get("name", "")
            ).casefold():
                return str(category_id)
        return ""

    def _find_category_locked(
        self, state: dict[str, Any], scope: str, value: Any
    ) -> dict[str, Any] | None:
        category_id = self._category_identity(state, value, scope)
        if not category_id:
            return None
        category = state.get("categories", {}).get(category_id)
        if isinstance(category, dict) and not category.get("tombstone"):
            return category
        return None

    def _ensure_category_locked(
        self,
        state: dict[str, Any],
        *,
        scope: str,
        name: str,
        actor_id: str,
        enforce_limit: bool = True,
    ) -> dict[str, Any]:
        name = self._normalise_category_name(name)
        existing = self._find_category_locked(state, scope, name)
        if existing is not None:
            return existing
        active_count = sum(
            1
            for category in state.setdefault("categories", {}).values()
            if isinstance(category, dict)
            and not category.get("tombstone")
            and category.get("owner_scope") == scope
        )
        if enforce_limit and active_count >= self.max_categories:
            raise ContractError("category limit reached")
        category_id = "cat_" + uuid.uuid4().hex
        now = float(self.clock())
        revision = self._bump_revision(
            state,
            "category_create",
            category_id=category_id,
            owner_scope=scope,
        )
        category = {
            "category_id": category_id,
            "name": name,
            "owner_scope": scope,
            "created_by": actor_id,
            "created_at": now,
            "updated_at": now,
            "revision": revision,
            "tombstone": False,
            "tombstone_at": None,
            "tombstone_by": "",
        }
        state.setdefault("categories", {})[category_id] = category
        return category

    def _resolve_category_ids_for_args_locked(
        self,
        state: dict[str, Any],
        context: RequestContext,
        args: Mapping[str, Any],
        *,
        create: bool,
    ) -> tuple[list[str], list[str]]:
        scope = context.owner_scope
        raw_values: list[Any] = []
        categories_raw = args.get("categories")
        if isinstance(categories_raw, (list, tuple)):
            raw_values.extend(categories_raw)
        category_ids_raw = args.get("category_ids")
        if isinstance(category_ids_raw, (list, tuple)):
            raw_values.extend(category_ids_raw)
        single = args.get("category")
        if single not in (None, ""):
            raw_values.append(single)
        if not raw_values:
            raw_values = [DEFAULT_CATEGORY]
        category_ids: list[str] = []
        names: list[str] = []
        for raw in raw_values[: self.max_categories]:
            text = str(raw or "").strip()
            if not text:
                continue
            category = self._find_category_locked(state, scope, text)
            if category is None:
                if not create:
                    raise ContractError("category not found")
                category = self._ensure_category_locked(
                    state,
                    scope=scope,
                    name=self._normalise_category_name(text),
                    actor_id=context.actor_id,
                )
            cid = str(category.get("category_id", ""))
            if cid and cid not in category_ids:
                category_ids.append(cid)
                names.append(str(category.get("name", "")))
        if not category_ids:
            raise ContractError("at least one category is required")
        return category_ids, names

    def _public_sticker_locked(
        self, state: dict[str, Any], row: Mapping[str, Any]
    ) -> dict[str, Any]:
        category_ids = [
            str(value) for value in row.get("category_ids", []) if str(value)
        ]
        return {
            "sticker_id": str(row.get("sticker_id", "")),
            "content_hash": str(row.get("content_hash", "")),
            "asset_ref": _asset_ref_safe(row.get("asset_ref", "")),
            "mime": str(row.get("mime", "") or ""),
            "bytes": _safe_int(row.get("bytes"), 0),
            "width": _safe_int(row.get("width"), 0),
            "height": _safe_int(row.get("height"), 0),
            "category_ids": category_ids,
            "categories": self._category_names(state, category_ids),
            "owner_scope": str(row.get("owner_scope", "")),
            "source_message_id": str(row.get("source_message_id", "")),
            "created_by": str(row.get("created_by", "")),
            "created_at": float(row.get("created_at", 0) or 0),
            "updated_at": float(row.get("updated_at", 0) or 0),
            "revision": _safe_int(row.get("revision"), 0),
            "tombstone": bool(row.get("tombstone")),
            "tombstone_at": row.get("tombstone_at"),
            "tags": list(row.get("tags", []) or []),
            "meaning": str(row.get("meaning", "") or ""),
            "tone": str(row.get("tone", "") or ""),
        }

    def _asset_relative_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            if resolved != self.root and self.root not in resolved.parents:
                raise ContractError("asset escapes sticker root")
        except OSError as exc:
            raise ContractError("asset path is unavailable") from exc
        if resolved.is_symlink() or not stat.S_ISREG(resolved.lstat().st_mode):
            raise ContractError("asset is not a regular file")
        return resolved.relative_to(self.root).as_posix()

    def resolve_asset_path(self, asset_ref: str) -> Path | None:
        try:
            safe_ref = _asset_ref_safe(asset_ref)
        except ContractError:
            return None
        candidate = (self.root / safe_ref).resolve()
        try:
            if candidate == self.root or self.root not in candidate.parents:
                return None
            if candidate.is_symlink() or not stat.S_ISREG(candidate.lstat().st_mode):
                return None
            if candidate.stat().st_size > self.max_file_bytes:
                return None
            return candidate
        except OSError:
            return None

    def _record_orphan_best_effort(self, asset_ref: str, detail: str) -> None:
        try:
            orphan = {
                "asset_ref": _asset_ref_safe(asset_ref),
                "detail": _safe_text(detail, 200),
                "at": float(self.clock()),
            }
            current: list[dict[str, Any]] = []
            if self.orphan_path.exists() and not self.orphan_path.is_symlink():
                raw = self.store._read_bytes_nofollow(self.orphan_path, 1024 * 1024)
                if raw:
                    loaded = json.loads(raw.decode("utf-8"))
                    if isinstance(loaded, list):
                        current = [item for item in loaded if isinstance(item, dict)]
            current.append(orphan)
            self.store.atomic_write_bytes(
                self.orphan_path,
                json.dumps(current[-1000:], ensure_ascii=False, indent=2).encode(
                    "utf-8"
                ),
                mode=0o600,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Bootstrap existing legacy asset store into legacy-compatible scope
    # ------------------------------------------------------------------
    def _bootstrap_legacy_store_locked(self, state: dict[str, Any]) -> bool:
        changed = False
        try:
            legacy_entries = self.store.load_index()
        except Exception:
            return False
        categories = state.setdefault("categories", {})
        collections = state.setdefault("collections", {})
        for entry in legacy_entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("category", "")) == ASSET_CATEGORY:
                continue
            digest = str(entry.get("sha256", "") or "").lower()
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                continue
            existing = next(
                (
                    row
                    for row in collections.values()
                    if isinstance(row, dict)
                    and row.get("owner_scope") == LEGACY_SCOPE
                    and row.get("content_hash") == digest
                ),
                None,
            )
            if existing is not None:
                continue
            try:
                asset_ref = self._asset_relative_path(Path(entry["path"]))
            except Exception:
                continue
            category_names = entry.get("categories")
            if not isinstance(category_names, (list, tuple)):
                category_names = [entry.get("category", DEFAULT_CATEGORY)]
            category_ids: list[str] = []
            for name in category_names:
                try:
                    category = self._ensure_category_locked(
                        state,
                        scope=LEGACY_SCOPE,
                        name=self._normalise_category_name(name),
                        actor_id="legacy-migration",
                        enforce_limit=False,
                    )
                except ContractError:
                    continue
                category_ids.append(str(category["category_id"]))
            if not category_ids:
                category = self._ensure_category_locked(
                    state,
                    scope=LEGACY_SCOPE,
                    name=DEFAULT_CATEGORY,
                    actor_id="legacy-migration",
                    enforce_limit=False,
                )
                category_ids = [str(category["category_id"])]
            sticker_id = "legacy_" + digest[:24]
            now = float(self.clock())
            revision = self._bump_revision(
                state,
                "legacy_import",
                sticker_id=sticker_id,
                owner_scope=LEGACY_SCOPE,
            )
            row = {
                "sticker_id": sticker_id,
                "content_hash": digest,
                "asset_ref": asset_ref,
                "mime": str(entry.get("mime", "") or ""),
                "bytes": _safe_int(entry.get("size"), 0),
                "width": _safe_int(entry.get("width"), 0),
                "height": _safe_int(entry.get("height"), 0),
                "category_ids": category_ids,
                "owner_scope": LEGACY_SCOPE,
                "source_message_id": "legacy:" + digest[:16],
                "created_by": "legacy-migration",
                "created_at": now,
                "updated_at": now,
                "revision": revision,
                "tombstone": False,
                "tombstone_at": None,
                "tombstone_by": "",
                "tags": list(entry.get("tags", []) or []),
                "meaning": str(entry.get("meaning", "") or ""),
                "tone": str(entry.get("tone", "") or ""),
            }
            collections[sticker_id] = row
            state.setdefault("assets", {}).setdefault(
                digest,
                {
                    "content_hash": digest,
                    "asset_ref": asset_ref,
                    "mime": row["mime"],
                    "bytes": row["bytes"],
                    "width": row["width"],
                    "height": row["height"],
                    "created_at": now,
                },
            )
            changed = True
        return changed

    # ------------------------------------------------------------------
    # Result helpers
    # ------------------------------------------------------------------
    def _confirmation_payload(
        self, action: str, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "action": str(action),
            "args": _exclude_confirmation_fields(args),
        }

    def _confirmation_ref(self, context: RequestContext, args: Mapping[str, Any]) -> str:
        token = (
            args.get("confirmation_ref")
            or args.get("confirmation_token")
            or args.get("confirm_token")
            or context.extra.get("confirmation_ref")
            or ""
        )
        return str(token or "").strip()

    def _issue_confirmation_result(
        self,
        context: RequestContext,
        *,
        action: str,
        args: Mapping[str, Any],
        preview: Mapping[str, Any],
        call_id: str = "",
    ) -> dict[str, Any]:
        issued = self.confirmations.issue(
            context,
            action=action,
            payload=self._confirmation_payload(action, args),
            ttl_seconds=self.confirmation_ttl_seconds,
        )
        data = {
            **issued,
            "preview": dict(preview),
        }
        return tool_result(
            context,
            status="confirmation_required",
            result_type="confirmation",
            data=data,
            error_code="confirmation_required",
            retryable=False,
            side_effect_state="none",
            provider_operation_id="preview-" + stable_hash(
                {"action": action, "args": _exclude_confirmation_fields(args)}
            )[:24],
            call_id=call_id,
        )

    def _validate_confirmation(
        self,
        context: RequestContext,
        *,
        action: str,
        args: Mapping[str, Any],
        call_id: str = "",
    ) -> dict[str, Any] | None:
        token = self._confirmation_ref(context, args)
        if not token:
            return self._issue_confirmation_result(
                context,
                action=action,
                args=args,
                preview={"reason": "confirmation_required", "action": action},
                call_id=call_id,
            )
        decision = self.confirmations.validate(
            token,
            context,
            action=action,
            payload=self._confirmation_payload(action, args),
            consume=True,
        )
        if not decision.ok:
            return error_result(
                context,
                error_code=decision.error_code or "confirmation_expired",
                data={"reason": decision.reason, "action": action},
                operation=action,
                args=args,
                call_id=call_id,
            )
        return None

    def _prepare_collect_metadata(
        self, args: Mapping[str, Any]
    ) -> dict[str, Any]:
        raw_categories: list[str] = []
        categories = args.get("categories")
        if isinstance(categories, (list, tuple)):
            raw_categories.extend(str(item) for item in categories if str(item).strip())
        category = args.get("category")
        if category not in (None, ""):
            raw_categories.insert(0, str(category))
        if not raw_categories:
            raw_categories = [DEFAULT_CATEGORY]
        normalised_categories: list[str] = []
        for name in raw_categories:
            value = self._normalise_category_name(name)
            if value not in normalised_categories:
                normalised_categories.append(value)
        if len(normalised_categories) > self.max_categories:
            raise ContractError("too many categories")
        tags = self.store._sanitize_tags(args.get("tags", []))
        meaning = self.store._sanitize_meaning(args.get("meaning", ""))
        tone = self.store._sanitize_tone(args.get("tone", ""))
        if tags is None or meaning is None or tone is None:
            raise ContractError("invalid semantic metadata")
        return {
            "categories": normalised_categories,
            "tags": tags,
            "meaning": meaning,
            "tone": tone,
        }

    # ------------------------------------------------------------------
    # Fixed ABI methods: collect plan/commit
    # ------------------------------------------------------------------
    async def sticker_collect_plan(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            data = _normalise_data(
                args_map.get("data", args_map.get("content", args_map.get("image_bytes")))
            )
            if data is None:
                raise ContractError("image data is required")
            info = inspect_image_bytes(
                data,
                max_bytes=self.max_file_bytes,
                max_dimension=self.max_dimension,
                max_pixels=self.max_pixels,
            )
            metadata = self._prepare_collect_metadata(args_map)
            content_hash = hashlib.sha256(data).hexdigest()
            plan_id = "pln_" + uuid.uuid4().hex
            now = float(self.clock())
            with self._plan_lock:
                expired = [
                    key
                    for key, value in self._pending_collect.items()
                    if now > float(value.get("expires_at", 0) or 0)
                ]
                for key in expired:
                    self._pending_collect.pop(key, None)
                if len(self._pending_collect) >= MAX_PENDING_PLANS:
                    oldest = sorted(
                        self._pending_collect.items(),
                        key=lambda pair: float(pair[1].get("created_at", 0) or 0),
                    )
                    for key, _ in oldest[: len(self._pending_collect) - MAX_PENDING_PLANS + 1]:
                        self._pending_collect.pop(key, None)
                self._pending_collect[plan_id] = {
                    "plan_id": plan_id,
                    "data": data,
                    "content_hash": content_hash,
                    "created_at": now,
                    "expires_at": now + min(300, self.confirmation_ttl_seconds),
                    "instance_id": context.instance_id,
                    "actor_id": context.actor_id,
                    "owner_scope": context.owner_scope,
                    "conversation_id": context.conversation_id,
                    "persona_id": context.persona_id,
                    "owner_epoch": context.owner_epoch,
                    "metadata": metadata,
                    "metadata_hash": stable_hash(metadata),
                    "args_hash": stable_hash(_exclude_confirmation_fields(args_map)),
                    "source_message_id": str(
                        args_map.get("source_message_id")
                        or context.source_message_id
                    ),
                    "idempotency_key": str(
                        args_map.get("idempotency_key", "") or ""
                    ),
                }
            call_id = str(args_map.get("call_id", "") or "")
            result = tool_result(
                context,
                status="planned",
                result_type="sticker_collect_plan",
                data={
                    "plan_id": plan_id,
                    "content_hash": content_hash,
                    "size": info.size,
                    "mime": info.mime,
                    "width": info.width,
                    "height": info.height,
                    "categories": metadata["categories"],
                    "expires_at": now + min(300, self.confirmation_ttl_seconds),
                },
                error_code="",
                retryable=False,
                side_effect_state="none",
                provider_operation_id="collect-plan-" + content_hash[:24],
                call_id=call_id,
            )
            return result
        except ImageValidationError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": exc.code},
                operation="sticker_collect_plan",
                args=args_map,
            )
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="sticker_collect_plan",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_collect_plan",
                args=args_map,
                side_effect_state="unknown",
            )

    async def sticker_collect_commit(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            plan_id = str(args_map.get("plan_id", "") or "").strip()
            if not plan_id:
                raise ContractError("plan_id is required")
            with self._plan_lock:
                plan = self._pending_collect.get(plan_id)
                if not isinstance(plan, dict):
                    raise ContractError("collect plan not found or expired")
                if float(self.clock()) > float(plan.get("expires_at", 0) or 0):
                    self._pending_collect.pop(plan_id, None)
                    raise ContractError("collect plan expired")
                binding = (
                    plan.get("instance_id") == context.instance_id
                    and plan.get("actor_id") == context.actor_id
                    and plan.get("owner_scope") == context.owner_scope
                    and plan.get("conversation_id") == context.conversation_id
                    and plan.get("persona_id") == context.persona_id
                    and _safe_int(plan.get("owner_epoch"), -1) == context.owner_epoch
                )
                if not binding:
                    raise ContractError("collect plan context mismatch")
                metadata = copy.deepcopy(plan["metadata"])
                supplied_data = args_map.get(
                    "data", args_map.get("content", args_map.get("image_bytes"))
                )
                if supplied_data is not None:
                    supplied_bytes = _normalise_data(supplied_data)
                    if supplied_bytes is None or hashlib.sha256(
                        supplied_bytes
                    ).hexdigest() != plan["content_hash"]:
                        raise ContractError("collect plan payload changed")
                metadata_keys = ("category", "categories", "tags", "meaning", "tone")
                if any(key in args_map for key in metadata_keys):
                    supplied_metadata = self._prepare_collect_metadata(args_map)
                    if stable_hash(supplied_metadata) != plan.get("metadata_hash"):
                        raise ContractError("collect plan metadata changed")
                effective_args = dict(args_map)
                effective_args["source_message_id"] = plan.get(
                    "source_message_id", context.source_message_id
                )
                if plan.get("idempotency_key"):
                    effective_args.setdefault("idempotency_key", plan["idempotency_key"])

            async with self._write_guard():
                state = self._load_state_locked(create=True)
                replay, prior = self._check_operation_replay_locked(
                    state, context, "sticker_collect_commit", effective_args
                )
                if replay:
                    return prior or error_result(
                        context,
                        error_code="revision_conflict",
                        operation="sticker_collect_commit",
                        args=effective_args,
                    )
                result, publish = self._collect_data_locked(
                    state,
                    context,
                    data=plan["data"],
                    metadata=metadata,
                    args=effective_args,
                )
                if result.get("status") != "succeeded":
                    return result
                self._record_operation_locked(
                    state,
                    context,
                    "sticker_collect_commit",
                    effective_args,
                    result,
                )
                if not self._commit_state_locked(state, publish_index=publish):
                    try:
                        ref = result.get("data", {}).get("sticker", {}).get(
                            "asset_ref", ""
                        )
                        if ref:
                            self._record_orphan_best_effort(
                                str(ref), "collection state/index commit failed"
                            )
                    except Exception:
                        pass
                    response = error_result(
                        context,
                        error_code="storage_error",
                        data={"orphan_recorded": True},
                        operation="sticker_collect_commit",
                        args=effective_args,
                        side_effect_state="unknown",
                    )
                    return response
                with self._plan_lock:
                    self._pending_collect.pop(plan_id, None)
                self._collect_count += 1
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="sticker_collect_commit",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_collect_commit",
                args=args_map,
                side_effect_state="unknown",
            )

    def _collect_data_locked(
        self,
        state: dict[str, Any],
        context: RequestContext,
        *,
        data: bytes,
        metadata: Mapping[str, Any],
        args: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        info = inspect_image_bytes(
            data,
            max_bytes=self.max_file_bytes,
            max_dimension=self.max_dimension,
            max_pixels=self.max_pixels,
        )
        content_hash = hashlib.sha256(info.data).hexdigest()
        category_ids, _category_names = self._resolve_category_ids_for_args_locked(
            state,
            context,
            {
                "categories": metadata.get("categories", []),
                "tags": metadata.get("tags", []),
                "meaning": metadata.get("meaning", ""),
                "tone": metadata.get("tone", ""),
            },
            create=True,
        )
        existing_row = next(
            (
                row
                for row in state.get("collections", {}).values()
                if isinstance(row, dict)
                and not row.get("tombstone")
                and row.get("owner_scope") == context.owner_scope
                and row.get("content_hash") == content_hash
            ),
            None,
        )
        if existing_row is not None:
            changed = False
            old_categories = list(existing_row.get("category_ids", []))
            merged_categories = list(old_categories)
            for category_id in category_ids:
                if category_id not in merged_categories:
                    merged_categories.append(category_id)
                    changed = True
            merged_tags = list(existing_row.get("tags", []))
            for tag in metadata.get("tags", []):
                if tag not in merged_tags:
                    merged_tags.append(tag)
                    changed = True
            new_meaning = (
                str(metadata.get("meaning", "") or "")
                or str(existing_row.get("meaning", "") or "")
            )
            new_tone = str(metadata.get("tone", "") or "") or str(
                existing_row.get("tone", "") or ""
            )
            if (
                new_meaning != existing_row.get("meaning", "")
                or new_tone != existing_row.get("tone", "")
                or merged_tags != existing_row.get("tags", [])
            ):
                changed = True
            if changed:
                revision = self._bump_revision(
                    state,
                    "collection_update",
                    sticker_id=existing_row["sticker_id"],
                    owner_scope=context.owner_scope,
                )
                existing_row.update(
                    {
                        "category_ids": merged_categories,
                        "tags": merged_tags,
                        "meaning": new_meaning,
                        "tone": new_tone,
                        "updated_at": float(self.clock()),
                        "revision": revision,
                    }
                )
            sticker = self._public_sticker_locked(state, existing_row)
            result = success_result(
                context,
                result_type="sticker_collection",
                data={
                    "sticker": sticker,
                    "duplicate": True,
                    "asset_reused": True,
                    "created": False,
                },
                operation="sticker_collect_commit",
                args=args,
            )
            return result, changed

        add_status, entry = self.store.add_sticker(
            info.data,
            category=ASSET_CATEGORY,
            categories=[ASSET_CATEGORY],
            tags=[],
            meaning="",
            tone="",
            source=_safe_text(context.platform, 64),
            merge_duplicate=False,
        )
        if add_status not in {"saved", "duplicate"} or not isinstance(entry, dict):
            error_code = "storage_error" if add_status in {"storage", "limit"} else "invalid_args"
            result = error_result(
                context,
                error_code=error_code,
                data={"reason": add_status},
                operation="sticker_collect_commit",
                args=args,
            )
            return result, False
        try:
            asset_ref = self._asset_relative_path(Path(entry["path"]))
        except Exception:
            return (
                error_result(
                    context,
                    error_code="storage_error",
                    data={"reason": "unsafe_asset_path"},
                    operation="sticker_collect_commit",
                    args=args,
                ),
                False,
            )
        now = float(self.clock())
        state.setdefault("assets", {}).setdefault(
            content_hash,
            {
                "content_hash": content_hash,
                "asset_ref": asset_ref,
                "mime": info.mime,
                "bytes": info.size,
                "width": info.width,
                "height": info.height,
                "created_at": now,
            },
        )
        sticker_id = "stk_" + uuid.uuid4().hex
        revision = self._bump_revision(
            state,
            "collection_create",
            sticker_id=sticker_id,
            owner_scope=context.owner_scope,
            content_hash=content_hash,
        )
        row = {
            "sticker_id": sticker_id,
            "content_hash": content_hash,
            "asset_ref": asset_ref,
            "mime": info.mime,
            "bytes": info.size,
            "width": info.width,
            "height": info.height,
            "category_ids": category_ids,
            "owner_scope": context.owner_scope,
            "source_message_id": str(
                args.get("source_message_id") or context.source_message_id
            ),
            "created_by": context.actor_id,
            "created_at": now,
            "updated_at": now,
            "revision": revision,
            "tombstone": False,
            "tombstone_at": None,
            "tombstone_by": "",
            "tags": list(metadata.get("tags", []) or []),
            "meaning": str(metadata.get("meaning", "") or ""),
            "tone": str(metadata.get("tone", "") or ""),
        }
        state.setdefault("collections", {})[sticker_id] = row
        result = success_result(
            context,
            result_type="sticker_collection",
            data={
                "sticker": self._public_sticker_locked(state, row),
                "duplicate": False,
                "asset_reused": add_status == "duplicate",
                "created": True,
            },
            operation="sticker_collect_commit",
            args=args,
        )
        return result, True

    # ------------------------------------------------------------------
    # Fixed ABI methods: categories
    # ------------------------------------------------------------------
    async def category_list(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            with self._read_guard():
                state = self._load_state_locked()
                counts: dict[str, int] = {}
                for row in state.get("collections", {}).values():
                    if (
                        not isinstance(row, dict)
                        or row.get("tombstone")
                        or row.get("owner_scope") != context.owner_scope
                    ):
                        continue
                    for category_id in row.get("category_ids", []):
                        counts[str(category_id)] = counts.get(str(category_id), 0) + 1
                items: list[dict[str, Any]] = []
                for category in state.get("categories", {}).values():
                    if (
                        not isinstance(category, dict)
                        or category.get("tombstone")
                        or category.get("owner_scope") != context.owner_scope
                    ):
                        continue
                    category_id = str(category.get("category_id", ""))
                    items.append(
                        {
                            "category_id": category_id,
                            "name": str(category.get("name", "")),
                            "owner_scope": context.owner_scope,
                            "collection_count": counts.get(category_id, 0),
                            "revision": _safe_int(category.get("revision"), 0),
                        }
                    )
                items.sort(key=lambda item: item["name"])
            return tool_result(
                context,
                status="succeeded",
                result_type="category_list",
                data={"categories": items, "total": len(items)},
                side_effect_state="none",
                provider_operation_id="category-list-"
                + stable_hash({"scope": context.owner_scope})[:20],
                call_id=str(args_map.get("call_id", "") or ""),
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="category_list",
                args=args_map,
            )

    async def category_create(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            name = self._normalise_category_name(args_map.get("name"))
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                replay, prior = self._check_operation_replay_locked(
                    state, context, "category_create", args_map
                )
                if replay:
                    return prior or error_result(
                        context,
                        error_code="revision_conflict",
                        operation="category_create",
                        args=args_map,
                    )
                existing = self._find_category_locked(
                    state, context.owner_scope, name
                )
                created = False
                if existing is None:
                    existing = self._ensure_category_locked(
                        state,
                        scope=context.owner_scope,
                        name=name,
                        actor_id=context.actor_id,
                    )
                    created = True
                data = {
                    "category": {
                        "category_id": str(existing["category_id"]),
                        "name": str(existing["name"]),
                        "owner_scope": context.owner_scope,
                        "revision": _safe_int(existing.get("revision"), 0),
                    },
                    "created": created,
                }
                result = success_result(
                    context,
                    result_type="category",
                    data=data,
                    operation="category_create",
                    args=args_map,
                )
                self._record_operation_locked(
                    state, context, "category_create", args_map, result
                )
                if not self._commit_state_locked(state):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="category_create",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="category_create",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="category_create",
                args=args_map,
                side_effect_state="unknown",
            )

    async def category_rename(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            category_id_or_name = str(
                args_map.get("category_id") or args_map.get("category") or args_map.get("name")
            ).strip()
            new_name = self._normalise_category_name(
                args_map.get("new_name") or args_map.get("name_new")
            )
            if not category_id_or_name:
                raise ContractError("category is required")
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                replay, prior = self._check_operation_replay_locked(
                    state, context, "category_rename", args_map
                )
                if replay:
                    return prior or error_result(
                        context,
                        error_code="revision_conflict",
                        operation="category_rename",
                        args=args_map,
                    )
                category = self._find_category_locked(
                    state, context.owner_scope, category_id_or_name
                )
                if category is None:
                    return error_result(
                        context,
                        error_code="not_found",
                        data={"reason": "category_not_found"},
                        operation="category_rename",
                        args=args_map,
                    )
                conflict = self._find_category_locked(
                    state, context.owner_scope, new_name
                )
                if conflict is not None and conflict.get("category_id") != category.get("category_id"):
                    return error_result(
                        context,
                        error_code="revision_conflict",
                        data={"reason": "category_name_exists"},
                        operation="category_rename",
                        args=args_map,
                    )
                revision = self._bump_revision(
                    state,
                    "category_rename",
                    category_id=str(category["category_id"]),
                )
                old_name = str(category.get("name", ""))
                category.update(
                    {
                        "name": new_name,
                        "updated_at": float(self.clock()),
                        "revision": revision,
                    }
                )
                result = success_result(
                    context,
                    result_type="category",
                    data={
                        "category": {
                            "category_id": str(category["category_id"]),
                            "name": new_name,
                            "owner_scope": context.owner_scope,
                            "revision": revision,
                        },
                        "old_name": old_name,
                    },
                    operation="category_rename",
                    args=args_map,
                )
                self._record_operation_locked(
                    state, context, "category_rename", args_map, result
                )
                if not self._commit_state_locked(state):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="category_rename",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="category_rename",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="category_rename",
                args=args_map,
                side_effect_state="unknown",
            )

    async def category_remove(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            category_id_or_name = str(
                args_map.get("category_id") or args_map.get("category") or args_map.get("name")
            ).strip()
            if not category_id_or_name:
                raise ContractError("category is required")
            if not self._confirmation_ref(context, args_map):
                with self._read_guard():
                    state = self._load_state_locked()
                    category = self._find_category_locked(
                        state, context.owner_scope, category_id_or_name
                    )
                    if category is None:
                        return error_result(
                            context,
                            error_code="not_found",
                            data={"reason": "category_not_found"},
                            operation="category_remove",
                            args=args_map,
                        )
                    affected = sum(
                        1
                        for row in state.get("collections", {}).values()
                        if isinstance(row, dict)
                        and not row.get("tombstone")
                        and row.get("owner_scope") == context.owner_scope
                        and str(category["category_id"]) in row.get("category_ids", [])
                    )
                    preview = {
                        "category_id": str(category["category_id"]),
                        "category_name": str(category["name"]),
                        "owner_scope": context.owner_scope,
                        "affected_collections": affected,
                        "assets_preserved": True,
                    }
                return self._issue_confirmation_result(
                    context,
                    action="category_remove",
                    args=args_map,
                    preview=preview,
                )
            confirmation_error = self._validate_confirmation(
                context, action="category_remove", args=args_map
            )
            if confirmation_error is not None:
                return confirmation_error
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                replay, prior = self._check_operation_replay_locked(
                    state, context, "category_remove", args_map
                )
                if replay:
                    return prior or error_result(
                        context,
                        error_code="revision_conflict",
                        operation="category_remove",
                        args=args_map,
                    )
                category = self._find_category_locked(
                    state, context.owner_scope, category_id_or_name
                )
                if category is None:
                    return error_result(
                        context,
                        error_code="not_found",
                        data={"reason": "category_not_found"},
                        operation="category_remove",
                        args=args_map,
                    )
                category_id = str(category["category_id"])
                affected_rows: list[str] = []
                for row in state.get("collections", {}).values():
                    if (
                        not isinstance(row, dict)
                        or row.get("tombstone")
                        or row.get("owner_scope") != context.owner_scope
                        or category_id not in row.get("category_ids", [])
                    ):
                        continue
                    updated = [
                        value
                        for value in row.get("category_ids", [])
                        if str(value) != category_id
                    ]
                    revision = self._bump_revision(
                        state,
                        "category_relation_remove",
                        sticker_id=str(row.get("sticker_id", "")),
                        category_id=category_id,
                    )
                    row["category_ids"] = updated
                    row["revision"] = revision
                    row["updated_at"] = float(self.clock())
                    affected_rows.append(str(row.get("sticker_id", "")))
                remove_revision = self._bump_revision(
                    state,
                    "category_remove",
                    category_id=category_id,
                    owner_scope=context.owner_scope,
                )
                category.update(
                    {
                        "tombstone": True,
                        "tombstone_at": float(self.clock()),
                        "tombstone_by": context.actor_id,
                        "updated_at": float(self.clock()),
                        "revision": remove_revision,
                    }
                )
                result = success_result(
                    context,
                    result_type="category_removed",
                    data={
                        "category_id": category_id,
                        "category_name": str(category.get("name", "")),
                        "affected_collections": len(affected_rows),
                        "affected_sticker_ids": affected_rows,
                        "assets_preserved": True,
                    },
                    operation="category_remove",
                    args=args_map,
                )
                self._record_operation_locked(
                    state, context, "category_remove", args_map, result
                )
                if not self._commit_state_locked(state):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="category_remove",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="category_remove",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="category_remove",
                args=args_map,
                side_effect_state="unknown",
            )

    # ------------------------------------------------------------------
    # Fixed ABI methods: categorize/search
    # ------------------------------------------------------------------
    async def sticker_categorize(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            sticker_id = str(args_map.get("sticker_id", "") or "").strip()
            if not sticker_id:
                raise ContractError("sticker_id is required")
            if not self._confirmation_ref(context, args_map):
                with self._read_guard():
                    state = self._load_state_locked()
                    row = state.get("collections", {}).get(sticker_id)
                    if (
                        not isinstance(row, dict)
                        or row.get("tombstone")
                        or row.get("owner_scope") != context.owner_scope
                    ):
                        return error_result(
                            context,
                            error_code="not_found",
                            data={"reason": "sticker_not_found"},
                            operation="sticker_categorize",
                            args=args_map,
                        )
                    existing = self._public_sticker_locked(state, row)
                    requested = args_map.get("categories") or args_map.get(
                        "category_ids"
                    ) or args_map.get("category")
                if not requested:
                    raise ContractError("target category is required")
                return self._issue_confirmation_result(
                    context,
                    action="sticker_categorize",
                    args=args_map,
                    preview={"sticker": existing, "requested_categories": requested},
                )
            confirmation_error = self._validate_confirmation(
                context, action="sticker_categorize", args=args_map
            )
            if confirmation_error is not None:
                return confirmation_error
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                replay, prior = self._check_operation_replay_locked(
                    state, context, "sticker_categorize", args_map
                )
                if replay:
                    return prior or error_result(
                        context,
                        error_code="revision_conflict",
                        operation="sticker_categorize",
                        args=args_map,
                    )
                row = state.get("collections", {}).get(sticker_id)
                if (
                    not isinstance(row, dict)
                    or row.get("tombstone")
                    or row.get("owner_scope") != context.owner_scope
                ):
                    return error_result(
                        context,
                        error_code="not_found",
                        data={"reason": "sticker_not_found"},
                        operation="sticker_categorize",
                        args=args_map,
                    )
                category_ids, category_names = self._resolve_category_ids_for_args_locked(
                    state, context, args_map, create=True
                )
                revision = self._bump_revision(
                    state,
                    "sticker_categorize",
                    sticker_id=sticker_id,
                    category_ids=category_ids,
                )
                row["category_ids"] = category_ids
                row["revision"] = revision
                row["updated_at"] = float(self.clock())
                result = success_result(
                    context,
                    result_type="sticker",
                    data={
                        "sticker": self._public_sticker_locked(state, row),
                        "categories": category_names,
                    },
                    operation="sticker_categorize",
                    args=args_map,
                )
                self._record_operation_locked(
                    state, context, "sticker_categorize", args_map, result
                )
                if not self._commit_state_locked(state):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="sticker_categorize",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="sticker_categorize",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_categorize",
                args=args_map,
                side_effect_state="unknown",
            )

    def _filter_index_entries(
        self,
        state: dict[str, Any],
        entries: list[dict[str, Any]],
        context: RequestContext,
        args: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        scope = context.owner_scope
        query = str(args.get("query", args.get("q", "")) or "").strip().casefold()
        category_filter = str(
            args.get("category_id") or args.get("category") or ""
        ).strip()
        if category_filter:
            category = self._find_category_locked(state, scope, category_filter)
            category_filter_id = (
                str(category["category_id"]) if category is not None else ""
            )
            category_filter_name = (
                str(category["name"]).casefold() if category is not None else category_filter.casefold()
            )
        else:
            category_filter_id = ""
            category_filter_name = ""
        tag = str(args.get("tag", "") or "").strip().casefold()
        emotion = str(args.get("emotion", "") or "").strip().casefold()
        sticker_id = str(args.get("sticker_id", "") or "").strip()
        result: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("owner_scope") != scope or entry.get("tombstone"):
                continue
            if sticker_id and entry.get("sticker_id") != sticker_id:
                continue
            if category_filter_id:
                if category_filter_id not in entry.get("category_ids", []):
                    continue
            elif category_filter_name:
                if not any(
                    category_filter_name in str(name).casefold()
                    for name in entry.get("categories", [])
                ):
                    continue
            text_parts = [
                str(entry.get("sticker_id", "")),
                str(entry.get("content_hash", "")),
                str(entry.get("meaning", "")),
                str(entry.get("tone", "")),
            ]
            text_parts.extend(str(value) for value in entry.get("tags", []))
            text_parts.extend(str(value) for value in entry.get("categories", []))
            combined = "\n".join(text_parts).casefold()
            if query and query not in combined:
                continue
            if tag and tag not in combined:
                continue
            if emotion and emotion not in combined:
                from .sticker_intent import EMOTION_KEYWORDS, SNARK_KEYWORDS

                keywords: tuple[str, ...] = ()
                for words, emotion_name in EMOTION_KEYWORDS:
                    if emotion_name == emotion:
                        keywords = tuple(words)
                        break
                if emotion in {"angry", "cool"}:
                    keywords = tuple(SNARK_KEYWORDS)
                if not any(keyword.casefold() in combined for keyword in keywords):
                    continue
            result.append(entry)
        return result

    async def sticker_search(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            limit = _safe_int(
                args_map.get("limit", 20), 20, minimum=1
            )
            limit = min(MAX_SEARCH_LIMIT, limit)
            with self._read_guard():
                state = self._load_state_locked()
                entries = self._active_entries_locked(state)
                filtered = self._filter_index_entries(
                    state, entries, context, args_map
                )
                filtered.sort(
                    key=lambda item: (
                        -float(item.get("updated_at", 0) or 0),
                        str(item.get("sticker_id", "")),
                    )
                )
                items = [copy.deepcopy(item) for item in filtered[:limit]]
            return tool_result(
                context,
                status="succeeded",
                result_type="sticker_search",
                data={
                    "items": items,
                    "count": len(items),
                    "total": len(filtered),
                    "limit": limit,
                    "owner_scope": context.owner_scope,
                },
                side_effect_state="none",
                provider_operation_id="search-"
                + stable_hash(
                    {"scope": context.owner_scope, "args": _exclude_confirmation_fields(args_map)}
                )[:24],
                call_id=str(args_map.get("call_id", "") or ""),
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_search",
                args=args_map,
            )

    # ------------------------------------------------------------------
    # Fixed ABI methods: send plan and delivery accounting
    # ------------------------------------------------------------------
    def _delivery_key(
        self, context: RequestContext, args: Mapping[str, Any]
    ) -> str:
        business_key = str(
            args.get("idempotency_key")
            or args.get("source_message_id")
            or context.source_message_id
            or context.turn_id
        )
        payload = {
            "instance": context.instance_id,
            "bot": context.bot_id,
            "actor": context.actor_id,
            "scope": context.owner_scope,
            "business_key": business_key,
            "target": {
                key: args.get(key)
                for key in ("sticker_id", "category", "query", "tag", "emotion")
                if args.get(key) not in (None, "")
            },
        }
        return "dlv_" + stable_hash(payload)[:40]

    def _plan_from_delivery_locked(
        self,
        state: dict[str, Any],
        context: RequestContext,
        delivery: Mapping[str, Any],
        *,
        replay: bool,
    ) -> dict[str, Any]:
        row = state.get("collections", {}).get(str(delivery.get("sticker_id", "")))
        if not isinstance(row, dict) or row.get("tombstone"):
            return error_result(
                context,
                error_code="not_found",
                data={"reason": "sticker_not_found"},
                operation="sticker_send_plan",
            )
        sticker = self._public_sticker_locked(state, row)
        delivery_key = str(delivery.get("delivery_key", ""))
        plan = reply_plan(
            context,
            text_segments=[],
            media_segments=[
                {
                    "type": "image",
                    "asset_ref": sticker["asset_ref"],
                    "mime": sticker["mime"],
                    "bytes": sticker["bytes"],
                    "sticker_id": sticker["sticker_id"],
                }
            ],
            terminal=True,
            delivery_key=delivery_key,
            data={
                "sticker": sticker,
                "delivery_state": str(delivery.get("state", "planned")),
                "replayed": bool(replay),
            },
        )
        plan["side_effect_state"] = "planned"
        return plan

    async def sticker_send_plan(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        delivery_key = self._delivery_key(context, args_map)
        payload_hash = self._operation_payload_hash("sticker_send_plan", args_map)
        try:
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                deliveries = state.setdefault("deliveries", {})
                existing = deliveries.get(delivery_key)
                if isinstance(existing, dict):
                    if str(existing.get("payload_hash", "")) != payload_hash:
                        return error_result(
                            context,
                            error_code="revision_conflict",
                            data={"reason": "delivery_payload_changed"},
                            operation="sticker_send_plan",
                            args=args_map,
                        )
                    state_name = str(existing.get("state", "planned"))
                    if state_name in {"sending", "delivery_unknown"}:
                        return error_result(
                            context,
                            error_code="outcome_unknown",
                            status="outcome_unknown",
                            data={
                                "delivery_key": delivery_key,
                                "delivery_state": state_name,
                                "resend": False,
                            },
                            side_effect_state="unknown",
                            operation="sticker_send_plan",
                            args=args_map,
                        )
                    if state_name == "delivered":
                        return error_result(
                            context,
                            error_code="already_delivered",
                            data={
                                "delivery_key": delivery_key,
                                "delivery_state": "delivered",
                                "resend": False,
                                "message_id": existing.get("message_id", ""),
                            },
                            side_effect_state="committed",
                            operation="sticker_send_plan",
                            args=args_map,
                        )
                    return self._plan_from_delivery_locked(
                        state, context, existing, replay=True
                    )
                entries = [
                    entry
                    for entry in self._active_entries_locked(state)
                    if entry.get("owner_scope") == context.owner_scope
                ]
                filtered = self._filter_index_entries(
                    state, entries, context, args_map
                )
                if not filtered:
                    return error_result(
                        context,
                        error_code="not_found",
                        data={"reason": "no_matching_sticker"},
                        operation="sticker_send_plan",
                        args=args_map,
                    )
                recent = [
                    record
                    for record in deliveries.values()
                    if isinstance(record, dict)
                    and record.get("owner_scope") == context.owner_scope
                    and record.get("actor_id") == context.actor_id
                    and record.get("state") in {"planned", "sending", "delivered"}
                ]
                recent.sort(key=lambda item: float(item.get("created_at", 0) or 0))
                last_sticker = str(recent[-1].get("sticker_id", "")) if recent else ""
                cooldown = max(0, int(self.send_cooldown_seconds))
                if recent and cooldown > 0:
                    latest_at = max(float(item.get("created_at", 0) or 0) for item in recent)
                    if float(self.clock()) - latest_at < cooldown:
                        return error_result(
                            context,
                            error_code="permission_denied",
                            data={"reason": "cooldown", "retry_after": cooldown},
                            operation="sticker_send_plan",
                            args=args_map,
                        )
                candidates = list(filtered)
                if last_sticker and len(candidates) > 1:
                    non_repeating = [
                        item
                        for item in candidates
                        if item.get("sticker_id") != last_sticker
                    ]
                    if non_repeating:
                        candidates = non_repeating
                candidates.sort(key=lambda item: str(item.get("sticker_id", "")))
                chosen = self.rng.choice(candidates)
                now = float(self.clock())
                delivery = {
                    "delivery_key": delivery_key,
                    "payload_hash": payload_hash,
                    "sticker_id": str(chosen.get("sticker_id", "")),
                    "owner_scope": context.owner_scope,
                    "actor_id": context.actor_id,
                    "instance_id": context.instance_id,
                    "source_message_id": context.source_message_id,
                    "state": "planned",
                    "message_id": "",
                    "created_at": now,
                    "updated_at": now,
                    "revision": self._bump_revision(
                        state,
                        "send_plan",
                        delivery_key=delivery_key,
                        sticker_id=str(chosen.get("sticker_id", "")),
                        owner_scope=context.owner_scope,
                    ),
                }
                deliveries[delivery_key] = delivery
                if len(deliveries) > MAX_DELIVERIES:
                    ordered = sorted(
                        deliveries.items(),
                        key=lambda pair: float(pair[1].get("created_at", 0) or 0),
                    )
                    for old_key, _ in ordered[: len(deliveries) - MAX_DELIVERIES]:
                        deliveries.pop(old_key, None)
                if not self._commit_state_locked(state, publish_index=False):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="sticker_send_plan",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return self._plan_from_delivery_locked(
                    state, context, delivery, replay=False
                )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_send_plan",
                args=args_map,
                side_effect_state="unknown",
            )

    async def sticker_delivery_update(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Internal/non-provider helper for sender delivery accounting."""

        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            delivery_key = str(args_map.get("delivery_key", "") or "").strip()
            new_state = str(args_map.get("state", "") or "").strip()
            if not delivery_key or new_state not in {
                "sending",
                "delivered",
                "delivery_unknown",
            }:
                raise ContractError("delivery_key and valid state are required")
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                delivery = state.setdefault("deliveries", {}).get(delivery_key)
                if not isinstance(delivery, dict):
                    return error_result(
                        context,
                        error_code="not_found",
                        data={"reason": "delivery_not_found"},
                        operation="sticker_delivery_update",
                        args=args_map,
                    )
                if (
                    delivery.get("owner_scope") != context.owner_scope
                    or delivery.get("actor_id") != context.actor_id
                    or delivery.get("instance_id") != context.instance_id
                ):
                    return error_result(
                        context,
                        error_code="permission_denied",
                        operation="sticker_delivery_update",
                        args=args_map,
                    )
                old_state = str(delivery.get("state", "planned"))
                allowed = {
                    "planned": {"sending", "delivered", "delivery_unknown"},
                    "sending": {"delivered", "delivery_unknown"},
                    "delivery_unknown": {"delivery_unknown"},
                    "delivered": {"delivered"},
                }
                if new_state not in allowed.get(old_state, set()):
                    return error_result(
                        context,
                        error_code="revision_conflict",
                        data={
                            "reason": "invalid_delivery_transition",
                            "old_state": old_state,
                            "new_state": new_state,
                        },
                        operation="sticker_delivery_update",
                        args=args_map,
                    )
                delivery["state"] = new_state
                delivery["updated_at"] = float(self.clock())
                if args_map.get("message_id"):
                    delivery["message_id"] = _safe_text(args_map.get("message_id"), 200)
                if old_state != new_state:
                    delivery["revision"] = self._bump_revision(
                        state,
                        "delivery_update",
                        delivery_key=delivery_key,
                        state=new_state,
                    )
                result = success_result(
                    context,
                    result_type="delivery_status",
                    data={
                        "delivery_key": delivery_key,
                        "delivery_state": new_state,
                        "message_id": str(delivery.get("message_id", "")),
                        "resend": False,
                    },
                    operation="sticker_delivery_update",
                    args=args_map,
                )
                if not self._commit_state_locked(state, publish_index=False):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="sticker_delivery_update",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="sticker_delivery_update",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_delivery_update",
                args=args_map,
                side_effect_state="unknown",
            )

    # ------------------------------------------------------------------
    # Fixed ABI methods: logical remove
    # ------------------------------------------------------------------
    async def sticker_remove(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            sticker_id = str(args_map.get("sticker_id", "") or "").strip()
            if not sticker_id:
                raise ContractError("sticker_id is required")
            if not self._confirmation_ref(context, args_map):
                with self._read_guard():
                    state = self._load_state_locked()
                    row = state.get("collections", {}).get(sticker_id)
                    if (
                        not isinstance(row, dict)
                        or row.get("tombstone")
                        or row.get("owner_scope") != context.owner_scope
                    ):
                        return error_result(
                            context,
                            error_code="not_found",
                            data={"reason": "sticker_not_found"},
                            operation="sticker_remove",
                            args=args_map,
                        )
                    preview = {
                        "sticker": self._public_sticker_locked(state, row),
                        "asset_preserved": True,
                        "tombstone_mode": "logical_only",
                    }
                return self._issue_confirmation_result(
                    context,
                    action="sticker_remove",
                    args=args_map,
                    preview=preview,
                )
            confirmation_error = self._validate_confirmation(
                context, action="sticker_remove", args=args_map
            )
            if confirmation_error is not None:
                return confirmation_error
            async with self._write_guard():
                state = self._load_state_locked(create=True)
                replay, prior = self._check_operation_replay_locked(
                    state, context, "sticker_remove", args_map
                )
                if replay:
                    return prior or error_result(
                        context,
                        error_code="revision_conflict",
                        operation="sticker_remove",
                        args=args_map,
                    )
                row = state.get("collections", {}).get(sticker_id)
                if (
                    not isinstance(row, dict)
                    or row.get("tombstone")
                    or row.get("owner_scope") != context.owner_scope
                ):
                    return error_result(
                        context,
                        error_code="not_found",
                        data={"reason": "sticker_not_found"},
                        operation="sticker_remove",
                        args=args_map,
                    )
                revision = self._bump_revision(
                    state,
                    "sticker_tombstone",
                    sticker_id=sticker_id,
                    owner_scope=context.owner_scope,
                )
                row.update(
                    {
                        "tombstone": True,
                        "tombstone_at": float(self.clock()),
                        "tombstone_by": context.actor_id,
                        "updated_at": float(self.clock()),
                        "revision": revision,
                    }
                )
                result = success_result(
                    context,
                    result_type="sticker_removed",
                    data={
                        "sticker_id": sticker_id,
                        "tombstone": True,
                        "asset_preserved": True,
                        "asset_ref": str(row.get("asset_ref", "")),
                        "revision": revision,
                    },
                    operation="sticker_remove",
                    args=args_map,
                )
                self._record_operation_locked(
                    state, context, "sticker_remove", args_map, result
                )
                if not self._commit_state_locked(state):
                    return error_result(
                        context,
                        error_code="storage_error",
                        operation="sticker_remove",
                        args=args_map,
                        side_effect_state="unknown",
                    )
                return result
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="sticker_remove",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="sticker_remove",
                args=args_map,
                side_effect_state="unknown",
            )

    # ------------------------------------------------------------------
    # Fixed ABI methods: generation/high-water rebuild
    # ------------------------------------------------------------------
    async def index_rebuild_plan(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            with self._read_guard():
                state = self._load_state_locked()
                preview = {
                    "high_water": _safe_int(state.get("revision"), 0),
                    "active_generation": _safe_int(
                        state.get("index_generation"), 0
                    ),
                    "collection_rows": len(state.get("collections", {})),
                    "mode": "isolated_generation_high_water",
                    "old_generation_retained_on_failure": True,
                }
            return self._issue_confirmation_result(
                context,
                action="index_rebuild",
                args=args_map,
                preview=preview,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="index_rebuild_plan",
                args=args_map,
            )

    async def index_rebuild_commit(
        self, request_context: Any, args: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        context, error, args_map = self._prepare(request_context, args)
        if error is not None or context is None:
            return error  # type: ignore[return-value]
        try:
            if not self._confirmation_ref(context, args_map):
                return self._issue_confirmation_result(
                    context,
                    action="index_rebuild",
                    args=args_map,
                    preview={"reason": "confirmation_required"},
                )
            confirmation_error = self._validate_confirmation(
                context, action="index_rebuild", args=args_map
            )
            if confirmation_error is not None:
                return confirmation_error
            # 1) Snapshot under the writer lock and release while building.
            with self._read_guard():
                snapshot = copy.deepcopy(self._load_state_locked())
            snapshot_revision = _safe_int(snapshot.get("revision"), 0)
            candidate_entries = self._index_entries_locked(snapshot)
            candidate_map = {
                str(entry["sticker_id"]): entry for entry in candidate_entries
            }
            candidate = {
                "snapshot_revision": snapshot_revision,
                "entries": candidate_entries,
                "high_water": snapshot_revision,
            }
            hook = self.rebuild_snapshot_hook
            if callable(hook):
                value = hook(candidate)
                if asyncio.iscoroutine(value) or hasattr(value, "__await__"):
                    await value
            # 2) Catch up isolated candidate while mutations may have happened.
            async with self._write_guard():
                current = self._load_state_locked(create=True)
                current_revision = _safe_int(current.get("revision"), 0)
                catchup_count = 0
                changed_category_ids = {
                    str(category.get("category_id", ""))
                    for category in current.get("categories", {}).values()
                    if isinstance(category, dict)
                    and _safe_int(category.get("revision"), 0) > snapshot_revision
                }
                if changed_category_ids:
                    for sticker_id, entry in list(candidate_map.items()):
                        if changed_category_ids.intersection(
                            set(entry.get("category_ids", []))
                        ):
                            row = current.get("collections", {}).get(sticker_id)
                            if isinstance(row, dict):
                                rebuilt = self._index_entry_locked(current, row)
                                if rebuilt is None:
                                    candidate_map.pop(sticker_id, None)
                                else:
                                    candidate_map[sticker_id] = rebuilt
                                    catchup_count += 1
                for sticker_id, row in current.get("collections", {}).items():
                    if not isinstance(row, dict):
                        continue
                    if _safe_int(row.get("revision"), 0) <= snapshot_revision:
                        continue
                    rebuilt = self._index_entry_locked(current, row)
                    if rebuilt is None:
                        candidate_map.pop(sticker_id, None)
                    else:
                        candidate_map[sticker_id] = rebuilt
                    catchup_count += 1
                final_entries = sorted(
                    candidate_map.values(),
                    key=lambda item: (
                        float(item.get("created_at", 0) or 0),
                        str(item.get("sticker_id", "")),
                    ),
                )
                generation = _safe_int(current.get("index_generation"), 0) + 1
                current["index_generation"] = generation
                current["last_rebuild_revision"] = snapshot_revision
                current["last_rebuild_high_water"] = current_revision
                # State is saved before the pointer switch.  A failure here or
                # in generation write leaves the old active pointer untouched.
                self._save_state_locked(current)
                try:
                    relative = self._write_generation_atomic(
                        generation,
                        entries=final_entries,
                        high_water=current_revision,
                        rebuilt_from=snapshot_revision,
                    )
                    # Atomic active-generation pointer switch is the commit point.
                    self._switch_active_pointer_atomic(
                        generation=generation,
                        relative_path=relative,
                        high_water=current_revision,
                    )
                except Exception:
                    return error_result(
                        context,
                        error_code="storage_error",
                        data={
                            "generation": generation,
                            "active_generation": _safe_int(
                                snapshot.get("index_generation"), 0
                            ),
                            "old_generation_preserved": True,
                        },
                        operation="index_rebuild_commit",
                        args=args_map,
                        side_effect_state="none",
                    )
                self._rebuild_count += 1
                return success_result(
                    context,
                    result_type="index_rebuild",
                    data={
                        "generation": generation,
                        "high_water": current_revision,
                        "snapshot_revision": snapshot_revision,
                        "catchup_changes": catchup_count,
                        "entries": len(final_entries),
                        "old_generation_preserved": True,
                    },
                    operation="index_rebuild_commit",
                    args=args_map,
                )
        except ContractError as exc:
            return error_result(
                context,
                error_code="invalid_args",
                data={"reason": str(exc)},
                operation="index_rebuild_commit",
                args=args_map,
            )
        except Exception:
            return error_result(
                context,
                error_code="storage_error",
                operation="index_rebuild_commit",
                args=args_map,
                side_effect_state="unknown",
            )

    # ------------------------------------------------------------------
    # Legacy-compatible convenience surface used only by plugin adapters
    # ------------------------------------------------------------------
    async def collect_bytes(
        self,
        context: RequestContext,
        data: bytes,
        *,
        category: str = DEFAULT_CATEGORY,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        meaning: str = "",
        tone: str = "",
        idempotency_key: str = "",
        source_message_id: str = "",
    ) -> dict[str, Any]:
        """Collect bytes directly through the same single writer.

        This is an adapter convenience; provider callers should use the
        collect_plan/collect_commit pair.
        """

        if not self.ready:
            return error_result(context, error_code="not_ready")
        if context.owner_epoch != self.owner_epoch:
            return error_result(
                context,
                error_code="permission_denied",
                data={"reason": "stale_owner_epoch"},
            )
        metadata = self._prepare_collect_metadata(
            {
                "category": category,
                "categories": list(categories or [category]),
                "tags": list(tags or []),
                "meaning": meaning,
                "tone": tone,
            }
        )
        args = {
            "category": category,
            "categories": list(categories or [category]),
            "tags": list(tags or []),
            "meaning": meaning,
            "tone": tone,
            "idempotency_key": idempotency_key,
            "source_message_id": source_message_id or context.source_message_id,
        }
        async with self._write_guard():
            state = self._load_state_locked(create=True)
            replay, prior = self._check_operation_replay_locked(
                state, context, "sticker_collect_commit", args
            )
            if replay:
                return prior or error_result(
                    context, error_code="revision_conflict", args=args
                )
            result, publish = self._collect_data_locked(
                state, context, data=data, metadata=metadata, args=args
            )
            if result.get("status") != "succeeded":
                return result
            self._record_operation_locked(
                state, context, "sticker_collect_commit", args, result
            )
            if not self._commit_state_locked(state, publish_index=publish):
                return error_result(
                    context,
                    error_code="storage_error",
                    args=args,
                    side_effect_state="unknown",
                )
            self._collect_count += 1
            return result

    async def legacy_collect(
        self,
        context: RequestContext,
        data: bytes,
        *,
        category: str = DEFAULT_CATEGORY,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        meaning: str = "",
        tone: str = "",
        idempotency_key: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        result = await self.collect_bytes(
            context,
            data,
            category=category,
            categories=categories,
            tags=tags,
            meaning=meaning,
            tone=tone,
            idempotency_key=idempotency_key,
        )
        if result.get("status") != "succeeded":
            code = str(result.get("error_code", ""))
            reason = str(result.get("data", {}).get("reason", "") or "")
            if reason == "limit":
                return "limit", None
            if reason == "too_large":
                return "too_large", None
            if reason in {"invalid_image", "invalid_dimensions"}:
                return reason, None
            if code == "not_ready":
                return "storage", None
            if code in {"invalid_args", "not_found"}:
                return "invalid", None
            return "storage", None
        sticker = result.get("data", {}).get("sticker")
        if not isinstance(sticker, dict):
            return "storage", None
        entry = self.legacy_entry_view(context, sticker)
        return ("duplicate" if result.get("data", {}).get("duplicate") else "saved"), entry

    def legacy_entry_view(
        self, context: RequestContext, sticker: Mapping[str, Any]
    ) -> dict[str, Any]:
        category_ids = [str(value) for value in sticker.get("category_ids", [])]
        with self._read_guard():
            state = self._load_state_locked()
        categories = self._category_names(state, category_ids)
        asset_ref = str(sticker.get("asset_ref", "") or "")
        path = self.resolve_asset_path(asset_ref)
        category = categories[0] if categories else DEFAULT_CATEGORY
        return {
            "sha256": str(sticker.get("content_hash", "")),
            "filename": Path(asset_ref).name,
            "category": category,
            "categories": categories or [category],
            "size": _safe_int(sticker.get("bytes"), 0),
            "created_at": int(float(sticker.get("created_at", 0) or 0)),
            "source": "",
            "tags": list(sticker.get("tags", []) or []),
            "meaning": str(sticker.get("meaning", "") or ""),
            "tone": str(sticker.get("tone", "") or ""),
            "updated_at": int(float(sticker.get("updated_at", 0) or 0)),
            "path": path,
            "sticker_id": str(sticker.get("sticker_id", "")),
            "owner_scope": str(sticker.get("owner_scope", "")),
            "revision": _safe_int(sticker.get("revision"), 0),
            "tombstone": bool(sticker.get("tombstone")),
            "asset_ref": asset_ref,
        }

    def legacy_entries(self, scope: str = LEGACY_SCOPE) -> list[dict[str, Any]]:
        with self._read_guard():
            state = self._load_state_locked()
            rows = [
                self._public_sticker_locked(state, row)
                for row in state.get("collections", {}).values()
                if isinstance(row, dict)
                and not row.get("tombstone")
                and row.get("owner_scope") == scope
            ]
        rows.sort(key=lambda item: (item.get("created_at", 0), item["sticker_id"]))
        return [self.legacy_entry_view_for_sticker(item) for item in rows]

    def legacy_entry_view_for_sticker(
        self, sticker: Mapping[str, Any]
    ) -> dict[str, Any]:
        with self._read_guard():
            state = self._load_state_locked()
        return self._legacy_entry_view_for_sticker_locked(state, sticker)

    def _legacy_entry_view_for_sticker_locked(
        self, state: Mapping[str, Any], sticker: Mapping[str, Any]
    ) -> dict[str, Any]:
        category_ids = [str(value) for value in sticker.get("category_ids", [])]
        categories = self._category_names(state, category_ids)
        asset_ref = str(sticker.get("asset_ref", "") or "")
        path = self.resolve_asset_path(asset_ref)
        category = categories[0] if categories else DEFAULT_CATEGORY
        return {
            "sha256": str(sticker.get("content_hash", "")),
            "filename": Path(asset_ref).name,
            "category": category,
            "categories": categories or [category],
            "size": _safe_int(sticker.get("bytes"), 0),
            "created_at": int(float(sticker.get("created_at", 0) or 0)),
            "source": "",
            "tags": list(sticker.get("tags", []) or []),
            "meaning": str(sticker.get("meaning", "") or ""),
            "tone": str(sticker.get("tone", "") or ""),
            "updated_at": int(float(sticker.get("updated_at", 0) or 0)),
            "path": path,
            "sticker_id": str(sticker.get("sticker_id", "")),
            "owner_scope": str(sticker.get("owner_scope", "")),
            "revision": _safe_int(sticker.get("revision"), 0),
            "tombstone": bool(sticker.get("tombstone")),
            "asset_ref": asset_ref,
        }

    async def legacy_categories(
        self, context: RequestContext, scope: str = LEGACY_SCOPE
    ) -> dict[str, int]:
        result = await self.category_list(context, {})
        if result.get("status") != "succeeded":
            return {}
        return {
            str(item.get("name", "")): _safe_int(item.get("collection_count"), 0)
            for item in result.get("data", {}).get("categories", [])
            if item.get("owner_scope") == scope
        }

    async def legacy_update_sticker(
        self,
        context: RequestContext,
        *,
        content_hash: str,
        category: str,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        meaning: str = "",
        tone: str = "",
        idempotency_key: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        if not self.ready or context.owner_epoch != self.owner_epoch:
            return "storage", None
        args = {
            "content_hash": str(content_hash),
            "category": category,
            "categories": list(categories or [category]),
            "tags": list(tags or []),
            "meaning": meaning,
            "tone": tone,
            "idempotency_key": idempotency_key,
        }
        async with self._write_guard():
            state = self._load_state_locked(create=True)
            row = next(
                (
                    item
                    for item in state.get("collections", {}).values()
                    if isinstance(item, dict)
                    and not item.get("tombstone")
                    and item.get("owner_scope") == context.owner_scope
                    and item.get("content_hash") == content_hash
                ),
                None,
            )
            if row is None:
                return "not_found", None
            category_ids, category_names = self._resolve_category_ids_for_args_locked(
                state, context, args, create=True
            )
            tags_value = self.store._sanitize_tags(tags or [])
            meaning_value = self.store._sanitize_meaning(meaning)
            tone_value = self.store._sanitize_tone(tone)
            if tags_value is None or meaning_value is None or tone_value is None:
                return "invalid", None
            same = (
                list(row.get("category_ids", [])) == category_ids
                and list(row.get("tags", [])) == tags_value
                and str(row.get("meaning", "")) == meaning_value
                and str(row.get("tone", "")) == tone_value
            )
            if same:
                return "same", self._legacy_entry_view_for_sticker_locked(
                    state, self._public_sticker_locked(state, row)
                )
            revision = self._bump_revision(
                state,
                "legacy_update",
                sticker_id=str(row.get("sticker_id", "")),
            )
            row.update(
                {
                    "category_ids": category_ids,
                    "tags": tags_value,
                    "meaning": meaning_value,
                    "tone": tone_value,
                    "revision": revision,
                    "updated_at": float(self.clock()),
                }
            )
            if not self._commit_state_locked(state):
                return "storage", None
            return "updated", self._legacy_entry_view_for_sticker_locked(
                state, self._public_sticker_locked(state, row)
            )

    async def legacy_add_categories(
        self,
        context: RequestContext,
        content_hash: str,
        categories: list[str],
        *,
        remove: bool = False,
    ) -> tuple[str, dict[str, Any] | None]:
        if not self.ready or context.owner_epoch != self.owner_epoch:
            return "storage", None
        async with self._write_guard():
            state = self._load_state_locked(create=True)
            row = next(
                (
                    item
                    for item in state.get("collections", {}).values()
                    if isinstance(item, dict)
                    and not item.get("tombstone")
                    and item.get("owner_scope") == context.owner_scope
                    and item.get("content_hash") == content_hash
                ),
                None,
            )
            if row is None:
                return "not_found", None
            current_ids = [str(value) for value in row.get("category_ids", [])]
            if remove:
                remove_ids: set[str] = set()
                for name in categories:
                    category = self._find_category_locked(
                        state, context.owner_scope, name
                    )
                    if category is not None:
                        remove_ids.add(str(category["category_id"]))
                new_ids = [value for value in current_ids if value not in remove_ids]
                if not new_ids:
                    return "invalid", None
            else:
                new_ids = list(current_ids)
                for name in categories:
                    category = self._ensure_category_locked(
                        state,
                        scope=context.owner_scope,
                        name=self._normalise_category_name(name),
                        actor_id=context.actor_id,
                    )
                    category_id = str(category["category_id"])
                    if category_id not in new_ids:
                        new_ids.append(category_id)
            if new_ids == current_ids:
                return "same", self._legacy_entry_view_for_sticker_locked(
                    state, self._public_sticker_locked(state, row)
                )
            revision = self._bump_revision(
                state,
                "legacy_categories",
                sticker_id=str(row.get("sticker_id", "")),
            )
            row["category_ids"] = new_ids
            row["revision"] = revision
            row["updated_at"] = float(self.clock())
            if not self._commit_state_locked(state):
                return "storage", None
            return "updated", self._legacy_entry_view_for_sticker_locked(
                state, self._public_sticker_locked(state, row)
            )

    def stats(self) -> dict[str, Any]:
        try:
            with self._read_guard():
                state = self._load_state_locked()
                return {
                    "ready": self.ready,
                    "revision": _safe_int(state.get("revision"), 0),
                    "index_generation": _safe_int(
                        state.get("index_generation"), 0
                    ),
                    "collections": len(state.get("collections", {})),
                    "categories": len(state.get("categories", {})),
                    "assets": len(state.get("assets", {})),
                    "deliveries": len(state.get("deliveries", {})),
                    "index_writes": self._index_writes,
                    "rebuild_count": self._rebuild_count,
                    "collect_count": self._collect_count,
                }
        except Exception:
            return {
                "ready": self.ready,
                "revision": 0,
                "index_generation": 0,
                "collections": 0,
                "categories": 0,
                "assets": 0,
                "deliveries": 0,
            }


__all__ = [
    "StickerService",
    "StickerServiceError",
    "SecondWriterActive",
    "ServiceNotReady",
    "DEFAULT_CATEGORY",
    "LEGACY_SCOPE",
    "ASSET_CATEGORY",
]
