from __future__ import annotations

import ctypes
import os
import re
import shutil
import sys
import unicodedata
from functools import cache
from pathlib import Path

from PySide6.QtCore import (
    QEvent,
    QObject,
    QRunnable,
    QSettings,
    QSize,
    Qt,
    QThreadPool,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

import database
import dictionary_remote
import updates
from app_paths import RESOURCE_ROOT
from copybook_pdf import (
    CopybookError,
    CopybookStyle,
    generate_assembled_copybook,
    generate_hanzi_copybook,
    hanzi_sequence,
    suggested_copybook_name,
)
from cursive_view import CursivePage
from database import DB_PATH, get_examples, get_stats, search_entries, warm_search_index
from handwriting_view import HandwritingDialog
from scripts.text_normalization import CJK_RE
from scripts.text_normalization import normalize_pinyin as _pinyin_base
from stroke_order import StrokeOrderPanel
from study_database import StudyRepository
from study_view import NoWheelComboBox, NoWheelSpinBox, StudyPage
from text_formatting import (
    format_example_blocks,
    normalize_display_text,
    valid_pinyin_syllables,
)
from version import APP_VERSION

APP_TITLE = "HanziLab — китайско-русский словарь"
FONT_DIR = RESOURCE_ROOT / "assets" / "fonts"
ICON_DIR = RESOURCE_ROOT / "assets" / "icons"
APP_ICON_PNG = ICON_DIR / (
    "hanzilab-macos.png" if sys.platform == "darwin" else "hanzilab.png"
)
APP_ICON_ICO = ICON_DIR / "hanzilab.ico"
APP_ICON_FILE = APP_ICON_ICO if sys.platform == "win32" else APP_ICON_PNG
SPINBOX_PLUS_ICON = ICON_DIR / "spinbox-plus.svg"
SPINBOX_MINUS_ICON = ICON_DIR / "spinbox-minus.svg"
SIDEBAR_DROPDOWN_ICON = ICON_DIR / "dropdown-triangle-light.svg"
KAITI_FAMILY = "KaiTi"
XINGSHU_FAMILY = "QXyingbixing"
INPUT_KAITI_FAMILY = "HanziLab KaiTi CJK"
RUSSIAN_FONT_FAMILY = "Times New Roman"
DEVELOPER_NAME = "Choose_Goose"
VK_URL = "https://vk.ru/kamereka"
TELEGRAM_URL = "https://t.me/choose_o_goose"
TELEGRAM_CHANNEL_URL = "https://t.me/yi_bi_yi_hua"
APP_USER_MODEL_ID = "HanziLab.Desktop"


def localize_message_box_details(dialog: QMessageBox) -> None:
    """Keep Qt's automatically created details button in Russian."""
    for button in dialog.findChildren(QPushButton):
        normalized = button.text().replace("&", "").strip().lower()
        if "details" not in normalized and "подробн" not in normalized:
            continue
        hidden = "hide" in normalized or "скрыт" in normalized
        button.setText("Скрыть подробности" if hidden else "Подробнее…")
        button.setMinimumWidth(170 if hidden else 120)
        if not button.property("hanzilabDetailsLocalized"):
            button.setProperty("hanzilabDetailsLocalized", True)
            button.clicked.connect(
                lambda _checked=False, box=dialog: QTimer.singleShot(
                    0, lambda: localize_message_box_details(box)
                )
            )


def exec_message_box(dialog: QMessageBox) -> int:
    """Open a styled message box after localizing Qt-owned controls."""
    localize_message_box_details(dialog)
    QTimer.singleShot(0, lambda: localize_message_box_details(dialog))
    return dialog.exec()


def center_combo_box_text(combo: QComboBox) -> None:
    combo.setEditable(True)
    editor = combo.lineEdit()
    if editor is not None:
        editor.setReadOnly(True)
        editor.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        editor.setAlignment(Qt.AlignmentFlag.AlignCenter)
    for index in range(combo.count()):
        combo.setItemData(
            index,
            Qt.AlignmentFlag.AlignCenter,
            Qt.ItemDataRole.TextAlignmentRole,
        )


class BackgroundTaskSignals(QObject):
    finished = Signal(int, object, object)


class WindowCenteringFilter(QObject):
    """Centers every application window on the screen where it is opened."""

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Show and isinstance(
            watched, (QMainWindow, QDialog)
        ):
            QTimer.singleShot(0, lambda window=watched: self.center(window))
        return super().eventFilter(watched, event)

    @staticmethod
    def center(window: QWidget) -> None:
        try:
            parent = window.parentWidget()
            screen = parent.screen() if parent is not None else window.screen()
            if screen is None:
                screen = QApplication.primaryScreen()
            if screen is None:
                return
            frame = window.frameGeometry()
            frame.moveCenter(screen.availableGeometry().center())
            window.move(frame.topLeft())
        except RuntimeError:
            # A short-lived message box may be destroyed before the queued call.
            pass


class BackgroundTask(QRunnable):
    """Выполняет тяжёлое чтение SQLite, не блокируя Qt-интерфейс."""

    def __init__(
        self,
        generation: int,
        function,
        *arguments,
        category: str = "general",
    ) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.generation = generation
        self.function = function
        self.arguments = arguments
        self.category = category
        self.cancelled = False
        self.signals = BackgroundTaskSignals()

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        if self.cancelled:
            self.signals.finished.emit(
                self.generation, None, RuntimeError("background task cancelled")
            )
            return
        try:
            result = self.function(*self.arguments)
            error = None
        except Exception as exception:  # noqa: BLE001 - граница фоновой задачи.
            result = None
            error = exception
        if self.cancelled:
            result = None
            error = RuntimeError("background task cancelled")
        self.signals.finished.emit(self.generation, result, error)


class DictionaryDownloadSignals(QObject):
    progress = Signal(int)
    finished = Signal(object, object)


class DictionaryDownloadTask(QRunnable):
    category = "dictionary-download"

    def __init__(self, destination: Path) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.destination = destination
        self.cancelled = False
        self.signals = DictionaryDownloadSignals()

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            result = dictionary_remote.download_dictionary(
                self.destination,
                progress=lambda downloaded, total: self.signals.progress.emit(
                    min(100, round(downloaded * 100 / total))
                ),
                cancelled=lambda: self.cancelled,
            )
            error = None
        except Exception as exception:  # noqa: BLE001 - boundary of worker task.
            result = None
            error = exception
        self.signals.finished.emit(result, error)


class UpdateTaskSignals(QObject):
    progress = Signal(int)
    finished = Signal(object, object)


class UpdateCheckTask(QRunnable):
    def __init__(self) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.cancelled = False
        self.signals = UpdateTaskSignals()

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            result = updates.check_for_update()
            error = None
        except Exception as exception:  # noqa: BLE001 - worker boundary
            result = None
            error = exception
        if not self.cancelled:
            self.signals.finished.emit(result, error)


class UpdateDownloadTask(QRunnable):
    def __init__(self, update: updates.UpdateInfo, destination: Path) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.update = update
        self.destination = destination
        self.cancelled = False
        self.signals = UpdateTaskSignals()

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        try:
            result = updates.download_update(
                self.update,
                self.destination,
                progress=lambda downloaded, total: self.signals.progress.emit(
                    min(100, round(downloaded * 100 / total))
                ),
                cancelled=lambda: self.cancelled,
            )
            error = None
        except Exception as exception:  # noqa: BLE001 - worker boundary
            result = None
            error = exception
            shutil.rmtree(self.destination.parent, ignore_errors=True)
        if self.cancelled and result is not None:
            shutil.rmtree(self.destination.parent, ignore_errors=True)
            result = None
            error = updates.UpdateError("Загрузка обновления отменена")
        self.signals.finished.emit(result, error)


def _split_compact_pinyin(value: str, syllable_count: int) -> list[str] | None:
    compact = _pinyin_base(value)
    valid = valid_pinyin_syllables()

    @cache
    def split(position: int, remaining: int) -> tuple[str, ...] | None:
        if position == len(compact):
            return () if remaining == 0 else None
        if remaining <= 0:
            return None
        for end in range(min(len(compact), position + 7), position, -1):
            candidate = compact[position:end]
            is_erhua = candidate.endswith("r") and candidate[:-1] in valid
            if candidate not in valid and not is_erhua:
                continue
            tail = split(end, remaining - 1)
            if tail is not None:
                return (candidate, *tail)
        return None

    bases = split(0, syllable_count)
    return list(bases) if bases is not None else None


def readable_pinyin(hanzi: str, pinyin: str) -> str:
    """Разделяет слитный пиньинь, не подменяя сохранённые чтения и тоны."""
    hanzi = normalize_display_text(hanzi, preserve_line_breaks=False)
    value = normalize_display_text(pinyin, preserve_line_breaks=False)
    hanzi_characters = CJK_RE.findall(hanzi)
    if " " in value or len(hanzi_characters) < 2:
        return value
    syllable_count = len(hanzi_characters)
    if hanzi_characters[-1] == "儿" and _pinyin_base(value).endswith("r"):
        syllable_count -= 1
    generated_bases = _split_compact_pinyin(value, syllable_count) or []
    if not generated_bases:
        return value

    # Границы берём из лёгкого локального списка слогов, а текст
    # вырезаем из сохранённого чтения, чтобы не терять словарные тоны.
    source = unicodedata.normalize("NFC", value)
    position = 0
    separated: list[str] = []
    for generated_base in generated_bases:
        while position < len(source) and not _pinyin_base(source[position]):
            position += 1
        start = position
        consumed = 0
        while position < len(source) and consumed < len(generated_base):
            length = 2 if source[position:position + 2].lower() == "u:" else 1
            consumed += len(_pinyin_base(source[position:position + length]))
            position += length
        while position < len(source) and (
            source[position].isdigit() or unicodedata.combining(source[position])
        ):
            position += 1
        syllable = re.sub(r"[\s'’·-]+", "", source[start:position])
        if not syllable or consumed != len(generated_base):
            return value
        separated.append(syllable)
    return (
        " ".join(separated)
        if _pinyin_base("".join(separated)) == _pinyin_base(value)
        else value
    )


def load_chinese_fonts() -> None:
    for filename in ("KaiTi.ttf", "xingshu.ttf", "HanziLabKaiTiCJK.ttf"):
        path = FONT_DIR / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


class ElidedLabel(QLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.full_text = text
        self.setMinimumWidth(0)
        self.setToolTip(text)

    def update_elision(self) -> None:
        available = max(0, self.contentsRect().width())
        QLabel.setText(
            self,
            self.fontMetrics().elidedText(
                self.full_text, Qt.TextElideMode.ElideRight, available
            ),
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.update_elision()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self.update_elision()


class ResultRow(QWidget):
    def __init__(self, result: dict, hanzi_font_family: str = KAITI_FAMILY) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 10, 18, 10)
        layout.setSpacing(16)

        hanzi_text = normalize_display_text(
            result["hanzi"], preserve_line_breaks=False
        )
        pinyin_source = normalize_display_text(
            result["pinyin"], preserve_line_breaks=False
        )
        hanzi = QLabel(hanzi_text)
        hanzi.setTextFormat(Qt.TextFormat.PlainText)
        hanzi.setObjectName("resultHanzi")
        character_count = len(CJK_RE.findall(hanzi_text))
        hanzi_size = 16 if character_count <= 4 else 14 if character_count <= 6 else 12
        hanzi.setFont(QFont(hanzi_font_family, hanzi_size, QFont.Weight.DemiBold))
        hanzi.setFixedWidth(120)
        hanzi.setMaximumHeight(58)
        hanzi.setWordWrap(True)
        hanzi.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        hanzi.setToolTip(hanzi_text)

        text = QVBoxLayout()
        text.setSpacing(4)
        pinyin_text = readable_pinyin(hanzi_text, pinyin_source)
        pinyin = ElidedLabel(pinyin_text)
        pinyin.setObjectName("resultPinyin")
        translation_text = normalize_display_text(
            result["translation"], preserve_line_breaks=False
        )
        translation_words = translation_text.split()
        translation_preview = " ".join(translation_words[:3])
        if len(translation_words) > 3:
            translation_preview += "…"
        translation = QLabel(translation_preview)
        translation.setTextFormat(Qt.TextFormat.PlainText)
        translation.setObjectName("resultTranslation")
        translation.setFont(QFont(RUSSIAN_FONT_FAMILY, 11))
        translation.setToolTip(translation_text)
        translation.setWordWrap(True)
        translation.setMaximumHeight(36)
        text.addWidget(pinyin)
        text.addWidget(translation)
        text.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        layout.addWidget(hanzi)
        layout.addLayout(text, 1)


class CopybookCollectionDialog(QDialog):
    def __init__(self, hanzi_font_family: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.hanzi_font_family = hanzi_font_family
        self.setObjectName("copybookCollectionDialog")
        self.setWindowTitle("Собрать прописи")
        self.setWindowIcon(QIcon(str(APP_ICON_FILE)))
        self.setFont(QFont(RUSSIAN_FONT_FAMILY, 10))
        self.resize(850, 610)
        self.setMinimumSize(700, 520)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 26, 28, 24)
        layout.setSpacing(14)

        title = QLabel("Собрать прописи")
        title.setObjectName("copybookCollectionTitle")
        subtitle = QLabel(
            "Найдите иероглифы или слова, добавьте их в список и создайте один PDF."
        )
        subtitle.setObjectName("copybookCollectionSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        search_shell = QFrame()
        search_shell.setObjectName("collectionSearchShell")
        search_layout = QHBoxLayout(search_shell)
        search_layout.setContentsMargins(14, 4, 7, 4)
        self.search = QLineEdit()
        self.search.setObjectName("collectionSearch")
        self.search.setPlaceholderText("Введите 学习, xuéxí или «учиться»")
        self.search.setClearButtonEnabled(True)
        search_font = QFont()
        search_font.setFamilies([INPUT_KAITI_FAMILY, RUSSIAN_FONT_FAMILY])
        search_font.setPointSize(13)
        self.search.setFont(search_font)
        self.search.setMinimumHeight(52)
        self.search.returnPressed.connect(self.run_search)
        self.search.textChanged.connect(self.schedule_search)
        search_layout.addWidget(self.search, 1)
        layout.addWidget(search_shell)

        self.search_generation = 0
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(280)
        self.search_timer.timeout.connect(self.run_search)
        self.finished.connect(self.stop_search)

        columns = QSplitter(Qt.Orientation.Horizontal)
        columns.setObjectName("collectionSplitter")
        columns.setChildrenCollapsible(False)

        results_panel = QFrame()
        results_panel.setObjectName("collectionPanel")
        results_layout = QVBoxLayout(results_panel)
        results_layout.setContentsMargins(14, 14, 14, 14)
        results_label = QLabel("РЕЗУЛЬТАТЫ ПОИСКА")
        results_label.setObjectName("collectionSectionLabel")
        self.search_status = QLabel("Введите запрос")
        self.search_status.setObjectName("collectionHint")
        self.results = QListWidget()
        self.results.setObjectName("collectionList")
        self.results.itemDoubleClicked.connect(self.add_selected_result)
        add_button = QPushButton("Добавить в сборник →")
        add_button.setObjectName("collectionSecondaryButton")
        add_button.setCursor(Qt.CursorShape.PointingHandCursor)
        add_button.clicked.connect(self.add_selected_result)
        results_layout.addWidget(results_label)
        results_layout.addWidget(self.search_status)
        results_layout.addWidget(self.results, 1)
        results_layout.addWidget(add_button)
        columns.addWidget(results_panel)

        selected_panel = QFrame()
        selected_panel.setObjectName("collectionPanel")
        selected_layout = QVBoxLayout(selected_panel)
        selected_layout.setContentsMargins(14, 14, 14, 14)
        selected_label = QLabel("ОБЩИЙ СПИСОК")
        selected_label.setObjectName("collectionSectionLabel")
        self.selected_status = QLabel("Пока ничего не добавлено")
        self.selected_status.setObjectName("collectionHint")
        self.selected = QListWidget()
        self.selected.setObjectName("collectionList")
        self.selected.itemDoubleClicked.connect(self.remove_selected_item)
        remove_button = QPushButton("Удалить из списка")
        remove_button.setObjectName("collectionSecondaryButton")
        remove_button.setCursor(Qt.CursorShape.PointingHandCursor)
        remove_button.clicked.connect(self.remove_selected_item)
        selected_layout.addWidget(selected_label)
        selected_layout.addWidget(self.selected_status)
        selected_layout.addWidget(self.selected, 1)
        selected_layout.addWidget(remove_button)
        columns.addWidget(selected_panel)
        columns.setSizes([410, 410])
        layout.addWidget(columns, 1)

        settings = QFrame()
        settings.setObjectName("collectionSettings")
        settings_layout = QHBoxLayout(settings)
        settings_layout.setContentsMargins(16, 12, 16, 12)
        spacing_label = QLabel("Пустых строк между элементами")
        spacing_label.setObjectName("collectionSettingLabel")
        self.row_spacing = NoWheelSpinBox()
        self.row_spacing.setObjectName("collectionSpacing")
        self.row_spacing.setRange(0, 20)
        self.row_spacing.setValue(3)
        self.row_spacing.setSuffix(" стр.")
        self.row_spacing.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        style_label = QLabel("Стиль")
        style_label.setObjectName("collectionSettingLabel")
        self.style = NoWheelComboBox()
        self.style.setObjectName("collectionStyle")
        self.style.view().setObjectName("collectionStylePopup")
        self.style.addItem(
            "楷书 · стандартные прописи",
            CopybookStyle.KAITI.value,
        )
        self.style.addItem(
            "行书 · прописи XingShu",
            CopybookStyle.XINGSHU.value,
        )
        settings_layout.addWidget(spacing_label)
        settings_layout.addWidget(self.row_spacing)
        settings_layout.addSpacing(18)
        settings_layout.addWidget(style_label)
        settings_layout.addWidget(self.style)
        settings_layout.addStretch()
        layout.addWidget(settings)

        actions = QHBoxLayout()
        actions.addStretch()
        cancel_button = QPushButton("Отмена")
        cancel_button.setObjectName("collectionCancelButton")
        cancel_button.setFixedSize(120, 42)
        cancel_button.clicked.connect(self.reject)
        self.generate_button = QPushButton("Создать PDF")
        self.generate_button.setObjectName("collectionPrimaryButton")
        self.generate_button.setFixedSize(120, 42)
        self.generate_button.setEnabled(False)
        self.generate_button.clicked.connect(self.create_pdf)
        actions.addWidget(cancel_button)
        actions.addWidget(self.generate_button)
        layout.addLayout(actions)
        self.search.setFocus()

    def schedule_search(self, text: str) -> None:
        self.search_timer.stop()
        self.search_generation += 1
        self.results.clear()
        if not normalize_display_text(text, preserve_line_breaks=False):
            self.search_status.setText("Введите запрос")
            return
        self.search_status.setText("Поиск…")
        self.search_timer.start()

    def run_search(self) -> None:
        self.search_timer.stop()
        query = normalize_display_text(self.search.text(), preserve_line_breaks=False)
        self.results.clear()
        if not query:
            self.search_status.setText("Введите запрос")
            return
        self.search_generation += 1
        generation = self.search_generation
        self.search_status.setText("Поиск…")
        parent = self.parent()
        if hasattr(parent, "start_background_task"):
            task = BackgroundTask(
                generation,
                search_entries,
                query,
                40,
                category="collection-search",
            )
            task.signals.finished.connect(
                lambda task_generation, rows, error, requested=query:
                    self.apply_collection_search_results(
                        task_generation,
                        requested,
                        rows,
                        error,
                    )
            )
            parent.start_background_task(task)
            return
        try:
            rows = search_entries(query, 40)
            error = None
        except Exception as exception:  # noqa: BLE001 - граница источника.
            rows = None
            error = exception
        self.apply_collection_search_results(generation, query, rows, error)

    def apply_collection_search_results(
        self,
        generation: int,
        query: str,
        rows: object,
        error: object,
    ) -> None:
        if (
            generation != self.search_generation
            or query
            != normalize_display_text(
                self.search.text(), preserve_line_breaks=False
            )
        ):
            return
        self.results.clear()
        if error is not None:
            self.search_status.setText("Словарь недоступен")
            return
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            hanzi = normalize_display_text(row.get("hanzi", ""), preserve_line_breaks=False)
            if not hanzi_sequence(hanzi):
                continue
            result = {
                "hanzi": hanzi,
                "pinyin": normalize_display_text(
                    row.get("pinyin", ""), preserve_line_breaks=False
                ),
                "translation": normalize_display_text(
                    row.get("translation", ""), preserve_line_breaks=False
                ),
            }
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, result)
            item.setSizeHint(QSize(100, 82))
            self.results.addItem(item)
            self.results.setItemWidget(
                item,
                ResultRow(result, self.hanzi_font_family),
            )
        self.search_status.setText(
            f"Найдено: {self.results.count()}"
            if self.results.count()
            else "Ничего не найдено"
        )
        if self.results.count():
            self.results.setCurrentRow(0)

    def stop_search(self, _result: int = 0) -> None:
        self.search_timer.stop()
        self.search_generation += 1
        parent = self.parent()
        if hasattr(parent, "cancel_background_tasks"):
            parent.cancel_background_tasks("collection-search")

    def add_selected_result(self, _item: QListWidgetItem | None = None) -> None:
        item = self.results.currentItem()
        if item is None:
            return
        result = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(result, dict):
            return
        hanzi = result.get("hanzi", "")
        existing = {
            self.selected.item(index).data(Qt.ItemDataRole.UserRole).get("hanzi", "")
            for index in range(self.selected.count())
        }
        if not hanzi or hanzi in existing:
            return
        selected_item = QListWidgetItem()
        selected_item.setData(Qt.ItemDataRole.UserRole, result)
        selected_item.setSizeHint(QSize(100, 82))
        self.selected.addItem(selected_item)
        self.selected.setItemWidget(
            selected_item,
            ResultRow(result, self.hanzi_font_family),
        )
        self.selected.setCurrentItem(selected_item)
        self.update_selected_status()

    def remove_selected_item(self, _item: QListWidgetItem | None = None) -> None:
        row = self.selected.currentRow()
        if row >= 0:
            self.selected.takeItem(row)
            self.update_selected_status()

    def update_selected_status(self) -> None:
        count = self.selected.count()
        self.selected_status.setText(
            f"Добавлено: {count}"
            if count
            else "Пока ничего не добавлено"
        )
        self.generate_button.setEnabled(count > 0)

    def selected_hanzi(self) -> list[str]:
        return [
            self.selected.item(index).data(Qt.ItemDataRole.UserRole)["hanzi"]
            for index in range(self.selected.count())
        ]

    def create_pdf(self) -> None:
        items = self.selected_hanzi()
        if not items:
            return
        style = CopybookStyle(self.style.currentData())
        suffix = "_行书" if style is CopybookStyle.XINGSHU else ""
        suggested = Path.home() / "Documents" / f"сборник_прописей{suffix}.pdf"
        if not suggested.parent.exists():
            suggested = Path.home() / suggested.name
        output_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Собрать прописи",
            str(suggested),
            "PDF (*.pdf)",
        )
        if not output_path:
            return
        try:
            result = generate_assembled_copybook(
                items,
                output_path,
                row_spacing=self.row_spacing.value(),
                style=style,
            )
        except (CopybookError, OSError) as exception:
            QMessageBox.warning(self, "Не удалось создать прописи", str(exception))
            return
        message = f"Сборник сохранён:\n{result.path}"
        if result.missing_characters:
            missing_label = (
                "Нет образца XingShu для: "
                if style is CopybookStyle.XINGSHU
                else "Нет данных о порядке черт для: "
            )
            message += "\n\n" + missing_label + "、".join(result.missing_characters)
        QMessageBox.information(self, "Прописи созданы", message)
        self.accept()


class HanziLabWindow(QMainWindow):
    def __init__(self, study_repository: StudyRepository | None = None) -> None:
        super().__init__()
        self.results: list[dict] = []
        self.current_result: dict | None = None
        self.pending_result: dict | None = None
        self.current_examples: list[dict] = []
        self.search_generation = 0
        self.examples_generation = 0
        self.dictionary_stats_generation = 0
        self.closing = False
        self.dictionary_source_connected = False
        self.background_pool = QThreadPool(self)
        self.background_pool.setMaxThreadCount(2)
        self.background_tasks: set[BackgroundTask] = set()
        self.update_pool = QThreadPool(self)
        self.update_pool.setMaxThreadCount(1)
        self.update_tasks: set[UpdateCheckTask | UpdateDownloadTask] = set()
        self.update_check_active = False
        self.update_progress: QProgressDialog | None = None
        self.update_download_path: Path | None = None
        self.study_repository = study_repository or StudyRepository()
        self.settings = QSettings("HanziLab", "HanziLab")
        saved_source = self.settings.value("dictionary_source", "server", type=str)
        forced_local = os.environ.get("HANZILAB_DICTIONARY_LOCAL") == "1"
        self.dictionary_source = (
            "local"
            if forced_local or (saved_source == "local" and database.FULL_DB_PATH.exists())
            else "server"
        )
        if self.dictionary_source == "local":
            os.environ["HANZILAB_DICTIONARY_LOCAL"] = "1"
            if database.FULL_DB_PATH.exists():
                database.use_local_dictionary()
        else:
            os.environ.pop("HANZILAB_DICTIONARY_LOCAL", None)
            self.settings.setValue("dictionary_source", "server")
        font_default_version = self.settings.value("font_default_version", 0, type=int)
        if font_default_version < 1:
            saved_font = KAITI_FAMILY
            self.settings.setValue("hanzi_font", KAITI_FAMILY)
            self.settings.setValue("font_default_version", 1)
        else:
            saved_font = self.settings.value("hanzi_font", KAITI_FAMILY, type=str)
        self.hanzi_font_family = saved_font if saved_font in {KAITI_FAMILY, XINGSHU_FAMILY} else KAITI_FAMILY
        self.setWindowTitle(APP_TITLE)
        self.setWindowIcon(QIcon(str(APP_ICON_FILE)))
        self.resize(1180, 780)
        self.setMinimumSize(900, 640)

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self.create_sidebar())
        self.page_stack = QStackedWidget()
        self.page_stack.setObjectName("pageStack")
        self.page_stack.addWidget(self.create_content())
        self.page_stack.addWidget(self.create_cards_page())
        self.cursive_page: CursivePage | None = None
        self.cursive_placeholder = QWidget()
        self.page_stack.addWidget(self.cursive_placeholder)
        self.page_stack.addWidget(self.create_about_page())
        shell.addWidget(self.page_stack, 1)

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(280)
        self.search_timer.timeout.connect(self.run_search)
        self.search_warmup_started = False
        self.search_warmup_timer = QTimer(self)
        self.search_warmup_timer.setSingleShot(True)
        self.search_warmup_timer.setInterval(700)
        self.search_warmup_timer.timeout.connect(self.start_search_warmup)
        self.update_timer = QTimer(self)
        self.update_timer.setSingleShot(True)
        self.update_timer.setInterval(2500)
        self.update_timer.timeout.connect(self.check_for_updates)
        self.search_input.textChanged.connect(self.on_search_text_changed)
        self.search_input.returnPressed.connect(self.run_search)
        self.show_empty_state()
        self.refresh_dictionary_stats()

    def schedule_search_warmup(self) -> None:
        """Прогреть FTS после показа окна, не задерживая запуск интерфейса."""
        if not self.closing and not self.search_warmup_started:
            self.search_warmup_timer.start()

    def schedule_update_check(self) -> None:
        if updates.updates_supported() and not self.closing:
            self.update_timer.start()

    def _start_update_task(
        self, task: UpdateCheckTask | UpdateDownloadTask
    ) -> None:
        self.update_tasks.add(task)
        task.signals.finished.connect(
            lambda *_arguments, worker=task: self.update_tasks.discard(worker)
        )
        self.update_pool.start(task)

    def check_for_updates(self, *, manual: bool = False) -> None:
        self.update_timer.stop()
        if self.closing or self.update_check_active:
            return
        if not updates.updates_supported():
            if manual:
                QMessageBox.information(
                    self,
                    "Обновления HanziLab",
                    "Автообновление доступно в собранной Windows-версии.",
                )
            return
        self.update_check_active = True
        self.check_updates_button.setEnabled(False)
        self.check_updates_button.setText("Проверяю…")
        task = UpdateCheckTask()
        task.signals.finished.connect(
            lambda result, error, requested=manual: self._update_check_finished(
                result, error, requested
            )
        )
        self._start_update_task(task)

    def _update_check_finished(
        self, result: object, error: object, manual: bool
    ) -> None:
        self.update_check_active = False
        if self.closing:
            return
        self.check_updates_button.setEnabled(True)
        self.check_updates_button.setText("Проверить обновления")
        if error is not None:
            if manual:
                QMessageBox.warning(
                    self, "Не удалось проверить обновления", str(error)
                )
            return
        if result is None:
            if manual:
                QMessageBox.information(
                    self,
                    "Обновления HanziLab",
                    f"У вас уже установлена актуальная версия {APP_VERSION}.",
                )
            return
        update = result
        dialog = QMessageBox(self)
        dialog.setIcon(QMessageBox.Icon.Information)
        dialog.setWindowTitle("Доступно обновление")
        dialog.setText(f"Вышла новая версия HanziLab {update.version}.")
        dialog.setInformativeText("Желаете обновить программу сейчас?")
        if update.notes:
            dialog.setDetailedText(update.notes[:8000])
        install_button = dialog.addButton("Обновить", QMessageBox.ButtonRole.AcceptRole)
        dialog.addButton("Не сейчас", QMessageBox.ButtonRole.RejectRole)
        exec_message_box(dialog)
        if dialog.clickedButton() is install_button:
            self._download_update(update)

    def _download_update(self, update: updates.UpdateInfo) -> None:
        destination = updates.create_download_path(update.version)
        self.update_download_path = destination
        progress = QProgressDialog(
            f"Загрузка HanziLab {update.version}…", "Отмена", 0, 100, self
        )
        progress.setWindowTitle("Обновление HanziLab")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        self.update_progress = progress
        task = UpdateDownloadTask(update, destination)
        task.signals.progress.connect(progress.setValue)
        progress.canceled.connect(task.cancel)
        task.signals.finished.connect(self._update_download_finished)
        self._start_update_task(task)
        progress.show()

    def _update_download_finished(self, result: object, error: object) -> None:
        download_path = self.update_download_path
        self.update_download_path = None
        progress = self.update_progress
        self.update_progress = None
        if progress is not None:
            progress.close()
            progress.deleteLater()
        if self.closing:
            if download_path is not None:
                shutil.rmtree(download_path.parent, ignore_errors=True)
            return
        if error is not None:
            if download_path is not None:
                shutil.rmtree(download_path.parent, ignore_errors=True)
            if "отменена" not in str(error).lower():
                QMessageBox.warning(
                    self, "Не удалось обновить HanziLab", str(error)
                )
            return
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Information)
        confirmation.setWindowTitle("Всё готово к обновлению")
        confirmation.setText("HanziLab сейчас установит новую версию.")
        confirmation.setInformativeText(
            "Программа закроется, покажет ход установки и откроется снова."
        )
        install_button = confirmation.addButton(
            "Установить", QMessageBox.ButtonRole.AcceptRole
        )
        confirmation.addButton("Отмена", QMessageBox.ButtonRole.RejectRole)
        exec_message_box(confirmation)
        if confirmation.clickedButton() is not install_button:
            if download_path is not None:
                shutil.rmtree(download_path.parent, ignore_errors=True)
            return
        try:
            updates.launch_updater(Path(result))
        except (OSError, updates.UpdateError) as exception:
            if download_path is not None:
                shutil.rmtree(download_path.parent, ignore_errors=True)
            QMessageBox.warning(
                self, "Не удалось запустить обновление", str(exception)
            )
            return
        QApplication.quit()

    def start_search_warmup(self) -> None:
        if (
            self.closing
            or self.search_warmup_started
            or self.search_input.text().strip()
        ):
            return
        self.search_warmup_started = True
        self.start_background_task(
            BackgroundTask(0, warm_search_index, category="warmup")
        )

    def create_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(24, 28, 24, 24)
        layout.setSpacing(8)

        brand = QHBoxLayout()
        brand.setSpacing(11)
        # The brand mark is the character 汉 in the bundled XingShu font.
        # Keep it rendered natively so its calligraphic outline is never
        # distorted by bitmap scaling in the sidebar.
        seal = QLabel("汉")
        seal.setObjectName("seal")
        seal.setAlignment(Qt.AlignmentFlag.AlignCenter)
        seal.setFont(QFont(XINGSHU_FAMILY, 18, QFont.Weight.Bold))
        seal.setFixedSize(38, 38)
        name = QLabel("HanziLab")
        name.setObjectName("brandName")
        brand.addWidget(seal)
        brand.addWidget(name)
        brand.addStretch()
        layout.addLayout(brand)
        layout.addSpacing(36)

        section = QLabel("РАЗДЕЛЫ")
        section.setObjectName("sidebarSection")
        layout.addWidget(section)

        self.dictionary_button = QPushButton("Словарь")
        self.dictionary_button.setObjectName("navActive")
        self.dictionary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dictionary_button.clicked.connect(lambda: self.show_page(0))
        layout.addWidget(self.dictionary_button)

        self.cards_button = QPushButton()
        self.cards_button.setObjectName("navButton")
        self.cards_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cards_button.clicked.connect(lambda: self.show_page(1))
        self.update_cards_button(self.study_repository.get_card_count())
        layout.addWidget(self.cards_button)

        self.cursive_button = QPushButton("Скоропись")
        self.cursive_button.setObjectName("navButton")
        self.cursive_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cursive_button.clicked.connect(lambda: self.show_page(2))
        layout.addWidget(self.cursive_button)

        self.about_button = QPushButton("О программе")
        self.about_button.setObjectName("navButton")
        self.about_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.about_button.clicked.connect(lambda: self.show_page(3))
        layout.addWidget(self.about_button)

        reviews = QPushButton("Повторение")
        reviews.setObjectName("navDisabled")
        reviews.setToolTip("Появится на следующем этапе")
        reviews.setEnabled(False)
        layout.addWidget(reviews)
        layout.addStretch()

        font_label = QLabel("ШРИФТ ИЕРОГЛИФОВ")
        font_label.setObjectName("sidebarSection")
        font_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(font_label)
        self.font_selector = QComboBox()
        self.font_selector.setObjectName("fontSelector")
        self.font_selector.setFixedHeight(36)
        self.font_selector.addItem("KaiTi · 楷体", KAITI_FAMILY)
        self.font_selector.addItem("XingShu · 行书", XINGSHU_FAMILY)
        center_combo_box_text(self.font_selector)
        selected_index = self.font_selector.findData(self.hanzi_font_family)
        self.font_selector.setCurrentIndex(max(selected_index, 0))
        self.font_selector.currentIndexChanged.connect(self.change_hanzi_font)
        layout.addWidget(self.font_selector)
        layout.addSpacing(12)

        database_label = QLabel("БАЗА СЛОВАРЯ")
        database_label.setObjectName("sidebarSection")
        database_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(database_label)

        self.dictionary_entries: int | None = None
        status_panel = QFrame()
        status_panel.setObjectName("localStatus")
        status_layout = QVBoxLayout(status_panel)
        status_layout.setContentsMargins(14, 14, 14, 14)
        status_layout.setSpacing(7)
        self.dictionary_status_title = QLabel()
        self.dictionary_status_title.setObjectName("dictionaryStatusTitle")
        self.dictionary_status_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.dictionary_status = QLabel()
        self.dictionary_status.setObjectName("dictionaryStatusDetails")
        self.dictionary_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        status_layout.addWidget(self.dictionary_status_title)
        status_layout.addWidget(self.dictionary_status)
        layout.addWidget(status_panel)

        self.dictionary_source_selector = QComboBox()
        self.dictionary_source_selector.setObjectName("dictionarySource")
        self.dictionary_source_selector.setFixedHeight(36)
        self.dictionary_source_selector.addItem("Серверная база", "server")
        self.dictionary_source_selector.addItem("Локальная база", "local")
        center_combo_box_text(self.dictionary_source_selector)
        local_item = self.dictionary_source_selector.model().item(1)
        if local_item is not None:
            local_item.setEnabled(database.FULL_DB_PATH.exists())
        source_index = self.dictionary_source_selector.findData(self.dictionary_source)
        self.dictionary_source_selector.setCurrentIndex(max(source_index, 0))
        self.dictionary_source_selector.currentIndexChanged.connect(
            self.change_dictionary_source
        )
        self.dictionary_source_connected = True
        layout.addWidget(self.dictionary_source_selector)

        self.download_dictionary_button = QPushButton(
            "Обновить локальную базу"
            if database.FULL_DB_PATH.exists()
            else "Скачать базу"
        )
        self.download_dictionary_button.setObjectName("downloadDictionary")
        self.download_dictionary_button.setFixedHeight(36)
        self.download_dictionary_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_dictionary_button.clicked.connect(self.start_dictionary_download)
        layout.addWidget(self.download_dictionary_button)
        self.update_dictionary_status()
        return sidebar

    def update_dictionary_status(self) -> None:
        label = "Локальная база" if self.dictionary_source == "local" else "Серверная база"
        details = (
            f"{self.dictionary_entries:,} слов".replace(",", " ")
            if self.dictionary_entries is not None
            else "Получение данных…"
        )
        self.dictionary_status_title.setText(label)
        self.dictionary_status.setText(details)

    def refresh_dictionary_stats(self) -> None:
        self.dictionary_stats_generation += 1
        generation = self.dictionary_stats_generation
        source = self.dictionary_source
        self.dictionary_entries = None
        self.update_dictionary_status()
        self.cancel_background_tasks("dictionary-stats")
        task = BackgroundTask(
            generation, get_stats, category="dictionary-stats"
        )
        task.signals.finished.connect(
            lambda task_generation, result, error, requested_source=source:
                self.apply_dictionary_stats(
                    task_generation, requested_source, result, error
                )
        )
        self.start_background_task(task)

    def apply_dictionary_stats(
        self, generation: int, source: str, result: object, error: object
    ) -> None:
        if (
            self.closing
            or generation != self.dictionary_stats_generation
            or source != self.dictionary_source
        ):
            return
        if error is not None or not isinstance(result, dict):
            label = "Локальная база" if source == "local" else "Серверная база"
            self.dictionary_status_title.setText(label)
            self.dictionary_status.setText("Недоступна")
            return
        self.dictionary_entries = int(result.get("entries", 0))
        self.update_dictionary_status()

    def change_dictionary_source(self, _index: int = -1) -> None:
        source = self.dictionary_source_selector.currentData()
        if source not in {"server", "local"}:
            return
        if source == "local" and not database.FULL_DB_PATH.exists():
            self.dictionary_source_selector.setCurrentIndex(
                self.dictionary_source_selector.findData("server")
            )
            return
        self.dictionary_source = source
        self.settings.setValue("dictionary_source", source)
        if source == "local":
            os.environ["HANZILAB_DICTIONARY_LOCAL"] = "1"
            database.use_local_dictionary()
        else:
            os.environ.pop("HANZILAB_DICTIONARY_LOCAL", None)
            database._close_thread_connection()
        self.refresh_dictionary_stats()

    def start_dictionary_download(self) -> None:
        if self.dictionary_source == "local":
            self.dictionary_source_selector.setCurrentIndex(
                self.dictionary_source_selector.findData("server")
            )
        self.download_dictionary_button.setEnabled(False)
        self.download_dictionary_button.setText("Загрузка · 0%")
        task = DictionaryDownloadTask(database.FULL_DB_PATH)
        task.signals.progress.connect(
            lambda percent: self.download_dictionary_button.setText(
                f"Загрузка · {percent}%"
            )
        )
        task.signals.finished.connect(self.finish_dictionary_download)
        self.start_background_task(task)

    def finish_dictionary_download(self, path: object, error: object) -> None:
        if self.closing:
            return
        self.download_dictionary_button.setEnabled(True)
        self.download_dictionary_button.setText(
            "Обновить локальную базу" if path else "Скачать базу"
        )
        if error is not None:
            if str(error) != "Загрузка отменена":
                QMessageBox.warning(self, "Не удалось скачать базу", str(error))
            return
        local_item = self.dictionary_source_selector.model().item(1)
        if local_item is not None:
            local_item.setEnabled(True)
        QMessageBox.information(
            self,
            "База загружена",
            "Локальная база готова. Теперь её можно выбрать в списке источников.",
        )

    def show_page(self, index: int) -> None:
        if self.page_stack.currentIndex() == index:
            return
        if index == 2 and self.cursive_page is None:
            self.cursive_page = CursivePage(self)
            self.page_stack.removeWidget(self.cursive_placeholder)
            self.cursive_placeholder.deleteLater()
            self.page_stack.insertWidget(2, self.cursive_page)
        self.page_stack.setCurrentIndex(index)
        self.dictionary_button.setObjectName("navActive" if index == 0 else "navButton")
        self.cards_button.setObjectName("navActive" if index == 1 else "navButton")
        self.cursive_button.setObjectName("navActive" if index == 2 else "navButton")
        self.about_button.setObjectName("navActive" if index == 3 else "navButton")
        for button in (
            self.dictionary_button,
            self.cards_button,
            self.cursive_button,
            self.about_button,
        ):
            button.style().unpolish(button)
            button.style().polish(button)
        if index == 1:
            self.study_page.activate()
        else:
            self.study_page.deactivate()

    def update_cards_button(self, count: int) -> None:
        self.cards_button.setText(f"Карточки · {count}" if count else "Карточки")

    def create_cards_page(self) -> QWidget:
        self.study_page = StudyPage(
            self.study_repository,
            get_examples,
            self.hanzi_font_family,
            RUSSIAN_FONT_FAMILY,
            readable_pinyin,
        )
        self.study_page.card_count_changed.connect(self.update_cards_button)
        self.study_page.cards_changed.connect(self.refresh_current_card_button)
        return self.study_page

    def create_about_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("aboutPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(32, 28, 32, 24)
        layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        title = QLabel("О HanziLab")
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        subtitle = QLabel(
            "Иероглифы, пиньинь, перевод и примеры в одном месте."
        )
        subtitle.setObjectName("pageSubtitle")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        layout.addSpacing(12)

        card = QFrame()
        card.setObjectName("aboutCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(24, 24, 24, 24)
        card_layout.setSpacing(20)
        card_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        identity = QHBoxLayout()
        identity.setSpacing(18)
        mark = QLabel("汉")
        mark.setObjectName("aboutMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(58, 58)
        mark.setFont(QFont(XINGSHU_FAMILY, 30))
        identity.addWidget(mark)

        identity_text = QVBoxLayout()
        identity_text.setSpacing(3)
        role = QLabel("СОЗДАТЕЛЬ HANZILAB")
        role.setObjectName("aboutRole")
        role.setWordWrap(True)
        identity_text.addWidget(role)

        developer = QLabel(DEVELOPER_NAME)
        developer.setObjectName("aboutDeveloper")
        identity_text.addWidget(developer)
        identity.addLayout(identity_text, 1)
        card_layout.addLayout(identity)

        divider = QFrame()
        divider.setObjectName("aboutDivider")
        divider.setFrameShape(QFrame.Shape.HLine)
        card_layout.addWidget(divider)

        description = QLabel(
            "Словарь, интервальные карточки, порядок черт и прописи собраны "
            "в одном спокойном рабочем пространстве."
        )
        description.setObjectName("aboutDescription")
        description.setWordWrap(True)
        card_layout.addWidget(description)

        version_row = QHBoxLayout()
        version_label = QLabel(f"Версия {APP_VERSION}")
        version_label.setObjectName("aboutVersion")
        version_row.addWidget(version_label)
        version_row.addStretch()
        self.check_updates_button = QPushButton("Проверить обновления")
        self.check_updates_button.setObjectName("checkUpdatesButton")
        self.check_updates_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.check_updates_button.clicked.connect(
            lambda: self.check_for_updates(manual=True)
        )
        version_row.addWidget(self.check_updates_button)
        card_layout.addLayout(version_row)

        contact_title = QLabel("КОНТАКТЫ")
        contact_title.setObjectName("aboutContactTitle")
        contact_title.setWordWrap(True)
        card_layout.addWidget(contact_title)

        contacts = QFrame()
        contacts.setObjectName("aboutContacts")
        contacts_layout = QVBoxLayout(contacts)
        contacts_layout.setContentsMargins(18, 16, 18, 16)
        contacts_layout.setSpacing(14)
        for url, text in (
            (VK_URL, "VK · профиль разработчика ↗"),
            (TELEGRAM_URL, "Telegram · @choose_o_goose ↗"),
            (TELEGRAM_CHANNEL_URL, "Канал · «一笔一画» ↗"),
        ):
            link = QLabel(
                f'<a style="color:#C94D3C;text-decoration:none" href="{url}">{text}</a>'
            )
            link.setObjectName("aboutContactLink")
            link.setWordWrap(True)
            link.setTextFormat(Qt.TextFormat.RichText)
            link.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
            link.setOpenExternalLinks(True)
            link.setAlignment(Qt.AlignmentFlag.AlignCenter)
            contacts_layout.addWidget(link)
        card_layout.addWidget(contacts)

        layout.addWidget(card)

        footer = QLabel("汉字 · Пиньинь · Перевод · Практика")
        footer.setObjectName("aboutFooter")
        footer.setWordWrap(True)
        layout.addWidget(footer)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("aboutScroll")
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(page)
        return scroll

    def create_content(self) -> QWidget:
        content = QWidget()
        content.setObjectName("content")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(38, 30, 38, 32)
        layout.setSpacing(0)

        header = QHBoxLayout()
        heading_box = QVBoxLayout()
        heading_box.setSpacing(4)
        heading = QLabel("Словарь")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("Поиск по иероглифам, пиньиню и русскому переводу")
        subtitle.setObjectName("pageSubtitle")
        heading_box.addWidget(heading)
        heading_box.addWidget(subtitle)
        header.addLayout(heading_box)
        header.addStretch()
        self.collection_button = QPushButton("Собрать прописи")
        self.collection_button.setObjectName("collectionOpenButton")
        self.collection_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.collection_button.setToolTip(
            "Найти несколько слов или иероглифов и собрать их в один PDF"
        )
        self.collection_button.clicked.connect(self.open_copybook_collection)
        header.addWidget(self.collection_button)
        layout.addLayout(header)
        layout.addSpacing(24)

        search_shell = QFrame()
        search_shell.setObjectName("searchShell")
        search_layout = QHBoxLayout(search_shell)
        search_layout.setContentsMargins(18, 4, 8, 4)
        self.search_input = QLineEdit()
        self.search_input.setObjectName("searchInput")
        input_font = QFont()
        input_font.setFamilies([INPUT_KAITI_FAMILY, RUSSIAN_FONT_FAMILY])
        input_font.setPointSize(13)
        self.search_input.setFont(input_font)
        self.search_input.setPlaceholderText("Введите 学习, xuéxí или «учиться»")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.search_input.customContextMenuRequested.connect(self.show_edit_menu)
        self.search_input.setMinimumHeight(60)
        search_layout.addWidget(self.search_input, 1)
        handwriting_button = QPushButton("✎")
        handwriting_button.setObjectName("handwritingButton")
        handwriting_button.setFont(QFont(RUSSIAN_FONT_FAMILY, 18))
        handwriting_button.setCursor(Qt.CursorShape.PointingHandCursor)
        handwriting_button.setToolTip("Нарисовать иероглиф")
        handwriting_button.clicked.connect(self.open_handwriting_input)
        search_layout.addWidget(handwriting_button)
        layout.addWidget(search_shell)
        layout.addSpacing(18)

        result_line = QHBoxLayout()
        self.result_title = QLabel("Начните поиск")
        self.result_title.setTextFormat(Qt.TextFormat.PlainText)
        self.result_title.setObjectName("resultTitle")
        self.result_count = QLabel("")
        self.result_count.setObjectName("resultCount")
        result_line.addWidget(self.result_title)
        result_line.addStretch()
        result_line.addWidget(self.result_count)
        layout.addLayout(result_line)
        layout.addSpacing(10)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("resultSplitter")
        splitter.setChildrenCollapsible(False)

        self.result_list = QListWidget()
        self.result_list.setObjectName("resultList")
        self.result_list.setMinimumWidth(390)
        self.result_list.currentRowChanged.connect(self.show_result)
        splitter.addWidget(self.result_list)

        self.detail_stack = QStackedWidget()
        self.detail_stack.setObjectName("detailStack")
        self.detail_stack.addWidget(self.create_empty_detail())
        self.detail_stack.addWidget(self.create_detail())
        splitter.addWidget(self.detail_stack)
        splitter.setSizes([460, 560])
        layout.addWidget(splitter, 1)
        return content

    def open_copybook_collection(self) -> None:
        dialog = CopybookCollectionDialog(self.hanzi_font_family, self)
        dialog.exec()

    def open_handwriting_input(self) -> None:
        dialog = HandwritingDialog(self.hanzi_font_family, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        text = dialog.selected_text()
        if not text:
            return
        self.search_input.insert(text)
        self.search_input.setFocus()

    def create_empty_detail(self) -> QWidget:
        empty = QFrame()
        empty.setObjectName("emptyDetail")
        layout = QVBoxLayout(empty)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark = QLabel("查")
        self.empty_mark = mark
        mark.setObjectName("emptyMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFont(QFont(self.hanzi_font_family, 54, QFont.Weight.Normal))
        title = QLabel("Найдите нужное слово")
        title.setObjectName("emptyTitle")
        layout.addWidget(mark)
        layout.addSpacing(10)
        layout.addWidget(title, alignment=Qt.AlignmentFlag.AlignCenter)
        return empty

    def create_detail(self) -> QWidget:
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setObjectName("detailScroll")
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        detail = QFrame()
        detail.setObjectName("detail")
        layout = QVBoxLayout(detail)
        layout.setContentsMargins(38, 34, 38, 34)
        layout.setSpacing(10)

        eyebrow = QLabel("СЛОВАРНАЯ СТАТЬЯ")
        eyebrow.setObjectName("detailEyebrow")
        self.add_card_button = QPushButton("＋ Добавить в карточки")
        self.add_card_button.setObjectName("addCardButton")
        self.add_card_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.add_card_button.clicked.connect(self.add_current_card)
        self.detail_hanzi = QLabel()
        self.detail_hanzi.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_hanzi.setObjectName("detailHanzi")
        self.detail_hanzi.setFont(QFont(self.hanzi_font_family, 52, QFont.Weight.Bold))
        self.detail_hanzi.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copybook_button = QPushButton("Создать прописи")
        self.copybook_button.setObjectName("createCopybookButton")
        self.copybook_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copybook_button.setToolTip(
            "Выбрать стиль и создать PDF-прописи"
        )
        self.copybook_menu = QMenu(self.copybook_button)
        self.copybook_menu.setObjectName("copybookMenu")
        self.copybook_kaiti_action = self.copybook_menu.addAction(
            "楷书 · стандартные прописи"
        )
        self.copybook_kaiti_action.setObjectName("copybookKaitiAction")
        self.copybook_kaiti_action.setToolTip(
            "Точный порядок черт в стандартном стиле 楷书"
        )
        self.copybook_xingshu_action = self.copybook_menu.addAction(
            "行书 · прописи XingShu"
        )
        self.copybook_xingshu_action.setObjectName("copybookXingshuAction")
        self.copybook_xingshu_action.setToolTip(
            "Цельные образцы XingShu без пошагового порядка черт"
        )
        self.copybook_kaiti_action.triggered.connect(
            lambda _checked=False: self.create_copybook_pdf(CopybookStyle.KAITI)
        )
        self.copybook_xingshu_action.triggered.connect(
            lambda _checked=False: self.create_copybook_pdf(CopybookStyle.XINGSHU)
        )
        self.copybook_button.setMenu(self.copybook_menu)
        self.detail_pinyin = QLabel()
        self.detail_pinyin.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_pinyin.setObjectName("detailPinyin")
        self.detail_pinyin.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        divider = QFrame()
        divider.setObjectName("divider")
        divider.setFrameShape(QFrame.Shape.HLine)

        translation_label = QLabel("ПЕРЕВОД")
        translation_label.setObjectName("detailLabel")
        self.detail_translation = QLabel()
        self.detail_translation.setTextFormat(Qt.TextFormat.PlainText)
        self.detail_translation.setObjectName("detailTranslation")
        self.detail_translation.setFont(QFont(RUSSIAN_FONT_FAMILY, 13))
        self.detail_translation.setWordWrap(True)
        self.detail_translation.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.examples_label = QLabel("ПРИМЕРЫ")
        self.examples_label.setObjectName("detailLabel")
        self.detail_examples = QLabel()
        self.detail_examples.setObjectName("detailExamples")
        self.detail_examples.setFont(QFont(RUSSIAN_FONT_FAMILY, 12))
        self.detail_examples.setWordWrap(True)
        self.detail_examples.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.stroke_order = StrokeOrderPanel(self.hanzi_font_family)

        article_header = QHBoxLayout()
        article_header.setSpacing(8)
        article_header.addWidget(eyebrow)
        article_header.addStretch()
        article_header.addWidget(self.copybook_button)
        article_header.addWidget(self.add_card_button)
        layout.addLayout(article_header)
        layout.addSpacing(8)
        layout.addWidget(self.detail_hanzi)
        layout.addWidget(self.detail_pinyin)
        layout.addSpacing(18)
        layout.addWidget(divider)
        layout.addSpacing(16)
        layout.addWidget(translation_label)
        layout.addWidget(self.detail_translation)
        layout.addSpacing(18)
        layout.addWidget(self.examples_label)
        layout.addWidget(self.detail_examples)
        layout.addSpacing(18)
        layout.addWidget(self.stroke_order)
        layout.addStretch()
        self.detail_scroll.setWidget(detail)
        return self.detail_scroll

    def create_copybook_pdf(
        self,
        style: CopybookStyle = CopybookStyle.KAITI,
    ) -> None:
        if not self.current_result:
            return
        hanzi = normalize_display_text(
            self.current_result.get("hanzi", ""),
            preserve_line_breaks=False,
        )
        suggested = (
            Path.home()
            / "Documents"
            / suggested_copybook_name(hanzi, style)
        )
        if not suggested.parent.exists():
            suggested = Path.home() / suggested.name
        output_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Создать прописи",
            str(suggested),
            "PDF (*.pdf)",
        )
        if not output_path:
            return
        try:
            result = generate_hanzi_copybook(hanzi, output_path, style=style)
        except (CopybookError, OSError) as exception:
            QMessageBox.warning(
                self,
                "Не удалось создать прописи",
                str(exception),
            )
            return

        message = (
            f"Прописи {result.style.display_name} сохранены:\n{result.path}"
        )
        if result.missing_characters:
            missing_label = (
                "Нет образца XingShu для: "
                if result.style is CopybookStyle.XINGSHU
                else "Нет данных о порядке черт для: "
            )
            message += "\n\n" + missing_label + "、".join(result.missing_characters)
        QMessageBox.information(self, "Прописи созданы", message)

    def show_edit_menu(self, position) -> None:
        field = self.search_input
        menu = QMenu(field)

        undo = menu.addAction("Отменить")
        undo.setEnabled(field.isUndoAvailable())
        undo.triggered.connect(field.undo)

        redo = menu.addAction("Повторить")
        redo.setEnabled(field.isRedoAvailable())
        redo.triggered.connect(field.redo)
        menu.addSeparator()

        cut = menu.addAction("Вырезать")
        cut.setEnabled(field.hasSelectedText() and not field.isReadOnly())
        cut.triggered.connect(field.cut)

        copy = menu.addAction("Копировать")
        copy.setEnabled(field.hasSelectedText())
        copy.triggered.connect(field.copy)

        paste = menu.addAction("Вставить")
        paste.setEnabled(QApplication.clipboard().mimeData().hasText() and not field.isReadOnly())
        paste.triggered.connect(field.paste)

        delete = menu.addAction("Удалить")
        delete.setEnabled(field.hasSelectedText() and not field.isReadOnly())
        delete.triggered.connect(lambda _checked=False: field.insert(""))
        menu.addSeparator()

        select_all = menu.addAction("Выделить всё")
        select_all.setEnabled(bool(field.text()))
        select_all.triggered.connect(field.selectAll)

        menu.exec(field.mapToGlobal(position))

    def change_hanzi_font(self, _index: int = -1) -> None:
        family = self.font_selector.currentData()
        if family not in {KAITI_FAMILY, XINGSHU_FAMILY}:
            return
        self.hanzi_font_family = family
        self.settings.setValue("hanzi_font", family)
        self.detail_hanzi.setFont(QFont(family, 52, QFont.Weight.Bold))
        self.stroke_order.set_chinese_font(family)
        self.study_page.set_chinese_font(family)
        self.empty_mark.setFont(QFont(family, 54, QFont.Weight.Normal))

        for index in range(self.result_list.count()):
            item = self.result_list.item(index)
            row = self.result_list.itemWidget(item)
            hanzi = row.findChild(QLabel, "resultHanzi") if row else None
            if hanzi:
                character_count = len(CJK_RE.findall(hanzi.text()))
                size = 16 if character_count <= 4 else 14 if character_count <= 6 else 12
                hanzi.setFont(QFont(family, size, QFont.Weight.DemiBold))

        current_row = self.result_list.currentRow()
        if self.current_result and 0 <= current_row < len(self.results):
            # The examples are already in memory. Reformatting them is enough;
            # re-reading the multi-gigabyte dictionary made a font change feel
            # like a second search and briefly hid the article.
            self.detail_examples.setText(self.format_examples(self.current_examples))

    def on_search_text_changed(self, text: str) -> None:
        self.update_search_input_font(text)
        if self.closing:
            return
        if not text.strip():
            self.search_timer.stop()
            self.show_empty_state()
            return
        # Invalidate both pending search and example reads immediately. This
        # prevents an article for the previous query from appearing during the
        # debounce delay and being accidentally added to cards.
        self.search_generation += 1
        self.examples_generation += 1
        self.cancel_background_tasks()
        self.results = []
        self.current_result = None
        self.pending_result = None
        self.current_examples = []
        self.result_list.clear()
        self.result_title.setText("Подготовка поиска…")
        self.result_count.clear()
        self.detail_stack.setCurrentIndex(0)
        self.search_timer.start()

    def update_search_input_font(self, text: str) -> None:
        """Keep normal queries compact while making entered hanzi easy to inspect."""
        font = QFont()
        font.setFamilies([INPUT_KAITI_FAMILY, RUSSIAN_FONT_FAMILY])
        font.setPointSize(22 if CJK_RE.search(text) else 13)
        self.search_input.setFont(font)

    def show_empty_state(self) -> None:
        self.search_generation += 1
        self.examples_generation += 1
        self.cancel_background_tasks()
        self.result_list.clear()
        self.results = []
        self.current_result = None
        self.pending_result = None
        self.current_examples = []
        self.result_title.setText("Начните поиск")
        self.result_count.clear()
        self.detail_stack.setCurrentIndex(0)

    def run_search(self) -> None:
        self.search_timer.stop()
        if self.closing:
            return
        query = normalize_display_text(
            self.search_input.text(), preserve_line_breaks=False
        )
        if not query:
            self.show_empty_state()
            return
        self.search_generation += 1
        generation = self.search_generation
        self.examples_generation += 1
        self.results = []
        self.current_result = None
        self.pending_result = None
        self.current_examples = []
        self.result_list.clear()
        self.result_title.setText(f"Ищу «{query}»…")
        self.result_count.clear()
        self.detail_stack.setCurrentIndex(0)
        self.cancel_background_tasks()
        task = BackgroundTask(
            generation, search_entries, query, 50, category="search"
        )
        task.signals.finished.connect(
            lambda task_generation, rows, error, requested=query: self.apply_search_results(
                task_generation, requested, rows, error
            )
        )
        self.start_background_task(task)

    def start_background_task(self, task: BackgroundTask) -> None:
        if self.closing:
            return
        self.background_tasks.add(task)
        task.signals.finished.connect(
            lambda *_arguments, worker=task: self.background_tasks.discard(worker)
        )
        self.background_pool.start(task)

    def cancel_background_tasks(self, category: str | None = None) -> None:
        for task in tuple(self.background_tasks):
            if category is None or task.category == category:
                task.cancel()

    def apply_search_results(
        self, generation: int, query: str, rows: object, error: object
    ) -> None:
        if (
            self.closing
            or generation != self.search_generation
            or query
            != normalize_display_text(
                self.search_input.text(), preserve_line_breaks=False
            )
        ):
            return
        if error is not None:
            self.result_title.setText("Не удалось выполнить поиск")
            self.result_count.setText("Проверьте интернет и повторите запрос" if dictionary_remote.enabled() else "Словарь недоступен")
            self.detail_stack.setCurrentIndex(0)
            return
        self.results = list(rows or [])
        self.result_title.setText(f"Результаты для «{query}»")
        self.result_count.setText(f"Найдено: {len(self.results)}")

        for result in self.results:
            item = QListWidgetItem()
            row = ResultRow(result, self.hanzi_font_family)
            item.setSizeHint(QSize(100, 82))
            self.result_list.addItem(item)
            self.result_list.setItemWidget(item, row)

        if self.results:
            self.result_list.setCurrentRow(0)
        else:
            self.result_title.setText("Ничего не найдено")
            self.detail_stack.setCurrentIndex(0)

    def show_result(self, row: int) -> None:
        if self.closing or row < 0 or row >= len(self.results):
            return
        result = self.results[row]
        self.examples_generation += 1
        generation = self.examples_generation
        self.pending_result = result
        self.current_result = None
        self.current_examples = []
        self.detail_stack.setCurrentIndex(0)
        self.cancel_background_tasks("examples")
        examples_task = BackgroundTask(
            generation,
            get_examples,
            result["hanzi"],
            6,
            result["pinyin"],
            category="examples",
        )
        examples_task.signals.finished.connect(
            lambda task_generation, examples, error, hanzi=result["hanzi"]: self.apply_examples(
                task_generation, hanzi, examples, error
            )
        )
        self.start_background_task(examples_task)

    def apply_examples(
        self, generation: int, hanzi: str, examples: object, error: object
    ) -> None:
        if (
            self.closing
            or generation != self.examples_generation
            or not self.pending_result
            or self.pending_result["hanzi"] != hanzi
        ):
            return
        result = self.pending_result
        self.pending_result = None
        self.current_result = result
        self.current_examples = list(examples or []) if error is None else []
        display_hanzi = normalize_display_text(
            result["hanzi"], preserve_line_breaks=False
        )
        display_pinyin = normalize_display_text(
            result["pinyin"], preserve_line_breaks=False
        )
        self.detail_hanzi.setText(display_hanzi)
        self.detail_pinyin.setText(readable_pinyin(display_hanzi, display_pinyin))
        self.detail_translation.setText(normalize_display_text(result["translation"]))
        self.detail_examples.setText(self.format_examples(self.current_examples))
        example_html = self.detail_examples.text()
        self.examples_label.setVisible(bool(example_html))
        self.detail_examples.setVisible(bool(example_html))
        self.stroke_order.set_word(display_hanzi)
        self.set_card_button_state(self.study_repository.has_card(display_hanzi))
        self.detail_stack.setCurrentIndex(1)
        self.detail_scroll.verticalScrollBar().setValue(0)

    def format_examples(self, examples: list[dict]) -> str:
        return format_example_blocks(
            examples, self.hanzi_font_family, 14, RUSSIAN_FONT_FAMILY
        )

    def add_current_card(self) -> None:
        if not self.current_result:
            return
        result = self.current_result
        hanzi = normalize_display_text(result["hanzi"], preserve_line_breaks=False)
        if self.study_repository.has_card(hanzi):
            answer = QMessageBox.question(
                self,
                "Убрать карточку?",
                f"Убрать «{hanzi}» из карточек?\n"
                "Расписание и история повторений этой карточки будут удалены.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if self.study_repository.remove_card(hanzi):
                self.set_card_button_state(False)
                self.update_cards_button(self.study_repository.get_card_count())
                self.study_page.card_was_removed(hanzi)
                self.refresh_current_card_button()
            return
        added = self.study_repository.add_card(
            hanzi, result["pinyin"], result["translation"]
        )
        if added:
            self.set_card_button_state(True)
            self.update_cards_button(self.study_repository.get_card_count())
            self.study_page.refresh()
            self.refresh_current_card_button()

    def refresh_current_card_button(self) -> None:
        if self.current_result:
            hanzi = normalize_display_text(
                self.current_result["hanzi"], preserve_line_breaks=False
            )
            self.set_card_button_state(
                self.study_repository.has_card(hanzi)
            )

    def closeEvent(self, event) -> None:
        self.closing = True
        try:
            if not self.dictionary_source_connected:
                raise AttributeError
            self.dictionary_source_selector.currentIndexChanged.disconnect(
                self.change_dictionary_source
            )
            self.dictionary_source_connected = False
        except (AttributeError, RuntimeError):
            pass
        self.search_generation += 1
        self.examples_generation += 1
        self.pending_result = None
        self.current_result = None
        self.search_timer.stop()
        self.search_warmup_timer.stop()
        self.update_timer.stop()
        for task in tuple(self.update_tasks):
            task.cancel()
        self.update_pool.clear()
        self.study_page.shutdown()
        self.cancel_background_tasks()
        self.background_pool.clear()
        super().closeEvent(event)

    def set_card_button_state(self, in_cards: bool) -> None:
        self.add_card_button.setProperty("removeMode", in_cards)
        self.add_card_button.setText(
            "− Из карточек" if in_cards else "＋ В карточки"
        )
        self.add_card_button.setToolTip(
            "Удалить слово и его историю повторений"
            if in_cards
            else "Добавить слово в учебные карточки"
        )
        self.add_card_button.setEnabled(True)
        self.add_card_button.style().unpolish(self.add_card_button)
        self.add_card_button.style().polish(self.add_card_button)


STYLESHEET = """
QWidget#root { background: #F4F6F8; color: #182026; }
QMessageBox, QProgressDialog { background: #F7F9FA; color: #182026; }
QMessageBox QLabel, QProgressDialog QLabel { background: transparent; color: #344149; font-size: 13px; }
QMessageBox QPushButton, QProgressDialog QPushButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 18px; min-width: 76px; font-size: 12px; font-weight: 600; }
QMessageBox QPushButton:hover, QProgressDialog QPushButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QMessageBox QPushButton:pressed, QProgressDialog QPushButton:pressed { background: #FADFD9; color: #B44334; border-color: #E2A297; }
QMessageBox QTextEdit { background: #FFFFFF; color: #425159; border: 1px solid #DDE4E6; border-radius: 8px; padding: 7px; selection-background-color: #FADFD9; selection-color: #7A3027; }
QProgressDialog QProgressBar { background: #E8EDEF; color: #344149; border: none; border-radius: 6px; text-align: center; min-height: 12px; }
QProgressDialog QProgressBar::chunk { background: #E05945; border-radius: 6px; }
QFrame#sidebar { background: #111D22; border: none; }
QLabel#seal { background: #E05945; color: white; border-radius: 10px; }
QLabel#brandName { color: #F7FAF9; font-size: 18px; font-weight: 700; }
QLabel#sidebarSection { color: #71838B; font-size: 10px; font-weight: 700; letter-spacing: 1.4px; padding: 0 10px 7px; }
QPushButton#navActive, QPushButton#navButton, QPushButton#navDisabled { border: none; border-radius: 9px; padding: 12px 14px; text-align: left; font-size: 14px; }
QPushButton#navActive { background: #24343A; color: #FFFFFF; font-weight: 600; }
QPushButton#navActive:hover { background: #2C4047; }
QPushButton#navButton { background: transparent; color: #B3C0C5; }
QPushButton#navButton:hover { background: #1A2A30; color: #FFFFFF; }
QPushButton#navDisabled { background: transparent; color: #71838B; }
QWidget#aboutPage { background: #F4F6F8; }
QFrame#aboutCard { background: #FFFFFF; border: 1px solid #DDE4E6; border-radius: 16px; }
QLabel#aboutMark { background: #E05945; color: #FFFFFF; border-radius: 14px; }
QLabel#aboutRole, QLabel#aboutContactTitle { color: #829198; font-size: 10px; font-weight: 700; letter-spacing: 1.5px; }
QLabel#aboutDeveloper { color: #1D2B31; font-size: 18px; font-weight: 700; }
QFrame#aboutDivider { color: #E8ECEE; background: #E8ECEE; border: none; max-height: 1px; }
QLabel#aboutDescription { color: #536269; font-size: 14px; line-height: 1.55; }
QLabel#aboutVersion { color: #66767D; font-size: 12px; font-weight: 600; }
QPushButton#checkUpdatesButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; padding: 9px 13px; font-size: 12px; font-weight: 600; }
QPushButton#checkUpdatesButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#checkUpdatesButton:disabled { background: #F2F4F5; color: #A8B1B5; }
QFrame#aboutContacts { background: #F7F9F9; border: 1px solid #E5EAEC; border-radius: 10px; }
QLabel#aboutContactLink { background: transparent; border: none; font-size: 14px; }
QLabel#aboutFooter { color: #91A0A6; font-size: 11px; letter-spacing: 0.7px; padding: 10px 2px; }
QFrame#localStatus { background: #17262C; border-radius: 10px; }
QLabel#dictionaryStatusTitle { color: #83949B; font-size: 12px; background: transparent; }
QLabel#dictionaryStatusDetails { color: #B3C1C7; font-size: 13px; background: transparent; }
QComboBox#fontSelector { background: #17262C; color: #D8E1E4; border: 1px solid #2A3C43; border-radius: 9px; padding: 9px 11px; font-size: 12px; }
QComboBox#dictionarySource { background: #17262C; color: #D8E1E4; border: 1px solid #2A3C43; border-radius: 9px; padding: 8px 10px; font-size: 11px; }
QComboBox#fontSelector QLineEdit, QComboBox#dictionarySource QLineEdit { background: transparent; color: #D8E1E4; border: none; padding: 0; }
QComboBox#fontSelector:hover, QComboBox#dictionarySource:hover { border-color: #496069; }
QComboBox#fontSelector::drop-down, QComboBox#dictionarySource::drop-down { border: none; width: 24px; }
QComboBox#fontSelector QAbstractItemView, QComboBox#dictionarySource QAbstractItemView { background: #17262C; color: #EAF0F2; border: 1px solid #2A3C43; padding: 3px; font-size: 13px; selection-background-color: #E05945; outline: none; }
QComboBox#fontSelector QAbstractItemView::item, QComboBox#dictionarySource QAbstractItemView::item { min-height: 28px; padding: 3px 8px; }
QPushButton#downloadDictionary { background: transparent; color: #D6E0E3; border: 1px solid #354A52; border-radius: 9px; padding: 7px 10px; font-size: 11px; font-weight: 600; }
QPushButton#downloadDictionary:hover { background: #24343A; border-color: #526A73; color: #FFFFFF; }
QPushButton#downloadDictionary:disabled { color: #71838B; border-color: #293B42; }
QWidget#content { background: #F4F6F8; }
QWidget#cursivePage { background: #F4F6F8; color: #182026; }
QLineEdit#cursiveSearch { background: #FFFFFF; color: #243139; border: 1px solid #DCE2E5; border-radius: 10px; padding: 2px 13px; font-size: 15px; }
QLineEdit#cursiveSearch:focus { border-color: #E05945; }
QTableWidget#cursiveTable { background: #FFFFFF; color: #1B272C; gridline-color: #E1E6E8; border: 1px solid #DFE4E6; border-radius: 10px; outline: none; selection-background-color: #FFF1ED; selection-color: #C94D3C; }
QTableWidget#cursiveTable::item { color: #1B272C; }
QTableWidget#cursiveTable::item:hover { background: #F8FAFA; }
QTableWidget#cursiveTable::item:selected { background: #FFF1ED; color: #C94D3C; }
QFrame#cursiveDetail { background: #FFFFFF; border: 1px solid #DFE4E6; border-radius: 12px; }
QScrollArea#cursiveDetailScroll { background: #F4F6F8; border: none; }
QLabel#cursiveHeading { color: #152126; background: transparent; border: none; }
QLabel#cursivePreview { background: #FFFFFF; color: #7B888E; border: 1px solid #E2E7E9; border-radius: 10px; padding: 6px; font-size: 13px; }
QComboBox#cursiveVariant { background: #F4F7F8; color: #344249; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 28px 8px 12px; font-size: 13px; }
QComboBox#cursiveVariant:disabled { color: #75838A; }
QComboBox#cursiveVariant::drop-down { border: none; width: 24px; }
QComboBox#cursiveVariant QAbstractItemView { background: #FFFFFF; color: #344249; selection-background-color: #FFF1ED; selection-color: #C94D3C; border: 1px solid #D7DEE1; }
QPushButton#cursiveSaveButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 9px; padding: 11px 15px; font-size: 13px; font-weight: 700; }
QPushButton#cursiveSaveButton:hover { background: #C94D3C; }
QPushButton#cursiveSaveButton:disabled { background: #D9E0E3; color: #89969C; }
QPushButton#cursiveOpenButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; padding: 9px 13px; font-size: 12px; }
QPushButton#cursiveOpenButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QLabel#pageTitle { color: #182026; font-size: 28px; font-weight: 700; }
QLabel#pageSubtitle { color: #6E7C83; font-size: 13px; }
QFrame#searchShell { background: #FFFFFF; border: 1px solid #DCE2E5; border-radius: 13px; }
QFrame#searchShell:focus-within { border-color: #E05945; }
QLineEdit#searchInput { border: none; background: transparent; color: #182026; padding: 0 4px; }
QLineEdit#searchInput::placeholder { color: #96A2A7; }
QPushButton#handwritingButton { background: #F2F6F7; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; min-width: 44px; max-width: 44px; min-height: 40px; max-height: 40px; padding: 0; }
QPushButton#handwritingButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#handwritingButton:pressed { background: #FADFD9; color: #B44334; }
QDialog#handwritingDialog { background: #F7F9FA; }
QLabel#handwritingTitle { color: #182026; font-size: 21px; font-weight: 700; }
QLabel#handwritingSubtitle, QLabel#handwritingStatus { color: #6E7C83; font-size: 12px; }
QLineEdit#handwritingComposition { background: #FFFFFF; color: #17242A; border: 1px solid #D7DEE1; border-radius: 9px; padding: 8px 12px; min-height: 38px; }
QWidget#handwritingCanvas { background: #FFFFFF; border: 1px solid #DCE4E7; border-radius: 12px; }
QPushButton#handwritingCandidate { background: #FFFFFF; color: #17242A; border: 1px solid #D7DEE1; border-radius: 9px; min-width: 50px; min-height: 46px; padding: 2px; }
QPushButton#handwritingCandidate:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#handwritingSecondaryButton, QPushButton#handwritingCancelButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 8px; padding: 9px 13px; }
QPushButton#handwritingSecondaryButton:hover, QPushButton#handwritingCancelButton:hover { background: #EEF3F5; border-color: #B7C5CA; }
QPushButton#handwritingInsertButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 9px; padding: 10px 18px; font-weight: 700; }
QPushButton#handwritingInsertButton:hover { background: #C94D3C; }
QPushButton#handwritingInsertButton:disabled { background: #D9E0E3; color: #89969C; }
QLabel#resultTitle { color: #344149; font-size: 13px; font-weight: 600; }
QLabel#resultCount { color: #7E8A90; font-size: 12px; }
QSplitter#resultSplitter::handle { background: transparent; width: 12px; }
QListWidget#resultList, QStackedWidget#detailStack { background: #FFFFFF; border: 1px solid #DFE4E6; border-radius: 12px; outline: none; }
QListWidget#resultList::item { border-bottom: 1px solid #EDF0F1; }
QListWidget#resultList::item:selected { background: #FFF1ED; border-left: 3px solid #E05945; }
QListWidget#resultList::item:hover:!selected { background: #F8FAFA; }
QLabel#resultHanzi { color: #1B272C; }
QLabel#resultPinyin { color: #D45240; font-size: 13px; font-weight: 700; }
QLabel#resultTranslation { color: #647178; font-size: 12px; }
QFrame#emptyDetail, QFrame#detail { background: #FFFFFF; border: none; border-radius: 12px; }
QScrollArea#detailScroll { background: #FFFFFF; border: none; }
QScrollArea#detailScroll > QWidget > QWidget { background: #FFFFFF; }
QLabel#emptyMark { color: #E7B1A8; font-size: 62px; font-weight: 700; }
QLabel#emptyTitle { color: #253239; font-size: 17px; font-weight: 700; }
QLabel#emptyText { color: #7B888E; font-size: 13px; line-height: 1.5; }
QLabel#detailEyebrow, QLabel#detailLabel { color: #8B989D; font-size: 10px; font-weight: 700; letter-spacing: 1.3px; }
QLabel#detailHanzi { color: #152126; }
QLabel#detailPinyin { color: #D45240; font-size: 18px; font-weight: 600; }
QPushButton#createCopybookButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; padding: 9px 13px; font-size: 12px; font-weight: 600; }
QPushButton#createCopybookButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#createCopybookButton:pressed { background: #FADFD9; color: #B44334; }
QPushButton#collectionOpenButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; padding: 10px 15px; font-size: 12px; font-weight: 600; }
QPushButton#collectionOpenButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QDialog#copybookCollectionDialog { background: #F4F6F8; color: #182026; }
QLabel#copybookCollectionTitle { color: #182026; font-size: 28px; font-weight: 700; }
QLabel#copybookCollectionSubtitle { color: #6E7C83; font-size: 13px; }
QFrame#collectionSearchShell { background: #FFFFFF; border: 1px solid #DCE2E5; border-radius: 13px; }
QLineEdit#collectionSearch { background: transparent; color: #182026; border: none; padding: 0 4px; }
QFrame#collectionPanel { background: #FFFFFF; border: 1px solid #DFE4E6; border-radius: 12px; }
QLabel#collectionSectionLabel { color: #8B989D; font-size: 10px; font-weight: 700; letter-spacing: 1.2px; }
QLabel#collectionHint { color: #75838A; font-size: 11px; }
QListWidget#collectionList { background: #FFFFFF; color: #253239; border: 1px solid #E2E7E9; border-radius: 8px; outline: none; }
QListWidget#collectionList::item { border-bottom: 1px solid #EDF0F1; }
QListWidget#collectionList::item:selected { background: #FFF1ED; color: #B44334; border-left: 3px solid #E05945; }
QFrame#collectionSettings { background: #FFFFFF; border: 1px solid #DFE4E6; border-radius: 10px; }
QLabel#collectionSettingLabel { color: #536269; font-size: 12px; }
QSpinBox#collectionSpacing, QComboBox#collectionStyle { background: #F4F7F8; color: #344249; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 30px 8px 11px; min-height: 20px; font-size: 12px; }
QSpinBox#collectionSpacing:hover, QComboBox#collectionStyle:hover { background: #FFF9F7; border-color: #E8B5AC; }
QSpinBox#collectionSpacing:focus, QComboBox#collectionStyle:focus { background: #FFFFFF; border-color: #E05945; }
QComboBox#collectionStyle::drop-down { border: none; width: 24px; }
QAbstractItemView#collectionStylePopup { background: #FFFFFF; color: #344249; border: 1px solid #D7DEE1; border-radius: 8px; padding: 4px; outline: none; selection-background-color: #FFF1ED; selection-color: #C94D3C; }
QAbstractItemView#collectionStylePopup::item { min-height: 30px; padding: 4px 9px; border-radius: 6px; }
QAbstractItemView#collectionStylePopup::item:hover { background: #F8FAFA; color: #26343B; }
QAbstractItemView#collectionStylePopup::item:selected { background: #FFF1ED; color: #C94D3C; }
QSpinBox#collectionSpacing::up-button, QSpinBox#collectionSpacing::down-button { background: #EEF2F3; border: none; border-left: 1px solid #D7DEE1; width: 24px; }
QSpinBox#collectionSpacing::up-button { border-top-right-radius: 7px; }
QSpinBox#collectionSpacing::down-button { border-bottom-right-radius: 7px; }
QSpinBox#collectionSpacing::up-button:hover, QSpinBox#collectionSpacing::down-button:hover { background: #FFF1ED; }
QPushButton#collectionPrimaryButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 9px; padding: 12px 22px; font-size: 13px; font-weight: 700; }
QPushButton#collectionPrimaryButton:hover { background: #C94D3C; }
QPushButton#collectionPrimaryButton:disabled { background: #D9E0E3; color: #89969C; }
QPushButton#collectionSecondaryButton, QPushButton#collectionCancelButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 8px; padding: 9px 13px; font-size: 12px; }
QPushButton#collectionSecondaryButton:hover, QPushButton#collectionCancelButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QMenu#copybookMenu { background: #FFFFFF; color: #334249; border: 1px solid #D7DEE1; border-radius: 9px; padding: 5px; }
QMenu#copybookMenu::item { border-radius: 7px; padding: 9px 16px; }
QMenu#copybookMenu::item:selected { background: #FFF1ED; color: #C94D3C; }
QPushButton#addCardButton { background: #F4F7F8; color: #344249; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 12px; font-size: 12px; font-weight: 600; }
QPushButton#addCardButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#addCardButton:disabled { background: #EFF6F2; color: #568269; border-color: #D6E7DC; }
QPushButton#addCardButton[removeMode="true"] { background: #FFF4F2; color: #B94A3A; border-color: #E9C0B9; }
QPushButton#addCardButton[removeMode="true"]:hover { background: #FCE5E1; color: #9F3528; border-color: #DFA89F; }
QFrame#divider { color: #E7EBED; }
QLabel#detailTranslation { color: #354249; font-size: 16px; line-height: 1.6; }
QLabel#detailExamples { color: #536168; font-size: 14px; line-height: 1.55; }
QFrame#strokePanel { background: #FFFFFF; border: none; }
QWidget#strokeCanvas { background: #FFFFFF; border: 1px solid #E2E7E9; border-radius: 10px; }
QFrame#strokeCharacterSelector { background: #F3F6F7; border: 1px solid #DEE5E7; border-radius: 11px; }
QPushButton#strokeCharacterChip { background: transparent; color: #405057; border: none; border-radius: 8px; min-width: 42px; min-height: 38px; padding: 0 8px; }
QPushButton#strokeCharacterChip:hover { background: #FFFFFF; color: #D45240; }
QPushButton#strokeCharacterChip:checked { background: #FFFFFF; color: #D45240; border: 1px solid #E8D8D4; font-weight: 700; }
QLabel#strokeStatus { color: #75838A; font-size: 12px; }
QWidget#studyPage { background: #F4F6F8; }
QLabel#studySettingLabel, QLabel#studyQueueInfo { color: #6E7C83; font-size: 12px; }
QPushButton#importCardsButton, QPushButton#exportAnkiButton, QPushButton#cardListButton, QPushButton#schedulerSettingsButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; padding: 10px 14px; font-size: 12px; font-weight: 600; }
QPushButton#importCardsButton:hover, QPushButton#exportAnkiButton:hover, QPushButton#cardListButton:hover, QPushButton#schedulerSettingsButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#importCardsButton:pressed, QPushButton#exportAnkiButton:pressed, QPushButton#cardListButton:pressed, QPushButton#schedulerSettingsButton:pressed { background: #FADFD9; color: #B44334; }
QPushButton#manualCardButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 9px; padding: 11px 15px; font-size: 12px; font-weight: 700; }
QPushButton#manualCardButton:hover { background: #C94D3C; }
QPushButton#manualCardButton:pressed { background: #B44334; }
QDialog#cardListDialog { background: #F7F9FA; }
QDialog#spacedRepetitionSettingsDialog { background: #F7F9FA; }
QLabel#settingsDialogTitle { color: #182026; font-size: 20px; font-weight: 700; }
QLabel#settingsDialogHint { color: #66767D; font-size: 12px; }
QScrollArea#schedulerSettingsScroll { background: transparent; border: none; }
QScrollArea#schedulerSettingsScroll > QWidget > QWidget { background: #F7F9FA; }
QWidget#schedulerSettingsContent { background: #F7F9FA; }
QFrame#schedulerSettingsSection, QFrame#fsrsSettingsSection { background: #FFFFFF; border: 1px solid #DCE3E5; border-radius: 11px; }
QFrame#fsrsSettingsSection { border-color: #E8C3BC; }
QLabel#schedulerSectionTitle { color: #182026; font-size: 15px; font-weight: 700; }
QLabel#schedulerSectionDescription { color: #75838A; font-size: 12px; padding-top: 2px; }
QFrame#schedulerSettingRow { background: transparent; border: none; border-top: 1px solid #EDF0F1; }
QLabel#schedulerSettingTitle { color: #26343B; font-size: 13px; font-weight: 600; }
QLabel#schedulerSettingDescription { color: #75838A; font-size: 11px; }
QLabel#easyDayLabel { color: #66767D; font-size: 11px; font-weight: 600; }
QComboBox#easyDayLoad { min-width: 70px; }
QDialog#spacedRepetitionSettingsDialog QLineEdit, QDialog#spacedRepetitionSettingsDialog QSpinBox, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox, QDialog#spacedRepetitionSettingsDialog QComboBox, QPlainTextEdit#fsrsParameters { background: #F4F7F8; color: #344249; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 30px 8px 11px; min-height: 20px; font-size: 12px; selection-background-color: #FADFD9; selection-color: #9F3528; }
QDialog#spacedRepetitionSettingsDialog QLineEdit:hover, QDialog#spacedRepetitionSettingsDialog QSpinBox:hover, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox:hover, QDialog#spacedRepetitionSettingsDialog QComboBox:hover, QPlainTextEdit#fsrsParameters:hover { background: #FFF9F7; border-color: #E8B5AC; }
QDialog#spacedRepetitionSettingsDialog QLineEdit:focus, QDialog#spacedRepetitionSettingsDialog QSpinBox:focus, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox:focus, QDialog#spacedRepetitionSettingsDialog QComboBox:focus, QPlainTextEdit#fsrsParameters:focus { background: #FFFFFF; border-color: #E05945; }
QDialog#spacedRepetitionSettingsDialog QComboBox::drop-down { background: transparent; border: none; width: 28px; }
QAbstractItemView#schedulerComboPopup { background: #FFFFFF; color: #344249; border: 1px solid #D7DEE1; border-radius: 8px; padding: 4px; outline: none; selection-background-color: #FFF1ED; selection-color: #C94D3C; }
QAbstractItemView#schedulerComboPopup::item { min-height: 30px; padding: 4px 9px; border-radius: 6px; }
QAbstractItemView#schedulerComboPopup::item:hover { background: #F8FAFA; color: #26343B; }
QAbstractItemView#schedulerComboPopup::item:selected { background: #FFF1ED; color: #C94D3C; }
QDialog#spacedRepetitionSettingsDialog QSpinBox::up-button, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::up-button, QDialog#spacedRepetitionSettingsDialog QSpinBox::down-button, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::down-button { background: #EEF2F3; border: none; border-left: 1px solid #D7DEE1; width: 24px; }
QDialog#spacedRepetitionSettingsDialog QSpinBox::up-button, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::up-button { border-top-right-radius: 7px; }
QDialog#spacedRepetitionSettingsDialog QSpinBox::down-button, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::down-button { border-bottom-right-radius: 7px; }
QDialog#spacedRepetitionSettingsDialog QSpinBox::up-button:hover, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::up-button:hover, QDialog#spacedRepetitionSettingsDialog QSpinBox::down-button:hover, QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::down-button:hover { background: #FFF1ED; }
QDialog#spacedRepetitionSettingsDialog QPushButton[schedulerToggle="true"] { background: #FFFFFF; color: #66767D; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 12px; font-size: 12px; font-weight: 600; }
QDialog#spacedRepetitionSettingsDialog QPushButton[schedulerToggle="true"]:hover { background: #F8FAFA; color: #425159; border-color: #B7C5CA; }
QDialog#spacedRepetitionSettingsDialog QPushButton[schedulerToggle="true"]:checked { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QDialog#spacedRepetitionSettingsDialog QPushButton[schedulerToggle="true"]:checked:hover { background: #FADFD9; color: #B44334; border-color: #DFA89F; }
QLabel#retentionEffect { color: #66767D; font-size: 11px; }
QLabel#retentionWarning { color: #9A5A16; background: #FFF6E8; border: none; border-radius: 7px; padding: 9px; }
QLabel#schedulerInfoCallout { color: #536168; background: #F2F6F7; border: 1px solid #DCE5E7; border-radius: 7px; padding: 10px; }
QPushButton#parametersToggle, QPushButton#resetFsrsParameters { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 12px; font-weight: 600; }
QPushButton#parametersToggle:hover, QPushButton#resetFsrsParameters:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#parametersToggle:checked { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QFrame#parametersPanel { background: #F7F9FA; border: 1px solid #E2E7E9; border-radius: 8px; }
QDialog#spacedRepetitionSettingsDialog QDialogButtonBox QPushButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 8px; padding: 9px 16px; min-width: 88px; font-weight: 600; }
QDialog#spacedRepetitionSettingsDialog QDialogButtonBox QPushButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QDialog#spacedRepetitionSettingsDialog QDialogButtonBox QPushButton:pressed { background: #FADFD9; color: #B44334; border-color: #E8B5AC; }
QDialog#spacedRepetitionSettingsDialog QPushButton#schedulerSettingsSave { background: #E05945; color: #FFFFFF; border-color: #E05945; }
QDialog#spacedRepetitionSettingsDialog QPushButton#schedulerSettingsSave:hover { background: #C94D3C; color: #FFFFFF; border-color: #C94D3C; }
QDialog#spacedRepetitionSettingsDialog QPushButton#schedulerSettingsSave:pressed { background: #B44334; color: #FFFFFF; border-color: #B44334; }
QLabel#cardListTitle { color: #182026; font-size: 20px; font-weight: 700; }
QLabel#cardListCount { color: #6E7C83; font-size: 12px; }
QTableWidget#cardListTable { background: #FFFFFF; color: #243139; gridline-color: #E1E6E8; border: 1px solid #DFE4E6; border-radius: 10px; outline: none; selection-background-color: #FFF1ED; selection-color: #C94D3C; }
QTableWidget#cardListTable::item { padding: 7px; }
QTableWidget#cardListTable::item:hover { background: #F8FAFA; }
QTableWidget#cardListTable::item:selected { background: #FFF1ED; color: #C94D3C; }
QHeaderView::section { background: #EEF2F3; color: #526168; border: none; border-bottom: 1px solid #D7DEE1; padding: 9px; font-size: 12px; font-weight: 700; }
QPushButton#cardListCloseButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 9px; padding: 10px 20px; min-width: 90px; font-weight: 700; }
QPushButton#cardListCloseButton:hover { background: #C94D3C; }
QPushButton#cardListCloseButton:pressed { background: #B44334; }
QPushButton#cardListResumeButton { background: #FFFFFF; color: #425159; border: 1px solid #D7DEE1; border-radius: 9px; padding: 10px 14px; font-weight: 600; }
QPushButton#cardListResumeButton:hover { background: #FFF1ED; color: #C94D3C; border-color: #E8B5AC; }
QPushButton#cardListResumeButton:disabled { background: #F2F4F5; color: #A8B1B5; }
QPushButton#cardListDeleteSelected, QPushButton#cardListDeleteAll { background: #FFFFFF; color: #B44334; border: 1px solid #E3B2AA; border-radius: 9px; padding: 10px 13px; font-weight: 600; }
QPushButton#cardListDeleteSelected:hover, QPushButton#cardListDeleteAll:hover { background: #FFF1ED; border-color: #D98F83; }
QPushButton#cardListDeleteSelected:pressed, QPushButton#cardListDeleteAll:pressed { background: #FADFD9; }
QPushButton#cardListDeleteSelected:disabled, QPushButton#cardListDeleteAll:disabled { background: #F2F4F5; color: #A8B1B5; border-color: #D7DEE1; }
QDialog#manualCardDialog { background: #F7F9FA; }
QLabel#manualCardTitle { color: #182026; font-size: 20px; font-weight: 700; }
QLabel#manualCardSubtitle { color: #6E7C83; font-size: 12px; }
QLineEdit#manualCardHanzi, QLineEdit#manualCardInput, QPlainTextEdit#manualCardTranslation { background: #FFFFFF; color: #243139; border: 1px solid #D7DEE1; border-radius: 8px; padding: 9px 11px; }
QLineEdit#manualCardHanzi:focus, QLineEdit#manualCardInput:focus, QPlainTextEdit#manualCardTranslation:focus { border-color: #E05945; }
QLineEdit#manualCardHanzi[duplicate="true"] { background: #FFF0EE; color: #B42318; border: 2px solid #D92D20; }
QLabel#manualCardError { color: #B44334; font-size: 12px; }
QLabel#manualCardStatus { color: #6E7C83; font-size: 12px; }
QLabel#manualCardStatus[status="duplicate"] { color: #B42318; font-weight: 700; }
QLabel#manualCardStatus[status="success"] { color: #217A4A; font-weight: 700; }
QPushButton#manualCardCancelButton { background: #FFFFFF; color: #33444D; border: 1px solid #CFD9DD; border-radius: 9px; padding: 10px 18px; min-width: 90px; }
QPushButton#manualCardCancelButton:hover { background: #EEF3F5; border-color: #B7C5CA; }
QPushButton#manualCardSaveButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 9px; padding: 11px 20px; min-width: 145px; font-weight: 700; }
QPushButton#manualCardSaveButton:hover { background: #C94D3C; }
QPushButton#manualCardSaveButton:pressed { background: #B44334; }
QPushButton#manualCardSaveButton:disabled { background: #D9E0E3; color: #89969C; }
QPushButton#manualCardPriorityButton { background: #FFF4E8; color: #9A4D00; border: 1px solid #F0B978; border-radius: 8px; padding: 9px 15px; font-weight: 700; }
QPushButton#manualCardPriorityButton:hover { background: #FFE8CC; border-color: #DC9850; }
QPushButton#manualCardPriorityButton:disabled { background: #E9F5EE; color: #217A4A; border-color: #A8D5BA; }
QFrame#limitControl { background: #FFFFFF; border: 1px solid #D9E0E3; border-radius: 10px; }
QSpinBox#dailyLimit { background: transparent; color: #263239; border: none; padding: 6px 3px; min-width: 52px; font-size: 13px; font-weight: 600; }
QPushButton#limitStepButton { background: transparent; color: #66767D; border: none; border-radius: 7px; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; font-size: 17px; font-weight: 600; padding: 0; }
QPushButton#limitStepButton:hover { background: #FFF1ED; color: #D45240; }
QPushButton#limitStepButton:pressed { background: #FADFD9; color: #B44334; }
QLabel#studyProgress { color: #D45240; font-size: 18px; font-weight: 700; }
QStackedWidget#studyBody, QFrame#studyEmpty { background: transparent; border: none; }
QLabel#studyEmptyMark { color: #E7B1A8; }
QPushButton#continueStudyButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 10px; padding: 12px 22px; min-width: 190px; font-size: 13px; font-weight: 700; }
QPushButton#continueStudyButton:hover { background: #C94D3C; }
QPushButton#continueStudyButton:pressed { background: #B44334; }
QPushButton#continueStudyButton:disabled { background: #D9E0E3; color: #89969C; }
QFrame#studyCard { background: #FFFFFF; border: 1px solid #DFE4E6; border-radius: 16px; }
QLabel#studyDirection { color: #8A979D; font-size: 10px; font-weight: 700; letter-spacing: 1.2px; }
QLabel#cardQuestion { color: #17242A; }
QPushButton#revealPinyin { background: #EEF2F3; color: #D45240; border: 1px solid #D4DBDE; border-radius: 9px; padding: 10px 18px; font-size: 16px; font-weight: 600; }
QPushButton#showAnswerButton { background: #E05945; color: #FFFFFF; border: none; border-radius: 10px; padding: 12px 28px; font-size: 13px; font-weight: 700; }
QPushButton#showAnswerButton:hover { background: #C94D3C; }
QLabel#cardAnswerPrimary { color: #253239; }
QScrollArea#cardAnswerScroll { background: transparent; border: none; }
QScrollArea#cardAnswerScroll QWidget#qt_scrollarea_viewport, QScrollArea#cardAnswerScroll QLabel { background: transparent; }
QLabel#cardAnswerPinyin { color: #D45240; font-size: 16px; font-weight: 600; }
QPushButton#showExamplesButton { background: #F4F7F8; color: #425159; border: 1px solid #D7DEE1; border-radius: 8px; padding: 8px 14px; }
QPushButton#showExamplesButton:hover { background: #EAF0F2; }
QScrollArea#cardExamplesScroll { background: #F8FAFA; border: 1px solid #E5EAEC; border-radius: 9px; }
QScrollArea#cardExamplesScroll QWidget#qt_scrollarea_viewport, QWidget#cardExamplesContainer { background: #F8FAFA; }
QLabel#cardExamples { color: #536168; font-size: 14px; }
QPushButton#ratingVeryHard, QPushButton#ratingHard, QPushButton#ratingMedium, QPushButton#ratingEasy { border-radius: 9px; padding: 9px 6px; font-size: 11px; font-weight: 700; min-height: 42px; }
QPushButton#ratingVeryHard { background: #FCEAE7; color: #B53F32; border: 1px solid #F0B9B1; }
QPushButton#ratingHard { background: #FFF3E1; color: #A86419; border: 1px solid #EBCB9C; }
QPushButton#ratingMedium { background: #EDF5F7; color: #346B78; border: 1px solid #BDD6DC; }
QPushButton#ratingEasy { background: #EAF5EE; color: #35704C; border: 1px solid #BEDCC9; }
QScrollBar:vertical { background: transparent; width: 8px; margin: 5px 2px; }
QScrollBar::handle:vertical { background: #CDD5D8; border-radius: 4px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""

STYLESHEET += f"""
QDialog#copybookCollectionDialog QSpinBox::up-arrow,
QDialog#spacedRepetitionSettingsDialog QSpinBox::up-arrow,
QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::up-arrow {{
    image: url("{SPINBOX_PLUS_ICON.as_posix()}");
    width: 10px;
    height: 10px;
}}
QDialog#copybookCollectionDialog QSpinBox::down-arrow,
QDialog#spacedRepetitionSettingsDialog QSpinBox::down-arrow,
QDialog#spacedRepetitionSettingsDialog QDoubleSpinBox::down-arrow {{
    image: url("{SPINBOX_MINUS_ICON.as_posix()}");
    width: 10px;
    height: 10px;
}}
QComboBox#fontSelector::drop-down,
QComboBox#dictionarySource::drop-down {{
    background: #203138;
    border: none;
    border-left: 1px solid #354A52;
    border-top-right-radius: 8px;
    border-bottom-right-radius: 8px;
    width: 30px;
}}
QComboBox#fontSelector::drop-down:hover,
QComboBox#dictionarySource::drop-down:hover {{
    background: #2A4048;
}}
QComboBox#fontSelector::down-arrow,
QComboBox#dictionarySource::down-arrow {{
    image: url("{SIDEBAR_DROPDOWN_ICON.as_posix()}");
    width: 10px;
    height: 6px;
}}
"""


def main() -> None:
    smoke_test = "--smoke-test" in sys.argv
    server_check = "--server-check" in sys.argv
    download_check = "--download-check" in sys.argv
    if server_check:
        if not dictionary_remote.enabled():
            raise SystemExit("Server configuration is missing")
        stats = dictionary_remote.call("stats")
        if int(stats.get("entries", 0)) <= 0:
            raise SystemExit("Dictionary server returned an empty database")
        return
    if download_check:
        dictionary_remote.check_dictionary_download()
        return
    if not smoke_test and not dictionary_remote.enabled() and not DB_PATH.exists():
        raise SystemExit("В комплекте приложения не найдена база HanziLab")
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                APP_USER_MODEL_ID
            )
        except (AttributeError, OSError):
            pass
    app = QApplication(sys.argv)
    app.setApplicationName("HanziLab")
    app.setOrganizationName("HanziLab")
    app.setWindowIcon(QIcon(str(APP_ICON_FILE)))
    window_centering_filter = WindowCenteringFilter(app)
    app.installEventFilter(window_centering_filter)
    if smoke_test:
        return
    app.setStyle("Fusion")
    load_chinese_fonts()
    app.setFont(QFont(RUSSIAN_FONT_FAMILY, 10))
    app.setStyleSheet(STYLESHEET)
    if updates.is_update_in_progress():
        QMessageBox.information(
            None,
            "HanziLab обновляется",
            "Уже идёт установка новой версии.\n\n"
            "После завершения HanziLab откроется автоматически.",
        )
        return
    window = HanziLabWindow()
    window.show()
    window.schedule_search_warmup()
    window.schedule_update_check()
    raise SystemExit(app.exec())


if __name__ == "__main__":
    main()
