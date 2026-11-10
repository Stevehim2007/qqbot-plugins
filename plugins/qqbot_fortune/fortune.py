"""Deterministic daily fortune core (stdlib only)."""
from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")


_LUCK = (
    "大吉", "中吉", "小吉", "吉", "末吉", "凶",
)
_SUITABLE = (
    "清日常", "打肉鸽", "刷材料", "整理基建", "读剧情", "抽卡前先洗手",
    "和群友聊天", "早睡", "吃一顿好的",
)
_AVOID = (
    "上头抽卡", "熬夜刷本", "和群友对线", "冲动碎石", "忘记收菜",
    "在公开场合晒卡", "把理智药当水喝",
)
_ITEMS = (
    "理智药", "源石碎片", "龙门币", "赤金", "装置", "酮凝集组",
    "一杯热茶", "猫猫抱枕", "写满计划的便签",
)
_OPS = (
    ("能天使", "狙击", "今天适合主动出击，但别把弹药一次打空。"),
    ("银灰", "近卫", "先看清局势再落子，效率会比蛮干高。"),
    ("艾雅法拉", "术师", "灵感不错，适合处理积压已久的难题。"),
    ("塞雷娅", "重装", "稳住节奏，你是今天最可靠的那块盾。"),
    ("夜莺", "医疗", "照顾别人之前，也记得给自己留一点休息。"),
    ("德克萨斯", "先锋", "开局要快，但别忘记给后面的人留位置。"),
)


def _rng(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16)


def generate_fortune(*, user_id: str, display_name: str = "", when: datetime | None = None, show_operator: bool = True) -> str:
    now = when or datetime.now(TZ)
    day = now.astimezone(TZ).strftime("%Y-%m-%d")
    n = _rng(f"{user_id}:{day}")
    luck = _LUCK[n % len(_LUCK)]
    suitable = _SUITABLE[(n // 7) % len(_SUITABLE)]
    avoid = _AVOID[(n // 13) % len(_AVOID)]
    item = _ITEMS[(n // 17) % len(_ITEMS)]
    op, cls, advice = _OPS[(n // 23) % len(_OPS)]
    name = display_name.strip() or "博士"
    lines = [
        f"【{name} 的明日方舟每日运势】",
        f"日期：{day}",
        f"综合运势：{luck}",
        f"宜：{suitable}",
        f"忌：{avoid}",
        f"幸运物：{item}",
    ]
    if show_operator:
        lines.extend([f"今日干员：{op}（{cls}）", f"提示：{advice}"])
    lines.append("娱乐向内容，不代表官方；理性游戏，别上头。")
    return "\n".join(lines)

