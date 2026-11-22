"""config.overlay.v1 wiring for qqbot_stickers."""

from __future__ import annotations

import unittest

import policy_overlay

import main as stickers_main


def _overlay(**values):
    return policy_overlay.build(
        "qqbot_stickers",
        {
            "params": {"decoration_probability": 0.25},
            "config_values": {"sticker_decorate_probability": 0.25, **values},
            "revision": 5,
        },
        scope="group",
        scope_id="789",
    )


class _Bare:
    def __init__(self, config):
        self._plugin_config = dict(config)

    _cfg = stickers_main.StickerPlugin._cfg
    _cfg_bool = stickers_main.StickerPlugin._cfg_bool
    _cfg_float = stickers_main.StickerPlugin._cfg_float
    _cfg_str = stickers_main.StickerPlugin._cfg_str


class StickerOverlayTest(unittest.TestCase):
    def test_cfg_prefers_overlay(self):
        plugin = _Bare({"sticker_decorate_probability": 1.0})
        self.assertAlmostEqual(plugin._cfg_float("sticker_decorate_probability", 1.0), 1.0)
        with policy_overlay.policy_scope(_overlay()):
            self.assertAlmostEqual(plugin._cfg_float("sticker_decorate_probability", 1.0), 0.25)
        self.assertAlmostEqual(plugin._cfg_float("sticker_decorate_probability", 1.0), 1.0)

    def test_policy_name_fallback(self):
        plugin = _Bare({})
        with policy_overlay.policy_scope(_overlay()):
            self.assertAlmostEqual(plugin._cfg_float("decoration_probability", 1.0), 0.25)


if __name__ == "__main__":
    unittest.main()
