"""China Weather 7-day forecast client and image renderer."""
from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Callable

USER_AGENT = "Mozilla/5.0 (compatible; QQBotStudio/1.0)"
REFERER = "http://www.weather.com.cn/"
CITY_SEARCH = "http://toy1.weather.com.cn/search?cityname={query}"
PAGE_URL = "http://www.weather.com.cn/weather/{code}.shtml"
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
    high: int | None
    low: int | None
    code: str
    wind: str
    icon: str
    description: str = ""


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


class _ForecastHTMLParser(HTMLParser):
    """Extract the 7-day list from weather.com.cn's official forecast page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_list = False
        self.current: dict[str, Any] | None = None
        self.items: list[dict[str, Any]] = []
        self.capture = ""
        self.buffer: list[str] = []
        self.wind_titles: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {k: (v or "") for k, v in attrs}
        if tag == "ul" and "t clearfix" in attrs_map.get("class", ""):
            self.in_list = True
            return
        if not self.in_list:
            return
        if tag == "li":
            self.current = {"date": "", "label": "", "high": None, "low": None, "code": "", "wind": "", "description": ""}
            self.wind_titles = []
            return
        if self.current is None:
            return
        if tag == "h1":
            self.capture = "label"
            self.buffer = []
        elif tag == "p" and "wea" in attrs_map.get("class", ""):
            self.capture = "description"
            self.buffer = []
        elif tag == "p" and "tem" in attrs_map.get("class", ""):
            self.capture = "tem"
            self.buffer = []
        elif tag == "p" and "win" in attrs_map.get("class", ""):
            self.capture = "win"
            self.buffer = []
        elif tag == "big" and "png40" in attrs_map.get("class", ""):
            match = re.search(r"\b([dn]?\d{2})\b", attrs_map.get("class", ""))
            if match and not self.current.get("code"):
                self.current["code"] = match.group(1)
        elif tag == "span" and self.capture == "win":
            title = attrs_map.get("title", "").strip()
            if title:
                self.wind_titles.append(title)

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self.in_list:
            return
        if tag in {"h1", "p"} and self.capture:
            text = "".join(self.buffer).strip()
            if self.capture == "label":
                self.current["label"] = text
                match = re.match(r"(\d{1,2}日)", text)
                if match:
                    self.current["date"] = match.group(1)
            elif self.capture == "description":
                self.current["description"] = text
            elif self.capture == "tem":
                numbers = [int(x) for x in re.findall(r"-?\d+", text)]
                if len(numbers) >= 2:
                    self.current["high"], self.current["low"] = numbers[0], numbers[1]
                elif len(numbers) == 1:
                    self.current["low"] = numbers[0]
            elif self.capture == "win":
                unique = list(dict.fromkeys(self.wind_titles))
                direction = "转".join(unique)
                self.current["wind"] = (direction + text).strip()
            self.capture = ""
            self.buffer = []
        elif tag == "li" and self.current is not None:
            item = self.current
            if item.get("label") or item.get("description"):
                self.items.append(item)
            self.current = None
        elif tag == "ul" and self.in_list:
            self.in_list = False


def parse_forecast_html(payload: str, *, city: str = "") -> Forecast:
    parser = _ForecastHTMLParser()
    try:
        parser.feed(payload)
    except Exception as exc:
        raise WeatherError("天气页面解析失败") from exc
    days: list[ForecastDay] = []
    for row in parser.items[:7]:
        description = str(row.get("description") or "")
        label = str(row.get("label") or "")
        code = str(row.get("code") or "")
        days.append(ForecastDay(
            date=str(row.get("date") or ""),
            label=label,
            high=row.get("high"),
            low=row.get("low"),
            code=code,
            wind=str(row.get("wind") or ""),
            icon=_icon_from_text(description, code),
            description=description,
        ))
    if not days:
        raise WeatherError("天气页面没有可用日期")
    return Forecast(city=city or "本地", days=tuple(days))


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
            description="",
        ))
    if not days:
        raise WeatherError("天气数据没有可用日期")
    return Forecast(city=city or "本地", days=tuple(days))


def fetch_forecast(city: str, *, open_url: Callable[..., Any] | None = None, timeout: float = 10.0) -> Forecast:
    code, name = resolve_city(city, open_url=open_url, timeout=timeout)
    opener = open_url or _opener()
    # Prefer the official 7-day page: it carries the exact descriptions and
    # wind text shown by 中国天气网, not just numeric codes.
    try:
        raw = opener(PAGE_URL.format(code=code), timeout)
        return parse_forecast_html(raw.decode("utf-8", "replace"), city=name)
    except Exception as page_exc:
        try:
            raw = opener(FORECAST_URL.format(code=code), timeout)
            return parse_forecast_payload(raw.decode("utf-8", "replace"), city=name)
        except Exception as json_exc:
            raise WeatherError(f"获取天气失败：{page_exc}") from json_exc


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


def render_forecast_png(forecast: Forecast, *, width: int = 1500, height: int = 900) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), "#eef4fb")
    draw = ImageDraw.Draw(img)
    # Official-style blue masthead, flat seven-column table below it.
    for y in range(176):
        ratio = y / 176
        color = (
            int(30 + 26 * ratio),
            int(111 + 38 * ratio),
            int(178 + 29 * ratio),
        )
        draw.line([(0, y), (width, y)], fill=color)
    title_font, sub_font, label_font, desc_font, temp_font, wind_font, small_font = (
        _font(46), _font(24), _font(26), _font(25), _font(38), _font(22), _font(18)
    )
    draw.text((56, 38), forecast.city, fill="#ffffff", font=title_font)
    draw.text((58, 104), "7天天气预报", fill="#e2f2ff", font=sub_font)
    draw.text((width - 300, 108), "中国天气网 · weather.com.cn", fill="#d8ecff", font=small_font)
    left, top, table_h = 44, 220, 580
    cell_w = (width - left * 2) // max(1, len(forecast.days))
    draw.rounded_rectangle((left, top, width - left, top + table_h), radius=8, fill="#ffffff", outline="#d9e6f2", width=2)
    for i, day in enumerate(forecast.days):
        x = left + i * cell_w
        if i:
            draw.line((x, top + 18, x, top + table_h - 18), fill="#e6eef7", width=1)
        draw.text((x + 22, top + 26), day.label or _weekday(day.date), fill="#1b5e9e", font=label_font)
        if day.date:
            draw.text((x + 22, top + 62), day.date, fill="#8aa0b5", font=small_font)
        _draw_icon(draw, day.icon, x + cell_w // 2, top + 190, 38)
        description = day.description or _description_for_icon(day.icon)
        draw.text((x + 22, top + 292), description, fill="#243b55", font=desc_font)
        temp_y = top + 350
        if day.high is not None and day.low is not None:
            high_text, low_text = f"{day.high}℃", f"{day.low}℃"
            high_w = draw.textlength(high_text, font=temp_font)
            low_w = draw.textlength(low_text, font=temp_font)
            total_w = high_w + 16 + low_w
            start_x = x + (cell_w - total_w) / 2
            draw.text((start_x, temp_y), high_text, fill="#e85d2a", font=temp_font)
            draw.text((start_x + high_w + 16, temp_y), low_text, fill="#2878c8", font=temp_font)
        elif day.low is not None:
            text = f"{day.low}℃"
            text_w = draw.textlength(text, font=temp_font)
            draw.text((x + (cell_w - text_w) / 2, temp_y), text, fill="#2878c8", font=temp_font)
        elif day.high is not None:
            text = f"{day.high}℃"
            text_w = draw.textlength(text, font=temp_font)
            draw.text((x + (cell_w - text_w) / 2, temp_y), text, fill="#e85d2a", font=temp_font)
        wind_lines = _wrap_text(draw, day.wind, wind_font, cell_w - 34)
        for line_index, line in enumerate(wind_lines[:2]):
            draw.text((x + 22, top + 425 + line_index * 30), line, fill="#65798e", font=wind_font)
    draw.text((left + 4, height - 52), "数据来源：中国天气网", fill="#8da4ba", font=small_font)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def _icon_from_text(description: str, code: str) -> str:
    text = description or ""
    if any(word in text for word in ("雪", "冰雹")):
        return "snow"
    if any(word in text for word in ("雨", "雷")):
        return "rain"
    if "晴" in text and "云" not in text and "阴" not in text:
        return "sun"
    if any(word in text for word in ("云", "阴", "雾", "霾")):
        return "cloud"
    return _icon_kind(code)


def _wrap_text(draw, text: str, font, max_width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for char in text:
        candidate = current + char
        if current and draw.textlength(candidate, font=font) > max_width:
            lines.append(current)
            current = char
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines or [""]


def _description_for_icon(kind: str) -> str:
    return {"sun": "晴", "cloud": "多云", "rain": "雨", "snow": "雪"}.get(kind, "多云")


def _weekday(date_text: str) -> str:
    try:
        return "周" + "一二三四五六日"[datetime.strptime(date_text, "%m/%d").weekday()]
    except Exception:
        return date_text


def _draw_icon(draw, kind: str, cx: int, cy: int, r: int) -> None:
    if kind == "sun":
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#ffc83d")
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0), (-.7, -.7), (.7, -.7), (-.7, .7), (.7, .7)):
            draw.line((cx + int(dx * r * 1.4), cy + int(dy * r * 1.4), cx + int(dx * r * 1.9), cy + int(dy * r * 1.9)), fill="#ffc83d", width=5)
        return
    draw.ellipse((cx - r * 1.6, cy - r * .2, cx + r * .4, cy + r * 1.2), fill="#b9c9dc")
    draw.ellipse((cx - r * .8, cy - r * 1.2, cx + r * .9, cy + r * .4), fill="#c9d7e8")
    draw.ellipse((cx - r * .2, cy - r * .7, cx + r * 1.5, cy + r * .8), fill="#dbe6f2")
    if kind == "rain":
        for k in range(3):
            x = cx - r + k * r
            draw.line((x, cy + r * 1.2, x - 6, cy + r * 1.8), fill="#3f9bd8", width=5)
    elif kind == "snow":
        for k in range(3):
            x = cx - r + k * r
            draw.ellipse((x - 4, cy + r * 1.35, x + 4, cy + r * 1.58), fill="#7fb7e8")
    else:
        draw.ellipse((cx - r * .4, cy - r * .5, cx + r * .4, cy + r * .3), fill="#e9f1fa")
