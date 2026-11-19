"""V6 expression state: deterministic mood + style observation and prompt block."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:
    from . import expression_state
except ImportError:  # AstrBot loads plugin modules as top-level
    import expression_state  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_expression_state"
PLUGIN_VERSION = "1.0.0"
DEFAULTS = {
    "enabled": True,
    "observe_enabled": True,
    "apply_effects": True,
    "half_life_minutes": 45,
    "max_event_delta": 0.15,
    "style_min_samples": 30,
    "inject_max_chars": 120,
    "allow_unmentioned_command": False,
}


def _safe_platform(event: Any) -> str:
    text = str(getattr(event, "unified_msg_origin", "") or "")
    if "qqbot-onebot" in text or "aiocqhttp" in text:
        return "onebot"
    if "qqbot-official" in text or "qq_official" in text:
        return "qq_official"
    return "default"


def event_scope(event: Any) -> str:
    try:
        group = str(event.get_group_id() or "")
    except Exception:
        group = ""
    try:
        user = str(event.get_sender_id() or "")
    except Exception:
        user = ""
    platform = _safe_platform(event)
    if group:
        return f"group:{platform}:{group}"[:200]
    return f"user:{platform}:{user or 'unknown'}"[:200]


@register(PLUGIN_NAME, "QQ Bot Studio", "V6 情绪与风格表达状态", PLUGIN_VERSION)
class ExpressionStatePlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config_obj = config if isinstance(config, dict) else {}
        self._store: expression_state.ExpressionStore | None = None
        self._store_path: Path | None = None

    def _cfg(self, key: str, default: Any = None) -> Any:
        value = self.config_obj.get(key, DEFAULTS.get(key, default))
        if value is None:
            return DEFAULTS.get(key, default)
        return value

    def _enabled(self) -> bool:
        return bool(self._cfg("enabled", True))

    def _data_dir(self) -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            return Path("data/plugin_data") / PLUGIN_NAME

    def _get_store(self) -> expression_state.ExpressionStore:
        path = self._data_dir() / "expression_state.db"
        if self._store is None or self._store_path != path:
            self._store = expression_state.ExpressionStore(
                path,
                max_event_delta=float(self._cfg("max_event_delta", 0.15)),
                half_life_seconds=float(self._cfg("half_life_minutes", 45)) * 60.0,
                style_min_samples=int(self._cfg("style_min_samples", 30)),
            )
            self._store_path = path
        return self._store

    async def initialize(self) -> None:
        if self._enabled():
            try:
                self._get_store()
            except Exception:
                logger.exception("qqbot_expression_state 初始化失败")

    async def terminate(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=119)
    async def observe_message(self, event: AstrMessageEvent):
        """Record one bounded observation; never replies and never stops events."""
        if not self._enabled() or not bool(self._cfg("observe_enabled", True)):
            return
        text = str(getattr(event, "message_str", "") or "").strip()
        if not text or text.startswith("/") or text.startswith("[[") or text.startswith("【"):
            return
        try:
            if str(event.get_sender_id() or "") == str(event.get_self_id() or ""):
                return
        except Exception:
            pass
        try:
            self._get_store().observe(event_scope(event), text, reason="message")
        except Exception:
            logger.debug("qqbot_expression_state observe failed", exc_info=True)

    # ------------------------------------------------------------------
    # Public ABI used by qqbot_core_reply / Studio
    # ------------------------------------------------------------------
    def expression_context(self, event: Any, message_text: str = "") -> str:
        if not self._enabled() or not bool(self._cfg("apply_effects", True)):
            return ""
        try:
            return self._get_store().context(
                event_scope(event), int(self._cfg("inject_max_chars", 120))
            )
        except Exception:
            return ""

    def expression_snapshot(self, event: Any) -> dict[str, Any]:
        try:
            return self._get_store().snapshot(event_scope(event))
        except Exception:
            return {"scope": "", "emotion": "", "style_ready": False, "optout": False}

    def expression_status(self) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "enabled": self._enabled(),
                "apply_effects": bool(self._cfg("apply_effects", True)),
                "style_min_samples": int(self._cfg("style_min_samples", 30)),
                "db": str(self._data_dir() / "expression_state.db"),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:120]}

    def set_optout(self, enabled: bool, scope: str = "studio:global") -> dict[str, Any]:
        try:
            key = str(scope or "studio:global")[:200]
            self._get_store().set_optout(key, bool(enabled))
            return {"ok": True, "scope": key, "optout": bool(enabled)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:120]}

    def snapshot_for_scope(self, scope: str) -> dict[str, Any]:
        try:
            return self._get_store().snapshot(scope)
        except Exception as exc:
            return {"scope": scope, "error": str(exc)[:120]}

    def reset_scope(self, scope: str) -> dict[str, Any]:
        try:
            self._get_store().reset(scope)
            return {"ok": True, "scope": scope}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:120]}

    def export_scope(self, scope: str = "") -> dict[str, Any]:
        try:
            return {"ok": True, "data": self._get_store().export(scope or None)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:120]}
