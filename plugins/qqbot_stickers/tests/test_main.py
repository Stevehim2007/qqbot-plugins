import unittest

class StickerTests(unittest.TestCase):
    def test_importable(self):
        import main
        self.assertTrue(main.StickerPlugin)

if __name__ == "__main__":
    unittest.main()
