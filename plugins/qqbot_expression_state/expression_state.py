"""Deterministic mood/style state for the V6 expression layer.

No LLM calls.  The store only keeps bounded numeric state and emits a short,
non-instructional prompt block.  The model remains free to write its own reply.
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA_VERSION = 1
DIMENSIONS = (
    "warmth",
    "directness",
    "playfulness",
    "formality",
    "verbosity",
    "emoji",
    "curiosity",
    "assertiveness",
)
DIM_LABELS = {
    "warmth": "亲和",
    "directness": "直接",
    "playfulness": "俏皮",
    "formality": "正式",
    "verbosity": "话多",
    "emoji": "表情",
    "curiosity": "好奇",
    "assertiveness": "果断",
}
MAX_EVENT_DELTA = 0.15
HALF_LIFE_SECONDS = 45 * 60
STYLE_MIN_SAMPLES = 30
MAX_SCOPE = 200

_POS = re.compile(r"谢谢|感谢|开心|高兴|喜欢|不错|很好|棒|温暖|舒服|太好了|哈哈|赞|可爱|有用|帮到")
_NEG = re.compile(r"难过|生气|烦|讨厌|垃圾|糟糕|累|失望|不开心|难受|敷衍|无语|生气")
_EMOJI = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
_CATCH = re.compile(r"(.)\1{2,}")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _clean_scope(value: Any) -> str:
    text = str(value or "").strip()
    return text[:MAX_SCOPE]


def _clean_text(value: Any, limit: int = 500) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return text.strip()[:limit]


def _saturating(value: float) -> float:
    return clamp(value, -1.0, 1.0)


def emotion_label(valence: float, arousal: float) -> str:
    if valence >= 0.25:
        mood = "愉快"
    elif valence <= -0.25:
        mood = "低落"
    else:
        mood = "平稳"
    if arousal >= 0.6:
        energy = "活跃"
    elif arousal <= 0.2:
        energy = "安静"
    else:
        energy = "适中"
    return f"{mood}/{energy}"


def style_vector(text: str) -> dict[str, float]:
    """Deterministic 0..1 style observation; never uses an external model."""
    raw = _clean_text(text, 500)
    length = len(raw)
    exclam = raw.count("!") + raw.count("！")
    question = raw.count("?") + raw.count("？")
    emoji = len(_EMOJI.findall(raw))
    warm_hits = len(_POS.findall(raw))
    direct_hits = len(re.findall(r"直接|就|马上|确定|必须|应该", raw))
    playful_hits = len(re.findall(r"哈哈|嘿|喵|啦|呀|嘛|~|～", raw))
    formal_hits = len(re.findall(r"您|请|感谢|抱歉|请问|麻烦", raw))
    assertive_hits = len(re.findall(r"一定|必须|肯定|绝对|建议你|你应该", raw))
    catch = bool(_CATCH.search(raw))
    return {
        "warmth": clamp((warm_hits * 0.35) + (1 if "你" in raw or "您" in raw else 0) * 0.15, 0, 1),
        "directness": clamp(0.25 + direct_hits * 0.18 + exclam * 0.08, 0, 1),
        "playfulness": clamp(playful_hits * 0.16 + emoji * 0.08 + (0.2 if catch else 0), 0, 1),
        "formality": clamp(formal_hits * 0.25, 0, 1),
        "verbosity": clamp(length / 160.0, 0, 1),
        "emoji": clamp(emoji / 3.0, 0, 1),
        "curiosity": clamp(question * 0.28, 0, 1),
        "assertiveness": clamp(assertive_hits * 0.25 + exclam * 0.05, 0, 1),
    }


def _mean_vector(rows: Iterable[str]) -> dict[str, float] | None:
    vectors: list[dict[str, float]] = []
    for raw in rows:
        try:
            item = json.loads(raw)
            if isinstance(item, dict):
                vectors.append({dim: float(item.get(dim, 0.0)) for dim in DIMENSIONS})
        except Exception:
            continue
    if not vectors:
        return None
    return {dim: sum(v[dim] for v in vectors) / len(vectors) for dim in DIMENSIONS}


class ExpressionStore:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] | None = None,
        max_event_delta: float = MAX_EVENT_DELTA,
        half_life_seconds: float = HALF_LIFE_SECONDS,
        style_min_samples: int = STYLE_MIN_SAMPLES,
    ) -> None:
        self.path = Path(path)
        self.max_event_delta = clamp(max_event_delta, 0.01, MAX_EVENT_DELTA)
        self.half_life_seconds = max(60.0, float(half_life_seconds))
        self.style_min_samples = max(1, int(style_min_samples))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init()
        self._relax_permissions()

    def _relax_permissions(self) -> None:
        """Let the local Studio (different uid) manage this non-secret state DB."""
        try:
            os.chmod(self.path.parent, 0o777)
        except OSError:
            pass
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            try:
                if candidate.exists():
                    os.chmod(candidate, 0o666)
            except OSError:
                pass

    def _init(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS emotion (
                scope_key TEXT PRIMARY KEY,
                valence REAL NOT NULL DEFAULT 0,
                arousal REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS emotion_event (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                valence_delta REAL NOT NULL,
                arousal_delta REAL NOT NULL,
                reason TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS style_observation (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scope_key TEXT NOT NULL,
                vector TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS style_profile (
                scope_key TEXT PRIMARY KEY,
                samples INTEGER NOT NULL DEFAULT 0,
                vector TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS style_pin (
                scope_key TEXT NOT NULL,
                dimension TEXT NOT NULL,
                value REAL NOT NULL,
                PRIMARY KEY (scope_key, dimension)
            );
            CREATE TABLE IF NOT EXISTS style_optout (
                scope_key TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_emotion_event_scope ON emotion_event(scope_key, created_at);
            CREATE INDEX IF NOT EXISTS idx_style_obs_scope ON style_observation(scope_key, created_at);
            """
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    def _now(self) -> float:
        return float(self._clock())

    def _decay(self, scope: str) -> dict[str, float]:
        row = self._conn.execute(
            "SELECT valence, arousal, updated_at FROM emotion WHERE scope_key=?", (scope,)
        ).fetchone()
        if not row:
            return {"valence": 0.0, "arousal": 0.0}
        elapsed = max(0.0, self._now() - float(row["updated_at"]))
        factor = 0.5 ** (elapsed / self.half_life_seconds)
        valence = float(row["valence"]) * factor
        arousal = float(row["arousal"]) * factor
        return {"valence": valence, "arousal": arousal}

    def observe(self, scope: str, text: str, *, reason: str = "message") -> dict[str, Any]:
        key = _clean_scope(scope)
        if not key:
            raise ValueError("empty_scope")
        current = self._decay(key)
        raw = _clean_text(text, 500)
        positive = len(_POS.findall(raw))
        negative = len(_NEG.findall(raw))
        valence_delta = clamp((positive - negative) * 0.05, -self.max_event_delta, self.max_event_delta)
        arousal_delta = clamp(
            (raw.count("!") + raw.count("！")) * 0.025
            + (raw.count("?") + raw.count("？")) * 0.01
            + min(2, len(_EMOJI.findall(raw))) * 0.02,
            0,
            self.max_event_delta,
        )
        valence = _saturating(current["valence"] + valence_delta)
        arousal = _saturating(current["arousal"] + arousal_delta)
        now = self._now()
        self._conn.execute(
            """INSERT INTO emotion(scope_key, valence, arousal, updated_at) VALUES(?,?,?,?)
               ON CONFLICT(scope_key) DO UPDATE SET valence=excluded.valence,
               arousal=excluded.arousal, updated_at=excluded.updated_at""",
            (key, valence, arousal, now),
        )
        self._conn.execute(
            "INSERT INTO emotion_event(scope_key, valence_delta, arousal_delta, reason, created_at) VALUES(?,?,?,?,?)",
            (key, valence_delta, arousal_delta, _clean_text(reason, 32), now),
        )
        vector = style_vector(raw)
        self._conn.execute(
            "INSERT INTO style_observation(scope_key, vector, created_at) VALUES(?,?,?)",
            (key, json.dumps(vector, ensure_ascii=False), now),
        )
        count = int(
            self._conn.execute(
                "SELECT COUNT(*) FROM style_observation WHERE scope_key=?", (key,)
            ).fetchone()[0]
        )
        profile = None
        if count >= self.style_min_samples:
            rows = [
                row["vector"]
                for row in self._conn.execute(
                    "SELECT vector FROM style_observation WHERE scope_key=? ORDER BY id DESC LIMIT 200",
                    (key,),
                ).fetchall()
            ]
            profile = _mean_vector(rows)
            if profile:
                self._conn.execute(
                    """INSERT INTO style_profile(scope_key, samples, vector, updated_at) VALUES(?,?,?,?)
                       ON CONFLICT(scope_key) DO UPDATE SET samples=excluded.samples,
                       vector=excluded.vector, updated_at=excluded.updated_at""",
                    (key, count, json.dumps(profile, ensure_ascii=False), now),
                )
        self._conn.commit()
        return {
            "valence": round(valence, 4),
            "arousal": round(arousal, 4),
            "style_samples": count,
            "style": profile or {},
        }

    def snapshot(self, scope: str) -> dict[str, Any]:
        key = _clean_scope(scope)
        emotion = self._decay(key)
        row = self._conn.execute(
            "SELECT samples, vector FROM style_profile WHERE scope_key=?", (key,)
        ).fetchone()
        samples = int(row["samples"]) if row else 0
        if not samples:
            samples = int(
                self._conn.execute(
                    "SELECT COUNT(*) FROM style_observation WHERE scope_key=?", (key,)
                ).fetchone()[0]
            )
        profile: dict[str, float] = {}
        if row:
            try:
                profile = {dim: float(json.loads(row["vector"]).get(dim, 0.0)) for dim in DIMENSIONS}
            except Exception:
                profile = {}
        pins = {
            r["dimension"]: float(r["value"])
            for r in self._conn.execute(
                "SELECT dimension, value FROM style_pin WHERE scope_key=?", (key,)
            ).fetchall()
        }
        profile.update({dim: value for dim, value in pins.items() if dim in DIMENSIONS})
        opt = self._conn.execute(
            "SELECT enabled FROM style_optout WHERE scope_key=?", (key,)
        ).fetchone()
        return {
            "scope": key,
            "valence": round(emotion["valence"], 4),
            "arousal": round(emotion["arousal"], 4),
            "emotion": emotion_label(emotion["valence"], emotion["arousal"]),
            "style_samples": samples,
            "style": {dim: round(profile.get(dim, 0.0), 3) for dim in DIMENSIONS},
            "style_ready": samples >= self.style_min_samples or bool(pins),
            "optout": bool(opt and int(opt["enabled"])),
        }

    def context(self, scope: str, max_chars: int = 120) -> str:
        state = self.snapshot(scope)
        if state["optout"]:
            return ""
        parts = [f"情绪={state['emotion']}"]
        if state["style_ready"]:
            ranked = sorted(
                ((dim, value) for dim, value in state["style"].items() if value >= 0.55),
                key=lambda item: item[1],
                reverse=True,
            )
            hints = [DIM_LABELS[dim] for dim, _ in ranked[:2]]
            if hints:
                parts.append("风格=" + "/".join(hints))
        text = "表达状态（内部参考）：" + "；".join(parts) + "。"
        return text[: max(20, int(max_chars))]

    def set_optout(self, scope: str, enabled: bool) -> None:
        key = _clean_scope(scope)
        self._conn.execute(
            """INSERT INTO style_optout(scope_key, enabled, updated_at) VALUES(?,?,?)
               ON CONFLICT(scope_key) DO UPDATE SET enabled=excluded.enabled, updated_at=excluded.updated_at""",
            (key, 1 if enabled else 0, self._now()),
        )
        self._conn.commit()

    def pin(self, scope: str, dimension: str, value: float) -> None:
        key = _clean_scope(scope)
        dim = str(dimension or "")
        if dim not in DIMENSIONS:
            raise ValueError("unknown_dimension")
        self._conn.execute(
            """INSERT INTO style_pin(scope_key, dimension, value) VALUES(?,?,?)
               ON CONFLICT(scope_key, dimension) DO UPDATE SET value=excluded.value""",
            (key, dim, clamp(value, 0, 1)),
        )
        self._conn.commit()

    def reset(self, scope: str | None = None) -> None:
        key = _clean_scope(scope) if scope else ""
        if key:
            for table in ("emotion", "emotion_event", "style_observation", "style_profile", "style_pin", "style_optout"):
                self._conn.execute(f"DELETE FROM {table} WHERE scope_key=?", (key,))
        else:
            for table in ("emotion", "emotion_event", "style_observation", "style_profile", "style_pin", "style_optout"):
                self._conn.execute(f"DELETE FROM {table}")
        self._conn.commit()

    def export(self, scope: str | None = None) -> dict[str, Any]:
        key = _clean_scope(scope) if scope else ""
        where = " WHERE scope_key=?" if key else ""
        params = (key,) if key else ()
        out: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "scopes": {}}
        rows = self._conn.execute("SELECT * FROM emotion" + where, params).fetchall()
        for row in rows:
            out["scopes"].setdefault(row["scope_key"], {})["emotion"] = dict(row)
        rows = self._conn.execute("SELECT * FROM style_profile" + where, params).fetchall()
        for row in rows:
            out["scopes"].setdefault(row["scope_key"], {})["style"] = dict(row)
        return out
