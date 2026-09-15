import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QColor, QImage, QPalette
from PySide6.QtPdf import QPdfDocument
from PySide6.QtWidgets import QApplication, QComboBox, QLabel, QPushButton

import desktop
from cursive import (
    ASSET_DIR,
    find_components,
    group_samples,
    load_catalog,
    search_cursive,
)
from cursive_copybook import generate_cursive_copybook
from cursive_view import CursivePage


class CursiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        desktop.load_chinese_fonts()

    def dispose_page(self, page):
        page.close()
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_every_catalogue_entry_has_a_readable_sample_and_unique_location(self):
        entries = load_catalog()
        self.assertGreater(len(entries), 2000)
        self.assertEqual(len({e.id for e in entries}), len(entries))
        for entry in entries:
            with self.subTest(sample=entry.id):
                self.assertEqual(len(entry.character), 1)
                self.assertFalse(QImage(str(entry.image_path)).isNull())
                self.assertFalse(QImage(str(ASSET_DIR / entry.label)).isNull())
                self.assertEqual(entry.book_page, entry.page - 6)
        self.assertGreater(len(group_samples()["学"]), 1)
        self.assertEqual(set(group_samples("学习学")), {"学", "习"})
        self.assertEqual(group_samples("abc"), {})

    def test_search_selection_and_variants_keep_correct_handwriting(self):
        page = CursivePage()
        self.addCleanup(self.dispose_page, page)
        page.resize(676, 640)
        page.show()
        self.app.processEvents()
        self.assertFalse(page.save_button.isEnabled())
        page.search.setText("学")
        self.assertEqual(set(page.groups), {"学"})
        page.table.setCurrentCell(0, 0)
        self.assertEqual(page.sample.character, "学")
        self.assertTrue(page.save_button.isEnabled())
        page.variant.setCurrentIndex(page.variant.count() - 1)
        self.assertEqual(page.sample, page.variants[-1])
        self.assertFalse(page.preview.pixmap().isNull())
        page.search.setText("不存在的非汉字 xyz")
        page.search.setText("xyz")
        self.assertIsNone(page.sample)
        self.assertFalse(page.save_button.isEnabled())
        self.assertEqual(page.table.rowCount(), 0)

    def test_missing_character_shows_clickable_components(self):
        page = CursivePage()
        self.addCleanup(self.dispose_page, page)
        page.search.setText("憬")
        self.assertEqual(list(page.groups), ["忄", "景"])
        self.assertEqual(page.count.text(), "憬 → 忄 + 景")
        page.table.setCurrentCell(0, 1)
        self.assertEqual(page.sample.character, "景")
        self.assertTrue(page.save_button.isEnabled())
        self.assertFalse(page.preview.pixmap().isNull())
        page.search.clear()
        self.assertEqual(len(page.groups), len(group_samples()))

    def test_direct_match_is_never_replaced_by_components(self):
        with patch(
            "cursive.load_decompositions",
            side_effect=AssertionError("must not decompose"),
        ):
            result = search_cursive("学")
        self.assertEqual(list(result.groups), ["学"])
        self.assertEqual(result.components, ())

    def test_recursive_breakdown_stops_at_largest_available_part(self):
        ids = {"憬": "⿰忄景", "景": "⿱日京", "京": "⿱亠⿱口小"}
        match = find_components("憬", {"忄", "日", "京", "口"}, ids)
        self.assertEqual(match.parts, ("忄", "日", "京"))
        self.assertEqual(match.unresolved, ())

    def test_unknown_radicals_and_cycles_do_not_claim_complete_breakdown(self):
        match = find_components("甲", {"木"}, {"甲": "⿱木⺮"})
        self.assertEqual(match.parts, ("木",))
        self.assertEqual(match.unresolved, ("⺮",))
        match = find_components("甲", {"木"}, {"甲": "⿰乙木", "乙": "甲"})
        self.assertEqual(match.parts, ("木",))
        self.assertEqual(match.unresolved, ("甲",))
        match = find_components("甲", {"木"}, {"甲": "⿰木？"})
        self.assertEqual(match.unresolved, ("？",))

    def test_repeated_components_share_one_table_cell(self):
        result = search_cursive("淼")
        self.assertEqual(list(result.groups), ["水"])
        self.assertEqual(result.components[0].parts, ("水", "水", "水"))

    def test_mixed_query_keeps_direct_matches_and_reports_unavailable_characters(self):
        result = search_cursive("学憬\U00020000 学")
        self.assertEqual(list(result.groups), ["学", "忄", "景"])
        self.assertEqual(result.missing, ("\U00020000",))

    def test_visible_controls_have_no_book_navigation_or_print_button(self):
        page = CursivePage()
        self.addCleanup(self.dispose_page, page)
        page.show_character("学")
        text = "\n".join(
            w.text() for w in page.findChildren(QLabel) + page.findChildren(QPushButton)
        )
        text += "\n".join(page.variant.itemText(i) for i in range(page.variant.count()))
        for forbidden in ("книг", "страниц", "стр.", "Хуан", "Жочжоу", "Печать"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(page.findChildren(QComboBox), [page.variant])

    def test_cancelled_export_creates_nothing_and_failed_export_reports_error(self):
        page = CursivePage()
        self.addCleanup(self.dispose_page, page)
        page.show_character("学")
        with (
            patch("cursive_view.QFileDialog.getSaveFileName", return_value=("", "")),
            patch("cursive_view.generate_cursive_copybook") as generate,
        ):
            page.save_pdf()
            generate.assert_not_called()
        with (
            patch(
                "cursive_view.QFileDialog.getSaveFileName",
                return_value=("test.pdf", ""),
            ),
            patch(
                "cursive_view.generate_cursive_copybook", side_effect=OSError("denied")
            ),
            patch("cursive_view.QMessageBox.warning") as warning,
        ):
            page.save_pdf()
            warning.assert_called_once()
            self.assertIsNone(page.pdf_path)

    def test_pdf_is_a4_and_contains_only_practice_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "пропись.pdf"
            sample = group_samples()["学"][-1]
            generate_cursive_copybook(sample, path)
            doc = QPdfDocument()
            self.assertEqual(doc.load(str(path)), QPdfDocument.Error.None_)
            self.assertEqual(doc.pageCount(), 1)
            self.assertAlmostEqual(doc.pagePointSize(0).width(), 595.28, delta=1)
            self.assertAlmostEqual(doc.pagePointSize(0).height(), 841.89, delta=1)
            text = doc.getAllText(0).text()
            for forbidden in ("книг", "Хуан", "Жочжоу", "страниц"):
                self.assertNotIn(forbidden, text)
            self.assertIn("Скоропись", text)
            doc.close()
            del doc
            self.app.processEvents()

    def test_dark_system_palette_does_not_make_table_text_white(self):
        original_palette = self.app.palette()
        original_style = self.app.styleSheet()
        self.addCleanup(self.app.setPalette, original_palette)
        self.addCleanup(self.app.setStyleSheet, original_style)
        dark = QPalette(original_palette)
        dark.setColor(QPalette.ColorRole.Text, QColor("white"))
        dark.setColor(QPalette.ColorRole.WindowText, QColor("white"))
        dark.setColor(QPalette.ColorRole.Base, QColor("#202020"))
        self.app.setPalette(dark)
        self.app.setStyleSheet(desktop.STYLESHEET)
        page = CursivePage()
        self.addCleanup(self.dispose_page, page)
        page.show()
        self.app.processEvents()
        self.assertEqual(
            page.table.palette().color(QPalette.ColorRole.Text), QColor("#1B272C")
        )
        self.assertEqual(
            page.search.palette().color(QPalette.ColorRole.Text), QColor("#243139")
        )


if __name__ == "__main__":
    unittest.main()
