import unittest

class ExampleTests(unittest.TestCase):
    def test_import(self):
        import main
        self.assertTrue(main.ExamplePlugin)

if __name__ == "__main__":
    unittest.main()
