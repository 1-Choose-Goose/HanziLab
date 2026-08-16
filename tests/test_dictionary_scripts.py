import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from scripts.compact_dictionary import compact_database, create_clean_schema
from scripts.create_placeholder_database import create_placeholder_database
from scripts.import_dabkrs import create_schema, import_dabkrs


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


if __name__ == "__main__":
    unittest.main()
