import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from stroke_order import load_character_data
from xingshu_trajectories import (
    CANVAS_SIZE,
    RASTER_SIZE,
    _render_font_mask,
    _simplify,
    load_xingshu_trajectory,
    xingshu_font_family,
    xingshu_supports_character,
)


class XingShuTrajectoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_trajectory_is_extracted_from_real_xingshu_glyph(self):
        trajectory = load_xingshu_trajectory("学")
        mask = _render_font_mask("学")

        self.assertTrue(xingshu_supports_character("学"))
        self.assertIsNotNone(trajectory)
        self.assertIsNotNone(mask)
        self.assertEqual(
            len(trajectory.strokes),
            len(load_character_data("学")["medians"]),
        )
        for stroke in trajectory.strokes:
            self.assertGreaterEqual(len(stroke), 2)
            for x, y in stroke:
                self.assertGreaterEqual(x, 0.0)
                self.assertGreaterEqual(y, 0.0)
                self.assertLessEqual(x, CANVAS_SIZE)
                self.assertLessEqual(y, CANVAS_SIZE)
                column = min(RASTER_SIZE - 1, round(x / CANVAS_SIZE * RASTER_SIZE))
                row = min(RASTER_SIZE - 1, round(y / CANVAS_SIZE * RASTER_SIZE))
                self.assertTrue(mask[row, column])

    def test_trajectory_is_cached(self):
        self.assertIs(
            load_xingshu_trajectory("好"),
            load_xingshu_trajectory("好"),
        )

    def test_missing_font_glyph_is_not_replaced_with_regular_script(self):
        self.assertFalse(xingshu_supports_character("𠀀"))
        self.assertIsNone(load_xingshu_trajectory("𠀀"))

    def test_simplifying_a_retraced_stroke_preserves_its_turnaround(self):
        stroke = ((0.0, 0.0), (20.0, 0.0), (2.0, 0.0))
        self.assertEqual(_simplify(stroke), stroke)

    def test_failed_font_registration_does_not_silently_use_another_font(self):
        xingshu_font_family.cache_clear()
        self.addCleanup(xingshu_font_family.cache_clear)
        with patch("xingshu_trajectories.QFontDatabase.addApplicationFont", return_value=-1):
            self.assertEqual(xingshu_font_family(), "")
            self.assertFalse(xingshu_supports_character("学"))


if __name__ == "__main__":
    unittest.main()
