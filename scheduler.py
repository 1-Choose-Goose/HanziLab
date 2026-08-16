from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum


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
    first_again_interval_minutes: int = 2
    second_again_interval_minutes: int = 10
    hard_initial_interval_days: float = 1.0
    medium_initial_interval_days: float = 3.0
    easy_initial_interval_days: float = 8.0
    minimum_review_interval_days: float = 1.0
    maximum_interval_days: float = 3650.0
    difficulty_min: float = 1.0
    difficulty_max: float = 10.0
    initial_difficulty: float = 5.0
    minimum_stability: float = 0.1
    minimum_relearning_stability_days: float = 0.5


DEFAULT_CONFIG = SchedulerConfig()


@dataclass(frozen=True)
class CardState:
    id: int
    hanzi: str
    pinyin: str
    translation: str
    difficulty: float = DEFAULT_CONFIG.initial_difficulty
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

    @classmethod
    def from_mapping(cls, row: Mapping) -> CardState:
        return cls(
            id=int(row["id"]),
            hanzi=row["hanzi"],
            pinyin=row["pinyin"],
            translation=row["translation"],
            difficulty=float(row.get("difficulty", DEFAULT_CONFIG.initial_difficulty)),
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
    return value


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
    # A malformed user config must not make the scheduler crash in ``log``.
    # With the normal 0.90 setting this normalization does not change anything.
    retention = clamp(config.desired_retention, 1e-6, 1.0)
    value = math.exp(math.log(retention) * elapsed_days / card.stability)
    return clamp(value, 0.0, 1.0)


def _difficulty_after(
    difficulty: float, rating: Rating, config: SchedulerConfig
) -> float:
    deltas = {
        Rating.VERY_HARD: 1.20,
        Rating.HARD: 0.35,
        Rating.MEDIUM: -0.05,
        Rating.EASY: -0.45,
    }
    return clamp(difficulty + deltas[rating], config.difficulty_min, config.difficulty_max)


def _review_stability(
    card: CardState,
    rating: Rating,
    retrievability: float,
    config: SchedulerConfig,
) -> float:
    base = max(card.stability, card.current_interval_days, config.minimum_review_interval_days)
    difficulty_span = max(config.difficulty_max - config.difficulty_min, 1e-9)
    difficulty_relief = clamp(
        (config.difficulty_max - card.difficulty) / difficulty_span,
        0.0,
        1.0,
    )
    forgetting_bonus = 1.0 - retrievability
    experience = min(math.log1p(max(card.review_count, 0)) * 0.035, 0.22)
    lapse_penalty = 1.0 / (1.0 + 0.035 * max(card.lapse_count, 0))
    if rating == Rating.HARD:
        growth = 1.16 + 0.18 * forgetting_bonus + 0.12 * difficulty_relief + experience * 0.3
    elif rating == Rating.MEDIUM:
        growth = 1.78 + 0.52 * forgetting_bonus + 0.30 * difficulty_relief + experience
    else:
        growth = 2.58 + 0.82 * forgetting_bonus + 0.55 * difficulty_relief + experience * 1.4
    return clamp(
        base * growth * lapse_penalty,
        config.minimum_review_interval_days,
        config.maximum_interval_days,
    )


def _daily_outcome(
    card: CardState,
    rating: Rating,
    now: datetime,
    days: float,
    config: SchedulerConfig,
    state_before: LearningState,
    retrievability: float,
) -> ScheduleOutcome:
    days = clamp(days, config.minimum_review_interval_days, config.maximum_interval_days)
    interval = timedelta(days=days)
    updated = replace(
        card,
        difficulty=_difficulty_after(card.difficulty, rating, config),
        stability=days,
        last_review_at=now,
        next_review_at=now + interval,
        current_interval_days=days,
        review_count=card.review_count + 1,
        last_rating=rating.value,
        learning_state=LearningState.REVIEW,
        learning_step=0,
    )
    return ScheduleOutcome(updated, interval, retrievability, state_before)


def schedule_rating(
    card: CardState,
    rating: Rating,
    reviewed_at: datetime,
    config: SchedulerConfig = DEFAULT_CONFIG,
) -> ScheduleOutcome:
    now = ensure_aware(reviewed_at)
    state_before = card.learning_state
    retrievability = calculate_retrievability(card, now, config)
    difficulty = _difficulty_after(card.difficulty, rating, config)

    if rating == Rating.VERY_HARD:
        interval = timedelta(minutes=config.first_again_interval_minutes)
        was_review = state_before == LearningState.REVIEW
        proposed_stability = (
            max(
                card.stability * 0.45,
                config.minimum_relearning_stability_days,
            )
            if was_review or state_before == LearningState.RELEARNING
            else config.minimum_stability
        )
        stability = clamp(
            proposed_stability,
            config.minimum_stability,
            config.maximum_interval_days,
        )
        updated = replace(
            card,
            difficulty=difficulty,
            stability=stability,
            last_review_at=now,
            next_review_at=now + interval,
            current_interval_days=interval.total_seconds() / 86400,
            review_count=card.review_count + 1,
            lapse_count=card.lapse_count + (1 if was_review else 0),
            last_rating=rating.value,
            learning_state=(
                LearningState.RELEARNING
                if was_review or state_before == LearningState.RELEARNING
                else LearningState.LEARNING
            ),
            learning_step=0,
        )
        return ScheduleOutcome(updated, interval, retrievability, state_before)

    if state_before in {LearningState.LEARNING, LearningState.RELEARNING}:
        if card.learning_step == 0:
            if rating == Rating.HARD:
                interval = timedelta(minutes=config.second_again_interval_minutes)
                updated = replace(
                    card,
                    difficulty=difficulty,
                    stability=max(card.stability, config.minimum_stability),
                    last_review_at=now,
                    next_review_at=now + interval,
                    current_interval_days=interval.total_seconds() / 86400,
                    review_count=card.review_count + 1,
                    last_rating=rating.value,
                    learning_step=1,
                )
                return ScheduleOutcome(updated, interval, retrievability, state_before)

            if state_before == LearningState.LEARNING:
                first_success_days = {
                    Rating.MEDIUM: config.hard_initial_interval_days,
                    Rating.EASY: config.medium_initial_interval_days,
                }
            else:
                recovered_base = max(
                    card.stability, config.minimum_review_interval_days
                )
                first_success_days = {
                    Rating.MEDIUM: max(
                        config.hard_initial_interval_days, recovered_base * 0.5
                    ),
                    Rating.EASY: max(
                        config.medium_initial_interval_days, recovered_base * 0.8
                    ),
                }
            return _daily_outcome(
                card,
                rating,
                now,
                first_success_days[rating],
                config,
                state_before,
                retrievability,
            )

        if state_before == LearningState.LEARNING:
            graduation_days = {
                Rating.HARD: config.hard_initial_interval_days,
                Rating.MEDIUM: config.medium_initial_interval_days,
                Rating.EASY: config.easy_initial_interval_days,
            }
        else:
            recovered_base = max(card.stability, config.minimum_review_interval_days)
            graduation_days = {
                Rating.HARD: max(
                    config.hard_initial_interval_days, recovered_base * 0.5
                ),
                Rating.MEDIUM: max(
                    config.medium_initial_interval_days, recovered_base * 0.8
                ),
                Rating.EASY: max(
                    config.easy_initial_interval_days, recovered_base
                ),
            }
        return _daily_outcome(
            card,
            rating,
            now,
            graduation_days[rating],
            config,
            state_before,
            retrievability,
        )

    if state_before == LearningState.NEW:
        initial_days = {
            Rating.HARD: config.hard_initial_interval_days,
            Rating.MEDIUM: config.medium_initial_interval_days,
            Rating.EASY: config.easy_initial_interval_days,
        }[rating]
        return _daily_outcome(
            card, rating, now, initial_days, config, state_before, retrievability
        )

    stability = _review_stability(card, rating, retrievability, config)
    return _daily_outcome(
        card, rating, now, stability, config, state_before, retrievability
    )


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
