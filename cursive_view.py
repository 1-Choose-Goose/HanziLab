from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cursive import CursiveSample, search_cursive
from cursive_copybook import generate_cursive_copybook


class CursivePage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("cursivePage")
        self.sample: CursiveSample | None = None
        self.variants: tuple[CursiveSample, ...] = ()
        self.groups = {}
        self.pdf_path: Path | None = None
        self.relayout_timer = QTimer(self)
        self.relayout_timer.setSingleShot(True)
        self.relayout_timer.timeout.connect(self.fill_table)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 24)
        layout.setSpacing(12)
        title = QLabel("Скоропись")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Изучайте быстрое написание иероглифов и создавайте прописи для практики."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Найти иероглиф, например 学")
        self.search.setClearButtonEnabled(True)
        self.search.setAccessibleName("Поиск иероглифа")
        self.search.setObjectName("cursiveSearch")
        self.search.setMinimumHeight(42)
        filters.addWidget(self.search, 1)
        layout.addLayout(filters)
        self.count = QLabel()
        self.count.setObjectName("pageSubtitle")
        self.count.setWordWrap(True)
        layout.addWidget(self.count)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(splitter, 1)
        self.table = QTableWidget()
        self.table.setObjectName("cursiveTable")
        self.table.setAccessibleName("Таблица иероглифов")
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().hide()
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setDefaultSectionSize(55)
        self.table.setFont(QFont("KaiTi", 27))
        self.table.viewport().installEventFilter(self)
        splitter.addWidget(self.table)
        panel = QFrame()
        panel.setObjectName("cursiveDetail")
        panel.setMinimumWidth(260)
        detail = QVBoxLayout(panel)
        detail.setContentsMargins(18, 20, 18, 20)
        detail.setSpacing(12)
        self.heading = QLabel("Выберите знак")
        self.heading.setObjectName("cursiveHeading")
        self.heading.setFont(QFont("Times New Roman", 16))
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        detail.addWidget(self.heading)
        self.preview = QLabel("Нажмите на ячейку таблицы")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(205)
        self.preview.setObjectName("cursivePreview")
        self.preview.setWordWrap(True)
        detail.addWidget(self.preview)
        self.variant = QComboBox()
        self.variant.setAccessibleName("Вариант написания")
        self.variant.setObjectName("cursiveVariant")
        arrow_path = (
            Path(__file__).resolve().parent / "assets/icons/dropdown-triangle.svg"
        ).as_posix()
        self.variant.setStyleSheet(
            f'QComboBox#cursiveVariant::down-arrow {{ image: url("{arrow_path}"); '
            "width: 8px; height: 5px; }"
        )
        self.variant.setEnabled(False)
        detail.addWidget(self.variant)
        self.save_button = QPushButton("Создать пропись")
        self.save_button.setObjectName("cursiveSaveButton")
        self.save_button.clicked.connect(self.save_pdf)
        detail.addWidget(self.save_button)
        self.open_button = QPushButton("Открыть PDF")
        self.open_button.setObjectName("cursiveOpenButton")
        self.open_button.hide()
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self.open_pdf)
        detail.addWidget(self.open_button)
        detail.addStretch()
        detail_scroll = QScrollArea()
        detail_scroll.setObjectName("cursiveDetailScroll")
        detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        detail_scroll.setWidgetResizable(True)
        detail_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        detail_scroll.setMinimumWidth(280)
        detail_scroll.setMaximumWidth(370)
        detail_scroll.setWidget(panel)
        splitter.addWidget(detail_scroll)
        splitter.setObjectName("resultSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.setStretchFactor(0, 1)
        splitter.setSizes([620, 280])
        self.save_button.setEnabled(False)
        try:
            self.refresh()
        except (OSError, ValueError, TypeError) as error:
            self.count.setText(f"Не удалось загрузить каталог скорописи: {error}")
            self.search.setEnabled(False)
        self.search.textChanged.connect(self.refresh)
        self.table.currentItemChanged.connect(self.select_item)
        self.variant.currentIndexChanged.connect(self.select_variant)

    def refresh(self, *_):
        result = search_cursive(self.search.text())
        self.groups = result.groups
        self.fill_table()
        if result.components:
            descriptions = []
            for match in result.components:
                parts = " + ".join(match.parts)
                suffix = " · найдены не все части" if match.unresolved else ""
                descriptions.append(f"{match.character} → {parts}{suffix}")
            if result.missing:
                descriptions.append("Нет образцов: " + " ".join(result.missing))
            self.count.setText("\n".join(descriptions))
        elif self.groups:
            text = (
                f"{len(self.groups)} знаков · нажмите на знак, чтобы увидеть скоропись"
            )
            if result.missing:
                text += " · нет образцов: " + " ".join(result.missing)
            self.count.setText(text)
        else:
            self.count.setText(
                "Нет образца и доступных частей. Попробуйте другой иероглиф."
            )
        if self.sample and self.sample.character not in self.groups:
            self.clear_selection()
        elif self.sample:
            self.show_character(self.sample.character)
        if result.components and not self.sample:
            self.table.setCurrentCell(0, 0)

    def fill_table(self):
        selected = self.sample.character if self.sample else None
        columns = max(4, self.table.viewport().width() // 54)
        self.table.blockSignals(True)
        self.table.clearContents()
        self.table.setColumnCount(columns)
        self.table.setRowCount((len(self.groups) + columns - 1) // columns)
        for i, character in enumerate(self.groups):
            item = QTableWidgetItem(character)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item.setToolTip(f"{character} · образцов: {len(self.groups[character])}")
            self.table.setItem(i // columns, i % columns, item)
            if character == selected:
                self.table.setCurrentItem(item)
        self.table.blockSignals(False)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "table") and self.groups:
            self.fill_table()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Resize and self.groups:
            desired = max(4, self.table.viewport().width() // 54)
            if desired != self.table.columnCount():
                self.relayout_timer.start(0)
        return super().eventFilter(watched, event)

    def select_item(self, current, previous=None):
        if current:
            self.show_character(current.text())

    def show_character(self, character):
        previous = self.sample.id if self.sample else None
        self.variants = self.groups[character]
        self.variant.blockSignals(True)
        self.variant.clear()
        for i, sample in enumerate(self.variants):
            self.variant.addItem(f"Вариант {i + 1}", sample.id)
        index = next((i for i, s in enumerate(self.variants) if s.id == previous), 0)
        self.variant.setCurrentIndex(index)
        self.variant.blockSignals(False)
        self.select_variant(index)

    def clear_selection(self):
        self.sample = None
        self.variants = ()
        self.variant.clear()
        self.heading.setFont(QFont("Times New Roman", 16))
        self.heading.setText("Выберите знак")
        self.preview.setText("Нажмите на ячейку таблицы")
        self.variant.setEnabled(False)
        self.open_button.hide()
        for button in (self.save_button, self.open_button):
            button.setEnabled(False)

    def select_variant(self, index):
        if not 0 <= index < len(self.variants):
            return
        self.sample = self.variants[index]
        self.heading.setFont(QFont("KaiTi", 30))
        self.heading.setText(self.sample.character)
        self.variant.setEnabled(len(self.variants) > 1)
        pixmap = QPixmap(str(self.sample.image_path))
        self.preview.setPixmap(
            pixmap.scaled(
                QSize(190, 190),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self.save_button.setEnabled(not pixmap.isNull())
        self.open_button.hide()
        self.pdf_path = None
        self.open_button.setEnabled(False)

    def save_pdf(self):
        if not self.sample:
            return
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить пропись",
            f"{self.sample.character}_скоропись.pdf",
            "PDF (*.pdf)",
        )
        if not filename:
            return
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        try:
            self.pdf_path = generate_cursive_copybook(self.sample, Path(filename))
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Не удалось создать пропись", str(error))
            return
        self.open_button.setEnabled(True)
        self.open_button.show()
        self.open_pdf()

    def open_pdf(self):
        if self.pdf_path and not QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(self.pdf_path.resolve()))
        ):
            QMessageBox.information(
                self, "Пропись сохранена", str(self.pdf_path.resolve())
            )
