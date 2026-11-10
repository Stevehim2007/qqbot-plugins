"""China Weather 7-day image plugin; no LLM calls."""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from . import weather
except ImportError:  # AstrBot loads plugin modules as top-level
    import weather  # type: ignore[no-redef]

PLUGIN_NAME = "qqbot_weather"
PLUGIN_VERSION = "1.0.0"
DEFAULTS = {
    "enabled": True,
    "default_city": "",
    "timeout_seconds": 10,
    "cache_seconds": 1800,
    "max_image_bytes": 5_000_000,
}


@register(PLUGIN_NAME, "QQ Bot Studio", "中国天气网 7 天天气图片", PLUGIN_VERSION)
class WeatherPlugin(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.config_obj = config if isinstance(config, dict) else {}
        self._cache: dict[str, tuple[float, weather.Forecast, bytes]] = {}

    def _cfg(self, key: str, default: Any) -> Any:
        value = self.config_obj.get(key, default)
        return value if isinstance(value, type(default)) else default

    @filter.command("天气", alias={"weather"}, priority=110)
    async def weather_command(self, event: AstrMessageEvent):
        if not self._cfg("enabled", True):
            event.stop_event()
            return
        parts = str(getattr(event, "message_str", "") or "").strip().split(maxsplit=1)
        city = parts[1].strip() if len(parts) > 1 else ""
        if not city:
            city = str(self._cfg("default_city", "") or "").strip()
        if not city:
            yield event.plain_result("用法：/天气 城市，例如 /天气 长春")
            event.stop_event()
            return
        try:
            forecast, png = await asyncio.to_thread(self._get, city)
        except weather.WeatherError as exc:
            yield event.plain_result(str(exc))
            event.stop_event()
            return
        except Exception:
            logger.exception("qqbot_weather failed")
            yield event.plain_result("天气查询暂时失败，请稍后再试。")
            event.stop_event()
            return
        path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="qqbot-weather-", suffix=".png", delete=False) as fh:
                fh.write(png)
                path = Path(fh.name)
            image_cls = getattr(Comp, "Image", None)
            factory = getattr(image_cls, "fromFileSystem", None) if image_cls is not None else None
            if not callable(factory):
                raise RuntimeError("Image component unavailable")
            image = factory(str(path))
            tracker = getattr(event, "track_temporary_local_file", None)
            if callable(tracker):
                try:
                    tracker(str(path))
                except Exception:
                    pass
            summary = f"{forecast.city} 未来7天："
            summary += "；".join(f"{d.label or d.date} {d.high}°/{d.low}°" for d in forecast.days[:4])
            yield event.chain_result([Comp.Plain(summary), image])
        except Exception:
            logger.exception("qqbot_weather image reply failed")
            text = f"{forecast.city} 未来7天：" + "；".join(
                f"{d.label or d.date} {d.high}°/{d.low}°" for d in forecast.days
            )
            yield event.plain_result(text + "\n（天气图片生成失败，已改用文字）")
        finally:
            event.stop_event()

    def _get(self, city: str) -> tuple[weather.Forecast, bytes]:
        key = city.casefold()
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and now - cached[0] < int(self._cfg("cache_seconds", 1800)):
            return cached[1], cached[2]
        timeout = int(self._cfg("timeout_seconds", 10))
        forecast = weather.fetch_forecast(city, timeout=timeout)
        png = weather.render_forecast_png(forecast)
        max_bytes = int(self._cfg("max_image_bytes", 5_000_000))
        if len(png) > max_bytes:
            raise weather.WeatherError("天气图片过大")
        self._cache[key] = (now, forecast, png)
        return forecast, png
