"""Small, dependency-free rolling buffers for memory extraction.

Both buffers live in a JSON file so a container restart does not throw away
the turns a group already produced (previously every restart reset the
counters that gate extraction, which made quiet groups never remember
anything).
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

BUFFER_CAP = 200


class BufferStore:
    def __init__(self, path: Path | str, *, cap: int = BUFFER_CAP) -> None:
        self.path = Path(path)
        self.cap = max(16, int(cap))
        self.turns: dict[str, list[dict[str, str]]] = {}
        self.digests: dict[str, list[str]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    # -- persistence ---------------------------------------------------
    def load(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self._loaded = True
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return
            if not isinstance(raw, dict):
                return
            turns = raw.get("turns")
            if isinstance(turns, dict):
                for scope, rows in turns.items():
                    if not isinstance(rows, list):
                        continue
                    clean: list[dict[str, str]] = []
                    for row in rows[: self.cap]:
                        if not isinstance(row, dict):
                            continue
                        clean.append(
                            {
                                "h": str(row.get("h") or ""),
                                "l": str(row.get("l") or ""),
                                "t": str(row.get("t") or "")[:500],
                            }
                        )
                    if clean:
                        self.turns[str(scope)] = clean
            digests = raw.get("digests")
            if isinstance(digests, dict):
                for umo, rows in digests.items():
                    if isinstance(rows, list):
                        clean_lines = [str(item)[:220] for item in rows[-self.cap :]]
                        if clean_lines:
                            self.digests[str(umo)] = clean_lines

    def save(self) -> None:
        with self._lock:
            payload: dict[str, Any] = {"turns": self.turns, "digests": self.digests}
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                os.replace(tmp, self.path)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass
            except OSError:
                return

    # -- turn buffer ---------------------------------------------------
    def add_turn(
        self,
        scope: str,
        *,
        actor_hash: str,
        actor_label: str,
        text: str,
        every: int,
    ) -> list[tuple[str, str, list[str]]]:
        """Buffer one message; when the scope fills up, return per-actor batches.

        The threshold counts *all* messages in the group rather than messages
        from a single sender: in a busy group a quiet bot still extracts facts,
        and in a small group four different people finally add up to a batch.
        """

        every = max(2, min(20, int(every)))
        self.load()
        buffer = self.turns.setdefault(str(scope), [])
        buffer.append(
            {"h": str(actor_hash or ""), "l": str(actor_label or ""), "t": str(text or "")[:500]}
        )
        del buffer[: -self.cap]
        if len(buffer) < every:
            self.save()
            return []
        batch = list(buffer)
        self.turns[str(scope)] = []
        self.save()
        grouped: dict[tuple[str, str], list[str]] = {}
        for row in batch:
            key = (row.get("h") or "", row.get("l") or "")
            grouped.setdefault(key, []).append(row.get("t") or "")
        return [(h, label, texts[-12:]) for (h, label), texts in grouped.items()]

    # -- digest buffer -------------------------------------------------
    def add_digest_line(self, umo: str, line: str, *, every: int) -> list[str]:
        every = max(2, min(200, int(every)))
        self.load()
        buffer = self.digests.setdefault(str(umo), [])
        buffer.append(str(line or "")[:220])
        del buffer[:-every]
        if len(buffer) < every:
            self.save()
            return []
        batch = list(buffer)
        self.digests[str(umo)] = []
        self.save()
        return batch
