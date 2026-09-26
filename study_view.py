from __future__ import annotations

import csv
import re
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyleOptionComboBox,
    QStylePainter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from anki_export import export_anki_package
from anki_import import parse_anki_export
from card_widgets import RevealPinyinButton
from database import get_entries_by_hanzi
from scheduler import (
    DEFAULT_CONFIG,
    FSRS6_DEFAULT_PARAMETERS,
    Rating,
    ReviewDirection,
    format_interval,
    interval_for_stability,
    preview_ratings,
    validate_config,
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


class NoWheelComboBox(QComboBox):
    """Let a surrounding scroll area handle the wheel while the popup is closed."""

    def wheelEvent(self, event) -> None:
        if self.view().isVisible():
            super().wheelEvent(event)
        else:
            event.ignore()


class CenteredNoWheelComboBox(NoWheelComboBox):
    """Draw the selected value in the center while keeping the arrow separate."""

    def paintEvent(self, event) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.ComplexControl.CC_ComboBox, option)
        text_rect = self.style().subControlRect(
            QStyle.ComplexControl.CC_ComboBox,
            option,
            QStyle.SubControl.SC_ComboBoxEditField,
            self,
        )
        painter.setPen(option.palette.text().color())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextSingleLine,
            option.currentText,
        )


class NoWheelSpinBox(QSpinBox):
    """Let the settings page scroll without changing a numeric value."""

    def wheelEvent(self, event) -> None:
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """Let the settings page scroll without changing a numeric value."""

    def wheelEvent(self, event) -> None:
        event.ignore()


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
        self.repository = repository
        self.setObjectName("manualCardDialog")
        self.setWindowTitle("Новая карточка")
        self.setModal(True)
        self.setMinimumWidth(470)
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


class SpacedRepetitionSettingsDialog(QDialog):
    """User-facing FSRS controls; model weights stay available but deliberately advanced."""

    def __init__(self, repository: StudyRepository, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repository = repository
        self.config = repository.get_scheduler_config()
        self.setObjectName("spacedRepetitionSettingsDialog")
        self.setWindowTitle("Настройки интервального повторения")
        self.resize(860, 760)
        self.setMinimumSize(700, 580)

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 24, 28, 22)
        root.setSpacing(16)
        title = QLabel("Настройки повторения")
        title.setObjectName("settingsDialogTitle")
        intro = QLabel(
            "Управляйте дневной нагрузкой, первичным обучением и расписанием FSRS. "
            "Рекомендуемые значения уже выбраны — менять всё сразу не требуется."
        )
        intro.setObjectName("settingsDialogHint")
        intro.setWordWrap(True)
        root.addWidget(title)
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setObjectName("schedulerSettingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        content.setObjectName("schedulerSettingsContent")
        sections = QVBoxLayout(content)
        sections.setContentsMargins(0, 0, 8, 0)
        sections.setSpacing(14)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        daily = self._section(
            "Дневные лимиты",
            "Задайте, сколько карточек программа подготовит на один учебный день.",
        )
        sections.addWidget(daily)
        daily_layout = daily.layout()
        self.daily_limit = NoWheelSpinBox()
        self._prepare_spinbox(self.daily_limit)
        self.daily_limit.setObjectName("settingsDailyLimit")
        self.daily_limit.setRange(1, 1000)
        self.daily_limit.setSuffix(" карточек")
        self.daily_limit.setValue(repository.get_daily_limit())
        daily_layout.addWidget(self._setting_row(
            "Максимум повторений в день",
            "Общее число карточек на сегодня: сюда входят и новые слова, и ранее изученные. Уже начатое повторение не прерывается.",
            self.daily_limit,
        ))
        self.new_cards_limit = NoWheelSpinBox()
        self._prepare_spinbox(self.new_cards_limit)
        self.new_cards_limit.setObjectName("newCardsPerDay")
        self.new_cards_limit.setRange(0, 1000)
        self.new_cards_limit.setSuffix(" карточек")
        self.new_cards_limit.setValue(repository.get_new_cards_limit())
        daily_layout.addWidget(self._setting_row(
            "Новых карточек в день",
            "Сколько ещё не изучавшихся слов можно впервые показать сегодня. Значение 0 оставит только повторения.",
            self.new_cards_limit,
        ))

        order = self._section(
            "Порядок показа",
            "Выберите, в какой последовательности карточки будут появляться во время занятия.",
        )
        order_layout = order.layout()
        self.new_review_order = NoWheelComboBox()
        self.new_review_order.setObjectName("newReviewOrder")
        self._fill_combo(self.new_review_order, (
            ("Сначала новые", "new_first"),
            ("Сначала повторения", "reviews_first"),
            ("Перемешивать", "mix"),
        ), self.config.new_review_order)
        order_layout.addWidget(self._setting_row(
            "Новые и повторяемые",
            "Показывать незнакомые слова до повторений, после них или чередовать оба типа.",
            self.new_review_order,
        ))
        self.new_card_order = NoWheelComboBox()
        self.new_card_order.setObjectName("newCardOrder")
        self._fill_combo(self.new_card_order, (
            ("В случайном порядке", "random"),
            ("Сначала добавленные раньше", "added"),
        ), self.config.new_card_order)
        order_layout.addWidget(self._setting_row(
            "Порядок новых карточек",
            "«Случайно» перемешивает новые слова. «Сначала добавленные раньше» сохраняет очередь по дате добавления.",
            self.new_card_order,
        ))
        self.review_order = NoWheelComboBox()
        self.review_order.setObjectName("reviewOrder")
        self._fill_combo(self.review_order, (
            ("Сначала самые просроченные", "due"),
            ("Сначала хуже запоминаемые", "retrievability"),
            ("Полностью случайно", "random"),
        ), self.config.review_order)
        order_layout.addWidget(self._setting_row(
            "Порядок повторений",
            "«Просроченные» показывает сначала карточки, срок которых прошёл раньше. «Хуже запоминаемые» начинает с самых трудных для памяти.",
            self.review_order,
        ))

        learning = self._section(
            "Новые карточки",
            "При первом изучении слово показывается несколько раз с короткими паузами. После последнего успешного ответа начинается обычное расписание.",
        )
        sections.addWidget(learning)
        learning_layout = learning.layout()
        self.learning_steps = QLineEdit(
            self._format_steps(self.config.learning_steps_seconds)
        )
        self.learning_steps.setObjectName("learningSteps")
        self.learning_steps.setPlaceholderText("например: 2 минуты; 10 минут")
        learning_layout.addWidget(self._setting_row(
            "Паузы при первом изучении",
            "Каждое значение — время до следующего короткого показа. «2 минуты; 10 минут» означает: сначала повторить через 2 минуты, затем через 10 минут.",
            self.learning_steps,
        ))
        lapse = self._section(
            "Забыто",
            "Настройки для случая, когда ранее изученное слово совсем не удалось вспомнить.",
        )
        sections.addWidget(lapse)
        lapse_layout = lapse.layout()
        self.relearning_steps = QLineEdit(
            self._format_steps(self.config.relearning_steps_seconds)
        )
        self.relearning_steps.setObjectName("relearningSteps")
        self.relearning_steps.setPlaceholderText("например: 10 минут; пусто — без паузы")
        lapse_layout.addWidget(self._setting_row(
            "Паузы после полного забывания",
            "После ответа «Очень тяжело» карточка снова появится через указанное время. Если поле пустое, сразу будет назначена следующая дата в календаре.",
            self.relearning_steps,
        ))
        self.leech_threshold = NoWheelSpinBox()
        self._prepare_spinbox(self.leech_threshold)
        self.leech_threshold.setObjectName("leechThreshold")
        self.leech_threshold.setRange(0, 100)
        self.leech_threshold.setSpecialValueText("Отключено")
        self.leech_threshold.setSuffix(" ошибок")
        self.leech_threshold.setValue(self.config.leech_threshold)
        lapse_layout.addWidget(self._setting_row(
            "Порог трудной карточки",
            "После указанного числа ответов «Очень тяжело» карточка будет считаться проблемной. Значение 0 отключает эту проверку.",
            self.leech_threshold,
        ))
        self.leech_action = NoWheelComboBox()
        self.leech_action.setObjectName("leechAction")
        self._fill_combo(self.leech_action, (
            ("Только отметить", "tag_only"),
            ("Приостановить", "suspend"),
        ), self.config.leech_action)
        lapse_layout.addWidget(self._setting_row(
            "Что делать с трудной карточкой",
            "«Только отметить» оставит её в занятиях. «Приостановить» уберёт из очереди, пока вы не вернёте её через список карточек.",
            self.leech_action,
        ))

        sections.addWidget(order)

        fsrs = self._section(
            "FSRS 6",
            "Алгоритм оценивает, когда слово начнёт забываться, и назначает следующую дату повторения.",
        )
        fsrs.setObjectName("fsrsSettingsSection")
        sections.addWidget(fsrs)
        fsrs_layout = fsrs.layout()
        self.preset = NoWheelComboBox()
        self.preset.setObjectName("schedulerPreset")
        self._prepare_combo(self.preset)
        self.preset.addItems(("Сбалансированно · 90%", "Надёжно · 95%", "Меньше нагрузки · 85%", "Свои настройки"))
        fsrs_layout.addWidget(self._setting_row(
            "Предустановка",
            "Быстрый выбор между обычной нагрузкой, более надёжным запоминанием и более редкими занятиями.",
            self.preset,
        ))
        self.retention = NoWheelSpinBox()
        self._prepare_spinbox(self.retention)
        self.retention.setObjectName("desiredRetention")
        self.retention.setRange(70, 97)
        self.retention.setSuffix(" %")
        self.retention.setValue(round(self.config.desired_retention * 100))
        self.retention_effect = QLabel()
        self.retention_effect.setObjectName("retentionEffect")
        retention_control = QWidget()
        retention_control.setObjectName("inlineSettingControl")
        retention_layout = QVBoxLayout(retention_control)
        retention_layout.setContentsMargins(0, 0, 0, 0)
        retention_layout.setSpacing(4)
        retention_layout.addWidget(self.retention)
        retention_layout.addWidget(self.retention_effect)
        fsrs_layout.addWidget(self._setting_row(
            "Желаемое усвоение",
            "Ожидаемая доля правильных ответов: 90% означает примерно 90 успешных ответов из 100. Чем выше процент, тем чаще повторения.",
            retention_control,
        ))
        self.retention_warning = QLabel()
        self.retention_warning.setObjectName("retentionWarning")
        self.retention_warning.setWordWrap(True)
        fsrs_layout.addWidget(self.retention_warning)
        rating_hint = QLabel(
            "Важно: если ответ не вспомнился, выбирайте «Очень тяжело». «Тяжело» — "
            "успешный ответ с большим усилием. Это различие влияет на расчёт памяти."
        )
        rating_hint.setObjectName("schedulerInfoCallout")
        rating_hint.setWordWrap(True)
        fsrs_layout.addWidget(rating_hint)

        easy_days_section = self._section(
            "Лёгкие дни",
            "Можно заранее уменьшить число будущих повторений в дни, когда у вас меньше времени.",
        )
        sections.addWidget(easy_days_section)
        easy_days_section_layout = easy_days_section.layout()

        advanced = self._section(
            "Дополнительные",
            "Ограничения расписания. Если вы не уверены, оставьте значения по умолчанию.",
        )
        sections.addWidget(advanced)
        advanced_layout = advanced.layout()
        self.maximum_interval = NoWheelSpinBox()
        self._prepare_spinbox(self.maximum_interval)
        self.maximum_interval.setObjectName("maximumReviewInterval")
        self.maximum_interval.setRange(1, 36500)
        self.maximum_interval.setSuffix(" дней")
        self.maximum_interval.setValue(round(self.config.maximum_interval_days))
        advanced_layout.addWidget(self._setting_row(
            "Максимальный интервал",
            "Самая длинная разрешённая пауза между двумя повторениями одной карточки. Обычно это ограничение менять не нужно.",
            self.maximum_interval,
        ))
        self.minimum_interval = NoWheelDoubleSpinBox()
        self._prepare_spinbox(self.minimum_interval)
        self.minimum_interval.setRange(0.1, 30.0)
        self.minimum_interval.setDecimals(1)
        self.minimum_interval.setSuffix(" дней")
        self.minimum_interval.setValue(self.config.minimum_review_interval_days)
        advanced_layout.addWidget(self._setting_row(
            "Минимальный дневной интервал",
            "Самая короткая пауза в календарных днях после завершения начального обучения. К минутным паузам выше не относится.",
            self.minimum_interval,
        ))
        self.fuzzing = QPushButton()
        self.fuzzing.setObjectName("enableIntervalFuzzing")
        self._prepare_toggle(self.fuzzing)
        self.fuzzing.setChecked(self.config.enable_fuzzing)
        advanced_layout.addWidget(self._setting_row(
            "Разброс интервалов",
            "Слегка сдвигает повторения на соседние дни, чтобы слишком много карточек не собиралось на одну дату.",
            self.fuzzing,
        ))
        easy_days = QWidget()
        easy_days.setObjectName("easyDaysControl")
        easy_days_layout = QVBoxLayout(easy_days)
        easy_days_layout.setContentsMargins(0, 11, 0, 11)
        easy_days_layout.setSpacing(7)
        easy_days_title = QLabel("Нагрузка по дням недели")
        easy_days_title.setObjectName("schedulerSettingTitle")
        easy_days_hint = QLabel(
            "«Обычно» не снижает нагрузку, «Легче» старается назначать меньше карточек, "
            "а «Минимум» по возможности избегает этого дня. Уже просроченные карточки не переносятся."
        )
        easy_days_hint.setObjectName("schedulerSettingDescription")
        easy_days_hint.setWordWrap(True)
        easy_days_grid = QGridLayout()
        easy_days_grid.setContentsMargins(0, 3, 0, 0)
        easy_days_grid.setHorizontalSpacing(8)
        self.easy_day_controls = []
        for index, (day, value) in enumerate(zip(
            ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"),
            self.config.easy_days_percentages,
        )):
            grid_column = (index % 4) * 2 + (1 if index >= 4 else 0)
            grid_row = (index // 4) * 2
            label = QLabel(day)
            label.setObjectName("easyDayLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            combo = CenteredNoWheelComboBox()
            combo.setObjectName("easyDayLoad")
            self._prepare_combo(combo)
            combo.addItem("Обычно", 1.0)
            combo.addItem("Легче", 0.5)
            combo.addItem("Минимум", 0.1)
            combo.setCurrentIndex(max(0, combo.findData(value)))
            combo.setSizePolicy(
                QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
            )
            self.easy_day_controls.append(combo)
            easy_days_grid.addWidget(label, grid_row, grid_column, 1, 2)
            easy_days_grid.addWidget(combo, grid_row + 1, grid_column, 1, 2)
        for column in range(8):
            easy_days_grid.setColumnStretch(column, 1)
        easy_days_layout.addWidget(easy_days_title)
        easy_days_layout.addWidget(easy_days_hint)
        easy_days_layout.addLayout(easy_days_grid)
        easy_days_section_layout.addWidget(easy_days)
        self.reschedule = QPushButton()
        self.reschedule.setObjectName("rescheduleOnSettingsChange")
        self._prepare_toggle(self.reschedule)
        self.reschedule.setChecked(True)
        advanced_layout.addWidget(self._setting_row(
            "Применить изменения сейчас",
            "Если включено, даты уже изученных карточек будут пересчитаны сразу. Если выключено — каждая карточка получит новые правила после следующего ответа.",
            self.reschedule,
        ))

        parameters_toggle = QPushButton("Экспертные параметры алгоритма")
        parameters_toggle.setObjectName("parametersToggle")
        parameters_toggle.setCheckable(True)
        advanced_layout.addWidget(parameters_toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        self.parameters_panel = QFrame()
        self.parameters_panel.setObjectName("parametersPanel")
        parameters_layout = QVBoxLayout(self.parameters_panel)
        parameters_layout.setContentsMargins(14, 12, 14, 14)
        parameters_hint = QLabel(
            "Это коэффициенты модели, а не интервалы. Не копируйте чужие значения: "
            "они меняют внутренние расчёты памяти. Если вы специально их не настраивали, оставьте значения по умолчанию."
        )
        parameters_hint.setObjectName("settingsDialogHint")
        parameters_hint.setWordWrap(True)
        self.parameters = QPlainTextEdit()
        self.parameters.setObjectName("fsrsParameters")
        self.parameters.setPlainText(self._format_parameters(self.config.fsrs_parameters))
        self.parameters.setMaximumHeight(110)
        reset_parameters = QPushButton("Вернуть параметры FSRS по умолчанию")
        reset_parameters.setObjectName("resetFsrsParameters")
        reset_parameters.clicked.connect(
            lambda: self.parameters.setPlainText(
                self._format_parameters(FSRS6_DEFAULT_PARAMETERS)
            )
        )
        parameters_layout.addWidget(parameters_hint)
        parameters_layout.addWidget(self.parameters)
        parameters_layout.addWidget(reset_parameters, alignment=Qt.AlignmentFlag.AlignLeft)
        self.parameters_panel.setVisible(False)
        parameters_toggle.toggled.connect(self.parameters_panel.setVisible)
        parameters_toggle.toggled.connect(
            lambda visible: parameters_toggle.setText(
                "Скрыть экспертные параметры" if visible else "Экспертные параметры алгоритма"
            )
        )
        advanced_layout.addWidget(self.parameters_panel)
        sections.addStretch()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Сохранить")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Отмена")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText("По умолчанию")
        buttons.button(QDialogButtonBox.StandardButton.Save).setObjectName(
            "schedulerSettingsSave"
        )
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName(
            "schedulerSettingsCancel"
        )
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setObjectName(
            "schedulerSettingsDefaults"
        )
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self.restore_defaults
        )
        root.addWidget(buttons)

        self.preset.currentIndexChanged.connect(self.apply_preset)
        self.retention.valueChanged.connect(self.retention_changed)
        self.retention_changed(self.retention.value())
        if self.retention.value() == 90:
            self.preset.setCurrentIndex(0)
        elif self.retention.value() == 95:
            self.preset.setCurrentIndex(1)
        elif self.retention.value() == 85:
            self.preset.setCurrentIndex(2)
        else:
            self.preset.setCurrentIndex(3)

    @staticmethod
    def _section(title: str, subtitle: str) -> QFrame:
        section = QFrame()
        section.setObjectName("schedulerSettingsSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(20, 17, 20, 18)
        layout.setSpacing(0)
        heading = QLabel(title)
        heading.setObjectName("schedulerSectionTitle")
        description = QLabel(subtitle)
        description.setObjectName("schedulerSectionDescription")
        description.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(description)
        layout.addSpacing(10)
        return section

    @staticmethod
    def _setting_row(title: str, description: str, control: QWidget) -> QFrame:
        row = QFrame()
        row.setObjectName("schedulerSettingRow")
        row.setMinimumWidth(0)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 11, 0, 11)
        layout.setSpacing(20)
        copy_container = QWidget()
        copy_container.setObjectName("schedulerSettingCopy")
        copy_container.setMinimumWidth(0)
        copy_container.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        copy = QVBoxLayout(copy_container)
        copy.setContentsMargins(0, 0, 0, 0)
        copy.setSpacing(3)
        heading = QLabel(title)
        heading.setObjectName("schedulerSettingTitle")
        heading.setWordWrap(True)
        heading.setMinimumWidth(0)
        heading.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        hint = QLabel(description)
        hint.setObjectName("schedulerSettingDescription")
        hint.setWordWrap(True)
        hint.setMinimumWidth(0)
        hint.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        copy.addWidget(heading)
        copy.addWidget(hint)
        layout.addWidget(copy_container, 1)
        if control.property("compactControl"):
            control.setMinimumWidth(112)
            control.setMaximumWidth(128)
        else:
            control.setMinimumWidth(250)
            control.setMaximumWidth(250)
        layout.addWidget(control, alignment=Qt.AlignmentFlag.AlignTop)
        return row

    @staticmethod
    def _fill_combo(combo: QComboBox, items, current_value: str) -> None:
        SpacedRepetitionSettingsDialog._prepare_combo(combo)
        for title, value in items:
            combo.addItem(title, value)
        index = combo.findData(current_value)
        combo.setCurrentIndex(max(0, index))

    @staticmethod
    def _prepare_combo(combo: QComboBox) -> None:
        combo.view().setObjectName("schedulerComboPopup")

    @staticmethod
    def _prepare_spinbox(spinbox: QAbstractSpinBox) -> None:
        spinbox.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)

    @staticmethod
    def _prepare_toggle(button: QPushButton) -> None:
        button.setCheckable(True)
        button.setProperty("schedulerToggle", True)
        button.setProperty("compactControl", True)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.toggled.connect(
            lambda checked: button.setText("Включено" if checked else "Выключено")
        )
        button.setText("Включено" if button.isChecked() else "Выключено")

    @staticmethod
    def _format_parameters(parameters) -> str:
        return ", ".join(f"{value:g}" for value in parameters)

    @staticmethod
    def _format_steps(steps: tuple[int, ...]) -> str:
        formatted = []
        for seconds in steps:
            if seconds % 86400 == 0:
                value = seconds // 86400
                forms = ("день", "дня", "дней")
            elif seconds % 3600 == 0:
                value = seconds // 3600
                forms = ("час", "часа", "часов")
            elif seconds % 60 == 0:
                value = seconds // 60
                forms = ("минута", "минуты", "минут")
            else:
                value = seconds
                forms = ("секунда", "секунды", "секунд")
            if value % 10 == 1 and value % 100 != 11:
                unit = forms[0]
            elif value % 10 in {2, 3, 4} and value % 100 not in {12, 13, 14}:
                unit = forms[1]
            else:
                unit = forms[2]
            formatted.append(f"{value} {unit}")
        return "; ".join(formatted)

    @staticmethod
    def _parse_steps(source: str) -> tuple[int, ...]:
        source = source.strip().lower()
        if not source:
            return ()
        units = {
            "с": 1, "s": 1, "сек": 1, "секунда": 1,
            "секунды": 1, "секунд": 1,
            "м": 60, "m": 60, "мин": 60, "минута": 60,
            "минуты": 60, "минут": 60,
            "ч": 3600, "h": 3600, "час": 3600,
            "часа": 3600, "часов": 3600,
            "д": 86400, "d": 86400, "дн": 86400, "день": 86400,
            "дня": 86400, "дней": 86400,
        }
        steps = []
        unit_pattern = "|".join(
            re.escape(unit) for unit in sorted(units, key=len, reverse=True)
        )
        pattern = re.compile(rf"(\d+(?:[.,]\d+)?)\s*({unit_pattern})")
        cursor = 0
        for match in pattern.finditer(source):
            if source[cursor:match.start()].strip(" ,;"):
                raise ValueError(
                    "Введите паузы словами, например: 2 минуты; 10 минут"
                )
            steps.append(
                round(
                    float(match.group(1).replace(",", "."))
                    * units[match.group(2)]
                )
            )
            cursor = match.end()
        if not steps or source[cursor:].strip(" ,;"):
            raise ValueError(
                "Введите паузы словами, например: 2 минуты; 10 минут"
            )
        return tuple(steps)

    def parsed_parameters(self) -> tuple[float, ...]:
        source = self.parameters.toPlainText().replace(",", " ").replace(";", " ")
        try:
            return tuple(float(part) for part in source.split())
        except ValueError as exception:
            raise ValueError("Параметры FSRS должны быть числами через запятую") from exception

    def apply_preset(self, index: int) -> None:
        if index == 0:
            self.retention.setValue(90)
        elif index == 1:
            self.retention.setValue(95)
        elif index == 2:
            self.retention.setValue(85)

    def retention_changed(self, value: int) -> None:
        if value not in {85, 90, 95}:
            self.preset.blockSignals(True)
            self.preset.setCurrentIndex(3)
            self.preset.blockSignals(False)
        self.retention_warning.setText(
            "Выше 95% нагрузка растёт особенно быстро."
            if value > 95
            else "Диапазон 85–95% обычно даёт практичный баланс памяти и времени."
        )
        current = replace(self.config, desired_retention=value / 100)
        baseline = replace(self.config, desired_retention=0.90)
        current_interval = interval_for_stability(30.0, current)
        baseline_interval = interval_for_stability(30.0, baseline)
        difference = round((current_interval / baseline_interval - 1) * 100)
        if difference == 0:
            effect = "Ориентир: интервалы примерно как при стандартных 90%."
        elif difference > 0:
            effect = f"Ориентир: интервалы примерно на {difference}% длиннее, чем при 90%."
        else:
            effect = f"Ориентир: интервалы примерно на {abs(difference)}% короче, чем при 90%."
        self.retention_effect.setText(effect)

    def restore_defaults(self) -> None:
        defaults = DEFAULT_CONFIG
        self.preset.setCurrentIndex(0)
        self.retention.setValue(round(defaults.desired_retention * 100))
        self.daily_limit.setValue(30)
        self.new_cards_limit.setValue(30)
        self.new_card_order.setCurrentIndex(
            max(0, self.new_card_order.findData(defaults.new_card_order))
        )
        self.new_review_order.setCurrentIndex(
            max(0, self.new_review_order.findData(defaults.new_review_order))
        )
        self.review_order.setCurrentIndex(
            max(0, self.review_order.findData(defaults.review_order))
        )
        self.minimum_interval.setValue(defaults.minimum_review_interval_days)
        self.maximum_interval.setValue(round(defaults.maximum_interval_days))
        self.fuzzing.setChecked(defaults.enable_fuzzing)
        self.leech_threshold.setValue(defaults.leech_threshold)
        self.leech_action.setCurrentIndex(
            max(0, self.leech_action.findData(defaults.leech_action))
        )
        for control, value in zip(
            self.easy_day_controls, defaults.easy_days_percentages
        ):
            control.setCurrentIndex(max(0, control.findData(value)))
        self.learning_steps.setText(self._format_steps(defaults.learning_steps_seconds))
        self.relearning_steps.setText(self._format_steps(defaults.relearning_steps_seconds))
        self.parameters.setPlainText(self._format_parameters(defaults.fsrs_parameters))

    def save(self) -> None:
        try:
            config = replace(
                self.config,
                desired_retention=self.retention.value() / 100,
                learning_steps_seconds=self._parse_steps(self.learning_steps.text()),
                relearning_steps_seconds=self._parse_steps(self.relearning_steps.text()),
                minimum_review_interval_days=self.minimum_interval.value(),
                maximum_interval_days=float(self.maximum_interval.value()),
                enable_fuzzing=self.fuzzing.isChecked(),
                easy_days_percentages=tuple(
                    float(control.currentData()) for control in self.easy_day_controls
                ),
                new_card_order=str(self.new_card_order.currentData()),
                new_review_order=str(self.new_review_order.currentData()),
                review_order=str(self.review_order.currentData()),
                leech_threshold=self.leech_threshold.value(),
                leech_action=str(self.leech_action.currentData()),
                fsrs_parameters=self.parsed_parameters(),
            )
            validate_config(config)
        except ValueError as exception:
            QMessageBox.warning(self, "Проверьте настройки", str(exception))
            return
        self.repository.set_scheduler_config(config)
        self.repository.set_daily_limit(self.daily_limit.value())
        self.repository.set_new_cards_limit(self.new_cards_limit.value())
        if self.reschedule.isChecked():
            self.repository.reschedule_review_cards(config)
        self.config = config
        self.accept()


class CardListDialog(QDialog):
    def __init__(
        self,
        repository: StudyRepository,
        chinese_font_family: str,
        russian_font_family: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.setObjectName("cardListDialog")
        self.setWindowTitle("Все карточки")
        self.resize(820, 560)
        self.setMinimumSize(620, 420)

        self.cards = repository.get_cards()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        heading = QLabel("Все карточки")
        heading.setObjectName("cardListTitle")
        count = QLabel(f"Сохранено карточек: {len(self.cards)}")
        count.setObjectName("cardListCount")
        layout.addWidget(heading)
        layout.addWidget(count)

        self.table = QTableWidget(len(self.cards), 4)
        self.table.setObjectName("cardListTable")
        self.table.setHorizontalHeaderLabels(("Слово", "Пиньинь", "Перевод", "Состояние"))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(48)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        for row, card in enumerate(self.cards):
            status = (
                "Приостановлена"
                if card.is_suspended
                else ("Трудная" if card.is_leech else "Обычная")
            )
            values = (card.hanzi, card.pinyin, card.translation, status)
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

        self.resume_button = QPushButton("Вернуть в обучение")
        self.resume_button.setObjectName("cardListResumeButton")
        self.resume_button.setEnabled(False)
        self.resume_button.clicked.connect(self.resume_selected_card)
        self.table.itemSelectionChanged.connect(self.update_resume_button)
        close_button = QPushButton("Закрыть")
        close_button.setObjectName("cardListCloseButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.clicked.connect(self.accept)
        actions = QHBoxLayout()
        actions.addWidget(self.resume_button)
        actions.addStretch()
        actions.addWidget(close_button)
        layout.addLayout(actions)

    def update_resume_button(self) -> None:
        row = self.table.currentRow()
        self.resume_button.setEnabled(
            0 <= row < len(self.cards) and self.cards[row].is_suspended
        )

    def resume_selected_card(self) -> None:
        row = self.table.currentRow()
        if not 0 <= row < len(self.cards) or not self.cards[row].is_suspended:
            return
        card = self.cards[row]
        self.repository.set_card_suspended(card.id, False)
        self.cards[row] = replace(card, is_suspended=False)
        self.table.item(row, 3).setText("Трудная" if card.is_leech else "Обычная")
        self.update_resume_button()


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

        self.import_anki_button = QPushButton("Импорт из Anki")
        self.import_anki_button.setObjectName("importCardsButton")
        self.import_anki_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_anki_button.setToolTip("Импортировать карточки из Anki")
        self.import_anki_button.clicked.connect(self.import_anki_cards)
        self.export_anki_button = QPushButton("Экспорт в Anki")
        self.export_anki_button.setObjectName("exportAnkiButton")
        self.export_anki_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_anki_button.setToolTip(
            "Сохранить все карточки в формате для импорта в Anki"
        )
        self.export_anki_button.clicked.connect(self.export_anki_cards)
        self.card_list_button = QPushButton("Все карточки")
        self.card_list_button.setObjectName("cardListButton")
        self.card_list_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.card_list_button.setToolTip("Показать список всех сохранённых карточек")
        self.card_list_button.clicked.connect(self.show_card_list)
        self.scheduler_settings_button = QPushButton("Настройки повторения")
        self.scheduler_settings_button.setObjectName("schedulerSettingsButton")
        self.scheduler_settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.scheduler_settings_button.setToolTip(
            "Настроить FSRS, желаемое усвоение и интервалы"
        )
        self.scheduler_settings_button.clicked.connect(self.show_scheduler_settings)
        self.manual_card_button = QPushButton("Новая карточка")
        self.manual_card_button.setObjectName("manualCardButton")
        self.manual_card_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.manual_card_button.clicked.connect(self.add_manual_card)

        root.addLayout(header)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addWidget(self.manual_card_button)
        actions.addWidget(self.card_list_button)
        actions.addWidget(self.import_anki_button)
        actions.addWidget(self.export_anki_button)
        actions.addWidget(self.scheduler_settings_button)
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

    def import_anki_cards(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Импорт из Anki",
            str(Path.home()),
            "Текстовый экспорт карточек (*.txt *.tsv *.csv);;Все файлы (*)",
        )
        if not path:
            return
        try:
            result = parse_anki_export(path)
        except (OSError, UnicodeError, ValueError, csv.Error) as exception:
            QMessageBox.warning(
                self,
                "Не удалось импортировать карточки",
                f"Файл не похож на поддерживаемый текстовый экспорт карточек.\n{exception}",
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

    def export_anki_cards(self) -> None:
        cards = self.repository.get_cards()
        if not cards:
            QMessageBox.information(
                self,
                "Экспорт в Anki",
                "В HanziLab пока нет карточек для экспорта.",
            )
            return

        path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Экспорт карточек в Anki",
            str(Path.home() / "HanziLab.apkg"),
            "Пакет колоды Anki (*.apkg);;Все файлы (*)",
        )
        if not path:
            return
        if not Path(path).suffix:
            path += ".apkg"
        try:
            exported = export_anki_package(cards, path)
        except (OSError, UnicodeError, ValueError) as exception:
            QMessageBox.warning(
                self,
                "Не удалось экспортировать карточки",
                f"Не удалось сохранить файл.\n{exception}",
            )
            return

        QMessageBox.information(
            self,
            "Экспорт завершён",
            (
                f"Экспортировано слов: {exported}\n\n"
                "Порядок импорта в Anki:\n"
                "1. Выберите «Файл → Импорт».\n"
                "2. Откройте сохранённый файл."
            ),
        )

    def finish_anki_import(
        self, generation: int, rows: object, error: object
    ) -> None:
        if self.shutting_down or generation != self.anki_import_generation:
            return
        self.import_anki_button.setEnabled(True)
        self.import_anki_button.setText("Импорт из Anki")
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

    def show_scheduler_settings(self) -> None:
        dialog = SpacedRepetitionSettingsDialog(self.repository, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.session.invalidate_daily_priority(utc_now())
            self.refresh()

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
        previews = preview_ratings(
            card, utc_now(), self.repository.get_scheduler_config()
        )
        for rating, button in self.rating_buttons.items():
            button.setText(f"{RATING_LABELS[rating]}\n{format_interval(previews[rating])}")

    def open_answer_from_keyboard(self) -> None:
        if (
            self.current_item is not None
            and self.body.currentIndex() == 1
            and self.card_sides.currentIndex() == 0
        ):
            self.show_answer()

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
