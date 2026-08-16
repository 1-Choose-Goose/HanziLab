import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import desktop
from handwriting import recognize_handwriting
from handwriting_view import HandwritingDialog
from stroke_order import load_character_data


def median_strokes(character: str):
    data = load_character_data(character)
    return [
        [(x * 1.7 + 25, (900 - y) * 1.7 + 40) for x, y in stroke]
        for stroke in data["medians"]
    ]


class HandwritingRecognitionTests(unittest.TestCase):
    def test_bundled_strokes_recognize_the_source_character_first(self):
        for character in ("你", "好", "学", "汉"):
            self.assertEqual(
                recognize_handwriting(median_strokes(character), 5)[0],
                character,
            )


class HandwritingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_candidate_can_be_composed_for_search(self):
        dialog = HandwritingDialog("KaiTi")
        self.assertEqual(dialog.composition.font().family(), "Times New Roman")
        dialog.generation = 4
        dialog.finish_recognition(4, ["你", "好"], None)

        dialog.candidate_buttons[0].click()
        self.assertEqual(dialog.selected_text(), "你")
        self.assertEqual(dialog.composition.font().family(), "KaiTi")
        self.assertTrue(dialog.insert_button.isEnabled())
        dialog.close()

    def test_candidate_results_do_not_move_the_drawing_canvas(self):
        dialog = HandwritingDialog("KaiTi")
        dialog.resize(580, 760)
        dialog.show()
        self.app.processEvents()
        initial_dialog_size = dialog.size()
        initial_canvas_geometry = dialog.canvas.geometry()

        dialog.generation = 8
        dialog.finish_recognition(8, list("你好学习中国文字汉语"), None)
        self.app.processEvents()

        self.assertEqual(dialog.size(), initial_dialog_size)
        self.assertEqual(dialog.canvas.geometry(), initial_canvas_geometry)
        dialog.close()

    def test_control_z_undoes_a_stroke_and_then_a_selected_character(self):
        dialog = HandwritingDialog("KaiTi")
        dialog.show()
        self.app.processEvents()
        dialog.canvas.strokes = [[(0.1, 0.1), (0.8, 0.1)]]
        dialog.composition.setText("你")

        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertEqual(dialog.canvas.strokes, [])
        self.assertEqual(dialog.selected_text(), "你")

        QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        self.app.processEvents()
        self.assertEqual(dialog.selected_text(), "")
        dialog.close()

    def test_selected_handwriting_is_inserted_at_search_cursor(self):
        window = desktop.HanziLabWindow()
        window.search_input.setText("学")
        window.search_input.setCursorPosition(0)
        with patch.object(desktop, "HandwritingDialog") as dialog_class:
            dialog = dialog_class.return_value
            dialog.exec.return_value = QDialog.DialogCode.Accepted
            dialog.selected_text.return_value = "你好"

            window.open_handwriting_input()

        self.assertEqual(window.search_input.text(), "你好学")
        window.close()

    def test_search_input_enlarges_hanzi_but_keeps_other_queries_compact(self):
        window = desktop.HanziLabWindow()
        self.assertGreaterEqual(window.search_input.minimumHeight(), 60)

        window.search_input.setText("学习")
        self.assertEqual(window.search_input.font().pointSize(), 22)
        self.assertEqual(
            window.search_input.font().families()[0],
            desktop.INPUT_KAITI_FAMILY,
        )

        window.search_input.setText("учиться")
        self.assertEqual(window.search_input.font().pointSize(), 13)
        window.close()


if __name__ == "__main__":
    unittest.main()
