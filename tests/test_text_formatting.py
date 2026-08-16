import tempfile
import unittest
from pathlib import Path

from study_database import StudyRepository
from text_formatting import (
    card_translation_without_pinyin,
    mixed_script_html,
    normalize_display_text,
)


class TextFormattingTests(unittest.TestCase):
    def test_card_translation_hides_pinyin_but_keeps_russian_annotations(self):
        source = (
            "вещь, предмет [народное]\n"
            "[dōngxī]\n"
            "1) восток и запад\n"
            "[dōngxi]\n"
            "•I shàng\n"
            "•II, -shang\n"
            "I [piányi]\n"
            "[А.] грамматическая помета"
        )

        result = card_translation_without_pinyin(source)

        self.assertEqual(
            result,
            "вещь, предмет [народное]\n"
            "1) восток и запад\n"
            "•I\n"
            "•II\n"
            "I\n"
            "[А.] грамматическая помета",
        )

    def test_removes_empty_lines_and_repeated_horizontal_whitespace(self):
        source = (
            "  первая\t  строка \r\n\r\n \t\r\n"
            "  вторая\u200b   строка  "
        )

        self.assertEqual(
            normalize_display_text(source),
            "первая строка\nвторая строка",
        )

    def test_can_turn_non_empty_lines_into_one_compact_line(self):
        self.assertEqual(
            normalize_display_text("一路\n\n  平安", preserve_line_breaks=False),
            "一路 平安",
        )

    def test_mixed_script_html_has_no_empty_html_lines(self):
        rendered = mixed_script_html(
            "  你好  \n\n\t\n  привет   вам ",
            "KaiTi",
            16,
            "Times New Roman",
            12,
        )

        self.assertIn("你好", rendered)
        self.assertIn("привет вам", rendered)
        self.assertNotIn("<br><br>", rendered)

    def test_new_cards_are_saved_in_normalized_form(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = StudyRepository(Path(folder) / "study.db")

            self.assertTrue(
                repository.add_card(
                    "  一路\n平安 ",
                    " yī\t lù  píng  ān ",
                    " счастливого  пути\n\n  всего   доброго ",
                )
            )
            card = repository.get_cards()[0]

            self.assertEqual(card.hanzi, "一路 平安")
            self.assertEqual(card.pinyin, "yī lù píng ān")
            self.assertEqual(
                card.translation,
                "счастливого пути\nвсего доброго",
            )


if __name__ == "__main__":
    unittest.main()
