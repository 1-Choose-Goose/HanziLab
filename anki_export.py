from __future__ import annotations

import html
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

import genanki


DIRECT_MODEL_ID = 1938475622
REVERSE_MODEL_ID = 1938475623
DECK_ID = 2059440712
DIRECT_MODEL_NAME = "HanziLab · Китайский → русский"
REVERSE_MODEL_NAME = "HanziLab · Русский → китайский"
DECK_NAME = "HanziLab"

CARD_CSS = r"""
.card {
    box-sizing: border-box;
    padding: 28px 20px;
    background: #ffffff;
    color: #17242a;
    font-family: Arial, "Microsoft YaHei", sans-serif;
    text-align: center;
}
.hanzilab-hanzi {
    margin: 0 0 26px;
    font-family: "KaiTi", "STKaiti", "Microsoft YaHei", sans-serif;
    font-size: 64px;
    line-height: 1.25;
}
.hanzilab-pinyin-toggle {
    font-size: 20px;
    line-height: 1.4;
}
.hint {
    display: inline-block;
    padding: 8px 14px;
    border-radius: 8px;
    color: #b44334;
}
.hanzilab-translation {
    margin: 0 auto;
    max-width: 720px;
    font-size: 28px;
    line-height: 1.5;
    white-space: normal;
}
.nightMode.card, .night_mode.card {
    background: #2b2b2b;
    color: #f4f6f7;
}
.nightMode .hint, .night_mode .hint {
    color: #ffd7cf;
}
"""

FRONT_TEMPLATE = r"""
<div class="hanzilab-hanzi">{{Hanzi}}</div>
<div class="hanzilab-pinyin-toggle">{{hint:Pinyin}}</div>
"""

BACK_TEMPLATE = r"""
{{FrontSide}}
<hr id="answer">
<div class="hanzilab-translation">{{Translation}}</div>
"""

REVERSE_FRONT_TEMPLATE = r"""
<div class="hanzilab-translation">{{Translation}}</div>
"""

REVERSE_BACK_TEMPLATE = r"""
{{FrontSide}}
<hr id="answer">
<div class="hanzilab-hanzi">{{Hanzi}}</div>
<div class="hanzilab-pinyin-toggle">{{hint:Pinyin}}</div>
"""

class ExportableCard(Protocol):
    hanzi: str
    pinyin: str
    translation: str


def _html_field(value: str) -> str:
    """Escape user text and preserve line breaks in an Anki HTML field."""
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    return html.escape(normalized, quote=False).replace("\n", "<br>")


def export_anki_package(cards: Iterable[ExportableCard], path: str | Path) -> int:
    """Create an .apkg with HanziLab's fields, card template and styling."""
    fields = [
        {"name": "Hanzi"},
        {"name": "Pinyin"},
        {"name": "Translation"},
    ]
    direct_model = genanki.Model(
        DIRECT_MODEL_ID,
        DIRECT_MODEL_NAME,
        fields=fields,
        templates=[
            {
                "name": "Китайский → русский",
                "qfmt": FRONT_TEMPLATE,
                "afmt": BACK_TEMPLATE,
            },
        ],
        css=CARD_CSS,
    )
    reverse_model = genanki.Model(
        REVERSE_MODEL_ID,
        REVERSE_MODEL_NAME,
        fields=[
            {"name": "Hanzi"},
            {"name": "Pinyin"},
            {"name": "Translation"},
        ],
        templates=[
            {
                "name": "Русский → китайский",
                "qfmt": REVERSE_FRONT_TEMPLATE,
                "afmt": REVERSE_BACK_TEMPLATE,
            },
        ],
        css=CARD_CSS,
    )
    deck = genanki.Deck(DECK_ID, DECK_NAME)
    exported = 0
    for card in cards:
        hanzi = _html_field(card.hanzi)
        pinyin = _html_field(card.pinyin)
        translation = _html_field(card.translation)
        for direction, model in (
            ("chinese-russian", direct_model),
            ("russian-chinese", reverse_model),
        ):
            deck.add_note(
                genanki.Note(
                    model=model,
                    fields=[hanzi, pinyin, translation],
                    guid=genanki.guid_for(
                        "HanziLab-bidirectional-v2",
                        direction,
                        card.hanzi,
                    ),
                )
            )
        exported += 1

    destination = Path(path)
    descriptor, temporary = tempfile.mkstemp(
        suffix=".apkg",
        dir=destination.parent,
    )
    os.close(descriptor)
    try:
        genanki.Package(deck).write_to_file(temporary)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return exported
