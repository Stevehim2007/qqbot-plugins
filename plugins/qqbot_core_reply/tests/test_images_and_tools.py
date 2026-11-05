from __future__ import annotations

import asyncio
import types
import unittest

from support import (
    FakeContext,
    FakeEvent,
    FakeProvider,
    FakeStarMeta,
    collect_event,
    make_plugin,
)
from astrbot.api import message_components as Comp


def run(coro):
    return asyncio.run(coro)


def image_event(*images, text="", private=True):
    components = list(images)
    if text:
        components.append(Comp.Plain(text))
    return FakeEvent(text, private=private, components=components)


class ImageFallbackTests(unittest.TestCase):
    def test_native_vision_success_one_call(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["vision reply"]
        event = image_event(Comp.Image(url="https://x/a.png"), text="what is it")
        results = run(collect_event(plugin, event))
        self.assertEqual(results[0].text, "vision reply")
        self.assertEqual(len(ctx.llm_calls), 1)
        self.assertEqual(ctx.llm_calls[0]["chat_provider_id"], "prov-1")
        self.assertEqual(ctx.llm_calls[0]["image_urls"], ["https://x/a.png"])
        self.assertIn("用户发来了图片", ctx.llm_calls[0]["prompt"])

    def test_native_empty_then_caption_then_text(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "vision_provider_id": "cap-1",
                "auto_use_astrbot_caption_provider": False,
            },
            context=FakeContext(
                provider_id="prov-1",
                providers={"cap-1": FakeProvider("cap-1")},
            ),
        )
        ctx.llm_responses = ["", "a cat on a mat", "final caption reply"]
        event = image_event(Comp.Image(url="https://x/a.png"))
        results = run(collect_event(plugin, event))
        self.assertEqual(results[0].text, "final caption reply")
        self.assertEqual(
            [call["chat_provider_id"] for call in ctx.llm_calls],
            ["prov-1", "cap-1", "prov-1"],
        )
        self.assertIn(
            "<image_caption>a cat on a mat</image_caption>",
            ctx.llm_calls[2]["prompt"],
        )

    def test_native_exception_then_caption_fallback(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "vision_provider_id": "cap-1",
                "auto_use_astrbot_caption_provider": False,
            },
            context=FakeContext(
                provider_id="prov-1",
                providers={"cap-1": FakeProvider("cap-1")},
            ),
        )
        ctx.llm_responses = [
            RuntimeError("vision unavailable"),
            "caption text",
            "final",
        ]
        results = run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(results[0].text, "final")
        self.assertEqual(len(ctx.llm_calls), 3)

    def test_provider_capability_false_skips_native(self):
        provider = FakeProvider("prov-1", modalities=["text"])
        ctx = FakeContext(
            provider_id="prov-1",
            provider=provider,
            providers={
                "prov-1": provider,
                "cap-1": FakeProvider("cap-1"),
            },
        )
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "vision_provider_id": "cap-1",
                "auto_use_astrbot_caption_provider": False,
            },
        )
        ctx.llm_responses = ["caption only", "final"]
        results = run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(results[0].text, "final")
        self.assertEqual(
            [call["chat_provider_id"] for call in ctx.llm_calls],
            ["cap-1", "prov-1"],
        )

    def test_caption_description_returned_when_no_main_provider(self):
        ctx = FakeContext(
            provider_id="",
            provider=None,
            providers={"cap-1": FakeProvider("cap-1")},
        )
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "vision_provider_id": "cap-1",
                "auto_use_astrbot_caption_provider": False,
            },
        )
        ctx.llm_responses = ["a cat"]
        results = run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(results[0].text, "我看了一下图片：a cat")
        self.assertEqual(len(ctx.llm_calls), 1)

    def test_native_disabled_no_caption_uses_unavailable_reply(self):
        plugin, ctx, _ = make_plugin(
            overrides={
                "enabled": True,
                "native_vision_first": False,
                "auto_use_astrbot_caption_provider": False,
                "vision_provider_id": "",
            }
        )
        ctx.llm_responses = ["unused"]
        results = run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(results[0].text, plugin._cfg_str("image_unavailable_reply"))
        self.assertEqual(ctx.llm_calls, [])

    def test_unreadable_image_uses_unavailable_reply_without_call(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        event = FakeEvent(
            "",
            private=True,
            components=[Comp.Image(base64_data="")],
        )
        results = run(collect_event(plugin, event))
        self.assertEqual(results[0].text, plugin._cfg_str("image_unavailable_reply"))
        self.assertEqual(ctx.llm_calls, [])

    def test_max_images_per_message(self):
        plugin, ctx, _ = make_plugin(
            overrides={"enabled": True, "max_images_per_message": 2}
        )
        ctx.llm_responses = ["ok"]
        event = image_event(
            Comp.Image(url="https://x/a.png"),
            Comp.Image(url="https://x/b.png"),
            Comp.Image(url="https://x/c.png"),
        )
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertEqual(
            ctx.llm_calls[0]["image_urls"],
            ["https://x/a.png", "https://x/b.png"],
        )

    def test_convert_to_base64_fallback(self):
        plugin, ctx, _ = make_plugin(overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        event = FakeEvent(
            "",
            private=True,
            components=[Comp.Image(base64_data="YWJj")],
        )
        results = run(collect_event(plugin, event))
        self.assertEqual(len(results), 1)
        self.assertEqual(ctx.llm_calls[0]["image_urls"], ["base64://YWJj"])

    def test_caption_max_chars_truncates_direct_description(self):
        ctx = FakeContext(
            provider_id="",
            provider=None,
            providers={"cap-1": FakeProvider("cap-1")},
        )
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "vision_provider_id": "cap-1",
                "auto_use_astrbot_caption_provider": False,
                "max_caption_chars": 5,
            },
        )
        ctx.llm_responses = ["abcdefghij"]
        results = run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(results[0].text, "我看了一下图片：abcde")

    def test_global_caption_provider_config_is_used(self):
        ctx = FakeContext(
            provider_id="prov-1",
            providers={"global-cap": FakeProvider("global-cap")},
            global_config={
                "provider_settings": {
                    "default_image_caption_provider_id": "global-cap"
                }
            },
        )
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "vision_provider_id": "",
                "auto_use_astrbot_caption_provider": True,
            },
        )
        ctx.llm_responses = ["", "global caption", "final"]
        results = run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(results[0].text, "final")
        self.assertEqual(
            [call["chat_provider_id"] for call in ctx.llm_calls],
            ["prov-1", "global-cap", "prov-1"],
        )

    def test_llm_timeout_falls_back(self):
        async def slow(_kwargs):
            await asyncio.sleep(2)
            return "late"

        ctx = FakeContext(llm_handler=slow)
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "llm_timeout_seconds": 1,
                "llm_error_reply": "TIMEOUT",
            },
        )
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(results[0].text, "TIMEOUT")

    def test_completion_text_object_supported(self):
        ctx = FakeContext(
            llm_responses=[types.SimpleNamespace(completion_text="from object")]
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        results = run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertEqual(results[0].text, "from object")

    def test_safe_tools_handshake_once_for_image_fallback(self):
        class SafeStar:
            def __init__(self):
                self.calls = 0

            def tool_instruction(self, enabled_tools=None):
                self.calls += 1
                return "SAFE-IMAGE"

        star = SafeStar()
        ctx = FakeContext(
            provider_id="prov-1",
            providers={
                "prov-1": FakeProvider("prov-1"),
                "cap-1": FakeProvider("cap-1"),
            },
            registered_stars={"qqbot_safe_tools": FakeStarMeta(star)},
        )
        plugin, _, _ = make_plugin(
            context=ctx,
            overrides={
                "enabled": True,
                "vision_provider_id": "cap-1",
                "auto_use_astrbot_caption_provider": False,
            },
        )
        ctx.llm_responses = ["", "caption", "final"]
        run(collect_event(plugin, image_event(Comp.Image(url="https://x/a.png"))))
        self.assertEqual(star.calls, 1)
        self.assertIn("SAFE-IMAGE", ctx.llm_calls[0]["system_prompt"])
        self.assertIn("SAFE-IMAGE", ctx.llm_calls[2]["system_prompt"])
        self.assertNotIn("SAFE-IMAGE", ctx.llm_calls[1]["system_prompt"])

    def test_tool_instruction_optional_arg_variant(self):
        class SafeStar:
            def tool_instruction(self, enabled_tools=None):
                return f"tools={enabled_tools!r}"

        ctx = FakeContext(
            registered_stars={"qqbot_safe_tools": FakeStarMeta(SafeStar())}
        )
        plugin, _, _ = make_plugin(context=ctx, overrides={"enabled": True})
        ctx.llm_responses = ["ok"]
        run(collect_event(plugin, FakeEvent("hello", private=True)))
        self.assertIn("tools=None", ctx.llm_calls[0]["system_prompt"])


if __name__ == "__main__":
    unittest.main()
