from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from scripts.text_normalization import CJK_RE

SOUND_RE = re.compile(r"\[sound:[^\]]*]", flags=re.IGNORECASE)
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
        self.hidden_tag: str | None = None

    def handle_starttag(self, tag: str, _attributes) -> None:
        if tag in {"script", "style"}:
            self.hidden_tag = tag
        elif tag in BLOCK_TAGS and self.hidden_tag is None:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == self.hidden_tag:
            self.hidden_tag = None
        elif tag in BLOCK_TAGS and self.hidden_tag is None:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self.hidden_tag is None:
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


def _delimiter_and_data(content: str) -> tuple[str, str, set[int]]:
    delimiter = "\t"
    metadata_columns: set[int] = set()
    data_lines: list[str] = []
    reading_directives = True
    for line in content.splitlines(keepends=True):
        stripped = line.strip().lstrip("\ufeff")
        if reading_directives and not stripped:
            continue
        if reading_directives and stripped.startswith("#"):
            key, separator, value = stripped.partition(":")
            if separator and key.lower() == "#separator":
                requested = value.strip().lower()
                delimiter = SEPARATORS.get(requested, value.strip())
                if len(delimiter) != 1 or delimiter in {"\r", "\n", "\x00"}:
                    raise ValueError("В экспорте указан неверный разделитель полей")
            elif separator and key.lower() in {
                "#notetype column", "#deck column", "#tags column", "#guid column"
            }:
                column = int(value.strip())
                if column < 1:
                    raise ValueError("Номер служебного столбца должен быть положительным")
                metadata_columns.add(column - 1)
            continue
        reading_directives = False
        data_lines.append(line)
    return delimiter, "".join(data_lines), metadata_columns


def parse_anki_export(path: str | Path) -> AnkiImportResult:
    content = Path(path).read_text(encoding="utf-8-sig")
    delimiter, data, metadata_columns = _delimiter_and_data(content)
    reader = csv.reader(io.StringIO(data, newline=""), delimiter=delimiter, strict=True)

    cards: list[ImportedCard] = []
    seen_hanzi: set[str] = set()
    source_rows = invalid_rows = duplicate_rows = 0

    for fields in reader:
        if not fields or not any(field.strip() for field in fields):
            continue
        source_rows += 1
        fields = [value for index, value in enumerate(fields) if index not in metadata_columns]
        if not fields:
            invalid_rows += 1
            continue

        hanzi = clean_anki_field(fields[0])
        pinyin = clean_anki_field(fields[1]) if len(fields) > 1 else ""
        translation = clean_anki_field(fields[2]) if len(fields) > 2 else ""
        # Only the Chinese headword is used for lookup. Exported pinyin,
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
