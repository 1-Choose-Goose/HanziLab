from __future__ import annotations

import html
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QByteArray, QMarginsF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QPen,
)
from PySide6.QtSvg import QSvgRenderer

from stroke_order import load_character_data
from text_formatting import is_cjk
from xingshu_trajectories import (
    load_xingshu_trajectory,
    xingshu_font_family,
)

PAGE_WIDTH = 595.276
PAGE_HEIGHT = 841.89
GRID_LEFT = 29.524
GRID_TOP = 29.518
CELL_WIDTH = 35.813
CELL_HEIGHT = 35.790
ROW_STEP = 39.049
GRID_COLUMNS = 15
GRID_ROWS = 20
CELLS_PER_PAGE = GRID_COLUMNS * GRID_ROWS
OUTLINE_COLOR = QColor("#FF3300")
# Keep the inner writing guides behind traced and handwritten strokes.
GUIDE_COLOR = QColor("#F8D4C8")
SOLID_CHARACTER_COLOR = "#172126"
TRACE_CHARACTER_COLOR = "#AAB2B5"


class CopybookError(RuntimeError):
    pass


class CopybookStyle(str, Enum):
    KAITI = "kaiti"
    XINGSHU = "xingshu"

    @property
    def display_name(self) -> str:
        return "楷书" if self is CopybookStyle.KAITI else "行书"


@dataclass(frozen=True)
class CopybookResult:
    path: Path
    characters: tuple[str, ...]
    missing_characters: tuple[str, ...]
    word_page_added: bool
    style: CopybookStyle


@dataclass(frozen=True)
class _CopybookPage:
    characters: tuple[str, ...]
    stroke_groups: tuple[tuple[str, ...], ...]
    show_stroke_order: bool
    word_start_cell: int = 0


@dataclass(frozen=True)
class _AssemblyCell:
    character: str
    strokes: tuple[str, ...]
    sequence_index: int


def _assembled_cells(
    entries: tuple[tuple[tuple[str, tuple[str, ...]], ...], ...],
    row_spacing: int,
    style: CopybookStyle | str = CopybookStyle.KAITI,
) -> list[list[_AssemblyCell | None]]:
    """Lay entries out on the grid, leaving full blank rows between them."""
    selected_style = CopybookStyle(style)
    pages: list[list[_AssemblyCell | None]] = []
    cursor = 0
    for entry_index, entry in enumerate(entries):
        if entry_index:
            occupied_rows = (cursor + GRID_COLUMNS - 1) // GRID_COLUMNS
            cursor = (occupied_rows + row_spacing) * GRID_COLUMNS
        for character, strokes in entry:
            sequence_length = (
                4
                if selected_style is CopybookStyle.XINGSHU
                else len(strokes) + 4
            )
            for sequence_index in range(sequence_length):
                page_index, page_cell = divmod(cursor, CELLS_PER_PAGE)
                while len(pages) <= page_index:
                    pages.append([None] * CELLS_PER_PAGE)
                pages[page_index][page_cell] = _AssemblyCell(
                    character,
                    strokes,
                    sequence_index,
                )
                cursor += 1
    return pages


def hanzi_sequence(text: str) -> tuple[str, ...]:
    return tuple(character for character in text if is_cjk(character))


def unique_hanzi(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(hanzi_sequence(text)))


def suggested_copybook_name(
    text: str,
    style: CopybookStyle | str = CopybookStyle.KAITI,
) -> str:
    characters = "".join(hanzi_sequence(text)[:48]) or "иероглиф"
    selected_style = CopybookStyle(style)
    suffix = "_行书" if selected_style is CopybookStyle.XINGSHU else ""
    return f"{characters}_прописи{suffix}.pdf"


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
    guide_pen.setWidthF(0.40)
    guide_pen.setStyle(Qt.PenStyle.CustomDashLine)
    guide_pen.setDashPattern([4.0, 4.0])

    for index in range(CELLS_PER_PAGE):
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


def _draw_xingshu_character(
    painter: QPainter,
    cell_index: int,
    character: str,
    *,
    color: str,
    opacity: float,
) -> None:
    target = _cell_rect(cell_index).adjusted(1.3, 1.1, -1.3, -1.1)
    font = QFont(xingshu_font_family())
    font.setPixelSize(max(10, round(target.height() * 0.88)))
    font.setWeight(QFont.Weight.Normal)
    painter.save()
    try:
        painter.setOpacity(opacity)
        painter.setPen(QColor(color))
        painter.setFont(font)
        painter.drawText(target, Qt.AlignmentFlag.AlignCenter, character)
    finally:
        painter.restore()


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


def _draw_xingshu_character_sequence(
    painter: QPainter,
    character: str,
) -> None:
    """Draw one XingShu model and three complete tracing copies."""
    _draw_xingshu_character(
        painter,
        0,
        character,
        color=SOLID_CHARACTER_COLOR,
        opacity=1.0,
    )

    for offset in range(3):
        _draw_xingshu_character(
            painter,
            1 + offset,
            character,
            color=TRACE_CHARACTER_COLOR,
            opacity=0.48,
        )


def _word_cells(length: int, start_cell: int) -> Iterator[tuple[int, int, str, float]]:
    """Yield the model and three tracing copies across fixed-size pages."""
    for index in range(start_cell, min(length * 4, start_cell + CELLS_PER_PAGE)):
        is_model = index < length
        yield (
            index - start_cell,
            index % length,
            SOLID_CHARACTER_COLOR if is_model else TRACE_CHARACTER_COLOR,
            1.0 if is_model else 0.48,
        )


def _draw_word_sequence(
    painter: QPainter,
    stroke_groups: tuple[tuple[str, ...], ...],
    *,
    start_cell: int = 0,
) -> None:
    """Draw this page's part of the complete word and its tracing copies."""
    for cell_index, character_index, color, opacity in _word_cells(len(stroke_groups), start_cell):
        _draw_strokes(
            painter,
            cell_index,
            list(stroke_groups[character_index]),
            color=color,
            opacity=opacity,
        )


def _draw_xingshu_word_sequence(
    painter: QPainter,
    characters: tuple[str, ...],
    *,
    start_cell: int = 0,
) -> None:
    """Draw this page's part of the XingShu word and its tracing copies."""
    for cell_index, character_index, color, opacity in _word_cells(len(characters), start_cell):
        _draw_xingshu_character(
            painter,
            cell_index,
            characters[character_index],
            color=color,
            opacity=opacity,
        )


def _draw_assembly_cell(
    painter: QPainter,
    cell_index: int,
    cell: _AssemblyCell,
    style: CopybookStyle,
) -> None:
    if style is CopybookStyle.XINGSHU:
        _draw_xingshu_character(
            painter,
            cell_index,
            cell.character,
            color=(
                SOLID_CHARACTER_COLOR
                if cell.sequence_index == 0
                else TRACE_CHARACTER_COLOR
            ),
            opacity=1.0 if cell.sequence_index == 0 else 0.48,
        )
        return

    stroke_count = len(cell.strokes)
    if cell.sequence_index == 0:
        _draw_strokes(
            painter,
            cell_index,
            list(cell.strokes),
            color=SOLID_CHARACTER_COLOR,
            opacity=1.0,
        )
        return

    if cell.sequence_index <= stroke_count:
        _draw_strokes(
            painter,
            cell_index,
            list(cell.strokes[: cell.sequence_index]),
            color=TRACE_CHARACTER_COLOR,
            opacity=0.48,
        )
        return

    _draw_strokes(
        painter,
        cell_index,
        list(cell.strokes),
        color=TRACE_CHARACTER_COLOR,
        opacity=0.48,
    )


def _write_copybook(
    path: Path,
    pages: list[_CopybookPage],
    style: CopybookStyle,
) -> None:
    writer = QPdfWriter(str(path))
    writer.setTitle(f"HanziLab - прописи {style.display_name}")
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
            if style is CopybookStyle.XINGSHU and page.show_stroke_order:
                _draw_xingshu_character_sequence(
                    painter,
                    page.characters[0],
                )
            elif style is CopybookStyle.XINGSHU:
                _draw_xingshu_word_sequence(
                    painter, page.characters, start_cell=page.word_start_cell,
                )
            elif page.show_stroke_order:
                _draw_character_sequence(painter, list(page.stroke_groups[0]))
            else:
                _draw_word_sequence(
                    painter, page.stroke_groups, start_cell=page.word_start_cell,
                )
    finally:
        painter.end()


def _write_assembled_copybook(
    path: Path,
    pages: list[list[_AssemblyCell | None]],
    style: CopybookStyle,
) -> None:
    writer = QPdfWriter(str(path))
    writer.setTitle(f"HanziLab - сборник прописей {style.display_name}")
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
            for cell_index, cell in enumerate(page):
                if cell is not None:
                    _draw_assembly_cell(painter, cell_index, cell, style)
    finally:
        painter.end()


def generate_hanzi_copybook(
    text: str,
    output_path: str | Path,
    style: CopybookStyle | str = CopybookStyle.KAITI,
) -> CopybookResult:
    selected_style = CopybookStyle(style)
    sequence = hanzi_sequence(text)
    characters = unique_hanzi(text)
    if not characters:
        raise CopybookError("В выбранной статье нет иероглифов.")

    strokes_by_character: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for character in characters:
        data = load_character_data(character)
        strokes = tuple(data.get("strokes", [])) if data else ()
        trajectory_available = (
            selected_style is not CopybookStyle.XINGSHU
            or load_xingshu_trajectory(character) is not None
        )
        if strokes and trajectory_available:
            strokes_by_character[character] = strokes
        else:
            missing.append(character)
    if not strokes_by_character:
        if selected_style is CopybookStyle.XINGSHU:
            raise CopybookError("Для выбранного иероглифа нет образца XingShu.")
        raise CopybookError("Для выбранного иероглифа нет данных о порядке черт.")

    pages: list[_CopybookPage] = []
    word_page_added = len(sequence) >= 2 and all(
        character in strokes_by_character for character in sequence
    )
    if word_page_added:
        stroke_groups = tuple(strokes_by_character[character] for character in sequence)
        pages.extend(
            _CopybookPage(
                characters=sequence,
                stroke_groups=stroke_groups,
                show_stroke_order=False,
                word_start_cell=start_cell,
            )
            for start_cell in range(0, len(sequence) * 4, CELLS_PER_PAGE)
        )
    pages.extend(
        _CopybookPage(
            characters=(character,),
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
        prefix=".hanzilab_",
        suffix=".pdf",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _write_copybook(temporary_path, pages, selected_style)
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
        style=selected_style,
    )


def generate_assembled_copybook(
    items: list[str] | tuple[str, ...],
    output_path: str | Path,
    *,
    row_spacing: int = 3,
    style: CopybookStyle | str = CopybookStyle.KAITI,
) -> CopybookResult:
    """Create one compact copybook from independently spaced words or hanzi."""
    selected_style = CopybookStyle(style)
    if not 0 <= row_spacing <= GRID_ROWS:
        raise CopybookError("Отступ должен быть от 0 до 20 строк.")

    sequences = [hanzi_sequence(item) for item in items]
    sequences = [sequence for sequence in sequences if sequence]
    if not sequences:
        raise CopybookError("Добавьте в сборник хотя бы один иероглиф.")

    characters = tuple(dict.fromkeys(character for sequence in sequences for character in sequence))
    strokes_by_character: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for character in characters:
        data = load_character_data(character)
        strokes = tuple(data.get("strokes", [])) if data else ()
        trajectory_available = (
            selected_style is not CopybookStyle.XINGSHU
            or load_xingshu_trajectory(character) is not None
        )
        if strokes and trajectory_available:
            strokes_by_character[character] = strokes
        else:
            missing.append(character)

    entries = tuple(
        tuple(
            (character, strokes_by_character[character])
            for character in sequence
            if character in strokes_by_character
        )
        for sequence in sequences
    )
    entries = tuple(entry for entry in entries if entry)
    if not entries:
        if selected_style is CopybookStyle.XINGSHU:
            raise CopybookError("Для выбранных иероглифов нет образцов XingShu.")
        raise CopybookError("Для выбранных иероглифов нет данных о порядке черт.")
    pages = _assembled_cells(entries, row_spacing, selected_style)

    target = Path(output_path).expanduser()
    if target.suffix.lower() != ".pdf":
        target = target.with_suffix(".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".hanzilab_",
        suffix=".pdf",
        dir=target.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        _write_assembled_copybook(temporary_path, pages, selected_style)
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
        word_page_added=False,
        style=selected_style,
    )
