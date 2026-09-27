from __future__ import annotations

import re
import sqlite3
import threading
import unicodedata
from contextlib import closing
from functools import lru_cache
from itertools import product
from pathlib import Path

import dictionary_remote
from app_paths import USER_DATA_DIR, placeholder_dictionary_path, select_dictionary_path
from scripts.text_normalization import (
    CJK_RE,
    PINYIN_VARIANT_RE,
    normalize_pinyin,
    normalize_translation,
    normalized_pinyin_variants,
)

FULL_DB_PATH = USER_DATA_DIR / "hanzi.db"
PLACEHOLDER_DB_PATH = placeholder_dictionary_path()
DB_PATH = select_dictionary_path()
ORIGINAL_VOCABULARY_SIZE = 1204

CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_THREAD_CONNECTION = threading.local()
_CURATED_CACHE_LOCK = threading.Lock()
_CURATED_CACHE: dict[str, tuple[tuple[int, str, str, str, str, str], ...]] = {}

# ORDER BY именно по rowid FTS-таблицы важен: FTS5 может отдавать
# совпадения прямо из индекса. Составная сортировка с bm25()
# строила временное B-tree для всех совпадений в базе на 3,45 млн строк.
_ENTRY_FTS_SQL = """SELECT e.rowid AS id, e.hanzi, e.pinyin, e.translation, 8 AS rank
                    FROM entries_fts
                    JOIN entries e ON e.rowid = entries_fts.rowid
                    WHERE entries_fts MATCH ?
                    ORDER BY entries_fts.rowid
                    LIMIT ?"""
_PINYIN_FTS_SQL = _ENTRY_FTS_SQL.replace(
    "ORDER BY entries_fts.rowid",
    "AND pinyin_matches(e.pinyin, ?) ORDER BY entries_fts.rowid",
)

_EXAMPLE_FTS_SQL = """SELECT e.rowid AS id, e.chinese, e.pinyin, e.translation
                      FROM examples_fts
                      JOIN examples e ON e.rowid = examples_fts.rowid
                      WHERE examples_fts MATCH ?
                      ORDER BY examples_fts.rowid
                      LIMIT ?"""


class _ConnectionHolder:
    """Thread-local owner, который явно закрывает SQLite при завершении worker-потока."""

    def __init__(self, database_key: str, connection: sqlite3.Connection) -> None:
        self.database_key = database_key
        self.connection: sqlite3.Connection | None = connection

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001 - __del__ must never leak errors.
            # Деструктор может вызываться на поздней стадии завершения Python.
            pass


def _configure_connection(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.create_function(
        "pinyin_matches", 2,
        lambda value, query: query in normalized_pinyin_variants(value),
        deterministic=True,
    )
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA temp_store=MEMORY")
    # mmap уменьшает число копирований страниц большого неизменяемого словаря.
    connection.execute("PRAGMA mmap_size=268435456")
    connection.execute("PRAGMA cache_size=-8192")
    return connection


def connect_dictionary() -> sqlite3.Connection:
    """Открыть новое неизменяемое подключение к словарю."""
    uri = f"{DB_PATH.resolve().as_uri()}?mode=ro&immutable=1"
    return _configure_connection(sqlite3.connect(uri, uri=True))


def _thread_connection() -> sqlite3.Connection:
    """Переиспользовать readonly-подключение внутри одного рабочего потока."""
    database_key = _database_key()
    holder = getattr(_THREAD_CONNECTION, "holder", None)
    if holder is not None and holder.database_key != database_key:
        holder.close()
        holder = None
    if holder is None or holder.connection is None:
        holder = _ConnectionHolder(database_key, connect_dictionary())
        _THREAD_CONNECTION.holder = holder
    return holder.connection


def _close_thread_connection() -> None:
    """Закрыть подключение текущего потока (нужно тестам со временной базой)."""
    holder = getattr(_THREAD_CONNECTION, "holder", None)
    if holder is not None:
        holder.close()
    _THREAD_CONNECTION.holder = None


def use_local_dictionary(path: Path = FULL_DB_PATH) -> None:
    """Point subsequent calls at a freshly downloaded local dictionary."""
    global DB_PATH
    _close_thread_connection()
    DB_PATH = path


def _database_key() -> str:
    # DB_PATH уже абсолютен. Path.resolve()/stat() на Windows могут занимать
    # десятки миллисекунд, поэтому не трогаем файловую систему на каждый запрос.
    return str(DB_PATH)


def get_stats() -> dict[str, int]:
    if dictionary_remote.enabled():
        return dictionary_remote.call("stats")
    with closing(connect_dictionary()) as connection:
        # ANALYZE хранит число строк в sqlite_stat1, не требуя COUNT(*)
        # по 3,45 млн элементов. Свежесозданная база может ещё не иметь
        # sqlite_stat1; для таких баз считаем строки явно: rowid может
        # содержать пропуски после удаления или переноса старых записей.
        try:
            row = connection.execute(
                "SELECT stat FROM sqlite_stat1 WHERE tbl='entries' LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row and row[0]:
            entries = int(row[0].split()[0])
        else:
            entries = int(
                connection.execute("SELECT count(*) FROM entries").fetchone()[0]
            )
    return {"entries": entries, "corrections": 0}


def warm_search_index() -> None:
    """Прогреть корневые страницы B-tree/FTS без сканирования словаря.

    Вызывать один раз в фоновом потоке после показа окна. Каждый
    запрос останавливается на первом совпадении.
    """
    if dictionary_remote.enabled():
        return
    connection = _thread_connection()
    connection.execute(
        "SELECT rowid FROM entries WHERE hanzi >= ? ORDER BY hanzi LIMIT 1",
        ("一",),
    ).fetchone()
    for phrase in (
        _fts_phrase("translation", "при"),
        _fts_phrase("pinyin", "shi"),
        _fts_phrase("hanzi", "中国人"),
    ):
        connection.execute(
            """SELECT rowid FROM entries_fts
               WHERE entries_fts MATCH ? ORDER BY entries_fts.rowid LIMIT 1""",
            (phrase,),
        ).fetchone()
    connection.execute(
        """SELECT rowid FROM examples_fts
           WHERE examples_fts MATCH ? ORDER BY examples_fts.rowid LIMIT 1""",
        (_fts_phrase("chinese", "我们在"),),
    ).fetchone()


def _hanzi_query(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    if not CJK_RE.search(value):
        return ""
    # В китайских заголовках встречаются латинские буквы и цифры: T恤衫, B超.
    return "".join(
        char
        for char in value
        if CJK_RE.fullmatch(char) or (char.isascii() and char.isalnum())
    )


def _fts_phrase(column: str, value: str) -> str:
    if column not in {"hanzi", "pinyin", "translation", "chinese"}:
        raise ValueError(f"Unsupported FTS column: {column}")
    return f'{column}:"{value.replace(chr(34), chr(34) * 2)}"'


def _pinyin_fts_phrase(value: str) -> str:
    """Читать и старые индексы, где тонированное ü было записано как u."""
    # В редком длинном запросе ограничиваем вариативный префикс: не больше
    # восьми FTS-веток, затем SQL проверяет полное чтение до применения LIMIT.
    positions = [index for index, char in enumerate(value) if char == "v"]
    prefix = value[: positions[3]] if len(positions) > 3 else value
    return " OR ".join(
        _fts_phrase("pinyin", "".join(chars))
        for chars in product(*(tuple("vu") if char == "v" else (char,) for char in prefix))
    )


@lru_cache(maxsize=256)
def _exact_translation_variant_pattern(value: str) -> re.Pattern[str]:
    # Находим ровно один вариант перевода, не разрезая и не нормализуя
    # многостраничную словарную статью по кускам. Е/Ё для поиска эквивалентны.
    escaped = re.escape(value).replace(r"\ ", r"\s+").replace("е", "[её]")
    return re.compile(
        rf"(?:^|[;,\n]|\d+\))\s*{escaped}\s*(?=$|[;,\n]|\d+\))",
        flags=re.IGNORECASE,
    )


@lru_cache(maxsize=256)
def _primary_translation_pattern(value: str) -> re.Pattern[str]:
    escaped = re.escape(value).replace(r"\ ", r"\s+").replace("е", "[её]")
    return re.compile(
        rf"^\s*(?:1\)\s*)?{escaped}\s*(?=$|[;,\n])",
        flags=re.IGNORECASE,
    )


def _row_rank(item: dict, *, hanzi_query: str, pinyin_query: str, translation_query: str) -> int:
    if hanzi_query:
        if item["hanzi"] == hanzi_query:
            return 0
        if item["hanzi"].startswith(hanzi_query):
            return 3
        return 4

    if pinyin_query:
        variants = [
            normalize_pinyin(part)
            for part in PINYIN_VARIANT_RE.split(item["pinyin"])
            if part.strip()
        ]
        if pinyin_query in variants:
            return 1
        if any(variant.startswith(pinyin_query) for variant in variants):
            return 2
        return 8

    if translation_query:
        normalized = normalize_translation(item["translation"])
        if normalized == translation_query or _primary_translation_pattern(
            translation_query
        ).search(item["translation"]):
            return 1
        if normalized.startswith(translation_query):
            if (
                len(translation_query) <= 2
                and len(normalized) > len(translation_query)
                and normalized[len(translation_query)].isalpha()
            ):
                # «мы» должно быть выше «мыть», «он» — выше «онлайн».
                return 6
            return 2
        if _exact_translation_variant_pattern(translation_query).search(item["translation"]):
            return 4
    return 8


def _curated_search_rows(
    connection: sqlite3.Connection,
) -> tuple[tuple[int, str, str, str, str, str], ...]:
    """Кэш нормализованных полей базового словаря для коротких запросов."""
    database_key = _database_key()
    cached = _CURATED_CACHE.get(database_key)
    if cached is not None:
        return cached
    with _CURATED_CACHE_LOCK:
        cached = _CURATED_CACHE.get(database_key)
        if cached is None:
            rows = connection.execute(
                """SELECT rowid AS id, hanzi, pinyin, translation
                   FROM entries WHERE rowid <= ? ORDER BY rowid""",
                (ORIGINAL_VOCABULARY_SIZE,),
            ).fetchall()
            cached = tuple(
                (
                    int(row["id"]),
                    row["hanzi"],
                    row["pinyin"],
                    row["translation"],
                    normalized_pinyin_variants(row["pinyin"]),
                    normalize_translation(row["translation"]),
                )
                for row in rows
            )
            _CURATED_CACHE[database_key] = cached
    return cached


def search_entries(query: str, limit: int = 50) -> list[dict]:
    if dictionary_remote.enabled():
        return dictionary_remote.call("search", query=query, limit=min(max(limit, 1), 100)) if query.strip() else []
    query = unicodedata.normalize("NFKC", query).strip()
    if not query:
        return []
    limit = min(max(limit, 1), 100)

    hanzi_query = _hanzi_query(query) if CJK_RE.search(query) else ""
    pinyin_query = "" if hanzi_query or CYRILLIC_RE.search(query) else normalize_pinyin(query)
    translation_query = (
        normalize_translation(query) if not hanzi_query and CYRILLIC_RE.search(query) else ""
    )
    if not (hanzi_query or pinyin_query or translation_query):
        return []

    connection = _thread_connection()
    rows_by_id: dict[int, dict] = {}
    fetch_limit = min(max(limit * 4, 50), 200)

    def collect(row: sqlite3.Row | dict, default_rank: int = 8) -> None:
        item = dict(row)
        item["rank"] = min(
            item.get("rank", default_rank),
            _row_rank(
                item,
                hanzi_query=hanzi_query,
                pinyin_query=pinyin_query,
                translation_query=translation_query,
            ),
        )
        previous = rows_by_id.get(item["id"])
        if previous is None or item["rank"] < previous["rank"]:
            rows_by_id[item["id"]] = item

    if hanzi_query:
        exact_rows = connection.execute(
            """SELECT rowid AS id, hanzi, pinyin, translation, 0 AS rank
               FROM entries WHERE hanzi = ? LIMIT 1""",
            (hanzi_query,),
        ).fetchall()
        for row in exact_rows:
            collect(row, 0)

        prefix_rows = connection.execute(
            """SELECT rowid AS id, hanzi, pinyin, translation, 3 AS rank
               FROM entries
               WHERE hanzi >= ? AND hanzi < ?
               ORDER BY hanzi LIMIT ?""",
            (hanzi_query, hanzi_query + "\U0010ffff", fetch_limit),
        ).fetchall()
        for row in prefix_rows:
            collect(row, 3)

    if len(hanzi_query or pinyin_query or translation_query) >= 3:
        if hanzi_query:
            field, value = "hanzi", hanzi_query
        elif translation_query:
            field, value = "translation", translation_query
        else:
            field, value = "pinyin", pinyin_query
        if pinyin_query:
            fts_rows = connection.execute(
                _PINYIN_FTS_SQL,
                (_pinyin_fts_phrase(value), value, fetch_limit),
            ).fetchall()
        else:
            fts_rows = connection.execute(
                _ENTRY_FTS_SQL,
                (_fts_phrase(field, value), fetch_limit),
            ).fetchall()
        for row in fts_rows:
            collect(row)
    else:
        # Trigram FTS не индексирует 1–2 символа. Быстро проверяем
        # небольшой базовый словарь, не сканируя все 3,45 млн строк.
        needle = hanzi_query or pinyin_query or translation_query
        for row_id, hanzi, pinyin, translation, normalized_pinyin, normalized_translation in (
            _curated_search_rows(connection)
        ):
            if hanzi_query:
                haystack = hanzi
            elif pinyin_query:
                haystack = normalized_pinyin
            else:
                haystack = normalized_translation
            if needle in haystack:
                collect(
                    {
                        "id": row_id,
                        "hanzi": hanzi,
                        "pinyin": pinyin,
                        "translation": translation,
                        "rank": 8,
                    }
                )

    rows = list(rows_by_id.values())
    rows.sort(
        key=lambda row: (
            row["rank"],
            -int(row["id"] <= ORIGINAL_VOCABULARY_SIZE),
            len(row["hanzi"]),
            row["id"],
        )
    )
    return rows[:limit]


def get_entries_by_hanzi(words) -> dict[str, dict]:
    """Точно найти набор заголовков в readonly-словаре."""
    requested = tuple(dict.fromkeys(word.strip() for word in words if word.strip()))
    if not requested:
        return {}
    if dictionary_remote.enabled():
        found = {}
        for offset in range(0, len(requested), 100):
            found.update(dictionary_remote.call("entries", words=requested[offset:offset + 100]))
        return found
    connection = _thread_connection()
    found: dict[str, dict] = {}
    for offset in range(0, len(requested), 900):
        chunk = requested[offset : offset + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""SELECT rowid AS id, hanzi, pinyin, translation
                FROM entries WHERE hanzi IN ({placeholders})""",
            chunk,
        ).fetchall()
        found.update((row["hanzi"], dict(row)) for row in rows)
    return found


def get_examples(hanzi: str, limit: int = 6, pinyin: str = "") -> list[dict]:
    if dictionary_remote.enabled():
        return dictionary_remote.call("examples", hanzi=hanzi, limit=min(max(limit, 1), 20), pinyin=pinyin)
    del pinyin  # Чтение не меняет поиск: примеры связаны с написанием слова.
    hanzi = _hanzi_query(hanzi)
    if not hanzi:
        return []
    limit = min(max(limit, 1), 20)
    connection = _thread_connection()
    if len(hanzi) >= 3:
        rows = connection.execute(
            _EXAMPLE_FTS_SQL,
            (_fts_phrase("chinese", hanzi), limit),
        ).fetchall()
    else:
        # Для 1–2 иероглифов trigram-индекс неприменим. Индексный
        # диапазон мгновенно находит примеры, начинающиеся с этого слова,
        # вместо полного просмотра миллиона предложений.
        rows = connection.execute(
            """SELECT rowid AS id, chinese, pinyin, translation
               FROM examples
               WHERE chinese >= ? AND chinese < ?
               ORDER BY chinese LIMIT ?""",
            (hanzi, hanzi + "\U0010ffff", limit),
        ).fetchall()
    return [dict(row) for row in rows]
