from __future__ import annotations

import unittest

from support import FakeEvent, collect_event, core_main, make_plugin


class CoreSmokeTests(unittest.TestCase):
    def test_plugin_class_and_handler_exist(self):
        self.assertTrue(core_main.CoreReplyPlugin)
        self.assertTrue(callable(core_main.CoreReplyPlugin.on_message))

    def test_private_message_smoke(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        results = __import__("asyncio").run(
            collect_event(plugin, FakeEvent("hello", private=True))
        )
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
