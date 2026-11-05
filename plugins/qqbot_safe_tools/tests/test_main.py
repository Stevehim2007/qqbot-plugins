import unittest
from safe_tools import parse_tool_markers

class SafeTests(unittest.TestCase):
    def test_parse(self):
        result = parse_tool_markers('hello [[tool:send_sticker emotion="happy"]]')
        self.assertTrue(result.had_marker)
        self.assertEqual(result.intents[0].name, "send_sticker")

if __name__ == "__main__":
    unittest.main()
