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
    CELLS_PER_PAGE,
    GRID_COLUMNS,
    CopybookError,
    CopybookStyle,
    _assembled_cells,
    _draw_character_sequence,
    _draw_word_sequence,
    _draw_xingshu_character_sequence,
    _draw_xingshu_word_sequence,
    generate_assembled_copybook,
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
        self.assertEqual(
            suggested_copybook_name("学习学", CopybookStyle.XINGSHU),
            "学习学_прописи_行书.pdf",
        )

    def test_assembled_layout_counts_spacing_after_all_stroke_rows(self):
        entries = (
            (("学", tuple("1234567890")), ("习", tuple("12345"))),
            (("人", tuple("12")),),
        )
        pages = _assembled_cells(entries, row_spacing=3)
        occupied = [index for index, cell in enumerate(pages[0]) if cell is not None]
        first_entry_size = (10 + 4) + (5 + 4)
        self.assertEqual(occupied[:first_entry_size], list(range(first_entry_size)))
        last_used_row = (first_entry_size - 1) // GRID_COLUMNS
        expected_next = (last_used_row + 1 + 3) * GRID_COLUMNS
        self.assertEqual(occupied[first_entry_size], expected_next)

    def test_xingshu_assembled_layout_has_no_stroke_order_cells(self):
        entries = (
            (("学", tuple("12345678")), ("习", tuple("123"))),
            (("人", tuple("12")),),
        )
        pages = _assembled_cells(
            entries,
            row_spacing=3,
            style=CopybookStyle.XINGSHU,
        )
        occupied = [index for index, cell in enumerate(pages[0]) if cell is not None]
        self.assertEqual(occupied[:8], list(range(8)))
        self.assertEqual(occupied[8:], list(range(4 * GRID_COLUMNS, 4 * GRID_COLUMNS + 4)))

    def test_generates_assembled_copybook(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "collection.pdf"
            result = generate_assembled_copybook(
                ["学习", "人"], output, row_spacing=2
            )
            self.assertEqual(result.path, output)
            self.assertEqual(result.characters, ("学", "习", "人"))
            self.assertGreater(output.stat().st_size, 10_000)

    def test_generates_word_page_and_one_page_per_unique_character(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "worksheet.pdf"
            result = generate_hanzi_copybook("学习学", output)

            self.assertEqual(result.path, output)
            self.assertEqual(result.characters, ("学", "习"))
            self.assertEqual(result.missing_characters, ())
            self.assertTrue(result.word_page_added)
            self.assertEqual(result.style, CopybookStyle.KAITI)
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

    def test_generates_xingshu_word_and_character_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "xingshu.pdf"
            result = generate_hanzi_copybook(
                "学习",
                output,
                style=CopybookStyle.XINGSHU,
            )

            self.assertEqual(result.style, CopybookStyle.XINGSHU)
            self.assertTrue(result.word_page_added)
            self.assertGreater(output.stat().st_size, 10_000)
            document = QPdfDocument()
            self.assertEqual(document.load(str(output)), QPdfDocument.Error.None_)
            self.assertEqual(document.pageCount(), 3)
            document.close()
            del document
            gc.collect()
            self.app.processEvents()

    def test_xingshu_character_has_no_progressive_stroke_order(self):
        painter = object()
        with patch("copybook_pdf._draw_xingshu_character") as draw_character:
            _draw_xingshu_character_sequence(painter, "学")

        self.assertEqual(
            [item.args[1] for item in draw_character.call_args_list],
            [0, 1, 2, 3],
        )
        self.assertEqual(draw_character.call_args_list[0].kwargs["opacity"], 1.0)
        self.assertEqual(
            [item.kwargs["opacity"] for item in draw_character.call_args_list[1:]],
            [0.48, 0.48, 0.48],
        )

    def test_xingshu_whole_word_has_one_dark_and_three_tracing_copies(self):
        painter = object()
        with patch("copybook_pdf._draw_xingshu_character") as draw_character:
            _draw_xingshu_word_sequence(painter, ("学", "习"))

        self.assertEqual(draw_character.call_count, 8)
        self.assertEqual(
            [
                (item.args[1], item.args[2])
                for item in draw_character.call_args_list
            ],
            list(enumerate(("学", "习") * 4)),
        )

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

    def test_long_word_continues_on_new_pages_without_losing_tracing_cells(self):
        for style in CopybookStyle:
            with self.subTest(style=style), tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "long.pdf"
                generate_hanzi_copybook("学" * 76, output, style)
                document = QPdfDocument()
                self.assertEqual(document.load(str(output)), QPdfDocument.Error.None_)
                self.assertEqual(document.pageCount(), 3)
                document.close()
                del document
                gc.collect()

    def test_word_renderers_continue_colors_and_character_order_on_next_page(self):
        characters = tuple("学习" * 38)
        strokes = tuple((character,) for character in characters)
        for draw_sequence, values, target in (
            (_draw_word_sequence, strokes, "copybook_pdf._draw_strokes"),
            (_draw_xingshu_word_sequence, characters, "copybook_pdf._draw_xingshu_character"),
        ):
            with self.subTest(renderer=target), patch(target) as draw:
                draw_sequence(object(), values)
                self.assertEqual(draw.call_count, CELLS_PER_PAGE)
                self.assertEqual([item.args[1] for item in draw.call_args_list], list(range(CELLS_PER_PAGE)))
                self.assertEqual(sum(item.kwargs["opacity"] == 1.0 for item in draw.call_args_list), 76)
                draw.reset_mock()
                draw_sequence(object(), values, start_cell=CELLS_PER_PAGE)
                self.assertEqual([item.args[1] for item in draw.call_args_list], [0, 1, 2, 3])
                self.assertTrue(all(item.kwargs["opacity"] == 0.48 for item in draw.call_args_list))

    def test_failed_export_preserves_existing_pdf_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "existing.pdf"
            output.write_bytes(b"original document")
            with (
                patch("copybook_pdf._write_copybook", side_effect=CopybookError("failed")),
                self.assertRaises(CopybookError),
            ):
                generate_hanzi_copybook("学", output)
            self.assertEqual(output.read_bytes(), b"original document")
            self.assertEqual(list(Path(folder).iterdir()), [output])

    def test_suggested_name_fits_filesystem_component_limits(self):
        for style in CopybookStyle:
            name = suggested_copybook_name("𠀀" * 300, style)
            self.assertLessEqual(len(name.encode("utf-8")), 255)
            self.assertTrue(name.endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
