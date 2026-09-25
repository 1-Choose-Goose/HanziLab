"""Offline catalogue of actual handwritten samples from Huang Ruozhou's book."""

from __future__ import annotations

import json
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path

from text_formatting import is_cjk

ASSET_DIR = Path(__file__).resolve().parent / "assets" / "cursive"
STROKE_DATA_DIR = Path(__file__).resolve().parent / "assets" / "strokes"

# Variant forms which occur in the cursive catalogue but do not have their own
# Hanzi Writer record.  Keeping their counts here lets the whole catalogue take
# part in stroke-count sorting.
STROKE_COUNT_FALLBACKS = {
    "𠂇": 2,
    "𠂉": 2,
    "牜": 4,
    "巟": 6,
    "彫": 11,
    "滆": 13,
}


@dataclass(frozen=True)
class CursiveSample:
    id: str
    character: str
    page: int
    book_page: int
    row: int
    column: int
    image: str
    label: str

    @property
    def image_path(self) -> Path:
        return ASSET_DIR / self.image


@cache
def load_catalog() -> tuple[CursiveSample, ...]:
    data = json.loads((ASSET_DIR / "catalog.json").read_text(encoding="utf-8"))
    return tuple(CursiveSample(**sample) for sample in data["samples"])


@cache
def stroke_count(character: str) -> int:
    """Return the number of strokes, placing unknown characters last."""
    try:
        data = json.loads(
            (STROKE_DATA_DIR / f"{character}.json").read_text(encoding="utf-8")
        )
        strokes = data.get("strokes")
        if isinstance(strokes, list) and strokes:
            return len(strokes)
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    return STROKE_COUNT_FALLBACKS.get(character, 10_000)


@lru_cache(maxsize=64)
def group_samples(
    query: str = "", page: int | None = None
) -> dict[str, tuple[CursiveSample, ...]]:
    groups: dict[str, list[CursiveSample]] = {}
    for sample in load_catalog():
        if page is not None and sample.page != page:
            continue
        if query.strip() and sample.character not in query.strip():
            continue
        groups.setdefault(sample.character, []).append(sample)
    return {
        character: tuple(groups[character])
        for character in sorted(groups, key=stroke_count)
    }


@cache
def load_decompositions() -> dict[str, str]:
    return json.loads(
        (ASSET_DIR / "decomposition" / "ids.json").read_text(encoding="utf-8")
    )


@dataclass(frozen=True)
class ComponentMatch:
    character: str
    parts: tuple[str, ...]
    unresolved: tuple[str, ...]


def find_components(
    character: str,
    available: AbstractSet[str],
    decompositions: Mapping[str, str] | None = None,
) -> ComponentMatch:
    """Keep the largest available named components; recursively expand missing ones."""
    ids = load_decompositions() if decompositions is None else decompositions

    def visit(part: str, ancestors: frozenset[str]) -> tuple[list[str], list[str]]:
        if part in available:
            return [part], []
        if part in ancestors or len(ancestors) >= 24:
            return [], [part]
        decomposition = ids.get(part, "")
        if not decomposition or decomposition.startswith("？"):
            return [], [part]
        # IDS layout operators describe placement, not additional pen strokes.
        leaves = [
            ch
            for ch in decomposition
            if is_cjk(ch) or 0x2E80 <= ord(ch) <= 0x2FDF or ch == "？"
        ]
        if not leaves:
            return [], [part]
        found, missing = [], []
        for leaf in leaves:
            matches, unknown = visit(leaf, ancestors | {part})
            found.extend(matches)
            missing.extend(unknown)
        return found, missing

    parts, unresolved = visit(character, frozenset())
    return ComponentMatch(character, tuple(parts), tuple(unresolved))


@dataclass(frozen=True)
class CursiveSearchResult:
    groups: dict[str, tuple[CursiveSample, ...]]
    components: tuple[ComponentMatch, ...] = ()
    missing: tuple[str, ...] = ()


@lru_cache(maxsize=128)
def search_cursive(query: str) -> CursiveSearchResult:
    catalogue = group_samples()
    if not query.strip():
        return CursiveSearchResult(catalogue)
    characters = tuple(
        dict.fromkeys(ch for ch in query if is_cjk(ch) or ch in catalogue)
    )
    groups, components, missing = {}, [], []
    for character in characters:
        if character in catalogue:
            groups[character] = catalogue[character]
            continue
        match = find_components(character, catalogue.keys())
        if match.parts:
            components.append(match)
            for part in match.parts:
                groups.setdefault(part, catalogue[part])
        else:
            missing.append(character)
    return CursiveSearchResult(groups, tuple(components), tuple(missing))
