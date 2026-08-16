import unittest

from stroke_order import STROKE_DATA_DIR, load_character_data


class StrokeOrderDataTests(unittest.TestCase):
    def test_common_character_has_ordered_strokes(self):
        data = load_character_data("你")
        self.assertIsNotNone(data)
        self.assertEqual(len(data["strokes"]), 7)
        self.assertEqual(len(data["strokes"]), len(data["medians"]))

    def test_license_is_bundled(self):
        self.assertTrue((STROKE_DATA_DIR / "ARPHICPL.TXT").exists())


if __name__ == "__main__":
    unittest.main()
