from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Sequence
from functools import lru_cache
from itertools import pairwise

import stroke_order

Point = tuple[float, float]
Stroke = Sequence[Point]
SAMPLES_PER_STROKE = 10


def _resample(stroke: Stroke, count: int = SAMPLES_PER_STROKE) -> list[Point]:
    if count <= 0:
        return []
    if not stroke:
        return [(0.0, 0.0)] * count
    if len(stroke) == 1 or count == 1:
        return [stroke[0]] * count
    lengths = [0.0]
    for first, second in pairwise(stroke):
        lengths.append(lengths[-1] + math.dist(first, second))
    total = lengths[-1]
    if total <= 1e-9:
        return [stroke[0]] * count

    result: list[Point] = []
    segment = 1
    for index in range(count):
        target = total * index / (count - 1)
        while segment < len(lengths) - 1 and lengths[segment] < target:
            segment += 1
        start_length = lengths[segment - 1]
        end_length = lengths[segment]
        ratio = (
            (target - start_length) / (end_length - start_length)
            if end_length > start_length
            else 0.0
        )
        start = stroke[segment - 1]
        end = stroke[segment]
        result.append(
            (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
        )
    return result


def _signature(strokes: Iterable[Stroke]) -> tuple[bytes, ...]:
    prepared = [[(float(x), float(y)) for x, y in stroke] for stroke in strokes]
    prepared = [stroke for stroke in prepared if stroke]
    if not prepared:
        return ()
    points = [point for stroke in prepared for point in stroke]
    left = min(point[0] for point in points)
    right = max(point[0] for point in points)
    top = min(point[1] for point in points)
    bottom = max(point[1] for point in points)
    width = max(right - left, 1e-6)
    height = max(bottom - top, 1e-6)
    scale = max(width, height)
    x_margin = (1.0 - width / scale) / 2.0
    y_margin = (1.0 - height / scale) / 2.0

    result: list[bytes] = []
    for stroke in prepared:
        values: list[int] = []
        for x, y in _resample(stroke):
            normalized_x = x_margin + (x - left) / scale
            normalized_y = y_margin + (y - top) / scale
            values.extend(
                (
                    max(0, min(255, round(normalized_x * 255))),
                    max(0, min(255, round(normalized_y * 255))),
                )
            )
        result.append(bytes(values))
    return tuple(result)


def _stroke_center(stroke: bytes) -> tuple[float, float]:
    xs = stroke[0::2]
    ys = stroke[1::2]
    return sum(ys) / len(ys), sum(xs) / len(xs)


def _distance(first: tuple[bytes, ...], second: tuple[bytes, ...]) -> float:
    ordered = sum(
        sum((left - right) ** 2 for left, right in zip(a, b))
        for a, b in zip(first, second)
    )
    # A spatial comparison keeps recognition useful when a learner draws the
    # correct lines in a slightly different order.
    spatial_first = sorted(first, key=_stroke_center)
    spatial_second = sorted(second, key=_stroke_center)
    spatial = sum(
        sum((left - right) ** 2 for left, right in zip(a, b))
        for a, b in zip(spatial_first, spatial_second)
    )
    return ordered * 0.68 + spatial * 0.32


@lru_cache(maxsize=1)
def handwriting_index() -> dict[int, tuple[tuple[str, tuple[bytes, ...]], ...]]:
    """Build a compact offline index from the bundled stroke medians."""
    grouped: dict[int, list[tuple[str, tuple[bytes, ...]]]] = {}
    for path in stroke_order.STROKE_DATA_DIR.glob("*.json"):
        character = path.stem
        if len(character) != 1:
            continue
        data = stroke_order.load_character_data(character)
        if data is None:
            continue
        medians = data.get("medians")
        if not medians or len(medians) > 40:
            continue
        # Make Me a Hanzi uses an upward Y axis; the drawing canvas uses the
        # conventional screen direction, so mirror it before matching.
        canvas_strokes = [
            [(float(x), float(900 - y)) for x, y in stroke]
            for stroke in medians
            if stroke
        ]
        signature = _signature(canvas_strokes)
        if signature:
            grouped.setdefault(len(signature), []).append((character, signature))
    return {count: tuple(values) for count, values in grouped.items()}


def recognize_handwriting(strokes: Iterable[Stroke], limit: int = 10) -> list[str]:
    """Return locally ranked character candidates for the drawn strokes."""
    signature = _signature(strokes)
    if not signature or limit <= 0:
        return []
    candidates = handwriting_index().get(len(signature), ())
    ranked = heapq.nsmallest(
        limit,
        ((_distance(signature, candidate), character) for character, candidate in candidates),
    )
    return [character for _score, character in ranked]
