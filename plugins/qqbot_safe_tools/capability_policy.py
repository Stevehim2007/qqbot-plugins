"""Per-group capability policy with one writer and atomic persistence.

The policy file is a sidecar owned by ``qqbot_safe_tools``.  Other plugins read
an effective snapshot; they never write the file.  The format is intentionally
strict: unknown plugins, unknown parameters, sensitive parameters and wrong
types are rejected instead of being ignored.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 2
MAX_AUDIT_ENTRIES = 500


class PolicyError(ValueError):
    """Stable policy failure with a machine-readable code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = str(code)
        self.message = str(message or code)
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class ParamSpec:
    kind: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    max_chars: int | None = None
    enum: tuple[str, ...] = ()
    group_overridable: bool = True
    sensitive: bool = False


@dataclass(frozen=True, slots=True)
class EffectivePolicy:
    plugin_id: str
    enabled: bool
    params: Mapping[str, Any]
    sources: tuple[str, ...]
    revision: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "enabled": bool(self.enabled),
            "params": dict(self.params),
            "sources": list(self.sources),
            "revision": int(self.revision),
        }


def _p(kind: str, **kwargs: Any) -> ParamSpec:
    return ParamSpec(kind=kind, **kwargs)


# The registry is deliberately explicit.  A missing schema must not silently
# make a parameter writable.
PLUGIN_PARAM_SPECS: dict[str, dict[str, ParamSpec]] = {
    "qqbot_core_reply": {
        "participation.reply_probability": _p("float", minimum=0.0, maximum=1.0),
        "participation.timezone": _p("str", max_chars=64),
        "context.max_chars": _p("int", minimum=500, maximum=8000),
        "persona.profile_id": _p("str", max_chars=64),
    },
    "qqbot_safe_tools": {
        "tool_allowlist": _p("list_str", max_chars=64, group_overridable=False, sensitive=True),
        "tool_cooldown_seconds": _p("int", minimum=0, maximum=3600),
        "require_confirm": _p("bool", group_overridable=False),
    },
    "qqbot_food": {
        "default_city": _p("str", max_chars=32),
        "max_results": _p("int", minimum=1, maximum=20),
        "sort": _p("enum", enum=("total", "taste", "service", "environment")),
        "allow_submit_review": _p("bool"),
    },
    "qqbot_stickers": {
        "decoration_probability": _p("float", minimum=0.0, maximum=1.0),
        "ai_decoration_enabled": _p("bool"),
        "ai_decoration_probability": _p("float", minimum=0.0, maximum=1.0),
        "candidate_count": _p("int", minimum=1, maximum=20),
        "collect_enabled": _p("bool"),
    },
    "qqbot_memory_commands": {
        "allow_write": _p("bool"),
        "max_results": _p("int", minimum=1, maximum=50),
        "visible_scopes": _p("list_str", max_chars=16, group_overridable=False, sensitive=True),
    },
    "qqbot_web_reader": {},
    "qqbot_image_search": {},
    "qqbot_social_state": {
        "favorability.half_life_days": _p("int", minimum=0, maximum=365),
        "relationship.enabled": _p("bool"),
        "relationship.min_evidence": _p("int", minimum=1, maximum=10),
    },
    "qqbot_expression_state": {
        "emotion.half_life_minutes": _p("int", minimum=5, maximum=1440),
        "style.enabled": _p("bool"),
        "style.refresh_hours": _p("int", minimum=1, maximum=168),
    },
    "qqbot_vocabulary": {
        "inject.max_entries": _p("int", minimum=1, maximum=20),
        "inject.max_chars": _p("int", minimum=0, maximum=1000),
    },
}


def _new_store() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "global": {"plugins": {}},
        "templates": {},
        "groups": {},
        "private": {},
        "audit": [],
    }


def _target_id(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 128:
        raise PolicyError("invalid_target", f"{field_name} must be 1..128 characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise PolicyError("invalid_target", f"{field_name} contains control characters")
    return text


def _plugin_name(value: Any) -> str:
    text = str(value or "").strip()
    if text not in PLUGIN_PARAM_SPECS:
        raise PolicyError("unknown_plugin", "plugin is not in the policy schema")
    return text


def _coerce_scalar(spec: ParamSpec, value: Any) -> Any:
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise PolicyError("invalid_param", "expected boolean")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise PolicyError("invalid_param", "expected integer")
        if spec.minimum is not None and value < spec.minimum:
            raise PolicyError("invalid_param", "integer below minimum")
        if spec.maximum is not None and value > spec.maximum:
            raise PolicyError("invalid_param", "integer above maximum")
        return value
    if spec.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise PolicyError("invalid_param", "expected number")
        number = float(value)
        if spec.minimum is not None and number < spec.minimum:
            raise PolicyError("invalid_param", "number below minimum")
        if spec.maximum is not None and number > spec.maximum:
            raise PolicyError("invalid_param", "number above maximum")
        return number
    if spec.kind == "str":
        if not isinstance(value, str):
            raise PolicyError("invalid_param", "expected string")
        text = value.strip()
        if not text:
            raise PolicyError("invalid_param", "string must not be empty")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise PolicyError("invalid_param", "string contains control characters")
        if spec.max_chars is not None:
            text = text[: spec.max_chars]
        return text
    if spec.kind == "enum":
        text = str(value or "").strip()
        if text not in spec.enum:
            raise PolicyError("invalid_param", "value is not in enum")
        return text
    if spec.kind == "list_str":
        if not isinstance(value, (list, tuple)):
            raise PolicyError("invalid_param", "expected list of strings")
        result: list[str] = []
        for item in value:
            text = str(item or "").strip()
            if not text or (spec.max_chars is not None and len(text) > spec.max_chars):
                raise PolicyError("invalid_param", "invalid list item")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
                raise PolicyError("invalid_param", "list item contains control characters")
            if text not in result:
                result.append(text)
        return result
    raise PolicyError("invalid_schema", "unsupported parameter kind")


def _validate_params(
    plugin_id: str, params: Any, *, global_scope: bool = False
) -> dict[str, Any]:
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise PolicyError("invalid_param", "params must be an object")
    specs = PLUGIN_PARAM_SPECS[plugin_id]
    result: dict[str, Any] = {}
    for raw_key, raw_value in params.items():
        key = str(raw_key)
        spec = specs.get(key)
        if spec is None:
            raise PolicyError("unknown_param", f"unknown parameter: {key}")
        if spec.sensitive or (not spec.group_overridable and not global_scope):
            raise PolicyError("sensitive_param", f"parameter is not group-overridable: {key}")
        result[key] = _coerce_scalar(spec, raw_value)
    return result


def _validate_plugin_override(
    plugin_id: str, raw: Any, *, global_scope: bool = False
) -> dict[str, Any]:
    if raw is None:
        return {"enabled": True, "params": {}}
    if not isinstance(raw, Mapping):
        raise PolicyError("invalid_store", "plugin override must be an object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise PolicyError("invalid_param", "enabled must be boolean")
    return {
        "enabled": enabled,
        "params": _validate_params(
            plugin_id, raw.get("params", {}), global_scope=global_scope
        ),
    }


def _merge_plugin(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(overlay.get("enabled", base.get("enabled", True))),
        "params": {**dict(base.get("params", {})), **dict(overlay.get("params", {}))},
    }


class CapabilityPolicy:
    """Single-writer policy store with optimistic revision checks."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._lock = threading.RLock()
        self._store = _new_store()
        self._loaded = False
        if self.path is not None:
            self.load()

    @property
    def revision(self) -> int:
        with self._lock:
            return int(self._store.get("revision", 0))

    def load(self) -> None:
        with self._lock:
            if self.path is None:
                self._store = _new_store()
                self._loaded = True
                return
            if not self.path.exists():
                self._store = _new_store()
                self._loaded = True
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise PolicyError("invalid_store", "policy file is not valid JSON") from exc
            self._store = self._normalise_store(raw)
            self._loaded = True

    def _normalise_store(self, raw: Any) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise PolicyError("invalid_store", "policy root must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise PolicyError("schema_mismatch", "unsupported policy schema version")
        revision = raw.get("revision", 0)
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise PolicyError("invalid_store", "revision must be a non-negative integer")
        store = _new_store()
        store["revision"] = revision
        global_raw = raw.get("global", {})
        if not isinstance(global_raw, Mapping):
            raise PolicyError("invalid_store", "global must be an object")
        store["global"]["plugins"] = self._normalise_plugins(
            global_raw.get("plugins", {}), global_scope=True
        )
        for section in ("templates", "groups", "private"):
            raw_section = raw.get(section, {})
            if not isinstance(raw_section, Mapping):
                raise PolicyError("invalid_store", f"{section} must be an object")
            normalized: dict[str, Any] = {}
            for raw_id, raw_entry in raw_section.items():
                target = _target_id(raw_id, section)
                if not isinstance(raw_entry, Mapping):
                    raise PolicyError("invalid_store", f"{section} entry must be an object")
                entry: dict[str, Any] = {"plugins": self._normalise_plugins(raw_entry.get("plugins", {}))}
                if section == "groups":
                    template = raw_entry.get("template", "")
                    if template:
                        entry["template"] = _target_id(template, "template")
                normalized[target] = entry
            store[section] = normalized
        audit = raw.get("audit", [])
        if not isinstance(audit, list):
            raise PolicyError("invalid_store", "audit must be a list")
        store["audit"] = deepcopy(audit[-MAX_AUDIT_ENTRIES:])
        return store

    def _normalise_plugins(
        self, raw: Any, *, global_scope: bool = False
    ) -> dict[str, Any]:
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            raise PolicyError("invalid_store", "plugins must be an object")
        result: dict[str, Any] = {}
        for raw_plugin, raw_override in raw.items():
            plugin_id = _plugin_name(raw_plugin)
            result[plugin_id] = _validate_plugin_override(
                plugin_id, raw_override, global_scope=global_scope
            )
        return result

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise PolicyError("not_loaded", "policy is not loaded")

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        payload = json.dumps(self._store, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix=".capability_policy.", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def effective(
        self,
        plugin_id: str,
        *,
        group_id: str | None = None,
        user_id: str | None = None,
        is_private: bool = False,
    ) -> EffectivePolicy:
        with self._lock:
            self._require_loaded()
            plugin = _plugin_name(plugin_id)
            base = {"enabled": True, "params": {}}
            sources: list[str] = ["global"]
            global_override = self._store["global"]["plugins"].get(plugin)
            if global_override:
                base = _merge_plugin(base, global_override)
            if is_private:
                target = _target_id(user_id, "user_id") if user_id else ""
                override = self._store["private"].get(target, {}).get("plugins", {}).get(plugin) if target else None
                if override:
                    base = _merge_plugin(base, override)
                    sources.append(f"private:{target}")
            elif group_id:
                target = _target_id(group_id, "group_id")
                group_entry = self._store["groups"].get(target, {})
                template_id = str(group_entry.get("template", "") or "")
                if template_id:
                    template_override = self._store["templates"].get(template_id, {}).get("plugins", {}).get(plugin)
                    if template_override:
                        base = _merge_plugin(base, template_override)
                        sources.append(f"template:{template_id}")
                group_override = group_entry.get("plugins", {}).get(plugin)
                if group_override:
                    base = _merge_plugin(base, group_override)
                    sources.append(f"group:{target}")
            return EffectivePolicy(
                plugin_id=plugin,
                enabled=bool(base["enabled"]),
                params=dict(base["params"]),
                sources=tuple(sources),
                revision=self.revision,
            )

    def _audit(self, action: str, target: str, plugin: str, before: Any, after: Any) -> None:
        audit = self._store.setdefault("audit", [])
        audit.append(
            {
                "at": int(time.time()),
                "action": action,
                "target": target,
                "plugin": plugin,
                "before": deepcopy(before),
                "after": deepcopy(after),
            }
        )
        if len(audit) > MAX_AUDIT_ENTRIES:
            del audit[: len(audit) - MAX_AUDIT_ENTRIES]

    def set_global_override(
        self,
        *,
        plugin_id: str,
        enabled: bool = True,
        params: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> EffectivePolicy:
        with self._lock:
            self._require_loaded()
            plugin = _plugin_name(plugin_id)
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            if not isinstance(enabled, bool):
                raise PolicyError("invalid_value", "enabled must be boolean")
            override = {
                "enabled": enabled,
                "params": _validate_params(plugin, params or {}, global_scope=True),
            }
            entry = self._store["global"]["plugins"]
            before = deepcopy(entry.get(plugin))
            entry[plugin] = override
            self._audit("set", "global", plugin, before, override)
            self._store["revision"] = self.revision + 1
            self._save()
            return self.effective(plugin)

    def reset_global_override(
        self,
        *,
        plugin_id: str,
        expected_revision: int | None = None,
    ) -> EffectivePolicy:
        with self._lock:
            self._require_loaded()
            plugin = _plugin_name(plugin_id)
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            entry = self._store["global"]["plugins"]
            before = deepcopy(entry.pop(plugin, None))
            self._audit("reset", "global", plugin, before, None)
            self._store["revision"] = self.revision + 1
            self._save()
            return self.effective(plugin)

    def set_override(
        self,
        *,
        plugin_id: str,
        group_id: str | None = None,
        user_id: str | None = None,
        is_private: bool = False,
        enabled: bool = True,
        params: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> EffectivePolicy:
        with self._lock:
            self._require_loaded()
            plugin = _plugin_name(plugin_id)
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            if not isinstance(enabled, bool):
                raise PolicyError("invalid_value", "enabled must be boolean")
            override = {"enabled": enabled, "params": _validate_params(plugin, params or {})}
            if is_private:
                target = _target_id(user_id, "user_id")
                section = self._store["private"]
                label = f"private:{target}"
            else:
                target = _target_id(group_id, "group_id")
                section = self._store["groups"]
                label = f"group:{target}"
            entry = section.setdefault(target, {"plugins": {}})
            before = deepcopy(entry.get("plugins", {}).get(plugin))
            entry.setdefault("plugins", {})[plugin] = override
            self._audit("set", label, plugin, before, override)
            self._store["revision"] = self.revision + 1
            self._save()
            return self.effective(plugin, group_id=target if not is_private else None, user_id=target if is_private else None, is_private=is_private)

    def reset_override(
        self,
        *,
        plugin_id: str,
        group_id: str | None = None,
        user_id: str | None = None,
        is_private: bool = False,
        expected_revision: int | None = None,
    ) -> EffectivePolicy:
        with self._lock:
            self._require_loaded()
            plugin = _plugin_name(plugin_id)
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            if is_private:
                target = _target_id(user_id, "user_id")
                section = self._store["private"]
                label = f"private:{target}"
            else:
                target = _target_id(group_id, "group_id")
                section = self._store["groups"]
                label = f"group:{target}"
            entry = section.get(target)
            if entry is not None:
                before = deepcopy(entry.get("plugins", {}).get(plugin))
                entry.get("plugins", {}).pop(plugin, None)
                self._audit("reset", label, plugin, before, None)
            self._store["revision"] = self.revision + 1
            self._save()
            return self.effective(plugin, group_id=target if not is_private else None, user_id=target if is_private else None, is_private=is_private)

    def set_group_template(
        self,
        group_id: str,
        template_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            group = _target_id(group_id, "group_id")
            template = ""
            if template_id:
                template = _target_id(template_id, "template")
                if template not in self._store["templates"]:
                    raise PolicyError("unknown_template", "template does not exist")
            entry = self._store["groups"].setdefault(group, {"plugins": {}})
            before = str(entry.get("template", "") or "")
            entry["template"] = template
            self._audit("apply_template", f"group:{group}", "*", before, template)
            self._store["revision"] = self.revision + 1
            self._save()
            return {"ok": True, "group_id": group, "template": template, "revision": self.revision}

    def set_template_override(
        self,
        *,
        template_id: str,
        plugin_id: str,
        enabled: bool = True,
        params: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> None:
        with self._lock:
            self._require_loaded()
            plugin = _plugin_name(plugin_id)
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            template = _target_id(template_id, "template")
            override = {"enabled": bool(enabled), "params": _validate_params(plugin, params or {})}
            entry = self._store["templates"].setdefault(template, {"plugins": {}})
            before = deepcopy(entry.get("plugins", {}).get(plugin))
            entry.setdefault("plugins", {})[plugin] = override
            self._audit("set_template", f"template:{template}", plugin, before, override)
            self._store["revision"] = self.revision + 1
            self._save()

    def param_schema(self, plugin_id: str) -> dict[str, Any]:
        with self._lock:
            plugin = _plugin_name(plugin_id)
            fields: dict[str, Any] = {}
            for name, spec in PLUGIN_PARAM_SPECS.get(plugin, {}).items():
                fields[name] = {
                    "kind": spec.kind,
                    "minimum": spec.minimum,
                    "maximum": spec.maximum,
                    "max_chars": spec.max_chars,
                    "enum": list(spec.enum) if spec.enum else [],
                    "group_overridable": bool(spec.group_overridable),
                    "sensitive": bool(spec.sensitive),
                }
            return {"plugin_id": plugin, "fields": fields}

    def scope_snapshot(
        self, scope_type: str, scope_id: str
    ) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            kind = str(scope_type or "").strip().lower()
            if kind not in {"group", "private", "template", "global"}:
                raise PolicyError("invalid_scope", "scope_type must be group/private/template/global")
            if kind == "global":
                target = "global"
                entry = {"plugins": deepcopy(self._store["global"]["plugins"])}
            elif kind == "template":
                target = _target_id(scope_id, "template_id")
                entry = deepcopy(self._store["templates"].get(target, {"plugins": {}}))
            elif kind == "private":
                target = _target_id(scope_id, "user_id")
                entry = deepcopy(self._store["private"].get(target, {"plugins": {}}))
            else:
                target = _target_id(scope_id, "group_id")
                entry = deepcopy(self._store["groups"].get(target, {"plugins": {}}))
            plugins = deepcopy(entry.get("plugins", {}))
            effective: dict[str, Any] = {}
            for plugin in PLUGIN_PARAM_SPECS:
                if kind == "global":
                    value = self.effective(plugin)
                elif kind == "template":
                    value = EffectivePolicy(
                        plugin_id=plugin,
                        enabled=True,
                        params={},
                        sources=("global",),
                        revision=self.revision,
                    )
                elif kind == "private":
                    value = self.effective(plugin, user_id=target, is_private=True)
                else:
                    value = self.effective(plugin, group_id=target)
                effective[plugin] = value.as_dict()
            schemas = {plugin: self.param_schema(plugin)["fields"] for plugin in PLUGIN_PARAM_SPECS}
            return {
                "ok": True,
                "scope_type": kind,
                "scope_id": target,
                "template": str(entry.get("template", "") or ""),
                "plugins": plugins,
                "effective": effective,
                "schemas": schemas,
                "available_plugins": sorted(PLUGIN_PARAM_SPECS),
                "templates": sorted(self._store["templates"]),
                "groups": sorted(self._store["groups"]),
                "private": sorted(self._store["private"]),
                "revision": self.revision,
            }

    def scope_reset(
        self,
        scope_type: str,
        scope_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            kind = str(scope_type or "").strip().lower()
            if kind not in {"group", "private"}:
                raise PolicyError("invalid_scope", "scope_type must be group/private")
            if kind == "private":
                target = _target_id(scope_id, "user_id")
                section = self._store["private"]
                label = f"private:{target}"
            else:
                target = _target_id(scope_id, "group_id")
                section = self._store["groups"]
                label = f"group:{target}"
            before = deepcopy(section.pop(target, None))
            self._audit("reset_scope", label, "*", before, None)
            self._store["revision"] = self.revision + 1
            self._save()
            return {"ok": True, "revision": self.revision, "before": before}

    def audit_entries(
        self,
        *,
        scope_type: str = "",
        scope_id: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            prefix = ""
            if scope_type:
                kind = str(scope_type).strip().lower()
                if kind not in {"group", "private", "template", "global"}:
                    raise PolicyError("invalid_scope", "invalid audit scope")
                if kind == "global":
                    prefix = "global"
                elif kind == "template":
                    prefix = f"template:{_target_id(scope_id, 'template_id')}"
                elif kind == "private":
                    prefix = f"private:{_target_id(scope_id, 'user_id')}"
                else:
                    prefix = f"group:{_target_id(scope_id, 'group_id')}"
            rows = list(reversed(self._store.get("audit", [])))
            if prefix:
                rows = [row for row in rows if str(row.get("target", "")).startswith(prefix)]
            try:
                limit_value = max(1, min(200, int(limit)))
                offset_value = max(0, int(offset))
            except (TypeError, ValueError) as exc:
                raise PolicyError("invalid_param", "limit/offset must be integers") from exc
            total = len(rows)
            return {
                "total": total,
                "limit": limit_value,
                "offset": offset_value,
                "items": rows[offset_value : offset_value + limit_value],
                "revision": self.revision,
            }

    def list_templates(self) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            used: dict[str, list[str]] = {}
            for group_id, entry in self._store["groups"].items():
                template = str(entry.get("template", "") or "")
                if template:
                    used.setdefault(template, []).append(group_id)
            return {
                "ok": True,
                "revision": self.revision,
                "templates": {
                    template_id: {
                        "plugins": deepcopy(entry.get("plugins", {})),
                        "used_by": used.get(template_id, []),
                    }
                    for template_id, entry in self._store["templates"].items()
                },
            }

    def upsert_template(
        self,
        template_id: str,
        *,
        plugins: Mapping[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            template = _target_id(template_id, "template")
            if plugins is not None:
                if not isinstance(plugins, Mapping):
                    raise PolicyError("invalid_param", "plugins must be an object")
                normalized = self._normalise_plugins(plugins)
            else:
                normalized = deepcopy(self._store["templates"].get(template, {}).get("plugins", {}))
            before = deepcopy(self._store["templates"].get(template))
            self._store["templates"][template] = {"plugins": normalized}
            self._audit("set_template", f"template:{template}", "*", before, {"plugins": normalized})
            self._store["revision"] = self.revision + 1
            self._save()
            return {"ok": True, "template": template, "plugins": deepcopy(normalized), "revision": self.revision}

    def delete_template(
        self,
        template_id: str,
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            if expected_revision is not None and int(expected_revision) != self.revision:
                raise PolicyError("revision_conflict", "policy revision is stale")
            template = _target_id(template_id, "template")
            used = [
                group_id
                for group_id, entry in self._store["groups"].items()
                if str(entry.get("template", "") or "") == template
            ]
            if used:
                raise PolicyError("template_in_use", "template is used by groups: " + ",".join(used[:5]))
            before = deepcopy(self._store["templates"].pop(template, None))
            self._audit("delete_template", f"template:{template}", "*", before, None)
            self._store["revision"] = self.revision + 1
            self._save()
            return {"ok": True, "template": template, "revision": self.revision}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._require_loaded()
            return deepcopy(self._store)
