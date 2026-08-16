from __future__ import annotations

import html
import re
import unicodedata
from itertools import groupby

_HORIZONTAL_WHITESPACE_RE = re.compile(r"[^\S\n]+")
_INVISIBLE_FORMATTING_RE = re.compile(r"[\u00ad\u200b\u2060\ufeff]")
_BRACKETED_ANNOTATION_RE = re.compile(r"\[([^\]\n]{1,120})]")
_SECTION_READING_RE = re.compile(
    r"^(?P<label>\s*•?\s*(?:[IVXLCDM]+|\d+\)))"
    r"[\s,.:;–—-]+(?P<reading>.+)$"
)


def normalize_display_text(
    text: object,
    *,
    preserve_line_breaks: bool = True,
) -> str:
    """Remove layout noise while preserving meaningful, non-empty lines."""
    source = "" if text is None else str(text)
    source = _INVISIBLE_FORMATTING_RE.sub("", source)
    # splitlines() also handles CR-only data and Unicode line separators.
    lines = [
        _HORIZONTAL_WHITESPACE_RE.sub(" ", line).strip()
        for line in source.splitlines()
    ]
    non_empty_lines = [line for line in lines if line]
    separator = "\n" if preserve_line_breaks else " "
    return separator.join(non_empty_lines)


def _looks_like_pinyin_annotation(text: str) -> bool:
    """Recognize Latin pinyin notes without mistaking Russian labels for them."""
    has_lowercase_latin = False
    for character in text.strip():
        if character.isalpha():
            if "LATIN" not in unicodedata.name(character, ""):
                return False
            has_lowercase_latin = has_lowercase_latin or character.islower()
        elif (
            character.isdigit()
            or character.isspace()
            or character in "'\u2019·,;:/|.-–—()"
        ):
            continue
        else:
            return False
    # Uppercase A/B and Roman section numbers are grammatical labels, not
    # readings, and therefore stay visible.
    return has_lowercase_latin


def card_translation_without_pinyin(text: object) -> str:
    """Hide dictionary pronunciation annotations only on a card's Russian side."""
    cleaned_lines: list[str] = []
    for raw_line in normalize_display_text(text).splitlines():
        line = _BRACKETED_ANNOTATION_RE.sub(
            lambda match: (
                "" if _looks_like_pinyin_annotation(match.group(1)) else match.group(0)
            ),
            raw_line,
        )
        line = normalize_display_text(line, preserve_line_breaks=False)
        section = _SECTION_READING_RE.fullmatch(line)
        if section and _looks_like_pinyin_annotation(section.group("reading")):
            line = section.group("label").replace(" ", "")
        if line:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
    )


def mixed_script_html(
    text: str,
    chinese_family: str,
    chinese_size: int,
    text_family: str,
    text_size: int,
) -> str:
    """Форматирует CJK и остальной текст разными шрифтами без смены метрик."""
    text = normalize_display_text(text)
    parts: list[str] = []
    for chinese, characters in groupby(text, key=is_cjk):
        value = html.escape("".join(characters)).replace("\n", "<br>")
        family = chinese_family if chinese else text_family
        size = chinese_size if chinese else text_size
        parts.append(
            f'<span style="font-family:\'{html.escape(family)}\'; font-size:{size}pt;">'
            f"{value}</span>"
        )
    return "".join(parts)
