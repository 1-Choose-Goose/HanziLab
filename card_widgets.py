from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QPushButton


class RevealPinyinButton(QPushButton):
    """Показывает пиньинь только после нажатия, до этого рисует штриховку."""

    def __init__(self) -> None:
        super().__init__()
        self.pinyin = ""
        self.revealed = False
        self.setObjectName("revealPinyin")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumSize(250, 50)
        self.clicked.connect(self.reveal)

    def set_pinyin(self, pinyin: str) -> None:
        self.pinyin = pinyin
        self.revealed = False
        self.setText("")
        self.update()

    def reveal(self) -> None:
        if self.revealed:
            return
        self.revealed = True
        self.setText(self.pinyin)
        self.update()

    def paintEvent(self, event) -> None:
        if self.revealed:
            super().paintEvent(event)
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        area = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(QPen(QColor("#D4DBDE"), 1))
        painter.setBrush(QColor("#EEF2F3"))
        painter.drawRoundedRect(area, 9, 9)

        painter.save()
        painter.setClipPath(self._rounded_clip(area))
        painter.setPen(QPen(QColor("#C3CDD1"), 2))
        spacing = 10
        start = -self.height()
        while start < self.width():
            painter.drawLine(start, self.height(), start + self.height(), 0)
            start += spacing
        painter.restore()

    @staticmethod
    def _rounded_clip(area: QRectF):
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        path.addRoundedRect(area, 9, 9)
        return path
