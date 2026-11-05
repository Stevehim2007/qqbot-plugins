"""Standalone read-only sticker inventory command."""
from __future__ import annotations

from pathlib import Path

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools


def _root() -> Path:
    try:
        base = Path(StarTools.get_data_dir())
    except Exception:
        base = Path(".")
    return base / "collected_stickers"


class StickerPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("表情库", priority=110)
    async def status(self, event: AstrMessageEvent):
        event.should_call_llm(True)
        root = _root()
        try:
            count = len(list(root.glob("*"))) if root.is_dir() else 0
            yield event.plain_result(f"独立表情库：{count} 个文件\n数据目录：{root}")
        except OSError as exc:
            yield event.plain_result(f"表情库读取失败：{exc}")
        event.stop_event()
