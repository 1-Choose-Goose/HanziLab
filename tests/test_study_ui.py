import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import desktop
import study_view
from scheduler import Rating, ReviewDirection
from study_database import StudyRepository, utc_now
from study_session import SessionItem


class StudyUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        desktop.load_chinese_fonts()
        cls.app.setStyleSheet(desktop.STYLESHEET)

    def test_compact_pinyin_is_split_without_changing_dictionary_tones(self):
        self.assertEqual(
            desktop.readable_pinyin("一路平安", "yīlùpíng'ān"),
            "yī lù píng ān",
        )
        self.assertEqual(desktop.readable_pinyin("聊天儿", "liáotiānr"), "liáo tiānr")
        self.assertEqual(desktop.readable_pinyin("系上", "jìshang"), "jì shang")

    def test_all_cards_button_opens_complete_card_list(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            repository.add_card("学习", "xué xí", "учиться")
            page = study_view.StudyPage(
                repository,
                lambda *_arguments: [],
                "HanziLab KaiTi CJK",
                "Times New Roman",
                lambda _hanzi, pinyin: pinyin,
            )

            self.assertEqual(page.card_list_button.text(), "Все карточки")
            with patch.object(study_view.CardListDialog, "exec", return_value=0) as show:
                page.card_list_button.click()
            show.assert_called_once_with()

            dialog = study_view.CardListDialog(
                repository,
                "HanziLab KaiTi CJK",
                "Times New Roman",
            )
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertEqual(dialog.table.columnCount(), 3)
            self.assertEqual(dialog.table.item(0, 0).text(), "你好")
            self.assertEqual(dialog.table.item(1, 2).text(), "учиться")
            dialog.close()
            page.shutdown()

    def test_russian_card_side_hides_pinyin_without_changing_stored_translation(self):
        translation = (
            "вещь, предмет [народное]\n"
            "[dōngxī]\n"
            "1) восток и запад\n"
            "[dōngxi]\n"
            "1) предмет, вещь"
        )
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("东西", "dōng xī; dōng xi", translation)
            card = repository.get_cards()[0]
            page = study_view.StudyPage(
                repository,
                lambda *_arguments: [],
                "HanziLab KaiTi CJK",
                "Times New Roman",
                lambda _hanzi, pinyin: pinyin,
            )
            now = utc_now()

            russian_question = SessionItem(
                card=card,
                direction=ReviewDirection.RUSSIAN_TO_CHINESE,
                presentation_id=1,
                shown_at=now,
                is_relearning_repeat=False,
            )
            page.show_item(russian_question)
            self.assertNotIn("dōng", page.question.text())
            self.assertIn("вещь, предмет [народное]", page.question.text())

            russian_answer = replace(
                russian_question,
                direction=ReviewDirection.CHINESE_TO_RUSSIAN,
            )
            page.current_item = russian_answer
            page.show_item(russian_answer)
            page.show_answer()
            self.assertNotIn("dōng", page.answer_primary.text())
            self.assertIn("вещь, предмет [народное]", page.answer_primary.text())

            # Display cleanup must not rewrite card or dictionary source data.
            self.assertEqual(repository.get_cards()[0].translation, translation)
            page.shutdown()

    @patch.object(desktop, "get_examples", return_value=[{
        "chinese": "这是学校。", "pinyin": "zhè shì xué xiào", "translation": "это школа",
    }])
    @patch.object(desktop, "search_entries", return_value=[{
        "hanzi": "学校", "pinyin": "xué xiào", "translation": "школа",
    }])
    def test_dictionary_word_becomes_one_two_sided_card(self, _search, _examples):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            window.resize(1000, 700)
            window.show()
            self.app.processEvents()

            window.search_input.setText("学校")
            window.run_search()
            deadline = time.monotonic() + 20
            while window.current_result is None and time.monotonic() < deadline:
                self.app.processEvents()
                QTest.qWait(10)
            self.assertIsNotNone(window.current_result)
            self.assertEqual(window.current_result["hanzi"], "学校")
            self.assertTrue(window.detail_examples.text())
            self.assertTrue(window.detail_examples.isVisible())
            window.add_card_button.click()
            self.assertEqual(repository.get_card_count(), 1)
            self.assertTrue(window.add_card_button.isEnabled())
            self.assertIn("Из карточек", window.add_card_button.text())

            window.show_page(1)
            self.app.processEvents()
            page = window.study_page
            self.assertIsNotNone(page.current_item)
            self.assertIn(
                page.current_item.direction,
                {
                    ReviewDirection.CHINESE_TO_RUSSIAN,
                    ReviewDirection.RUSSIAN_TO_CHINESE,
                },
            )
            if page.current_item.direction == ReviewDirection.CHINESE_TO_RUSSIAN:
                self.assertEqual(page.question.font().family(), window.hanzi_font_family)
                self.assertGreaterEqual(page.question.font().pointSize(), 60)
                self.assertTrue(page.hidden_pinyin.isVisible())
                self.assertFalse(page.hidden_pinyin.revealed)
                self.assertEqual(page.hidden_pinyin.text(), "")
                window.activateWindow()
                page.setFocus()
                self.app.processEvents()
                QTest.keyClick(page, Qt.Key.Key_Space)
                self.app.processEvents()
                self.assertIn("xué", page.hidden_pinyin.text())
            else:
                self.assertFalse(page.hidden_pinyin.isVisible())

            page.show_answer_button.click()
            self.app.processEvents()
            if page.current_item.direction == ReviewDirection.RUSSIAN_TO_CHINESE:
                self.assertEqual(page.answer_primary.font().family(), window.hanzi_font_family)
                self.assertGreaterEqual(page.answer_primary.font().pointSize(), 60)
            self.assertEqual(page.card_sides.currentIndex(), 1)
            self.assertTrue(page.rating_area.isVisible())
            self.assertEqual(
                [shortcut.key().toString() for shortcut in page.rating_shortcuts.values()],
                ["1", "2", "3", "4"],
            )
            self.assertIn("2 мин", page.rating_buttons[next(iter(page.rating_buttons))].text())
            first_direction = page.current_item.direction
            first_card_id = page.current_item.card.id
            page.rate(Rating.VERY_HARD)
            self.app.processEvents()
            self.assertEqual(page.current_item.card.id, first_card_id)
            self.assertNotEqual(page.current_item.direction, first_direction)
            self.assertEqual(page.session.progress(utc_now()), (0, 30))
            page.show_answer()
            page.rate(Rating.VERY_HARD)
            self.app.processEvents()
            self.assertEqual(page.session.progress(utc_now()), (1, 30))
            window.close()

    def test_dictionary_copybook_button_generates_pdf_for_current_word(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            window.current_result = {"hanzi": "学习"}
            window.detail_stack.setCurrentIndex(1)
            window.resize(1000, 700)
            window.show()
            self.app.processEvents()
            self.assertLessEqual(
                abs(
                    window.copybook_button.geometry().top()
                    - window.add_card_button.geometry().top()
                ),
                1,
            )
            output = Path(folder) / "学习_прописи.pdf"

            with (
                patch.object(
                    desktop.QFileDialog,
                    "getSaveFileName",
                    return_value=(str(output), "PDF (*.pdf)"),
                ),
                patch.object(desktop.QMessageBox, "information"),
            ):
                window.copybook_kaiti_action.trigger()

            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 10_000)
            window.close()

    def test_dictionary_copybook_menu_offers_xingshu(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            window.current_result = {"hanzi": "学习"}
            output = Path(folder) / "学习_行书.pdf"

            with (
                patch.object(
                    desktop.QFileDialog,
                    "getSaveFileName",
                    return_value=(str(output), "PDF (*.pdf)"),
                ),
                patch.object(desktop.QMessageBox, "information"),
            ):
                window.copybook_xingshu_action.trigger()

            self.assertEqual(
                [action.text() for action in window.copybook_menu.actions()],
                ["楷书 · стандартные прописи", "行书 · прописи XingShu"],
            )
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 10_000)
            window.close()

    def test_dictionary_card_can_be_removed_with_confirmation(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("同学", "tóng xué", "одноклассник")
            window = desktop.HanziLabWindow(repository)
            window.current_result = {
                "hanzi": "同学",
                "pinyin": "tóng xué",
                "translation": "одноклассник",
            }
            window.set_card_button_state(True)
            with patch.object(
                desktop.QMessageBox,
                "question",
                return_value=desktop.QMessageBox.StandardButton.Yes,
            ):
                window.add_current_card()
            self.assertEqual(repository.get_card_count(), 0)
            self.assertIn("В карточки", window.add_card_button.text())
            window.close()

    def test_anki_import_uses_only_canonical_dictionary_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            export = Path(folder) / "anki.txt"
            export.write_text(
                "#separator:tab\n学校\twrong pinyin\tневерный перевод\tLeech\n",
                encoding="utf-8",
            )
            window = desktop.HanziLabWindow(repository)
            canonical = {
                "学校": {
                    "hanzi": "学校",
                    "pinyin": "xué xiào",
                    "translation": "школа",
                }
            }
            with (
                patch.object(
                    study_view.QFileDialog,
                    "getOpenFileName",
                    return_value=(str(export), ""),
                ),
                patch.object(
                    study_view,
                    "get_entries_by_hanzi",
                    return_value=canonical,
                ),
                patch.object(study_view.QMessageBox, "exec", return_value=0),
            ):
                window.study_page.import_anki_cards()

                deadline = time.monotonic() + 2
                while repository.get_card_count() == 0 and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)

            card = repository.get_cards()[0]
            self.assertEqual(card.hanzi, "学校")
            self.assertEqual(card.pinyin, "xué xiào")
            self.assertEqual(card.translation, "школа")
            window.close()

    def test_anki_import_reports_every_missing_dictionary_word(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            export = Path(folder) / "anki.txt"
            export.write_text(
                "#separator:tab\n学校\tignored\tignored\n不存在\tignored\tignored\n",
                encoding="utf-8",
            )
            window = desktop.HanziLabWindow(repository)
            canonical = {
                "学校": {
                    "hanzi": "学校",
                    "pinyin": "xué xiào",
                    "translation": "школа",
                }
            }
            detailed_texts: list[str] = []
            with (
                patch.object(
                    study_view.QFileDialog,
                    "getOpenFileName",
                    return_value=(str(export), ""),
                ),
                patch.object(
                    study_view,
                    "get_entries_by_hanzi",
                    return_value=canonical,
                ),
                patch.object(
                    study_view.QMessageBox,
                    "setDetailedText",
                    side_effect=detailed_texts.append,
                ),
                patch.object(study_view.QMessageBox, "exec", return_value=0),
            ):
                window.study_page.import_anki_cards()
                deadline = time.monotonic() + 2
                while not detailed_texts and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)

            self.assertEqual(repository.get_card_count(), 1)
            self.assertEqual(len(detailed_texts), 1)
            self.assertIn("不存在", detailed_texts[0])
            window.close()

    def test_manual_card_autofills_from_dictionary_when_word_exists(self):
        canonical = {
            "学校": {
                "hanzi": "学校",
                "pinyin": "xué xiào",
                "translation": "школа; учебное заведение",
            }
        }
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            with patch.object(
                study_view,
                "get_entries_by_hanzi",
                return_value=canonical,
            ):
                dialog = study_view.ManualCardDialog(
                    repository, "Times New Roman"
                )
                self.assertEqual(dialog.save_button.text(), "Добавить карточку")
                dialog.show()
                self.app.processEvents()
                self.assertTrue(dialog.save_button.isVisible())
                dialog.hanzi_input.setText("学校")
                dialog.lookup_timer.stop()
                dialog.start_dictionary_lookup()

                deadline = time.monotonic() + 2
                while not dialog.autofilled and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)

            self.assertTrue(dialog.autofilled)
            self.assertEqual(dialog.pinyin_input.text(), "xué xiào")
            self.assertEqual(
                dialog.translation_input.toPlainText(),
                "школа; учебное заведение",
            )
            self.assertEqual(
                dialog.hanzi_input.font().family(), study_view.INPUT_KAITI_FAMILY
            )
            dialog.close()

    def test_manual_card_is_added_without_dictionary_lookup(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)

            def add_during_dialog(dialog):
                dialog.hanzi_input.setText("自定义词")
                dialog.lookup_timer.stop()
                dialog.pinyin_input.setText("zì dìng yì cí")
                dialog.translation_input.setPlainText("моё слово")
                dialog.validate_and_add()
                return study_view.QDialog.DialogCode.Rejected

            with patch.object(
                study_view.ManualCardDialog,
                "exec",
                new=add_during_dialog,
            ):
                window.study_page.add_manual_card()

            card = repository.get_cards()[0]
            self.assertEqual(card.hanzi, "自定义词")
            self.assertEqual(card.pinyin, "zì dìng yì cí")
            self.assertEqual(card.translation, "моё слово")
            window.close()

    def test_manual_form_marks_duplicate_red_and_disables_add(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("学校", "xué xiào", "школа")
            dialog = study_view.ManualCardDialog(repository, "Times New Roman")
            dialog.show()
            dialog.hanzi_input.setText("学校")
            self.app.processEvents()

            self.assertTrue(dialog.duplicate)
            self.assertTrue(dialog.hanzi_input.property("duplicate"))
            self.assertFalse(dialog.save_button.isEnabled())
            self.assertIn("уже есть", dialog.dictionary_status.text())
            self.assertEqual(
                dialog.dictionary_status.property("status"), "duplicate"
            )
            self.assertFalse(dialog.priority_button.isVisible())
            dialog.close()

    def test_manual_duplicate_can_raise_mature_card_priority(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("学校", "xué xiào", "школа")
            with closing(repository.connect()) as connection, connection:
                connection.execute(
                    """
                    UPDATE cards SET learning_state='REVIEW', review_count=3,
                        stability=10, current_interval_days=10,
                        next_review_at='2027-01-01T00:00:00+00:00'
                    """
                )
            dialog = study_view.ManualCardDialog(repository, "Times New Roman")
            dialog.show()
            dialog.hanzi_input.setText("学校")
            self.app.processEvents()

            self.assertTrue(dialog.priority_button.isVisible())
            self.assertTrue(dialog.priority_button.isEnabled())
            dialog.priority_button.click()
            self.app.processEvents()

            self.assertTrue(repository.get_card(1).priority_boost)
            self.assertTrue(dialog.hanzi_input.property("duplicate"))
            self.assertFalse(dialog.priority_button.isEnabled())
            self.assertEqual(dialog.priority_button.text(), "Приоритет повышен")
            self.assertIn("появится раньше", dialog.dictionary_status.text())
            dialog.close()

    def test_manual_form_stays_open_and_clears_after_successful_add(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            dialog = study_view.ManualCardDialog(repository, "Times New Roman")
            dialog.show()
            self.app.processEvents()
            dialog.hanzi_input.setText("自定义词")
            dialog.lookup_timer.stop()
            dialog.pinyin_input.setText("zì dìng yì cí")
            dialog.translation_input.setPlainText("моё слово")

            dialog.validate_and_add()
            self.app.processEvents()

            self.assertEqual(repository.get_card_count(), 1)
            self.assertTrue(dialog.isVisible())
            self.assertEqual(dialog.hanzi_input.text(), "")
            self.assertEqual(dialog.pinyin_input.text(), "")
            self.assertEqual(dialog.translation_input.toPlainText(), "")
            self.assertIn("Можно вводить следующее", dialog.dictionary_status.text())
            self.assertTrue(dialog.hanzi_input.hasFocus())
            dialog.close()

    def test_manual_add_updates_sidebar_and_study_queue_without_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            window.show_page(1)
            window.show()
            self.app.processEvents()
            self.assertIsNone(window.study_page.current_item)

            dialog = study_view.ManualCardDialog(repository, "Times New Roman")
            dialog.card_added.connect(window.study_page.manual_card_was_added)
            dialog.hanzi_input.setText("马上")
            dialog.lookup_timer.stop()
            dialog.pinyin_input.setText("mǎ shàng")
            dialog.translation_input.setPlainText("сразу")
            dialog.validate_and_add()
            self.app.processEvents()

            self.assertEqual(window.cards_button.text(), "Карточки · 1")
            self.assertIsNotNone(window.study_page.current_item)
            self.assertEqual(window.study_page.current_item.card.hanzi, "马上")
            dialog.close()
            window.close()

    def test_completed_daily_plan_can_be_extended_from_empty_state(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("一", "yī", "один")
            repository.add_card("二", "èr", "два")
            repository.set_daily_limit(1)
            service = study_view.StudySessionService(repository)
            now = study_view.utc_now()
            service.ensure_daily_session(now)
            with closing(repository.connect()) as connection, connection:
                connection.execute(
                    "UPDATE daily_cards SET completed=1, completed_at=?",
                    (now.isoformat(),),
                )

            window = desktop.HanziLabWindow(repository)
            window.show_page(1)
            window.show()
            self.app.processEvents()
            page = window.study_page

            self.assertIsNone(page.current_item)
            self.assertTrue(page.continue_study_button.isVisible())
            self.assertEqual(page.progress_label.text(), "1 / 1")

            page.continue_study_button.click()
            self.app.processEvents()

            self.assertIsNotNone(page.current_item)
            self.assertFalse(page.continue_study_button.isVisible())
            self.assertEqual(page.progress_label.text(), "0 / 1")
            self.assertEqual(repository.get_daily_limit(), 1)
            window.close()

    def test_rating_number_shortcuts_work_only_after_answer(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            window = desktop.HanziLabWindow(repository)
            window.show_page(1)
            window.show()
            self.app.processEvents()
            page = window.study_page
            self.assertIsNotNone(page.current_item)
            window.activateWindow()
            page.setFocus()
            self.app.processEvents()

            QTest.keyClick(page, Qt.Key.Key_1)
            self.app.processEvents()
            self.assertEqual(repository.review_event_count(), 0)

            QTest.keyClick(page, Qt.Key.Key_Return)
            self.app.processEvents()
            self.assertEqual(page.card_sides.currentIndex(), 1)
            QTest.keyClick(page, Qt.Key.Key_1)
            self.app.processEvents()
            self.assertEqual(repository.review_event_count(), 1)
            window.close()

    def test_zero_also_reveals_pinyin_on_chinese_card_side(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            window = desktop.HanziLabWindow(repository)
            window.show_page(1)
            window.show()
            self.app.processEvents()
            page = window.study_page
            page.current_item = replace(
                page.current_item,
                direction=ReviewDirection.CHINESE_TO_RUSSIAN,
            )
            page.show_item(page.current_item)
            window.activateWindow()
            page.setFocus()
            self.app.processEvents()

            QTest.keyClick(page, Qt.Key.Key_0)
            self.app.processEvents()

            self.assertTrue(page.hidden_pinyin.revealed)
            self.assertIn("nǐ", page.hidden_pinyin.text())
            window.close()

    def test_direct_or_repeated_rating_cannot_skip_an_unseen_card(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            repository.add_card("学校", "xué xiào", "школа")
            window = desktop.HanziLabWindow(repository)
            window.show_page(1)
            window.show()
            self.app.processEvents()
            page = window.study_page

            first_id = page.current_item.card.id
            page.rate(Rating.EASY)
            self.assertEqual(repository.review_event_count(), 0)
            self.assertEqual(page.current_item.card.id, first_id)

            page.show_answer()
            page.rate(Rating.EASY)
            self.assertEqual(repository.review_event_count(), 1)
            self.assertIsNotNone(page.current_item)
            next_presentation = page.current_item.presentation_id

            # Simulates a queued second click from a double click on a rating.
            page.rate(Rating.EASY)
            self.assertEqual(repository.review_event_count(), 1)
            self.assertEqual(page.current_item.presentation_id, next_presentation)
            window.close()

    def test_daily_limit_editor_temporarily_disables_card_shortcuts(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            window = desktop.HanziLabWindow(repository)
            window.show_page(1)
            window.show()
            self.app.processEvents()
            page = window.study_page
            page.show_answer()

            page.daily_limit.setFocus()
            self.app.processEvents()
            self.assertTrue(all(not shortcut.isEnabled() for shortcut in page.rating_shortcuts.values()))
            QTest.keyClick(page.daily_limit, Qt.Key.Key_1)
            self.app.processEvents()
            self.assertEqual(repository.review_event_count(), 0)

            page.setFocus()
            self.app.processEvents()
            self.assertTrue(all(shortcut.isEnabled() for shortcut in page.rating_shortcuts.values()))
            window.close()

    def test_card_examples_are_loaded_without_blocking_the_gui(self):
        started = threading.Event()
        release = threading.Event()

        def slow_examples(_hanzi, _limit, _pinyin):
            started.set()
            release.wait(2)
            return [
                {
                    "chinese": "你好！",
                    "pinyin": "nǐ hǎo",
                    "translation": "здравствуйте",
                }
            ]

        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            with patch.object(desktop, "get_examples", side_effect=slow_examples):
                window = desktop.HanziLabWindow(repository)
                window.show_page(1)
                window.show()
                self.app.processEvents()
                page = window.study_page
                self.assertTrue(started.wait(1))
                page.show_answer()

                before = time.monotonic()
                page.toggle_examples()
                elapsed = time.monotonic() - before
                self.assertLess(elapsed, 0.1)
                self.assertFalse(page.show_examples_button.isEnabled())

                release.set()
                deadline = time.monotonic() + 2
                while not page.examples_scroll.isVisible() and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)
                self.assertTrue(page.examples_scroll.isVisible())
                self.assertIn("здравствуйте", page.card_examples.text())
                window.close()

    def test_search_index_warmup_runs_in_background_after_window_is_shown(self):
        warmed = threading.Event()

        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            with patch.object(desktop, "warm_search_index", side_effect=warmed.set):
                window = desktop.HanziLabWindow(repository)
                window.search_warmup_timer.setInterval(0)
                window.show()
                window.schedule_search_warmup()

                deadline = time.monotonic() + 2
                while not warmed.is_set() and time.monotonic() < deadline:
                    self.app.processEvents()
                    QTest.qWait(10)

                self.assertTrue(warmed.is_set())
                self.assertTrue(window.search_warmup_started)
                window.close()
                window.background_pool.waitForDone(2000)

    def test_hidden_study_page_does_not_start_or_time_a_presentation(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            repository.add_card("你好", "nǐ hǎo", "здравствуйте")
            window = desktop.HanziLabWindow(repository)
            window.show()
            self.app.processEvents()

            self.assertIsNone(window.study_page.current_item)
            connection = repository.connect()
            try:
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM presentations").fetchone()[0],
                    0,
                )
            finally:
                connection.close()

            window.show_page(1)
            self.app.processEvents()
            self.assertIsNotNone(window.study_page.current_item)
            presentation_id = window.study_page.current_item.presentation_id
            first_shown_at = window.study_page.current_item.shown_at
            session = window.study_page.session

            window.show_page(0)
            self.app.processEvents()
            self.assertIsNotNone(window.study_page.current_item)
            self.assertEqual(window.study_page.current_item.presentation_id, presentation_id)
            self.assertIs(window.study_page.session, session)
            QTest.qWait(10)
            window.show_page(1)
            self.app.processEvents()
            self.assertEqual(window.study_page.current_item.presentation_id, presentation_id)
            self.assertGreater(window.study_page.current_item.shown_at, first_shown_at)
            window.close()

    def test_same_russian_text_has_same_font_on_both_sides(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")
            window = desktop.HanziLabWindow(repository)
            text = "одноклассник; одногруппник; школьный товарищ; " * 8
            front_font = window.study_page.russian_card_font(text)
            back_font = window.study_page.russian_card_font(text)
            self.assertEqual(front_font.family(), "Times New Roman")
            self.assertEqual(front_font.pointSize(), back_font.pointSize())
            self.assertEqual(front_font.pointSize(), 15)
            window.close()


if __name__ == "__main__":
    unittest.main()
