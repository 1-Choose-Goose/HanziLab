from __future__ import annotations

import sqlite3
import sys
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

try:
    from scripts.text_normalization import normalize_pinyin, normalize_translation
except ModuleNotFoundError:
    from text_normalization import normalize_pinyin, normalize_translation


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "hanzi.db"


# Ручная редактура только там, где исходная запись содержит ошибку или заметно
# вводит ученика в заблуждение. Остальные значения сохраняются как авторские.
CORRECTIONS: dict[str, dict[str, str]] = {
    "什么": {"translation": "что; какой"},
    "是": {"translation": "быть; являться; представлять собой; да, верно"},
    "英国": {"translation": "Великобритания; Британия; Соединённое Королевство"},
    "呢": {"translation": "модальная частица: а…?; же; ведь"},
    "老师": {"pinyin": "lǎoshī"},
    "问": {"translation": "спрашивать; задавать вопрос; осведомляться"},
    "哪": {"translation": "какой; который; где (в сочетаниях)"},
    "几": {"translation": "сколько; несколько (обычно о небольшом количестве)"},
    "朋友": {"translation": "друг; приятель; знакомый"},
    "都": {"translation": "все; всё; оба; уже (в усилительных конструкциях)"},
    "本": {"translation": "счётное слово для книг и изданий; корень; основа; этот; данный"},
    "贵姓": {"translation": "как ваша фамилия? (вежливая форма)"},
    "上班": {"translation": "работать; идти или приходить на работу"},
    "下班": {"translation": "заканчивать работу; уходить с работы"},
    "职员": {"translation": "сотрудник; служащий"},
    "汽车": {"translation": "автомобиль; машина"},
    "甜": {"translation": "сладкий; сладость"},
    "正在": {"translation": "как раз; именно сейчас; находиться в процессе действия"},
    "舒服": {"translation": "удобный; комфортный; приятный; хорошо себя чувствовать"},
    "觉得": {"translation": "чувствовать; считать; полагать; думать; казаться"},
    "努力": {"translation": "стараться; прилагать усилия; усердный; упорно"},
    "营业": {"translation": "работать; быть открытым; вести коммерческую деятельность"},
    "层": {"translation": "слой; ярус; этаж; счётное слово для слоёв и этажей"},
    "放": {"translation": "класть; ставить; отпускать; выпускать; освобождать"},
    "行": {"translation": "можно; годится; хорошо; идти; действовать; способный"},
    "T恤衫": {"pinyin": "T-xùshān", "translation": "футболка"},
    "不行": {"pinyin": "bùxíng", "translation": "не годится; нельзя; невозможно; плохо себя чувствовать"},
    "东南边": {"pinyin": "dōngnánbiān", "translation": "юго-восточная сторона; на юго-востоке"},
    "天太冷": {"translation": "слишком холодно; погода слишком холодная"},
    "天太热": {"translation": "слишком жарко; погода слишком жаркая"},
    "学生城": {"pinyin": "xuéshēngchéng", "translation": "студенческий городок"},
    "学生登记表": {"pinyin": "xuésheng dēngjìbiǎo", "translation": "регистрационная карточка студента"},
    "阳光照耀着大地": {"pinyin": "yángguāng zhàoyào zhe dàdì", "translation": "солнечный свет озаряет землю"},
    "好啊": {"pinyin": "hǎo a", "translation": "хорошо; ладно; конечно"},
    "老得劲儿了": {"pinyin": "lǎo déjìnr le", "translation": "очень здорово; очень приятно или удобно (разг., северо-восточный диалект)"},
    "怎么办": {"pinyin": "zěnme bàn", "translation": "что делать?; как поступить?"},
    "多保重": {"translation": "береги себя; будь здоров"},
    "看完": {"translation": "досмотреть; дочитать; закончить просмотр или чтение"},
    "怎么卖": {"translation": "как продаётся?; сколько стоит?"},
    "奶渣": {"translation": "творожный сыр; сушёный молочный творог (тибетский продукт)"},
}

HANZI_REPLACEMENTS = {
    "学生成": "学生城",
    "什么办": "怎么办",
    "老的劲儿了": "老得劲儿了",
}


@dataclass
class Entry:
    hanzi: str
    pinyins: list[str] = field(default_factory=list)
    translations: list[str] = field(default_factory=list)
    source_rows: int = 0


def append_unique(items: list[str], value: str) -> None:
    value = value.strip()
    if not value:
        return
    normalized = normalize_translation(value)
    for index, item in enumerate(items):
        current = normalize_translation(item)
        if normalized == current or normalized in current:
            return
        if current in normalized:
            items[index] = value
            return
    items.append(value)


def load_rows(source: Path) -> tuple[list[Entry], list[tuple]]:
    grouped: OrderedDict[str, Entry] = OrderedDict()
    corrections_log: list[tuple] = []
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        for source_line, raw in enumerate(handle, 1):
            if raw.startswith("#") or not raw.strip():
                continue
            columns = raw.rstrip("\r\n").split("\t")
            if len(columns) < 3:
                corrections_log.append((source_line, "row", raw.strip(), "", "Пропущена неполная строка"))
                continue
            original_hanzi, original_pinyin, original_translation = (item.strip() for item in columns[:3])
            hanzi = HANZI_REPLACEMENTS.get(original_hanzi, original_hanzi)
            if hanzi != original_hanzi:
                corrections_log.append((source_line, "hanzi", original_hanzi, hanzi, "Исправлена опечатка"))

            manual = CORRECTIONS.get(hanzi, {})
            pinyin = manual.get("pinyin", original_pinyin)
            translation = manual.get("translation", original_translation)
            if pinyin != original_pinyin:
                corrections_log.append((source_line, "pinyin", original_pinyin, pinyin, "Исправлено чтение"))
            if translation != original_translation:
                corrections_log.append((source_line, "translation", original_translation, translation, "Уточнён перевод"))

            entry = grouped.setdefault(hanzi, Entry(hanzi=hanzi))
            for variant in pinyin.replace(",", ";").split(";"):
                if variant.strip() and normalize_pinyin(variant) not in {normalize_pinyin(item) for item in entry.pinyins}:
                    entry.pinyins.append(variant.strip())
            append_unique(entry.translations, translation)
            entry.source_rows += 1

    return list(grouped.values()), corrections_log


def build_database(source: Path, database: Path = DEFAULT_DB) -> dict[str, int]:
    entries, corrections = load_rows(source)
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(database.stem + ".building" + database.suffix)
    if temporary.exists():
        temporary.unlink()
    with closing(sqlite3.connect(temporary)) as connection, connection:
        connection.executescript(
            """
            PRAGMA journal_mode=DELETE;
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
            """
        )
        for entry in entries:
            pinyin = "; ".join(entry.pinyins)
            translation = "; ".join(entry.translations)
            cursor = connection.execute(
                "INSERT INTO entries(hanzi, pinyin, translation) VALUES (?, ?, ?)",
                (entry.hanzi, pinyin, translation),
            )
            connection.execute(
                "INSERT INTO entries_fts(rowid, hanzi, pinyin, translation) VALUES (?, ?, ?, ?)",
                (
                    cursor.lastrowid,
                    entry.hanzi,
                    " ".join(normalize_pinyin(item) for item in entry.pinyins),
                    normalize_translation(translation),
                ),
            )
        connection.execute("ANALYZE")
    temporary.replace(database)
    return {"source_rows": sum(entry.source_rows for entry in entries), "entries": len(entries), "corrections": len(corrections)}


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("Укажите путь к TSV: python scripts/import_vocabulary.py <файл>")
    source = Path(sys.argv[1]).expanduser().resolve()
    if not source.exists():
        raise SystemExit(f"Файл не найден: {source}")
    stats = build_database(source)
    print(f"Готово: {stats['source_rows']} строк -> {stats['entries']} записей; исправлений: {stats['corrections']}")
    print(f"База: {DEFAULT_DB}")


if __name__ == "__main__":
    main()
