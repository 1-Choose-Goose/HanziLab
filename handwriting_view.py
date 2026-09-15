from __future__ import annotations

import math
from itertools import pairwise

from PySide6.QtCore import (
    QObject,
    QPointF,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from handwriting import handwriting_index, recognize_handwriting

RUSSIAN_FONT_FAMILY = "Times New Roman"


class HandwritingCanvas(QWidget):
    strokes_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.strokes: list[list[tuple[float, float]]] = []
        self.current_stroke: list[tuple[float, float]] | None = None
        self.setObjectName("handwritingCanvas")
        self.setMinimumSize(280, 280)
        self.setCursor(Qt.CursorShape.CrossCursor)

    def sizeHint(self) -> QSize:
        return QSize(310, 310)

    def drawing_rect(self) -> QRectF:
        side = max(40.0, min(self.width(), self.height()) - 24.0)
        return QRectF(
            (self.width() - side) / 2.0,
            (self.height() - side) / 2.0,
            side,
            side,
        )

    def normalized_point(self, point: QPointF, clamp: bool = False) -> tuple[float, float] | None:
        area = self.drawing_rect()
        if not clamp and not area.contains(point):
            return None
        x = (point.x() - area.left()) / area.width()
        y = (point.y() - area.top()) / area.height()
        return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        point = self.normalized_point(event.position())
        if point is None:
            return
        self.current_stroke = [point]
        self.grabMouse()
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self.current_stroke is None:
            return
        point = self.normalized_point(event.position(), clamp=True)
        if point is None:
            return
        if math.dist(point, self.current_stroke[-1]) >= 0.003:
            self.current_stroke.append(point)
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.current_stroke is None:
            return
        point = self.normalized_point(event.position(), clamp=True)
        if point is not None and math.dist(point, self.current_stroke[-1]) >= 0.001:
            self.current_stroke.append(point)
        self.strokes.append(self.current_stroke)
        self.current_stroke = None
        self.releaseMouse()
        self.update()
        self.strokes_changed.emit()

    def undo(self) -> None:
        if self.current_stroke is not None:
            self.current_stroke = None
            self.releaseMouse()
        elif self.strokes:
            self.strokes.pop()
        self.update()
        self.strokes_changed.emit()

    def clear(self) -> None:
        if self.current_stroke is not None:
            self.releaseMouse()
        self.current_stroke = None
        self.strokes.clear()
        self.update()
        self.strokes_changed.emit()

    def recognition_strokes(self) -> tuple[tuple[tuple[float, float], ...], ...]:
        return tuple(tuple(stroke) for stroke in self.strokes)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        area = self.drawing_rect()
        grid_pen = QPen(QColor("#DDE5E8"), 1, Qt.PenStyle.DashLine)
        painter.setPen(grid_pen)
        painter.drawRect(area)
        painter.drawLine(area.left(), area.center().y(), area.right(), area.center().y())
        painter.drawLine(area.center().x(), area.top(), area.center().x(), area.bottom())
        painter.drawLine(area.topLeft(), area.bottomRight())
        painter.drawLine(area.topRight(), area.bottomLeft())

        pen = QPen(
            QColor("#17242A"),
            max(6.0, area.width() * 0.022),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        visible_strokes = list(self.strokes)
        if self.current_stroke:
            visible_strokes.append(self.current_stroke)
        for stroke in visible_strokes:
            points = [
                QPointF(
                    area.left() + x * area.width(),
                    area.top() + y * area.height(),
                )
                for x, y in stroke
            ]
            if len(points) == 1:
                painter.drawPoint(points[0])
            else:
                for first, second in pairwise(points):
                    painter.drawLine(first, second)


class RecognitionSignals(QObject):
    finished = Signal(int, object, object)


class RecognitionTask(QRunnable):
    def __init__(self, generation: int, strokes, warm_only: bool = False) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.generation = generation
        self.strokes = strokes
        self.warm_only = warm_only
        self.signals = RecognitionSignals()

    @Slot()
    def run(self) -> None:
        try:
            if self.warm_only:
                handwriting_index()
                candidates = []
            else:
                candidates = recognize_handwriting(self.strokes, 10)
            error = None
        except Exception as exception:  # noqa: BLE001 - worker boundary
            candidates = []
            error = exception
        try:
            self.signals.finished.emit(self.generation, candidates, error)
        except RuntimeError:
            # The dialog can be closed while the one-time local index warmup
            # is still running; the deleted UI must not surface a worker error.
            pass


class HandwritingDialog(QDialog):
    def __init__(self, chinese_font_family: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("handwritingDialog")
        self.setWindowTitle("Рукописный ввод")
        self.setModal(True)
        self.setMinimumWidth(520)
        self.chinese_font_family = chinese_font_family
        self.generation = 0
        self.tasks: set[RecognitionTask] = set()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.recognition_timer = QTimer(self)
        self.recognition_timer.setSingleShot(True)
        self.recognition_timer.setInterval(160)
        self.recognition_timer.timeout.connect(self.start_recognition)
        self.undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self.undo_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.undo_shortcut.activated.connect(self.undo_last_action)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(14)
        title = QLabel("Нарисуйте иероглиф")
        title.setObjectName("handwritingTitle")
        subtitle = QLabel(
            "Проводите черты мышью по одной. Порядок черт повышает точность."
        )
        subtitle.setObjectName("handwritingSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(subtitle)

        composition_row = QHBoxLayout()
        self.composition = QLineEdit()
        self.composition.setObjectName("handwritingComposition")
        self.composition.setReadOnly(True)
        self.composition.setPlaceholderText("Выбранные иероглифы")
        self.composition.textChanged.connect(self.update_composition_font)
        self.update_composition_font("")
        remove_character = QPushButton("Удалить знак")
        remove_character.setObjectName("handwritingSecondaryButton")
        remove_character.clicked.connect(self.remove_composed_character)
        composition_row.addWidget(self.composition, 1)
        composition_row.addWidget(remove_character)
        root.addLayout(composition_row)

        self.canvas = HandwritingCanvas()
        self.canvas.strokes_changed.connect(self.schedule_recognition)
        root.addWidget(self.canvas, alignment=Qt.AlignmentFlag.AlignCenter)

        drawing_actions = QHBoxLayout()
        undo = QPushButton("← Отменить черту")
        undo.setObjectName("handwritingSecondaryButton")
        undo.clicked.connect(self.canvas.undo)
        clear = QPushButton("Очистить рисунок")
        clear.setObjectName("handwritingSecondaryButton")
        clear.clicked.connect(self.canvas.clear)
        drawing_actions.addWidget(undo)
        drawing_actions.addWidget(clear)
        drawing_actions.addStretch()
        root.addLayout(drawing_actions)

        self.status = QLabel("Нарисуйте первую черту")
        self.status.setObjectName("handwritingStatus")
        self.status.setFixedHeight(18)
        root.addWidget(self.status)
        self.candidate_container = QWidget()
        self.candidate_container.setObjectName("handwritingCandidateArea")
        self.candidate_container.setFixedHeight(108)
        candidate_grid = QGridLayout(self.candidate_container)
        candidate_grid.setContentsMargins(0, 0, 0, 0)
        candidate_grid.setHorizontalSpacing(8)
        candidate_grid.setVerticalSpacing(8)
        self.candidate_buttons: list[QPushButton] = []
        for index in range(10):
            button = QPushButton()
            button.setObjectName("handwritingCandidate")
            button.setFont(QFont(chinese_font_family, 22))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setVisible(False)
            button.clicked.connect(
                lambda _checked=False, selected=index: self.select_candidate(selected)
            )
            candidate_grid.addWidget(button, index // 5, index % 5)
            self.candidate_buttons.append(button)
        root.addWidget(self.candidate_container)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel = QPushButton("Отмена")
        cancel.setObjectName("handwritingCancelButton")
        cancel.clicked.connect(self.reject)
        self.insert_button = QPushButton("Вставить в поиск")
        self.insert_button.setObjectName("handwritingInsertButton")
        self.insert_button.setEnabled(False)
        self.insert_button.clicked.connect(self.accept)
        footer.addWidget(cancel)
        footer.addWidget(self.insert_button)
        root.addLayout(footer)

        # Start loading the local index while the user is drawing the first
        # character. Recognition remains entirely offline.
        warmup = RecognitionTask(-1, (), warm_only=True)
        self.tasks.add(warmup)
        warmup.signals.finished.connect(
            lambda *_arguments, worker=warmup: self.tasks.discard(worker)
        )
        self.pool.start(warmup)

    def selected_text(self) -> str:
        return self.composition.text()

    def undo_last_action(self) -> None:
        """Undo the latest stroke or, when the canvas is empty, selected Hanzi."""
        if self.canvas.current_stroke is not None or self.canvas.strokes:
            self.canvas.undo()
        elif self.composition.text():
            self.remove_composed_character()

    def update_composition_font(self, text: str) -> None:
        """Keep the Russian hint readable and selected Hanzi in the CJK font."""
        if text:
            self.composition.setFont(QFont(self.chinese_font_family, 20))
        else:
            self.composition.setFont(QFont(RUSSIAN_FONT_FAMILY, 10))

    def schedule_recognition(self) -> None:
        self.generation += 1
        self.cancel_pending_tasks()
        self.show_candidates([])
        if not self.canvas.strokes:
            self.recognition_timer.stop()
            self.status.setText("Нарисуйте первую черту")
            return
        self.status.setText("Распознаю…")
        self.recognition_timer.start()

    def start_recognition(self) -> None:
        if not self.canvas.strokes:
            return
        task = RecognitionTask(self.generation, self.canvas.recognition_strokes())
        self.tasks.add(task)
        task.signals.finished.connect(self.finish_recognition)
        task.signals.finished.connect(
            lambda *_arguments, worker=task: self.tasks.discard(worker)
        )
        self.pool.start(task)

    def finish_recognition(self, generation: int, candidates: object, error: object) -> None:
        if generation != self.generation:
            return
        if error is not None:
            self.status.setText("Не удалось запустить распознавание")
            self.show_candidates([])
            return
        values = list(candidates or [])
        self.status.setText(
            "Выберите иероглиф"
            if values
            else "Совпадений не найдено"
        )
        self.show_candidates(values)

    def show_candidates(self, candidates: list[str]) -> None:
        for index, button in enumerate(self.candidate_buttons):
            if index < len(candidates):
                button.setText(candidates[index])
                button.setVisible(True)
            else:
                button.setText("")
                button.setVisible(False)

    def select_candidate(self, index: int) -> None:
        if not (0 <= index < len(self.candidate_buttons)):
            return
        character = self.candidate_buttons[index].text()
        if not character:
            return
        self.composition.setText(self.composition.text() + character)
        self.insert_button.setEnabled(True)
        self.canvas.clear()
        self.status.setText("Знак добавлен. Можно нарисовать следующий.")

    def remove_composed_character(self) -> None:
        self.composition.setText(self.composition.text()[:-1])
        self.insert_button.setEnabled(bool(self.composition.text()))

    def cancel_pending_tasks(self) -> None:
        # Removed runnables never emit finished, so release our references here.
        # Auto-delete is disabled to make tryTake safe even as a task finishes.
        for task in tuple(self.tasks):
            if self.pool.tryTake(task):
                self.tasks.discard(task)

    def done(self, result: int) -> None:
        self.generation += 1
        self.recognition_timer.stop()
        self.cancel_pending_tasks()
        super().done(result)
