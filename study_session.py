from __future__ import annotations

import random
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from scheduler import (
    CardState,
    Rating,
    ReviewDirection,
    ensure_aware,
    parse_datetime,
    schedule_rating,
)
from study_database import StudyRepository, local_date


class RandomSource(Protocol):
    def choice(self, sequence): ...
    def shuffle(self, sequence) -> None: ...


@dataclass(frozen=True)
class DailySelection:
    card_id: int
    direction: ReviewDirection


@dataclass(frozen=True)
class SessionItem:
    card: CardState
    direction: ReviewDirection
    presentation_id: int
    shown_at: datetime
    is_relearning_repeat: bool
    learning_queue_id: int | None = None


def choose_direction(rng: RandomSource = random) -> ReviewDirection:
    return rng.choice(
        [ReviewDirection.CHINESE_TO_RUSSIAN, ReviewDirection.RUSSIAN_TO_CHINESE]
    )


def select_daily_cards(
    cards: list[CardState],
    existing_card_ids: set[int],
    daily_limit: int,
    now: datetime,
    rng: RandomSource = random,
) -> list[DailySelection]:
    """Сначала выбирает незавершённые учебные и NEW, затем обычные due."""
    remaining = max(0, int(daily_limit) - len(existing_card_ids))
    if remaining == 0:
        return []
    now = ensure_aware(now)
    # The database has a primary key, but keeping the pure selector defensive
    # guarantees the central rule: one cardId can occupy only one daily slot.
    available_by_id: dict[int, CardState] = {}
    for card in cards:
        if card.id not in existing_card_ids:
            available_by_id.setdefault(card.id, card)
    available = list(available_by_id.values())
    priority_learning = [
        card
        for card in available
        if card.priority_boost
        or card.learning_state.value == "NEW"
        or (
            card.learning_state.value == "LEARNING"
            and (
                card.next_review_at is None
                or ensure_aware(card.next_review_at) <= now
            )
        )
    ]
    priority_learning.sort(
        key=lambda card: (
            0
            if card.priority_boost
            else (1 if card.learning_state.value == "LEARNING" else 2),
            (
                ensure_aware(card.next_review_at).timestamp()
                if card.next_review_at is not None
                else float("-inf")
            ),
            card.id,
        )
    )
    priority_selected = priority_learning[:remaining]
    remaining -= len(priority_selected)

    due = [
        card
        for card in available
        if not card.priority_boost
        and card.learning_state.value not in {"NEW", "LEARNING"}
        and (card.next_review_at is None or ensure_aware(card.next_review_at) <= now)
    ]
    due.sort(
        key=lambda card: (
            ensure_aware(card.next_review_at).timestamp()
            if card.next_review_at is not None
            else float("-inf"),
            card.id,
        )
    )
    due_selected = due[:remaining]
    # Ручное повышение, учебные карточки и due не смешиваются между собой.
    boosted_selected = [card for card in priority_selected if card.priority_boost]
    learning_selected = [
        card for card in priority_selected if not card.priority_boost
    ]
    rng.shuffle(boosted_selected)
    rng.shuffle(learning_selected)
    rng.shuffle(due_selected)
    selected = boosted_selected + learning_selected + due_selected
    return [DailySelection(card.id, choose_direction(rng)) for card in selected]


class StudySessionService:
    def __init__(
        self,
        repository: StudyRepository,
        rng: RandomSource = random,
    ) -> None:
        self.repository = repository
        self.rng = rng
        # A persisted active presentation is resumed after application restart.
        # Its direction remains stable, while the response timer starts from the
        # moment it becomes visible in this service instance.
        self._seen_card_ids: set[int] = set()
        self._prepared_dates: dict[str, tuple[int, int]] = {}

    def _begin_presentation(
        self,
        card_id: int,
        direction: ReviewDirection,
        shown_at: datetime,
    ) -> dict:
        presentation = self.repository.begin_presentation(
            card_id,
            direction,
            shown_at,
            resume_active=card_id not in self._seen_card_ids,
        )
        self._seen_card_ids.add(card_id)
        return presentation

    def ensure_daily_session(self, now: datetime) -> None:
        date_key = local_date(now)
        limit = self.repository.get_daily_session_limit(date_key)
        preparation_signature = (self.repository.get_card_count(), limit)
        if self._prepared_dates.get(date_key) != preparation_signature:
            # После обновления приоритета заменяем лишь ещё не открытые строки.
            # Завершённые, активные и ожидающие вторую сторону остаются на месте.
            self.repository.clear_unstarted_daily_rows(date_key)
            self._prepared_dates[date_key] = preparation_signature
        existing = self.repository.daily_rows(date_key)
        existing_ids = {int(row["card_id"]) for row in existing}
        if len(existing_ids) >= limit:
            return
        selections = select_daily_cards(
            self.repository.get_cards(), existing_ids, limit, now, self.rng
        )
        start_position = max((int(row["position"]) for row in existing), default=-1) + 1
        rows = [
            (selection.card_id, selection.direction.value, start_position + index)
            for index, selection in enumerate(selections)
        ]
        self.repository.add_daily_rows(date_key, rows, now)

    def can_continue(self, now: datetime) -> bool:
        """Whether another due/new logical card can be added without moving its due date."""
        self.ensure_daily_session(now)
        existing_ids = {
            int(row["card_id"])
            for row in self.repository.daily_rows(local_date(now))
        }
        now = ensure_aware(now)
        for card in self.repository.get_cards():
            if card.id in existing_ids:
                continue
            if card.priority_boost or card.learning_state.value == "NEW":
                return True
            if (
                card.learning_state.value == "LEARNING"
                and (
                    card.next_review_at is None
                    or ensure_aware(card.next_review_at) <= now
                )
            ):
                return True
            if (
                card.learning_state.value not in {"NEW", "LEARNING"}
                and (
                    card.next_review_at is None
                    or ensure_aware(card.next_review_at) <= now
                )
            ):
                return True
        return False

    def continue_daily_session(self, now: datetime) -> int:
        """Add one more normal-size batch for today and return its actual size."""
        self.ensure_daily_session(now)
        date_key = local_date(now)
        existing = self.repository.daily_rows(date_key)
        existing_ids = {int(row["card_id"]) for row in existing}
        batch_size = self.repository.get_daily_limit()
        selections = select_daily_cards(
            self.repository.get_cards(),
            existing_ids,
            len(existing_ids) + batch_size,
            now,
            self.rng,
        )
        if not selections:
            return 0
        start_position = max(
            (int(row["position"]) for row in existing), default=-1
        ) + 1
        rows = [
            (selection.card_id, selection.direction.value, start_position + index)
            for index, selection in enumerate(selections)
        ]
        self.repository.add_daily_rows(date_key, rows, now)
        target = self.repository.set_daily_session_limit(
            date_key, len(existing_ids) + len(selections)
        )
        self.repository.set_daily_batch_start(date_key, len(existing_ids))
        self._prepared_dates[date_key] = (
            self.repository.get_card_count(),
            target,
        )
        return len(selections)

    def invalidate_daily_priority(self, now: datetime) -> None:
        self._prepared_dates.pop(local_date(now), None)

    def next_item(self, now: datetime) -> SessionItem | None:
        self.ensure_daily_session(now)
        date_key = local_date(now)
        learning_items = self.repository.get_immediate_learning(now)
        daily_queue = self.repository.get_daily_queue(date_key, now)
        immediate = None
        if learning_items and daily_queue:
            if self.rng.choice(["LEARNING", "DAILY"]) == "LEARNING":
                immediate = self.rng.choice(learning_items)
        elif learning_items:
            immediate = self.rng.choice(learning_items)
        if immediate:
            card = CardState.from_mapping(immediate)
            proposed_direction = ReviewDirection(immediate["queued_direction"])
            presentation = self._begin_presentation(
                card.id, proposed_direction, now
            )
            return SessionItem(
                card=card,
                direction=ReviewDirection(presentation["direction"]),
                presentation_id=int(presentation["id"]),
                shown_at=parse_datetime(presentation["shown_at"]),
                is_relearning_repeat=True,
                learning_queue_id=int(immediate["learning_queue_id"]),
            )
        if not daily_queue:
            return None
        row = daily_queue[0]
        card = CardState.from_mapping(row)
        is_repeat = bool(row["daily_completed"])
        proposed_direction = (
            choose_direction(self.rng)
            if is_repeat
            else ReviewDirection(row["daily_direction"])
        )
        presentation = self._begin_presentation(card.id, proposed_direction, now)
        return SessionItem(
            card=card,
            direction=ReviewDirection(presentation["direction"]),
            presentation_id=int(presentation["id"]),
            shown_at=parse_datetime(presentation["shown_at"]),
            is_relearning_repeat=is_repeat,
            learning_queue_id=None,
        )

    def answer(
        self,
        item: SessionItem,
        rating: Rating,
        reviewed_at: datetime,
        response_time_ms: int,
    ) -> CardState:
        current = self.repository.get_card(item.card.id)
        if current is None:
            raise LookupError(f"Карточка {item.card.id} не найдена")
        outcome = schedule_rating(current, rating, reviewed_at)
        pair_matches = self.repository.save_review(
            current,
            outcome,
            rating,
            item.direction,
            reviewed_at,
            response_time_ms,
            item.presentation_id,
            item.learning_queue_id,
        )
        return (
            replace(outcome.card, priority_boost=False)
            if pair_matches
            else current
        )

    def progress(self, now: datetime) -> tuple[int, int]:
        date_key = local_date(now)
        completed, planned = self.repository.get_daily_progress(date_key)
        cumulative_total = max(
            completed,
            planned,
            self.repository.get_daily_session_limit(date_key),
        )
        batch_start = min(
            self.repository.get_daily_batch_start(date_key),
            completed,
            cumulative_total,
        )
        return completed - batch_start, cumulative_total - batch_start
