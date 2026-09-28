"""Print the selected scan sample, without substituting a cursive font."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from PySide6.QtCore import QMarginsF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPageSize, QPainter, QPdfWriter, QPen

from cursive import CursiveSample

WIDTH, HEIGHT = 595.276, 841.89
COLUMNS, ROWS = 12, 14
CELL = (WIDTH - 70) / COLUMNS


def page_font(family: str, size: int) -> QFont:
    font = QFont(family)
    # Page geometry is in points; QFont point sizes would apply device DPI again.
    font.setPixelSize(size)
    return font


def draw_sample(
    painter: QPainter, image: QImage, rect: QRectF, opacity: float = 1
) -> None:
    size = image.size().scaled(
        round(rect.width()), round(rect.height()), Qt.AspectRatioMode.KeepAspectRatio
    )
    target = QRectF(
        rect.center().x() - size.width() / 2,
        rect.center().y() - size.height() / 2,
        size.width(),
        size.height(),
    )
    painter.save()
    painter.setOpacity(opacity)
    painter.drawImage(target, image)
    painter.restore()


def paint_copybook(painter: QPainter, sample: CursiveSample, image: QImage) -> None:
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setPen(QColor("#172126"))
    painter.setFont(page_font("Times New Roman", 20))
    painter.drawText(QRectF(35, 26, 410, 30), "Скоропись · пропись")
    painter.setFont(page_font("KaiTi", 22))
    painter.drawText(
        QRectF(35, 62, 55, 35), Qt.AlignmentFlag.AlignCenter, sample.character
    )
    painter.setFont(page_font("Times New Roman", 10))
    painter.drawText(QRectF(95, 63, 330, 16), "Практика скорописного написания")
    painter.drawText(QRectF(95, 81, 330, 16), "Сохраняйте форму и пропорции иероглифа.")
    draw_sample(painter, image, QRectF(466, 29, 78, 78))
    painter.drawText(
        QRectF(35, 115, 530, 18), "Образец → обводка → самостоятельное письмо"
    )
    for row in range(ROWS):
        for col in range(COLUMNS):
            rect = QRectF(35 + col * CELL, 143 + row * CELL, CELL, CELL)
            # The scan has a white background: draw it first, guides afterwards.
            if col == 0 or col < (8 if row < 4 else 5 if row < 9 else 2):
                draw_sample(
                    painter, image, rect.adjusted(5, 5, -5, -5), 1 if col == 0 else 0.20
                )
            painter.setPen(QPen(QColor("#D68B79"), 0.45))
            painter.drawRect(rect)
            # Inner guides should remain visible without competing with handwriting.
            guide = QPen(QColor("#F0E6E3"), 0.25, Qt.PenStyle.DashLine)
            painter.setPen(guide)
            painter.drawLine(
                rect.center().x(), rect.top(), rect.center().x(), rect.bottom()
            )
            painter.drawLine(
                rect.left(), rect.center().y(), rect.right(), rect.center().y()
            )
    painter.setFont(page_font("Times New Roman", 9))
    painter.setPen(QColor("#53636A"))
    painter.drawText(
        QRectF(35, 777, 525, 30), Qt.TextFlag.TextWordWrap, "HanziLab · Скоропись"
    )


def generate_cursive_copybook(sample: CursiveSample, output: Path) -> Path:
    image = QImage.fromData(sample.image_data)
    if image.isNull():
        raise ValueError("Не удалось загрузить образец написания.")
    output = Path(output)
    # Do not leave an incomplete file or overwrite a valid PDF if painting fails.
    fd, temporary = tempfile.mkstemp(suffix=".pdf", dir=output.parent)
    os.close(fd)
    try:
        writer = QPdfWriter(temporary)
        writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
        writer.setPageMargins(QMarginsF(0, 0, 0, 0))
        writer.setResolution(144)
        writer.setTitle(f"Скоропись · {sample.character} · HanziLab")
        writer.setCreator("HanziLab")
        painter = QPainter(writer)
        if not painter.isActive():
            raise OSError("Не удалось создать PDF.")
        try:
            painter.scale(writer.width() / WIDTH, writer.height() / HEIGHT)
            paint_copybook(painter, sample, image)
        finally:
            painter.end()
        del writer
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output
