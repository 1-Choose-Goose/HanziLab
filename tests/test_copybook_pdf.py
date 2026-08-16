import gc
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication

from copybook_pdf import (
    CopybookError,
    _draw_character_sequence,
    _draw_word_sequence,
    generate_hanzi_copybook,
    hanzi_sequence,
    suggested_copybook_name,
    unique_hanzi,
)


class CopybookPdfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_extracts_unique_hanzi_in_word_order(self):
        self.assertEqual(hanzi_sequence("学习学！A"), ("学", "习", "学"))
        self.assertEqual(unique_hanzi("学习学！A"), ("学", "习"))
        self.assertEqual(suggested_copybook_name("学习学"), "学习学_прописи.pdf")

    def test_generates_word_page_and_one_page_per_unique_character(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "worksheet.pdf"
            result = generate_hanzi_copybook("学习学", output)

            self.assertEqual(result.path, output)
            self.assertEqual(result.characters, ("学", "习"))
            self.assertEqual(result.missing_characters, ())
            self.assertTrue(result.word_page_added)
            self.assertGreater(output.stat().st_size, 10_000)
            self.assertEqual(output.read_bytes()[:4], b"%PDF")

            document = QPdfDocument()
            self.assertEqual(document.load(str(output)), QPdfDocument.Error.None_)
            # Whole word first, then one page for each unique character.
            self.assertEqual(document.pageCount(), 3)
            size = document.pagePointSize(0)
            self.assertAlmostEqual(size.width(), 595.0, delta=1.0)
            self.assertAlmostEqual(size.height(), 842.0, delta=1.0)
            document.close()
            del document
            gc.collect()
            self.app.processEvents()

    def test_single_character_keeps_only_its_individual_page(self):
        with tempfile.TemporaryDirectory() as folder:
            result = generate_hanzi_copybook("学", Path(folder) / "single.pdf")

            self.assertEqual(result.characters, ("学",))
            self.assertFalse(result.word_page_added)

    def test_sequence_has_model_ordered_strokes_and_three_tracing_characters(self):
        painter = object()
        strokes = ["stroke-1", "stroke-2"]
        with patch("copybook_pdf._draw_strokes") as draw:
            _draw_character_sequence(painter, strokes)

        self.assertEqual(
            draw.call_args_list,
            [
                call(
                    painter,
                    0,
                    strokes,
                    color="#172126",
                    opacity=1.0,
                ),
                call(
                    painter,
                    1,
                    ["stroke-1"],
                    color="#AAB2B5",
                    opacity=0.48,
                ),
                call(
                    painter,
                    2,
                    strokes,
                    color="#AAB2B5",
                    opacity=0.48,
                ),
                call(
                    painter,
                    3,
                    strokes,
                    color="#AAB2B5",
                    opacity=0.48,
                ),
                call(
                    painter,
                    4,
                    strokes,
                    color="#AAB2B5",
                    opacity=0.48,
                ),
                call(
                    painter,
                    5,
                    strokes,
                    color="#AAB2B5",
                    opacity=0.48,
                ),
            ],
        )

    def test_whole_word_has_one_dark_and_three_translucent_copies(self):
        painter = object()
        first = ("first-stroke",)
        second = ("second-stroke",)
        with patch("copybook_pdf._draw_strokes") as draw:
            _draw_word_sequence(painter, (first, second))

        expected = []
        cell_index = 0
        for paths in (first, second):
            expected.append(
                call(
                    painter,
                    cell_index,
                    list(paths),
                    color="#172126",
                    opacity=1.0,
                )
            )
            cell_index += 1
        for _repetition in range(3):
            for paths in (first, second):
                expected.append(
                    call(
                        painter,
                        cell_index,
                        list(paths),
                        color="#AAB2B5",
                        opacity=0.48,
                    )
                )
                cell_index += 1
        self.assertEqual(draw.call_args_list, expected)

    def test_refuses_text_without_hanzi(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            self.assertRaises(CopybookError),
        ):
            generate_hanzi_copybook("hello", Path(folder) / "empty.pdf")


if __name__ == "__main__":
    unittest.main()
