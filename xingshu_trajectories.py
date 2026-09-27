from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter, QRawFont

from stroke_order import load_character_data

if TYPE_CHECKING:
    import numpy as np

Point = tuple[float, float]
Pixel = tuple[int, int]
Stroke = tuple[Point, ...]

CANVAS_SIZE = 1024.0
RASTER_SIZE = 256
FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "xingshu.ttf"


@dataclass(frozen=True)
class XingShuTrajectory:
    character: str
    strokes: tuple[Stroke, ...]


@lru_cache(maxsize=1)
def _numpy():
    import numpy

    return numpy


@lru_cache(maxsize=1)
def xingshu_font_family() -> str:
    font_id = QFontDatabase.addApplicationFont(str(FONT_PATH))
    if font_id >= 0:
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return ""


def _xingshu_font(pixel_size: int) -> QFont:
    font = QFont(xingshu_font_family())
    font.setPixelSize(pixel_size)
    font.setWeight(QFont.Weight.Normal)
    return font


def xingshu_supports_character(character: str) -> bool:
    if len(character) != 1 or not xingshu_font_family():
        return False
    raw_font = QRawFont.fromFont(_xingshu_font(192))
    return raw_font.isValid() and raw_font.supportsCharacter(ord(character))


def _render_font_mask(character: str) -> np.ndarray | None:
    np = _numpy()
    if not xingshu_supports_character(character):
        return None
    image = QImage(RASTER_SIZE, RASTER_SIZE, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    if not painter.isActive():
        return None
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#FFFFFF"))
        painter.setFont(_xingshu_font(round(RASTER_SIZE * 0.88)))
        painter.drawText(
            QRectF(0, 0, RASTER_SIZE, RASTER_SIZE),
            Qt.AlignmentFlag.AlignCenter,
            character,
        )
    finally:
        painter.end()

    buffer = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes())
    pixels = buffer.reshape((image.height(), image.bytesPerLine()))
    alpha = pixels[:, 3 : image.width() * 4 : 4]
    mask = alpha > 24
    return mask if np.any(mask) else None


def _neighbors(image: np.ndarray) -> tuple[np.ndarray, ...]:
    np = _numpy()
    padded = np.pad(image, 1, mode="constant")
    height, width = image.shape
    return (
        padded[0:height, 1 : width + 1],
        padded[0:height, 2 : width + 2],
        padded[1 : height + 1, 2 : width + 2],
        padded[2 : height + 2, 2 : width + 2],
        padded[2 : height + 2, 1 : width + 1],
        padded[2 : height + 2, 0:width],
        padded[1 : height + 1, 0:width],
        padded[0:height, 0:width],
    )


def _thinning_pass(image: np.ndarray, second: bool) -> bool:
    np = _numpy()
    adjacent = _neighbors(image)
    count = sum(item.astype(np.uint8) for item in adjacent)
    transitions = sum(
        ((first == 0) & (following == 1)).astype(np.uint8)
        for first, following in zip(
            adjacent, adjacent[1:] + adjacent[:1], strict=True
        )
    )
    north, _north_east, east, _south_east, south, _south_west, west, _north_west = adjacent
    if second:
        first_guard = ~(north & east & west)
        second_guard = ~(north & south & west)
    else:
        first_guard = ~(north & east & south)
        second_guard = ~(east & south & west)
    removable = (
        image
        & (count >= 2)
        & (count <= 6)
        & (transitions == 1)
        & first_guard
        & second_guard
    )
    if not np.any(removable):
        return False
    image[removable] = False
    return True


def _skeletonize(mask: np.ndarray) -> np.ndarray:
    skeleton = mask.copy()
    for _iteration in range(160):
        changed_first = _thinning_pass(skeleton, second=False)
        changed_second = _thinning_pass(skeleton, second=True)
        if not changed_first and not changed_second:
            break
    return skeleton


def _glyph_bounds(mask: np.ndarray) -> tuple[float, float, float, float]:
    np = _numpy()
    rows, columns = np.nonzero(mask)
    return (
        float(columns.min()),
        float(rows.min()),
        float(columns.max()),
        float(rows.max()),
    )


def _mapped_medians(character: str, mask: np.ndarray) -> tuple[Stroke, ...]:
    data = load_character_data(character)
    medians = data.get("medians", []) if data else []
    source_strokes: list[Stroke] = []
    for raw_stroke in medians:
        points = tuple(
            (float(point[0]), float(900 - point[1]))
            for point in raw_stroke
            if isinstance(point, list) and len(point) >= 2
        )
        if len(points) >= 2:
            source_strokes.append(points)
    all_points = tuple(point for stroke in source_strokes for point in stroke)
    if not all_points:
        return ()

    source_left = min(point[0] for point in all_points)
    source_top = min(point[1] for point in all_points)
    source_right = max(point[0] for point in all_points)
    source_bottom = max(point[1] for point in all_points)
    source_width = max(1.0, source_right - source_left)
    source_height = max(1.0, source_bottom - source_top)
    target_left, target_top, target_right, target_bottom = _glyph_bounds(mask)
    target_width = max(1.0, target_right - target_left)
    target_height = max(1.0, target_bottom - target_top)
    return tuple(
        tuple(
            (
                target_left + (x - source_left) / source_width * target_width,
                target_top + (y - source_top) / source_height * target_height,
            )
            for x, y in stroke
        )
        for stroke in source_strokes
    )


def _resample(points: Stroke, step: float = 2.4) -> Stroke:
    sampled: list[Point] = [points[0]]
    for start, end in pairwise(points):
        distance = math.dist(start, end)
        divisions = max(1, math.ceil(distance / step))
        sampled.extend(
            (
                start[0] + (end[0] - start[0]) * index / divisions,
                start[1] + (end[1] - start[1]) * index / divisions,
            )
            for index in range(1, divisions + 1)
        )
    return tuple(sampled)


def _nearest_skeleton_pixels(
    points: Stroke,
    skeleton_coordinates: np.ndarray,
) -> tuple[Pixel, ...]:
    np = _numpy()
    selected: list[Pixel] = []
    for x, y in _resample(points):
        distances = (
            (skeleton_coordinates[:, 1] - x) ** 2
            + (skeleton_coordinates[:, 0] - y) ** 2
        )
        row, column = skeleton_coordinates[int(np.argmin(distances))]
        pixel = (int(row), int(column))
        if not selected or selected[-1] != pixel:
            selected.append(pixel)
    return tuple(selected)


_PIXEL_NEIGHBORS = (
    (-1, -1),
    (-1, 0),
    (-1, 1),
    (0, -1),
    (0, 1),
    (1, -1),
    (1, 0),
    (1, 1),
)


def _skeleton_path(skeleton: np.ndarray, start: Pixel, end: Pixel) -> tuple[Pixel, ...]:
    if start == end:
        return (start,)
    height, width = skeleton.shape
    frontier: list[tuple[float, float, Pixel]] = [(math.dist(start, end), 0.0, start)]
    previous: dict[Pixel, Pixel] = {}
    best_cost: dict[Pixel, float] = {start: 0.0}
    reached = False
    while frontier:
        _priority, cost, current = heapq.heappop(frontier)
        if current == end:
            reached = True
            break
        if cost > best_cost.get(current, float("inf")):
            continue
        for row_step, column_step in _PIXEL_NEIGHBORS:
            neighbor = (current[0] + row_step, current[1] + column_step)
            row, column = neighbor
            if not (0 <= row < height and 0 <= column < width):
                continue
            if not skeleton[row, column]:
                continue
            new_cost = cost + math.hypot(row_step, column_step)
            if new_cost >= best_cost.get(neighbor, float("inf")):
                continue
            best_cost[neighbor] = new_cost
            previous[neighbor] = current
            heapq.heappush(
                frontier,
                (new_cost + math.dist(neighbor, end), new_cost, neighbor),
            )
    if not reached:
        return (start, end)
    path = [end]
    while path[-1] != start:
        path.append(previous[path[-1]])
    path.reverse()
    return tuple(path)


def _trace_projected_stroke(
    projected: tuple[Pixel, ...],
    skeleton: np.ndarray,
) -> tuple[Pixel, ...]:
    if len(projected) < 2:
        return projected
    traced: list[Pixel] = [projected[0]]
    for start, end in pairwise(projected):
        segment = _skeleton_path(skeleton, start, end)
        traced.extend(segment[1:])
    return tuple(pixel for index, pixel in enumerate(traced) if not index or pixel != traced[index - 1])


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    if start == end:
        return math.dist(point, start)
    dx, dy = end[0] - start[0], end[1] - start[1]
    projection = max(
        0.0,
        min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)),
    )
    return math.dist(point, (start[0] + projection * dx, start[1] + projection * dy))


def _simplify(points: Stroke, tolerance: float = 1.15) -> Stroke:
    if len(points) <= 2:
        return points
    start, end = points[0], points[-1]
    distances = tuple(
        _point_line_distance(point, start, end) for point in points[1:-1]
    )
    maximum = max(distances, default=0.0)
    if maximum <= tolerance:
        return (start, end)
    split = distances.index(maximum) + 1
    first = _simplify(points[: split + 1], tolerance)
    second = _simplify(points[split:], tolerance)
    return first[:-1] + second


def _to_canvas(points: tuple[Pixel, ...]) -> Stroke:
    scale = CANVAS_SIZE / RASTER_SIZE
    return tuple((column * scale, row * scale) for row, column in points)


@lru_cache(maxsize=256)
def load_xingshu_trajectory(character: str) -> XingShuTrajectory | None:
    np = _numpy()
    mask = _render_font_mask(character)
    if mask is None:
        return None
    medians = _mapped_medians(character, mask)
    if not medians:
        return None
    skeleton = _skeletonize(mask)
    skeleton_coordinates = np.argwhere(skeleton)
    if not len(skeleton_coordinates):
        return None

    strokes: list[Stroke] = []
    for median in medians:
        projected = _nearest_skeleton_pixels(median, skeleton_coordinates)
        traced = _trace_projected_stroke(projected, skeleton)
        canvas_stroke = _simplify(_to_canvas(traced))
        if len(canvas_stroke) < 2:
            return None
        strokes.append(canvas_stroke)
    return XingShuTrajectory(character=character, strokes=tuple(strokes))
