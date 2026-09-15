from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from pypinyin import Style, lazy_pinyin

try:
    from scripts.dictionary_schema import build_indexes
    from scripts.dictionary_schema import (
        create_dictionary_schema as create_clean_schema,
    )
    from scripts.text_normalization import (
        CJK_RE,
        normalize_translation,
        normalized_pinyin_variants,
    )
except ModuleNotFoundError:
    from dictionary_schema import build_indexes
    from dictionary_schema import create_dictionary_schema as create_clean_schema
    from text_normalization import (
        CJK_RE,
        normalize_translation,
        normalized_pinyin_variants,
    )


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "data" / "hanzi.db"
EXAMPLES_SOURCE = Path("D:/\u0420\u0430\u0431\u043e\u0447\u0438\u0439 \u0441\u0442\u043e\u043b/examples_260809")
CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")


@lru_cache(maxsize=32768)
def character_pinyin(character: str) -> str:
    return lazy_pinyin(character, style=Style.TONE, neutral_tone_with_five=False)[0]


def example_pinyin(text: str) -> str:
    return " ".join(character_pinyin(character) for character in CJK_RE.findall(text))


def unique_lines(lines: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in lines:
        normalized = normalize_translation(line)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(line)
    return result


def split_example_line(line: str) -> tuple[str, str]:
    match = CYRILLIC_RE.search(line)
    if not match:
        return line.strip(), ""
    chinese = line[: match.start()].strip(" \t—–-:;")
    translation = line[match.start() :].strip()
    return chinese or line.strip(), translation


def split_article(text: str) -> tuple[str, list[tuple[str, str]]]:
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]
    if len(lines) <= 1:
        return (lines[0] if lines else "", [])
    translation: list[str] = [lines[0]]
    examples: list[tuple[str, str]] = []
    for line in lines[1:]:
        # Русские пояснения могут упоминать иероглифы (например, «см. 学»).
        # Переносим только строки, действительно начинающиеся примером.
        first = line.lstrip(" \t\"'«“([{（【")
        if CJK_RE.match(first):
            examples.append(split_example_line(line))
        else:
            translation.append(line)
    return "\n".join(unique_lines(translation)), examples


def iter_external_examples(path: Path):
    chinese = ""
    translations: list[str] = []

    def current():
        if not chinese:
            return None
        return chinese, "\n".join(unique_lines(translations))

    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            raw = line.rstrip("\r\n")
            if raw.startswith("#") and not chinese:
                continue
            if not raw.strip():
                item = current()
                if item:
                    yield item
                chinese = ""
                translations = []
                continue
            if raw[:1].isspace():
                translations.append(raw.strip())
            else:
                item = current()
                if item:
                    yield item
                chinese = raw.strip()
                translations = []
    item = current()
    if item:
        yield item


def flush_entries(connection: sqlite3.Connection, entries: list[tuple], fts: list[tuple]) -> None:
    connection.executemany(
        "INSERT INTO entries(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)", entries
    )
    connection.executemany(
        "INSERT INTO entries_fts(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)", fts
    )
    entries.clear()
    fts.clear()


def flush_examples(connection: sqlite3.Connection, examples: list[tuple]) -> None:
    connection.executemany(
        "INSERT OR IGNORE INTO examples(chinese, pinyin, translation) VALUES (?, ?, ?)", examples
    )
    examples.clear()


def compact_database(database: Path = DATABASE, examples_source: Path = EXAMPLES_SOURCE) -> dict[str, int | float]:
    if not database.is_file():
        raise FileNotFoundError(database)
    temporary = database.with_name(database.stem + ".compacting" + database.suffix)
    if temporary.exists():
        temporary.unlink()
    started = time.monotonic()
    articles = preserved_examples = embedded_examples = external_examples = 0

    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as source, closing(sqlite3.connect(temporary)) as target:
        target.execute("PRAGMA temp_store=MEMORY")
        target.execute("PRAGMA cache_size=-262144")
        create_clean_schema(target)
        entry_batch: list[tuple] = []
        entry_fts_batch: list[tuple] = []
        example_batch: list[tuple] = []
        target.execute("BEGIN")

        # Keep examples already separated by an earlier compaction.  Without
        # this copy, rerunning the script could replace the dictionary with a
        # database containing no examples when EXAMPLES_SOURCE is unavailable.
        if source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='examples'"
        ).fetchone():
            examples_cursor = source.execute(
                "SELECT chinese, pinyin, translation FROM examples ORDER BY rowid"
            )
            for chinese, pinyin, translation in examples_cursor:
                example_batch.append((chinese, pinyin, translation))
                preserved_examples += 1
                if len(example_batch) >= 5000:
                    flush_examples(target, example_batch)
            examples_cursor.close()

        source_cursor = source.execute("SELECT rowid, hanzi, pinyin, translation FROM entries ORDER BY rowid")
        for rowid, hanzi, pinyin, raw_translation in source_cursor:
            translation, extracted = split_article(raw_translation)
            articles += 1
            entry_batch.append((rowid, hanzi, pinyin, translation))
            entry_fts_batch.append((
                rowid,
                hanzi,
                normalized_pinyin_variants(pinyin),
                normalize_translation(translation),
            ))
            for chinese, example_translation in extracted:
                if chinese and CJK_RE.search(chinese):
                    example_batch.append((chinese, example_pinyin(chinese), example_translation))
                    embedded_examples += 1
            if len(entry_batch) >= 5000:
                flush_entries(target, entry_batch, entry_fts_batch)
            if len(example_batch) >= 5000:
                flush_examples(target, example_batch)
            if articles % 250000 == 0:
                print(f"Перенесено {articles:,} словарных статей…".replace(",", " "), flush=True)

        if entry_batch:
            flush_entries(target, entry_batch, entry_fts_batch)
        if example_batch:
            flush_examples(target, example_batch)
        source_cursor.close()
        target.commit()

        if examples_source.exists():
            target.execute("BEGIN")
            for chinese, translation in iter_external_examples(examples_source):
                if chinese and CJK_RE.search(chinese):
                    example_batch.append((chinese, example_pinyin(chinese), translation))
                    external_examples += 1
                if len(example_batch) >= 5000:
                    flush_examples(target, example_batch)
                if external_examples and external_examples % 100000 == 0:
                    print(f"Прочитано {external_examples:,} внешних примеров…".replace(",", " "), flush=True)
            if example_batch:
                flush_examples(target, example_batch)
            target.commit()

        unique_examples = target.execute("SELECT count(*) FROM examples").fetchone()[0]
        print(f"Создаю индекс для {unique_examples:,} уникальных примеров…".replace(",", " "), flush=True)
        target.execute("BEGIN")
        build_indexes(target, tables=("examples",))
        target.commit()
        target.execute("ANALYZE")
        target.commit()

    temporary.replace(database)
    return {
        "articles": articles,
        "preserved_examples": preserved_examples,
        "embedded_examples": embedded_examples,
        "external_examples": external_examples,
        "unique_examples": unique_examples,
        "duplicates_removed": preserved_examples + embedded_examples + external_examples - unique_examples,
        "seconds": round(time.monotonic() - started, 2),
    }


if __name__ == "__main__":
    stats = compact_database()
    print(
        "Готово: {articles:,} статей, {unique_examples:,} примеров, удалено дублей: "
        "{duplicates_removed:,}; {seconds} с".format(**stats).replace(",", " "),
        flush=True,
    )
