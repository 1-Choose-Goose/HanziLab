import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from stroke_order import STROKE_DATA_DIR, load_character_data


class StrokeOrderDataTests(unittest.TestCase):
    def setUp(self):
        load_character_data.cache_clear()
        self.addCleanup(load_character_data.cache_clear)

    def test_common_character_has_ordered_strokes(self):
        data = load_character_data("你")
        self.assertIsNotNone(data)
        self.assertEqual(len(data["strokes"]), 7)
        self.assertEqual(len(data["strokes"]), len(data["medians"]))

    def test_license_is_bundled(self):
        self.assertTrue((STROKE_DATA_DIR / "ARPHICPL.TXT").exists())

    def test_invalid_data_is_ignored_without_crashing_consumers(self):
        invalid_values = (
            [], None, "invalid", {"strokes": "path"}, {"strokes": [None]},
            {"strokes": [""]}, {"strokes": ["M0 0"], "medians": None},
            {"strokes": ["M0 0"], "medians": [[]]},
            {"strokes": ["M0 0"], "medians": [[[1, 2, 3]]]},
            {"strokes": ["M0 0"], "medians": [[[1, "x"]]]},
            {"strokes": ["M0 0"], "medians": [[[1, float("nan")]]]},
            {"strokes": ["M0 0"], "medians": [[[1, 2]], [[3, 4]]]},
        )
        with tempfile.TemporaryDirectory() as folder, patch("stroke_order.STROKE_DATA_DIR", Path(folder)):
            for value in invalid_values:
                with self.subTest(value=value):
                    (Path(folder) / "学.json").write_text(json.dumps(value), encoding="utf-8")
                    load_character_data.cache_clear()
                    self.assertIsNone(load_character_data("学"))
            (Path(folder) / "学.json").write_bytes(b"\xff")
            load_character_data.cache_clear()
            self.assertIsNone(load_character_data("学"))

    def test_loader_rejects_paths_and_multi_character_queries(self):
        with patch("stroke_order.Path.open") as open_file:
            for character in ("", "学习", "../学", "..\\学", "/", "\\"):
                self.assertIsNone(load_character_data(character))
            open_file.assert_not_called()


if __name__ == "__main__":
    unittest.main()
