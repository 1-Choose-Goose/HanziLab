import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from study_database import SCHEMA_VERSION, StudyRepository, initialize_study_database


class StudyDatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "study.db"

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_migration_rolls_back_schema_and_preserves_legacy_card(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE cards (
                    id INTEGER PRIMARY KEY, hanzi TEXT UNIQUE,
                    pinyin TEXT, translation TEXT
                );
                INSERT INTO cards VALUES (1, '人', 'rén', 'человек');
                CREATE TABLE presentations (id INTEGER PRIMARY KEY, card_id INTEGER);
                PRAGMA user_version=1;
                """
            )
        with self.assertRaises(sqlite3.OperationalError):
            initialize_study_database(self.database)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                [row[1] for row in connection.execute("PRAGMA table_info(cards)")],
                ["id", "hanzi", "pinyin", "translation"],
            )
            self.assertEqual(
                connection.execute("SELECT * FROM cards").fetchall(),
                [(1, "人", "rén", "человек")],
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='review_events'"
                ).fetchone()
            )

    def test_legacy_due_times_are_normalized_before_sql_comparison(self):
        repository = StudyRepository(self.database)
        repository.add_card("前", "qián", "до")
        repository.add_card("后", "hòu", "после")
        now = datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
        with closing(repository.connect()) as connection, connection:
            connection.execute("PRAGMA user_version=6")
            connection.execute("UPDATE cards SET learning_state='REVIEW'")
            connection.execute(
                "UPDATE cards SET next_review_at=? WHERE id=1",
                ("2026-08-10T13:00:00+05:00",),
            )
            connection.execute(
                "UPDATE cards SET next_review_at=? WHERE id=2",
                ("2026-08-10 10:00:00",),
            )
        repository = StudyRepository(self.database)
        repository.add_daily_rows(
            "2026-08-10",
            [(1, "CHINESE_TO_RUSSIAN", 0), (2, "CHINESE_TO_RUSSIAN", 1)],
            now,
        )
        self.assertEqual(
            [row["id"] for row in repository.get_daily_queue("2026-08-10", now)],
            [1],
        )
        with closing(repository.connect()) as connection:
            self.assertEqual(
                connection.execute("SELECT next_review_at FROM cards ORDER BY id").fetchall()[0][0],
                "2026-08-10T08:00:00+00:00",
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )

    def test_invalid_timestamp_rolls_back_all_data_changes_and_schema_version(self):
        for table, column in (
            ("cards", "last_review_at"),
            ("cards", "next_review_at"),
            ("learning_queue", "available_at"),
        ):
            with self.subTest(table=table, column=column):
                database = self.database.with_name(f"{table}-{column}.db")
                repository = StudyRepository(database)
                repository.add_card("人", "rén", "человек")
                with closing(repository.connect()) as connection, connection:
                    connection.execute("PRAGMA user_version=6")
                    connection.execute(
                        """
                        UPDATE cards SET stability=0, review_count=2,
                            current_interval_days=3,
                            last_review_at='2026-08-10T13:00:00+05:00',
                            next_review_at='2026-08-13T13:00:00+05:00'
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO learning_queue(
                            card_id, direction, available_at, created_at
                        ) VALUES (
                            1, 'RUSSIAN_TO_CHINESE',
                            '2026-08-10T13:00:00+05:00',
                            '2026-08-10T13:00:00+05:00'
                        )
                        """
                    )
                    connection.execute(
                        f"UPDATE {table} SET {column}='malformed timestamp'"
                    )
                with closing(repository.connect()) as connection:
                    original_dump = list(connection.iterdump())

                with self.assertRaises(ValueError):
                    initialize_study_database(database)

                with closing(repository.connect()) as connection:
                    self.assertEqual(list(connection.iterdump()), original_dump)
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0], 6
                    )
                    self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_legacy_learning_queue_offset_does_not_delay_opposite_side(self):
        repository = StudyRepository(self.database)
        repository.add_card("人", "rén", "человек")
        now = datetime(2026, 8, 10, 9, tzinfo=timezone.utc)
        with closing(repository.connect()) as connection, connection:
            connection.execute("PRAGMA user_version=6")
            connection.execute(
                """
                INSERT INTO learning_queue(card_id, direction, available_at, created_at)
                VALUES (1, 'RUSSIAN_TO_CHINESE', ?, ?)
                """,
                ("2026-08-10T13:00:00+05:00", now.isoformat()),
            )
        repository = StudyRepository(self.database)
        self.assertEqual([row["id"] for row in repository.get_immediate_learning(now)], [1])


if __name__ == "__main__":
    unittest.main()
