import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel

import desktop
import study_view
from scheduler import ReviewDirection
from study_database import StudyRepository, utc_now
from study_session import SessionItem


class UiRegressionTests(unittest.TestCase):
    def test_compact_pinyin_preserves_umlaut_colon_notation_and_tones(self):
        self.assertEqual(desktop.readable_pinyin("女孩", "nu:3hai2"), "nu:3 hai2")
        self.assertEqual(desktop.readable_pinyin("女孩", "nǚhái"), "nǚ hái")

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repository = StudyRepository(Path(self.directory.name) / "study.db")

    def test_cancelled_dictionary_tasks_always_notify_completion(self):
        for cancel_during_read in (False, True):
            with self.subTest(cancel_during_read=cancel_during_read):
                task = study_view.DictionaryLookupTask(1, ["学校"])
                completions = []
                task.signals.finished.connect(
                    lambda *args, results=completions: results.append(args)
                )
                if not cancel_during_read:
                    task.cancel()

                def lookup(_words, worker=task):
                    worker.cancel()
                    return {"学校": {"translation": "школа"}}

                with patch.object(study_view, "get_entries_by_hanzi", side_effect=lookup) as read:
                    task.run()
                self.assertEqual(read.call_count, int(cancel_during_read))
                self.assertEqual(len(completions), 1)
                self.assertIsNone(completions[0][1])
                self.assertIsInstance(completions[0][2], Exception)

    def test_rejecting_manual_dialog_cancels_pending_lookup(self):
        dialog = study_view.ManualCardDialog(self.repository, "Times New Roman")
        dialog.show()
        dialog.hanzi_input.setText("学校")
        old_generation = dialog.lookup_generation
        task = study_view.DictionaryLookupTask(old_generation, ["学校"])
        dialog.lookup_tasks.add(task)
        self.assertTrue(dialog.lookup_timer.isActive())

        dialog.reject()

        self.assertFalse(dialog.lookup_timer.isActive())
        self.assertTrue(task.cancelled)
        self.assertGreater(dialog.lookup_generation, old_generation)
        dialog.apply_dictionary_lookup(old_generation, {
            "学校": {"pinyin": "xué xiào", "translation": "школа"},
        }, None)
        self.assertEqual(dialog.pinyin_input.text(), "")
        dialog.deleteLater()

    def test_manual_lookup_uses_same_normalization_as_saved_card(self):
        dialog = study_view.ManualCardDialog(self.repository, "Times New Roman")
        dialog.hanzi_input.setText(" 学\u200b校 ")
        dialog.lookup_timer.stop()
        with patch.object(study_view.QThreadPool, "globalInstance") as pool:
            dialog.start_dictionary_lookup()
        task = pool.return_value.start.call_args.args[0]
        self.assertEqual(task.words, ("学校",))
        dialog.apply_dictionary_lookup(dialog.lookup_generation, {
            "学校": {"pinyin": "xué xiào", "translation": "школа"},
        }, None)
        self.assertEqual(dialog.pinyin_input.text(), "xué xiào")
        dialog.reject()
        dialog.deleteLater()

    def test_font_change_preserves_revealed_pinyin_and_updates_empty_mark(self):
        self.repository.add_card("学校", "xué xiào", "школа")
        page = study_view.StudyPage(
            self.repository, lambda *_args: [], "KaiTi", "Times New Roman",
            lambda _hanzi, pinyin: pinyin,
        )
        item = SessionItem(
            card=self.repository.get_cards()[0],
            direction=ReviewDirection.CHINESE_TO_RUSSIAN,
            presentation_id=1, shown_at=utc_now(), is_relearning_repeat=False,
        )
        page.current_item = item
        page.show_item(item)
        page.hidden_pinyin.reveal()

        page.set_chinese_font("QXyingbixing")

        self.assertTrue(page.hidden_pinyin.revealed)
        self.assertEqual(page.hidden_pinyin.text(), "xué xiào")
        self.assertEqual(page.empty_mark.font().family(), "QXyingbixing")
        page.shutdown()
        page.deleteLater()

    def test_dictionary_data_is_displayed_as_literal_text(self):
        row = desktop.ResultRow({
            "hanzi": "学校", "pinyin": "xué xiào", "translation": "<b>школа</b>",
        })
        self.assertEqual(
            row.findChild(QLabel, "resultTranslation").textFormat(),
            Qt.TextFormat.PlainText,
        )
        with patch.object(desktop, "get_stats", return_value={"entries": 0}):
            window = desktop.HanziLabWindow(self.repository)
        try:
            self.assertEqual(window.detail_translation.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(window.study_page.question.textFormat(), Qt.TextFormat.PlainText)
            self.assertEqual(window.study_page.answer_primary.textFormat(), Qt.TextFormat.PlainText)
            window.update_search_input_font("\U00020000")
            self.assertEqual(window.search_input.font().pointSize(), 22)
        finally:
            window.close()
            window.deleteLater()
            row.deleteLater()

    def test_cursive_catalogue_is_created_only_when_opened(self):
        with patch.object(desktop, "get_stats", return_value={"entries": 0}):
            window = desktop.HanziLabWindow(self.repository)
        try:
            self.assertIsNone(window.cursive_page)
            window.show_page(2)
            self.assertIsNotNone(window.cursive_page)
            created = window.cursive_page
            window.show_page(0)
            window.show_page(2)
            self.assertIs(window.cursive_page, created)
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
