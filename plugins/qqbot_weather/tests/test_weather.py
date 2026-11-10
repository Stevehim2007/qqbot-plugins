import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import weather  # noqa: E402


CITY = '([{"ref":"101060101~jilin~长春~Changchun~长春~Changchun~431~130000~CC~吉林"}])'
DAYS = {
    "f": [
        {"fa": "00", "fc": "5", "fd": "-2", "fi": "11/10", "fj": "今天", "fe": "西南风"},
        {"fa": "01", "fc": "11", "fd": "1", "fi": "11/11", "fj": "明天", "fe": "西南风"},
        {"fa": "02", "fc": "8", "fd": "0", "fi": "11/12", "fj": "周三", "fe": "北风"},
        {"fa": "03", "fc": "6", "fd": "-1", "fi": "11/13", "fj": "周四", "fe": "北风"},
        {"fa": "04", "fc": "4", "fd": "-3", "fi": "11/14", "fj": "周五", "fe": "西北风"},
        {"fa": "05", "fc": "3", "fd": "-4", "fi": "11/15", "fj": "周六", "fe": "西北风"},
        {"fa": "10", "fc": "2", "fd": "-5", "fi": "11/16", "fj": "周日", "fe": "西风"},
    ]
}
FORECAST = "var fc =" + json.dumps(DAYS, ensure_ascii=False) + ";var other={};"


class WeatherParsingTests(unittest.TestCase):
    def test_city_search(self):
        self.assertEqual(weather.parse_city_search(CITY), ("101060101", "长春"))

    def test_city_search_empty(self):
        self.assertIsNone(weather.parse_city_search("()"))

    def test_forecast(self):
        f = weather.parse_forecast_payload(FORECAST, city="长春")
        self.assertEqual(f.city, "长春")
        self.assertEqual(len(f.days), 7)
        self.assertEqual(f.days[0].high, 5)
        self.assertEqual(f.days[0].low, -2)
        self.assertEqual(f.days[2].icon, "cloud")
        self.assertEqual(f.days[4].icon, "cloud")
        self.assertEqual(f.days[6].icon, "rain")

    def test_bad_forecast(self):
        with self.assertRaises(weather.WeatherError):
            weather.parse_forecast_payload("no data")

    def test_image_png(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow unavailable")
        f = weather.parse_forecast_payload(FORECAST, city="长春")
        data = weather.render_forecast_png(f)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertGreater(len(data), 1000)


if __name__ == "__main__":
    unittest.main()
