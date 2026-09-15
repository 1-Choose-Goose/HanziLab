import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts.compact_dictionary import (
    compact_database,
    create_clean_schema,
    split_article,
)
from scripts.create_placeholder_database import create_placeholder_database
from scripts.import_dabkrs import (
    build_indexes,
    create_schema,
    import_dabkrs,
    iter_dabkrs,
)
from scripts.import_vocabulary import load_rows


class DictionaryScriptTests(unittest.TestCase):
    def test_placeholder_database_has_complete_empty_schema(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "hanzi-placeholder.db"
            create_placeholder_database(output)
            with closing(sqlite3.connect(output)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT count(*) FROM entries").fetchone()[0], 0)
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0],
                    0,
                )

    def test_dabkrs_extends_existing_custom_output(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "custom.db"
            source = root / "dabkrs.html"
            source.write_text(
                '<script id="dabkrs_ids_1">\n'
                "['新', 'xīn', 'новый'],\n"
                "</script>\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(database)) as connection:
                create_schema(connection)
                connection.execute(
                    "INSERT INTO entries(hanzi, pinyin, translation) VALUES (?, ?, ?)",
                    ("旧", "jiù", "старый"),
                )
                connection.commit()

            with patch("scripts.import_dabkrs.DEFAULT_DATABASE", root / "missing.db"):
                import_dabkrs(source, database)

            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("SELECT hanzi FROM entries ORDER BY hanzi").fetchall(),
                    [("新",), ("旧",)],
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0],
                    2,
                )

    def test_compaction_uses_rowid_and_preserves_existing_examples(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            database = root / "dictionary.db"
            with closing(sqlite3.connect(database)) as connection:
                create_clean_schema(connection)
                connection.execute(
                    "INSERT INTO entries(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
                    (7, "你好", "nǐ hǎo", "здравствуйте"),
                )
                connection.execute(
                    "INSERT INTO examples(chinese, pinyin, translation) VALUES (?, ?, ?)",
                    ("你好！", "nǐ hǎo", "Здравствуйте!"),
                )
                connection.commit()

            stats = compact_database(database, root / "missing-examples.txt")

            self.assertEqual(stats["articles"], 1)
            self.assertEqual(stats["preserved_examples"], 1)
            self.assertEqual(stats["unique_examples"], 1)
            self.assertEqual(stats["duplicates_removed"], 0)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT chinese, pinyin, translation FROM examples"
                    ).fetchall(),
                    [("你好！", "nǐ hǎo", "Здравствуйте!")],
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM examples_fts_docsize").fetchone()[0],
                    1,
                )
                self.assertEqual(connection.execute("SELECT rowid FROM entries").fetchone()[0], 7)

    def test_compaction_preserves_russian_explanations_containing_hanzi(self):
        translation, examples = split_article("учиться\nсм. также 学习\n我学习 — я учусь")
        self.assertEqual(translation, "учиться\nсм. также 学习")
        self.assertEqual(examples, [("我学习", "я учусь")])

    def test_compaction_does_not_create_a_missing_source_database(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.db"
            with self.assertRaises(FileNotFoundError):
                compact_database(path)
            self.assertFalse(path.exists())

    def test_vocabulary_keeps_different_tones_and_substring_meanings(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "words.tsv"
            source.write_text(
                '中\tzhōng\tсередина\n中\tzhòng\tпопадать в середину\n'
                '门\tmén\tвход\n门\tmén\tвходить\n'
                '学\txué\t"учиться\nизучать"\n'
                '\tempty\tпустой заголовок\n',
                encoding="utf-8",
            )
            entries, corrections = load_rows(source)
        self.assertEqual(entries[0].pinyins, ["zhōng", "zhòng"])
        self.assertEqual(entries[1].translations, ["вход", "входить"])
        self.assertEqual(entries[2].translations, ["учиться\nизучать"])
        self.assertEqual(len(entries), 3)
        self.assertEqual(len(corrections), 1)

    def test_dabkrs_keeps_substring_readings_and_translations(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / "test.db"
            source = root / "source.html"
            source.write_text(
                '<script id="dabkrs_ids_1">\n'
                "['门', 'mén', 'вход'],\n"
                "['门', 'mén', 'вход']\n"
                "</script>\n",
                encoding="utf-8",
            )
            with closing(sqlite3.connect(output)) as connection, connection:
                create_schema(connection)
                connection.execute(
                    "INSERT INTO entries VALUES (?, ?, ?)", ("门", "ménr", "входить")
                )
            stats = import_dabkrs(source, output)
            with closing(sqlite3.connect(output)) as connection:
                row = connection.execute("SELECT pinyin, translation FROM entries").fetchone()
            self.assertEqual(row, ("ménr; mén", "входить\n\nвход"))
            self.assertEqual(stats["parsed"], 2)

    def test_dabkrs_accepts_double_quotes_and_last_row_without_comma(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.html"
            source.write_text(
                '<script id="dabkrs_ids_1">\n["新", "xīn", "новый"]\n</script>\n',
                encoding="utf-8",
            )
            self.assertEqual(list(iter_dabkrs(source)), [(2, "新", "xīn", "новый")])

    def test_dabkrs_rejects_invalid_limit_before_modifying_output(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.html"
            source.write_text("", encoding="utf-8")
            output = Path(folder) / "missing.db"
            for limit in (0, -1):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    import_dabkrs(source, output, limit)
            self.assertFalse(output.exists())

    def test_rebuilding_fts_removes_stale_tokens_and_separates_readings(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            create_schema(connection)
            connection.execute("INSERT INTO entries VALUES ('绿', 'lǜ', 'зелёный')")
            build_indexes(connection)
            connection.execute("UPDATE entries SET pinyin='zhōng; ài', translation='новый'")
            build_indexes(connection)
            self.assertEqual(
                connection.execute(
                    "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", ('translation:"зеленый"',)
                ).fetchall(), [],
            )
            self.assertEqual(
                connection.execute(
                    "SELECT rowid FROM entries_fts WHERE entries_fts MATCH ?", ('pinyin:"zhongai"',)
                ).fetchall(), [],
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM entries_fts_docsize").fetchone()[0], 1
            )


if __name__ == "__main__":
    unittest.main()
