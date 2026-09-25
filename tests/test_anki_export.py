import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from anki_export import export_anki_package


@dataclass
class Card:
    hanzi: str
    pinyin: str
    translation: str


class AnkiExportTests(unittest.TestCase):
    def test_exports_apkg_with_fields_native_hint_and_translation_back(self):
        cards = [
            Card("学校", "xué xiào", "школа"),
            Card("引号", 'yǐn "hào"', "кавычки\nвторая строка"),
        ]
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "cards.apkg"
            count = export_anki_package(cards, target)
            with zipfile.ZipFile(target) as package:
                package.extract("collection.anki2", folder)
            with closing(
                sqlite3.connect(Path(folder) / "collection.anki2")
            ) as connection:
                models = json.loads(
                    connection.execute("SELECT models FROM col").fetchone()[0]
                )
                notes = connection.execute(
                    "SELECT mid, flds FROM notes ORDER BY id"
                ).fetchall()
                card_count = connection.execute(
                    "SELECT count(*) FROM cards"
                ).fetchone()[0]

        self.assertEqual(count, 2)
        self.assertEqual(
            {model["name"] for model in models.values()},
            {
                "HanziLab · Китайский → русский",
                "HanziLab · Русский → китайский",
            },
        )
        for model in models.values():
            self.assertEqual(
                [field["name"] for field in model["flds"]],
                ["Hanzi", "Pinyin", "Translation"],
            )
            self.assertEqual(len(model["tmpls"]), 1)
        models_by_name = {model["name"]: model for model in models.values()}
        direct = models_by_name["HanziLab · Китайский → русский"]["tmpls"][0]
        reverse = models_by_name["HanziLab · Русский → китайский"]["tmpls"][0]
        self.assertIn("{{Hanzi}}", direct["qfmt"])
        self.assertIn("{{hint:Pinyin}}", direct["qfmt"])
        self.assertIn("{{Translation}}", direct["afmt"])
        self.assertIn("{{Translation}}", reverse["qfmt"])
        self.assertIn("{{Hanzi}}", reverse["afmt"])
        self.assertIn("{{hint:Pinyin}}", reverse["afmt"])
        self.assertEqual(card_count, 4)
        fields = [row[1].split("\x1f") for row in notes]
        self.assertEqual(len(fields), 4)
        self.assertEqual({row[0] for row in fields}, {"学校", "引号"})
        school = [row for row in fields if row[0] == "学校"]
        self.assertTrue(all(row[1:3] == ["xué xiào", "школа"] for row in school))
        school_model_names = {
            models[str(mid)]["name"]
            for mid, value in notes
            if value.split("\x1f")[0] == "学校"
        }
        self.assertEqual(
            school_model_names,
            {
                "HanziLab · Китайский → русский",
                "HanziLab · Русский → китайский",
            },
        )
        quotes = [row for row in fields if row[0] == "引号"]
        self.assertEqual(
            {tuple(row[1:3]) for row in quotes},
            {('yǐn "hào"', "кавычки<br>вторая строка")},
        )

    def test_escapes_html_from_card_data(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "cards.apkg"
            export_anki_package(
                [Card("<字>", "p&y", "<script>alert(1)</script>")],
                target,
            )
            with zipfile.ZipFile(target) as package:
                package.extract("collection.anki2", folder)
            with closing(
                sqlite3.connect(Path(folder) / "collection.anki2")
            ) as connection:
                data = connection.execute("SELECT flds FROM notes").fetchone()[0]

        self.assertIn("&lt;字&gt;", data)
        self.assertIn("p&amp;y", data)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", data)
        self.assertNotIn("<script>", data)
