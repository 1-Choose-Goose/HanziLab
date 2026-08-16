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


if __name__ == "__main__":
    unittest.main()
