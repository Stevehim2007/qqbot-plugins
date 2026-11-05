import unittest
from main import _html_text, _public_https

class ReaderTests(unittest.TestCase):
    def test_html_text(self):
        self.assertEqual(_html_text("<b>hi</b><script>x</script>"), "hi")
    def test_rejects_http(self):
        with self.assertRaises(ValueError):
            _public_https("http://example.com")

if __name__ == "__main__":
    unittest.main()
