from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

SOUND_RE = re.compile(r"\[sound:[^\]]*]", flags=re.IGNORECASE)
CJK_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\U00020000-\U0002fa1f]"
)
SEPARATORS = {
    "tab": "\t",
    "comma": ",",
    "semicolon": ";",
    "pipe": "|",
    "space": " ",
}
BLOCK_TAGS = {"br", "div", "p", "li", "tr", "section"}


class _PlainTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, _attributes) -> None:
        if tag.lower() in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class ImportedCard:
    hanzi: str
    pinyin: str
    translation: str


@dataclass(frozen=True)
class AnkiImportResult:
    cards: tuple[ImportedCard, ...]
    source_rows: int
    invalid_rows: int
    duplicate_rows: int


def clean_anki_field(value: str) -> str:
    extractor = _PlainTextExtractor()
    extractor.feed(SOUND_RE.sub(" ", value))
    extractor.close()
    return " ".join("".join(extractor.parts).replace("\xa0", " ").split())


def _delimiter_and_data(content: str) -> tuple[str, str]:
    delimiter = "\t"
    data_lines: list[str] = []
    reading_directives = True
    for line in content.splitlines(keepends=True):
        stripped = line.strip().lstrip("\ufeff")
        if reading_directives and stripped.startswith("#"):
            key, separator, value = stripped.partition(":")
            if separator and key.lower() == "#separator":
                requested = value.strip().lower()
                delimiter = SEPARATORS.get(requested, value[:1])
                if not delimiter:
                    raise ValueError("В экспорте Anki не указан разделитель полей")
            continue
        reading_directives = False
        data_lines.append(line)
    return delimiter, "".join(data_lines)


def parse_anki_export(path: str | Path) -> AnkiImportResult:
    content = Path(path).read_text(encoding="utf-8-sig")
    delimiter, data = _delimiter_and_data(content)
    reader = csv.reader(io.StringIO(data, newline=""), delimiter=delimiter)

    cards: list[ImportedCard] = []
    seen_hanzi: set[str] = set()
    source_rows = invalid_rows = duplicate_rows = 0

    for fields in reader:
        if not fields or not any(field.strip() for field in fields):
            continue
        source_rows += 1
        if len(fields) < 3:
            invalid_rows += 1
            continue

        hanzi = clean_anki_field(fields[0])
        pinyin = clean_anki_field(fields[1])
        translation = clean_anki_field(fields[2])
        # Only the Chinese headword is used for lookup. Anki's pinyin,
        # translation, tags and media never overwrite HanziLab dictionary data.
        if not hanzi or not CJK_RE.search(hanzi):
            invalid_rows += 1
            continue
        if hanzi in seen_hanzi:
            duplicate_rows += 1
            continue
        seen_hanzi.add(hanzi)
        cards.append(ImportedCard(hanzi, pinyin, translation))

    return AnkiImportResult(
        tuple(cards),
        source_rows,
        invalid_rows,
        duplicate_rows,
    )
