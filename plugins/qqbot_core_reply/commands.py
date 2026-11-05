"""Canonical command services for core_reply.

The AstrBot handlers in main.py are adapters only.  This module resolves
deterministic subcommands, returns terminal Chinese errors for unknown input,
and never forwards a command to the model.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:  # package import
    from . import command_exclusion as command_engine
    from .songs import SONG_SUFFIXES, list_song_files
    from .contracts import ContractResponse
except ImportError:  # test harness
    import command_exclusion as command_engine
    from songs import SONG_SUFFIXES, list_song_files
    from contracts import ContractResponse


@dataclass
class CommandReply:
    kind: str = "plain"
    text: str = ""
    chain: list[Any] = field(default_factory=list)


class CommandService:
    def __init__(
        self,
        context: Any,
        config: Any,
        state: Any,
        persona: Any,
        conversation: Any,
        gateway: Any,
        *,
        song_dir_getter: Callable[[], Path],
        set_ready: Callable[[bool], None] | None = None,
    ) -> None:
        self.context = context
        self.config = config
        self.state = state
        self.persona = persona
        self.conversation = conversation
        self.gateway = gateway
        self.song_dir_getter = song_dir_getter
        self.set_ready = set_ready or (lambda _ready: None)

    # ------------------------------------------------------------------
    # Root/subcommand parsing
    # ------------------------------------------------------------------
    def _match(self, event: Any) -> Any:
        prefixes = self.conversation_prefixes(event)
        raw = str(getattr(event, "message_str", "") or "")
        return command_engine.parse_root_command(
            raw,
            prefixes,
            extra_aliases=self.config.string_list("commands.exclude_aliases"),
        )

    def conversation_prefixes(self, event: Any) -> list[str]:
        return command_engine.command_prefixes(
            self.config.string_list("ignore_command_prefixes"),
            inherit_wake_prefix=self.config.bool("inherit_astrbot_wake_prefix", True),
            global_wake_prefix=self._global_wake_prefix(event),
        )

    def _global_wake_prefix(self, event: Any) -> Any:
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return []
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            config = getter(umo=umo)
        except TypeError:
            try:
                config = getter()
            except Exception:
                return []
        except Exception:
            return []
        if isinstance(config, dict):
            return config.get("wake_prefix", [])
        try:
            return config.get("wake_prefix", [])
        except Exception:
            return getattr(config, "wake_prefix", [])

    # ------------------------------------------------------------------
    # /回复
    # ------------------------------------------------------------------
    async def reply_control(self, event: Any) -> str:
        match = self._match(event)
        if match is None or match.operation != "reply":
            return "用法：/回复 状态|设置|启用|停用"
        try:
            subcommand = command_engine.parse_reply_subcommand(match.arguments)
        except command_engine.CommandParseError as exc:
            return exc.message
        if subcommand.action == "status":
            return self._status_text()
        if subcommand.action == "enable":
            return self._set_enabled(True)
        if subcommand.action == "disable":
            return self._set_enabled(False)
        return self._apply_set(subcommand.arguments)

    def _status_text(self) -> str:
        enabled = self.config.bool("enabled", False)
        mode = self.persona.memory_mode()
        probability = self.config.effective_float(
            "participation.group_probability",
            "reply_probability",
            default=0.12,
            minimum=0.0,
            maximum=1.0,
        )
        group_cooldown, user_cooldown = self._cooldowns()
        return "\n".join(
            [
                f"回复状态：{'启用' if enabled else '停用'}；provider ready={'是' if self.state.ready else '否'}",
                f"模式：{mode}；群概率 {probability:.0%}；群冷却 {group_cooldown:g}s；用户冷却 {user_cooldown:g}s",
                (
                    f"最短 {self.config.int('min_message_chars', 0)} 字；"
                    f"最长 {self.config.int('max_reply_chars', 1)} 字；"
                    f"ambient {'开启' if self.config.bool('ambient_group_chat_enabled') else '关闭'}"
                ),
                f"owner_epoch={self.state.owner_epoch}；config_revision={self.state.config_revision}",
            ]
        )

    def _set_enabled(self, enabled: bool) -> str:
        self.config.set_overrides({"enabled": bool(enabled)})
        self.state.config_revision += 1
        self.state.set_ready(bool(enabled))
        self.set_ready(bool(enabled))
        return "核心回复已启用。" if enabled else "核心回复已停用；不会回落到默认 LLM。"

    def _apply_set(self, arguments: str) -> str:
        if not arguments:
            return (
                "用法：/回复 设置 概率 0.2 | 群冷却 30 | 用户冷却 90 | "
                "最短 3 | 最长 500 | 模式 direct|astrbot"
            )
        token, _, raw_value = arguments.partition(" ")
        value = raw_value.strip()
        key = {
            "概率": "participation.group_probability",
            "probability": "participation.group_probability",
            "group_probability": "participation.group_probability",
            "群冷却": "cooldown.seconds",
            "group_cooldown": "cooldown.seconds",
            "用户冷却": "user_cooldown_seconds",
            "user_cooldown": "user_cooldown_seconds",
            "最短": "min_message_chars",
            "min_chars": "min_message_chars",
            "最长": "max_reply_chars",
            "max_chars": "max_reply_chars",
            "模式": "memory_mode",
            "mode": "memory_mode",
        }.get(token)
        if key is None:
            return (
                f"未知的设置项「{token}」。用法：/回复 设置 概率|群冷却|"
                "用户冷却|最短|最长|模式 <值>"
            )
        if not value:
            return f"设置项「{token}」缺少值。"
        if key == "memory_mode":
            mode = value.strip().lower()
            if mode not in {"direct", "astrbot", "memory"}:
                return "模式只能是 direct 或 astrbot。"
            self.config.set_overrides({"memory_mode": "astrbot" if mode in {"astrbot", "memory"} else "direct"})
        elif key in {"participation.group_probability"}:
            try:
                number = float(value)
            except ValueError:
                return "概率必须是 0 到 1 之间的数字。"
            if not 0 <= number <= 1:
                return "概率必须是 0 到 1 之间的数字。"
            self.config.set_overrides({key: number})
        else:
            try:
                number = int(value)
            except ValueError:
                return f"设置项「{token}」必须是整数。"
            if number < 0:
                return f"设置项「{token}」不能为负数。"
            self.config.set_overrides({key: number})
        self.state.config_revision += 1
        return f"已更新设置「{token}」。"

    # ------------------------------------------------------------------
    # /冷却
    # ------------------------------------------------------------------
    async def cooldown_status(self, event: Any) -> str:
        match = self._match(event)
        if match is None or match.operation != "cooldown":
            return "用法：/冷却 状态"
        try:
            command_engine.parse_cooldown_subcommand(match.arguments)
        except command_engine.CommandParseError as exc:
            return exc.message
        group_cooldown, user_cooldown = self._cooldowns()
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        remaining = self._remaining(session_key, f"{session_key}:{sender_id}", group_cooldown, user_cooldown)
        song_group = self.config.int("song_group_cooldown_seconds", 0)
        song_user = self.config.int("song_user_cooldown_seconds", 0)
        return "\n".join(
            [
                f"冷却状态：群 {group_cooldown:g}s / 用户 {user_cooldown:g}s；"
                f"当前会话剩余 {remaining}s",
                f"点歌冷却：群 {song_group}s / 用户 {song_user}s",
                f"key_mode={self.config.string('cooldown.key_mode', 'legacy_compatible')}",
            ]
        )

    def _cooldowns(self) -> tuple[float, float]:
        group = self.config.effective_float(
            "cooldown.seconds",
            "group_cooldown_seconds",
            default=30.0,
            minimum=0.0,
            maximum=3600.0,
        )
        user = float(self.config.int("user_cooldown_seconds", 0, 3600))
        mode = self.config.string("cooldown.key_mode", "legacy_compatible").lower()
        if mode == "user":
            group = 0.0
        elif mode in {"group", "conversation", "conversation_persona", "instance"}:
            user = 0.0
        return group, user

    def _remaining(
        self,
        session_key: str,
        user_key: str,
        group_cooldown: float,
        user_cooldown: float,
    ) -> int:
        now = self.state.clock()
        remaining = 0.0
        last_group = self.state.last_group_reply.get(session_key)
        last_user = self.state.last_user_reply.get(user_key)
        if last_group is not None:
            remaining = max(remaining, group_cooldown - (now - last_group))
        if last_user is not None:
            remaining = max(remaining, user_cooldown - (now - last_user))
        return max(0, int(math.ceil(remaining))) if remaining > 0 else 0

    # ------------------------------------------------------------------
    # /option
    # ------------------------------------------------------------------
    def option_text(self) -> str:
        display_name = "QQ 核心回复"
        version = "4.0.0"
        for star in self._loaded_stars():
            star_name = str(getattr(star, "name", "") or "")
            if (
                getattr(star, "star_cls", None) is self
                or star_name == "qqbot_core_reply"
                or getattr(star, "root_dir_name", "") == "qqbot_core_reply"
            ):
                display_name = (
                    str(getattr(star, "display_name", "") or "")
                    or star_name
                    or display_name
                )
                version = str(getattr(star, "version", "") or version)
                break
        probability = self.config.effective_float(
            "participation.group_probability",
            "reply_probability",
            default=0.12,
            minimum=0.0,
            maximum=1.0,
        )
        group_cooldown, user_cooldown = self._cooldowns()
        safe_tools_ready = (
            self.gateway.safe_tools_available()
            or self.gateway.process_output_available()
        )
        safe_tools = (
            "已检测到 qqbot_safe_tools.tool_instruction()/process_model_output()，"
            "direct prompt 会追加工具说明，完整模型输出交给 safe_tools 一次。"
            if safe_tools_ready
            else "未检测到 qqbot_safe_tools.process_model_output()；不提供 marker 协议。"
        )
        lines = [
            f"{display_name} 用法速览（v{version}）",
            "",
            "唯一主回复 / LLM owner：qqbot_core_reply。",
            "本插件命令：",
            "/回复 状态|设置|启用|停用（别名 /reply）",
            "/冷却 状态",
            "/唱歌 [文件名关键词]（别名 /点歌）",
            "/会话列表（群聊查看 AstrBot 会话）",
            "/会话 <名称>（别名 /切换会话；只切换已有）",
            "/新会话 <名称> [提示词标题]（群聊新建并切换）",
            "/删除会话 <名称|短ID>（别名 /会话删除）",
            "/option（别名 /插件用法、/用法）",
            "",
            (
                f"当前行为：memory_mode={self.persona.memory_mode()}；群 @/私聊必回；"
                f"非 @ 群消息概率 {probability:.0%}；"
                f"群冷却 {group_cooldown:g}s / 用户冷却 {user_cooldown:g}s；"
                f"最短 {self.config.int('min_message_chars', 0)} 字；"
                f"ambient {'开启' if self.config.bool('ambient_group_chat_enabled') else '关闭'}。"
            ),
            "配置入口：AstrBot WebUI → 插件 → QQ 核心回复 → 配置。",
            "概率键：participation.group_probability（旧 reply_probability）；冷却键：cooldown.seconds（旧 group_cooldown_seconds/user_cooldown_seconds）。",
            self.persona.persona_option_line(),
            self.persona.identity_option_line(),
            safe_tools,
            (
                "表情装饰：本插件只设置 qqbot_core_reply_candidate / "
                "qqbot_core_reply_allow_sticker extras，由 qqbot_stickers "
                "在结果钩子中决定，不自行附加图片。"
            ),
            (
                "工具 marker：本插件不解析、不校验、不剥离、不执行 "
                "[[tool:...]]；只把完整模型输出交给 safe_tools 一次。"
            ),
            (
                "限制：一次最多一条回复；不展示 QQ/群号/绝对路径；"
                "一个 AstrBot conversation 独立人格/提示词/记忆。"
            ),
        ]
        if self.config.bool("option_include_other_plugins", True):
            stars = self._loaded_stars()
            max_plugins = max(1, self.config.int("option_max_plugins", 1, 50))
            if stars:
                lines.extend(
                    [
                        "",
                        f"已加载插件元数据（共 {len(stars)} 个，最多显示 {max_plugins} 个）：",
                    ]
                )
                for star in stars[:max_plugins]:
                    star_name = str(getattr(star, "name", "") or "")
                    star_display = str(getattr(star, "display_name", "") or "") or star_name or "未命名插件"
                    star_version = str(getattr(star, "version", "") or "unknown")
                    star_desc = re.sub(r"\s+", " ", str(getattr(star, "desc", "") or "")).strip()
                    activated = bool(getattr(star, "activated", True))
                    line = f"- {star_display} v{star_version}{'' if activated else '（未激活）'}"
                    if star_desc:
                        line += f"：{star_desc[:80]}"
                    lines.append(line)
        return "\n".join(lines)

    def _loaded_stars(self) -> list[Any]:
        getter = getattr(self.context, "get_all_stars", None)
        if not callable(getter):
            return []
        try:
            value = getter()
        except Exception:
            return []
        return [item for item in value if item is not None] if isinstance(value, (list, tuple)) else []

    # ------------------------------------------------------------------
    # /唱歌 /点歌
    # ------------------------------------------------------------------
    async def song_reply(self, event: Any) -> CommandReply:
        if not self.config.bool("song_enabled", True):
            return CommandReply(text=self.config.string("song_disabled_reply", "本地点歌功能当前未启用。"))
        keyword = self._song_keyword(event)
        song_files, oversized = self._list_songs()
        if not song_files:
            return CommandReply(
                text=self.config.string(
                    "song_too_large_reply" if oversized else "song_empty_reply"
                )
            )
        if keyword:
            folded = keyword.casefold()
            matched = [path for path in song_files if folded in path.name.casefold()]
            if not matched:
                return CommandReply(text=self.config.string("song_not_found_reply"))
            song_files = matched
        session_key = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            sender_id = str(event.get_sender_id() or "")
        except Exception:
            sender_id = ""
        remaining = self._claim_song(
            session_key,
            f"{session_key}:{sender_id}",
            float(self.config.int("song_group_cooldown_seconds", 0)),
            float(self.config.int("song_user_cooldown_seconds", 0)),
        )
        if remaining > 0:
            template = self.config.string("song_cooldown_reply")
            try:
                text = template.format(seconds=remaining)
            except (KeyError, ValueError):
                text = template
            return CommandReply(text=text)
        chosen = self.state.rng.choice(song_files)
        try:
            from astrbot.api import message_components as Comp
        except Exception:
            Comp = None
        record = None
        record_cls = getattr(Comp, "Record", None) if Comp else None
        if record_cls is not None and callable(getattr(record_cls, "fromFileSystem", None)):
            try:
                record = record_cls.fromFileSystem(str(chosen))
            except Exception:
                record = None
        if record is None:
            return CommandReply(text=self.config.string("song_error_reply"))
        prefix_template = self.config.string("song_success_prefix", "🎵 本地歌曲：{name}")
        try:
            prefix = prefix_template.format(name=chosen.stem)
        except (KeyError, ValueError):
            prefix = prefix_template
        return CommandReply(kind="chain", chain=[Comp.Plain(prefix), record])

    def _song_keyword(self, event: Any) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        for prefix in self.conversation_prefixes(event):
            if prefix and text.startswith(prefix):
                text = text[len(prefix) :].strip()
                break
        for command_name in ("唱歌", "点歌"):
            if text == command_name:
                return ""
            if text.startswith(command_name):
                rest = text[len(command_name) :]
                if rest and rest[0].isspace():
                    keyword = re.sub(r"\s+", " ", rest).strip()
                    return "".join(char for char in keyword if char.isprintable()).strip()[:100]
        return ""

    def _list_songs(self) -> tuple[list[Path], bool]:
        max_bytes = max(1, self.config.int("song_max_file_bytes", 1))
        files, oversized = list_song_files(
            self.song_dir_getter(),
            max_bytes=max_bytes,
            suffixes=SONG_SUFFIXES,
        )
        return files, bool(oversized)

    def _claim_song(
        self,
        session_key: str,
        user_key: str,
        group_cooldown: float,
        user_cooldown: float,
    ) -> int:
        now = self.state.clock()
        remaining = 0.0
        last_group = self.state.last_song_group.get(session_key)
        last_user = self.state.last_song_user.get(user_key)
        if last_group is not None:
            remaining = max(remaining, group_cooldown - (now - last_group))
        if last_user is not None:
            remaining = max(remaining, user_cooldown - (now - last_user))
        if remaining > 0:
            return max(1, int(math.ceil(remaining)))
        self.state.last_song_group[session_key] = now
        self.state.last_song_user[user_key] = now
        return 0
