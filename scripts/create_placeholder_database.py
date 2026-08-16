from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "hanzi-placeholder.db"


def create_placeholder_database(output: Path = DEFAULT_OUTPUT) -> Path:
    """Создать пустую совместимую базу для исходников и демонстрационной сборки."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".building")
    if temporary.exists():
        temporary.unlink()

    with closing(sqlite3.connect(temporary)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE entries (
                hanzi TEXT NOT NULL UNIQUE,
                pinyin TEXT NOT NULL,
                translation TEXT NOT NULL
            );
            CREATE TABLE examples (
                chinese TEXT NOT NULL,
                pinyin TEXT NOT NULL,
                translation TEXT NOT NULL DEFAULT '',
                UNIQUE(chinese, translation)
            );
            CREATE VIRTUAL TABLE entries_fts USING fts5(
                hanzi, pinyin, translation,
                content='', tokenize='trigram'
            );
            CREATE VIRTUAL TABLE examples_fts USING fts5(
                chinese, pinyin, translation,
                content='', tokenize='trigram'
            );
            ANALYZE;
            """
        )

    temporary.replace(output)
    return output


if __name__ == "__main__":
    print(create_placeholder_database())
