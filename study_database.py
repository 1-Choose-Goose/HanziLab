from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from scheduler import (
    CardState,
    Rating,
    ReviewDirection,
    ScheduleOutcome,
    ensure_aware,
    parse_datetime,
)
from text_formatting import normalize_display_text

ROOT = Path(__file__).resolve().parent
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else ROOT
STUDY_DB_PATH = APP_ROOT / "data" / "study.db"
SCHEMA_VERSION = 7
DEFAULT_DAILY_REVIEW_LIMIT = 30
DAILY_SESSION_LIMIT_PREFIX = "daily_session_limit:"
DAILY_BATCH_START_PREFIX = "daily_batch_start:"


CARD_COLUMNS: dict[str, str] = {
    "difficulty": "REAL NOT NULL DEFAULT 5.0",
    "stability": "REAL NOT NULL DEFAULT 0.0",
    "last_review_at": "TEXT",
    "next_review_at": "TEXT",
    "current_interval_days": "REAL NOT NULL DEFAULT 0.0",
    "review_count": "INTEGER NOT NULL DEFAULT 0",
    "lapse_count": "INTEGER NOT NULL DEFAULT 0",
    "last_rating": "TEXT",
    "learning_state": "TEXT NOT NULL DEFAULT 'NEW'",
    "learning_step": "INTEGER NOT NULL DEFAULT 0",
    "priority_boost": "INTEGER NOT NULL DEFAULT 0",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_storage(value: datetime | None) -> str | None:
    if value is None:
        return None
    return ensure_aware(value).isoformat()


def local_date(value: datetime) -> str:
    return ensure_aware(value).astimezone().date().isoformat()


def initialize_study_database(database: Path = STUDY_DB_PATH) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database)) as connection, connection:
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if current_version > SCHEMA_VERSION:
            raise RuntimeError(
                "База карточек создана более новой версией HanziLab "
                f"(схема {current_version}, поддерживается {SCHEMA_VERSION})."
            )
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        # DDL is otherwise committed immediately, and executescript also
        # commits any open transaction before running its first statement.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cards (
                id INTEGER PRIMARY KEY,
                hanzi TEXT NOT NULL UNIQUE,
                pinyin TEXT NOT NULL,
                translation TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        existing = {
            row[1] for row in connection.execute("PRAGMA table_info(cards)").fetchall()
        }
        for name, definition in CARD_COLUMNS.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE cards ADD COLUMN {name} {definition}")

        schema = """
            CREATE TABLE IF NOT EXISTS review_events (
                id INTEGER PRIMARY KEY,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                reviewed_at TEXT NOT NULL,
                rating TEXT NOT NULL,
                direction TEXT NOT NULL,
                state_before TEXT NOT NULL,
                state_after TEXT NOT NULL,
                previous_interval_days REAL NOT NULL,
                new_interval_days REAL NOT NULL,
                difficulty_before REAL NOT NULL,
                difficulty_after REAL NOT NULL,
                stability_before REAL NOT NULL,
                stability_after REAL NOT NULL,
                retrievability_before REAL NOT NULL,
                response_time_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS daily_cards (
                local_date TEXT NOT NULL,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                position INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0,
                selected_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY(local_date, card_id)
            );
            CREATE TABLE IF NOT EXISTS presentations (
                id INTEGER PRIMARY KEY,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                shown_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS learning_queue (
                id INTEGER PRIMARY KEY,
                card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
                direction TEXT NOT NULL,
                available_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS direction_ratings (
                card_id INTEGER PRIMARY KEY
                    REFERENCES cards(id) ON DELETE CASCADE,
                chinese_to_russian_rating TEXT,
                russian_to_chinese_rating TEXT,
                started_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cards_due
                ON cards(learning_state, next_review_at);
            CREATE INDEX IF NOT EXISTS idx_review_events_card
                ON review_events(card_id, reviewed_at);
            CREATE INDEX IF NOT EXISTS idx_presentations_active
                ON presentations(card_id, completed_at);
            CREATE INDEX IF NOT EXISTS idx_learning_queue_available
                ON learning_queue(completed_at, available_at);
            """
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)
        # Старые версии не запрещали несколько незавершённых показов/повторов
        # одной логической карточки. Оставляем самую свежую запись и закрепляем
        # инвариант частичными UNIQUE-индексами.
        connection.execute(
            """
            UPDATE presentations
            SET completed_at=shown_at
            WHERE completed_at IS NULL AND id NOT IN (
                SELECT max(id) FROM presentations
                WHERE completed_at IS NULL GROUP BY card_id
            )
            """
        )
        connection.execute(
            """
            UPDATE learning_queue
            SET completed_at=created_at
            WHERE completed_at IS NULL AND id NOT IN (
                SELECT max(id) FROM learning_queue
                WHERE completed_at IS NULL GROUP BY card_id
            )
            """
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_presentations_one_active
               ON presentations(card_id) WHERE completed_at IS NULL"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_learning_queue_one_pending
               ON learning_queue(card_id) WHERE completed_at IS NULL"""
        )
        connection.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES ('daily_review_limit', ?)",
            (str(DEFAULT_DAILY_REVIEW_LIMIT),),
        )
        if current_version < 5:
            # Незавершённые повторы старой схемы уже имеют оценённую первую
            # сторону в review_events. Переносим её, чтобы обновление не
            # заставляло пользователя проходить эту сторону заново.
            connection.execute(
                """
                INSERT OR IGNORE INTO direction_ratings(
                    card_id, chinese_to_russian_rating,
                    russian_to_chinese_rating, started_at, updated_at
                )
                SELECT q.card_id,
                       CASE WHEN e.direction='CHINESE_TO_RUSSIAN'
                            THEN e.rating END,
                       CASE WHEN e.direction='RUSSIAN_TO_CHINESE'
                            THEN e.rating END,
                       e.reviewed_at, e.reviewed_at
                FROM learning_queue q
                JOIN review_events e ON e.id=(
                    SELECT max(previous.id) FROM review_events previous
                    WHERE previous.card_id=q.card_id
                )
                WHERE q.completed_at IS NULL
                """
            )
        # Безопасная миграция: зрелость старой карточки восстанавливается из
        # доступного интервала/счётчика, а не сбрасывается в NEW.
        connection.execute(
            """
            UPDATE cards
            SET stability = CASE
                    WHEN stability <= 0 AND current_interval_days > 0
                    THEN current_interval_days ELSE stability END,
                learning_state = CASE
                    WHEN (review_count > 0 OR current_interval_days > 0)
                         AND learning_state = 'NEW' THEN 'REVIEW'
                    ELSE learning_state END
            """
        )
        if current_version < 7:
            # SQL compares due dates as text. Legacy ISO timestamps may use a
            # local UTC offset or a space separator, which changes that order.
            for table, columns in (
                ("cards", ("last_review_at", "next_review_at")),
                ("learning_queue", ("available_at",)),
            ):
                for column in columns:
                    rows = connection.execute(
                        f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
                    ).fetchall()
                    connection.executemany(
                        f"UPDATE {table} SET {column}=? WHERE id=?",
                        [(to_storage(parse_datetime(value)), row_id) for row_id, value in rows],
                    )
        if current_version < SCHEMA_VERSION:
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


class StudyRepository:
    def __init__(self, database: Path = STUDY_DB_PATH) -> None:
        self.database = database
        initialize_study_database(database)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def add_card(self, hanzi: str, pinyin: str, translation: str) -> bool:
        hanzi = normalize_display_text(hanzi, preserve_line_breaks=False)
        pinyin = normalize_display_text(pinyin, preserve_line_breaks=False)
        translation = normalize_display_text(translation)
        if not hanzi or not pinyin or not translation:
            return False
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO cards(hanzi, pinyin, translation) VALUES (?, ?, ?)",
                (hanzi, pinyin, translation),
            )
            return cursor.rowcount == 1

    def add_cards(self, cards: Iterable[tuple[str, str, str]]) -> int:
        """Атомарно добавляет карточки, не перезаписывая существующие."""
        unique: dict[str, tuple[str, str, str]] = {}
        for raw_hanzi, raw_pinyin, raw_translation in cards:
            hanzi = normalize_display_text(
                raw_hanzi, preserve_line_breaks=False
            )
            pinyin = normalize_display_text(
                raw_pinyin, preserve_line_breaks=False
            )
            translation = normalize_display_text(raw_translation)
            if hanzi and pinyin and translation and hanzi not in unique:
                unique[hanzi] = (hanzi, pinyin, translation)
        if not unique:
            return 0
        with closing(self.connect()) as connection, connection:
            before = connection.total_changes
            connection.executemany(
                "INSERT OR IGNORE INTO cards(hanzi, pinyin, translation) VALUES (?, ?, ?)",
                unique.values(),
            )
            return connection.total_changes - before

    def has_card(self, hanzi: str) -> bool:
        hanzi = normalize_display_text(hanzi, preserve_line_breaks=False)
        with closing(self.connect()) as connection:
            return connection.execute(
                "SELECT 1 FROM cards WHERE hanzi = ?", (hanzi,)
            ).fetchone() is not None

    def can_raise_card_priority(self, hanzi: str) -> bool:
        """Проверяет, нужна ли существующей карточке ручная приоритизация."""
        hanzi = normalize_display_text(hanzi, preserve_line_breaks=False)
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT c.priority_boost, c.learning_state,
                       EXISTS(
                           SELECT 1 FROM presentations p
                           WHERE p.card_id=c.id AND p.completed_at IS NULL
                       ) AS has_active,
                       EXISTS(
                           SELECT 1 FROM learning_queue q
                           WHERE q.card_id=c.id AND q.completed_at IS NULL
                       ) AS has_pending,
                       EXISTS(
                           SELECT 1 FROM direction_ratings r
                           WHERE r.card_id=c.id
                       ) AS has_direction_rating
                FROM cards c WHERE c.hanzi=?
                """,
                (hanzi,),
            ).fetchone()
        if row is None:
            return False
        return not (
            bool(row["priority_boost"])
            or row["learning_state"] in {"NEW", "LEARNING"}
            or bool(row["has_active"])
            or bool(row["has_pending"])
            or bool(row["has_direction_rating"])
        )

    def raise_card_priority(self, hanzi: str) -> bool:
        """Поднимает обычную карточку до ближайшего полного прохождения."""
        hanzi = normalize_display_text(hanzi, preserve_line_breaks=False)
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE cards SET priority_boost=1
                WHERE hanzi=? AND priority_boost=0
                  AND learning_state NOT IN ('NEW', 'LEARNING')
                  AND NOT EXISTS (
                      SELECT 1 FROM presentations p
                      WHERE p.card_id=cards.id AND p.completed_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM learning_queue q
                      WHERE q.card_id=cards.id AND q.completed_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM direction_ratings r
                      WHERE r.card_id=cards.id
                  )
                """,
                (hanzi,),
            )
            return cursor.rowcount == 1

    def remove_card(self, hanzi: str) -> bool:
        """Удаляет логическую карточку и связанные данные через FK CASCADE."""
        hanzi = normalize_display_text(hanzi, preserve_line_breaks=False)
        with closing(self.connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM cards WHERE hanzi = ?", (hanzi,)
            )
            return cursor.rowcount == 1

    def get_card(self, card_id: int) -> CardState | None:
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
        return CardState.from_mapping(dict(row)) if row else None

    def get_cards(self) -> list[CardState]:
        with closing(self.connect()) as connection:
            rows = connection.execute("SELECT * FROM cards ORDER BY id").fetchall()
        return [CardState.from_mapping(dict(row)) for row in rows]

    def get_daily_candidates(
        self, date_key: str, now: datetime, limit: int
    ) -> list[CardState]:
        """Return only the highest-priority cards needed to fill today's queue."""
        if limit <= 0:
            return []
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.*
                FROM cards c
                WHERE NOT EXISTS (
                    SELECT 1 FROM daily_cards d
                    WHERE d.local_date=? AND d.card_id=c.id
                )
                  AND (
                    c.priority_boost=1
                    OR c.learning_state='NEW'
                    OR c.next_review_at IS NULL
                    OR c.next_review_at <= ?
                  )
                ORDER BY
                    CASE
                        WHEN c.priority_boost=1 THEN 0
                        WHEN c.learning_state='LEARNING' THEN 1
                        WHEN c.learning_state='NEW' THEN 2
                        ELSE 3
                    END,
                    c.next_review_at,
                    c.id
                LIMIT ?
                """,
                (date_key, to_storage(now), int(limit)),
            ).fetchall()
        return [CardState.from_mapping(dict(row)) for row in rows]

    def get_card_count(self) -> int:
        with closing(self.connect()) as connection:
            return connection.execute("SELECT count(*) FROM cards").fetchone()[0]

    def get_daily_limit(self) -> int:
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key='daily_review_limit'"
            ).fetchone()
        if row is None:
            return DEFAULT_DAILY_REVIEW_LIMIT
        try:
            return max(1, int(row[0]))
        except (TypeError, ValueError):
            return DEFAULT_DAILY_REVIEW_LIMIT

    def get_daily_session_limit(self, date_key: str) -> int:
        """Return today's possibly extended target without changing the preference."""
        base_limit = self.get_daily_limit()
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?",
                (f"{DAILY_SESSION_LIMIT_PREFIX}{date_key}",),
            ).fetchone()
        if row is None:
            return base_limit
        try:
            return max(base_limit, int(row[0]))
        except (TypeError, ValueError):
            return base_limit

    def set_daily_session_limit(self, date_key: str, value: int) -> int:
        """Persist an extended target for one date only."""
        value = max(self.get_daily_limit(), int(value))
        key = f"{DAILY_SESSION_LIMIT_PREFIX}{date_key}"
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            # Old per-day extensions have no effect and need not accumulate.
            connection.execute(
                "DELETE FROM settings WHERE key GLOB ? AND key<>?",
                (f"{DAILY_SESSION_LIMIT_PREFIX}*", key),
            )
        return value

    def get_daily_batch_start(self, date_key: str) -> int:
        """Completed-card offset from which the visible current batch begins."""
        with closing(self.connect()) as connection:
            row = connection.execute(
                "SELECT value FROM settings WHERE key=?",
                (f"{DAILY_BATCH_START_PREFIX}{date_key}",),
            ).fetchone()
            if row is None:
                # Compatibility with a session extended by the previous app
                # version, which stored the cumulative target but not the
                # visible batch offset.  The first extension always begins
                # after the normal daily target.  Do not use selected_at here:
                # after restart the remaining 28 of 30 rows are reselected
                # together and would otherwise be mistaken for a new batch.
                extended = connection.execute(
                    "SELECT value FROM settings WHERE key=?",
                    (f"{DAILY_SESSION_LIMIT_PREFIX}{date_key}",),
                ).fetchone()
                if extended is None:
                    return 0
                base = connection.execute(
                    "SELECT value FROM settings WHERE key='daily_review_limit'"
                ).fetchone()
                try:
                    base_limit = max(1, int(base[0])) if base else DEFAULT_DAILY_REVIEW_LIMIT
                    extended_limit = max(base_limit, int(extended[0]))
                except (TypeError, ValueError):
                    return 0
                if extended_limit <= base_limit:
                    return 0
                if extended_limit <= base_limit * 2:
                    return base_limit
                return max(base_limit, extended_limit - base_limit)
        try:
            return max(0, int(row[0]))
        except (TypeError, ValueError):
            return 0

    def set_daily_batch_start(self, date_key: str, value: int) -> int:
        """Persist the beginning of the currently displayed extra batch."""
        value = max(0, int(value))
        key = f"{DAILY_BATCH_START_PREFIX}{date_key}"
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            connection.execute(
                "DELETE FROM settings WHERE key GLOB ? AND key<>?",
                (f"{DAILY_BATCH_START_PREFIX}*", key),
            )
        return value

    def set_daily_limit(self, value: int, now: datetime | None = None) -> None:
        value = max(1, int(value))
        date_key = local_date(now or utc_now())
        with closing(self.connect()) as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES ('daily_review_limit', ?)",
                (str(value),),
            )
            connection.execute(
                "DELETE FROM settings WHERE key=?",
                (f"{DAILY_SESSION_LIMIT_PREFIX}{date_key}",),
            )
            connection.execute(
                "DELETE FROM settings WHERE key=?",
                (f"{DAILY_BATCH_START_PREFIX}{date_key}",),
            )
            rows = connection.execute(
                """
                SELECT d.card_id, d.completed,
                       EXISTS(
                           SELECT 1 FROM presentations p
                           WHERE p.card_id=d.card_id AND p.completed_at IS NULL
                       ) OR EXISTS(
                           SELECT 1 FROM learning_queue q
                           WHERE q.card_id=d.card_id AND q.completed_at IS NULL
                       ) OR EXISTS(
                           SELECT 1 FROM direction_ratings r
                           WHERE r.card_id=d.card_id
                       ) AS has_unfinished_pair
                FROM daily_cards d
                WHERE d.local_date=?
                ORDER BY d.position
                """,
                (date_key,),
            ).fetchall()
            protected_ids = {
                int(row[0]) for row in rows if bool(row[1]) or bool(row[2])
            }
            remaining_slots = max(0, value - len(protected_ids))
            kept_extra_ids: set[int] = set()
            for row in rows:
                card_id = int(row[0])
                if card_id in protected_ids:
                    continue
                if remaining_slots > 0:
                    kept_extra_ids.add(card_id)
                    remaining_slots -= 1
            remove_ids = [
                (date_key, int(row[0]))
                for row in rows
                if int(row[0]) not in protected_ids
                and int(row[0]) not in kept_extra_ids
            ]
            if remove_ids:
                connection.executemany(
                    "DELETE FROM daily_cards WHERE local_date=? AND card_id=?",
                    remove_ids,
                )

    def daily_rows(self, date_key: str) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM daily_cards WHERE local_date=? ORDER BY position",
                (date_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_unfinished_cards(self) -> list[dict]:
        """Pairs already started must keep a daily slot after midnight too."""
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.id AS card_id,
                       coalesce(p.direction, q.direction,
                           CASE WHEN r.chinese_to_russian_rating IS NOT NULL
                                THEN 'RUSSIAN_TO_CHINESE'
                                ELSE 'CHINESE_TO_RUSSIAN' END) AS direction
                FROM cards c
                LEFT JOIN presentations p
                    ON p.card_id=c.id AND p.completed_at IS NULL
                LEFT JOIN learning_queue q
                    ON q.card_id=c.id AND q.completed_at IS NULL
                LEFT JOIN direction_ratings r ON r.card_id=c.id
                WHERE p.id IS NOT NULL OR q.id IS NOT NULL OR r.card_id IS NOT NULL
                ORDER BY coalesce(p.shown_at, q.created_at, r.started_at), c.id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_unstarted_daily_rows(self, date_key: str) -> None:
        """Освобождает только ещё не показанные места для нового приоритета."""
        with closing(self.connect()) as connection, connection:
            connection.execute(
                """
                DELETE FROM daily_cards
                WHERE local_date=? AND completed=0
                  AND NOT EXISTS (
                      SELECT 1 FROM presentations p
                      WHERE p.card_id=daily_cards.card_id
                        AND p.completed_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM learning_queue q
                      WHERE q.card_id=daily_cards.card_id
                        AND q.completed_at IS NULL
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM direction_ratings r
                      WHERE r.card_id=daily_cards.card_id
                  )
                """,
                (date_key,),
            )

    def add_daily_rows(self, date_key: str, rows: list[tuple], selected_at: datetime) -> None:
        if not rows:
            return
        with closing(self.connect()) as connection, connection:
            connection.executemany(
                """
                INSERT OR IGNORE INTO daily_cards
                    (local_date, card_id, direction, position, selected_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (date_key, card_id, direction, position, to_storage(selected_at))
                    for card_id, direction, position in rows
                ],
            )

    def get_daily_queue(self, date_key: str, now: datetime) -> list[dict]:
        now_value = to_storage(now)
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.*, d.direction AS daily_direction, d.position,
                       d.completed AS daily_completed,
                       p.id AS active_presentation_id
                FROM daily_cards d
                JOIN cards c ON c.id=d.card_id
                LEFT JOIN presentations p
                    ON p.card_id=c.id AND p.completed_at IS NULL
                WHERE d.local_date=?
                  AND NOT EXISTS (
                      SELECT 1 FROM learning_queue q
                      WHERE q.card_id=c.id AND q.completed_at IS NULL
                  )
                  AND (
                    (
                        d.completed=0
                        AND (
                            c.priority_boost=1
                            OR c.learning_state='NEW'
                            OR c.next_review_at IS NULL
                            OR c.next_review_at <= ?
                        )
                    ) OR (
                        d.completed=1
                        AND (
                            c.priority_boost=1
                            OR (
                                c.learning_state IN ('LEARNING', 'RELEARNING')
                                AND c.next_review_at IS NOT NULL
                                AND c.next_review_at <= ?
                            )
                        )
                    )
                )
                ORDER BY
                    CASE
                        WHEN c.priority_boost=1 THEN 0
                        WHEN d.completed=1 THEN 1
                        ELSE 2
                    END,
                    CASE WHEN d.completed=1 THEN c.next_review_at ELSE d.position END
                """,
                (date_key, now_value, now_value),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_immediate_learning(self, now: datetime) -> list[dict]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """
                SELECT c.*, q.id AS learning_queue_id,
                       q.direction AS queued_direction,
                       p.id AS active_presentation_id
                FROM learning_queue q
                JOIN cards c ON c.id=q.card_id
                LEFT JOIN presentations p
                    ON p.card_id=c.id AND p.completed_at IS NULL
                WHERE q.completed_at IS NULL AND q.available_at <= ?
                ORDER BY q.id
                """,
                (to_storage(now),),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_daily_progress(self, date_key: str) -> tuple[int, int]:
        with closing(self.connect()) as connection:
            completed, planned = connection.execute(
                """SELECT coalesce(sum(completed), 0), count(*)
                   FROM daily_cards WHERE local_date=?""",
                (date_key,),
            ).fetchone()
        return int(completed), int(planned)

    def begin_presentation(
        self,
        card_id: int,
        direction: ReviewDirection,
        shown_at: datetime,
        resume_active: bool = False,
    ) -> dict:
        with closing(self.connect()) as connection, connection:
            active = connection.execute(
                """SELECT * FROM presentations
                   WHERE card_id=? AND completed_at IS NULL ORDER BY id DESC LIMIT 1""",
                (card_id,),
            ).fetchone()
            if active:
                if resume_active:
                    connection.execute(
                        "UPDATE presentations SET shown_at=? WHERE id=?",
                        (to_storage(shown_at), int(active["id"])),
                    )
                    active = connection.execute(
                        "SELECT * FROM presentations WHERE id=?",
                        (int(active["id"]),),
                    ).fetchone()
                return dict(active)
            cursor = connection.execute(
                "INSERT INTO presentations(card_id, direction, shown_at) VALUES (?, ?, ?)",
                (card_id, direction.value, to_storage(shown_at)),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM presentations WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
            )

    def save_review(
        self,
        before: CardState,
        outcome: ScheduleOutcome,
        rating: Rating,
        direction: ReviewDirection,
        reviewed_at: datetime,
        response_time_ms: int,
        presentation_id: int,
        learning_queue_id: int | None = None,
    ) -> bool:
        card = outcome.card
        date_key = local_date(reviewed_at)
        with closing(self.connect()) as connection, connection:
            presentation = connection.execute(
                """
                UPDATE presentations SET completed_at=?
                WHERE id=? AND card_id=? AND direction=? AND completed_at IS NULL
                """,
                (
                    to_storage(reviewed_at),
                    presentation_id,
                    card.id,
                    direction.value,
                ),
            )
            if presentation.rowcount != 1:
                raise ValueError("Показ карточки уже завершён или устарел")
            if learning_queue_id is not None:
                queued = connection.execute(
                    """
                    UPDATE learning_queue SET completed_at=?
                    WHERE id=? AND card_id=? AND completed_at IS NULL
                    """,
                    (to_storage(reviewed_at), learning_queue_id, card.id),
                )
                if queued.rowcount != 1:
                    raise ValueError("Повтор карточки уже завершён или устарел")
            stored_pair = connection.execute(
                "SELECT * FROM direction_ratings WHERE card_id=?",
                (card.id,),
            ).fetchone()
            chinese_rating = (
                stored_pair["chinese_to_russian_rating"] if stored_pair else None
            )
            russian_rating = (
                stored_pair["russian_to_chinese_rating"] if stored_pair else None
            )
            if direction == ReviewDirection.CHINESE_TO_RUSSIAN:
                chinese_rating = rating.value
            else:
                russian_rating = rating.value
            pair_matches = (
                chinese_rating is not None
                and russian_rating is not None
                and chinese_rating == russian_rating
            )

            if pair_matches:
                connection.execute(
                    """
                    UPDATE cards SET
                        difficulty=?, stability=?, last_review_at=?, next_review_at=?,
                        current_interval_days=?, review_count=?, lapse_count=?,
                        last_rating=?, learning_state=?, learning_step=?,
                        priority_boost=0
                    WHERE id=?
                    """,
                    (
                        card.difficulty,
                        card.stability,
                        to_storage(card.last_review_at),
                        to_storage(card.next_review_at),
                        card.current_interval_days,
                        card.review_count,
                        card.lapse_count,
                        card.last_rating,
                        card.learning_state.value,
                        card.learning_step,
                        card.id,
                    ),
                )
                connection.execute(
                    "DELETE FROM direction_ratings WHERE card_id=?", (card.id,)
                )
            else:
                connection.execute(
                    """
                    INSERT INTO direction_ratings(
                        card_id, chinese_to_russian_rating,
                        russian_to_chinese_rating, started_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(card_id) DO UPDATE SET
                        chinese_to_russian_rating=excluded.chinese_to_russian_rating,
                        russian_to_chinese_rating=excluded.russian_to_chinese_rating,
                        updated_at=excluded.updated_at
                    """,
                    (
                        card.id,
                        chinese_rating,
                        russian_rating,
                        to_storage(reviewed_at),
                        to_storage(reviewed_at),
                    ),
                )
            connection.execute(
                """
                INSERT INTO review_events(
                    card_id, reviewed_at, rating, direction, state_before, state_after,
                    previous_interval_days, new_interval_days,
                    difficulty_before, difficulty_after, stability_before, stability_after,
                    retrievability_before, response_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    card.id,
                    to_storage(reviewed_at),
                    rating.value,
                    direction.value,
                    outcome.state_before.value,
                    (
                        card.learning_state.value
                        if pair_matches
                        else before.learning_state.value
                    ),
                    before.current_interval_days,
                    (
                        card.current_interval_days
                        if pair_matches
                        else before.current_interval_days
                    ),
                    before.difficulty,
                    card.difficulty if pair_matches else before.difficulty,
                    before.stability,
                    card.stability if pair_matches else before.stability,
                    outcome.retrievability_before,
                    max(0, int(response_time_ms)),
                ),
            )
            if not pair_matches:
                opposite = (
                    ReviewDirection.RUSSIAN_TO_CHINESE
                    if direction == ReviewDirection.CHINESE_TO_RUSSIAN
                    else ReviewDirection.CHINESE_TO_RUSSIAN
                )
                connection.execute(
                    """
                    INSERT INTO learning_queue(card_id, direction, available_at, created_at)
                    SELECT ?, ?, ?, ?
                    WHERE NOT EXISTS (
                        SELECT 1 FROM learning_queue
                        WHERE card_id=? AND completed_at IS NULL
                    )
                    """,
                    (
                        card.id,
                        opposite.value,
                        to_storage(reviewed_at),
                        to_storage(reviewed_at),
                        card.id,
                    ),
                )
            if pair_matches:
                connection.execute(
                    """UPDATE daily_cards SET completed=1,
                           completed_at=coalesce(completed_at, ?)
                       WHERE local_date=? AND card_id=?""",
                    (to_storage(reviewed_at), date_key, card.id),
                )
            return pair_matches

    def review_event_count(self) -> int:
        with closing(self.connect()) as connection:
            return connection.execute("SELECT count(*) FROM review_events").fetchone()[0]
