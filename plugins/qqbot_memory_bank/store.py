"""Small, deterministic knowledge memory store.

This is deliberately separate from ``qqbot_memory_commands`` cards and from
``persona_corpus``: entries are retrieved only when the model calls a tool.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

_SPACE_RE = re.compile(r"\s+")
_NOISE_RE = re.compile(r"\[\[NO_REPLY\]\]|mqqapi://|https?://|ComponentType\.")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[A-Za-z0-9_\-]{2,}")


def _normalise(text: str) -> str:
    return _SPACE_RE.sub(" ", str(text or "")).strip()


def _hash(scope: str, persona_id: str, actor_hash: str, content: str) -> str:
    raw = f"{scope}\n{persona_id}\n{actor_hash}\n{_normalise(content).casefold()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _tokens(text: str) -> set[str]:
    value = str(text or "").casefold()
    out = {w for w in _WORD_RE.findall(value)}
    for run in _CJK_RE.findall(value):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i : i + 2] for i in range(len(run) - 1))
    return {t for t in out if t}


def clean_fact(text: str) -> str:
    value = _normalise(text)
    if any(marker in value for marker in ("[[NO_REPLY]]", "mqqapi://", "ComponentType.")):
        return ""
    value = re.sub(r"!?\[[^\]]*\]\([^)]*\)", "", value)
    value = _SPACE_RE.sub(" ", value).strip(" ，,。；;")
    if len(value) < 4 or len(value) > 200:
        return ""
    return value


def heuristic_facts(text: str) -> list[str]:
    """Conservative fallback used when the auxiliary model is unavailable."""
    markers = re.compile(
        r"我叫|我的名字|我喜欢|我不喜欢|我讨厌|我爱|我住在|我养|我每天|我通常|"
        r"我习惯|记住|以后|下次|答应|计划|想要|希望|生日|过敏|工作|上班|上学"
    )
    out: list[str] = []
    for sentence in re.split(r"[。！？!?；;\n]+", str(text or "")):
        candidate = clean_fact(sentence)
        if not candidate or candidate.endswith(("?", "？")):
            continue
        if markers.search(candidate):
            candidate = re.sub(r"^(?:麻衣|衣衣|电子羊)[，,]\s*", "", candidate)
            candidate = re.sub(r"^记住[:：]?\s*", "", candidate)
            candidate = re.sub(r"^我", "用户", candidate)
            candidate = normalise_fact_subject(candidate)
            if candidate and candidate not in out:
                out.append(candidate)
        if len(out) >= 5:
            break
    return out


def normalise_fact_subject(text: str) -> str:
    """Make extracted facts stable third-person statements.

    The auxiliary prompt asks for facts about the user, but models often
    return a predicate-only phrase.  Prefixing a subject also lets the
    exact-dedupe index merge the auxiliary copy with a model-written copy.
    """
    fact = clean_fact(text)
    if not fact:
        return ""
    if not re.match(r"^(?:用户|我|他|她|对方|群友|我们|大家)", fact):
        fact = "用户" + fact
    return fact


class KnowledgeStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._init()
        self._relax_permissions()

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_entry (
              id TEXT PRIMARY KEY,
              scope TEXT NOT NULL,
              persona_id TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'manual',
              confidence REAL NOT NULL DEFAULT 1.0,
              content_hash TEXT NOT NULL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              actor_hash TEXT NOT NULL DEFAULT '',
              actor_label TEXT NOT NULL DEFAULT '',
              entry_kind TEXT NOT NULL DEFAULT 'curated'
            );
            CREATE INDEX IF NOT EXISTS knowledge_scope
              ON knowledge_entry(scope, persona_id, actor_hash, updated_at);
            """
        )
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(knowledge_entry)").fetchall()
        }
        for name, ddl in (
            ("actor_hash", "TEXT NOT NULL DEFAULT ''"),
            ("actor_label", "TEXT NOT NULL DEFAULT ''"),
            ("entry_kind", "TEXT NOT NULL DEFAULT 'legacy'"),
        ):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE knowledge_entry ADD COLUMN {name} {ddl}")
        self._conn.execute("DROP INDEX IF EXISTS knowledge_hash")
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS knowledge_hash "
            "ON knowledge_entry(scope, persona_id, actor_hash, content_hash)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS knowledge_kind "
            "ON knowledge_entry(scope, persona_id, entry_kind, updated_at)"
        )
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS knowledge_doc (
              id TEXT PRIMARY KEY,
              scope TEXT NOT NULL,
              persona_id TEXT NOT NULL DEFAULT '',
              title TEXT NOT NULL DEFAULT '',
              content TEXT NOT NULL,
              tags TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT 'import',
              content_hash TEXT NOT NULL,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS knowledge_doc_hash
              ON knowledge_doc(scope, persona_id, content_hash);
            CREATE INDEX IF NOT EXISTS knowledge_doc_scope
              ON knowledge_doc(scope, persona_id, updated_at);
            """
        )
        self._migrate_curated_docs()
        self._conn.commit()

    def _migrate_curated_docs(self) -> None:
        """Move legacy operator-curated entries out of the AI fact table."""
        try:
            rows = self._conn.execute(
                "SELECT id, scope, persona_id, content, tags, source, content_hash, created_at, updated_at "
                "FROM knowledge_entry WHERE entry_kind='curated'"
            ).fetchall()
        except sqlite3.DatabaseError:
            return
        for row in rows:
            digest = str(row["content_hash"] or "")
            if not digest:
                digest = hashlib.sha256(
                    f"{row['scope']}\n{row['persona_id']}\n{str(row['content']).casefold()}".encode("utf-8")
                ).hexdigest()[:32]
            self._conn.execute(
                "INSERT OR IGNORE INTO knowledge_doc(id, scope, persona_id, title, content, tags, source, content_hash, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "doc-" + digest[:20], str(row["scope"]), str(row["persona_id"] or ""), "",
                    str(row["content"]), str(row["tags"] or ""), "import", digest,
                    float(row["created_at"] or time.time()), float(row["updated_at"] or time.time()),
                ),
            )
            self._conn.execute("DELETE FROM knowledge_entry WHERE id=?", (row["id"],))

    def _relax_permissions(self) -> None:
        try:
            os.chmod(self.path.parent, 0o777)
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path) + suffix)
                if p.exists():
                    os.chmod(p, 0o666)
        except OSError:
            pass

    def add(
        self,
        *,
        scope: str,
        content: str,
        tags: str = "",
        source: str = "manual",
        confidence: float = 1.0,
        persona_id: str = "",
        actor_hash: str = "",
        actor_label: str = "",
        entry_kind: str = "curated",
        max_entries: int = 2000,
    ) -> dict[str, Any]:
        fact = clean_fact(content)
        if not fact:
            return {"ok": False, "reason": "empty_or_noisy"}
        scope = str(scope or "")[:200]
        persona_id = str(persona_id or "")[:128]
        actor_hash = str(actor_hash or "")[:64]
        actor_label = str(actor_label or "")[:80]
        entry_kind = str(entry_kind or "curated")[:24]
        if not scope:
            return {"ok": False, "reason": "no_scope"}
        now = time.time()
        digest = _hash(scope, persona_id, actor_hash, fact)
        row = self._conn.execute(
            "SELECT id FROM knowledge_entry WHERE scope=? AND persona_id=? AND actor_hash=? AND content_hash=?",
            (scope, persona_id, actor_hash, digest),
        ).fetchone()
        if row:
            self._conn.execute(
                "UPDATE knowledge_entry SET updated_at=?, confidence=MAX(confidence, ?), tags=?, actor_label=?, entry_kind=? WHERE id=?",
                (now, float(confidence), str(tags or "")[:160], actor_label, entry_kind, row["id"]),
            )
            self._conn.commit()
            return {"ok": True, "id": row["id"], "duplicate": True, "content": fact}
        count = self._conn.execute(
            "SELECT COUNT(*) FROM knowledge_entry WHERE scope=? AND persona_id=? AND actor_hash=?",
            (scope, persona_id, actor_hash),
        ).fetchone()[0]
        if int(count) >= max(1, int(max_entries)):
            return {"ok": False, "reason": "bank_full"}
        entry_id = "kb-" + digest[:20]
        self._conn.execute(
            "INSERT INTO knowledge_entry(id, scope, persona_id, content, tags, source, confidence, content_hash, created_at, updated_at, actor_hash, actor_label, entry_kind) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id, scope, persona_id, fact, str(tags or "")[:160], str(source or "manual")[:24],
                float(confidence), digest, now, now, actor_hash, actor_label, entry_kind,
            ),
        )
        self._conn.commit()
        return {"ok": True, "id": entry_id, "duplicate": False, "content": fact}

    def search(
        self,
        *,
        scope: str,
        query: str,
        persona_id: str = "",
        actor_hash: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        scope = str(scope or "")[:200]
        if not scope:
            return []
        rows = self._conn.execute(
            "SELECT * FROM knowledge_entry WHERE scope=? AND (persona_id=? OR persona_id='') "
            "AND (actor_hash=? OR actor_hash='') ORDER BY updated_at DESC LIMIT 300",
            (scope, str(persona_id or "")[:128], str(actor_hash or "")[:64]),
        ).fetchall()
        terms = _tokens(query)
        if not terms:
            return [dict(r) for r in rows[: max(1, min(20, int(limit)))]]
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            hay = (item["content"] + " " + item["tags"]).casefold()
            score = sum(1 for term in terms if term in hay)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -float(pair[1].get("updated_at") or 0)))
        return [item for _, item in scored[: max(1, min(20, int(limit)))]]

    def forget(self, *, scope: str, entry_id: str, persona_id: str = "", actor_hash: str = "") -> bool:
        cur = self._conn.execute(
            "DELETE FROM knowledge_entry WHERE id=? AND scope=? AND persona_id=? AND actor_hash=? AND entry_kind!='curated'",
            (str(entry_id or ""), str(scope or ""), str(persona_id or ""), str(actor_hash or "")[:64]),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def stats(self, *, scope: str, persona_id: str = "", actor_hash: str = "") -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total FROM knowledge_entry WHERE scope=? AND (persona_id=? OR persona_id='') "
            "AND (actor_hash=? OR actor_hash='')",
            (str(scope or ""), str(persona_id or ""), str(actor_hash or "")[:64]),
        ).fetchone()
        return {"scope": scope, "total": int(row["total"] if row else 0)}

    # ------------------------------------------------------------------
    # User-imported knowledge documents.  These are deliberately a separate
    # table from AI-extracted facts: the model can only read them through
    # knowledge_search, never auto-write them.
    # ------------------------------------------------------------------
    def add_doc(
        self,
        *,
        scope: str,
        content: str,
        title: str = "",
        tags: str = "",
        persona_id: str = "",
        source: str = "import",
        max_entries: int = 2000,
    ) -> dict[str, Any]:
        scope = str(scope or "").strip()[:200]
        persona_id = str(persona_id or "").strip()[:128]
        title = _normalise(title)[:120]
        content = _normalise(content)
        tags = _normalise(tags)[:160]
        if not scope:
            return {"ok": False, "reason": "no_scope"}
        if len(content) < 2:
            return {"ok": False, "reason": "empty"}
        if len(content) > 4000:
            return {"ok": False, "reason": "too_long"}
        digest = hashlib.sha256(
            f"{scope}\n{persona_id}\n{content.casefold()}".encode("utf-8")
        ).hexdigest()[:32]
        row = self._conn.execute(
            "SELECT id FROM knowledge_doc WHERE scope=? AND persona_id=? AND content_hash=?",
            (scope, persona_id, digest),
        ).fetchone()
        now = time.time()
        if row:
            self._conn.execute(
                "UPDATE knowledge_doc SET title=?, tags=?, source=?, updated_at=? WHERE id=?",
                (title, tags, str(source or "import")[:24], now, row["id"]),
            )
            self._conn.commit()
            return {"ok": True, "id": row["id"], "duplicate": True, "content": content}
        count = self._conn.execute(
            "SELECT COUNT(*) FROM knowledge_doc WHERE scope=? AND persona_id=?",
            (scope, persona_id),
        ).fetchone()[0]
        if int(count) >= max(1, int(max_entries)):
            return {"ok": False, "reason": "bank_full"}
        doc_id = "doc-" + digest[:20]
        self._conn.execute(
            "INSERT INTO knowledge_doc(id, scope, persona_id, title, content, tags, source, content_hash, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (doc_id, scope, persona_id, title, content, tags, str(source or "import")[:24], digest, now, now),
        )
        self._conn.commit()
        return {"ok": True, "id": doc_id, "duplicate": False, "content": content}

    def search_docs(
        self,
        *,
        scope: str,
        query: str,
        persona_id: str = "",
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        scope = str(scope or "")[:200]
        if not scope:
            return []
        rows = self._conn.execute(
            "SELECT * FROM knowledge_doc WHERE (scope=? OR scope='global') "
            "AND (persona_id=? OR persona_id='') ORDER BY updated_at DESC LIMIT 500",
            (scope, str(persona_id or "")[:128]),
        ).fetchall()
        terms = _tokens(query)
        if not terms:
            return [dict(r) for r in rows[: max(1, min(20, int(limit)))]]
        scored: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            item = dict(row)
            hay = (item["title"] + " " + item["content"] + " " + item["tags"]).casefold()
            score = sum(1 for term in terms if term in hay)
            if score:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], -float(pair[1].get("updated_at") or 0)))
        return [item for _, item in scored[: max(1, min(20, int(limit)))]]

    def list_docs(self, *, scope: str = "", persona_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        scope = str(scope or "")[:200]
        persona_id = str(persona_id or "")[:128]
        where: list[str] = []
        args: list[Any] = []
        if scope:
            where.append("scope=?")
            args.append(scope)
        if persona_id:
            where.append("persona_id=?")
            args.append(persona_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        rows = self._conn.execute(
            "SELECT * FROM knowledge_doc" + clause + " ORDER BY updated_at DESC LIMIT ?",
            (*args, max(1, min(500, int(limit)))),
        ).fetchall()
        return [dict(r) for r in rows]

    def doc_scopes(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT scope, persona_id, COUNT(*) AS total FROM knowledge_doc "
            "GROUP BY scope, persona_id ORDER BY MAX(updated_at) DESC LIMIT 100"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_doc(self, *, entry_id: str, scope: str = "", persona_id: str = "") -> bool:
        args: list[Any] = [str(entry_id or "")]
        sql = "DELETE FROM knowledge_doc WHERE id=?"
        if scope:
            sql += " AND scope=?"
            args.append(str(scope))
        if persona_id:
            sql += " AND persona_id=?"
            args.append(str(persona_id))
        cur = self._conn.execute(sql, tuple(args))
        self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self._conn.close()
