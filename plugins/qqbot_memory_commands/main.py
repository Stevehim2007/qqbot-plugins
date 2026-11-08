"""Standalone deterministic memory/session/persona command plugin.

One handler owns ``/记忆`` and every registered root alias.  It suppresses the
default model, reads plugin settings only from AstrBot's injected config, and
forwards canonical operations to the fixed ABI-v1 provider.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from . import commands, contracts, memory_commands
from .backend import AstrBotBackend
from .provider import MemoryCommandsProvider, cancel_pending_confirmation
from .store import OperationStore

_PROVIDER_ID = "qqbot_memory_commands"
_REGISTRY_ATTR = "_qqbot_memory_provider_registry"
_FALLBACK_REGISTRIES: dict[int, contracts.ProviderRegistry] = {}

_PROVIDER_METHODS = {
    ("memory", "view"): "memory_read",
    ("memory", "search"): "memory_search",
    ("memory", "add"): "memory_add",
    ("memory", "preview_clear"): "memory_preview_clear",
    ("memory", "confirm"): "memory_confirm_clear",
    ("session", "status"): "session_read",
    ("session", "read"): "session_read",
    ("session", "open"): "session_open",
    ("session", "switch"): "session_switch",
    ("session", "reset"): "session_reset",
    ("persona", "list"): "persona_list",
    ("persona", "switch"): "persona_switch",
    ("persona", "bind"): "persona_bind",
}

_LEGACY_MEMORY_ACTIONS = {"status", "delete", "forget", "tidy"}


def _plugin_config(config: Any) -> Mapping[str, Any]:
    """Return the injected plugin config as a mapping (never global config)."""

    if isinstance(config, Mapping):
        return config
    try:
        converted = dict(config)
    except Exception:  # noqa: BLE001 - malformed injected config means disabled
        return {}
    return converted


def _event_text(event: AstrMessageEvent) -> str:
    return str(getattr(event, "message_str", "") or "")


def _stop_event(event: AstrMessageEvent) -> None:
    try:
        event.stop_event()
    except Exception:  # noqa: BLE001 - shutdown must not mask the reply path
        pass


def _global_config(context: Any) -> Mapping[str, Any]:
    try:
        config = context.get_config()
        if isinstance(config, Mapping):
            return config
    except Exception:  # noqa: BLE001 - optional legacy Context API
        pass
    return {}


def _build_backend(context: Any, config: Mapping[str, Any]) -> AstrBotBackend:
    """Resolve optional public APIs only; tests may monkeypatch this function."""

    memory_core = memory_commands.load_core_memory()
    memory_service = None
    if memory_core is not None and callable(
        getattr(memory_core, "get_memory_service", None)
    ):
        try:
            memory_service = memory_core.get_memory_service(_global_config(context))
        except Exception:  # noqa: BLE001 - memory remains dependency_missing
            memory_service = None
    return AstrBotBackend(
        memory_service=memory_service,
        conversation_manager=getattr(context, "conversation_manager", None),
        persona_manager=getattr(context, "persona_manager", None),
    )


def _store_path() -> str:
    """Use the public StarTools data directory when available."""

    try:
        from astrbot.api.star import StarTools  # noqa: WPS433 - optional runtime

        getter = getattr(StarTools, "get_data_dir", None)
        if callable(getter):
            data_dir = Path(
                getter(plugin_name="qqbot_memory_commands")
            ).expanduser()
            data_dir.mkdir(parents=True, exist_ok=True)
            return str(data_dir / "contracts.sqlite3")
    except Exception:  # noqa: BLE001 - in-memory ledger is the safe fallback
        pass
    configured = str(os.environ.get("QQBOT_MEMORY_LEDGER_PATH") or "").strip()
    return configured or ":memory:"


def _build_store(context: Any, config: Mapping[str, Any]) -> OperationStore:
    # Deliberately never use backend.store_id as a filesystem path.
    return OperationStore(_store_path())


def _registry_for_context(context: Any) -> contracts.ProviderRegistry:
    registry = getattr(context, _REGISTRY_ATTR, None)
    if isinstance(registry, contracts.ProviderRegistry):
        return registry
    registry = contracts.ProviderRegistry()
    try:
        setattr(context, _REGISTRY_ATTR, registry)
        return registry
    except Exception:  # noqa: BLE001 - retain one process-local registry
        key = id(context)
        existing = _FALLBACK_REGISTRIES.get(key)
        if isinstance(existing, contracts.ProviderRegistry):
            return existing
        _FALLBACK_REGISTRIES[key] = registry
        return registry


class MemoryCommandsPlugin(Star):
    """One handler for ``/记忆`` plus session/persona command aliases."""

    def __init__(
        self,
        context: Context,
        config: Mapping[str, Any] | None = None,
    ):
        super().__init__(context)
        self.config = _plugin_config(config)
        # Compatibility alias for the pre-ABI standalone package draft.
        self.plugin_config = self.config
        self.backend = _build_backend(context, self.config)
        self.store = _build_store(context, self.config)
        self.provider = MemoryCommandsProvider(
            self.backend,
            self.store,
            config=self.config,
        )
        self.registry = _registry_for_context(context)
        self.provider_id = self.registry.register(
            self.provider,
            provider_id=_PROVIDER_ID,
        )
        self.registry.mark_ready(
            self.provider_id,
            ready=bool(getattr(self.backend, "ready", False)),
        )
        self._terminated = False

    def _capability_enabled(self, event: AstrMessageEvent) -> bool:
        """Return False only when safe_tools explicitly disables memory commands."""
        try:
            getter = getattr(self.context, "get_registered_star", None)
            meta = getter("qqbot_safe_tools") if callable(getter) else None
            if meta is None:
                return True
            star = getattr(meta, "star_cls", None) or meta
            policy_fn = getattr(star, "policy_effective", None)
            if not callable(policy_fn):
                return True
            private = False
            try:
                private = bool(event.is_private_chat())
            except Exception:
                private = False
            group_id = ""
            try:
                group_id = str(event.get_group_id() or "").strip()
            except Exception:
                group_id = ""
            sender_id = ""
            try:
                sender_id = str(event.get_sender_id() or "").strip()
            except Exception:
                sender_id = ""
            policy = policy_fn(
                "qqbot_memory_commands",
                group_id=None if private else (group_id or None),
                user_id=sender_id or None,
                is_private=private,
            )
            return not isinstance(policy, dict) or bool(policy.get("enabled", True))
        except Exception:
            return True

    # ------------------------------------------------------------------
    # command handler
    # ------------------------------------------------------------------
    @filter.command(
        "记忆",
        alias={"记忆管理", "memory", "会话", "session", "人格", "persona"},
        priority=110,
    )
    async def memory_command(self, event: AstrMessageEvent):
        """Answer one fixed management command without the main model."""

        reply = memory_commands.HANDLER_FAILED_REPLY
        try:
            # AstrBot's True flag means "handled; do not call the default LLM".
            event.should_call_llm(True)
            if not self._capability_enabled(event):
                return
            reply = await self._dispatch(event)
        except Exception:  # noqa: BLE001 - bounded Chinese failure, no traceback
            reply = memory_commands.HANDLER_FAILED_REPLY

        try:
            yield event.plain_result(reply)
        except Exception:  # noqa: BLE001 - transport failure cannot be re-sent
            pass
        finally:
            _stop_event(event)

    async def _dispatch(self, event: AstrMessageEvent) -> str:
        parsed = commands.parse_management_command(_event_text(event))
        options = memory_commands.options_from_config(self.config)
        if parsed.action in {"empty", "help"}:
            return memory_commands._bounded(
                commands.help_text(parsed.root or "memory"),
                options,
            )
        if parsed.action == "parse_error":
            return memory_commands._bounded(
                f"参数错误：{parsed.error}。{commands.help_text(parsed.root)}",
                options,
            )
        if parsed.action == "unknown":
            return memory_commands._bounded(
                f"未知命令。{commands.help_text(parsed.root or 'memory')}",
                options,
            )

        if parsed.root == "memory" and parsed.action == "cancel":
            return self._cancel_confirmation(event, parsed, options)
        if parsed.root == "memory" and parsed.action in _LEGACY_MEMORY_ACTIONS:
            return self._legacy_reply(event, options)
        if parsed.root == "memory" and parsed.action == "search":
            if not self._provider_ready() and "scope" not in dict(parsed.options):
                return self._legacy_reply(event, options)

        return await self._provider_reply(event, parsed, options)

    def _provider_ready(self) -> bool:
        return bool(self.registry.is_ready(self.provider_id))

    def _legacy_reply(
        self,
        event: AstrMessageEvent,
        options: memory_commands.Options,
    ) -> str:
        return memory_commands.handle_text(
            text=_event_text(event),
            prefixes=("/",),
            event=event,
            context=self.context,
            options=options,
            core=memory_commands.load_core_memory(),
        )

    def _cancel_confirmation(
        self,
        event: AstrMessageEvent,
        parsed: commands.ParsedManagementCommand,
        options: memory_commands.Options,
    ) -> str:
        requested_scope = "group" if not bool(
            getattr(event, "is_private_chat", lambda: True)()
        ) else "user"
        context = commands.build_request_context(
            event,
            self.config,
            capability="memory.confirm_clear",
            requested_scope=requested_scope,
        )
        args, error = commands.build_provider_args(parsed, context)
        if error:
            return memory_commands._bounded(error, options)
        token = str(args.get("confirmation_token") or "")
        result = cancel_pending_confirmation(self.store, context, token)
        return memory_commands._bounded(
            commands.render_command_result(
                parsed,
                result,
                content_chars=options.content_chars,
                max_reply_chars=options.max_reply_chars,
                max_items=options.max_results,
            ),
            options,
        )

    async def _provider_reply(
        self,
        event: AstrMessageEvent,
        parsed: commands.ParsedManagementCommand,
        options: memory_commands.Options,
    ) -> str:
        initial_scope = "group" if not bool(
            getattr(event, "is_private_chat", lambda: True)()
        ) else "user"
        if parsed.root in {"session", "persona"}:
            initial_scope = "conversation" if parsed.root == "session" else "persona"
        context = commands.build_request_context(
            event,
            self.config,
            capability=commands.command_capability(parsed),
            requested_scope=initial_scope,
        )
        args, error = commands.build_provider_args(parsed, context)
        if error:
            return memory_commands._bounded(error, options)
        context = commands.build_request_context(
            event,
            self.config,
            capability=commands.command_capability(parsed),
            requested_scope=str(args.get("scope") or initial_scope),
        )
        method = _PROVIDER_METHODS.get((parsed.root, parsed.action))
        if method is None:
            return memory_commands._bounded(
                f"未知命令。{commands.help_text(parsed.root)}",
                options,
            )
        result = await getattr(self.provider, method)(context, args)
        return memory_commands._bounded(
            commands.render_command_result(
                parsed,
                result,
                content_chars=options.content_chars,
                max_reply_chars=options.max_reply_chars,
                max_items=options.max_results,
            ),
            options,
        )

    async def terminate(self):
        """Unregister this provider and close its local ledger."""

        if self._terminated:
            return
        self._terminated = True
        try:
            self.registry.terminate(self.provider_id)
        finally:
            try:
                self.store.close()
            except Exception:  # noqa: BLE001 - termination stays best effort
                pass
