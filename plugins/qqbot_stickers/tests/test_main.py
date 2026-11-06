from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from tests import fakes

fakes.install()

import main
from main import StickerPlugin
from tests.image_factory import gif, png


def _run(coro):
    return asyncio.run(coro)


async def _command(plugin, event, method_name):
    method = getattr(plugin, method_name)
    return [item async for item in method(event)]


async def _consume(generator):
    return [item async for item in generator]


def _payload(data: bytes) -> dict:
    from media import inspect_image_bytes

    info = inspect_image_bytes(data, max_bytes=100_000)
    import hashlib

    return {
        "data": info.data,
        "mime": info.mime,
        "extension": info.extension,
        "width": info.width,
        "height": info.height,
        "size": info.size,
        "sha256": hashlib.sha256(info.data).hexdigest(),
    }


class StickerPluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.data_dir = self.base / "plugin_data" / "qqbot_stickers"
        fakes.StarTools.data_dirs.clear()
        self.cfg = {
            "enabled": True,
            "sticker_enabled": True,
            "collected_sticker_enabled": True,
            "sticker_cooldown_seconds": 0,
            "sticker_prevent_repeat": True,
            "sticker_decorate_probability": 1.0,
            "sticker_probability": 1.0,
            "collected_sticker_migration_enabled": False,
            "collected_sticker_max_count": 20,
            "collected_sticker_max_file_bytes": 100_000,
            "collected_sticker_max_dimension": 512,
            "collected_sticker_max_pixels": 262144,
            "collected_sticker_list_limit": 30,
            "pending_sticker_ttl_seconds": 300,
            "auto_collect_user_sticker_enabled": False,
            "auto_collect_sticker_max_file_bytes": 100_000,
            "auto_collect_sticker_max_dimension": 512,
            "auto_collect_sticker_reply_enabled": True,
            "auto_collect_sticker_cooldown_seconds": 0,
            "safe_tools_collect_cooldown_seconds": 0,
            "sticker_natural_language_enabled": True,
            "sticker_natural_language_collect_enabled": True,
            "sticker_natural_language_reclassify_enabled": True,
            "sticker_natural_language_reclassify_ai_enabled": False,
        }
        self.context = fakes.FakeContext(
            {"collected_sticker_max_count": 999, "collected_sticker_max_file_bytes": 1}
        )
        self.clock_value = 1_000.0
        self.plugin = self.make_plugin()

    def tearDown(self):
        self.temp.cleanup()

    def make_plugin(self, **overrides):
        config = dict(self.cfg)
        config.update(overrides)
        plugin = StickerPlugin(self.context, config=config)
        plugin.configure_paths(data_dir=self.data_dir)
        plugin._clock = lambda: self.clock_value
        return plugin

    def collect(self, plugin=None, *, category="开心", data=None, message=None):
        plugin = plugin or self.plugin
        data = data or png(20, 20, tag=1)
        message = message or f"/收藏表情 {category}"
        event = fakes.FakeEvent(
            message,
            [fakes.Image(data=data)],
            unified_msg_origin="fake:private",
            private=True,
        )
        out = _run(_command(plugin, event, "collect_sticker_command"))
        return event, out

    def seed(self, plugin, data, category="开心", *, tags=None, meaning="", tone=""):
        event = fakes.FakeEvent(unified_msg_origin="fake:seed")
        payload = _payload(data)
        status, entry = _run(
            plugin._save_payload(
                event,
                payload,
                category,
                tags=tags or [],
                meaning=meaning,
                tone=tone,
            )
        )
        self.assertEqual(status, "saved")
        return entry

    def test_config_injection_is_used_instead_of_global_context(self):
        plugin = self.make_plugin(collected_sticker_max_count=1)
        store = _run(plugin._ensure_ready())
        self.assertEqual(store.max_count, 1)
        self.assertEqual(store.max_file_bytes, 100_000)
        self.assertEqual(plugin._cfg_int("collected_sticker_max_count", 20, 1), 1)

    def test_plugin_data_and_store_directories_are_traversable(self):
        _run(self.plugin._ensure_ready())
        self.assertEqual(self.data_dir.stat().st_mode & 0o777, 0o755)
        self.assertEqual(
            (self.data_dir / "collected_stickers").stat().st_mode & 0o777,
            0o755,
        )

    def test_collect_command_saves_sticker(self):
        event, out = self.collect()
        self.assertEqual(len(out), 1)
        self.assertIn("已收藏", out[0].chain[0].text)
        self.assertTrue(event.should_call_llm_arg is True)
        self.assertTrue(event.stopped)
        entries = _run(self.plugin._entries())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "开心")

    def test_collect_command_argument_survives_mention_prefix(self):
        event = fakes.FakeEvent(
            "[CQ:at,qq=1] /收藏表情 提及",
            [fakes.Image(data=png(tag=401))],
            unified_msg_origin="fake:mention",
            private=True,
        )
        out = _run(_command(self.plugin, event, "collect_sticker_command"))
        self.assertIn("已收藏", out[0].chain[0].text)
        self.assertEqual(_run(self.plugin._entries())[0]["category"], "提及")

    def test_collect_command_supports_multi_category(self):
        _, out = self.collect(category="开心,日常")
        self.assertIn("已收藏", out[0].chain[0].text)
        entry = _run(self.plugin._entries())[0]
        self.assertEqual(entry["categories"], ["开心", "日常"])

    def test_collect_invalid_category_does_not_write(self):
        event, out = self.collect(category="../evil")
        self.assertIn("分类名不合法", out[0].chain[0].text)
        self.assertFalse((self.data_dir / "collected_stickers" / "index.json").exists())
        self.assertTrue(event.stopped)

    def test_collect_invalid_image_reply(self):
        event = fakes.FakeEvent(
            "/收藏表情",
            [fakes.Image(data=b"not-an-image")],
            private=True,
        )
        out = _run(_command(self.plugin, event, "collect_sticker_command"))
        self.assertIn("无法识别", out[0].chain[0].text)

    def test_collect_no_image_reply(self):
        event = fakes.FakeEvent("/收藏表情", [], private=True)
        out = _run(_command(self.plugin, event, "collect_sticker_command"))
        self.assertIn("没有可收藏", out[0].chain[0].text)

    def test_max_count_reply(self):
        plugin = self.make_plugin(collected_sticker_max_count=1)
        self.collect(plugin=plugin, data=png(tag=1))
        _, out = self.collect(plugin=plugin, data=png(tag=2))
        self.assertIn("上限", out[0].chain[0].text)
        self.assertEqual(len(_run(plugin._entries())), 1)

    def test_status_command(self):
        self.seed(self.plugin, png(tag=3), category="开心")
        event = fakes.FakeEvent("/表情状态", private=True)
        out = _run(_command(self.plugin, event, "sticker_status_command"))
        text = out[0].chain[0].text
        self.assertIn("表情状态", text)
        self.assertIn("收藏：1 张", text)
        self.assertIn("存储：可用", text)
        self.assertTrue(event.stopped)

    def test_categories_command(self):
        self.seed(self.plugin, png(tag=4), category="开心")
        self.seed(self.plugin, png(tag=5), category="日常")
        event = fakes.FakeEvent("/表情分类", private=True)
        out = _run(_command(self.plugin, event, "sticker_categories_command"))
        text = out[0].chain[0].text
        self.assertIn("收藏表情共 2 张", text)
        self.assertIn("开心: 1 张", text)
        self.assertIn("日常: 1 张", text)

    def test_list_command_does_not_leak_absolute_path(self):
        self.seed(
            self.plugin, png(tag=6), category="开心", tags=["happy"], meaning="笑"
        )
        event = fakes.FakeEvent("/表情列表", private=True)
        out = _run(_command(self.plugin, event, "sticker_list_command"))
        text = out[0].chain[0].text
        self.assertIn("收藏表情共 1 张", text)
        self.assertIn("[开心]", text)
        self.assertIn("tags=happy", text)
        self.assertNotIn(str(self.base), text)

    def test_list_unknown_category(self):
        self.seed(self.plugin, png(tag=7), category="开心")
        event = fakes.FakeEvent("/表情列表 不存在", private=True)
        out = _run(_command(self.plugin, event, "sticker_list_command"))
        self.assertIn("没有找到", out[0].chain[0].text)

    def test_send_command_returns_one_image(self):
        self.seed(self.plugin, png(tag=8), category="开心")
        event = fakes.FakeEvent("/发表情 开心", private=True)
        out = _run(_command(self.plugin, event, "send_sticker_command"))
        chain = out[0].chain
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in chain), 1)
        self.assertTrue(event.stopped)

    def test_send_command_cooldown(self):
        plugin = self.make_plugin(sticker_cooldown_seconds=999)
        self.seed(plugin, png(tag=9), category="开心")
        first = fakes.FakeEvent("/发表情", private=True)
        _run(_command(plugin, first, "send_sticker_command"))
        second = fakes.FakeEvent("/发表情", private=True)
        out = _run(_command(plugin, second, "send_sticker_command"))
        self.assertIn("稍等", out[0].chain[0].text)

    def test_send_command_no_repeat_when_multiple(self):
        self.seed(self.plugin, png(tag=10), category="开心")
        self.seed(self.plugin, png(tag=11), category="开心")
        seen = set()
        for _ in range(2):
            event = fakes.FakeEvent("/发表情", private=True)
            out = _run(_command(self.plugin, event, "send_sticker_command"))
            image = next(item for item in out[0].chain if isinstance(item, fakes.Image))
            seen.add(str(image.path))
        self.assertEqual(len(seen), 2)

    def test_send_command_unknown_filter(self):
        self.seed(self.plugin, png(tag=12), category="开心")
        event = fakes.FakeEvent("/发表情 完全不存在", private=True)
        out = _run(_command(self.plugin, event, "send_sticker_command"))
        self.assertIn("没有找到", out[0].chain[0].text)

    def _make_decor_event(self, text="普通回复", extras=None, chain=None):
        chain = list(chain or [fakes.Plain(text)])
        event = fakes.FakeEvent("用户消息", private=True, result=fakes.Result(chain))
        event.extras.update(
            {
                "qqbot_core_reply_candidate": True,
                "qqbot_core_reply_allow_sticker": True,
            }
        )
        event.extras.update(extras or {})
        return event

    def test_decoration_appends_exactly_one_sticker(self):
        self.seed(self.plugin, png(tag=13), category="开心")
        event = self._make_decor_event()
        _run(self.plugin.decorate_core_result(event))
        images = [item for item in event.result.chain if isinstance(item, fakes.Image)]
        self.assertEqual(len(images), 1)
        self.assertTrue(event.extras.get("qqbot_stickers_decorated"))

    def test_decoration_is_idempotent(self):
        self.seed(self.plugin, png(tag=14), category="开心")
        event = self._make_decor_event()
        _run(self.plugin.decorate_core_result(event))
        _run(self.plugin.decorate_core_result(event))
        images = [item for item in event.result.chain if isinstance(item, fakes.Image)]
        self.assertEqual(len(images), 1)

    def test_decoration_legacy_probability_zero_skips(self):
        config = dict(self.cfg)
        config.pop("sticker_decorate_probability", None)
        config["sticker_probability"] = 0.0
        plugin = StickerPlugin(self.context, config=config)
        plugin.configure_paths(data_dir=self.base / "prob-data")
        self.seed(plugin, png(tag=150))
        event = fakes.FakeEvent(
            "用户消息",
            private=True,
            result=fakes.Result([fakes.Plain("回复")]),
        )
        event.extras.update(
            {
                "qqbot_core_reply_candidate": True,
                "qqbot_core_reply_allow_sticker": True,
            }
        )
        _run(plugin.decorate_core_result(event))
        self.assertEqual(len(event.result.chain), 1)
        self.assertEqual(event.result.chain[0].text, "回复")
        self.assertFalse(
            any(isinstance(item, fakes.Image) for item in event.result.chain)
        )

    def test_decoration_requires_core_candidate(self):
        self.seed(self.plugin, png(tag=15))
        event = self._make_decor_event(extras={"qqbot_core_reply_candidate": False})
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))

    def test_decoration_requires_allow_sticker(self):
        self.seed(self.plugin, png(tag=16))
        event = self._make_decor_event(extras={"qqbot_core_reply_allow_sticker": False})
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))

    def test_decoration_skips_safe_tools_processed(self):
        self.seed(self.plugin, png(tag=17))
        event = self._make_decor_event(extras={"qqbot_safe_tools_processed": True})
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))

    def test_decoration_skips_chain_with_existing_image(self):
        self.seed(self.plugin, png(tag=18))
        event = self._make_decor_event(chain=[fakes.Plain("x"), fakes.Image(data=png(tag=99))])
        _run(self.plugin.decorate_core_result(event))
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in event.result.chain), 1)

    def test_decoration_skips_tool_marker_without_consuming_it(self):
        self.seed(self.plugin, png(tag=19))
        event = self._make_decor_event(text="hello [[tool:send_sticker]]")
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))
        self.assertEqual(event.result.chain[0].text, "hello [[tool:send_sticker]]")

    def test_decoration_skips_command_result(self):
        self.seed(self.plugin, png(tag=20))
        event = self._make_decor_event()
        event.message_str = "/发表情"
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))

    def test_decoration_skips_command_extra(self):
        self.seed(self.plugin, png(tag=21))
        event = self._make_decor_event(extras={"qqbot_stickers_command_result": True})
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))

    def test_tool_send_sticker_category_filter(self):
        self.seed(self.plugin, png(tag=22), category="开心")
        self.seed(self.plugin, png(tag=23), category="日常")
        event = fakes.FakeEvent(unified_msg_origin="fake:tool")
        result = _run(self.plugin.tool_send_sticker(event, category="日常"))
        self.assertIsNotNone(result)
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in result.chain), 1)

    def test_tool_send_sticker_unknown_returns_none(self):
        self.seed(self.plugin, png(tag=24), category="开心")
        event = fakes.FakeEvent(unified_msg_origin="fake:tool")
        self.assertIsNone(
            _run(self.plugin.tool_send_sticker(event, category="不存在"))
        )

    def test_tool_send_sticker_disabled_returns_none(self):
        plugin = self.make_plugin(safe_tools_allow_send_sticker=False)
        self.seed(plugin, png(tag=25))
        event = fakes.FakeEvent(unified_msg_origin="fake:tool")
        self.assertIsNone(_run(plugin.tool_send_sticker(event)))

    def test_tool_collect_sticker_saves_image(self):
        event = fakes.FakeEvent(
            "",
            [fakes.Image(data=png(tag=26))],
            unified_msg_origin="fake:tool-collect",
        )
        result = _run(self.plugin.tool_collect_sticker(event, category="工具"))
        self.assertIsNotNone(result)
        entries = _run(self.plugin._entries())
        self.assertEqual(entries[0]["category"], "工具")

    def test_tool_collect_sticker_disabled_returns_none(self):
        plugin = self.make_plugin(safe_tools_allow_collect_sticker=False)
        event = fakes.FakeEvent(
            "", [fakes.Image(data=png(tag=27))], unified_msg_origin="fake:tool"
        )
        self.assertIsNone(_run(plugin.tool_collect_sticker(event)))

    def test_tool_list_categories(self):
        self.seed(self.plugin, png(tag=28), category="开心")
        self.seed(self.plugin, png(tag=29), category="日常")
        event = fakes.FakeEvent(unified_msg_origin="fake:tool")
        result = _run(self.plugin.tool_list_categories(event))
        self.assertIsNotNone(result)
        text = result.chain[0].text
        self.assertIn("开心(1)", text)
        self.assertIn("日常(1)", text)

    def test_natural_language_collect_private(self):
        event = fakes.FakeEvent(
            "把这个表情收藏到开心",
            [fakes.Image(data=png(tag=30))],
            unified_msg_origin="fake:nl",
            private=True,
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertEqual(len(out), 1)
        self.assertIn("已收藏", out[0].chain[0].text)
        self.assertTrue(event.should_call_llm_arg is True)
        self.assertTrue(event.stopped)

    def test_natural_language_ignored_in_group_without_mention(self):
        event = fakes.FakeEvent(
            "把这个表情收藏到开心",
            [fakes.Image(data=png(tag=31))],
            unified_msg_origin="fake:group",
            private=False,
            at_wake=False,
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertEqual(out, [])
        self.assertFalse(event.stopped)

    def test_natural_language_send(self):
        self.seed(self.plugin, png(tag=32), category="开心")
        event = fakes.FakeEvent(
            "发个开心的表情", unified_msg_origin="fake:nl-send", private=True
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertEqual(len(out), 1)
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in out[0].chain), 1)

    def test_natural_language_send_tag(self):
        self.seed(
            self.plugin,
            png(tag=320),
            category="梗图",
            tags=["笑哭"],
        )
        event = fakes.FakeEvent(
            "发个笑哭表情", unified_msg_origin="fake:nl-tag", private=True
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertEqual(len(out), 1)
        self.assertEqual(
            sum(isinstance(item, fakes.Image) for item in out[0].chain), 1
        )

    def test_natural_language_send_not_found(self):
        self.seed(self.plugin, png(tag=33), category="开心")
        event = fakes.FakeEvent(
            "发张好看的表情", unified_msg_origin="fake:nl-send2", private=True
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertIn("没有找到", out[0].chain[0].text)

    def test_natural_language_reclassify_recent(self):
        entry = self.seed(self.plugin, png(tag=34), category="旧")
        self.plugin._remember_collected(
            fakes.FakeEvent(unified_msg_origin="fake:reclass"), entry
        )
        event = fakes.FakeEvent(
            "把这个表情改成开心",
            unified_msg_origin="fake:reclass",
            private=True,
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertEqual(len(out), 1)
        self.assertTrue("移到分类" in out[0].chain[0].text or "已记住" in out[0].chain[0].text)
        loaded = _run(self.plugin._entries())[0]
        self.assertEqual(loaded["category"], "开心")

    def test_optional_ai_disabled_never_calls_model(self):
        entry = self.seed(self.plugin, png(tag=35), category="旧")
        self.plugin._remember_collected(
            fakes.FakeEvent(unified_msg_origin="fake:ai"), entry
        )
        event = fakes.FakeEvent(
            "把这个表情改成@@@",
            unified_msg_origin="fake:ai",
            private=True,
        )
        out = _run(_consume(self.plugin.on_message(event)))
        self.assertIn("没听清", out[0].chain[0].text)
        self.assertEqual(self.context.llm_calls, 0)

    def test_optional_ai_unavailable_falls_back(self):
        plugin = self.make_plugin(
            sticker_natural_language_reclassify_ai_enabled=True,
            sticker_natural_language_reclassify_provider_id="missing-provider",
        )
        entry = self.seed(plugin, png(tag=36), category="旧")
        plugin._remember_collected(
            fakes.FakeEvent(unified_msg_origin="fake:ai-unavailable"), entry
        )
        event = fakes.FakeEvent(
            "把这个表情改成@@@",
            unified_msg_origin="fake:ai-unavailable",
            private=True,
        )
        out = _run(_consume(plugin.on_message(event)))
        self.assertIn("没听清", out[0].chain[0].text)
        self.assertEqual(self.context.llm_calls, 1)

    def test_auto_collect_local_only(self):
        plugin = self.make_plugin(
            auto_collect_user_sticker_enabled=True,
            auto_collect_sticker_default_category="自动",
        )
        event = fakes.FakeEvent(
            "",
            [fakes.Image(data=png(32, 32, tag=37))],
            unified_msg_origin="fake:auto",
            private=True,
        )
        out = _run(_consume(plugin.on_message(event)))
        self.assertEqual(len(out), 1)
        self.assertIn("自动", out[0].chain[0].text)
        entries = _run(plugin._entries())
        self.assertEqual(entries[0]["category"], "自动")
        self.assertEqual(self.context.llm_calls, 0)

    def test_user_auto_collect_ignores_non_explicit_group_image(self):
        plugin = self.make_plugin(auto_collect_user_sticker_enabled=True)
        event = fakes.FakeEvent(
            "",
            [fakes.Image(data=png(32, 32, tag=391))],
            unified_msg_origin="fake:auto-group",
            private=False,
            at_wake=False,
        )
        out = _run(_consume(plugin.on_message(event)))
        self.assertEqual(out, [])
        self.assertEqual(_run(plugin._entries()), [])

    def test_ai_auto_collect_uses_local_fallback_and_category(self):
        plugin = self.make_plugin(
            sticker_ai_auto_collect_enabled=True,
            auto_collect_sticker_default_category="AI本地",
        )
        event = fakes.FakeEvent(
            "",
            [fakes.Image(data=png(32, 32, tag=390))],
            unified_msg_origin="fake:ai-auto",
        )
        out = _run(_consume(plugin.on_message(event)))
        self.assertEqual(len(out), 1)
        self.assertIn("AI本地", out[0].chain[0].text)
        entries = _run(plugin._entries())
        self.assertEqual(entries[0]["category"], "AI本地")
        self.assertEqual(self.context.llm_calls, 0)

    def test_auto_collect_reply_can_be_disabled(self):
        plugin = self.make_plugin(
            auto_collect_user_sticker_enabled=True,
            auto_collect_sticker_reply_enabled=False,
        )
        event = fakes.FakeEvent(
            "",
            [fakes.Image(data=png(32, 32, tag=38))],
            unified_msg_origin="fake:auto2",
            private=True,
        )
        out = _run(_consume(plugin.on_message(event)))
        self.assertEqual(out, [])
        self.assertEqual(len(_run(plugin._entries())), 1)

    def test_tool_methods_never_raise_on_minimal_event(self):
        class MinimalEvent:
            unified_msg_origin = "minimal"

        event = MinimalEvent()
        self.assertIsNone(_run(self.plugin.tool_send_sticker(event)))
        self.assertIsNone(_run(self.plugin.tool_collect_sticker(event)))
        # A read-only helper can still degrade to None rather than raising.
        result = _run(self.plugin.tool_list_categories(event))
        self.assertTrue(result is None or hasattr(result, "chain"))

    def test_command_suppresses_model_for_all_commands(self):
        methods = (
            "collect_sticker_command",
            "sticker_status_command",
            "sticker_categories_command",
            "sticker_list_command",
            "send_sticker_command",
        )
        for name in methods:
            event = fakes.FakeEvent("/" + name, private=True)
            _run(_command(self.plugin, event, name))
            self.assertTrue(event.should_call_llm_arg, name)
            self.assertTrue(event.stopped, name)
        self.assertEqual(self.context.llm_calls, 0)

    def test_config_attribute_is_injected_mapping(self):
        config = dict(self.cfg)
        config["collected_sticker_max_count"] = 7
        plugin = StickerPlugin(self.context, config=config)
        self.assertEqual(plugin.config["collected_sticker_max_count"], 7)

    def test_collect_quoted_image(self):
        event = fakes.FakeEvent(
            "/收藏表情 引用",
            [fakes.Reply([fakes.Image(data=png(tag=40))])],
            unified_msg_origin="fake:quoted",
            private=True,
        )
        out = _run(_command(self.plugin, event, "collect_sticker_command"))
        self.assertIn("已收藏", out[0].chain[0].text)
        self.assertEqual(_run(self.plugin._entries())[0]["category"], "引用")

    def test_collect_recent_cached_image(self):
        first = fakes.FakeEvent(
            "看看这个",
            [fakes.Image(data=png(tag=41))],
            unified_msg_origin="fake:recent",
            private=True,
        )
        self.assertEqual(_run(_consume(self.plugin.on_message(first))), [])
        second = fakes.FakeEvent(
            "/收藏表情 最近",
            [],
            unified_msg_origin="fake:recent",
            private=True,
        )
        out = _run(_command(self.plugin, second, "collect_sticker_command"))
        self.assertIn("已收藏", out[0].chain[0].text)
        self.assertEqual(_run(self.plugin._entries())[0]["category"], "最近")

    def test_image_component_converter_path_is_supported(self):
        import base64

        class ConverterImage(fakes.Image):
            def __init__(self):
                super().__init__()
                self.file = "remote-file-id.jpg"
                self.url = "https://example.invalid/image.jpg"

            async def convert_to_base64(self):
                return base64.b64encode(png(tag=421)).decode("ascii")

        event = fakes.FakeEvent(
            "/收藏表情 下载",
            [ConverterImage()],
            unified_msg_origin="fake:converter",
            private=True,
        )
        out = _run(_command(self.plugin, event, "collect_sticker_command"))
        self.assertIn("已收藏", out[0].chain[0].text)

    def test_symlink_image_component_is_rejected(self):
        outside = self.base / "outside.png"
        outside.write_bytes(png(tag=42))
        link = self.base / "link.png"
        link.symlink_to(outside)
        event = fakes.FakeEvent(
            "/收藏表情 链接",
            [fakes.Image(path=link)],
            unified_msg_origin="fake:symlink-image",
            private=True,
        )
        out = _run(_command(self.plugin, event, "collect_sticker_command"))
        self.assertIn("无法识别", out[0].chain[0].text)
        self.assertEqual(_run(self.plugin._entries()), [])

    def test_command_send_emotion_filter(self):
        self.seed(self.plugin, png(tag=43), category="开心", tags=["happy"])
        event = fakes.FakeEvent("/发表情 happy", private=True)
        out = _run(_command(self.plugin, event, "send_sticker_command"))
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in out[0].chain), 1)

    def test_command_send_tag_filter(self):
        self.seed(self.plugin, png(tag=44), category="梗图", tags=["笑哭"])
        event = fakes.FakeEvent("/发表情 笑哭", private=True)
        out = _run(_command(self.plugin, event, "send_sticker_command"))
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in out[0].chain), 1)

    def test_tool_send_sticker_emotion_filter(self):
        self.seed(self.plugin, png(tag=45), category="开心", tags=["happy"])
        event = fakes.FakeEvent(unified_msg_origin="fake:tool-emotion")
        result = _run(self.plugin.tool_send_sticker(event, emotion="happy"))
        self.assertIsNotNone(result)
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in result.chain), 1)

    def test_tool_send_sticker_tag_filter(self):
        self.seed(self.plugin, png(tag=46), category="梗图", tags=["笑哭"])
        event = fakes.FakeEvent(unified_msg_origin="fake:tool-tag")
        result = _run(self.plugin.tool_send_sticker(event, tag="笑哭"))
        self.assertIsNotNone(result)
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in result.chain), 1)

    def test_decoration_skips_spaced_tool_marker(self):
        self.seed(self.plugin, png(tag=47))
        event = self._make_decor_event(text="hello [[ tool : send_sticker ]]")
        _run(self.plugin.decorate_core_result(event))
        self.assertFalse(any(isinstance(item, fakes.Image) for item in event.result.chain))
        self.assertIn("[[ tool", event.result.chain[0].text)

    def test_decoration_skips_nested_reply_image(self):
        self.seed(self.plugin, png(tag=48))
        nested = fakes.Reply([fakes.Image(data=png(tag=98))])
        event = self._make_decor_event(chain=[fakes.Plain("x"), nested])
        _run(self.plugin.decorate_core_result(event))
        self.assertEqual(sum(isinstance(item, fakes.Image) for item in event.result.chain), 0)

    def test_startup_migration_runs_once(self):
        legacy = self.base / "legacy" / "collected_stickers"
        (legacy / "旧分类").mkdir(parents=True)
        data = png(tag=49)
        import hashlib

        (legacy / "旧分类" / (hashlib.sha256(data).hexdigest() + ".png")).write_bytes(data)
        plugin = self.make_plugin(collected_sticker_migration_enabled=True)
        plugin.configure_paths(
            data_dir=self.data_dir,
            legacy_root=legacy,
        )
        entries = _run(plugin._entries())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["category"], "旧分类")
        result = plugin.last_migration_result
        self.assertIsNotNone(result)
        self.assertEqual(result.imported, 1)
        _run(plugin._ensure_ready())
        self.assertIs(plugin.last_migration_result, result)
        self.assertEqual(len(_run(plugin._entries())), 1)

    def test_star_tools_paths_drive_data_and_migration(self):
        plugin_data = self.base / "star-plugin-data"
        legacy_data = self.base / "star-legacy-data"
        legacy = legacy_data / "collected_stickers"
        (legacy / "迁移").mkdir(parents=True)
        data = png(tag=450)
        import hashlib

        (legacy / "迁移" / (hashlib.sha256(data).hexdigest() + ".png")).write_bytes(data)
        fakes.StarTools.data_dirs["qqbot_stickers"] = plugin_data
        fakes.StarTools.data_dirs["qqbot_enhancer"] = legacy_data
        plugin = StickerPlugin(
            self.context,
            config=dict(self.cfg, collected_sticker_migration_enabled=True),
        )
        entries = _run(plugin._entries())
        self.assertEqual(len(entries), 1)
        self.assertTrue(
            (plugin_data / "collected_stickers" / "index.json").is_file()
        )

    def test_natural_language_send_disabled_sticker_is_silent(self):
        plugin = self.make_plugin(sticker_enabled=False)
        self.seed(plugin, png(tag=50), category="开心")
        event = fakes.FakeEvent(
            "发个开心的表情", unified_msg_origin="fake:disabled-send", private=True
        )
        out = _run(_consume(plugin.on_message(event)))
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
