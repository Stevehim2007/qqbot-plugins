"""Package-level smoke tests for the migrated AstrBot plugin entrypoint."""

from __future__ import annotations

import unittest

from _support import (
    Event,
    Plain,
    Result,
    base_config,
    candidate_event,
    make_plugin,
    plain_text,
    register_meta,
    run_decorate,
    safe_tools,
)
from types import SimpleNamespace


class EntrypointSmokeTests(unittest.TestCase):
    def test_plugin_constructs_with_injected_config(self):
        config = base_config(allow_fetch_webpage=False)
        plugin = make_plugin(config)
        self.assertIs(plugin.config, config)
        self.assertNotIn("fetch_webpage", plugin.tool_instruction())

    def test_parser_smoke(self):
        parsed = safe_tools.parse_tool_markers(
            'hello [[tool:send_sticker emotion="happy"]]'
        )
        self.assertTrue(parsed.had_marker)
        self.assertEqual(parsed.intents[0].name, "send_sticker")
        self.assertTrue(parsed.intents[0].valid)

    def test_decorating_entrypoint_replaces_chain(self):
        plugin = make_plugin()
        api = SimpleNamespace(
            tool_list_categories=lambda event: Result([Plain("分类列表")])
        )
        register_meta(plugin, "qqbot_stickers", api)
        event = candidate_event("[[tool:list_sticker_categories()]]")
        run_decorate(plugin, event)
        self.assertEqual(plain_text(event.get_result().chain), "分类列表")
        self.assertTrue(event.stopped)


if __name__ == "__main__":
    unittest.main()
