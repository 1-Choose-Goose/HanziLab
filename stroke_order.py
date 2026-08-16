from __future__ import annotations

import html
import json
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
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
    path = STROKE_DATA_DIR / f"{character}.json"
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("strokes"):
        return None
    return data


class StrokeCanvas(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.character = ""
        self.data: dict | None = None
        self.current_stroke = 0
        self.setMinimumHeight(300)

    def set_character(self, character: str, data: dict) -> None:
        self.character = character
        self.data = data
        self.current_stroke = 0
        self.update()

    def set_current_stroke(self, index: int) -> None:
        if not self.data:
            return
        self.current_stroke = min(max(index, 0), len(self.data["strokes"]) - 1)
        self.update()

    @staticmethod
    def stroke_svg(path: str, color: str) -> QByteArray:
        document = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024">'
            '<g transform="translate(0,900) scale(1,-1)">'
            f'<path d="{html.escape(path, quote=True)}" fill="{color}"/>'
            "</g></svg>"
        )
        return QByteArray(document.encode("utf-8"))

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
            elif index == self.current_stroke:
                color = "#E05945"
            else:
                color = "#E5EAEC"
            QSvgRenderer(self.stroke_svg(path, color)).render(painter, target)

        medians = self.data.get("medians", [])
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

        controls = QHBoxLayout()
        self.previous_button = QPushButton("← Назад")
        self.previous_button.setObjectName("strokeButton")
        self.play_button = QPushButton("▶ Показать")
        self.play_button.setObjectName("strokePlayButton")
        self.next_button = QPushButton("Следующая →")
        self.next_button.setObjectName("strokeButton")
        self.status = QLabel()
        self.status.setObjectName("strokeStatus")
        self.previous_button.clicked.connect(self.previous_stroke)
        self.next_button.clicked.connect(self.next_stroke)
        self.play_button.clicked.connect(self.toggle_animation)
        controls.addWidget(self.previous_button)
        controls.addWidget(self.play_button)
        controls.addWidget(self.next_button)
        controls.addStretch()
        controls.addWidget(self.status)
        layout.addLayout(controls)

        self.timer = QTimer(self)
        self.timer.setInterval(650)
        self.timer.timeout.connect(self.animation_step)
        self.setVisible(False)

    def set_chinese_font(self, family: str) -> None:
        self.chinese_font_family = family
        for button in self.selector_buttons:
            button.setFont(QFont(family, 18))

    def set_word(self, word: str) -> None:
        self.timer.stop()
        self.play_button.setText("▶ Показать")
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
        self.play_button.setText("▶ Показать")
        character = self.characters[index]
        data = load_character_data(character)
        if data:
            self.canvas.set_character(character, data)
            self.update_status()

    def update_status(self) -> None:
        count = len(self.canvas.data["strokes"]) if self.canvas.data else 0
        current = self.canvas.current_stroke + 1 if count else 0
        self.status.setText(f"Черта {current} из {count}")
        self.previous_button.setEnabled(current > 1)
        self.next_button.setEnabled(current < count)

    def previous_stroke(self) -> None:
        self.timer.stop()
        self.play_button.setText("▶ Показать")
        self.canvas.set_current_stroke(self.canvas.current_stroke - 1)
        self.update_status()

    def next_stroke(self) -> None:
        self.timer.stop()
        self.play_button.setText("▶ Показать")
        self.canvas.set_current_stroke(self.canvas.current_stroke + 1)
        self.update_status()

    def toggle_animation(self) -> None:
        if self.timer.isActive():
            self.timer.stop()
            self.play_button.setText("▶ Продолжить")
            return
        if self.play_button.text() != "▶ Продолжить":
            self.canvas.set_current_stroke(0)
            self.update_status()
        self.play_button.setText("Ⅱ Пауза")
        self.timer.start()

    def animation_step(self) -> None:
        if not self.canvas.data:
            self.timer.stop()
            return
        last = len(self.canvas.data["strokes"]) - 1
        if self.canvas.current_stroke >= last:
            self.timer.stop()
            self.play_button.setText("↻ Повторить")
            return
        self.canvas.set_current_stroke(self.canvas.current_stroke + 1)
        self.update_status()
