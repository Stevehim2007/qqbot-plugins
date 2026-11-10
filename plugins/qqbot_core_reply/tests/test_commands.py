from __future__ import annotations

import asyncio
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from support import (
    FakeContext,
    FakeConversation,
    FakeConversationManager,
    FakeEvent,
    FakePersonaManager,
    FakeStarMeta,
    collect_command,
    make_plugin,
)
from astrbot.api import message_components as Comp
import main as core_main


def run(coro):
    return asyncio.run(coro)


GROUP = "123456789"


def manager_with(*titles, current="cid-1"):
    conversations = [
        FakeConversation(f"cid-{index + 1}", title=title, created_at=float(index))
        for index, title in enumerate(titles)
    ]
    manager = FakeConversationManager(conversations)
    manager.current[f"aiocqhttp:GroupMessage:{GROUP}"] = current
    return manager


def make_command_plugin(*, overrides=None, manager=None, persona_manager=None):
    ctx = FakeContext()
    ctx.conversation_manager = manager
    ctx.persona_manager = persona_manager
    plugin, _, _ = make_plugin(
        context=ctx,
        overrides={"enabled": True, **(overrides or {})},
    )
    return plugin, ctx


class ConversationListTests(unittest.TestCase):
    def test_list_disabled(self):
        plugin, ctx = make_command_plugin(overrides={"conversation_switch_enabled": False})
        event = FakeEvent("/会话列表", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_list_command", event))
        self.assertIn("未启用", results[0].text)
        self.assertTrue(event.llm_blocked)
        self.assertTrue(event.stopped)

    def test_list_private(self):
        plugin, ctx = make_command_plugin()
        event = FakeEvent("/会话列表", private=True)
        results = run(collect_command(plugin, "conversation_list_command", event))
        self.assertIn("私聊", results[0].text)

    def test_list_unsupported_manager(self):
        plugin, ctx = make_command_plugin(manager=None)
        event = FakeEvent("/会话列表", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_list_command", event))
        self.assertIn("没有可用的官方会话管理 API", results[0].text)

    def test_list_empty(self):
        plugin, ctx = make_command_plugin(manager=FakeConversationManager())
        event = FakeEvent("/会话列表", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_list_command", event))
        self.assertIn("还没有可用会话", results[0].text)

    def test_list_success_current_marker_and_limit(self):
        manager = manager_with("Alpha", "Beta", current="cid-2")
        plugin, ctx = make_command_plugin(
            manager=manager,
            overrides={"conversation_list_limit": 1},
        )
        event = FakeEvent("/会话列表", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_list_command", event))
        text = results[0].text
        self.assertIn("Alpha", text)
        self.assertNotIn("Beta", text)
        self.assertIn("上限", text)
        self.assertIn("提示词", text)

    def test_list_none_candidate_has_no_candidate_extra(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/会话列表", group_id=GROUP)
        run(collect_command(plugin, "conversation_list_command", event))
        self.assertNotIn("qqbot_core_reply_candidate", event.extras)


class ConversationSwitchTests(unittest.TestCase):
    def test_switch_usage_without_argument(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/会话", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("用法", results[0].text)

    def test_switch_disabled(self):
        plugin, ctx = make_command_plugin(
            manager=manager_with("Alpha"),
            overrides={"conversation_switch_enabled": False},
        )
        event = FakeEvent("/会话 Alpha", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("未启用", results[0].text)

    def test_switch_private(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/会话 Alpha", private=True)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("私聊", results[0].text)

    def test_switch_success(self):
        manager = manager_with("Alpha", "Beta", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/会话 Beta", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("已切换到会话「Beta」", results[0].text)
        self.assertEqual(manager.current[f"aiocqhttp:GroupMessage:{GROUP}"], "cid-2")

    def test_switch_duplicate_refuses(self):
        manager = manager_with("Same", "Same", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/会话 Same", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("多个同名会话", results[0].text)

    def test_switch_not_found_does_not_create(self):
        manager = manager_with("Alpha", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/会话 Missing", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("没有找到会话", results[0].text)
        self.assertEqual(manager.new_calls, [])

    def test_switch_manager_failure_degrades(self):
        class BrokenManager(FakeConversationManager):
            async def switch_conversation(self, umo, cid):
                raise RuntimeError("broken")

        manager = BrokenManager([FakeConversation("cid-1", "Alpha", created_at=1.0)])
        manager.current[f"aiocqhttp:GroupMessage:{GROUP}"] = "cid-1"
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/会话 Alpha", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_switch_command", event))
        self.assertIn("没有可用的官方会话管理 API", results[0].text)


class ConversationNewTests(unittest.TestCase):
    def test_new_usage_without_title(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/新会话", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("用法", results[0].text)

    def test_new_duplicate_title(self):
        manager = manager_with("Alpha", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/新会话 Alpha", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("已存在", results[0].text)

    def test_new_missing_prompt_template(self):
        manager = manager_with("Alpha", current="cid-1")
        plugin, ctx = make_command_plugin(
            manager=manager,
            overrides={"prompt_profiles": "[]"},
        )
        event = FakeEvent("/新会话 Beta MissingProfile", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("未找到提示词模板", results[0].text)

    def test_new_success_without_persona(self):
        manager = manager_with("Alpha", current="cid-1")
        plugin, ctx = make_command_plugin(
            manager=manager,
            overrides={"conversation_auto_prune_enabled": True},
        )
        event = FakeEvent("/新会话 Beta", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("已新建并切换到会话「Beta」", results[0].text)
        self.assertEqual(len(manager.new_calls), 1)
        self.assertEqual(manager.new_calls[0]["title"], "Beta")

    def test_new_success_with_prompt_profile_and_persona_bridge(self):
        manager = manager_with("Alpha", current="cid-1")
        persona_manager = FakePersonaManager()
        profiles = json.dumps(
            [{"id": "p1", "name": "Profile One", "prompt": "PROMPT ONE"}]
        )
        plugin, ctx = make_command_plugin(
            manager=manager,
            persona_manager=persona_manager,
            overrides={"prompt_profiles": profiles},
        )
        event = FakeEvent("/新会话 Beta Profile One", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("已新建并切换到会话「Beta」", results[0].text)
        self.assertIn("提示词：Profile One", results[0].text)
        persona_id = "qqbot_core_reply_prompt_p1"
        self.assertEqual(manager.new_calls[0]["persona_id"], persona_id)
        self.assertIn(persona_id, persona_manager.personas)

    def test_new_limit_without_auto_prune(self):
        manager = manager_with("Alpha", current="cid-1")
        plugin, ctx = make_command_plugin(
            manager=manager,
            overrides={
                "conversation_max_per_group": 1,
                "conversation_auto_prune_enabled": False,
            },
        )
        event = FakeEvent("/新会话 Beta", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("上限", results[0].text)
        self.assertEqual(manager.new_calls, [])

    def test_new_auto_prunes_oldest_non_current(self):
        manager = manager_with("Current", "Old", current="cid-1")
        plugin, ctx = make_command_plugin(
            manager=manager,
            overrides={
                "conversation_max_per_group": 2,
                "conversation_auto_prune_enabled": True,
            },
        )
        event = FakeEvent("/新会话 New", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("已新建并切换到会话「New」", results[0].text)
        self.assertEqual(manager.delete_calls[0][1], "cid-2")

    def test_new_manager_exception_degrades(self):
        class BrokenManager(FakeConversationManager):
            async def new_conversation(self, *args, **kwargs):
                raise RuntimeError("cannot create")

        manager = BrokenManager([FakeConversation("cid-1", "Alpha", created_at=1.0)])
        manager.current[f"aiocqhttp:GroupMessage:{GROUP}"] = "cid-1"
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/新会话 Beta", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_new_command", event))
        self.assertIn("新会话创建失败", results[0].text)


class ConversationDeleteTests(unittest.TestCase):
    def test_delete_usage_without_argument(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/删除会话", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("用法", results[0].text)

    def test_delete_not_found(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/删除会话 Missing", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("没有找到会话", results[0].text)

    def test_delete_duplicate_title(self):
        manager = manager_with("Same", "Same", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/删除会话 Same", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("多个同名会话", results[0].text)
        self.assertEqual(manager.delete_calls, [])

    def test_delete_by_title_success(self):
        manager = manager_with("Alpha", "Beta", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/删除会话 Beta", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("已删除会话「Beta」", results[0].text)
        self.assertEqual(manager.delete_calls[0][1], "cid-2")

    def test_delete_by_short_id(self):
        manager = manager_with("Alpha", "Beta", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/删除会话 cid-2", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("已删除会话「Beta」", results[0].text)

    def test_delete_last_conversation_refused(self):
        manager = manager_with("Only", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/删除会话 Only", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("不能删除最后一个会话", results[0].text)

    def test_delete_current_switches_then_deletes(self):
        manager = manager_with("Current", "Other", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/删除会话 Current", group_id=GROUP)
        results = run(collect_command(plugin, "conversation_delete_command", event))
        self.assertIn("已删除会话「Current」", results[0].text)
        self.assertEqual(manager.delete_calls[0][1], "cid-1")
        self.assertIn(
            (f"aiocqhttp:GroupMessage:{GROUP}", "cid-2"),
            manager.switch_calls,
        )

    def test_delete_without_manager_delete_api(self):
        manager = manager_with("Alpha", current="cid-1")
        plugin, ctx = make_command_plugin(manager=manager)
        event = FakeEvent("/删除会话 Alpha", group_id=GROUP)
        with mock.patch.object(manager, "delete_conversation", None):
            results = run(
                collect_command(plugin, "conversation_delete_command", event)
            )
        self.assertIn("没有可用的官方会话删除 API", results[0].text)


class OptionTests(unittest.TestCase):
    def _plugin_with_commands(self):
        plugin, ctx = make_command_plugin()
        for plugin_id in ("qqbot_memory_commands", "qqbot_food", "qqbot_stickers"):
            ctx.registered_stars[plugin_id] = object()
        return plugin, ctx

    def test_option_lists_only_user_facing_commands(self):
        plugin, _ctx = self._plugin_with_commands()
        event = FakeEvent("/option", group_id=GROUP)
        results = run(collect_command(plugin, "option_command", event))
        text = results[0].text
        self.assertIn("/option", text)
        self.assertIn("/记忆", text)
        self.assertIn("/美食", text)
        self.assertIn("/表情", text)
        self.assertNotIn("reply_probability", text)
        self.assertNotIn("唯一主回复 / LLM owner", text)
        self.assertNotIn("qqbot_safe_tools", text)

    def test_option_detail(self):
        plugin, _ctx = self._plugin_with_commands()
        event = FakeEvent("/option 美食", group_id=GROUP)
        results = run(collect_command(plugin, "option_command", event))
        text = results[0].text
        self.assertIn("用途：搜索美食", text)
        self.assertIn("用法：/美食", text)
        self.assertIn("示例：", text)

    def test_option_unknown_command(self):
        plugin, _ctx = self._plugin_with_commands()
        event = FakeEvent("/option not-a-command", group_id=GROUP)
        results = run(collect_command(plugin, "option_command", event))
        self.assertIn("没有找到指令", results[0].text)


class SongCommandTests(unittest.TestCase):
    def test_song_disabled(self):
        plugin, ctx = make_command_plugin(
            manager=manager_with("Alpha"),
            overrides={"song_enabled": False},
        )
        event = FakeEvent("/唱歌", group_id=GROUP)
        results = run(collect_command(plugin, "local_song_command", event))
        self.assertIn("未启用", results[0].text)
        self.assertTrue(event.llm_blocked)
        self.assertTrue(event.stopped)

    def test_song_empty_directory(self):
        plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
        event = FakeEvent("/唱歌", group_id=GROUP)
        results = run(collect_command(plugin, "local_song_command", event))
        self.assertIn("还没有音频文件", results[0].text)

    def test_song_not_found(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "alpha.mp3").write_bytes(b"ID3")
            plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
            plugin._song_dir = Path(temp_dir)
            event = FakeEvent("/唱歌 beta", group_id=GROUP)
            results = run(collect_command(plugin, "local_song_command", event))
            self.assertIn("没有找到匹配", results[0].text)
            self.assertTrue(event.stopped)

    def test_song_success_chain(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "alpha.mp3").write_bytes(b"ID3")
            plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
            plugin._song_dir = Path(temp_dir)
            event = FakeEvent("/唱歌 alpha", group_id=GROUP)
            results = run(collect_command(plugin, "local_song_command", event))
            self.assertEqual(results[0].kind, "chain")
            self.assertEqual(results[0].chain[0].text, "🎵 本地歌曲：alpha")
            self.assertEqual(len(results[0].chain), 2)
            self.assertTrue(event.stopped)
            self.assertNotIn("qqbot_core_reply_candidate", event.extras)

    def test_song_cooldown_blocks_second(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "alpha.mp3").write_bytes(b"ID3")
            plugin, ctx = make_command_plugin(
                manager=manager_with("Alpha"),
                overrides={
                    "song_group_cooldown_seconds": 60,
                    "song_user_cooldown_seconds": 60,
                },
            )
            plugin._song_dir = Path(temp_dir)
            first = run(
                collect_command(
                    plugin,
                    "local_song_command",
                    FakeEvent("/唱歌", group_id=GROUP),
                )
            )
            second = run(
                collect_command(
                    plugin,
                    "local_song_command",
                    FakeEvent("/唱歌", group_id=GROUP),
                )
            )
            self.assertEqual(first[0].kind, "chain")
            self.assertIn("冷却中", second[0].text)

    def test_song_record_error(self):
        class BrokenRecord:
            @classmethod
            def fromFileSystem(cls, path):
                raise RuntimeError("bad audio")

        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "alpha.mp3").write_bytes(b"ID3")
            plugin, ctx = make_command_plugin(manager=manager_with("Alpha"))
            plugin._song_dir = Path(temp_dir)
            event = FakeEvent("/唱歌", group_id=GROUP)
            with mock.patch.object(Comp, "Record", BrokenRecord):
                results = run(collect_command(plugin, "local_song_command", event))
            self.assertIn("本地音频构造失败", results[0].text)


if __name__ == "__main__":
    unittest.main()
