import csv
import tempfile
import unittest
from pathlib import Path

from anki_import import clean_anki_field, parse_anki_export


class AnkiImportTests(unittest.TestCase):
    def test_html_media_tags_and_duplicate_rows_are_cleaned(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "anki.txt"
            source.write_text(
                "#separator:tab\n"
                "#html:true\n"
                "#tags column:4\n"
                "美国<br> [sound:voice.mp3]\tměiguó\tСША\tLeech\n"
                "不行\t\t<div>нельзя&nbsp;идти</div>\t\n"
                "美国\twrong\tне используется\tduplicate\n"
                "hello\thello\tпривет\t\n",
                encoding="utf-8",
            )

            result = parse_anki_export(source)

        self.assertEqual([card.hanzi for card in result.cards], ["美国", "不行"])
        self.assertEqual(result.source_rows, 4)
        self.assertEqual(result.duplicate_rows, 1)
        self.assertEqual(result.invalid_rows, 1)
        self.assertEqual(result.cards[1].pinyin, "")
        self.assertEqual(result.cards[1].translation, "нельзя идти")

    def test_clean_field_removes_sound_without_losing_visible_text(self):
        self.assertEqual(
            clean_anki_field("<div>中国<br>[sound:test.mp3]</div>"),
            "中国",
        )

    def test_nonvisible_script_and_style_contents_are_ignored(self):
        self.assertEqual(
            clean_anki_field("<style>.card { color:red; }</style>中国<script>text()</script>"),
            "中国",
        )

    def test_blank_lines_and_metadata_columns_do_not_shift_card_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "anki.txt"
            source.write_text(
                "\n#separator: comma\n\n#notetype column:1\n#deck column:2\n#tags column:6\n"
                'Basic,Chinese,中国,zhōngguó,"Китай, страна",tag\n',
                encoding="utf-8",
            )
            result = parse_anki_export(source)
        self.assertEqual(result.source_rows, 1)
        self.assertEqual(result.invalid_rows, 0)
        self.assertEqual(result.cards[0].hanzi, "中国")
        self.assertEqual(result.cards[0].translation, "Китай, страна")

    def test_invalid_directives_and_truncated_csv_are_rejected(self):
        for content, error in (
            ("#separator:unknown\n中国\tzhong\tКитай\n", ValueError),
            ("#tags column:0\n中国\tzhong\tКитай\n", ValueError),
            ('中国\tzhong\t"Китай\n', csv.Error),
        ):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / "anki.txt"
                source.write_text(content, encoding="utf-8")
                with self.assertRaises(error):
                    parse_anki_export(source)

    def test_one_or_two_field_notes_can_supply_dictionary_headwords(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "anki.txt"
            source.write_text("中国\n学校\tшкола\nword\tне китайский\n", encoding="utf-8")
            result = parse_anki_export(source)
        self.assertEqual([card.hanzi for card in result.cards], ["中国", "学校"])
        self.assertEqual(result.source_rows, 3)
        self.assertEqual(result.invalid_rows, 1)


if __name__ == "__main__":
    unittest.main()
