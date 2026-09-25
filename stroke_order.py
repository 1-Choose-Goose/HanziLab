from __future__ import annotations

import html
import json
import math
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from PySide6.QtCore import QByteArray, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parent
STROKE_DATA_DIR = ROOT / "assets" / "strokes"


@lru_cache(maxsize=256)
def load_character_data(character: str) -> dict | None:
    if len(character) != 1 or character in '/\\:' or ord(character) < 32:
        return None
    path = STROKE_DATA_DIR / f"{character}.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    strokes = data.get("strokes")
    if not isinstance(strokes, list) or not strokes or not all(
        isinstance(stroke, str) and stroke.strip() for stroke in strokes
    ):
        return None
    medians = data.get("medians", [])
    if not isinstance(medians, list) or (medians and len(medians) != len(strokes)):
        return None
    try:
        if any(
            not isinstance(stroke, list)
            or not stroke
            or any(
                not isinstance(point, list)
                or len(point) != 2
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in point
                )
                for point in stroke
            )
            for stroke in medians
        ):
            return None
    except OverflowError:
        return None
    return data


class StrokeCanvas(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.character = ""
        self.data: dict | None = None
        self.current_stroke = 0
        self.stroke_progress = 0.0
        self.setMinimumHeight(300)

    def set_character(self, character: str, data: dict) -> None:
        self.character = character
        self.data = data
        self.current_stroke = 0
        self.stroke_progress = 0.0
        self.update()

    def set_current_stroke(self, index: int) -> None:
        if not self.data:
            return
        self.current_stroke = min(max(index, 0), len(self.data["strokes"]) - 1)
        self.update()

    def set_stroke_progress(self, progress: float) -> None:
        self.stroke_progress = min(max(progress, 0.0), 1.0)
        self.update()

    def reset_animation(self) -> None:
        self.current_stroke = 0
        self.stroke_progress = 0.0
        self.update()

    def current_stroke_duration_ms(self) -> int:
        if not self.data:
            return 500
        medians = self.data.get("medians", [])
        if not (0 <= self.current_stroke < len(medians)):
            return 500
        points = medians[self.current_stroke]
        length = sum(
            math.hypot(x2 - x1, y2 - y1)
            for (x1, y1), (x2, y2) in pairwise(points)
        )
        return round(max(480.0, min(1350.0, length * 1.45)))

    @staticmethod
    def stroke_svg(path: str, color: str) -> QByteArray:
        document = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            '<g transform="translate(0,900) scale(1,-1)">'
            f'<path d="{html.escape(path, quote=True)}" fill="{color}"/>'
            "</g></svg>"
        )
        return QByteArray(document.encode("utf-8"))

    @staticmethod
    def _canvas_point(point: list[float], target: QRectF) -> QPointF:
        x, y = point
        return QPointF(
            target.left() + target.width() * x / 1024,
            target.top() + target.height() * (900 - y) / 1024,
        )

    def _revealed_stroke_area(
        self,
        points: list[list[float]],
        target: QRectF,
        progress: float,
    ) -> QPainterPath:
        if not points or progress <= 0:
            return QPainterPath()
        mapped = [self._canvas_point(point, target) for point in points]
        segment_lengths = [
            math.hypot(second.x() - first.x(), second.y() - first.y())
            for first, second in pairwise(mapped)
        ]
        remaining = sum(segment_lengths) * min(progress, 1.0)
        center_line = QPainterPath(mapped[0])
        for first, second, segment_length in zip(
            mapped, mapped[1:], segment_lengths
        ):
            if segment_length <= 0:
                continue
            if remaining >= segment_length:
                center_line.lineTo(second)
                remaining -= segment_length
                continue
            fraction = remaining / segment_length
            center_line.lineTo(
                QPointF(
                    first.x() + (second.x() - first.x()) * fraction,
                    first.y() + (second.y() - first.y()) * fraction,
                )
            )
            break
        brush_width = max(16.0, target.width() * 0.11)
        stroker = QPainterPathStroker()
        stroker.setWidth(brush_width)
        stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
        stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        revealed = stroker.createStroke(center_line)
        radius = brush_width / 2
        revealed.addEllipse(mapped[0], radius, radius)
        return revealed

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))

        side = max(40.0, min(self.width(), self.height()) - 24.0)
        target = QRectF((self.width() - side) / 2, 12, side, side)
        grid_pen = QPen(QColor("#E4E9EB"), 1, Qt.PenStyle.DashLine)
        painter.setPen(grid_pen)
        painter.drawRect(target)
        painter.drawLine(target.left(), target.center().y(), target.right(), target.center().y())
        painter.drawLine(target.center().x(), target.top(), target.center().x(), target.bottom())
        painter.drawLine(target.topLeft(), target.bottomRight())
        painter.drawLine(target.topRight(), target.bottomLeft())

        if not self.data:
            return

        strokes = self.data["strokes"]
        for index, path in enumerate(strokes):
            if index < self.current_stroke:
                color = "#253239"
            else:
                color = "#E5EAEC"
            QSvgRenderer(self.stroke_svg(path, color)).render(painter, target)

        medians = self.data.get("medians", [])
        if 0 <= self.current_stroke < len(strokes):
            current_path = strokes[self.current_stroke]
            if self.current_stroke < len(medians) and medians[self.current_stroke]:
                revealed = self._revealed_stroke_area(
                    medians[self.current_stroke], target, self.stroke_progress
                )
                painter.save()
                painter.setClipPath(revealed)
                QSvgRenderer(self.stroke_svg(current_path, "#E05945")).render(
                    painter, target
                )
                painter.restore()
            elif self.stroke_progress > 0:
                clip = QRectF(
                    target.left(),
                    target.top(),
                    target.width() * self.stroke_progress,
                    target.height(),
                )
                painter.save()
                painter.setClipRect(clip)
                QSvgRenderer(self.stroke_svg(current_path, "#E05945")).render(
                    painter, target
                )
                painter.restore()

        for index, points in enumerate(medians):
            if not points or index > self.current_stroke:
                continue
            x, y = points[0]
            px = target.left() + target.width() * x / 1024
            py = target.top() + target.height() * (900 - y) / 1024
            radius = max(9.0, side * 0.026)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#E05945" if index == self.current_stroke else "#516168"))
            painter.drawEllipse(QRectF(px - radius, py - radius, radius * 2, radius * 2))
            painter.setPen(QColor("#FFFFFF"))
            painter.setFont(QFont("Times New Roman", max(8, int(radius))))
            painter.drawText(
                QRectF(px - radius, py - radius, radius * 2, radius * 2),
                Qt.AlignmentFlag.AlignCenter,
                str(index + 1),
            )


class StrokeOrderPanel(QFrame):
    def __init__(self, chinese_font_family: str) -> None:
        super().__init__()
        self.setObjectName("strokePanel")
        self.chinese_font_family = chinese_font_family
        self.characters: list[str] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("ПОРЯДОК ЧЕРТ")
        title.setObjectName("detailLabel")
        self.selector = QFrame()
        self.selector.setObjectName("strokeCharacterSelector")
        self.selector_layout = QHBoxLayout(self.selector)
        self.selector_layout.setContentsMargins(3, 3, 3, 3)
        self.selector_layout.setSpacing(2)
        self.selector_group = QButtonGroup(self.selector)
        self.selector_group.setExclusive(True)
        self.selector_group.idClicked.connect(self.select_character)
        self.selector_buttons: list[QPushButton] = []
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.selector)
        layout.addLayout(header)

        self.canvas = StrokeCanvas()
        self.canvas.setObjectName("strokeCanvas")
        layout.addWidget(self.canvas)

        self.status = QLabel()
        self.status.setObjectName("strokeStatus")
        layout.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignRight)

        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.animation_step)
        self.loop_delay_remaining_ms = 0
        self.setVisible(False)

    def set_chinese_font(self, family: str) -> None:
        self.chinese_font_family = family
        for button in self.selector_buttons:
            button.setFont(QFont(family, 18))

    def set_word(self, word: str) -> None:
        self.timer.stop()
        self.loop_delay_remaining_ms = 0
        self.characters = []
        for character in word:
            if character not in self.characters and load_character_data(character):
                self.characters.append(character)
        for button in self.selector_buttons:
            self.selector_group.removeButton(button)
            self.selector_layout.removeWidget(button)
            button.deleteLater()
        self.selector_buttons.clear()
        for index, character in enumerate(self.characters):
            button = QPushButton(character)
            button.setObjectName("strokeCharacterChip")
            button.setCheckable(True)
            button.setFont(QFont(self.chinese_font_family, 18))
            button.setToolTip(f"Показать порядок черт: {character}")
            self.selector_group.addButton(button, index)
            self.selector_layout.addWidget(button)
            self.selector_buttons.append(button)
        self.setVisible(bool(self.characters))
        if self.characters:
            self.selector_buttons[0].setChecked(True)
            self.select_character(0)

    def select_character(self, index: int) -> None:
        if 0 <= index < len(self.selector_buttons):
            self.selector_buttons[index].setChecked(True)
        if not (0 <= index < len(self.characters)):
            return
        self.timer.stop()
        character = self.characters[index]
        data = load_character_data(character)
        if data:
            self.canvas.set_character(character, data)
            self.update_status()
            self.start_animation()

    def update_status(self) -> None:
        count = len(self.canvas.data["strokes"]) if self.canvas.data else 0
        current = self.canvas.current_stroke + 1 if count else 0
        self.status.setText(f"Черта {current} из {count}")

    def start_animation(self) -> None:
        if not self.canvas.data:
            return
        self.loop_delay_remaining_ms = 0
        self.canvas.reset_animation()
        self.update_status()
        self.timer.start()

    def animation_step(self) -> None:
        if not self.canvas.data:
            self.timer.stop()
            return
        if self.loop_delay_remaining_ms > 0:
            self.loop_delay_remaining_ms -= self.timer.interval()
            if self.loop_delay_remaining_ms <= 0:
                self.canvas.reset_animation()
                self.update_status()
            return
        last = len(self.canvas.data["strokes"]) - 1
        duration = self.canvas.current_stroke_duration_ms()
        progress = self.canvas.stroke_progress + self.timer.interval() / duration
        if progress < 1.0:
            self.canvas.set_stroke_progress(progress)
            return
        self.canvas.set_stroke_progress(1.0)
        if self.canvas.current_stroke >= last:
            self.loop_delay_remaining_ms = 750
            return
        self.canvas.set_current_stroke(self.canvas.current_stroke + 1)
        self.canvas.set_stroke_progress(0.0)
        self.update_status()
