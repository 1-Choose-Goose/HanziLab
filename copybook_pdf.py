from __future__ import annotations

import html
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QMarginsF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
)
from PySide6.QtSvg import QSvgRenderer

from stroke_order import load_character_data
from text_formatting import is_cjk

PAGE_WIDTH = 595.276
PAGE_HEIGHT = 841.89
GRID_LEFT = 29.524
GRID_TOP = 29.518
CELL_WIDTH = 35.813
CELL_HEIGHT = 35.790
ROW_STEP = 39.049
GRID_COLUMNS = 15
GRID_ROWS = 20
OUTLINE_COLOR = QColor("#FF3300")
GUIDE_COLOR = QColor("#FF6600")
SOLID_CHARACTER_COLOR = "#172126"
TRACE_CHARACTER_COLOR = "#AAB2B5"


class CopybookError(RuntimeError):
    pass


@dataclass(frozen=True)
class CopybookResult:
    path: Path
    characters: tuple[str, ...]
    missing_characters: tuple[str, ...]
    word_page_added: bool


@dataclass(frozen=True)
class _CopybookPage:
    stroke_groups: tuple[tuple[str, ...], ...]
    show_stroke_order: bool


def hanzi_sequence(text: str) -> tuple[str, ...]:
    return tuple(character for character in text if is_cjk(character))


def unique_hanzi(text: str) -> tuple[str, ...]:
    characters: list[str] = []
    for character in hanzi_sequence(text):
        if character not in characters:
            characters.append(character)
    return tuple(characters)


def suggested_copybook_name(text: str) -> str:
    characters = "".join(hanzi_sequence(text)) or "иероглиф"
    return f"{characters}_прописи.pdf"


def _stroke_svg(paths: list[str], color: str, opacity: float = 1.0) -> QByteArray:
    elements = "".join(
        f'<path d="{html.escape(path, quote=True)}"/>' for path in paths
    )
    document = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
        f'<g transform="translate(0,900) scale(1,-1)" fill="{color}" '
        f'opacity="{opacity:.3f}">{elements}</g></svg>'
    )
    return QByteArray(document.encode("utf-8"))


def _cell_rect(index: int) -> QRectF:
    row, column = divmod(index, GRID_COLUMNS)
    return QRectF(
        GRID_LEFT + column * CELL_WIDTH,
        GRID_TOP + row * ROW_STEP,
        CELL_WIDTH,
        CELL_HEIGHT,
    )


def _draw_grid(painter: QPainter) -> None:
    outline_pen = QPen(OUTLINE_COLOR)
    outline_pen.setWidthF(1.1)
    outline_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    guide_pen = QPen(GUIDE_COLOR)
    guide_pen.setWidthF(0.55)
    guide_pen.setStyle(Qt.PenStyle.CustomDashLine)
    guide_pen.setDashPattern([4.0, 4.0])

    for index in range(GRID_COLUMNS * GRID_ROWS):
        rect = _cell_rect(index)
        painter.setPen(outline_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(rect)
        painter.setPen(guide_pen)
        painter.drawLine(rect.left(), rect.center().y(), rect.right(), rect.center().y())
        painter.drawLine(rect.center().x(), rect.top(), rect.center().x(), rect.bottom())


def _draw_strokes(
    painter: QPainter,
    cell_index: int,
    paths: list[str],
    *,
    color: str,
    opacity: float,
) -> None:
    if not paths:
        return
    target = _cell_rect(cell_index).adjusted(1.7, 1.7, -1.7, -1.7)
    renderer = QSvgRenderer(_stroke_svg(paths, color, opacity))
    if not renderer.isValid():
        raise CopybookError("Не удалось подготовить контуры иероглифа.")
    renderer.render(painter, target)


def _draw_character_sequence(painter: QPainter, paths: list[str]) -> None:
    # 1. A complete dark model.
    _draw_strokes(
        painter,
        0,
        paths,
        color=SOLID_CHARACTER_COLOR,
        opacity=1.0,
    )

    # 2. The writing order: every next cell adds one more translucent stroke.
    for stroke_index in range(len(paths)):
        _draw_strokes(
            painter,
            stroke_index + 1,
            paths[: stroke_index + 1],
            color=TRACE_CHARACTER_COLOR,
            opacity=0.48,
        )

    # 3. Three complete translucent characters for tracing.
    first_trace_cell = len(paths) + 1
    for offset in range(3):
        _draw_strokes(
            painter,
            first_trace_cell + offset,
            paths,
            color=TRACE_CHARACTER_COLOR,
            opacity=0.48,
        )


def _draw_word_sequence(
    painter: QPainter,
    stroke_groups: tuple[tuple[str, ...], ...],
) -> None:
    """Draw one complete word followed by three complete tracing copies."""
    cell_index = 0
    for paths in stroke_groups:
        _draw_strokes(
            painter,
            cell_index,
            list(paths),
            color=SOLID_CHARACTER_COLOR,
            opacity=1.0,
        )
        cell_index += 1

    for _repetition in range(3):
        for paths in stroke_groups:
            _draw_strokes(
                painter,
                cell_index,
                list(paths),
                color=TRACE_CHARACTER_COLOR,
                opacity=0.48,
            )
            cell_index += 1


def _write_copybook(path: Path, pages: list[_CopybookPage]) -> None:
    writer = QPdfWriter(str(path))
    writer.setTitle("HanziLab - прописи иероглифов")
    writer.setCreator("HanziLab")
    writer.setResolution(72)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Point)

    painter = QPainter(writer)
    if not painter.isActive():
        raise CopybookError("Не удалось открыть PDF для записи.")
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for page_index, page in enumerate(pages):
            if page_index and not writer.newPage():
                raise CopybookError("Не удалось добавить страницу в PDF.")
            painter.fillRect(QRectF(0, 0, PAGE_WIDTH, PAGE_HEIGHT), QColor("#FFFFFF"))
            _draw_grid(painter)
            if page.show_stroke_order:
                _draw_character_sequence(painter, list(page.stroke_groups[0]))
            else:
                _draw_word_sequence(painter, page.stroke_groups)
    finally:
        painter.end()


def generate_hanzi_copybook(text: str, output_path: str | Path) -> CopybookResult:
    sequence = hanzi_sequence(text)
    characters = unique_hanzi(text)
    if not characters:
        raise CopybookError("В выбранной статье нет иероглифов.")

    strokes_by_character: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for character in characters:
        data = load_character_data(character)
        strokes = tuple(data.get("strokes", [])) if data else ()
        if strokes:
            strokes_by_character[character] = strokes
        else:
            missing.append(character)
    if not strokes_by_character:
        raise CopybookError(
            "Для выбранного иероглифа нет данных о порядке черт."
        )

    pages: list[_CopybookPage] = []
    word_page_added = len(sequence) >= 2 and all(
        character in strokes_by_character for character in sequence
    )
    if word_page_added:
        pages.append(
            _CopybookPage(
                stroke_groups=tuple(
                    strokes_by_character[character] for character in sequence
                ),
                show_stroke_order=False,
            )
        )
    pages.extend(
        _CopybookPage(
            stroke_groups=(strokes_by_character[character],),
            show_stroke_order=True,
        )
        for character in characters
        if character in strokes_by_character
    )

    target = Path(output_path).expanduser()
    if target.suffix.lower() != ".pdf":
        target = target.with_suffix(".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.stem}_",
        suffix=".pdf",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _write_copybook(temporary_path, pages)
        if temporary_path.stat().st_size < 1000:
            raise CopybookError("Созданный PDF оказался пустым.")
        os.replace(temporary_path, target)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return CopybookResult(
        path=target,
        characters=tuple(strokes_by_character),
        missing_characters=tuple(missing),
        word_page_added=word_page_added,
    )
