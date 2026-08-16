import math
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from scheduler import (
    CardState,
    LearningState,
    Rating,
    SchedulerConfig,
    calculate_retrievability,
    preview_ratings,
    schedule_rating,
)

NOW = datetime(2026, 12, 31, 23, 59, tzinfo=timezone.utc)


def new_card() -> CardState:
    return CardState(1, "苹果", "píng guǒ", "яблоко")


class SchedulerTests(unittest.TestCase):
    def test_new_card_intervals(self):
        previews = preview_ratings(new_card(), NOW)
        self.assertEqual(previews[Rating.VERY_HARD], timedelta(minutes=2))
        self.assertEqual(previews[Rating.HARD], timedelta(days=1))
        self.assertEqual(previews[Rating.MEDIUM], timedelta(days=3))
        self.assertEqual(previews[Rating.EASY], timedelta(days=8))

    def test_learning_chain_two_then_ten_minutes_then_review(self):
        first = schedule_rating(new_card(), Rating.VERY_HARD, NOW)
        self.assertEqual(first.interval, timedelta(minutes=2))
        self.assertEqual(first.card.learning_state, LearningState.LEARNING)
        second_time = NOW + first.interval
        second = schedule_rating(first.card, Rating.HARD, second_time)
        self.assertEqual(second.interval, timedelta(minutes=10))
        self.assertEqual(second.card.learning_step, 1)
        third = schedule_rating(second.card, Rating.MEDIUM, second_time + second.interval)
        self.assertEqual(third.card.learning_state, LearningState.REVIEW)
        self.assertEqual(third.interval, timedelta(days=3))

    def test_learning_buttons_do_not_collapse_to_one_interval(self):
        failed = schedule_rating(new_card(), Rating.VERY_HARD, NOW)
        previews = preview_ratings(failed.card, NOW + failed.interval)
        self.assertEqual(
            previews,
            {
                Rating.VERY_HARD: timedelta(minutes=2),
                Rating.HARD: timedelta(minutes=10),
                Rating.MEDIUM: timedelta(days=1),
                Rating.EASY: timedelta(days=3),
            },
        )

        hard = schedule_rating(failed.card, Rating.HARD, NOW + failed.interval)
        previews = preview_ratings(hard.card, NOW + failed.interval + hard.interval)
        self.assertEqual(
            previews,
            {
                Rating.VERY_HARD: timedelta(minutes=2),
                Rating.HARD: timedelta(days=1),
                Rating.MEDIUM: timedelta(days=3),
                Rating.EASY: timedelta(days=8),
            },
        )

    def test_mature_lapse_preserves_history_and_some_stability(self):
        mature = replace(
            new_card(),
            stability=100,
            current_interval_days=100,
            review_count=12,
            lapse_count=2,
            last_review_at=NOW - timedelta(days=100),
            next_review_at=NOW,
            learning_state=LearningState.REVIEW,
        )
        outcome = schedule_rating(mature, Rating.VERY_HARD, NOW)
        self.assertEqual(outcome.card.learning_state, LearningState.RELEARNING)
        self.assertEqual(outcome.interval, timedelta(minutes=2))
        self.assertEqual(outcome.card.review_count, 13)
        self.assertEqual(outcome.card.lapse_count, 3)
        self.assertGreater(outcome.card.stability, 0)
        self.assertLess(outcome.card.stability, mature.stability)

    def test_successful_review_intervals_are_ordered_and_adaptive(self):
        mature = replace(
            new_card(),
            difficulty=5,
            stability=20,
            current_interval_days=20,
            review_count=5,
            last_review_at=NOW - timedelta(days=20),
            next_review_at=NOW,
            learning_state=LearningState.REVIEW,
        )
        outcomes = {
            rating: schedule_rating(mature, rating, NOW)
            for rating in (Rating.HARD, Rating.MEDIUM, Rating.EASY)
        }
        self.assertLess(outcomes[Rating.HARD].interval, outcomes[Rating.MEDIUM].interval)
        self.assertLess(outcomes[Rating.MEDIUM].interval, outcomes[Rating.EASY].interval)
        self.assertLess(outcomes[Rating.HARD].card.stability, outcomes[Rating.MEDIUM].card.stability)
        self.assertLess(outcomes[Rating.MEDIUM].card.stability, outcomes[Rating.EASY].card.stability)
        self.assertGreater(outcomes[Rating.HARD].card.difficulty, mature.difficulty)
        self.assertLess(outcomes[Rating.EASY].card.difficulty, mature.difficulty)

    def test_very_hard_increases_difficulty_more_than_hard(self):
        very_hard = schedule_rating(new_card(), Rating.VERY_HARD, NOW)
        hard = schedule_rating(new_card(), Rating.HARD, NOW)
        self.assertGreater(very_hard.card.difficulty, hard.card.difficulty)

    def test_review_interval_depends_on_card_difficulty(self):
        common = replace(
            new_card(),
            stability=20,
            current_interval_days=20,
            review_count=4,
            last_review_at=NOW - timedelta(days=20),
            learning_state=LearningState.REVIEW,
        )
        easier = schedule_rating(replace(common, difficulty=2), Rating.MEDIUM, NOW)
        harder = schedule_rating(replace(common, difficulty=9), Rating.MEDIUM, NOW)
        self.assertGreater(easier.interval, harder.interval)

    def test_preview_is_pure(self):
        card = new_card()
        before = card
        first = preview_ratings(card, NOW)
        second = preview_ratings(card, NOW)
        self.assertEqual(card, before)
        self.assertEqual(first, second)

    def test_retrievability_decreases_over_time(self):
        card = replace(new_card(), stability=10, last_review_at=NOW)
        early = calculate_retrievability(card, NOW + timedelta(days=1))
        late = calculate_retrievability(card, NOW + timedelta(days=20))
        self.assertGreater(early, late)
        self.assertAlmostEqual(calculate_retrievability(card, NOW + timedelta(days=10)), 0.9)

    def test_invalid_retention_config_cannot_crash_retrievability(self):
        card = replace(new_card(), stability=10, last_review_at=NOW)
        for retention in (0, -1, float("nan")):
            value = calculate_retrievability(
                card,
                NOW + timedelta(days=10),
                SchedulerConfig(desired_retention=retention),
            )
            self.assertTrue(math.isfinite(value))
            self.assertGreaterEqual(value, 0)
            self.assertLessEqual(value, 1)

    def test_relearning_stability_floor_comes_from_config(self):
        mature = replace(
            new_card(),
            stability=1,
            current_interval_days=1,
            review_count=3,
            last_review_at=NOW - timedelta(days=1),
            learning_state=LearningState.REVIEW,
        )
        config = SchedulerConfig(minimum_relearning_stability_days=0.25)
        outcome = schedule_rating(mature, Rating.VERY_HARD, NOW, config)
        self.assertAlmostEqual(outcome.card.stability, 0.45)

    def test_real_datetime_arithmetic_crosses_boundaries(self):
        first = schedule_rating(new_card(), Rating.VERY_HARD, NOW)
        self.assertEqual(first.card.next_review_at, datetime(2027, 1, 1, 0, 1, tzinfo=timezone.utc))
        learning = schedule_rating(first.card, Rating.HARD, first.card.next_review_at)
        self.assertEqual(learning.card.next_review_at.minute, 11)

    def test_maximum_interval_and_math_safety(self):
        config = SchedulerConfig(maximum_interval_days=3650)
        huge = replace(
            new_card(),
            difficulty=1,
            stability=3600,
            current_interval_days=3600,
            review_count=500,
            last_review_at=NOW - timedelta(days=5000),
            learning_state=LearningState.REVIEW,
        )
        outcome = schedule_rating(huge, Rating.EASY, NOW, config)
        self.assertLessEqual(outcome.interval, timedelta(days=3650))
        self.assertTrue(math.isfinite(outcome.card.stability))
        self.assertGreater(outcome.card.stability, 0)

        failed = schedule_rating(
            replace(huge, stability=float("inf")), Rating.VERY_HARD, NOW, config
        )
        self.assertTrue(math.isfinite(failed.card.stability))
        self.assertLessEqual(failed.card.stability, config.maximum_interval_days)


if __name__ == "__main__":
    unittest.main()
