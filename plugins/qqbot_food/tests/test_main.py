import unittest
from food import build_food_url, validate_food_final_url, FOOD_RESTAURANTS_PATH

class FoodTests(unittest.TestCase):
    def test_url(self):
        url = build_food_url(FOOD_RESTAURANTS_PATH, {"q": "火锅"})
        self.assertTrue(url.startswith("https://food.dlsz.top/api/restaurants?"))
        self.assertTrue(validate_food_final_url(url))

if __name__ == "__main__":
    unittest.main()
