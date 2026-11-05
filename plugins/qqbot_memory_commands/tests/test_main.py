import unittest
from memory_commands import Options, ParsedCommand, parse_command

class MemoryTests(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_command("/记忆 搜索 生日").action, "search")
    def test_options(self):
        self.assertFalse(Options().enabled)

if __name__ == "__main__":
    unittest.main()
