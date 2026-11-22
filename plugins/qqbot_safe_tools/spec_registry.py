"""Schema-derived parameter specs for the capability policy.

``capability_policy.PLUGIN_PARAM_SPECS`` stays authoritative and hand-written.
This module only *adds* the scalar parameters a plugin actually declares in its
own ``_conf_schema.json`` next to ``main.py``, so the Studio parameter editor can
offer per-group overrides for real config keys instead of a 16-entry allowlist.

Rules (framework §15):

* scalar schema types (``bool``/``int``/``float``/``string``/``text``) become specs;
  ``list``/``object``/``dict`` are reported as unsupported (v2);
* hand-written specs win: a generated key is dropped when a manual spec already
  uses that policy name or that ``config_key``;
* credential-looking keys stay ``sensitive`` (never written to the policy store);
* machine-managed anchors (``*_owner_epoch``, ``*_version``) stay
  ``system_managed`` and are never group-overridable.

Nothing here raises on a broken schema: a missing or malformed file simply
yields no generated specs (callers fall back to the hand-written table).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

SCHEMA_FILE = "_conf_schema.json"
OVERLAY_FILE = "policy_overlay.py"
PLUGIN_PREFIX = "qqbot_"

#: schema ``type`` -> policy ``ParamSpec.kind``
SCALAR_TYPES: dict[str, str] = {
    "bool": "bool",
    "boolean": "bool",
    "int": "int",
    "integer": "int",
    "float": "float",
    "number": "float",
    "string": "str",
    "text": "text",
}

#: declared, but deliberately not overridable in v1
COMPLEX_TYPES = frozenset({"list", "object", "dict", "file", "json"})

CREDENTIAL_RE = re.compile(
    r"(api_?key|access_?key|secret|token|password|passwd|cookie|appid|app_?id"
    r"|credential|private_key|client_secret)",
    re.IGNORECASE,
)
SYSTEM_MANAGED_KEYS = frozenset(
    {"schema_version", "abi_version", "registry_abi_version", "index_schema_version"}
)
SYSTEM_MANAGED_SUFFIXES = ("owner_epoch",)


@dataclass(frozen=True, slots=True)
class GeneratedSpec:
    """One parameter derived from a plugin's ``_conf_schema.json``."""

    key: str
    kind: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    max_chars: int | None = None
    enum: tuple[str, ...] = ()
    default: Any = None
    config_key: str = ""
    sensitive: bool = False
    system_managed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "max_chars": self.max_chars,
            "enum": list(self.enum),
            "default": self.default,
            "config_key": self.config_key or self.key,
            "sensitive": bool(self.sensitive),
            "system_managed": bool(self.system_managed),
            "group_overridable": not (self.sensitive or self.system_managed),
            "source": "schema",
        }


def plugins_root() -> Path:
    """Directory that holds every installed plugin (siblings of safe_tools)."""
    return Path(__file__).resolve().parent.parent


def is_system_managed(key: str) -> bool:
    text = str(key or "")
    return text in SYSTEM_MANAGED_KEYS or text.endswith(SYSTEM_MANAGED_SUFFIXES)


def is_credential(key: str) -> bool:
    return bool(CREDENTIAL_RE.search(str(key or "")))


def _plugin_dir(plugin_id: str, root: Path | None = None) -> Path | None:
    text = str(plugin_id or "").strip()
    if not text or "/" in text or "\\" in text or text in {".", ".."}:
        return None
    if not text.startswith(PLUGIN_PREFIX):
        return None
    base = Path(root) if root is not None else plugins_root()
    candidate = base / text
    return candidate if candidate.is_dir() else None


def schema_path(plugin_id: str, root: Path | None = None) -> Path | None:
    directory = _plugin_dir(plugin_id, root)
    if directory is None:
        return None
    path = directory / SCHEMA_FILE
    return path if path.is_file() else None


def overlay_supported(plugin_id: str, root: Path | None = None) -> bool:
    directory = _plugin_dir(plugin_id, root)
    return bool(directory and (directory / OVERLAY_FILE).is_file())


def known_plugin_ids(root: Path | None = None) -> tuple[str, ...]:
    """Every installed plugin directory that carries a schema or an overlay."""
    base = Path(root) if root is not None else plugins_root()
    try:
        children = sorted(
            child
            for child in base.iterdir()
            if child.is_dir() and child.name.startswith(PLUGIN_PREFIX)
        )
    except OSError:
        return ()
    return tuple(
        child.name
        for child in children
        if (child / SCHEMA_FILE).is_file() or (child / OVERLAY_FILE).is_file()
    )


def _coerce_bound(value: Any, kind: str) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if kind == "int" else float(value)


def _enum_from(options: Any) -> tuple[str, ...]:
    if isinstance(options, Mapping):
        return tuple(str(key) for key in options)
    if isinstance(options, (list, tuple)):
        return tuple(str(item) for item in options if isinstance(item, (str, int, float)))
    return ()


def _spec_from_entry(key: str, raw: Mapping[str, Any]) -> GeneratedSpec | None:
    declared = str(raw.get("type") or "").strip().lower()
    kind = SCALAR_TYPES.get(declared)
    if kind is None:
        return None
    minimum = maximum = None
    slider = raw.get("slider")
    if kind in {"int", "float"} and isinstance(slider, Mapping):
        minimum = _coerce_bound(slider.get("min"), kind)
        maximum = _coerce_bound(slider.get("max"), kind)
    max_chars = raw.get("max_chars")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        max_chars = None
    return GeneratedSpec(
        key=key,
        kind=kind,
        minimum=minimum,
        maximum=maximum,
        max_chars=max_chars,
        enum=_enum_from(raw.get("options")),
        default=raw.get("default"),
        config_key=key,
        sensitive=is_credential(key),
        system_managed=is_system_managed(key),
    )


_SPEC_CACHE: dict[tuple[str, str], tuple[float, dict[str, GeneratedSpec]]] = {}


def generate_specs(
    plugin_id: str, root: Path | None = None
) -> dict[str, GeneratedSpec]:
    """Scalar specs declared by ``plugin_id``'s schema (cached by mtime)."""
    path = schema_path(plugin_id, root)
    if path is None:
        return {}
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    cache_key = (plugin_id, str(path))
    cached = _SPEC_CACHE.get(cache_key)
    if cached is not None and cached[0] == stamp:
        return dict(cached[1])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    specs: dict[str, GeneratedSpec] = {}
    if isinstance(raw, Mapping):
        for key, entry in raw.items():
            if not isinstance(entry, Mapping):
                continue
            name = str(key).strip()
            if not name:
                continue
            spec = _spec_from_entry(name, entry)
            if spec is not None:
                specs[name] = spec
    _SPEC_CACHE[cache_key] = (stamp, specs)
    return dict(specs)


def unsupported_keys(plugin_id: str, root: Path | None = None) -> dict[str, str]:
    """Declared keys that v1 cannot override, with their schema type."""
    path = schema_path(plugin_id, root)
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, entry in raw.items():
        if isinstance(entry, Mapping):
            declared = str(entry.get("type") or "").strip().lower()
            if declared in COMPLEX_TYPES:
                out[str(key)] = declared
    return out


def digest(plugin_id: str, root: Path | None = None) -> str:
    """Stable short digest of the schema file (for ETag / cache busting)."""
    path = schema_path(plugin_id, root)
    if path is None:
        return ""
    try:
        payload = path.read_bytes()
    except OSError:
        return ""
    return hashlib.sha256(payload).hexdigest()[:16]


def registry_digest(root: Path | None = None) -> str:
    """Digest over every installed schema, so the Studio can cache per registry."""
    base = Path(root) if root is not None else plugins_root()
    parts: list[str] = []
    for plugin_id in known_plugin_ids(base):
        parts.append(f"{plugin_id}:{digest(plugin_id, base)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def clear_cache() -> None:
    """Test hook: forget every cached schema."""
    _SPEC_CACHE.clear()
