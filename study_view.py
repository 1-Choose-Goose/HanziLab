from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QApplication,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anki_import import parse_anki_export
from card_widgets import RevealPinyinButton
from database import get_entries_by_hanzi
from scheduler import (
    Rating,
    ReviewDirection,
    format_interval,
    preview_ratings,
)
from study_database import StudyRepository, utc_now
from study_session import SessionItem, StudySessionService
from text_formatting import (
    card_translation_without_pinyin,
    format_example_blocks,
    normalize_display_text,
)

RATING_KEYS = {
    Rating.VERY_HARD: "1",
    Rating.HARD: "2",
    Rating.MEDIUM: "3",
    Rating.EASY: "4",
}

RATING_LABELS = {
    Rating.VERY_HARD: "Очень тяжело",
    Rating.HARD: "Тяжело",
    Rating.MEDIUM: "Средне",
    Rating.EASY: "Легко",
}

INPUT_KAITI_FAMILY = "HanziLab KaiTi CJK"


class DictionaryLookupSignals(QObject):
    finished = Signal(int, object, object)


class DictionaryLookupTask(QRunnable):
    def __init__(self, generation: int, words) -> None:
        super().__init__()
        self.setAutoDelete(False)
        self.generation = generation
        self.words = tuple(words)
        self.cancelled = False
        self.signals = DictionaryLookupSignals()

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        if self.cancelled:
            self.signals.finished.emit(
                self.generation, None, RuntimeError("dictionary lookup cancelled")
            )
            return
        try:
            result = get_entries_by_hanzi(self.words)
            error = None
        except Exception as exception:  # noqa: BLE001 - граница фоновой задачи.
            result = None
            error = exception
        if self.cancelled:
            result = None
            error = RuntimeError("dictionary lookup cancelled")
        self.signals.finished.emit(self.generation, result, error)


class ManualCardDialog(QDialog):
    card_added = Signal(str)
    priority_raised = Signal(str)

    def __init__(
        self,
        repository: StudyRepository,
        russian_font_family: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("manualCardDialog")
        self.setWindowTitle("Новая карточка")
        self.setModal(True)
        self.setMinimumWidth(470)
        self.repository = repository
        self.lookup_generation = 0
        self.lookup_tasks: set[DictionaryLookupTask] = set()
        self.lookup_timer = QTimer(self)
        self.lookup_timer.setSingleShot(True)
        self.lookup_timer.setInterval(280)
        self.lookup_timer.timeout.connect(self.start_dictionary_lookup)
        self.autofilled = False
        self.duplicate = False
        self.resetting_form = False

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 22)
        root.setSpacing(16)
        title = QLabel("Добавить карточку вручную")
        title.setObjectName("manualCardTitle")
        subtitle = QLabel("Карточка будет независима от словаря HanziLab")
        subtitle.setObjectName("manualCardSubtitle")
        root.addWidget(title)
        root.addWidget(subtitle)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(13)
        self.hanzi_input = QLineEdit()
        self.hanzi_input.setObjectName("manualCardHanzi")
        self.hanzi_input.setPlaceholderText("例如：学习")
        # Поля ввода иероглифов всегда используют KaiTi независимо от
        # выбранного шрифта словарных статей.
        self.hanzi_input.setFont(QFont(INPUT_KAITI_FAMILY, 18))
        self.hanzi_input.textChanged.connect(self.schedule_dictionary_lookup)
        self.pinyin_input = QLineEdit()
        self.pinyin_input.setObjectName("manualCardInput")
        self.pinyin_input.setPlaceholderText("Например: xué xí")
        self.pinyin_input.setFont(QFont(russian_font_family, 12))
        self.translation_input = QPlainTextEdit()
        self.translation_input.setObjectName("manualCardTranslation")
        self.translation_input.setPlaceholderText("Русский перевод")
        self.translation_input.setFont(QFont(russian_font_family, 12))
        self.translation_input.setMaximumHeight(105)
        form.addRow("Китайское слово", self.hanzi_input)
        form.addRow("Пиньинь", self.pinyin_input)
        form.addRow("Перевод", self.translation_input)
        root.addLayout(form)

        self.dictionary_status = QLabel(
            "Введите слово — HanziLab попробует заполнить данные из словаря."
        )
        self.dictionary_status.setTextFormat(Qt.TextFormat.PlainText)
        self.dictionary_status.setObjectName("manualCardStatus")
        self.dictionary_status.setWordWrap(True)
        root.addWidget(self.dictionary_status)

        self.priority_button = QPushButton("Повысить приоритет")
        self.priority_button.setObjectName("manualCardPriorityButton")
        self.priority_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.priority_button.setVisible(False)
        self.priority_button.clicked.connect(self.raise_existing_priority)
        root.addWidget(
            self.priority_button, alignment=Qt.AlignmentFlag.AlignLeft
        )

        self.validation_error = QLabel()
        self.validation_error.setObjectName("manualCardError")
        self.validation_error.setVisible(False)
        root.addWidget(self.validation_error)

        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addStretch(1)
        self.cancel_button = QPushButton("Отмена")
        self.cancel_button.setObjectName("manualCardCancelButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.clicked.connect(self.reject)
        self.save_button = QPushButton("Добавить карточку")
        self.save_button.setObjectName("manualCardSaveButton")
        self.save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self.validate_and_add)
        button_row.addWidget(self.cancel_button)
        button_row.addWidget(self.save_button)
        root.addLayout(button_row)

    def schedule_dictionary_lookup(self, text: str) -> None:
        if self.resetting_form:
            return
        self.lookup_timer.stop()
        self.lookup_generation += 1
        for task in tuple(self.lookup_tasks):
            task.cancel()
        self.validation_error.setVisible(False)
        if self.autofilled:
            self.pinyin_input.clear()
            self.translation_input.clear()
            self.autofilled = False
        word = normalize_display_text(text, preserve_line_breaks=False)
        if word and self.repository.has_card(word):
            self.show_duplicate(word)
            return
        self.set_duplicate_state(False)
        self.priority_button.setVisible(False)
        self.save_button.setEnabled(True)
        if word:
            self.set_status("Ищу слово в словаре HanziLab…")
            self.save_button.setEnabled(False)
            self.lookup_timer.start()
        else:
            self.set_status(
                "Введите слово — HanziLab попробует заполнить данные из словаря."
            )

    def set_duplicate_state(self, duplicate: bool) -> None:
        self.duplicate = duplicate
        self.hanzi_input.setProperty("duplicate", duplicate)
        self.hanzi_input.style().unpolish(self.hanzi_input)
        self.hanzi_input.style().polish(self.hanzi_input)

    def set_status(self, text: str, status: str = "") -> None:
        self.dictionary_status.setText(text)
        self.dictionary_status.setProperty("status", status)
        self.dictionary_status.style().unpolish(self.dictionary_status)
        self.dictionary_status.style().polish(self.dictionary_status)

    def show_duplicate(self, word: str) -> None:
        self.set_duplicate_state(True)
        self.save_button.setEnabled(False)
        can_raise = self.repository.can_raise_card_priority(word)
        self.priority_button.setVisible(can_raise)
        self.priority_button.setEnabled(can_raise)
        self.priority_button.setText("Повысить приоритет")
        if can_raise:
            message = (
                "Это слово уже есть в карточках. При необходимости его можно "
                "вернуть в приоритетное изучение."
            )
        else:
            message = (
                "Это слово уже есть в карточках и уже имеет высокий приоритет "
                "либо изучается сейчас."
            )
        self.set_status(message, "duplicate")

    def raise_existing_priority(self) -> None:
        word = self.card_data()[0]
        if not word:
            return
        if not self.repository.raise_card_priority(word):
            self.show_duplicate(word)
            return
        self.priority_button.setVisible(True)
        self.priority_button.setEnabled(False)
        self.priority_button.setText("Приоритет повышен")
        self.set_status(
            "Приоритет повышен: карточка появится раньше обычных повторений.",
            "success",
        )
        self.priority_raised.emit(word)

    def start_dictionary_lookup(self) -> None:
        word = self.card_data()[0]
        if not word:
            return
        if self.repository.has_card(word):
            self.show_duplicate(word)
            return
        generation = self.lookup_generation
        task = DictionaryLookupTask(generation, [word])
        self.lookup_tasks.add(task)
        task.signals.finished.connect(self.apply_dictionary_lookup)
        task.signals.finished.connect(
            lambda *_arguments, worker=task: self.lookup_tasks.discard(worker)
        )
        QThreadPool.globalInstance().start(task)

    def apply_dictionary_lookup(
        self, generation: int, rows: object, error: object
    ) -> None:
        if generation != self.lookup_generation:
            return
        self.save_button.setEnabled(True)
        word = self.card_data()[0]
        if self.repository.has_card(word):
            self.show_duplicate(word)
            return
        if error is not None:
            self.set_status(
                "Не удалось проверить словарь. Поля можно заполнить вручную."
            )
            return
        entry = dict(rows or {}).get(word)
        if entry is None:
            self.set_status(
                "Слово не найдено в словаре. Заполните пиньинь и перевод вручную."
            )
            return
        self.pinyin_input.setText(
            normalize_display_text(entry["pinyin"], preserve_line_breaks=False)
        )
        self.translation_input.setPlainText(
            normalize_display_text(entry["translation"])
        )
        self.autofilled = True
        self.set_status("Данные загружены из словаря HanziLab.")

    def validate_and_add(self) -> None:
        hanzi, pinyin, translation = self.card_data()
        if hanzi and self.repository.has_card(hanzi):
            self.show_duplicate(hanzi)
            return
        if not all((hanzi, pinyin, translation)):
            self.validation_error.setText("Заполните китайское слово, пиньинь и перевод.")
            self.validation_error.setVisible(True)
            return
        if not self.repository.add_card(hanzi, pinyin, translation):
            self.show_duplicate(hanzi)
            return
        self.card_added.emit(hanzi)
        self.reset_after_add(hanzi)

    def reset_after_add(self, added_hanzi: str) -> None:
        self.lookup_generation += 1
        self.lookup_timer.stop()
        for task in tuple(self.lookup_tasks):
            task.cancel()
        self.resetting_form = True
        self.hanzi_input.clear()
        self.pinyin_input.clear()
        self.translation_input.clear()
        self.resetting_form = False
        self.autofilled = False
        self.set_duplicate_state(False)
        self.priority_button.setVisible(False)
        self.validation_error.setVisible(False)
        self.save_button.setEnabled(True)
        self.set_status(
            f"Карточка «{added_hanzi}» добавлена. Можно вводить следующее слово.",
            "success",
        )
        self.hanzi_input.setFocus()

    def card_data(self) -> tuple[str, str, str]:
        return (
            normalize_display_text(
                self.hanzi_input.text(), preserve_line_breaks=False
            ),
            normalize_display_text(
                self.pinyin_input.text(), preserve_line_breaks=False
            ),
            normalize_display_text(self.translation_input.toPlainText()),
        )

    def done(self, result: int) -> None:
        self.lookup_generation += 1
        self.lookup_timer.stop()
        for task in tuple(self.lookup_tasks):
            task.cancel()
        super().done(result)


class ExampleLoadSignals(QObject):
    finished = Signal(int, object, object, object)


class ExampleLoadCancelled(Exception):
    pass


class ExampleLoadTask(QRunnable):
    """Loads examples without blocking Qt's GUI thread."""

    def __init__(
        self,
        generation: int,
        key: tuple[str, str],
        loader: Callable[[str, int, str], list[dict]],
    ) -> None:
        super().__init__()
        # Python keeps the runnable until its signal is delivered and the page
        # removes it from ``example_tasks``.
        self.setAutoDelete(False)
        self.generation = generation
        self.key = key
        self.loader = loader
        self.signals = ExampleLoadSignals()
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True

    @Slot()
    def run(self) -> None:
        if self.cancelled:
            self.signals.finished.emit(
                self.generation, self.key, None, ExampleLoadCancelled()
            )
            return
        try:
            examples = self.loader(self.key[0], 6, self.key[1])
            error = None
        except Exception as exception:  # noqa: BLE001 - граница фоновой задачи.
            examples = None
            error = exception
        if self.cancelled:
            examples = None
            error = ExampleLoadCancelled()
        self.signals.finished.emit(self.generation, self.key, examples, error)


class CardListDialog(QDialog):
    def __init__(
        self,
        repository: StudyRepository,
        chinese_font_family: str,
        russian_font_family: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cardListDialog")
        self.setWindowTitle("Все карточки")
        self.resize(820, 560)
        self.setMinimumSize(620, 420)

        cards = repository.get_cards()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        heading = QLabel("Все карточки")
        heading.setObjectName("cardListTitle")
        count = QLabel(f"Сохранено карточек: {len(cards)}")
        count.setObjectName("cardListCount")
        layout.addWidget(heading)
        layout.addWidget(count)

        self.table = QTableWidget(len(cards), 3)
        self.table.setObjectName("cardListTable")
        self.table.setHorizontalHeaderLabels(("Слово", "Пиньинь", "Перевод"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(48)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)

        for row, card in enumerate(cards):
            values = (card.hanzi, card.pinyin, card.translation)
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setFont(
                    QFont(chinese_font_family, 16)
                    if column == 0
                    else QFont(russian_font_family, 11)
                )
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        layout.addWidget(self.table, 1)

        close_button = QPushButton("Закрыть")
        close_button.setObjectName("cardListCloseButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)


class StudyPage(QWidget):
    card_count_changed = Signal(int)
    cards_changed = Signal()

    def __init__(
        self,
        repository: StudyRepository,
        example_loader: Callable[[str, int, str], list[dict]],
        chinese_font_family: str,
        russian_font_family: str,
        pinyin_formatter: Callable[[str, str], str],
    ) -> None:
        super().__init__()
        self.setObjectName("studyPage")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.repository = repository
        self.session = StudySessionService(repository)
        self.known_card_count = repository.get_card_count()
        self.example_loader = example_loader
        self.chinese_font_family = chinese_font_family
        self.russian_font_family = russian_font_family
        self.pinyin_formatter = pinyin_formatter
        self.current_item: SessionItem | None = None
        self.example_generation = 0
        self.example_pool = QThreadPool(self)
        self.example_pool.setMaxThreadCount(1)
        self.example_tasks: set[ExampleLoadTask] = set()
        self.dictionary_tasks: set[DictionaryLookupTask] = set()
        self.anki_import_generation = 0
        self.pending_anki_import = None
        self.example_cache: dict[tuple[str, str], list[dict]] = {}
        self.current_example_key: tuple[str, str] | None = None
        self.current_examples: list[dict] | None = None
        self.current_examples_error: Exception | None = None
        self.examples_requested_visible = False
        self.session_active = False
        self.deactivated_at = None
        self.shutting_down = False

        root = QVBoxLayout(self)
        root.setContentsMargins(38, 30, 38, 32)
        root.setSpacing(18)

        header = QHBoxLayout()
        headings = QVBoxLayout()
        heading = QLabel("Карточки HanziLab")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("Система интервального повторения")
        subtitle.setObjectName("pageSubtitle")
        headings.addWidget(heading)
        headings.addWidget(subtitle)
        header.addLayout(headings)
        header.addStretch()

        self.import_anki_button = QPushButton("Импорт карточек")
        self.import_anki_button.setObjectName("importCardsButton")
        self.import_anki_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_anki_button.setToolTip("Импортировать текстовый экспорт Anki")
        self.import_anki_button.clicked.connect(self.import_anki_cards)
        self.card_list_button = QPushButton("Все карточки")
        self.card_list_button.setObjectName("cardListButton")
        self.card_list_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.card_list_button.setToolTip("Показать список всех сохранённых карточек")
        self.card_list_button.clicked.connect(self.show_card_list)
        self.manual_card_button = QPushButton("Новая карточка")
        self.manual_card_button.setObjectName("manualCardButton")
        self.manual_card_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manual_card_button.clicked.connect(self.add_manual_card)

        limit_label = QLabel("Карточек в день")
        limit_label.setObjectName("studySettingLabel")
        self.daily_limit = QSpinBox()
        self.daily_limit.setObjectName("dailyLimit")
        self.daily_limit.setRange(1, 1000)
        self.daily_limit.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.daily_limit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.daily_limit.setValue(repository.get_daily_limit())
        self.daily_limit.valueChanged.connect(self.change_daily_limit)
        limit_control = QFrame()
        limit_control.setObjectName("limitControl")
        self.limit_control = limit_control
        limit_layout = QHBoxLayout(limit_control)
        limit_layout.setContentsMargins(3, 3, 3, 3)
        limit_layout.setSpacing(2)
        decrease = QPushButton("−")
        decrease.setObjectName("limitStepButton")
        decrease.setCursor(Qt.CursorShape.PointingHandCursor)
        decrease.clicked.connect(self.daily_limit.stepDown)
        increase = QPushButton("+")
        increase.setObjectName("limitStepButton")
        increase.setCursor(Qt.CursorShape.PointingHandCursor)
        increase.clicked.connect(self.daily_limit.stepUp)
        limit_layout.addWidget(decrease)
        limit_layout.addWidget(self.daily_limit)
        limit_layout.addWidget(increase)
        header.addWidget(limit_label)
        header.addWidget(limit_control)
        root.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.manual_card_button)
        actions.addWidget(self.card_list_button)
        actions.addWidget(self.import_anki_button)
        actions.addStretch()
        root.addLayout(actions)

        progress_line = QHBoxLayout()
        self.progress_label = QLabel("0 / 30")
        self.progress_label.setObjectName("studyProgress")
        self.queue_label = QLabel()
        self.queue_label.setObjectName("studyQueueInfo")
        progress_line.addWidget(self.progress_label)
        progress_line.addStretch()
        progress_line.addWidget(self.queue_label)
        root.addLayout(progress_line)

        self.body = QStackedWidget()
        self.body.setObjectName("studyBody")
        self.body.addWidget(self.create_empty_state())
        self.body.addWidget(self.create_card_view())
        root.addWidget(self.body, 1)

        self.answer_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.open_answer_from_keyboard)
            self.answer_shortcuts.append(shortcut)
        self.pinyin_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Space, Qt.Key.Key_0):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.open_pinyin_from_keyboard)
            self.pinyin_shortcuts.append(shortcut)
        self.rating_shortcuts: dict[Rating, QShortcut] = {}
        for rating, key in RATING_KEYS.items():
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(
                lambda selected_rating=rating: self.rate_from_keyboard(selected_rating)
            )
            self.rating_shortcuts[rating] = shortcut
        application = QApplication.instance()
        if application is not None:
            application.focusChanged.connect(self.update_shortcut_scope)

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setInterval(30_000)
        self.refresh_timer.timeout.connect(self.refresh_if_waiting)
        self.refresh_timer.start()
        # Activation is deferred by one event-loop turn so the first visible
        # card receives the same polished Qt styles as subsequent cards.
        self.initial_refresh_timer = QTimer(self)
        self.initial_refresh_timer.setSingleShot(True)
        self.initial_refresh_timer.timeout.connect(self.refresh)

    def create_empty_state(self) -> QWidget:
        empty = QFrame()
        empty.setObjectName("studyEmpty")
        layout = QVBoxLayout(empty)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark = QLabel("习")
        self.empty_mark = mark
        mark.setObjectName("studyEmptyMark")
        mark.setFont(QFont(self.chinese_font_family, 54))
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("Добавьте слова из словаря")
        self.empty_title.setObjectName("emptyTitle")
        self.empty_text = QLabel(
            "Для доступных сегодня карточек здесь появится учебная сессия."
        )
        self.empty_text.setObjectName("emptyText")
        self.empty_text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.continue_study_button = QPushButton("Продолжить обучение")
        self.continue_study_button.setObjectName("continueStudyButton")
        self.continue_study_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.continue_study_button.clicked.connect(self.continue_studying)
        self.continue_study_button.setVisible(False)
        layout.addWidget(mark)
        layout.addSpacing(8)
        layout.addWidget(self.empty_title, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_text, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(10)
        layout.addWidget(
            self.continue_study_button,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        return empty

    def create_card_view(self) -> QWidget:
        shell = QWidget()
        outer = QVBoxLayout(shell)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.card_frame = QFrame()
        self.card_frame.setObjectName("studyCard")
        self.card_frame.setMinimumWidth(520)
        self.card_frame.setMaximumWidth(760)
        card_layout = QVBoxLayout(self.card_frame)
        card_layout.setContentsMargins(42, 32, 42, 30)
        card_layout.setSpacing(18)

        self.direction_label = QLabel()
        self.direction_label.setObjectName("studyDirection")
        self.direction_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        card_layout.addWidget(self.direction_label)

        self.card_sides = QStackedWidget()
        self.card_sides.setObjectName("cardSides")
        self.card_sides.addWidget(self.create_question_side())
        self.card_sides.addWidget(self.create_answer_side())
        card_layout.addWidget(self.card_sides, 1)

        self.rating_area = QWidget()
        rating_layout = QHBoxLayout(self.rating_area)
        rating_layout.setContentsMargins(0, 0, 0, 0)
        rating_layout.setSpacing(8)
        self.rating_buttons: dict[Rating, QPushButton] = {}
        objects = {
            Rating.VERY_HARD: "ratingVeryHard",
            Rating.HARD: "ratingHard",
            Rating.MEDIUM: "ratingMedium",
            Rating.EASY: "ratingEasy",
        }
        for rating in Rating:
            button = QPushButton(RATING_LABELS[rating])
            button.setObjectName(objects[rating])
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(f"Горячая клавиша: {RATING_KEYS[rating]}")
            button.clicked.connect(lambda _checked=False, value=rating: self.rate(value))
            rating_layout.addWidget(button, 1)
            self.rating_buttons[rating] = button
        self.rating_area.setVisible(False)
        card_layout.addWidget(self.rating_area)
        outer.addWidget(self.card_frame)
        return shell

    def create_question_side(self) -> QWidget:
        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(16)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.question = QLabel()
        self.question.setTextFormat(Qt.TextFormat.PlainText)
        self.question.setObjectName("cardQuestion")
        self.question.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.question.setWordWrap(True)
        self.hidden_pinyin = RevealPinyinButton()
        self.hidden_pinyin.setFont(QFont(self.russian_font_family, 13))
        self.show_answer_button = QPushButton("Показать ответ")
        self.show_answer_button.setObjectName("showAnswerButton")
        self.show_answer_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_answer_button.clicked.connect(self.show_answer)
        layout.addStretch()
        layout.addWidget(self.question)
        layout.addWidget(self.hidden_pinyin, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addSpacing(12)
        layout.addWidget(self.show_answer_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()
        return side

    def create_answer_side(self) -> QWidget:
        side = QWidget()
        layout = QVBoxLayout(side)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)
        self.answer_primary = QLabel()
        self.answer_primary.setTextFormat(Qt.TextFormat.PlainText)
        self.answer_primary.setObjectName("cardAnswerPrimary")
        self.answer_primary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.answer_primary.setWordWrap(True)
        self.answer_primary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.answer_primary_scroll = QScrollArea()
        self.answer_primary_scroll.setObjectName("cardAnswerScroll")
        self.answer_primary_scroll.setWidgetResizable(True)
        self.answer_primary_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.answer_primary_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.answer_primary_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.answer_primary_scroll.setMinimumHeight(82)
        self.answer_primary_scroll.setMaximumHeight(220)
        self.answer_primary_scroll.setWidget(self.answer_primary)
        self.answer_pinyin = QLabel()
        self.answer_pinyin.setTextFormat(Qt.TextFormat.PlainText)
        self.answer_pinyin.setObjectName("cardAnswerPinyin")
        self.answer_pinyin.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.show_examples_button = QPushButton("Показать примеры")
        self.show_examples_button.setObjectName("showExamplesButton")
        self.show_examples_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_examples_button.clicked.connect(self.toggle_examples)
        self.card_examples = QLabel()
        self.card_examples.setObjectName("cardExamples")
        self.card_examples.setWordWrap(True)
        self.card_examples.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.card_examples.setVisible(False)

        examples_scroll = QScrollArea()
        examples_scroll.setObjectName("cardExamplesScroll")
        examples_scroll.setWidgetResizable(True)
        examples_scroll.setFrameShape(QFrame.Shape.NoFrame)
        examples_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        examples_container = QWidget()
        examples_container.setObjectName("cardExamplesContainer")
        examples_layout = QVBoxLayout(examples_container)
        examples_layout.setContentsMargins(4, 4, 4, 4)
        examples_layout.addWidget(self.card_examples)
        examples_layout.addStretch()
        examples_scroll.setWidget(examples_container)
        examples_scroll.setMinimumHeight(150)
        examples_scroll.setVisible(False)
        self.examples_scroll = examples_scroll

        layout.addWidget(self.answer_primary_scroll)
        layout.addWidget(self.answer_pinyin)
        layout.addWidget(self.show_examples_button, alignment=Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(examples_scroll, 1)
        return side

    def set_chinese_font(self, family: str) -> None:
        self.chinese_font_family = family
        self.empty_mark.setFont(QFont(family, 54))
        self.refresh_current_visuals()

    def chinese_card_font(self, text: str) -> QFont:
        """Крупный выбранный китайский шрифт с защитой от переполнения."""
        character_count = max(1, len(text.strip()))
        if character_count <= 4:
            size = 64
        elif character_count <= 8:
            size = 52
        elif character_count <= 14:
            size = 42
        else:
            size = 32
        return QFont(self.chinese_font_family, size, QFont.Weight.Normal)

    def russian_card_font(self, text: str) -> QFont:
        """Одинаковый адаптивный размер русского текста на обеих сторонах."""
        text_length = len(text.strip())
        if text_length <= 80:
            size = 22
        elif text_length <= 180:
            size = 19
        elif text_length <= 300:
            size = 17
        else:
            size = 15
        return QFont(self.russian_font_family, size, QFont.Weight.Normal)

    def change_daily_limit(self, value: int) -> None:
        self.repository.set_daily_limit(value)
        self.refresh()

    def import_anki_cards(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Импорт карточек Anki",
            str(Path.home()),
            "Текстовый экспорт Anki (*.txt *.tsv *.csv);;Все файлы (*)",
        )
        if not path:
            return
        try:
            result = parse_anki_export(path)
        except (OSError, UnicodeError, ValueError, csv.Error) as exception:
            QMessageBox.warning(
                self,
                "Не удалось импортировать карточки",
                f"Файл не похож на текстовый экспорт Anki.\n{exception}",
            )
            return

        self.anki_import_generation += 1
        generation = self.anki_import_generation
        self.pending_anki_import = result
        self.import_anki_button.setEnabled(False)
        self.import_anki_button.setText("Проверяю слова…")
        task = DictionaryLookupTask(
            generation, (card.hanzi for card in result.cards)
        )
        self.dictionary_tasks.add(task)
        task.signals.finished.connect(self.finish_anki_import)
        task.signals.finished.connect(
            lambda *_arguments, worker=task: self.dictionary_tasks.discard(worker)
        )
        self.example_pool.start(task)

    def finish_anki_import(
        self, generation: int, rows: object, error: object
    ) -> None:
        if self.shutting_down or generation != self.anki_import_generation:
            return
        self.import_anki_button.setEnabled(True)
        self.import_anki_button.setText("Импорт карточек")
        result = self.pending_anki_import
        self.pending_anki_import = None
        if error is not None or result is None:
            QMessageBox.warning(
                self,
                "Не удалось проверить слова",
                f"Словарь HanziLab недоступен.\n{error or 'Неизвестная ошибка'}",
            )
            return

        dictionary_entries = dict(rows or {})
        added = self.repository.add_cards(
            (
                entry["hanzi"],
                entry["pinyin"],
                entry["translation"],
            )
            for entry in dictionary_entries.values()
        )

        missing = [
            card.hanzi
            for card in result.cards
            if card.hanzi not in dictionary_entries
        ]
        existing = len(dictionary_entries) - added
        self.refresh()
        if added:
            self.cards_changed.emit()
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Information)
        message.setWindowTitle("Импорт завершён")
        message.setText(
            "\n".join(
                (
                    f"Добавлено карточек: {added}",
                    f"Уже были в HanziLab: {existing}",
                    f"Не найдены в словаре HanziLab: {len(missing)}",
                    f"Дубли внутри файла: {result.duplicate_rows}",
                    f"Пропущено некорректных строк: {result.invalid_rows}",
                )
            )
        )
        if missing:
            message.setInformativeText(
                "Список ненайденных слов доступен по кнопке «Показать подробности»."
            )
            message.setDetailedText("Не найдены в словаре HanziLab:\n" + "\n".join(missing))
        message.exec()

    def add_manual_card(self) -> None:
        dialog = ManualCardDialog(
            self.repository,
            self.russian_font_family,
            self,
        )
        dialog.card_added.connect(self.manual_card_was_added)
        dialog.priority_raised.connect(self.manual_priority_was_raised)
        dialog.exec()

    def show_card_list(self) -> None:
        dialog = CardListDialog(
            self.repository,
            self.chinese_font_family,
            self.russian_font_family,
            self,
        )
        dialog.exec()

    def manual_card_was_added(self, _hanzi: str) -> None:
        self.refresh()
        self.cards_changed.emit()

    def manual_priority_was_raised(self, _hanzi: str) -> None:
        now = utc_now()
        self.session.invalidate_daily_priority(now)
        self.refresh()

    def refresh(self) -> None:
        count = self.repository.get_card_count()
        now = utc_now()
        if count != self.known_card_count:
            self.known_card_count = count
            self.session.invalidate_daily_priority(now)
        self.card_count_changed.emit(count)
        completed, limit = self.session.progress(now)
        self.progress_label.setText(f"{completed} / {limit}")
        if self.session_active and self.current_item is None:
            self.load_next_card(now)

    def refresh_if_waiting(self) -> None:
        if self.session_active and self.current_item is None:
            self.refresh()

    def continue_studying(self) -> None:
        """Extend only today's queue, leaving the daily preference unchanged."""
        now = utc_now()
        self.continue_study_button.setEnabled(False)
        added = self.session.continue_daily_session(now)
        self.continue_study_button.setEnabled(True)
        if added:
            self.current_item = None
            self.load_next_card(now)
        else:
            self.continue_study_button.setVisible(False)
            self.empty_title.setText("На сейчас карточек нет")
            self.empty_text.setText(
                "Оставшиеся карточки появятся, когда наступит их время."
            )

    def activate(self) -> None:
        if self.shutting_down:
            return
        self.session_active = True
        if self.current_item is not None:
            now = utc_now()
            if self.deactivated_at is not None:
                self.current_item = replace(
                    self.current_item,
                    shown_at=self.current_item.shown_at + (now - self.deactivated_at),
                )
            self.deactivated_at = None
            return
        self.deactivated_at = None
        self.initial_refresh_timer.start(0)

    def deactivate(self) -> None:
        if not self.session_active:
            return
        self.session_active = False
        self.deactivated_at = utc_now()
        self.initial_refresh_timer.stop()

    def shutdown(self) -> None:
        self.deactivate()
        self.shutting_down = True
        self.initial_refresh_timer.stop()
        self.refresh_timer.stop()
        self.example_generation += 1
        self.anki_import_generation += 1
        self.pending_anki_import = None
        for task in self.example_tasks:
            task.cancel()
        for task in self.dictionary_tasks:
            task.cancel()
        # Remove work that has not started. Already running SQLite reads are
        # allowed to finish; their generation is invalid and cannot touch UI.
        self.example_pool.clear()

    def card_was_removed(self, hanzi: str) -> None:
        if self.current_item and self.current_item.card.hanzi == hanzi:
            self.invalidate_examples()
            self.current_item = None
        self.refresh()

    def load_next_card(self, now=None) -> None:
        now = now or utc_now()
        item = self.session.next_item(now)
        self.current_item = item
        completed, limit = self.session.progress(now)
        self.progress_label.setText(f"{completed} / {limit}")
        if item is None:
            count = self.repository.get_card_count()
            can_continue = (
                count > 0
                and completed >= limit
                and self.session.can_continue(now)
            )
            self.continue_study_button.setVisible(can_continue)
            if can_continue:
                self.empty_title.setText("План на сегодня выполнен")
                self.empty_text.setText(
                    "Можно продолжить и взять ещё одну порцию доступных карточек."
                )
            elif count:
                self.empty_title.setText("На сейчас повторений нет")
                self.empty_text.setText(
                    "Следующая карточка появится, когда наступит запланированное время."
                )
            else:
                self.empty_title.setText("Добавьте слова из словаря")
                self.empty_text.setText(
                    "Для доступных сегодня карточек здесь появится учебная сессия."
                )
            self.queue_label.setText(f"Всего карточек: {count}")
            self.body.setCurrentIndex(0)
            return
        self.continue_study_button.setVisible(False)
        if item.is_relearning_repeat:
            self.queue_label.setText(f"Повтор карточки · прогресс {completed} / {limit}")
        else:
            self.queue_label.setText(f"Карточка {min(completed + 1, limit)} из {limit}")
        self.prepare_examples(item)
        self.show_item(item)
        self.body.setCurrentIndex(1)

    def show_item(self, item: SessionItem) -> None:
        self.card_sides.setCurrentIndex(0)
        self.rating_area.setVisible(False)
        self.card_examples.setVisible(False)
        self.examples_scroll.setVisible(False)
        self.show_examples_button.setEnabled(True)
        self.show_examples_button.setText("Показать примеры")
        hanzi = normalize_display_text(
            item.card.hanzi, preserve_line_breaks=False
        )
        pinyin_source = normalize_display_text(
            item.card.pinyin, preserve_line_breaks=False
        )
        translation = card_translation_without_pinyin(item.card.translation, item.card.pinyin)
        pinyin = self.pinyin_formatter(hanzi, pinyin_source)
        self.hidden_pinyin.set_pinyin(pinyin)
        if item.direction == ReviewDirection.CHINESE_TO_RUSSIAN:
            self.direction_label.setText("КИТАЙСКИЙ → РУССКИЙ")
            self.question.setText(hanzi)
            self.question.setFont(self.chinese_card_font(hanzi))
            self.hidden_pinyin.setVisible(True)
        else:
            self.direction_label.setText("РУССКИЙ → КИТАЙСКИЙ")
            self.question.setText(translation)
            self.question.setFont(self.russian_card_font(translation))
            self.hidden_pinyin.setVisible(False)

    def show_answer(self) -> None:
        if not self.current_item:
            return
        card = self.current_item.card
        hanzi = normalize_display_text(card.hanzi, preserve_line_breaks=False)
        pinyin_source = normalize_display_text(
            card.pinyin, preserve_line_breaks=False
        )
        translation = card_translation_without_pinyin(card.translation, card.pinyin)
        pinyin = self.pinyin_formatter(hanzi, pinyin_source)
        if self.current_item.direction == ReviewDirection.CHINESE_TO_RUSSIAN:
            self.answer_primary.setText(translation)
            self.answer_primary.setFont(self.russian_card_font(translation))
            self.answer_primary_scroll.setMaximumHeight(220)
            self.answer_pinyin.clear()
        else:
            self.answer_primary.setText(hanzi)
            self.answer_primary.setFont(self.chinese_card_font(hanzi))
            self.answer_primary_scroll.setMaximumHeight(140)
            self.answer_pinyin.setText(pinyin)
            self.answer_pinyin.setFont(QFont(self.russian_font_family, 15))
        self.card_sides.setCurrentIndex(1)
        self.rating_area.setVisible(True)
        previews = preview_ratings(card, utc_now())
        for rating, button in self.rating_buttons.items():
            button.setText(f"{RATING_LABELS[rating]}\n{format_interval(previews[rating])}")

    def open_answer_from_keyboard(self) -> None:
        if (
            self.current_item is not None
            and self.body.currentIndex() == 1
            and self.card_sides.currentIndex() == 0
        ):
            self.show_answer()

    def update_shortcut_scope(self, _old_widget: QWidget | None, new_widget: QWidget | None) -> None:
        """Lets the daily-limit editor receive digits, Enter and Space normally."""
        editing_limit = bool(
            new_widget
            and (
                new_widget is self.limit_control
                or self.limit_control.isAncestorOf(new_widget)
            )
        )
        enabled = not editing_limit
        for shortcut in self.answer_shortcuts:
            shortcut.setEnabled(enabled)
        for shortcut in self.pinyin_shortcuts:
            shortcut.setEnabled(enabled)
        for shortcut in self.rating_shortcuts.values():
            shortcut.setEnabled(enabled)

    def open_pinyin_from_keyboard(self) -> None:
        if (
            self.current_item is not None
            and self.current_item.direction == ReviewDirection.CHINESE_TO_RUSSIAN
            and self.body.currentIndex() == 1
            and self.card_sides.currentIndex() == 0
            and self.hidden_pinyin.isVisible()
        ):
            self.hidden_pinyin.reveal()

    def rate_from_keyboard(self, rating: Rating) -> None:
        if (
            self.current_item is not None
            and self.body.currentIndex() == 1
            and self.card_sides.currentIndex() == 1
            and self.rating_area.isVisible()
        ):
            self.rate(rating)

    def invalidate_examples(self) -> None:
        self.example_generation += 1
        for task in self.example_tasks:
            task.cancel()
        self.current_example_key = None
        self.current_examples = None
        self.current_examples_error = None
        self.examples_requested_visible = False
        self.card_examples.clear()
        self.card_examples.setVisible(False)
        self.examples_scroll.setVisible(False)
        self.show_examples_button.setEnabled(True)
        self.show_examples_button.setText("Показать примеры")

    def prepare_examples(self, item: SessionItem) -> None:
        """Starts preloading examples for a newly displayed card."""
        self.invalidate_examples()
        key = (item.card.hanzi, item.card.pinyin)
        self.current_example_key = key
        if key in self.example_cache:
            self.current_examples = self.example_cache[key]
            return
        generation = self.example_generation
        task = ExampleLoadTask(generation, key, self.example_loader)
        task.signals.finished.connect(self.examples_loaded)
        task.signals.finished.connect(
            lambda *_arguments, worker=task: self.example_tasks.discard(worker)
        )
        self.example_tasks.add(task)
        self.example_pool.start(task)

    def examples_loaded(
        self,
        generation: int,
        key: tuple[str, str],
        examples: object,
        error: object,
    ) -> None:
        if self.shutting_down:
            return
        loaded = list(examples or []) if error is None else []
        if error is None:
            self.example_cache[key] = loaded
            if len(self.example_cache) > 48:
                self.example_cache.pop(next(iter(self.example_cache)))
        if generation != self.example_generation or key != self.current_example_key:
            return
        self.current_examples = loaded
        self.current_examples_error = error if isinstance(error, Exception) else None
        self.show_examples_button.setEnabled(True)
        self.show_examples_button.setText("Показать примеры")
        if self.examples_requested_visible:
            self.show_loaded_examples()

    def show_loaded_examples(self) -> None:
        if self.current_examples is None:
            return
        if self.current_examples_error is not None:
            text = "Не удалось получить примеры"
        else:
            text = self.format_examples(self.current_examples)
        self.card_examples.setText(text)
        self.card_examples.setVisible(True)
        self.examples_scroll.setVisible(True)
        self.examples_scroll.verticalScrollBar().setValue(0)
        self.show_examples_button.setEnabled(True)
        self.show_examples_button.setText("Скрыть примеры")

    def toggle_examples(self) -> None:
        if not self.current_item:
            return
        if self.examples_scroll.isVisible():
            self.examples_requested_visible = False
            self.card_examples.setVisible(False)
            self.examples_scroll.setVisible(False)
            self.show_examples_button.setText("Показать примеры")
            return
        self.examples_requested_visible = True
        if self.current_examples is None:
            # The read already runs in the background. Disable repeated clicks
            # without blocking keyboard navigation or the rest of the page.
            self.show_examples_button.setEnabled(False)
            self.show_examples_button.setText("Подготавливаю примеры…")
            return
        self.show_loaded_examples()

    def format_examples(self, examples: list[dict]) -> str:
        return format_example_blocks(
            examples, self.chinese_font_family, 17, self.russian_font_family
        ) or "Примеры не найдены"

    def rate(self, rating: Rating) -> None:
        # The guard also protects against a queued second click: after the
        # first rating the next card is already on its question side.
        if (
            not self.current_item
            or self.body.currentIndex() != 1
            or self.card_sides.currentIndex() != 1
            or not self.rating_area.isVisible()
        ):
            return
        reviewed_at = utc_now()
        response_time_ms = max(
            0,
            int((reviewed_at - self.current_item.shown_at).total_seconds() * 1000),
        )
        self.session.answer(
            self.current_item, rating, reviewed_at, response_time_ms
        )
        self.invalidate_examples()
        self.current_item = None
        self.load_next_card(reviewed_at)

    def refresh_current_visuals(self) -> None:
        if self.current_item:
            current_side = self.card_sides.currentIndex()
            examples_visible = self.examples_scroll.isVisible()
            examples_requested = self.examples_requested_visible
            examples_scroll_position = self.examples_scroll.verticalScrollBar().value()
            pinyin_revealed = self.hidden_pinyin.revealed
            self.show_item(self.current_item)
            if pinyin_revealed:
                self.hidden_pinyin.reveal()
            if current_side == 1:
                self.show_answer()
            if examples_visible and self.current_examples is not None:
                self.examples_requested_visible = True
                self.show_loaded_examples()
                self.examples_scroll.verticalScrollBar().setValue(examples_scroll_position)
            elif examples_requested and self.current_examples is None:
                self.examples_requested_visible = True
                self.show_examples_button.setEnabled(False)
                self.show_examples_button.setText("Подготавливаю примеры…")
            elif self.card_examples.text() and self.current_examples is not None:
                # It was opened and then hidden: update the chosen Chinese font
                # without unexpectedly opening the examples again.
                self.card_examples.setText(self.format_examples(self.current_examples))
