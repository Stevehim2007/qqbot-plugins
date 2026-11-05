"""qqbot_stickers 3.0.0 -- standalone, stdlib-only sticker library.

The plugin owns collection, storage, deterministic intent parsing, sending and
optional decoration of results explicitly marked by qqbot_core_reply.  It has
no import or runtime dependency on qqbot_enhancer / qqbot_core_full, never
calls a chat model for command parsing, and never parses or consumes tool
markers.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import random
import re
import stat
import time
from pathlib import Path
from typing import Any, Iterable

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

try:  # Loaded as a package by AstrBot.
    from . import sticker_intent as sticker_intent_engine
    from . import sticker_reclassify as sticker_reclassify_engine
    from .media import (
        ImageValidationError,
        decode_base64_payload,
        inspect_image_bytes,
    )
    from .migration import MigrationResult, migrate_legacy_stickers
    from .sticker_store import (
        COLLECTED_CATEGORY_RE,
        COLLECTED_STICKER_DIR_NAME,
        CollectedStickerStore,
        DEFAULT_COLLECTED_CATEGORY,
        StickerStoreError,
        normalize_category_name,
    )
except ImportError:  # Loaded directly by a unit-test harness.
    import sys as _sys

    _plugin_dir = str(Path(__file__).resolve().parent)
    if _plugin_dir not in _sys.path:
        _sys.path.insert(0, _plugin_dir)
    import sticker_intent as sticker_intent_engine
    import sticker_reclassify as sticker_reclassify_engine
    from media import ImageValidationError, decode_base64_payload, inspect_image_bytes
    from migration import MigrationResult, migrate_legacy_stickers
    from sticker_store import (
        COLLECTED_CATEGORY_RE,
        COLLECTED_STICKER_DIR_NAME,
        CollectedStickerStore,
        DEFAULT_COLLECTED_CATEGORY,
        StickerStoreError,
        normalize_category_name,
    )

PLUGIN_NAME = "qqbot_stickers"
PLUGIN_VERSION = "3.0.0"
LEGACY_PLUGIN_NAME = "qqbot_enhancer"
RECENT_MEDIA_MAX_PER_SESSION = 3
RECENT_MEDIA_MAX_SESSIONS = 128
RECENT_MEDIA_TTL_SECONDS = 300
RECENT_COLLECTED_MAX_SESSIONS = 256
AI_REPLY_TIMEOUT_SECONDS = 20
TOOL_MARKER_PRESENCE_RE = re.compile(
    r"\[\[\s*(?:tool|qqbot_tool)\s*:", re.IGNORECASE
)


DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "sticker_enabled": True,
    "sticker_probability": 1.0,
    "sticker_decorate_enabled": True,
    "sticker_decorate_probability": 1.0,
    "sticker_cooldown_seconds": 120,
    "sticker_prevent_repeat": True,
    "sticker_natural_language_enabled": True,
    "sticker_natural_language_send_prefix": "给你一个表情：",
    "sticker_natural_language_not_found_reply": "没有找到「{target}」相关的表情，换个分类或情绪试试吧。",
    "sticker_natural_language_empty_reply": "暂时没有可发送的表情。",
    "sticker_natural_language_cooldown_reply": "表情刚发过，稍等一下再试吧。",
    "sticker_natural_language_collect_enabled": True,
    "sticker_natural_language_reclassify_enabled": True,
    "sticker_natural_language_reclassify_ai_enabled": False,
    "sticker_natural_language_reclassify_provider_id": "",
    "sticker_natural_language_reclassify_min_confidence": 0.6,
    "sticker_natural_language_reclassify_moved_reply": "已把刚才那个表情移到分类「{category}」。",
    "sticker_natural_language_reclassify_cached_reply": "已把刚才那张图保存到分类「{category}」。",
    "sticker_natural_language_reclassify_same_reply": "刚才那个表情已经是「{category}」分类了。",
    "sticker_natural_language_reclassify_not_found_reply": "没有找到最近的表情或可收藏的缓存图片。",
    "sticker_natural_language_reclassify_unclear_reply": "没听清要分到哪个分类，可以发 /收藏表情 分类 手动指定。",
    "sticker_natural_language_reclassify_failed_reply": "分类失败，旧收藏和分类未改动，请稍后再试。",
    "sticker_natural_language_reclassify_memorized_reply": "已记住：这个表情用于表达{meaning}（分类：{category}）。",
    "sticker_natural_language_reclassify_added_reply": "已把刚才那个表情加入分类「{categories}」。",
    "sticker_natural_language_reclassify_removed_reply": "已把刚才那个表情移出分类「{categories}」。",
    "sticker_natural_language_reclassify_categories_same_reply": "刚才那个表情的分类没有变化。",
    "sticker_semantic_tags_enabled": True,
    "collected_sticker_enabled": True,
    "collected_sticker_disabled_reply": "用户表情收藏/使用功能当前未启用。",
    "collected_sticker_max_count": 200,
    "collected_sticker_max_file_bytes": 8 * 1024 * 1024,
    "collected_sticker_max_dimension": 4096,
    "collected_sticker_max_pixels": 16_777_216,
    "collected_sticker_min_dimension": 1,
    "collected_sticker_category_max_chars": 32,
    "collected_sticker_multi_category_enabled": True,
    "collected_sticker_max_categories": 8,
    "collected_sticker_category_separators": ",，、;；|/",
    "collected_sticker_max_tags": 12,
    "collected_sticker_max_tag_chars": 24,
    "collected_sticker_max_meaning_chars": 200,
    "collected_sticker_max_tone_chars": 24,
    "collected_sticker_list_limit": 30,
    "collected_sticker_use_probability": 0.5,
    "pending_sticker_ttl_seconds": 300,
    "collected_sticker_migration_enabled": True,
    "collected_sticker_storage_unavailable_reply": "收藏存储目录不可用，请检查 AstrBot 的 data/plugin_data 权限。",
    "collected_sticker_no_image_reply": "这条消息里没有可收藏的图片。请发送图片/表情包并同时使用 /收藏表情。",
    "collected_sticker_face_unsupported_reply": "QQ 原生 Face 表情没有可保存的图片文件，无法收藏；请发送图片/贴纸形式的 Image。",
    "collected_sticker_invalid_category_reply": "分类名不合法，只允许中文、字母、数字、-、_，且不超过 {max_chars} 个字符。",
    "collected_sticker_invalid_image_reply": "无法识别这张图片的格式，未收藏。支持 JPEG/PNG/GIF/WebP/BMP/AVIF。",
    "collected_sticker_invalid_dimensions_reply": "图片尺寸超出限制，未收藏。",
    "collected_sticker_too_large_reply": "图片超过收藏大小限制，未收藏。",
    "collected_sticker_duplicate_reply": "这张表情已经收藏过（分类：{category}）。",
    "collected_sticker_limit_reply": "收藏数量已达上限 {max_count} 张，请先清理数据目录。",
    "collected_sticker_saved_reply": "已收藏到「{category}」：{filename}",
    "collected_sticker_empty_reply": "还没有收藏任何表情。",
    "collected_sticker_not_found_reply": "没有找到符合条件的收藏表情。",
    "collected_sticker_send_prefix": "送你一张收藏表情（{category}）：{filename}",
    "collected_sticker_list_header": "收藏表情共 {count} 张：",
    "collected_sticker_category_header": "分类「{category}」共 {count} 张：",
    "collected_sticker_list_more": "……另有 {count} 张未展开。",
    "collected_sticker_command_error_reply": "表情收藏命令处理失败，请稍后再试。",
    "auto_collect_user_sticker_enabled": False,
    "sticker_ai_auto_collect_enabled": False,
    "auto_collect_sticker_max_file_bytes": 2_000_000,
    "auto_collect_sticker_max_dimension": 512,
    "auto_collect_sticker_default_category": "未分类",
    "auto_collect_sticker_reply_enabled": True,
    "auto_collect_sticker_cooldown_seconds": 60,
    "auto_collect_sticker_saved_reply": "收下了，已收藏到「{category}」。",
    "safe_tools_allow_send_sticker": True,
    "safe_tools_allow_collect_sticker": True,
    "safe_tools_allow_list_categories": True,
    "safe_tools_collect_cooldown_seconds": 30,
    "safe_tools_sticker_prefix": "给你一个表情：",
}

# Legacy-compatible settings kept in the schema so existing configuration can
# be copied without an unknown-key failure.  This plugin never calls a
# model for these flags; local deterministic behavior is the fallback.
DEFAULTS.update({
    'sticker_ai_select_enabled': True,
    'sticker_ai_marker_enabled': True,
    'sticker_ai_select_probability': 0.6,
    'sticker_ai_auto_collect_probability': 0.2,
    'sticker_ai_auto_collect_max_per_hour': 5,
    'sticker_ai_auto_collect_default_category': '未分类',
    'sticker_ai_auto_collect_require_image': True,
    'sticker_ai_auto_collect_provider_id': '',
    'sticker_ai_auto_collect_min_confidence': 0.6,
    'sticker_ai_auto_collect_saved_reply': '这个表情我收下了（{category}）。',
    'sticker_random_fallback_enabled': False,
    'user_sticker_reaction_enabled': False,
    'user_sticker_reaction_probability': 0.85,
    'user_sticker_reaction_replies': [
        '收到这张表情啦～',
        '这个表情我收下啦！',
        '好可爱，回你一张～',
        '表情包已收到，嘿嘿～',
    ],
    'user_sticker_reaction_attach_sticker': True,
})


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on", "是", "开启"}
    return default


def _as_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def _as_float(value: Any, default: float, *, low=0.0, high=1.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = default
    return max(low, min(high, result))


def _is_image_component(component: Any) -> bool:
    image_cls = getattr(Comp, "Image", None)
    return image_cls is not None and isinstance(component, image_cls)


def _is_reply_component(component: Any) -> bool:
    reply_cls = getattr(Comp, "Reply", None)
    return reply_cls is not None and isinstance(component, reply_cls)


def _is_face_component(component: Any) -> bool:
    face_cls = getattr(Comp, "Face", None)
    return face_cls is not None and isinstance(component, face_cls)


def _is_plain_component(component: Any) -> bool:
    plain_cls = getattr(Comp, "Plain", None)
    return plain_cls is not None and isinstance(component, plain_cls)


def _chain_has_image(chain: Iterable[Any]) -> bool:
    for component in chain:
        if _is_image_component(component):
            return True
        if _is_reply_component(component):
            nested = getattr(component, "chain", None)
            if isinstance(nested, (list, tuple)) and _chain_has_image(nested):
                return True
    return False


def _chain_text(chain: Iterable[Any]) -> str:
    pieces: list[str] = []
    for component in chain:
        if _is_plain_component(component):
            pieces.append(str(getattr(component, "text", "") or ""))
        elif _is_reply_component(component):
            nested = getattr(component, "chain", None)
            if isinstance(nested, (list, tuple)):
                pieces.append(_chain_text(nested))
    return "".join(pieces)


def _has_tool_marker(text: str) -> bool:
    # Presence detection only.  The marker is never removed or executed.
    return TOOL_MARKER_PRESENCE_RE.search(str(text or "")) is not None


class StickerPlugin(Star):
    """AstrBot sticker plugin with a stable cross-plugin tool surface."""

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        try:
            super().__init__(context, config=config)
        except TypeError:
            # AstrBot versions around the 4.29 boundary accept context only.
            super().__init__(context)
        self.context = context
        if isinstance(config, dict):
            injected = dict(config)
        else:
            try:
                injected = dict(config) if config is not None else {}
            except (TypeError, ValueError):
                injected = {}
        # Some AstrBot compatibility versions expose the same injected mapping
        # as ``self.config``.  Read-only use keeps it harmless when absent.
        star_config = getattr(self, "config", None)
        if isinstance(star_config, dict):
            for key, value in star_config.items():
                injected.setdefault(key, value)
        self._plugin_config = injected
        # Keep the conventional public attribute available for frameworks and
        # diagnostics without ever reading the *global* context config.
        self.config = injected
        self._store: CollectedStickerStore | None = None
        self._data_dir: Path | None = None
        self.legacy_root_override: Path | None = None
        self.data_dir_override: Path | None = None
        self._ready = False
        self._ready_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._rng = random.Random()
        self._clock = time.time
        self._last_sent: dict[str, dict[str, Any]] = {}
        self._recent_media: dict[str, list[dict[str, Any]]] = {}
        self._last_collected: dict[str, dict[str, Any]] = {}
        self._last_auto_collect: dict[str, float] = {}
        self._last_tool_collect: dict[str, float] = {}
        self.last_migration_result: MigrationResult | None = None

    # ------------------------------------------------------------------
    # Configuration and lifecycle
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        if key in self._plugin_config:
            return self._plugin_config[key]
        return DEFAULTS.get(key, default)

    def _cfg_bool(self, key: str, default: bool = False) -> bool:
        return _as_bool(self._cfg(key, DEFAULTS.get(key, default)), default)

    def _cfg_int(self, key: str, default: int = 1, minimum: int | None = 1) -> int:
        return _as_int(self._cfg(key, DEFAULTS.get(key, default)), default, minimum)

    def _cfg_float(self, key: str, default: float = 0.0) -> float:
        return _as_float(self._cfg(key, DEFAULTS.get(key, default)), default)

    def _cfg_str(self, key: str, default: str = "") -> str:
        value = self._cfg(key, DEFAULTS.get(key, default))
        return str(value if value is not None else default)

    @property
    def data_dir(self) -> Path:
        if self._data_dir is not None:
            return self._data_dir
        return self._resolve_data_dir()

    @property
    def store(self) -> CollectedStickerStore | None:
        return self._store

    def configure_paths(
        self,
        *,
        data_dir: Path | str | None = None,
        legacy_root: Path | str | None = None,
    ) -> None:
        """Test/admin hook: set explicit per-plugin data and legacy roots."""

        if data_dir is not None:
            self.data_dir_override = Path(data_dir)
        if legacy_root is not None:
            self.legacy_root_override = Path(legacy_root)

    def _resolve_data_dir(self) -> Path:
        if self.data_dir_override is not None:
            return self.data_dir_override.resolve()
        try:
            raw = StarTools.get_data_dir(plugin_name=PLUGIN_NAME)
        except TypeError:  # older public signature
            raw = StarTools.get_data_dir(PLUGIN_NAME)
        if raw is None:
            raise StickerStoreError("StarTools.get_data_dir returned no path")
        return Path(raw).resolve()

    def _resolve_legacy_root(self) -> Path | None:
        if self.legacy_root_override is not None:
            candidate = self.legacy_root_override
        else:
            try:
                try:
                    raw = StarTools.get_data_dir(plugin_name=LEGACY_PLUGIN_NAME)
                except TypeError:
                    raw = StarTools.get_data_dir(LEGACY_PLUGIN_NAME)
                candidate = Path(raw) / COLLECTED_STICKER_DIR_NAME
            except Exception:
                return None
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                return None
            return candidate.resolve()
        except (OSError, ValueError):
            return None

    def _build_store(self) -> CollectedStickerStore:
        data_dir = self._resolve_data_dir()
        try:
            data_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
            data_dir.chmod(0o755)
        except OSError as exc:
            logger.warning(
                "qqbot_stickers cannot prepare/add host traversal mode on data dir: %s",
                exc,
            )
        root = data_dir / COLLECTED_STICKER_DIR_NAME
        if root.is_symlink():
            raise StickerStoreError("sticker root must not be a symlink")
        store = CollectedStickerStore(
            root,
            max_file_bytes=self._cfg_int(
                "collected_sticker_max_file_bytes", 8 * 1024 * 1024, 1
            ),
            category_normalizer=self._normalize_category,
            default_category=DEFAULT_COLLECTED_CATEGORY,
            log_warning=lambda message: logger.warning("%s", message),
            max_tags=self._cfg_int("collected_sticker_max_tags", 12, 0),
            max_tag_chars=self._cfg_int("collected_sticker_max_tag_chars", 24, 1),
            max_meaning_chars=self._cfg_int(
                "collected_sticker_max_meaning_chars", 200, 1
            ),
            max_tone_chars=self._cfg_int("collected_sticker_max_tone_chars", 24, 1),
            max_categories=self._cfg_int(
                "collected_sticker_max_categories", 8, 1
            ),
            max_count=self._cfg_int("collected_sticker_max_count", 200, 1),
            max_dimension=self._cfg_int(
                "collected_sticker_max_dimension", 4096, 1
            ),
            max_pixels=self._cfg_int(
                "collected_sticker_max_pixels", 16_777_216, 1
            ),
            min_dimension=self._cfg_int("collected_sticker_min_dimension", 1, 1),
            clock=self._clock,
        )
        # Materialize the bounded root during initialization so status and
        # diagnostics do not race with the first write for directory setup.
        store._ensure_root_mode()
        permission_summary = store.migrate_permissions(data_dir)
        if permission_summary.get("errors"):
            logger.warning(
                "qqbot_stickers permission repair completed with %s errors",
                permission_summary["errors"],
            )
        self._data_dir = data_dir
        self._store = store
        return store

    async def initialize(self) -> None:
        """AstrBot lifecycle hook; all failures degrade to command-time errors."""

        await self._ensure_ready()

    async def terminate(self) -> None:
        """AstrBot lifecycle hook; there are no background tasks to cancel."""

        return None

    async def _ensure_ready(self) -> CollectedStickerStore:
        if self._store is not None and self._ready:
            return self._store
        async with self._ready_lock:
            if self._store is None:
                self._store = self._build_store()
            if not self._ready:
                await self._run_migration_once(self._store)
                self._ready = True
            return self._store

    async def _run_migration_once(self, store: CollectedStickerStore) -> None:
        if not self._cfg_bool("collected_sticker_migration_enabled", True):
            return
        legacy = self._resolve_legacy_root()
        if legacy is None:
            return
        try:
            result = await asyncio.to_thread(
                migrate_legacy_stickers,
                legacy,
                store,
                max_file_bytes=self._cfg_int(
                    "collected_sticker_max_file_bytes", 8 * 1024 * 1024, 1
                ),
                max_dimension=self._cfg_int(
                    "collected_sticker_max_dimension", 4096, 1
                ),
                max_pixels=self._cfg_int(
                    "collected_sticker_max_pixels", 16_777_216, 1
                ),
                max_count=self._cfg_int("collected_sticker_max_count", 200, 1),
                default_category=DEFAULT_COLLECTED_CATEGORY,
                category_normalizer=self._normalize_category,
            )
            self.last_migration_result = result
        except Exception:
            logger.warning("qqbot_stickers legacy migration failed safely", exc_info=True)

    def _normalize_category(self, raw: Any) -> str | None:
        return normalize_category_name(
            raw,
            default_category=DEFAULT_COLLECTED_CATEGORY,
            max_chars=self._cfg_int(
                "collected_sticker_category_max_chars", 32, 1
            ),
        )

    def _parse_collect_categories(self, raw: str) -> list[str] | None:
        value = str(raw or "").strip()
        if not self._cfg_bool("collected_sticker_multi_category_enabled", True):
            category = self._normalize_category(value)
            return [category] if category is not None else None
        if not value:
            category = self._normalize_category("")
            return [category] if category is not None else None
        separators = self._cfg_str(
            "collected_sticker_category_separators", ",，、;；|/"
        ) or ",，、;；|/"
        parts = [
            part.strip()
            for part in re.split("[" + re.escape(separators) + "]+", value)
            if part.strip()
        ]
        categories: list[str] = []
        for part in parts:
            normalized = self._normalize_category(part)
            if normalized is None:
                return None
            if normalized not in categories:
                categories.append(normalized)
        limit = self._cfg_int("collected_sticker_max_categories", 8, 1)
        if not categories or len(categories) > limit:
            return None
        return categories

    async def _entries(self) -> list[dict[str, Any]]:
        store = await self._ensure_ready()
        return await asyncio.to_thread(store.load_index)

    def _format(self, template: str, **kwargs: Any) -> str:
        try:
            return str(template).format(**kwargs)
        except (KeyError, ValueError, IndexError):
            return str(template)

    def _category_max_chars(self) -> int:
        return min(32, self._cfg_int("collected_sticker_category_max_chars", 32, 1))

    # ------------------------------------------------------------------
    # Event / result helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        return str(getattr(event, "message_str", "") or "")

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        value = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if value:
            return value
        for attr in ("session_id", "group_id", "sender_id"):
            candidate = str(getattr(event, attr, "") or "").strip()
            if candidate:
                return candidate
        return "default"

    @staticmethod
    async def _event_messages(event: AstrMessageEvent) -> list[Any]:
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            try:
                value = getter()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, (list, tuple)):
                    return list(value)
            except Exception:
                return []
        value = getattr(event, "message", None)
        if isinstance(value, (list, tuple)):
            return list(value)
        return []

    def _is_explicit_conversation(self, event: AstrMessageEvent) -> bool:
        try:
            if callable(getattr(event, "is_private_chat", None)) and event.is_private_chat():
                return True
        except Exception:
            pass
        if _as_bool(getattr(event, "is_at_or_wake_command", False)):
            return True
        if _as_bool(getattr(event, "is_wake", False)):
            return True
        try:
            if _as_bool(event.get_extra("qqbot_stickers_natural_language")):
                return True
        except Exception:
            pass
        return False

    def _is_command_message(self, text: str) -> bool:
        return str(text or "").lstrip().startswith("/")

    @staticmethod
    def _command_argument(
        event: AstrMessageEvent, command_names: tuple[str, ...]
    ) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        # Some adapters retain an @mention/CQ prefix in message_str even after
        # the AstrBot command matcher has selected this handler.
        text = re.sub(
            r"^(?:\[CQ:[^\]]+\]|@\S+)\s*", "", text
        ).strip()
        if text.startswith("/"):
            text = text[1:].lstrip()
        for name in command_names:
            if text == name:
                return ""
            if text.startswith(name):
                rest = text[len(name) :]
                if not rest or rest[0].isspace():
                    return re.sub(r"\s+", " ", rest).strip()
        return ""

    @staticmethod
    def _plain_result(event: AstrMessageEvent, text: str):
        return event.plain_result(str(text))

    @staticmethod
    def _image_component(path: Path):
        factory = getattr(Comp.Image, "fromFileSystem", None)
        if callable(factory):
            return factory(str(path))
        return Comp.Image(str(path))

    def _chain_result(self, event: AstrMessageEvent, components: list[Any]):
        return event.chain_result(components)

    def _stop(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            try:
                stopper()
            except Exception:
                pass

    def _suppress_model(self, event: AstrMessageEvent) -> None:
        # AstrBot's public API uses should_call_llm(True) as the explicit
        # "handled locally, do not start a chat model" switch.
        method = getattr(event, "should_call_llm", None)
        if callable(method):
            try:
                method(True)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Image extraction / recent media cache
    # ------------------------------------------------------------------
    def _local_component_path(self, ref: Any) -> Path | None:
        _looks_local, candidate = self._local_ref_state(ref)
        return candidate

    def _local_ref_state(self, ref: Any) -> tuple[bool, Path | None]:
        """Return (looks_local, safe_path).

        ``looks_local`` is true for an explicit local path that must not fall
        back to ``convert_to_base64`` when unsafe (e.g. a symlink).
        """

        if isinstance(ref, Path):
            considered_local = True
            candidate = ref
        elif isinstance(ref, str):
            value = ref.strip()
            if not value:
                return False, None
            if value.startswith("file://"):
                value = value[7:]
            if "://" in value.casefold() or value.casefold().startswith(
                ("data:", "base64:")
            ):
                return False, None
            candidate = Path(value)
            considered_local = (
                candidate.exists()
                or candidate.is_symlink()
                or value.startswith(("/", "./", "../", "~"))
            )
        else:
            return False, None
        if not considered_local:
            return False, None
        if candidate.is_symlink() or not stat.S_ISREG(candidate.lstat().st_mode):
            return True, None
        return True, candidate

    async def _component_bytes(self, image: Any) -> tuple[str, bytes | None]:
        max_bytes = self._cfg_int(
            "collected_sticker_max_file_bytes", 8 * 1024 * 1024, 1
        )
        # Prefer a local path when the adapter exposes one: it lets us reject an
        # oversized file before reading it into memory.
        for attr in ("data", "path", "file"):
            ref = getattr(image, attr, None)
            if attr == "data" and isinstance(ref, (bytes, bytearray, memoryview)):
                raw = bytes(ref)
                return ("ok" if len(raw) <= max_bytes else "too_large", raw)
            looks_local, local = self._local_ref_state(ref)
            if local is None:
                if looks_local:
                    return "invalid_image", None
                continue
            try:
                size = local.stat().st_size
            except OSError:
                continue
            if size <= 0:
                return "invalid_image", None
            if size > max_bytes:
                return "too_large", None
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(local, flags)
                try:
                    data = os.read(fd, max_bytes + 1)
                finally:
                    os.close(fd)
            except OSError:
                return "invalid_image", None
            if len(data) > max_bytes:
                return "too_large", None
            return "ok", data
        converter = getattr(image, "convert_to_base64", None)
        if callable(converter):
            try:
                value = converter()
                if inspect.isawaitable(value):
                    value = await value
                if isinstance(value, str) and len(value) > max_bytes * 2 + 1024:
                    return "too_large", None
                data = decode_base64_payload(value)
                if data is None:
                    return "invalid_image", None
                if len(data) > max_bytes:
                    return "too_large", None
                return "ok", data
            except Exception:
                return "invalid_image", None
        return "no_image", None

    async def _decode_image_component(
        self, image: Any
    ) -> tuple[str, dict[str, Any] | None]:
        status, data = await self._component_bytes(image)
        if status != "ok" or data is None:
            return status, None
        try:
            info = inspect_image_bytes(
                data,
                max_bytes=self._cfg_int(
                    "collected_sticker_max_file_bytes", 8 * 1024 * 1024, 1
                ),
                max_dimension=self._cfg_int(
                    "collected_sticker_max_dimension", 4096, 1
                ),
                max_pixels=self._cfg_int(
                    "collected_sticker_max_pixels", 16_777_216, 1
                ),
                min_dimension=self._cfg_int("collected_sticker_min_dimension", 1, 1),
            )
        except ImageValidationError as exc:
            if exc.code == "too_large":
                return "too_large", None
            if exc.code == "invalid_dimensions":
                return "invalid_dimensions", None
            return "invalid_image", None
        import hashlib

        return (
            "ok",
            {
                "data": info.data,
                "mime": info.mime,
                "extension": info.extension,
                "width": info.width,
                "height": info.height,
                "size": info.size,
                "sha256": hashlib.sha256(info.data).hexdigest(),
            },
        )

    @staticmethod
    def _quoted_images(components: list[Any]) -> list[Any]:
        images: list[Any] = []
        for component in components:
            if not _is_reply_component(component):
                continue
            chain = getattr(component, "chain", None)
            if isinstance(chain, (list, tuple)):
                images.extend(item for item in chain if _is_image_component(item))
        return images

    async def _extract_image_payload(
        self, event: AstrMessageEvent
    ) -> tuple[str, dict[str, Any] | None]:
        components = await self._event_messages(event)
        current = [item for item in components if _is_image_component(item)]
        quoted = self._quoted_images(components)
        last_status = "no_image"
        for image in current + quoted:
            status, payload = await self._decode_image_component(image)
            if status == "ok" and payload is not None:
                return "ok", payload
            last_status = status
        if current or quoted:
            return last_status, None
        cached = self._get_recent_payload(self._session_key(event))
        if cached is not None:
            return "ok", cached
        if any(_is_face_component(item) for item in components):
            return "face", None
        return "no_image", None

    def _prune_recent_media(self, now: float | None = None) -> None:
        current = self._clock() if now is None else now
        ttl = max(
            1,
            self._cfg_int(
                "pending_sticker_ttl_seconds", RECENT_MEDIA_TTL_SECONDS, 1
            ),
        )
        for key, items in list(self._recent_media.items()):
            kept = [
                item
                for item in items
                if current - float(item.get("_cached_at", 0) or 0) <= ttl
            ]
            if kept:
                self._recent_media[key] = kept[:RECENT_MEDIA_MAX_PER_SESSION]
            else:
                self._recent_media.pop(key, None)
        if len(self._recent_media) > RECENT_MEDIA_MAX_SESSIONS:
            ordered = sorted(
                self._recent_media.items(),
                key=lambda pair: max(
                    (float(item.get("_cached_at", 0) or 0) for item in pair[1]),
                    default=0,
                ),
            )
            for key, _ in ordered[
                : len(self._recent_media) - RECENT_MEDIA_MAX_SESSIONS
            ]:
                self._recent_media.pop(key, None)

    def _get_recent_payload(self, session_key: str) -> dict[str, Any] | None:
        self._prune_recent_media()
        items = self._recent_media.get(str(session_key), [])
        if not items:
            return None
        payload = dict(items[0])
        payload.pop("_cached_at", None)
        return payload

    async def _remember_recent_images(
        self, event: AstrMessageEvent, components: list[Any]
    ) -> None:
        images = [item for item in components if _is_image_component(item)]
        if not images:
            return
        session = self._session_key(event)
        existing = list(self._recent_media.get(session, []))
        now = self._clock()
        for image in images[-RECENT_MEDIA_MAX_PER_SESSION:]:
            status, payload = await self._decode_image_component(image)
            if status != "ok" or payload is None:
                continue
            cached = dict(payload)
            cached["_cached_at"] = now
            existing = [
                item
                for item in existing
                if item.get("sha256") != cached.get("sha256")
            ]
            existing.insert(0, cached)
            existing = existing[:RECENT_MEDIA_MAX_PER_SESSION]
        if existing:
            self._recent_media[session] = existing
        self._prune_recent_media(now)

    # ------------------------------------------------------------------
    # Persistence writes and selection
    # ------------------------------------------------------------------
    async def _save_payload(
        self,
        event: AstrMessageEvent,
        payload: dict[str, Any],
        category: str,
        *,
        categories: list[str] | None = None,
        tags: list[str] | None = None,
        meaning: str = "",
        tone: str = "",
    ) -> tuple[str, dict[str, Any] | None]:
        try:
            store = await self._ensure_ready()
        except Exception:
            return "storage", None
        source = ""
        try:
            getter = getattr(event, "get_platform_name", None)
            if callable(getter):
                source = str(getter() or "")
        except Exception:
            source = ""
        data = payload.get("data")
        if not isinstance(data, bytes):
            return "invalid_image", None
        async with self._write_lock:
            return await asyncio.to_thread(
                store.add_sticker,
                data,
                category=category,
                categories=list(categories or [category]),
                tags=list(tags or []),
                meaning=str(meaning or ""),
                tone=str(tone or ""),
                source=source,
                merge_duplicate=False,
            )

    def _entry_matches_tag(self, entry: dict[str, Any], term: str) -> bool:
        wanted = str(term or "").strip().casefold()
        if not wanted:
            return False
        values = []
        categories = entry.get("categories")
        if isinstance(categories, (list, tuple, set)):
            values.extend(str(item) for item in categories)
        values.append(str(entry.get("category", "")))
        tags = entry.get("tags")
        if isinstance(tags, (list, tuple, set)):
            values.extend(str(item) for item in tags)
        values.append(str(entry.get("tone", "")))
        if any(value and value.casefold() == wanted for value in values):
            return True
        meaning = str(entry.get("meaning", "") or "").casefold()
        return bool(meaning and wanted in meaning)

    def _entry_matches_emotion(self, entry: dict[str, Any], emotion: str) -> bool:
        wanted = str(emotion or "").strip().casefold()
        if not wanted:
            return False
        text_parts = [
            str(entry.get("category", "")),
            str(entry.get("tone", "")),
            str(entry.get("meaning", "")),
        ]
        categories = entry.get("categories")
        if isinstance(categories, (list, tuple, set)):
            text_parts.extend(str(item) for item in categories)
        tags = entry.get("tags")
        if isinstance(tags, (list, tuple, set)):
            text_parts.extend(str(item) for item in tags)
        folded_parts = [part.casefold() for part in text_parts if part]
        if any(part == wanted for part in folded_parts):
            return True
        keywords: tuple[str, ...] = ()
        for words, emotion_name in sticker_intent_engine.EMOTION_KEYWORDS:
            if emotion_name == wanted:
                keywords = tuple(words)
                break
        if wanted in sticker_intent_engine.SNARK_EMOTIONS:
            keywords = tuple(sticker_intent_engine.SNARK_KEYWORDS)
        combined = "\n".join(folded_parts)
        return any(keyword.casefold() in combined for keyword in keywords)

    def _filter_entries(
        self,
        entries: list[dict[str, Any]],
        *,
        category: str = "",
        emotion: str = "",
        tag: str = "",
    ) -> list[dict[str, Any]]:
        result = list(entries)
        if category:
            wanted = str(category).strip().casefold()
            result = [
                entry
                for entry in result
                if any(
                    str(value).casefold() == wanted
                    for value in (
                        entry.get("categories")
                        if isinstance(entry.get("categories"), (list, tuple, set))
                        else [entry.get("category", "")]
                    )
                )
            ]
        if emotion:
            result = [
                entry
                for entry in result
                if self._entry_matches_emotion(entry, emotion)
            ]
        if tag:
            result = [
                entry for entry in result if self._entry_matches_tag(entry, tag)
            ]
        return result

    def _choose_entry(
        self, event: AstrMessageEvent, entries: list[dict[str, Any]]
    ) -> tuple[dict[str, Any] | None, str]:
        if not entries:
            return None, "no_match"
        session = self._session_key(event)
        now = self._clock()
        cooldown = self._cfg_int("sticker_cooldown_seconds", 120, 0)
        last = self._last_sent.get(session)
        if last is not None and now - float(last.get("at", 0) or 0) < cooldown:
            return None, "cooldown"
        candidates = list(entries)
        if self._cfg_bool("sticker_prevent_repeat", True) and candidates:
            last_filename = str((last or {}).get("filename", ""))
            if last_filename and len(candidates) > 1:
                non_repeating = [
                    entry
                    for entry in candidates
                    if str(entry.get("filename", "")) != last_filename
                ]
                if non_repeating:
                    candidates = non_repeating
        if not candidates:
            return None, "no_match"
        chosen = self._rng.choice(candidates)
        self._last_sent[session] = {
            "at": now,
            "filename": str(chosen.get("filename", "")),
            "sha256": str(chosen.get("sha256", "")),
        }
        if len(self._last_sent) > 256:
            ordered = sorted(
                self._last_sent.items(),
                key=lambda pair: float(pair[1].get("at", 0) or 0),
            )
            for key, _ in ordered[: len(self._last_sent) - 256]:
                self._last_sent.pop(key, None)
        return chosen, "selected"

    def _remember_collected(
        self, event: AstrMessageEvent, entry: dict[str, Any]
    ) -> None:
        session = self._session_key(event)
        self._last_collected[session] = {
            "sha256": str(entry.get("sha256", "")),
            "filename": str(entry.get("filename", "")),
            "category": str(entry.get("category", "")),
            "saved_at": int(self._clock()),
        }
        if len(self._last_collected) > RECENT_COLLECTED_MAX_SESSIONS:
            ordered = sorted(
                self._last_collected.items(),
                key=lambda pair: int(pair[1].get("saved_at", 0) or 0),
            )
            for key, _ in ordered[
                : len(self._last_collected) - RECENT_COLLECTED_MAX_SESSIONS
            ]:
                self._last_collected.pop(key, None)

    def _collect_status_reply(
        self, status: str, entry: dict[str, Any] | None = None
    ) -> str:
        if status == "saved" and entry is not None:
            return self._format(
                self._cfg_str("collected_sticker_saved_reply"),
                category=entry.get("category", DEFAULT_COLLECTED_CATEGORY),
                filename=entry.get("filename", ""),
            )
        if status == "duplicate" and entry is not None:
            return self._format(
                self._cfg_str("collected_sticker_duplicate_reply"),
                category=entry.get("category", DEFAULT_COLLECTED_CATEGORY),
            )
        if status == "limit":
            return self._format(
                self._cfg_str("collected_sticker_limit_reply"),
                max_count=self._cfg_int("collected_sticker_max_count", 200, 1),
            )
        if status == "too_large":
            return self._cfg_str("collected_sticker_too_large_reply")
        if status == "invalid_dimensions":
            return self._cfg_str("collected_sticker_invalid_dimensions_reply")
        if status in {"invalid", "invalid_image"}:
            return self._cfg_str("collected_sticker_invalid_image_reply")
        if status == "storage":
            return self._cfg_str("collected_sticker_storage_unavailable_reply")
        return self._cfg_str("collected_sticker_command_error_reply")

    def _collect_extraction_reply(self, status: str) -> str:
        if status == "too_large":
            return self._cfg_str("collected_sticker_too_large_reply")
        if status == "invalid_dimensions":
            return self._cfg_str("collected_sticker_invalid_dimensions_reply")
        if status == "invalid_image":
            return self._cfg_str("collected_sticker_invalid_image_reply")
        if status == "no_image":
            return self._cfg_str("collected_sticker_no_image_reply")
        if status == "face":
            return self._cfg_str("collected_sticker_face_unsupported_reply")
        return self._cfg_str("collected_sticker_command_error_reply")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------
    @filter.command("收藏表情", priority=110)
    async def collect_sticker_command(self, event: AstrMessageEvent):
        self._suppress_model(event)
        try:
            if not self._cfg_bool("enabled", True) or not self._cfg_bool(
                "collected_sticker_enabled", True
            ):
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_disabled_reply")
                )
                return
            raw_category = self._command_argument(event, ("收藏表情",))
            categories = self._parse_collect_categories(raw_category)
            if categories is None:
                yield self._plain_result(
                    event,
                    self._format(
                        self._cfg_str("collected_sticker_invalid_category_reply"),
                        max_chars=self._category_max_chars(),
                    ),
                )
                return
            status, payload = await self._extract_image_payload(event)
            if status != "ok" or payload is None:
                yield self._plain_result(event, self._collect_extraction_reply(status))
                return
            save_status, entry = await self._save_payload(
                event,
                payload,
                categories[0],
                categories=categories,
            )
            if entry is not None:
                self._remember_collected(event, entry)
            yield self._plain_result(event, self._collect_status_reply(save_status, entry))
        except Exception:
            logger.warning("qqbot_stickers /收藏表情 failed", exc_info=True)
            yield self._plain_result(
                event, self._cfg_str("collected_sticker_command_error_reply")
            )
        finally:
            self._stop(event)

    @filter.command("表情状态", alias={"表情诊断", "表情库"}, priority=110)
    async def sticker_status_command(self, event: AstrMessageEvent):
        self._suppress_model(event)
        try:
            lines = ["表情状态："]
            enabled = self._cfg_bool("collected_sticker_enabled", True)
            lines.append("- 收藏功能：" + ("已启用" if enabled else "未启用"))
            store_error = ""
            entries: list[dict[str, Any]] = []
            try:
                entries = await self._entries()
                lines.append("- 存储：可用")
            except Exception as exc:
                store_error = type(exc).__name__
                lines.append("- 存储：不可用")
            unique = len({str(item.get("sha256", "")) for item in entries})
            lines.append(
                f"- 收藏：{unique} 张 / 上限 "
                f"{self._cfg_int('collected_sticker_max_count', 200, 1)} 张"
            )
            counts = CollectedStickerStore.categories(entries)
            lines.append(f"- 分类：{len(counts)} 个")
            lines.append(
                "- 语义标签/含义："
                + ("已启用" if self._cfg_bool("sticker_semantic_tags_enabled", True) else "未启用")
            )
            lines.append(
                f"- 单图上限：{self._cfg_int('collected_sticker_max_file_bytes', 8 * 1024 * 1024, 1)} 字节；"
                f"最大边长 {self._cfg_int('collected_sticker_max_dimension', 4096, 1)}"
            )
            recent = self._get_recent_payload(self._session_key(event))
            lines.append("- 最近媒体缓存：" + ("有" if recent is not None else "无"))
            if self.last_migration_result is not None:
                result = self.last_migration_result
                lines.append(
                    f"- 旧数据迁移：导入 {result.imported}，重复 {result.duplicates}，"
                    f"跳过 {result.skipped}，错误 {result.errors}"
                )
            if store_error:
                lines.append(f"- 存储诊断：{store_error}")
            yield self._plain_result(event, "\n".join(lines))
        except Exception:
            yield self._plain_result(
                event, self._cfg_str("collected_sticker_command_error_reply")
            )
        finally:
            self._stop(event)

    @filter.command("表情分类", priority=110)
    async def sticker_categories_command(self, event: AstrMessageEvent):
        self._suppress_model(event)
        try:
            if not self._cfg_bool("collected_sticker_enabled", True):
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_disabled_reply")
                )
                return
            entries = await self._entries()
            if not entries:
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_empty_reply")
                )
                return
            counts = CollectedStickerStore.categories(entries)
            unique = len({str(item.get("sha256", "")) for item in entries})
            membership = sum(counts.values())
            lines = [
                f"收藏表情共 {unique} 张（分类归属 {membership} 次），"
                f"分类 {len(counts)} 个："
            ]
            limit = self._cfg_int("collected_sticker_list_limit", 30, 1)
            ordered = sorted(counts)
            for category in ordered[:limit]:
                lines.append(f"- {category}: {counts[category]} 张")
            if len(ordered) > limit:
                lines.append(
                    self._format(
                        self._cfg_str("collected_sticker_list_more"),
                        count=len(ordered) - limit,
                    )
                )
            yield self._plain_result(event, "\n".join(lines))
        except Exception:
            yield self._plain_result(
                event, self._cfg_str("collected_sticker_command_error_reply")
            )
        finally:
            self._stop(event)

    @filter.command("表情列表", priority=110)
    async def sticker_list_command(self, event: AstrMessageEvent):
        self._suppress_model(event)
        try:
            if not self._cfg_bool("collected_sticker_enabled", True):
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_disabled_reply")
                )
                return
            raw_category = self._command_argument(event, ("表情列表",))
            category: str | None = None
            if raw_category:
                category = self._normalize_category(raw_category)
                if category is None:
                    yield self._plain_result(
                        event,
                        self._format(
                            self._cfg_str("collected_sticker_invalid_category_reply"),
                            max_chars=self._category_max_chars(),
                        ),
                    )
                    return
            entries = await self._entries()
            if category is not None:
                entries = CollectedStickerStore.list_entries(entries, category)
            if not entries:
                key = (
                    "collected_sticker_not_found_reply"
                    if category is not None
                    else "collected_sticker_empty_reply"
                )
                yield self._plain_result(event, self._cfg_str(key))
                return
            limit = self._cfg_int("collected_sticker_list_limit", 30, 1)
            if category is None:
                header = self._format(
                    self._cfg_str("collected_sticker_list_header"),
                    count=len(entries),
                )
            else:
                header = self._format(
                    self._cfg_str("collected_sticker_category_header"),
                    category=category,
                    count=len(entries),
                )
            lines = [header]
            for entry in entries[:limit]:
                categories = entry.get("categories", [entry.get("category", "")])
                category_text = "、".join(str(item) for item in categories)
                tags = entry.get("tags", [])
                tag_text = ""
                if isinstance(tags, (list, tuple)) and tags:
                    tag_text = " tags=" + ",".join(str(item) for item in tags[:6])
                meaning = str(entry.get("meaning", "") or "").strip()
                if meaning:
                    tag_text += " 含义=" + meaning[:40]
                lines.append(
                    f"- [{category_text}] {entry.get('filename', '')} "
                    f"({entry.get('width', 0)}×{entry.get('height', 0)}){tag_text}"
                )
            if len(entries) > limit:
                lines.append(
                    self._format(
                        self._cfg_str("collected_sticker_list_more"),
                        count=len(entries) - limit,
                    )
                )
            yield self._plain_result(event, "\n".join(lines))
        except Exception:
            yield self._plain_result(
                event, self._cfg_str("collected_sticker_command_error_reply")
            )
        finally:
            self._stop(event)

    def _resolve_command_filter(self, raw: str) -> tuple[str, str, str] | None:
        value = str(raw or "").strip()
        if not value:
            return "", "", ""
        category = self._normalize_category(value)
        return "", value, ""

    @filter.command("发表情", priority=110)
    async def send_sticker_command(self, event: AstrMessageEvent):
        self._suppress_model(event)
        try:
            if not self._cfg_bool("enabled", True) or not self._cfg_bool(
                "collected_sticker_enabled", True
            ):
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_disabled_reply")
                )
                return
            if not self._cfg_bool("sticker_enabled", True):
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_not_found_reply")
                )
                return
            raw = self._command_argument(event, ("发表情",))
            entries = await self._entries()
            if not entries:
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_empty_reply")
                )
                return
            category = ""
            emotion = ""
            tag = ""
            if raw:
                category_names = {
                    str(value).casefold()
                    for entry in entries
                    for value in (
                        entry.get("categories")
                        if isinstance(entry.get("categories"), (list, tuple, set))
                        else [entry.get("category", "")]
                    )
                }
                if raw.casefold() in category_names:
                    category = raw
                else:
                    try:
                        emotion = sticker_intent_engine.resolve_sticker_target(
                            raw,
                            available_emotions={emotion for _words, emotion in sticker_intent_engine.EMOTION_KEYWORDS},
                            available_categories=category_names,
                        ).get("value", "")
                    except Exception:
                        emotion = ""
                    if not emotion:
                        tag = raw
                candidates = self._filter_entries(
                    entries, category=category, emotion=emotion, tag=tag
                )
            else:
                candidates = entries
            chosen, reason = self._choose_entry(event, candidates)
            if chosen is None:
                key = (
                    "sticker_natural_language_cooldown_reply"
                    if reason == "cooldown"
                    else (
                        "collected_sticker_not_found_reply"
                        if raw
                        else "collected_sticker_empty_reply"
                    )
                )
                yield self._plain_result(event, self._cfg_str(key))
                return
            try:
                image = self._image_component(Path(chosen["path"]))
            except Exception:
                yield self._plain_result(
                    event, self._cfg_str("collected_sticker_command_error_reply")
                )
                return
            prefix = self._format(
                self._cfg_str("collected_sticker_send_prefix"),
                category=chosen.get("category", DEFAULT_COLLECTED_CATEGORY),
                filename=chosen.get("filename", ""),
            )
            yield self._chain_result(event, [Comp.Plain(prefix), image])
        except Exception:
            logger.warning("qqbot_stickers /发表情 failed", exc_info=True)
            yield self._plain_result(
                event, self._cfg_str("collected_sticker_command_error_reply")
            )
        finally:
            self._stop(event)

    # ------------------------------------------------------------------
    # Natural-language handler and optional AI fallback
    # ------------------------------------------------------------------
    @filter.event_message_type(filter.EventMessageType.ALL, priority=120)
    async def on_message(self, event: AstrMessageEvent):
        """Handle deterministic sticker intents and cache recent images.

        This handler deliberately yields nothing for ordinary chat.  Model
        replies remain owned by qqbot_core_reply.
        """

        text = self._message_text(event)
        if self._is_command_message(text):
            return
        if not self._cfg_bool("enabled", True) or not self._cfg_bool(
            "collected_sticker_enabled", True
        ):
            return
        components = await self._event_messages(event)
        has_image = any(_is_image_component(item) for item in components)
        if has_image:
            await self._remember_recent_images(event, components)

        # Deterministic local auto-collect.  No model call is made for this
        # path; the bounded local heuristic is the fallback.  The legacy
        # non-AI flag is limited to explicit (private/@/wake) messages, while
        # the legacy AI flag retains its group-wide trigger but uses local
        # fallback semantics here.
        if (
            has_image
            and not str(text or "").strip()
            and self._cfg_bool("collected_sticker_enabled", True)
            and self._cfg_bool("enabled", True)
        ):
            explicit = self._is_explicit_conversation(event)
            user_auto = self._cfg_bool(
                "auto_collect_user_sticker_enabled", False
            )
            ai_auto = self._cfg_bool("sticker_ai_auto_collect_enabled", False)
            if ai_auto or (user_auto and explicit):
                handled = await self._maybe_auto_collect(
                    event, user_mode=(user_auto and explicit) or not ai_auto
                )
                if handled is not None:
                    self._suppress_model(event)
                    yield handled
                    self._stop(event)
                    return

        if not self._cfg_bool("sticker_natural_language_enabled", True):
            return
        if not self._is_explicit_conversation(event):
            return
        try:
            if self._cfg_bool(
                "sticker_natural_language_reclassify_enabled", True
            ):
                reclass_intent = sticker_reclassify_engine.detect_reclassify_request(
                    text
                )
                if reclass_intent is not None:
                    result = await self._handle_reclassify(event, reclass_intent)
                    if result is not None:
                        self._suppress_model(event)
                        yield result
                        self._stop(event)
                    return
            if self._cfg_bool("sticker_natural_language_collect_enabled", True):
                collect_intent = sticker_intent_engine.detect_collect_request(text)
                if collect_intent is not None:
                    result = await self._handle_natural_collect(event, collect_intent)
                    if result is not None:
                        self._suppress_model(event)
                        yield result
                        self._stop(event)
                    return
            send_entries = await self._entries()
            send_intent = sticker_intent_engine.detect_sticker_request(
                text,
                available_emotions={
                    emotion
                    for _words, emotion in sticker_intent_engine.EMOTION_KEYWORDS
                }
                | set(sticker_intent_engine.SNARK_EMOTIONS),
                available_categories=set(
                    CollectedStickerStore.categories(send_entries)
                ),
            )
            if (
                send_intent is not None
                and send_intent.get("kind") in {"not_found", "generic"}
                and str(send_intent.get("target", "")).strip()
            ):
                target = str(send_intent.get("target", "")).strip()
                if any(
                    self._entry_matches_tag(entry, target)
                    or self._entry_matches_emotion(entry, target)
                    for entry in send_entries
                ):
                    send_intent = {
                        "kind": "tag",
                        "value": target,
                        "target": target,
                    }
            if send_intent is not None:
                result = await self._handle_natural_send(event, send_intent)
                if result is not None:
                    self._suppress_model(event)
                    yield result
                    self._stop(event)
        except Exception:
            logger.warning("qqbot_stickers natural-language handling failed", exc_info=True)

    async def _maybe_auto_collect(
        self, event: AstrMessageEvent, *, user_mode: bool = True
    ):
        session = self._session_key(event)
        now = self._clock()
        cooldown = self._cfg_int("auto_collect_sticker_cooldown_seconds", 60, 0)
        last = self._last_auto_collect.get(session)
        if last is not None and now - last < cooldown:
            return None
        status, payload = await self._extract_image_payload(event)
        if status != "ok" or payload is None:
            return None
        max_bytes = self._cfg_int("auto_collect_sticker_max_file_bytes", 2_000_000, 1)
        max_dim = self._cfg_int("auto_collect_sticker_max_dimension", 512, 1)
        width = int(payload.get("width", 0) or 0)
        height = int(payload.get("height", 0) or 0)
        if (
            len(payload.get("data", b"")) > max_bytes
            or width <= 0
            or height <= 0
            or max(width, height) > max_dim
            or max(width, height) / max(1, min(width, height)) > 2.5
        ):
            return None
        user_auto_mode = bool(user_mode)
        if user_auto_mode:
            category_key = "auto_collect_sticker_default_category"
        else:
            category_key = (
                "sticker_ai_auto_collect_default_category"
                if self._cfg_bool("sticker_ai_auto_collect_enabled", False)
                else "auto_collect_sticker_default_category"
            )
        category = self._normalize_category(
            self._cfg_str(category_key, DEFAULT_COLLECTED_CATEGORY)
        ) or DEFAULT_COLLECTED_CATEGORY
        self._last_auto_collect[session] = now
        save_status, entry = await self._save_payload(event, payload, category)
        if save_status != "saved" or entry is None:
            return None
        self._remember_collected(event, entry)
        if user_auto_mode:
            if not self._cfg_bool("auto_collect_sticker_reply_enabled", True):
                return None
            template = self._cfg_str(
                "auto_collect_sticker_saved_reply",
                "收下了，已收藏到「{category}」。",
            )
        else:
            template = self._cfg_str(
                "sticker_ai_auto_collect_saved_reply",
                "这个表情我收下了（{category}）。",
            )
            if not template.strip():
                return None
        return self._plain_result(
            event,
            self._format(template, category=entry.get("category", category)),
        )

    async def _handle_natural_collect(
        self, event: AstrMessageEvent, intent: dict[str, str]
    ):
        if not self._cfg_bool("enabled", True) or not self._cfg_bool(
            "collected_sticker_enabled", True
        ):
            return None
        raw_category = str(intent.get("category", "") or "").strip()
        categories = self._parse_collect_categories(raw_category)
        if categories is None:
            return self._plain_result(
                event,
                self._format(
                    self._cfg_str("collected_sticker_invalid_category_reply"),
                    max_chars=self._category_max_chars(),
                ),
            )
        status, payload = await self._extract_image_payload(event)
        if status != "ok" or payload is None:
            return self._plain_result(event, self._collect_extraction_reply(status))
        save_status, entry = await self._save_payload(
            event, payload, categories[0], categories=categories
        )
        if entry is not None:
            self._remember_collected(event, entry)
        return self._plain_result(
            event, self._collect_status_reply(save_status, entry)
        )

    def _normalize_semantic_metadata(self, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        category = self._normalize_category(raw.get("category"))
        if category is None:
            return None
        raw_categories = raw.get("categories")
        if not isinstance(raw_categories, (list, tuple, set)):
            raw_categories = [category]
        categories: list[str] = []
        for value in raw_categories:
            normalized = self._normalize_category(value)
            if normalized is None:
                return None
            if normalized not in categories:
                categories.append(normalized)
        if category in categories:
            categories.remove(category)
        categories.insert(0, category)
        max_categories = self._cfg_int("collected_sticker_max_categories", 8, 1)
        if not categories or len(categories) > max_categories:
            return None
        raw_tags = raw.get("tags", [])
        if not isinstance(raw_tags, (list, tuple, set)):
            raw_tags = []
        tags: list[str] = []
        max_tags = self._cfg_int("collected_sticker_max_tags", 12, 0)
        max_tag_chars = self._cfg_int("collected_sticker_max_tag_chars", 24, 1)
        if max_tags <= 0:
            raw_tags = []
        for raw_tag in raw_tags:
            tag = str(raw_tag or "").strip()
            if not tag:
                continue
            if len(tag) > max_tag_chars or not re.fullmatch(
                r"[0-9A-Za-z_\-\u4e00-\u9fff]+", tag
            ):
                continue
            if tag not in tags:
                tags.append(tag)
            if len(tags) >= max_tags:
                break
        meaning = str(raw.get("meaning", "") or "").strip()
        if len(meaning) > self._cfg_int(
            "collected_sticker_max_meaning_chars", 200, 1
        ) or any(ord(char) < 32 or ord(char) == 127 for char in meaning):
            meaning = ""
        tone = str(raw.get("tone", "") or "").strip()
        if len(tone) > self._cfg_int("collected_sticker_max_tone_chars", 24, 1):
            tone = ""
        if tone and not re.fullmatch(r"[0-9A-Za-z_\-\u4e00-\u9fff]+", tone):
            tone = ""
        if not self._cfg_bool("sticker_semantic_tags_enabled", True):
            tags, meaning, tone = [], "", ""
        return {
            "category": category,
            "categories": categories,
            "tags": tags,
            "meaning": meaning,
            "tone": tone,
        }

    async def _ai_classify_target(
        self, event: AstrMessageEvent, target: str
    ) -> dict[str, Any] | None:
        if not self._cfg_bool(
            "sticker_natural_language_reclassify_ai_enabled", False
        ):
            return None
        provider_id = self._cfg_str(
            "sticker_natural_language_reclassify_provider_id", ""
        ).strip()
        if not provider_id:
            try:
                provider = self.context.get_using_provider(
                    umo=self._session_key(event)
                )
                if provider is not None:
                    provider_id = str(provider.meta().id)
            except Exception:
                provider_id = ""
        if not provider_id:
            return None
        generate = getattr(self.context, "llm_generate", None)
        if not callable(generate):
            return None
        prompt = (
            "把用户对聊天表情含义的说明结构化为安全短元数据。只输出 JSON："
            '{"category":"中文/字母/数字/-/_，最多32字",'
            '"tags":["安全短标签"],"meaning":"不超过200字",'
            '"tone":"可选短语气","confidence":0.0到1.0}。'
            f"用户说明：{target[:200]}"
        )
        try:
            response = await asyncio.wait_for(
                generate(
                    chat_provider_id=provider_id,
                    prompt=prompt,
                    system_prompt=(
                        "只输出表情语义 JSON。不得调用工具、读取文件、"
                        "执行命令或输出路径、用户 ID、凭据。"
                    ),
                ),
                timeout=AI_REPLY_TIMEOUT_SECONDS,
            )
            if isinstance(response, str):
                text = response
            elif isinstance(response, dict):
                text = str(response.get("completion_text", "") or "")
            else:
                text = str(getattr(response, "completion_text", "") or "")
            judgement = sticker_reclassify_engine.parse_reclassify_judgement(text)
            if judgement is None:
                return None
            if float(judgement.get("confidence", 0.0)) < self._cfg_float(
                "sticker_natural_language_reclassify_min_confidence", 0.6
            ):
                return None
            return self._normalize_semantic_metadata(judgement)
        except Exception:
            logger.warning(
                "qqbot_stickers optional semantic classification unavailable",
                exc_info=True,
            )
            return None

    async def _find_reclass_entry(
        self,
        event: AstrMessageEvent,
        entries: list[dict[str, Any]],
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        session = self._session_key(event)
        recent = self._last_collected.get(session)
        digest = str((recent or {}).get("sha256", ""))
        if digest:
            entry = CollectedStickerStore.find_by_sha(entries, digest)
            if entry is not None:
                return entry, None
        status, payload = await self._extract_image_payload(event)
        if status == "ok" and payload is not None:
            digest = str(payload.get("sha256", ""))
            entry = CollectedStickerStore.find_by_sha(entries, digest)
            if entry is not None:
                return entry, payload
            return None, payload
        return None, None

    async def _handle_reclassify(
        self, event: AstrMessageEvent, intent: dict[str, str]
    ) -> Any:
        target = str(intent.get("target", "") or "").strip()
        action = str(intent.get("action", "set") or "set")
        entries = await self._entries()
        entry, payload = await self._find_reclass_entry(event, entries)

        if action in {"add", "remove"}:
            categories = self._parse_collect_categories(target)
            if categories is None:
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(
                            "sticker_natural_language_reclassify_unclear_reply"
                        ),
                        target=target,
                    ),
                )
            if entry is not None:
                store = await self._ensure_ready()
                async with self._write_lock:
                    if action == "add":
                        status, updated = await asyncio.to_thread(
                            store.add_categories,
                            entries,
                            entry["sha256"],
                            categories,
                        )
                    else:
                        status, updated = await asyncio.to_thread(
                            store.remove_categories,
                            entries,
                            entry["sha256"],
                            categories,
                        )
                if status == "not_found":
                    return self._plain_result(
                        event,
                        self._cfg_str(
                            "sticker_natural_language_reclassify_not_found_reply"
                        ),
                    )
                if status not in {"updated", "same"}:
                    return self._plain_result(
                        event,
                        self._cfg_str(
                            "sticker_natural_language_reclassify_failed_reply"
                        ),
                    )
                if updated is not None:
                    self._remember_collected(event, updated)
                if status == "same":
                    return self._plain_result(
                        event,
                        self._cfg_str(
                            "sticker_natural_language_reclassify_categories_same_reply"
                        ),
                    )
                key = (
                    "sticker_natural_language_reclassify_added_reply"
                    if action == "add"
                    else "sticker_natural_language_reclassify_removed_reply"
                )
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(key),
                        categories=", ".join(categories),
                    ),
                )
            if action == "remove":
                return self._plain_result(
                    event,
                    self._cfg_str(
                        "sticker_natural_language_reclassify_not_found_reply"
                    ),
                )
            if payload is None:
                return self._plain_result(
                    event,
                    self._cfg_str(
                        "sticker_natural_language_reclassify_not_found_reply"
                    ),
                )
            status, saved = await self._save_payload(
                event,
                payload,
                categories[0],
                categories=categories,
            )
            if saved is not None:
                self._remember_collected(event, saved)
            if status == "saved":
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(
                            "sticker_natural_language_reclassify_added_reply"
                        ),
                        categories=", ".join(categories),
                    ),
                )
            return self._plain_result(
                event,
                self._cfg_str("sticker_natural_language_reclassify_failed_reply"),
            )

        semantics = sticker_reclassify_engine.build_local_semantics(target)
        if semantics is None:
            semantics = await self._ai_classify_target(event, target)
        normalized = self._normalize_semantic_metadata(semantics)
        if normalized is None:
            return self._plain_result(
                event,
                self._format(
                    self._cfg_str(
                        "sticker_natural_language_reclassify_unclear_reply"
                    ),
                    target=target,
                ),
            )
        if entry is not None:
            store = await self._ensure_ready()
            old_category = str(entry.get("category", ""))
            async with self._write_lock:
                status, updated = await asyncio.to_thread(
                    store.update_metadata,
                    entries,
                    entry["sha256"],
                    category=normalized["category"],
                    categories=normalized["categories"],
                    tags=normalized["tags"],
                    meaning=normalized["meaning"],
                    tone=normalized["tone"],
                )
            if status in {"not_found", "invalid", "conflict", "storage"}:
                return self._plain_result(
                    event,
                    self._cfg_str(
                        "sticker_natural_language_reclassify_failed_reply"
                    ),
                )
            if updated is not None:
                self._remember_collected(event, updated)
            if status == "same":
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(
                            "sticker_natural_language_reclassify_same_reply"
                        ),
                        category=normalized["category"],
                    ),
                )
            if normalized["meaning"]:
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(
                            "sticker_natural_language_reclassify_memorized_reply"
                        ),
                        meaning=normalized["meaning"],
                        category=normalized["category"],
                    ),
                )
            if old_category != normalized["category"]:
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(
                            "sticker_natural_language_reclassify_moved_reply"
                        ),
                        category=normalized["category"],
                    ),
                )
            return self._plain_result(
                event,
                self._format(
                    self._cfg_str(
                        "sticker_natural_language_reclassify_same_reply"
                    ),
                    category=normalized["category"],
                ),
            )
        if payload is None:
            return self._plain_result(
                event,
                self._cfg_str(
                    "sticker_natural_language_reclassify_not_found_reply"
                ),
            )
        status, saved = await self._save_payload(
            event,
            payload,
            normalized["category"],
            categories=normalized["categories"],
            tags=normalized["tags"],
            meaning=normalized["meaning"],
            tone=normalized["tone"],
        )
        if saved is not None:
            self._remember_collected(event, saved)
        if status in {"saved", "duplicate"}:
            if normalized["meaning"]:
                return self._plain_result(
                    event,
                    self._format(
                        self._cfg_str(
                            "sticker_natural_language_reclassify_memorized_reply"
                        ),
                        meaning=normalized["meaning"],
                        category=normalized["category"],
                    ),
                )
            return self._plain_result(
                event,
                self._format(
                    self._cfg_str(
                        "sticker_natural_language_reclassify_cached_reply"
                    ),
                    category=normalized["category"],
                ),
            )
        return self._plain_result(
            event,
            self._cfg_str("sticker_natural_language_reclassify_failed_reply"),
        )

    async def _handle_natural_send(
        self, event: AstrMessageEvent, intent: dict[str, str]
    ) -> Any:
        if (
            not self._cfg_bool("enabled", True)
            or not self._cfg_bool("collected_sticker_enabled", True)
            or not self._cfg_bool("sticker_enabled", True)
        ):
            return None
        kind = str(intent.get("kind", "generic"))
        if kind == "not_found":
            return self._plain_result(
                event,
                self._format(
                    self._cfg_str("sticker_natural_language_not_found_reply"),
                    target=intent.get("target", ""),
                ),
            )
        entries = await self._entries()
        candidates = entries
        if kind == "category":
            candidates = self._filter_entries(
                entries, category=str(intent.get("value", ""))
            )
        elif kind == "emotion":
            candidates = self._filter_entries(
                entries, emotion=str(intent.get("value", ""))
            )
        elif kind == "tag":
            candidates = self._filter_entries(
                entries, tag=str(intent.get("value", ""))
            )
        chosen, reason = self._choose_entry(event, candidates)
        if chosen is None:
            if reason == "cooldown":
                return self._plain_result(
                    event,
                    self._cfg_str("sticker_natural_language_cooldown_reply"),
                )
            return self._plain_result(
                event, self._cfg_str("sticker_natural_language_empty_reply")
            )
        try:
            image = self._image_component(Path(chosen["path"]))
        except Exception:
            return self._plain_result(
                event, self._cfg_str("sticker_natural_language_empty_reply")
            )
        prefix = self._cfg_str(
            "sticker_natural_language_send_prefix", "给你一个表情："
        )
        return self._chain_result(event, [Comp.Plain(prefix), image])

    # ------------------------------------------------------------------
    # Core-reply decoration (priority 100)
    # ------------------------------------------------------------------
    @filter.on_decorating_result(priority=100)
    async def decorate_core_result(self, event: AstrMessageEvent) -> None:
        """Append zero or one sticker to explicitly marked core replies only."""

        try:
            if not self._cfg_bool("enabled", True):
                return
            if not self._cfg_bool("sticker_enabled", True) or not self._cfg_bool(
                "sticker_decorate_enabled", True
            ):
                return
            if not self._cfg_bool("collected_sticker_enabled", True):
                return
            getter = getattr(event, "get_extra", None)
            if not callable(getter):
                return
            if not _as_bool(getter("qqbot_core_reply_candidate", False)):
                return
            if not _as_bool(getter("qqbot_core_reply_allow_sticker", False)):
                return
            if _as_bool(getter("qqbot_safe_tools_processed", False)):
                return
            for command_marker in (
                "qqbot_stickers_command_result",
                "qqbot_command_result",
                "qqbot_core_reply_command_result",
                "qqbot_core_reply_is_command",
            ):
                if _as_bool(getter(command_marker, False)):
                    return
            # A command result must never be decorated, even if a mistaken
            # upstream marked it as a core candidate.
            if self._is_command_message(self._message_text(event)):
                return
            if _as_bool(getattr(event, "is_command", False)):
                return
            result = event.get_result() if callable(getattr(event, "get_result", None)) else None
            if result is not None and _as_bool(
                getattr(result, "is_command", False)
            ):
                return
            chain = getattr(result, "chain", None) if result is not None else None
            if not isinstance(chain, list) or not chain:
                return
            if _chain_has_image(chain):
                return
            plain_text = _chain_text(chain)
            # Detect only; never strip, execute or otherwise parse markers.
            if _has_tool_marker(plain_text):
                return
            if _as_bool(getter("qqbot_stickers_decorated", False)):
                return
            if "sticker_decorate_probability" in self._plugin_config:
                probability = self._cfg_float("sticker_decorate_probability", 1.0)
            else:
                probability = self._cfg_float("sticker_probability", 1.0)
            if probability < 1.0 and self._rng.random() >= probability:
                return
            entries = await self._entries()
            if not entries:
                return
            chosen, _reason = self._choose_entry(event, entries)
            if chosen is None:
                return
            try:
                image = self._image_component(Path(chosen["path"]))
            except Exception:
                return
            chain.append(image)
            setter = getattr(event, "set_extra", None)
            if callable(setter):
                try:
                    setter("qqbot_stickers_decorated", True)
                except Exception:
                    pass
        except Exception:
            logger.warning("qqbot_stickers core-reply decoration skipped", exc_info=True)

    # ------------------------------------------------------------------
    # Stable cross-plugin API used by qqbot_safe_tools
    # ------------------------------------------------------------------
    async def tool_send_sticker(
        self,
        event: AstrMessageEvent,
        *,
        category: str = "",
        emotion: str = "",
        tag: str = "",
    ):
        """Return one image result or ``None``.  Never raises."""

        try:
            if not self._cfg_bool("enabled", True):
                return None
            if not self._cfg_bool("safe_tools_allow_send_sticker", True):
                return None
            if not self._cfg_bool("sticker_enabled", True) or not self._cfg_bool(
                "collected_sticker_enabled", True
            ):
                return None
            entries = await self._entries()
            candidates = self._filter_entries(
                entries,
                category=str(category or ""),
                emotion=str(emotion or ""),
                tag=str(tag or ""),
            )
            chosen, _reason = self._choose_entry(event, candidates)
            if chosen is None:
                return None
            image = self._image_component(Path(chosen["path"]))
            prefix = self._cfg_str(
                "safe_tools_sticker_prefix",
                self._cfg_str("sticker_natural_language_send_prefix", "给你一个表情："),
            )
            return self._chain_result(event, [Comp.Plain(prefix), image])
        except Exception:
            logger.warning("qqbot_stickers tool_send_sticker failed safely", exc_info=True)
            return None

    async def tool_collect_sticker(
        self, event: AstrMessageEvent, *, category: str = ""
    ):
        """Collect the current/quoted/recent image; return a result or ``None``."""

        try:
            if not self._cfg_bool("enabled", True):
                return None
            if not self._cfg_bool("safe_tools_allow_collect_sticker", True):
                return None
            if not self._cfg_bool("collected_sticker_enabled", True):
                return None
            session = self._session_key(event)
            now = self._clock()
            cooldown = self._cfg_int(
                "safe_tools_collect_cooldown_seconds", 30, 0
            )
            last = self._last_tool_collect.get(session)
            if last is not None and now - last < cooldown:
                return None
            categories = self._parse_collect_categories(category)
            if categories is None:
                return None
            status, payload = await self._extract_image_payload(event)
            if status != "ok" or payload is None:
                return None
            save_status, entry = await self._save_payload(
                event, payload, categories[0], categories=categories
            )
            if save_status != "saved" or entry is None:
                return None
            self._last_tool_collect[session] = now
            self._remember_collected(event, entry)
            return self._plain_result(
                event,
                self._format(
                    self._cfg_str("auto_collect_sticker_saved_reply"),
                    category=entry.get("category", categories[0]),
                ),
            )
        except Exception:
            logger.warning("qqbot_stickers tool_collect_sticker failed safely", exc_info=True)
            return None

    async def tool_list_categories(self, event: AstrMessageEvent):
        """Return a bounded category/count summary or ``None``.  Never raises."""

        try:
            if not self._cfg_bool("enabled", True):
                return None
            if not self._cfg_bool("collected_sticker_enabled", True):
                return None
            if not self._cfg_bool("safe_tools_allow_list_categories", True):
                return None
            entries = await self._entries()
            if not entries:
                return self._plain_result(
                    event, self._cfg_str("collected_sticker_empty_reply")
                )
            counts = CollectedStickerStore.categories(entries)
            unique = len({str(item.get("sha256", "")) for item in entries})
            limit = self._cfg_int("collected_sticker_list_limit", 30, 1)
            ordered = sorted(counts)
            text = (
                f"收藏表情共 {unique} 张，分类 {len(counts)} 个："
                + "、".join(
                    f"{name}({counts[name]})" for name in ordered[:limit]
                )
            )
            if len(ordered) > limit:
                text += f"；另有 {len(ordered) - limit} 个分类"
            return self._plain_result(event, text)
        except Exception:
            logger.warning("qqbot_stickers tool_list_categories failed safely", exc_info=True)
            return None
