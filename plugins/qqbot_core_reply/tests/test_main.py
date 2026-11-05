import unittest
class CoreTests(unittest.TestCase):
    def test_package(self):
        import main
        self.assertTrue(main.CoreReplyPlugin)
if __name__ == "__main__":
    unittest.main()
