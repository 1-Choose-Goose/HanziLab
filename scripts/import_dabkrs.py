from __future__ import annotations

import argparse
import ast
import sqlite3
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from pypinyin import Style, lazy_pinyin

try:
    from scripts.text_normalization import normalize_pinyin, normalize_translation
except ModuleNotFoundError:
    from text_normalization import normalize_pinyin, normalize_translation


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = ROOT / "data" / "hanzi.db"


def iter_dabkrs(source: Path):
    in_dictionary_block = False
    with source.open("r", encoding="utf-8", errors="strict") as handle:
        for line_number, line in enumerate(handle, 1):
            if 'id="dabkrs_ids_' in line:
                in_dictionary_block = True
                continue
            if in_dictionary_block and "</script>" in line:
                in_dictionary_block = False
                continue
            if not in_dictionary_block:
                continue
            stripped = line.strip()
            if not (stripped.startswith("['") and stripped.endswith("],")):
                continue
            row = ast.literal_eval(stripped[:-1])
            if isinstance(row, list) and len(row) == 3 and all(isinstance(value, str) for value in row):
                yield line_number, row[0].strip(), row[1].strip(), row[2].strip()


@lru_cache(maxsize=32768)
def character_pinyin(character: str) -> str:
    if not ("\u3400" <= character <= "\u9fff"):
        return character
    return lazy_pinyin(character, style=Style.TONE, neutral_tone_with_five=False)[0]


def generated_pinyin(hanzi: str) -> str:
    if not any("\u3400" <= character <= "\u9fff" for character in hanzi):
        return ""
    return "".join(character_pinyin(character) for character in hanzi)


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
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
            hanzi, pinyin, translation, content='', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE examples_fts USING fts5(
            chinese, pinyin, translation, content='', tokenize='trigram'
        );
        """
    )


def build_indexes(connection: sqlite3.Connection) -> None:
    for table, columns in (
        ("entries", ("hanzi", "pinyin", "translation")),
        ("examples", ("chinese", "pinyin", "translation")),
    ):
        fts = f"{table}_fts"
        cursor = connection.execute(
            f"SELECT rowid, {', '.join(columns)} FROM {table} ORDER BY rowid"
        )
        batch: list[tuple] = []
        for row in cursor:
            rowid, first, pinyin, translation = row
            batch.append((rowid, first, normalize_pinyin(pinyin), normalize_translation(translation)))
            if len(batch) >= 5000:
                connection.executemany(
                    f"INSERT INTO {fts}(rowid, {', '.join(columns)}) VALUES (?, ?, ?, ?)", batch
                )
                batch.clear()
        if batch:
            connection.executemany(
                f"INSERT INTO {fts}(rowid, {', '.join(columns)}) VALUES (?, ?, ?, ?)", batch
            )


def import_dabkrs(
    source: Path, output: Path = DEFAULT_DATABASE, limit: int | None = None
) -> dict[str, int | float]:
    if not source.exists():
        raise FileNotFoundError(source)

    # An existing --output is the database the caller asked to extend.  The
    # default database is only a seed when a new output file is requested.
    # Previously every custom output was silently rebuilt from DEFAULT_DATABASE.
    base_database = output if output.exists() else DEFAULT_DATABASE
    if not base_database.exists():
        raise FileNotFoundError(base_database)

    temporary = output.with_name(output.stem + ".importing" + output.suffix)
    if temporary.exists():
        temporary.unlink()
    started = time.monotonic()

    with closing(sqlite3.connect(base_database)) as current, closing(sqlite3.connect(temporary)) as target:
        current.execute("PRAGMA query_only=ON")
        create_schema(target)
        initial_entries = current.execute("SELECT count(*) FROM entries").fetchone()[0]
        target.executemany(
            "INSERT INTO entries(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
            current.execute("SELECT rowid, hanzi, pinyin, translation FROM entries ORDER BY rowid"),
        )
        if current.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='examples'"
        ).fetchone():
            target.executemany(
                "INSERT INTO examples(rowid, chinese, pinyin, translation) VALUES (?, ?, ?, ?)",
                current.execute("SELECT rowid, chinese, pinyin, translation FROM examples ORDER BY rowid"),
            )
        target.commit()

        parsed = generated = 0
        batch: list[tuple[str, str, str]] = []
        upsert = """
            INSERT INTO entries(hanzi, pinyin, translation) VALUES (?, ?, ?)
            ON CONFLICT(hanzi) DO UPDATE SET
                pinyin = CASE
                    WHEN excluded.pinyin = '' OR instr(entries.pinyin, excluded.pinyin) > 0 THEN entries.pinyin
                    WHEN entries.pinyin = '' THEN excluded.pinyin
                    ELSE entries.pinyin || '; ' || excluded.pinyin END,
                translation = CASE
                    WHEN excluded.translation = '' OR instr(entries.translation, excluded.translation) > 0 THEN entries.translation
                    WHEN entries.translation = '' THEN excluded.translation
                    ELSE entries.translation || char(10) || char(10) || excluded.translation END
        """
        target.execute("BEGIN")
        for _line_number, hanzi, pinyin, translation in iter_dabkrs(source):
            if not hanzi:
                continue
            parsed += 1
            if pinyin == "_":
                pinyin = generated_pinyin(hanzi)
                generated += bool(pinyin)
            batch.append((hanzi, pinyin, translation))
            if len(batch) >= 5000:
                target.executemany(upsert, batch)
                batch.clear()
            if parsed % 100000 == 0:
                print(f"Обработано {parsed:,} статей…".replace(",", " "), flush=True)
            if limit is not None and parsed >= limit:
                break
        if batch:
            target.executemany(upsert, batch)
        target.commit()

        final_entries = target.execute("SELECT count(*) FROM entries").fetchone()[0]
        print("Создаю поисковые индексы…", flush=True)
        build_indexes(target)
        target.execute("ANALYZE")
        target.commit()

    temporary.replace(output)
    return {
        "parsed": parsed,
        "inserted": final_entries - initial_entries,
        "duplicates_merged": parsed - (final_entries - initial_entries),
        "generated_pinyin": generated,
        "seconds": round(time.monotonic() - started, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт словаря DABKRS из автономного HTML")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--limit", type=int)
    arguments = parser.parse_args()
    stats = import_dabkrs(arguments.source.resolve(), arguments.output.resolve(), arguments.limit)
    print(
        "Готово: {parsed:,} обработано, {inserted:,} добавлено, {duplicates_merged:,} дублей объединено, "
        "{generated_pinyin:,} чтений создано; {seconds} с".format(**stats).replace(",", " "),
        flush=True,
    )


if __name__ == "__main__":
    main()
