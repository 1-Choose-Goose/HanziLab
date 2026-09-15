import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

import desktop
from handwriting import _resample, handwriting_index, recognize_handwriting
from handwriting_view import HandwritingDialog
from stroke_order import load_character_data
from study_database import StudyRepository


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

    def test_damaged_character_does_not_prevent_loading_other_candidates(self):
        data = load_character_data("你")
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "你.json").write_text(json.dumps(data), encoding="utf-8")
            (Path(folder) / "好.json").write_text("[]", encoding="utf-8")
            (Path(folder) / "学.json").write_text(
                '{"strokes": ["M0 0"], "medians": [[[1, "bad"]]]}', encoding="utf-8",
            )
            with patch("stroke_order.STROKE_DATA_DIR", Path(folder)):
                handwriting_index.cache_clear()
                load_character_data.cache_clear()
                try:
                    candidates = handwriting_index()
                    self.assertEqual([character for values in candidates.values() for character, _ in values], ["你"])
                finally:
                    handwriting_index.cache_clear()
                    load_character_data.cache_clear()

    def test_resampling_a_single_requested_point_does_not_divide_by_zero(self):
        self.assertEqual(_resample([(1.0, 2.0), (3.0, 4.0)], count=1), [(1.0, 2.0)])
        self.assertEqual(_resample([(1.0, 2.0)], count=0), [])


class HandwritingUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        handwriting_index()

    def create_window(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        repository = StudyRepository(Path(folder.name) / "study.db")
        window = desktop.HanziLabWindow(study_repository=repository)
        self.addCleanup(window.close)
        return window

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

    def test_new_stroke_clears_candidates_from_the_previous_drawing(self):
        dialog = HandwritingDialog("KaiTi")
        try:
            dialog.finish_recognition(dialog.generation, ["你"], None)
            previous_generation = dialog.generation
            dialog.canvas.strokes = [[(0.1, 0.1), (0.8, 0.1)]]
            dialog.schedule_recognition()
            dialog.select_candidate(0)
            self.assertEqual(dialog.selected_text(), "")
            dialog.finish_recognition(previous_generation, ["好"], None)
            self.assertTrue(all(not button.text() for button in dialog.candidate_buttons))
        finally:
            dialog.close()

    def test_accept_and_reject_cancel_pending_recognition(self):
        for result in (QDialog.DialogCode.Accepted, QDialog.DialogCode.Rejected):
            with self.subTest(result=result):
                dialog = HandwritingDialog("KaiTi")
                dialog.show()
                dialog.canvas.strokes = [[(0.1, 0.1), (0.8, 0.1)]]
                dialog.schedule_recognition()
                generation = dialog.generation
                dialog.accept() if result == QDialog.DialogCode.Accepted else dialog.reject()
                self.assertFalse(dialog.recognition_timer.isActive())
                self.assertGreater(dialog.generation, generation)
                self.assertEqual(dialog.result(), result)

    def test_cancelled_queued_tasks_release_their_references(self):
        started = threading.Event()
        release = threading.Event()

        def warm_index():
            started.set()
            release.wait(5)
            return {}

        with patch("handwriting_view.handwriting_index", side_effect=warm_index):
            dialog = HandwritingDialog("KaiTi")
            try:
                self.assertTrue(started.wait(2))
                dialog.canvas.strokes = [[(0.1, 0.1), (0.8, 0.1)]]
                dialog.start_recognition()
                self.assertEqual(len(dialog.tasks), 2)
                dialog.schedule_recognition()
                self.assertEqual(len(dialog.tasks), 1)
            finally:
                release.set()
                dialog.pool.waitForDone(5000)
                self.app.processEvents()
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
        window = self.create_window()
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
        window = self.create_window()
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
