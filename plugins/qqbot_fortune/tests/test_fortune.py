import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fortune import generate_fortune  # noqa: E402


class FortuneTests(unittest.TestCase):
    def test_deterministic(self):
        a = generate_fortune(user_id="u1", when=datetime(2026, 11, 10, 12, 0))
        b = generate_fortune(user_id="u1", when=datetime(2026, 11, 10, 18, 0))
        self.assertEqual(a, b)

    def test_user_and_day_differ(self):
        base = generate_fortune(user_id="u1", when=datetime(2026, 11, 10, 12, 0))
        self.assertNotEqual(base, generate_fortune(user_id="u2", when=datetime(2026, 11, 10, 12, 0)))
        self.assertNotEqual(base, generate_fortune(user_id="u1", when=datetime(2026, 11, 11, 12, 0)))

    def test_sections(self):
        text = generate_fortune(user_id="u1", when=datetime(2026, 11, 10, 12, 0), display_name="博士")
        for part in ("综合运势", "宜：", "忌：", "幸运物", "娱乐向"):
            self.assertIn(part, text)


if __name__ == "__main__":
    unittest.main()
