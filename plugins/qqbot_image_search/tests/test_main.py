import unittest
from main import _extract_image_urls

class ImageTests(unittest.TestCase):
    def test_extract(self):
        page = 'murl&quot;:&quot;https://example.com/a.jpg&quot;'
        self.assertEqual(_extract_image_urls(page), ["https://example.com/a.jpg"])

if __name__ == "__main__":
    unittest.main()
