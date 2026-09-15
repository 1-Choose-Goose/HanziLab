from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

try:
    from scripts.dictionary_schema import create_dictionary_schema
except ModuleNotFoundError:
    from dictionary_schema import create_dictionary_schema

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "data" / "hanzi-placeholder.db"


def create_placeholder_database(output: Path = DEFAULT_OUTPUT) -> Path:
    """Создать пустую совместимую базу для исходников и демонстрационной сборки."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".building")
    if temporary.exists():
        temporary.unlink()

    with closing(sqlite3.connect(temporary)) as connection, connection:
        create_dictionary_schema(connection)
        connection.execute("ANALYZE")

    temporary.replace(output)
    return output


if __name__ == "__main__":
    print(create_placeholder_database())
