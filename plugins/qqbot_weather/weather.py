"""China Weather 7-day forecast client and image renderer."""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

USER_AGENT = "Mozilla/5.0 (compatible; QQBotStudio/1.0)"
REFERER = "http://www.weather.com.cn/"
CITY_SEARCH = "http://toy1.weather.com.cn/search?cityname={query}"
FORECAST_URL = "http://d1.weather.com.cn/weather_index/{code}.html"
FONT_CANDIDATES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
)
_BUILTIN = {
    "北京": "101010100", "上海": "101020100", "天津": "101030100", "重庆": "101040100",
    "长春": "101060101", "吉林": "101060201", "沈阳": "101070101", "哈尔滨": "101050101",
    "南京": "101190101", "杭州": "101210101", "合肥": "101220101", "福州": "101230101",
    "南昌": "101240101", "济南": "101120101", "郑州": "101180101", "武汉": "101200101",
    "长沙": "101250101", "广州": "101280101", "深圳": "101280601", "南宁": "101300101",
    "海口": "101310101", "成都": "101270101", "贵阳": "101260101", "昆明": "101290101",
    "拉萨": "101140101", "西安": "101110101", "兰州": "101160101", "西宁": "101150101",
    "银川": "101170101", "乌鲁木齐": "101130101", "石家庄": "101090101", "太原": "101100101",
    "呼和浩特": "101080101", "苏州": "101190401", "青岛": "101120201", "大连": "101070201",
}


class WeatherError(RuntimeError):
    pass


@dataclass(frozen=True)
class ForecastDay:
    date: str
    label: str
    high: int
    low: int
    code: str
    wind: str
    icon: str


@dataclass(frozen=True)
class Forecast:
    city: str
    days: tuple[ForecastDay, ...]


def _opener() -> Callable[..., Any]:
    def open_url(url: str, timeout: float) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": REFERER})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(2_000_000)
    return open_url


def parse_city_search(payload: str) -> tuple[str, str] | None:
    text = payload.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    try:
        rows = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    ref = str(rows[0].get("ref") or "")
    parts = ref.split("~")
    if len(parts) < 3 or not parts[0].isdigit():
        return None
    return parts[0], parts[2]


def resolve_city(query: str, *, open_url: Callable[..., Any] | None = None, timeout: float = 10.0) -> tuple[str, str]:
    query = query.strip()
    if not query:
        raise WeatherError("请提供城市名，例如：/天气 长春")
    if query in _BUILTIN:
        return _BUILTIN[query], query
    opener = open_url or _opener()
    try:
        raw = opener(CITY_SEARCH.format(query=urllib.parse.quote(query)), timeout)
        found = parse_city_search(raw.decode("utf-8", "replace"))
    except Exception as exc:
        raise WeatherError(f"查询城市失败：{exc}") from exc
    if not found:
        if query.endswith("市") and query[:-1] in _BUILTIN:
            return _BUILTIN[query[:-1]], query[:-1]
        raise WeatherError(f"没有找到城市「{query}」，请换一个更完整的城市名")
    return found


def parse_forecast_payload(payload: str, *, city: str = "") -> Forecast:
    marker = "var fc ="
    pos = payload.find(marker)
    if pos < 0:
        raise WeatherError("天气数据格式变化，暂无法解析")
    start = payload.find("{", pos + len(marker))
    if start < 0:
        raise WeatherError("天气数据格式变化，暂无法解析")
    try:
        data, _ = json.JSONDecoder().raw_decode(payload[start:])
    except json.JSONDecodeError as exc:
        raise WeatherError("天气数据解析失败") from exc
    rows = data.get("f") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise WeatherError("天气数据为空")
    days: list[ForecastDay] = []
    for row in rows[:7]:
        if not isinstance(row, dict):
            continue
        try:
            high = int(float(str(row.get("fc", "")).strip() or "0"))
            low = int(float(str(row.get("fd", "")).strip() or "0"))
        except ValueError:
            continue
        code = str(row.get("fa") or "00")
        days.append(ForecastDay(
            date=str(row.get("fi") or ""),
            label=str(row.get("fj") or ""),
            high=high,
            low=low,
            code=code,
            wind=str(row.get("fe") or ""),
            icon=_icon_kind(code),
        ))
    if not days:
        raise WeatherError("天气数据没有可用日期")
    return Forecast(city=city or "本地", days=tuple(days))


def fetch_forecast(city: str, *, open_url: Callable[..., Any] | None = None, timeout: float = 10.0) -> Forecast:
    code, name = resolve_city(city, open_url=open_url, timeout=timeout)
    opener = open_url or _opener()
    try:
        raw = opener(FORECAST_URL.format(code=code), timeout)
    except Exception as exc:
        raise WeatherError(f"获取天气失败：{exc}") from exc
    return parse_forecast_payload(raw.decode("utf-8", "replace"), city=name)


def _icon_kind(code: str) -> str:
    code = code.strip().lstrip("dn")
    if code in {"00", "01"}:
        return "sun"
    if code in {"02", "03", "04", "05", "06", "07", "08", "09"}:
        return "cloud"
    if code in {"10", "11", "12", "13", "14", "15"}:
        return "rain"
    if code in {"16", "17", "18", "19"}:
        return "snow"
    return "cloud"


def _font(size: int):
    from PIL import ImageFont
    for path in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_forecast_png(forecast: Forecast, *, width: int = 1400, height: int = 760) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "#f4f7fb")
    draw = ImageDraw.Draw(img)
    # background
    for y in range(height):
        shade = int(244 - 10 * y / height)
        draw.line([(0, y), (width, y)], fill=(shade, shade + 2, 250))
    title_font, sub_font, big_font, mid_font, small_font = _font(44), _font(24), _font(38), _font(22), _font(18)
    draw.text((56, 38), f"{forecast.city} · 未来7天天气", fill="#102033", font=title_font)
    draw.text((58, 94), "数据来源：中国天气网", fill="#65758b", font=small_font)
    n = max(1, len(forecast.days))
    gap = 18
    card_w = (width - 112 - gap * (n - 1)) // n
    card_h = 520
    top = 150
    for i, day in enumerate(forecast.days):
        x = 56 + i * (card_w + gap)
        draw.rounded_rectangle((x, top, x + card_w, top + card_h), radius=22, fill="#ffffff", outline="#dbe5f1", width=2)
        label = day.label or _weekday(day.date)
        draw.text((x + 22, top + 22), label, fill="#17405f", font=mid_font)
        draw.text((x + 22, top + 54), day.date, fill="#7b8da3", font=small_font)
        _draw_icon(draw, day.icon, x + card_w // 2, top + 150, 34)
        draw.text((x + 22, top + 250), f"{day.high}°", fill="#e55a2b", font=big_font)
        draw.text((x + 22, top + 300), f"{day.low}°", fill="#2678c9", font=big_font)
        wind = day.wind[:9]
        if wind:
            draw.text((x + 22, top + 390), wind, fill="#5f7188", font=small_font)
    draw.text((58, height - 42), "QQ Bot Studio · 中国天气网 7 日预报", fill="#8ca0b6", font=small_font)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _weekday(date_text: str) -> str:
    try:
        return "周" + "一二三四五六日"[datetime.strptime(date_text, "%m/%d").weekday()]
    except Exception:
        return date_text


def _draw_icon(draw, kind: str, cx: int, cy: int, r: int) -> None:
    if kind == "sun":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#ffc83d")
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0), (-.7, -.7), (.7, -.7), (-.7, .7), (.7, .7)):
            draw.line((cx + int(dx * r * 1.4), cy + int(dy * r * 1.4), cx + int(dx * r * 1.9), cy + int(dy * r * 1.9)), fill="#ffc83d", width=4)
        return
    # cloud body
    draw.ellipse((cx - r * 1.6, cy - r * .2, cx + r * .4, cy + r * 1.2), fill="#b9c9dc")
    draw.ellipse((cx - r * .8, cy - r * 1.2, cx + r * .9, cy + r * .4), fill="#c9d7e8")
    draw.ellipse((cx - r * .2, cy - r * .7, cx + r * 1.5, cy + r * .8), fill="#dbe6f2")
    if kind == "rain":
        for k in range(3):
            x = cx - r + k * r
            draw.line((x, cy + r * 1.2, x - 6, cy + r * 1.8), fill="#3f9bd8", width=4)
    elif kind == "snow":
        for k in range(3):
            x = cx - r + k * r
            draw.ellipse((x - 3, cy + r * 1.35, x + 3, cy + r * 1.55), fill="#7fb7e8")
    else:
        draw.ellipse((cx - r * .4, cy - r * .5, cx + r * .4, cy + r * .3), fill="#e9f1fa")
