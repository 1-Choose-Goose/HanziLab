from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from itertools import pairwise

FSRS6_DEFAULT_PARAMETERS = (
    0.212,
    1.2931,
    2.3065,
    8.2956,
    6.4133,
    0.8334,
    3.0194,
    0.001,
    1.8722,
    0.1666,
    0.796,
    1.4835,
    0.0614,
    0.2629,
    1.6483,
    0.6014,
    1.8729,
    0.5425,
    0.0912,
    0.0658,
    0.1542,
)

FSRS6_PARAMETER_BOUNDS = (
    (0.001, 100.0), (0.001, 100.0), (0.001, 100.0), (0.001, 100.0),
    (1.0, 10.0), (0.001, 4.0), (0.001, 4.0), (0.001, 0.75),
    (0.0, 4.5), (0.0, 0.8), (0.001, 3.5), (0.001, 5.0),
    (0.001, 0.25), (0.001, 0.9), (0.0, 4.0), (0.0, 1.0),
    (1.0, 6.0), (0.0, 2.0), (0.0, 2.0), (0.0, 0.8), (0.1, 0.8),
)


class Rating(StrEnum):
    VERY_HARD = "VERY_HARD"
    HARD = "HARD"
    MEDIUM = "MEDIUM"
    EASY = "EASY"


class LearningState(StrEnum):
    NEW = "NEW"
    LEARNING = "LEARNING"
    RELEARNING = "RELEARNING"
    REVIEW = "REVIEW"


class ReviewDirection(StrEnum):
    CHINESE_TO_RUSSIAN = "CHINESE_TO_RUSSIAN"
    RUSSIAN_TO_CHINESE = "RUSSIAN_TO_CHINESE"


@dataclass(frozen=True)
class SchedulerConfig:
    desired_retention: float = 0.90
    learning_steps_seconds: tuple[int, ...] = (120, 600)
    relearning_steps_seconds: tuple[int, ...] = (120,)
    minimum_review_interval_days: float = 1.0
    maximum_interval_days: float = 36500.0
    difficulty_min: float = 1.0
    difficulty_max: float = 10.0
    minimum_stability: float = 0.001
    minimum_relearning_stability_days: float = 0.001
    enable_fuzzing: bool = True
    easy_days_percentages: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
    new_card_order: str = "random"
    new_review_order: str = "new_first"
    review_order: str = "due"
    leech_threshold: int = 8
    leech_action: str = "tag_only"
    fsrs_parameters: tuple[float, ...] = FSRS6_DEFAULT_PARAMETERS


DEFAULT_CONFIG = SchedulerConfig()


@dataclass(frozen=True)
class CardState:
    id: int
    hanzi: str
    pinyin: str
    translation: str
    difficulty: float = 5.0
    stability: float = 0.0
    last_review_at: datetime | None = None
    next_review_at: datetime | None = None
    current_interval_days: float = 0.0
    review_count: int = 0
    lapse_count: int = 0
    last_rating: str | None = None
    learning_state: LearningState = LearningState.NEW
    learning_step: int = 0
    priority_boost: bool = False
    is_leech: bool = False
    is_suspended: bool = False

    @classmethod
    def from_mapping(cls, row: Mapping) -> CardState:
        return cls(
            id=int(row["id"]),
            hanzi=row["hanzi"],
            pinyin=row["pinyin"],
            translation=row["translation"],
            difficulty=float(row.get("difficulty", 5.0)),
            stability=float(row.get("stability", 0.0)),
            last_review_at=parse_datetime(row.get("last_review_at")),
            next_review_at=parse_datetime(row.get("next_review_at")),
            current_interval_days=float(row.get("current_interval_days", 0.0)),
            review_count=int(row.get("review_count", 0)),
            lapse_count=int(row.get("lapse_count", 0)),
            last_rating=row.get("last_rating"),
            learning_state=LearningState(row.get("learning_state", LearningState.NEW)),
            learning_step=int(row.get("learning_step", 0)),
            priority_boost=bool(row.get("priority_boost", False)),
            is_leech=bool(row.get("is_leech", False)),
            is_suspended=bool(row.get("is_suspended", False)),
        )


@dataclass(frozen=True)
class ScheduleOutcome:
    card: CardState
    interval: timedelta
    retrievability_before: float
    state_before: LearningState


def ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    # Arithmetic between values with the same local tzinfo follows wall time.
    # Use UTC so a learning interval remains exact across DST transitions.
    return value.astimezone(timezone.utc)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    return ensure_aware(datetime.fromisoformat(value))


def clamp(value: float, minimum: float, maximum: float) -> float:
    if not math.isfinite(value):
        return minimum
    return min(max(value, minimum), maximum)


def validate_config(config: SchedulerConfig) -> None:
    """Validate persisted/user-entered settings before they reach the scheduler."""
    if not 0.70 <= config.desired_retention <= 0.97:
        raise ValueError("Желаемое усвоение должно быть от 70% до 97%")
    for title, steps in (
        ("обучения", config.learning_steps_seconds),
        ("переучивания", config.relearning_steps_seconds),
    ):
        if any(not isinstance(step, int) or step < 1 or step >= 86400 for step in steps):
            raise ValueError(f"Все шаги {title} должны быть короче суток")
        if any(current >= following for current, following in pairwise(steps)):
            raise ValueError(f"Шаги {title} должны идти по возрастанию")
    if not 0 < config.minimum_review_interval_days <= config.maximum_interval_days:
        raise ValueError("Минимальный интервал должен быть меньше максимального")
    if config.new_card_order not in {"added", "random"}:
        raise ValueError("Неизвестный порядок новых карточек")
    if config.new_review_order not in {"new_first", "reviews_first", "mix"}:
        raise ValueError("Неизвестный порядок новых карточек и повторений")
    if config.review_order not in {"due", "retrievability", "random"}:
        raise ValueError("Неизвестный порядок повторений")
    if not 0 <= config.leech_threshold <= 100:
        raise ValueError("Порог трудной карточки должен быть от 0 до 100")
    if config.leech_action not in {"tag_only", "suspend"}:
        raise ValueError("Неизвестное действие для трудной карточки")
    if len(config.easy_days_percentages) != 7 or any(
        not math.isfinite(value) or not 0.0 <= value <= 1.0
        for value in config.easy_days_percentages
    ):
        raise ValueError("Нагрузка по дням недели должна содержать 7 значений от 0 до 1")
    if len(config.fsrs_parameters) != 21:
        raise ValueError("FSRS-6 использует ровно 21 параметр")
    for index, (value, bounds) in enumerate(
        zip(config.fsrs_parameters, FSRS6_PARAMETER_BOUNDS)
    ):
        if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
            raise ValueError(
                f"Параметр FSRS #{index + 1} должен быть от {bounds[0]} до {bounds[1]}"
            )


def _fsrs_constants(config: SchedulerConfig) -> tuple[float, float]:
    decay = -clamp(config.fsrs_parameters[20], 0.1, 0.8)
    factor = 0.9 ** (1 / decay) - 1
    return decay, factor


def calculate_retrievability(
    card: CardState,
    now: datetime,
    config: SchedulerConfig = DEFAULT_CONFIG,
) -> float:
    if not card.last_review_at or card.stability <= 0:
        return 0.0
    elapsed_days = max(
        0.0,
        (ensure_aware(now) - ensure_aware(card.last_review_at)).total_seconds() / 86400,
    )
    decay, factor = _fsrs_constants(config)
    value = (1 + factor * elapsed_days / card.stability) ** decay
    return clamp(value, 0.0, 1.0)


def _difficulty_after(
    difficulty: float, rating: Rating, config: SchedulerConfig
) -> float:
    parameters = config.fsrs_parameters
    grade = list(Rating).index(rating) + 1
    initial_easy = parameters[4] - math.exp(parameters[5] * 3) + 1
    delta = -parameters[6] * (grade - 3)
    damped = difficulty + (10 - difficulty) * delta / 9
    reverted = parameters[7] * initial_easy + (1 - parameters[7]) * damped
    return clamp(reverted, config.difficulty_min, config.difficulty_max)


def _initial_memory(rating: Rating, config: SchedulerConfig) -> tuple[float, float]:
    grade = list(Rating).index(rating) + 1
    parameters = config.fsrs_parameters
    stability = clamp(
        parameters[grade - 1], config.minimum_stability, config.maximum_interval_days
    )
    difficulty = clamp(
        parameters[4] - math.exp(parameters[5] * (grade - 1)) + 1,
        config.difficulty_min,
        config.difficulty_max,
    )
    return stability, difficulty


def _review_stability(
    card: CardState,
    rating: Rating,
    retrievability: float,
    config: SchedulerConfig,
) -> float:
    parameters = config.fsrs_parameters
    base = max(card.stability, config.minimum_stability)
    if rating == Rating.VERY_HARD:
        long_term = (
            parameters[11]
            * card.difficulty ** -parameters[12]
            * ((base + 1) ** parameters[13] - 1)
            * math.exp((1 - retrievability) * parameters[14])
        )
        short_term = base / math.exp(parameters[17] * parameters[18])
        stability = min(long_term, short_term)
    else:
        hard_penalty = parameters[15] if rating == Rating.HARD else 1.0
        easy_bonus = parameters[16] if rating == Rating.EASY else 1.0
        stability = base * (
            1
            + math.exp(parameters[8])
            * (11 - card.difficulty)
            * base ** -parameters[9]
            * (math.exp((1 - retrievability) * parameters[10]) - 1)
            * hard_penalty
            * easy_bonus
        )
    return clamp(stability, config.minimum_stability, config.maximum_interval_days)


def _interval_for_stability(stability: float, config: SchedulerConfig) -> float:
    decay, factor = _fsrs_constants(config)
    retention = clamp(config.desired_retention, 0.70, 0.97)
    days = stability / factor * (retention ** (1 / decay) - 1)
    days = max(config.minimum_review_interval_days, round(days), 1)
    return clamp(days, 1, config.maximum_interval_days)


def interval_for_stability(stability: float, config: SchedulerConfig) -> float:
    """Return the FSRS target interval in days for settings previews/rescheduling."""
    return _interval_for_stability(stability, config)


def _short_term_stability(
    stability: float, rating: Rating, config: SchedulerConfig
) -> float:
    parameters = config.fsrs_parameters
    grade = list(Rating).index(rating) + 1
    increase = math.exp(parameters[17] * (grade - 3 + parameters[18]))
    increase *= max(stability, config.minimum_stability) ** -parameters[19]
    if rating != Rating.VERY_HARD:
        increase = max(increase, 1.0)
    return clamp(
        stability * increase, config.minimum_stability, config.maximum_interval_days
    )


def _hard_step_delay(steps: tuple[int, ...], step: int) -> int | None:
    if not steps:
        return None
    step = min(max(step, 0), len(steps) - 1)
    if step == 0 and len(steps) == 1:
        return round(steps[0] * 1.5)
    if step == 0:
        return round((steps[0] + steps[1]) / 2)
    return steps[step]


def _scheduled_outcome(
    card: CardState,
    rating: Rating,
    now: datetime,
    interval: timedelta,
    state_before: LearningState,
    retrievability: float,
    *,
    stability: float,
    difficulty: float,
    state: LearningState,
    step: int,
    lapse: bool = False,
) -> ScheduleOutcome:
    updated = replace(
        card,
        difficulty=difficulty,
        stability=stability,
        last_review_at=now,
        next_review_at=now + interval,
        current_interval_days=interval.total_seconds() / 86400,
        review_count=card.review_count + 1,
        lapse_count=card.lapse_count + int(lapse),
        last_rating=rating.value,
        learning_state=state,
        learning_step=step,
    )
    return ScheduleOutcome(updated, interval, retrievability, state_before)


def _with_leech_action(
    outcome: ScheduleOutcome, config: SchedulerConfig
) -> ScheduleOutcome:
    lapses = outcome.card.lapse_count
    threshold = config.leech_threshold
    repeat = max(1, threshold // 2)
    threshold_met = threshold > 0 and lapses >= threshold and (
        lapses == threshold or (lapses - threshold) % repeat == 0
    )
    if not threshold_met:
        return outcome
    card = replace(
        outcome.card,
        is_leech=True,
        is_suspended=config.leech_action == "suspend",
    )
    return replace(outcome, card=card)


def _review_interval(
    stability: float,
    config: SchedulerConfig,
    card: CardState | None = None,
    now: datetime | None = None,
) -> timedelta:
    days = _interval_for_stability(stability, config)
    if config.enable_fuzzing and days >= 2.5 and card is not None and now is not None:
        delta = 0.0
        for start, end, factor in ((2.5, 7.0, 0.15), (7.0, 20.0, 0.10), (20.0, math.inf, 0.05)):
            delta += max(0.0, min(days, end) - start) * factor
        digest = hashlib.sha256(f"{card.id}:{now.date().isoformat()}".encode()).digest()
        position = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        lower = max(1, math.ceil(days - delta))
        upper = min(math.floor(config.maximum_interval_days), math.floor(days + delta))
        candidates = list(range(lower, upper + 1))
        weights = [
            config.easy_days_percentages[(now + timedelta(days=candidate)).weekday()]
            for candidate in candidates
        ]
        total_weight = sum(weights)
        if candidates and total_weight > 0:
            target = position * total_weight
            cumulative = 0.0
            for candidate, weight in zip(candidates, weights):
                cumulative += weight
                if target <= cumulative:
                    days = candidate
                    break
        else:
            days = clamp(round(days), 1, config.maximum_interval_days)
    return timedelta(days=days)


def schedule_rating(
    card: CardState,
    rating: Rating,
    reviewed_at: datetime,
    config: SchedulerConfig = DEFAULT_CONFIG,
) -> ScheduleOutcome:
    """Schedule one logical review using FSRS-6 state transitions."""
    now = ensure_aware(reviewed_at)
    state_before = card.learning_state
    retrievability = calculate_retrievability(card, now, config)
    learning_steps = tuple(config.learning_steps_seconds)
    relearning_steps = tuple(config.relearning_steps_seconds)

    if state_before == LearningState.NEW:
        stability, difficulty = _initial_memory(rating, config)
        if rating == Rating.VERY_HARD and learning_steps:
            return _scheduled_outcome(
                card, rating, now, timedelta(seconds=learning_steps[0]), state_before,
                retrievability, stability=stability, difficulty=difficulty,
                state=LearningState.LEARNING, step=0,
            )
        if rating == Rating.HARD and learning_steps:
            seconds = _hard_step_delay(learning_steps, 0)
            return _scheduled_outcome(
                card, rating, now, timedelta(seconds=seconds), state_before,
                retrievability, stability=stability, difficulty=difficulty,
                state=LearningState.LEARNING, step=0,
            )
        if rating == Rating.MEDIUM and len(learning_steps) > 1:
            return _scheduled_outcome(
                card, rating, now, timedelta(seconds=learning_steps[1]), state_before,
                retrievability, stability=stability, difficulty=difficulty,
                state=LearningState.LEARNING, step=1,
            )
        return _scheduled_outcome(
            card, rating, now, _review_interval(stability, config, card, now), state_before,
            retrievability, stability=stability, difficulty=difficulty,
            state=LearningState.REVIEW, step=0,
        )

    difficulty = _difficulty_after(card.difficulty, rating, config)
    elapsed = (
        (now - ensure_aware(card.last_review_at)).total_seconds()
        if card.last_review_at else math.inf
    )
    stability = (
        _short_term_stability(card.stability, rating, config)
        if elapsed < 86400
        else _review_stability(card, rating, retrievability, config)
    )

    if state_before in {LearningState.LEARNING, LearningState.RELEARNING}:
        steps = learning_steps if state_before == LearningState.LEARNING else relearning_steps
        if rating == Rating.VERY_HARD and steps:
            return _scheduled_outcome(
                card, rating, now, timedelta(seconds=steps[0]), state_before,
                retrievability, stability=stability, difficulty=difficulty,
                state=state_before, step=0,
            )
        if rating == Rating.HARD and steps:
            seconds = _hard_step_delay(steps, card.learning_step)
            return _scheduled_outcome(
                card, rating, now, timedelta(seconds=seconds), state_before,
                retrievability, stability=stability, difficulty=difficulty,
                state=state_before, step=min(card.learning_step, len(steps) - 1),
            )
        next_step = card.learning_step + 1
        if rating == Rating.MEDIUM and next_step < len(steps):
            return _scheduled_outcome(
                card, rating, now, timedelta(seconds=steps[next_step]), state_before,
                retrievability, stability=stability, difficulty=difficulty,
                state=state_before, step=next_step,
            )
        return _scheduled_outcome(
            card, rating, now, _review_interval(stability, config, card, now), state_before,
            retrievability, stability=stability, difficulty=difficulty,
            state=LearningState.REVIEW, step=0,
        )

    # Review cards use the full FSRS memory update. A failed recall enters the
    # configurable relearning ladder, or stays in review if that ladder is empty.
    if rating == Rating.VERY_HARD and relearning_steps:
        stability = max(stability, config.minimum_relearning_stability_days)
        return _with_leech_action(_scheduled_outcome(
            card, rating, now, timedelta(seconds=relearning_steps[0]), state_before,
            retrievability, stability=stability, difficulty=difficulty,
            state=LearningState.RELEARNING, step=0, lapse=True,
        ), config)
    outcome = _scheduled_outcome(
        card, rating, now, _review_interval(stability, config, card, now), state_before,
        retrievability, stability=stability, difficulty=difficulty,
        state=LearningState.REVIEW, step=0,
        lapse=rating == Rating.VERY_HARD,
    )
    return _with_leech_action(outcome, config) if rating == Rating.VERY_HARD else outcome


def preview_ratings(
    card: CardState,
    now: datetime,
    config: SchedulerConfig = DEFAULT_CONFIG,
) -> dict[Rating, timedelta]:
    return {
        rating: schedule_rating(card, rating, now, config).interval
        for rating in Rating
    }


def format_interval(interval: timedelta) -> str:
    seconds = max(0, round(interval.total_seconds()))
    if seconds < 3600:
        minutes = max(1, round(seconds / 60))
        return f"{minutes} мин"
    days = seconds / 86400
    if days < 1:
        return f"{max(1, round(seconds / 3600))} ч"
    rounded = max(1, round(days))
    if rounded % 10 == 1 and rounded % 100 != 11:
        suffix = "день"
    elif rounded % 10 in {2, 3, 4} and rounded % 100 not in {12, 13, 14}:
        suffix = "дня"
    else:
        suffix = "дней"
    return f"{rounded} {suffix}"
