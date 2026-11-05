"""Atomic draft / idempotency persistence for qqbot_food.

The JSON file lives in the plugin's AstrBot data directory and never contains
the sender QQ (the "password") or any credential.  Draft keys are SHA-256
digests of ``unified_msg_origin + sender_id`` so one sender in one session can
never see or consume another sender's draft.

Writes use a process lock + a best-effort OS file lock, a temporary file in the
same directory, ``fsync`` and ``os.replace``.  A crash can therefore leave
either the old file or the complete new file, never a half-written JSON file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

try:  # POSIX; the plugin also works without it (single-process fallback).
    import fcntl  # type: ignore
except Exception:  # pragma: no cover - non-POSIX runtimes
    fcntl = None  # type: ignore

FOOD_DRAFT_FIELDS = frozenset(
    {"restaurant", "location", "taste", "value", "comment", "tags"}
)
STATE_VERSION = 1


def state_key(umo: str, sender_id: str) -> str:
    """Return a non-reversible per-sender draft key."""

    raw = f"{str(umo or '')}\x00{str(sender_id or '')}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def draft_fingerprint(fields: dict[str, Any]) -> str:
    safe = {
        str(key): value
        for key, value in (fields or {}).items()
        if str(key) in FOOD_DRAFT_FIELDS
    }
    try:
        raw = json.dumps(
            safe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
    except (TypeError, ValueError):
        raw = str(sorted(safe.items()))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if not isinstance(fields, dict):
        return result
    for key in FOOD_DRAFT_FIELDS:
        if key not in fields:
            continue
        value = fields.get(key)
        if key == "tags":
            if isinstance(value, (list, tuple)):
                tags = [str(item).strip() for item in value if str(item).strip()]
                if tags:
                    result[key] = tags
        elif value not in (None, ""):
            result[key] = value
    return result


class FoodStateStore:
    """Small JSON-backed state machine for one plugin data directory."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        draft_ttl: int = 600,
        clock: Callable[[], float] | None = None,
        use_file_lock: bool = True,
    ):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.draft_ttl = max(1, int(draft_ttl))
        self._clock = clock or time.time
        self._mutex = threading.RLock()
        self._use_file_lock = bool(use_file_lock)

    # ---- low-level persistence -------------------------------------
    def _now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return time.time()

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        if not self._use_file_lock:
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            try:
                yield
            finally:
                if fcntl is not None:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                    except OSError:
                        pass

    @staticmethod
    def _float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": STATE_VERSION, "drafts": {}, "last_submit": {}}

    def _load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            return self._empty()
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        drafts = data.get("drafts")
        last_submit = data.get("last_submit")
        if not isinstance(drafts, dict):
            drafts = {}
        if not isinstance(last_submit, dict):
            last_submit = {}
        return {
            "version": STATE_VERSION,
            "drafts": drafts,
            "last_submit": last_submit,
        }

    def _save(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=".food-state-", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    state,
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(tmp_name, 0o600)
            except OSError:
                pass
            os.replace(tmp_name, self.path)
            try:
                dir_fd = os.open(str(self.path.parent), os.O_DIRECTORY)
            except (AttributeError, OSError):
                dir_fd = None
            if dir_fd is not None:
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
            except OSError:
                pass

    def _prune(self, state: dict[str, Any], now: float) -> bool:
        changed = False
        drafts = state.get("drafts", {})
        if isinstance(drafts, dict):
            for key, draft in list(drafts.items()):
                if not isinstance(draft, dict):
                    drafts.pop(key, None)
                    changed = True
                    continue
                try:
                    updated = float(draft.get("updated", 0.0))
                except (TypeError, ValueError):
                    updated = 0.0
                status = str(draft.get("status", "") or "")
                limit = self.draft_ttl
                if status == "submitted":
                    limit = max(limit, 3600)
                if now - updated > limit:
                    drafts.pop(key, None)
                    changed = True
        last_submit = state.get("last_submit", {})
        if isinstance(last_submit, dict) and len(last_submit) > 256:
            def _timestamp(pair):
                try:
                    return float(pair[1] or 0)
                except (TypeError, ValueError):
                    return 0.0

            ordered = sorted(
                last_submit.items(),
                key=_timestamp,
                reverse=True,
            )[:256]
            state["last_submit"] = dict(ordered)
            changed = True
        return changed

    def _transact(
        self,
        operation: Callable[[dict[str, Any], float], Any],
        *,
        write: bool = True,
    ) -> Any:
        with self._mutex:
            with self._file_lock():
                state = self._load()
                now = self._now()
                pruned = self._prune(state, now)
                result = operation(state, now)
                if write or pruned:
                    self._save(state)
                return result

    # ---- drafts -----------------------------------------------------
    def get_draft(self, key: str) -> dict[str, Any] | None:
        if not key:
            return None

        def read(state: dict[str, Any], _now: float):
            draft = state["drafts"].get(key)
            if not isinstance(draft, dict):
                return None
            return {
                "fields": _clean_fields(draft.get("fields", {})),
                "status": str(draft.get("status", "pending_confirmation") or "pending_confirmation"),
                "created": self._float(draft.get("created", 0.0)),
                "updated": self._float(draft.get("updated", 0.0)),
                "submission_id": str(draft.get("submission_id", "") or ""),
                "duplicate": bool(draft.get("duplicate", False)),
            }

        return self._transact(read, write=False)

    def save_draft(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        status: str = "pending_confirmation",
    ) -> dict[str, Any] | None:
        if not key:
            return None
        clean = _clean_fields(fields)
        if not clean:
            return None

        def write(state: dict[str, Any], now: float):
            prior = state["drafts"].get(key)
            created = now
            submission_id = ""
            duplicate = False
            if isinstance(prior, dict):
                try:
                    created = float(prior.get("created", now) or now)
                except (TypeError, ValueError):
                    created = now
                submission_id = str(prior.get("submission_id", "") or "")
                duplicate = bool(prior.get("duplicate", False))
            state["drafts"][key] = {
                "fields": clean,
                "status": str(status or "pending_confirmation"),
                "created": created,
                "updated": now,
                "submission_id": submission_id,
                "duplicate": duplicate,
            }
            return {
                "fields": dict(clean),
                "status": str(status or "pending_confirmation"),
                "created": created,
                "updated": now,
                "submission_id": submission_id,
                "duplicate": duplicate,
            }

        return self._transact(write)

    def merge_draft(
        self,
        key: str,
        fields: dict[str, Any],
        *,
        status: str = "pending_confirmation",
    ) -> dict[str, Any] | None:
        """Merge new marker fields into a pending draft.

        A different restaurant name resets the old draft (the legacy behavior)
        so two unrelated reviews cannot be mixed together.
        """

        if not key:
            return None
        clean = _clean_fields(fields)
        if not clean:
            return None

        def write(state: dict[str, Any], now: float):
            prior = state["drafts"].get(key)
            existing = (
                _clean_fields(prior.get("fields", {}))
                if isinstance(prior, dict)
                else {}
            )
            if (
                clean.get("restaurant")
                and existing.get("restaurant")
                and clean.get("restaurant") != existing.get("restaurant")
            ):
                existing = {}
            if isinstance(prior, dict) and str(prior.get("status", "")) == "submitted":
                # A genuinely different new review may start a new draft.
                if draft_fingerprint(existing) == draft_fingerprint(clean):
                    return self._public(prior)
                existing = {}
            merged = dict(existing)
            merged.update(clean)
            created = now
            if isinstance(prior, dict):
                try:
                    created = float(prior.get("created", now) or now)
                except (TypeError, ValueError):
                    created = now
            state["drafts"][key] = {
                "fields": merged,
                "status": str(status or "pending_confirmation"),
                "created": created,
                "updated": now,
                "submission_id": "",
                "duplicate": False,
            }
            return self._public(state["drafts"][key])

        return self._transact(write)

    @staticmethod
    def _public(draft: dict[str, Any]) -> dict[str, Any]:
        return {
            "fields": _clean_fields(draft.get("fields", {})),
            "status": str(draft.get("status", "") or ""),
            "created": FoodStateStore._float(draft.get("created", 0.0)),
            "updated": FoodStateStore._float(draft.get("updated", 0.0)),
            "submission_id": str(draft.get("submission_id", "") or ""),
            "duplicate": bool(draft.get("duplicate", False)),
        }

    # ---- submission state machine -----------------------------------
    def claim_submit(self, key: str) -> str:
        """Atomically claim a pending draft.

        Returns ``claimed``, ``submitted``, ``submitting`` or ``missing``.
        A repeated confirmation can therefore never start a second POST after
        a successful one, and a crash while in-flight also fails closed.
        """

        if not key:
            return "missing"

        def write(state: dict[str, Any], now: float):
            draft = state["drafts"].get(key)
            if not isinstance(draft, dict):
                return "missing"
            status = str(draft.get("status", "") or "")
            if status == "submitted":
                return "submitted"
            if status == "submitting":
                return "submitting"
            draft["status"] = "submitting"
            draft["updated"] = now
            draft["submission_id"] = uuid.uuid4().hex
            return "claimed"

        return self._transact(write)

    def restore_pending(self, key: str) -> None:
        if not key:
            return

        def write(state: dict[str, Any], now: float):
            draft = state["drafts"].get(key)
            if isinstance(draft, dict) and str(draft.get("status", "")) == "submitting":
                draft["status"] = "pending_confirmation"
                draft["updated"] = now
                draft["submission_id"] = ""
            return None

        self._transact(write)

    def mark_submitted(self, key: str, *, duplicate: bool = False) -> None:
        if not key:
            return

        def write(state: dict[str, Any], now: float):
            draft = state["drafts"].get(key)
            if isinstance(draft, dict):
                draft["status"] = "submitted"
                draft["duplicate"] = bool(duplicate)
                draft["updated"] = now
            return None

        self._transact(write)

    def record_submit_attempt(self, key: str) -> None:
        if not key:
            return

        def write(state: dict[str, Any], now: float):
            state["last_submit"][key] = now
            return None

        self._transact(write)

    def cooldown_remaining(self, key: str, seconds: int) -> int:
        if not key or int(seconds) <= 0:
            return 0

        def read(state: dict[str, Any], now: float):
            try:
                last = float(state["last_submit"].get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                last = 0.0
            remaining = float(seconds) - (now - last)
            if remaining <= 0:
                return 0
            return max(1, int(math.ceil(remaining)))

        return int(self._transact(read, write=False) or 0)

    def delete_draft(self, key: str) -> None:
        if not key:
            return

        def write(state: dict[str, Any], _now: float):
            state["drafts"].pop(key, None)
            return None

        self._transact(write)

    def snapshot(self) -> dict[str, Any]:
        def read(state: dict[str, Any], _now: float):
            try:
                return json.loads(json.dumps(state, ensure_ascii=False))
            except (TypeError, ValueError):
                return {"version": STATE_VERSION, "drafts": {}, "last_submit": {}}

        return self._transact(read, write=False)
