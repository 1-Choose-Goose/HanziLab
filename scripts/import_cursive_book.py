"""Extract the book's labelled single-character tables (PDF pages 10–52).

Development-only dependencies: pymupdf, opencv-python-headless, numpy.
Coordinates are measured on a 2x rendering; transcriptions preserve source forms.
The desktop application needs only the resulting JSON/PNG/PDF assets.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pymupdf

ROOT = Path(__file__).resolve().parents[1]


def line_near(mask, expected, radius=12):
    lo, hi = (
        max(0, round(expected - radius)),
        min(len(mask), round(expected + radius + 1)),
    )
    candidates = np.flatnonzero(mask[lo:hi] > 0.65) + lo
    return (
        int(min(candidates, key=lambda p: abs(p - expected)))
        if len(candidates)
        else round(expected)
    )


def remove_border_fragments(image):
    result = image.copy()
    binary = cv2.threshold(image, 180, 255, cv2.THRESH_BINARY_INV)[1]
    height, width = image.shape
    _, ink_components, ink_bounds, _ = cv2.connectedComponentsWithStats(binary)
    # A frame corner is one connected component, so removing only narrow
    # components misses L/U-shaped borders. Isolate their straight thin runs
    # first, restricting removal to the margins of the original cell.
    lines = np.zeros_like(binary)
    for horizontal, kernel in (
        (True, (max(25, round(width * 0.35)), 1)),
        (False, (1, max(35, round(height * 0.55)))),
    ):
        opened = cv2.morphologyEx(
            binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, kernel)
        )
        n, components, bounds, _ = cv2.connectedComponentsWithStats(opened)
        for i in range(1, n):
            x, y, w, h, _ = bounds[i]
            border = (
                h <= 5 and (y + h < height * 0.18 or y > height * 0.82)
                if horizontal
                else w <= 5 and (x + w < width * 0.20 or x > width * 0.80)
            )
            if border:
                region = components == i
                for ink_id in np.unique(ink_components[region]):
                    if ink_id == 0:
                        continue
                    _, _, iw, ih, area = ink_bounds[ink_id]
                    # A thin run within a thick pen stroke is still handwriting.
                    # Only isolated rules or sparse frame components qualify.
                    if min(iw, ih) <= 5 or (
                        area < (iw + ih) * 6 and area < iw * ih * 0.20
                    ):
                        lines[ink_components == ink_id] = 255
    result[cv2.dilate(lines, np.ones((5, 5), np.uint8)) > 0] = 255
    binary = cv2.threshold(result, 180, 255, cv2.THRESH_BINARY_INV)[1]
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary)
    for index in range(1, count):
        x, y, w, h, area = stats[index]
        vertical = (
            h > height * 0.6
            and w < width * 0.12
            and (x < width * 0.22 or x + w > width * 0.78)
        )
        horizontal = (
            w > width * 0.3 and h < height * 0.08 and (y < 12 or y + h > height - 12)
        )
        edge_fragment = area < 100 and (
            x < 20 or x + w > width - 20 or y < 12 or y + h > height - 12
        )
        if vertical or horizontal or edge_fragment:
            result[
                cv2.dilate(
                    (labels == index).astype(np.uint8), np.ones((3, 3), np.uint8)
                )
                > 0
            ] = 255
    return result


def extract(source: Path, output: Path) -> None:
    rows_by_page = json.loads(
        (ROOT / "scripts/cursive_transcriptions.json").read_text(encoding="utf-8")
    )
    geometry = json.loads(
        (ROOT / "scripts/cursive_geometry.json").read_text(encoding="utf-8")
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "samples").mkdir(exist_ok=True)
    (output / "labels").mkdir(exist_ok=True)
    records = []
    document = pymupdf.open(source)
    for page_str, rows in rows_by_page.items():
        page = int(page_str)
        # Page 15 has seven rows and a blank footer inside the printed frame.
        count = len(rows)
        corners = np.float32(geometry[page_str]) * 2
        pix = document[page - 1].get_pixmap(
            matrix=pymupdf.Matrix(4, 4), colorspace=pymupdf.csGRAY
        )
        raw = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        flat = cv2.warpPerspective(
            raw,
            cv2.getPerspectiveTransform(
                corners,
                np.float32([[0, 0], [1800, 0], [1800, count * 300], [0, count * 300]]),
            ),
            (1800, count * 300),
            borderValue=255,
        )
        gutter = 20 if page <= 15 else 40 if page <= 34 else 22 if page <= 37 else 12
        step = 150 + gutter / count
        for row, characters in enumerate(rows):
            for col, character in enumerate(characters):
                if character == ".":
                    continue
                if page <= 15:
                    left, right = col * 900 / 7, (col + 1) * 900 / 7 - 7
                    ink_height, label_height = 91, 41
                elif page <= 34:
                    # Two panels: 0..424 and 478..900.
                    left = 125 + (col % 3) * 99 + (col // 3) * 482
                    right = left + 97
                    ink_height, label_height = 80, 37
                elif page <= 37:
                    left = (col // 2) * 238 + (col % 2) * 96
                    right = left + 96
                    ink_height, label_height = 92, 43
                else:
                    left, right = (col + 1) * 100, (col + 2) * 100
                    ink_height, label_height = 96, 43
                left, right = round(left * 2), min(flat.shape[1], round(right * 2))
                middle = flat[:, left + 16 : right - 16]
                horizontal = (middle < 130).mean(axis=1)
                top = line_near(horizontal, row * step * 2, 20)
                bottom = line_near(horizontal, row * step * 2 + ink_height * 2, 20)
                label_bottom = line_near(horizontal, bottom + label_height * 2, 16)
                # Refine vertical edges on each individual cell (scan pages are not perfectly regular).
                band = flat[top + 8 : bottom - 8]
                vertical = (band < 130).mean(axis=0)
                left = line_near(vertical, left, 22)
                right = line_near(vertical, right, 22)
                if not (right > left + 35 and bottom > top + 35):
                    raise ValueError(f"Invalid cell {page}/{row}/{col}")
                sample = remove_border_fragments(
                    flat[top + 5 : bottom - 5, left + 5 : right - 5]
                )
                # The original scans are monochrome. Discard faint antialiased
                # grid remnants without inventing or redrawing pen strokes.
                sample[sample > 180] = 255
                label = remove_border_fragments(
                    flat[bottom + 5 : label_bottom - 5, left + 5 : right - 5]
                )
                # Preserve proportions and all pen strokes, remove only white margins.
                ys, xs = np.where(sample < 170)
                if not len(xs):
                    raise ValueError(f"Empty sample {page}/{row}/{col}: {character}")
                sample = sample[
                    max(0, ys.min() - 5) : ys.max() + 6,
                    max(0, xs.min() - 5) : xs.max() + 6,
                ]
                key = f"{page:02d}-{row + 1:02d}-{col + 1:02d}"
                # OpenCV's Windows file API cannot reliably write Cyrillic paths.
                (output / "samples" / f"{key}.png").write_bytes(
                    cv2.imencode(".png", sample)[1].tobytes()
                )
                (output / "labels" / f"{key}.png").write_bytes(
                    cv2.imencode(".png", label)[1].tobytes()
                )
                records.append(
                    {
                        "id": key,
                        "character": character,
                        "page": page,
                        "book_page": page - 6,
                        "row": row + 1,
                        "column": col + 1,
                        "image": f"samples/{key}.png",
                        "label": f"labels/{key}.png",
                    }
                )
    document.close()
    shutil.copyfile(source, output / "source.pdf")
    manifest = {
        "title": "怎样快写钢笔字",
        "author": "黄若舟",
        "source": "source.pdf",
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "description": "Подписанные образцы одиночных знаков и компонентов, страницы PDF 10–52.",
        "samples": records,
    }
    (output / "catalog.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"{len(records)} samples; {len({r['character'] for r in records})} characters"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "assets/cursive")
    args = parser.parse_args()
    extract(args.pdf, args.output)
