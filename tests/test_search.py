import gc
import sqlite3
import tempfile
import unittest
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import database
from database import search_entries
from scripts.import_vocabulary import build_database
from scripts.text_normalization import normalize_pinyin


class ImportTests(unittest.TestCase):
    def test_pinyin_normalization(self):
        self.assertEqual(normalize_pinyin("xué shēng"), "xuesheng")
        self.assertEqual(normalize_pinyin("NÜ3"), "nv")
        for spelling in ("lǜ", "lü4", "lu:4", "lv4", "lu\u0308\u0300"):
            with self.subTest(spelling=spelling):
                self.assertEqual(normalize_pinyin(spelling), "lv")

    def test_import_corrects_and_merges(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "words.txt"
            source.write_text("#separator:tab\n老师\tlǎosh\tУчитель\n老师\tlǎoshī\tпреподаватель\n不行\t\tнельзя\n", encoding="utf-8")
            database = Path(folder) / "test.db"
            stats = build_database(source, database)
            self.assertEqual(stats["source_rows"], 3)
            self.assertEqual(stats["entries"], 2)
            self.assertGreaterEqual(stats["corrections"], 2)

    def test_search_by_all_three_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "words.txt"
            source.write_text("学校\txuéxiào\tшкола\n", encoding="utf-8")
            path = Path(folder) / "test.db"
            build_database(source, path)
            with patch.object(database, "DB_PATH", path):
                try:
                    for query in ("学校", "xue2xiao4", "школа"):
                        self.assertEqual(search_entries(query, 1)[0]["hanzi"], "学校")
                finally:
                    database._close_thread_connection()


class DictionarySearchTests(unittest.TestCase):
    def setUp(self):
        database._close_thread_connection()
        self.temporary_directory = tempfile.TemporaryDirectory()
        folder = Path(self.temporary_directory.name)
        source = folder / "search-words.txt"
        source.write_text(
            "#separator:tab\n"
            "学\txué\tучиться; изучать\n"
            "学校\txuéxiào\tшкола; учебное заведение\n"
            "家\tjiā\tдом; семья; школа философии\n"
            "欢迎\thuānyíng\tприветствовать; добро пожаловать\n"
            "爱\tài\tлюбить; любовь\n"
            "我们\twǒmen\tмы; нас\n"
            "妈妈\tmāma\tмама; мать\n"
            "妈咪\tmāmī\tмамаша; мамочка\n"
            "大学学校\tdàxué xuéxiào\tуниверситетская школа\n"
            "品牌\tpǐnpái\txuexiao brand\n"
            "汉字字型\thànzì zìxíng\tшрифт китайских иероглифов\n"
            "绿色\tlǜsè\tзелёный цвет\n"
            "T恤衫\tT-xùshān\tфутболка\n"
            "\U00030000\thē\tредкий иероглиф\n"
            "多音\ta; i\tразные чтения\n",
            encoding="utf-8",
        )
        self.database_path = folder / "search.db"
        build_database(source, self.database_path)
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO examples(chinese, pinyin, translation) VALUES (?, ?, ?)",
                ("这是汉字字型示例", "zhè shì hànzì zìxíng shìlì", "это пример шрифта"),
            )
            connection.execute(
                """INSERT INTO examples_fts(rowid, chinese, pinyin, translation)
                   VALUES (?, ?, ?, ?)""",
                (
                    cursor.lastrowid,
                    "这是汉字字型示例",
                    "zhe shi hanzi zixing shili",
                    "это пример шрифта",
                ),
            )
        self.path_patch = patch.object(database, "DB_PATH", self.database_path)
        self.path_patch.start()

    def tearDown(self):
        database._close_thread_connection()
        self.path_patch.stop()
        self.temporary_directory.cleanup()

    def test_primary_russian_translation_ranks_above_secondary_meaning(self):
        rows = database.search_entries("школа", 5)
        self.assertEqual(rows[0]["hanzi"], "学校")
        self.assertIn("家", {row["hanzi"] for row in rows})

    def test_exact_translation_variant_ranks_above_longer_word(self):
        rows = database.search_entries("мама", 5)
        self.assertEqual(rows[0]["hanzi"], "妈妈")
        self.assertGreater(rows[1]["rank"], rows[0]["rank"])

    def test_tone_marks_and_tone_numbers_find_same_pinyin(self):
        self.assertEqual(database.search_entries("xué xiào", 1)[0]["hanzi"], "学校")
        self.assertEqual(database.search_entries("xue2xiao4", 1)[0]["hanzi"], "学校")

    def test_latin_query_only_searches_pinyin_not_translation(self):
        rows = database.search_entries("xuexiao", 10)
        self.assertEqual(rows[0]["hanzi"], "学校")
        self.assertNotIn("品牌", {row["hanzi"] for row in rows})

    def test_short_cyrillic_and_pinyin_queries_use_curated_fallback(self):
        self.assertEqual(database.search_entries("мы", 1)[0]["hanzi"], "我们")
        self.assertEqual(database.search_entries("ai4", 1)[0]["hanzi"], "爱")

    def test_short_hanzi_finds_prefix_and_curated_infix(self):
        rows = database.search_entries("学", 20)
        headwords = {row["hanzi"] for row in rows}
        self.assertEqual(rows[0]["hanzi"], "学")
        self.assertIn("学校", headwords)
        self.assertIn("大学学校", headwords)

    def test_chinese_punctuation_does_not_break_lookup(self):
        self.assertEqual(database.search_entries("《学校》", 1)[0]["hanzi"], "学校")
        self.assertEqual(database.search_entries('"""'), [])

    def test_examples_accept_decorated_headword_and_use_fts(self):
        rows = database.get_examples("《汉字字型》")
        self.assertEqual(len(rows), 1)
        self.assertIn("汉字字型", rows[0]["chinese"])

    def test_fts_order_does_not_build_temporary_sort_tree(self):
        with closing(database.connect_dictionary()) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN " + database._ENTRY_FTS_SQL,
                (database._fts_phrase("translation", "школ"), 50),
            ).fetchall()
        descriptions = " ".join(row[3] for row in plan)
        self.assertNotIn("TEMP B-TREE", descriptions)

    def test_stats_falls_back_when_database_has_not_been_analyzed(self):
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("DELETE FROM entries WHERE hanzi = '学'")
            connection.execute("DROP TABLE sqlite_stat1")
        self.assertEqual(database.get_stats()["entries"], 14)

    def test_diaeresis_pinyin_finds_the_same_entry_for_all_tone_styles(self):
        for query in ("lǜ sè", "lü4se4", "lu:4se4", "lvse"):
            with self.subTest(query=query):
                self.assertEqual(database.search_entries(query, 1)[0]["hanzi"], "绿色")

    def test_legacy_pinyin_index_supports_diaeresis_without_false_u_matches(self):
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("INSERT INTO entries_fts(entries_fts) VALUES ('delete-all')")
            rowid = connection.execute("SELECT rowid FROM entries WHERE hanzi='绿色'").fetchone()[0]
            connection.execute(
                "INSERT INTO entries_fts(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
                (rowid, "绿色", "luse", "зеленый цвет"),
            )
            cursor = connection.execute("INSERT INTO entries VALUES ('路涩', 'lùsè', 'тест')")
            connection.execute(
                "INSERT INTO entries_fts(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
                (cursor.lastrowid, "路涩", "luse", "тест"),
            )
        self.assertEqual([row["hanzi"] for row in database.search_entries("lüse")], ["绿色"])
        self.assertEqual([row["hanzi"] for row in database.search_entries("luse")], ["路涩"])

    def test_pinyin_fts_with_legacy_fallback_does_not_build_temporary_sort_tree(self):
        with closing(database.connect_dictionary()) as connection:
            plan = connection.execute(
                "EXPLAIN QUERY PLAN " + database._PINYIN_FTS_SQL,
                (database._pinyin_fts_phrase("lvse"), "lvse", 50),
            ).fetchall()
        self.assertNotIn("TEMP B-TREE", " ".join(row[3] for row in plan))

    def test_legacy_pinyin_filter_runs_before_fetch_limit(self):
        with closing(sqlite3.connect(self.database_path)) as connection, connection:
            connection.execute("INSERT INTO entries_fts(entries_fts) VALUES ('delete-all')")
            connection.execute("UPDATE entries SET rowid=1000 WHERE hanzi='绿色'")
            for index in range(60):
                connection.execute(
                    "INSERT INTO entries(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
                    (100 + index, f"路涩{index}", "lùsè", "тест"),
                )
                connection.execute(
                    "INSERT INTO entries_fts(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
                    (100 + index, f"路涩{index}", "luse", "тест"),
                )
            connection.execute(
                "INSERT INTO entries_fts(rowid, hanzi, pinyin, translation) VALUES (1000, '绿色', 'luse', '')"
            )
        self.assertEqual(database.search_entries("lvse", 1)[0]["hanzi"], "绿色")

    def test_short_pinyin_does_not_match_across_reading_variants(self):
        self.assertNotIn("多音", {row["hanzi"] for row in database.search_entries("ai", 20)})

    def test_mixed_and_extended_hanzi_headwords_remain_searchable(self):
        self.assertEqual(database.search_entries("《Ｔ恤衫》", 1)[0]["hanzi"], "T恤衫")
        self.assertEqual(database.search_entries("\U00030000", 1)[0]["hanzi"], "\U00030000")

    def test_search_index_warmup_is_safe_on_a_small_database(self):
        self.assertIsNone(database.warm_search_index())

    def test_exact_bulk_lookup_uses_dictionary_fields(self):
        rows = database.get_entries_by_hanzi(["学校", "不存在", "学校"])
        self.assertEqual(set(rows), {"学校"})
        self.assertEqual(rows["学校"]["pinyin"], "xuéxiào")
        self.assertEqual(rows["学校"]["translation"], "школа; учебное заведение")

    def test_worker_connections_close_without_resource_warning(self):
        def worker() -> str:
            return database.search_entries("школа", 1)[0]["hanzi"]

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(lambda _: worker(), range(4))), ["学校"] * 4)
            gc.collect()
        resource_warnings = [item for item in caught if issubclass(item.category, ResourceWarning)]
        self.assertEqual(resource_warnings, [])


if __name__ == "__main__":
    unittest.main()
