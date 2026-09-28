import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scheduler import (
    CardState,
    LearningState,
    Rating,
    ReviewDirection,
    SchedulerConfig,
    preview_ratings,
)
from study_database import (
    SCHEMA_VERSION,
    StudyRepository,
    initialize_study_database,
    local_date,
)
from study_session import StudySessionService, choose_direction, select_daily_cards

NOW = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)


class DeterministicRng:
    def __init__(self, choices=None):
        self.choices = list(choices or [0])
        self.index = 0

    def choice(self, sequence):
        value = sequence[self.choices[self.index % len(self.choices)] % len(sequence)]
        self.index += 1
        return value

    def shuffle(self, sequence):
        sequence.reverse()


def card(card_id: int, state=LearningState.NEW, due=None) -> CardState:
    return CardState(
        card_id,
        f"词{card_id}",
        f"cí {card_id}",
        f"слово {card_id}",
        stability=10 if state == LearningState.REVIEW else 0,
        current_interval_days=10 if state == LearningState.REVIEW else 0,
        review_count=2 if state == LearningState.REVIEW else 0,
        next_review_at=due,
        learning_state=state,
    )


class SelectionTests(unittest.TestCase):
    def test_direction_is_injectable_and_can_return_both_values(self):
        rng = DeterministicRng([0, 1])
        self.assertEqual(choose_direction(rng), ReviewDirection.CHINESE_TO_RUSSIAN)
        self.assertEqual(choose_direction(rng), ReviewDirection.RUSSIAN_TO_CHINESE)

    def test_daily_limit_selects_unique_logical_cards(self):
        cards = [card(i, LearningState.REVIEW, NOW - timedelta(days=i)) for i in range(1, 101)]
        selected = select_daily_cards(cards, set(), 30, NOW, DeterministicRng([0, 1]))
        self.assertEqual(len(selected), 30)
        self.assertEqual(len({item.card_id for item in selected}), 30)

    def test_duplicate_source_rows_still_take_one_daily_slot_per_card_id(self):
        source = [card(1), card(1), card(2), card(2), card(3)]
        selected = select_daily_cards(source, set(), 30, NOW, DeterministicRng())
        self.assertEqual({item.card_id for item in selected}, {1, 2, 3})
        self.assertEqual(len(selected), 3)

    def test_most_overdue_cards_win_priority_before_shuffle(self):
        cards = [card(i, LearningState.REVIEW, NOW - timedelta(days=i)) for i in range(1, 46)]
        selected = select_daily_cards(cards, set(), 30, NOW, DeterministicRng())
        self.assertEqual({item.card_id for item in selected}, set(range(16, 46)))

    def test_existing_daily_card_cannot_enter_limit_twice(self):
        cards = [card(i) for i in range(1, 10)]
        selected = select_daily_cards(cards, {1, 2}, 5, NOW, DeterministicRng())
        self.assertEqual(len(selected), 3)
        self.assertFalse({1, 2} & {item.card_id for item in selected})

    def test_new_cards_have_priority_over_due(self):
        due = [card(i, LearningState.REVIEW, NOW - timedelta(days=i)) for i in range(1, 46)]
        new = [card(i) for i in range(100, 200)]
        selected = select_daily_cards(due + new, set(), 30, NOW, DeterministicRng())
        self.assertTrue(all(item.card_id >= 100 for item in selected))

    def test_due_fills_remaining_places_after_new(self):
        due = [card(i, LearningState.REVIEW, NOW) for i in range(1, 46)]
        new = [card(i) for i in range(100, 110)]
        selected = select_daily_cards(due + new, set(), 30, NOW, DeterministicRng())
        ids = [item.card_id for item in selected]
        self.assertEqual(set(ids[:10]), set(range(100, 110)))
        self.assertEqual(len(set(ids[10:]) & set(range(1, 46))), 20)

    def test_new_card_limit_reserves_remaining_places_for_reviews(self):
        due = [card(i, LearningState.REVIEW, NOW) for i in range(1, 10)]
        new = [card(i) for i in range(100, 110)]
        selected = select_daily_cards(
            due + new, set(), 5, NOW, DeterministicRng(), new_limit=2
        )
        ids = [item.card_id for item in selected]
        self.assertEqual(sum(item >= 100 for item in ids), 2)
        self.assertEqual(sum(item < 100 for item in ids), 3)

    def test_due_learning_step_is_prioritized_before_completely_new(self):
        learning = card(50, LearningState.LEARNING, NOW)
        new = [card(i) for i in range(100, 140)]
        selected = select_daily_cards(
            [*new, learning], set(), 30, NOW, DeterministicRng()
        )
        self.assertIn(50, {item.card_id for item in selected})

    def test_shuffle_does_not_move_new_cards_before_due_learning_cards(self):
        selected = select_daily_cards(
            [card(1), card(2, LearningState.LEARNING, NOW)],
            set(), 2, NOW, DeterministicRng(),
        )
        self.assertEqual([item.card_id for item in selected], [2, 1])

    def test_future_review_cards_are_not_taken_early(self):
        due = [card(i, LearningState.REVIEW, NOW) for i in range(1, 16)]
        new = [card(i) for i in range(100, 105)]
        future = [card(i, LearningState.REVIEW, NOW + timedelta(days=10)) for i in range(200, 300)]
        selected = select_daily_cards(due + new + future, set(), 30, NOW, DeterministicRng())
        self.assertEqual(len(selected), 20)
        self.assertTrue(all(item.card_id >= 100 for item in selected[:5]))
        self.assertFalse({item.card_id for item in selected} & set(range(200, 300)))

    def test_config_can_put_reviews_before_new_cards(self):
        selected = select_daily_cards(
            [card(10), card(1, LearningState.REVIEW, NOW)],
            set(),
            2,
            NOW,
            DeterministicRng(),
            config=SchedulerConfig(new_review_order="reviews_first"),
        )
        self.assertEqual([item.card_id for item in selected], [1, 10])

    def test_added_order_keeps_oldest_new_cards_first(self):
        selected = select_daily_cards(
            [card(3), card(1), card(2)],
            set(),
            3,
            NOW,
            DeterministicRng(),
            config=SchedulerConfig(new_card_order="added"),
        )
        self.assertEqual([item.card_id for item in selected], [1, 2, 3])

    def test_retrievability_order_puts_least_recallable_review_first(self):
        recent = replace(
            card(1, LearningState.REVIEW, NOW),
            last_review_at=NOW - timedelta(days=2),
        )
        forgotten = replace(
            card(2, LearningState.REVIEW, NOW),
            last_review_at=NOW - timedelta(days=20),
        )
        selected = select_daily_cards(
            [recent, forgotten],
            set(),
            2,
            NOW,
            DeterministicRng(),
            config=SchedulerConfig(review_order="retrievability"),
        )
        self.assertEqual([item.card_id for item in selected], [2, 1])

    def test_suspended_card_never_enters_daily_selection(self):
        suspended = replace(card(1), is_suspended=True, priority_boost=True)
        selected = select_daily_cards(
            [suspended, card(2)], set(), 10, NOW, DeterministicRng()
        )
        self.assertEqual([item.card_id for item in selected], [2])


class RepositoryIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "study.db"
        self.repository = StudyRepository(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_two_directions_share_one_card_id_and_state(self):
        self.assertTrue(self.repository.add_card("苹果", "píng guǒ", "яблоко"))
        self.assertFalse(self.repository.add_card("苹果", "píng guǒ", "яблоко"))
        cards = self.repository.get_cards()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].id, 1)

    def test_incomplete_card_data_is_not_inserted(self):
        self.assertFalse(self.repository.add_card("", "pinyin", "перевод"))
        self.assertFalse(self.repository.add_card("词", "", "перевод"))
        self.assertFalse(self.repository.add_card("词", "cí", ""))
        self.assertEqual(self.repository.get_card_count(), 0)

    def test_card_lookup_priority_and_removal_use_insertion_normalization(self):
        raw_word = " \ufeff学\u200b校\u2060 "
        self.assertTrue(self.repository.add_card(raw_word, "xué xiào", "школа"))
        self.assertTrue(self.repository.has_card(raw_word))
        with closing(self.repository.connect()) as connection, connection:
            connection.execute("UPDATE cards SET learning_state='REVIEW'")
        self.assertTrue(self.repository.can_raise_card_priority(raw_word))
        self.assertTrue(self.repository.raise_card_priority(raw_word))
        self.assertTrue(self.repository.remove_card(raw_word))
        self.assertEqual(self.repository.get_card_count(), 0)

    def test_selected_and_all_cards_can_be_removed_in_one_transaction(self):
        for hanzi in ("一", "二", "三"):
            self.assertTrue(self.repository.add_card(hanzi, "pinyin", "перевод"))
        cards = self.repository.get_cards()

        self.assertEqual(
            self.repository.remove_cards((cards[0].id, cards[2].id, cards[0].id)),
            2,
        )
        self.assertEqual([item.hanzi for item in self.repository.get_cards()], ["二"])
        self.assertEqual(self.repository.remove_all_cards(), 1)
        self.assertEqual(self.repository.get_card_count(), 0)

    def test_bulk_add_is_atomic_and_does_not_overwrite_existing_card(self):
        self.repository.add_card("学校", "xué xiào", "наш перевод")
        added = self.repository.add_cards(
            [
                ("学校", "wrong", "не перезаписывать"),
                ("老师", "lǎo shī", "учитель"),
                ("老师", "duplicate", "дубликат"),
                ("", "empty", "пропустить"),
            ]
        )
        self.assertEqual(added, 1)
        cards = {card.hanzi: card for card in self.repository.get_cards()}
        self.assertEqual(cards["学校"].translation, "наш перевод")
        self.assertEqual(cards["老师"].pinyin, "lǎo shī")

    def test_corrupt_daily_limit_falls_back_to_default(self):
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "UPDATE settings SET value='не число' WHERE key='daily_review_limit'"
            )
        self.assertEqual(self.repository.get_daily_limit(), 30)

    def test_card_is_scheduled_only_after_both_sides_have_same_rating(self):
        self.repository.add_card("猫", "māo", "кошка")
        self.repository.set_daily_limit(30)
        service = StudySessionService(self.repository, DeterministicRng([0, 1]))

        first = service.next_item(NOW)
        service.answer(first, Rating.VERY_HARD, NOW, 1200)
        self.assertEqual(service.progress(NOW), (0, 30))
        self.assertEqual(self.repository.get_card(first.card.id).review_count, 0)

        second_time = NOW + timedelta(seconds=1)
        second = service.next_item(second_time)
        self.assertEqual(second.card.id, first.card.id)
        self.assertNotEqual(second.direction, first.direction)
        self.assertIsNotNone(second.learning_queue_id)
        service.answer(second, Rating.HARD, second_time, 900)
        self.assertEqual(service.progress(second_time), (0, 30))

        third_time = second_time + timedelta(seconds=1)
        third = service.next_item(third_time)
        self.assertEqual(third.card.id, first.card.id)
        self.assertEqual(third.direction, first.direction)
        service.answer(third, Rating.HARD, third_time, 800)
        self.assertEqual(service.progress(third_time), (1, 30))
        stored = self.repository.get_card(first.card.id)
        self.assertEqual(stored.last_rating, Rating.HARD.value)
        self.assertEqual(stored.next_review_at, third_time + timedelta(minutes=6))
        self.assertEqual(self.repository.review_event_count(), 3)

    def test_pending_learning_card_is_not_duplicated_by_daily_due_queue(self):
        self.repository.add_card("水", "shuǐ", "вода")
        service = StudySessionService(self.repository, DeterministicRng([0]))
        first = service.next_item(NOW)
        service.answer(first, Rating.VERY_HARD, NOW, 1000)

        delayed = NOW + timedelta(minutes=3)
        self.assertEqual(
            self.repository.get_daily_queue(local_date(delayed), delayed), []
        )
        repeated = service.next_item(delayed)
        self.assertEqual(repeated.card.id, first.card.id)
        self.assertIsNotNone(repeated.learning_queue_id)
        self.assertNotEqual(repeated.direction, first.direction)

    def test_unfinished_daily_row_never_bypasses_future_next_review(self):
        self.repository.add_card("山", "shān", "гора")
        service = StudySessionService(self.repository, DeterministicRng([0]))
        service.ensure_daily_session(NOW)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                """
                UPDATE cards SET learning_state='REVIEW',
                    next_review_at=?, review_count=1,
                    stability=3, current_interval_days=3
                WHERE id=1
                """,
                ((NOW + timedelta(days=3)).isoformat(),),
            )
        self.assertEqual(
            self.repository.get_daily_queue(local_date(NOW), NOW), []
        )

    def test_new_card_added_mid_session_displaces_only_unstarted_due_card(self):
        self.repository.add_card("旧一", "jiù yī", "старое один")
        self.repository.add_card("旧二", "jiù èr", "старое два")
        self.repository.set_daily_limit(2, NOW)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                """
                UPDATE cards SET learning_state='REVIEW', review_count=2,
                    stability=3, current_interval_days=3, next_review_at=?
                """,
                ((NOW - timedelta(days=1)).isoformat(),),
            )
        service = StudySessionService(self.repository, DeterministicRng())
        service.ensure_daily_session(NOW)
        self.assertEqual(len(self.repository.daily_rows(local_date(NOW))), 2)

        self.repository.add_card("新", "xīn", "новый")
        service.ensure_daily_session(NOW)
        selected = {
            int(row["card_id"])
            for row in self.repository.daily_rows(local_date(NOW))
        }
        new_id = next(card.id for card in self.repository.get_cards() if card.hanzi == "新")
        self.assertEqual(len(selected), 2)
        self.assertIn(new_id, selected)

    def test_manual_priority_brings_future_review_forward_until_pair_is_complete(self):
        self.repository.add_card("远", "yuǎn", "далёкий")
        future = NOW + timedelta(days=30)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                """
                UPDATE cards SET learning_state='REVIEW', review_count=5,
                    stability=30, current_interval_days=30,
                    last_review_at=?, next_review_at=? WHERE id=1
                """,
                ((NOW - timedelta(days=1)).isoformat(), future.isoformat()),
            )
        self.assertTrue(self.repository.can_raise_card_priority("远"))
        self.assertTrue(self.repository.raise_card_priority("远"))
        self.assertTrue(self.repository.get_card(1).priority_boost)

        service = StudySessionService(self.repository, DeterministicRng([0]))
        first = service.next_item(NOW)
        self.assertEqual(first.card.id, 1)
        service.answer(first, Rating.MEDIUM, NOW, 500)
        second = service.next_item(NOW + timedelta(seconds=1))
        self.assertNotEqual(second.direction, first.direction)
        service.answer(second, Rating.MEDIUM, NOW + timedelta(seconds=1), 500)

        stored = self.repository.get_card(1)
        self.assertFalse(stored.priority_boost)
        self.assertEqual(stored.learning_state, LearningState.REVIEW)
        self.assertTrue(self.repository.can_raise_card_priority("远"))

    def test_answering_same_presentation_twice_is_rejected_atomically(self):
        self.repository.add_card("火", "huǒ", "огонь")
        service = StudySessionService(self.repository, DeterministicRng([0]))
        item = service.next_item(NOW)
        service.answer(item, Rating.MEDIUM, NOW, 500)
        with self.assertRaises(ValueError):
            service.answer(item, Rating.EASY, NOW + timedelta(seconds=1), 600)
        stored = self.repository.get_card(item.card.id)
        self.assertEqual(stored.review_count, 0)
        self.assertIsNone(stored.last_rating)
        self.assertEqual(self.repository.review_event_count(), 1)

    def test_only_one_pending_repeat_per_logical_card_is_allowed(self):
        self.repository.add_card("木", "mù", "дерево")
        with closing(self.repository.connect()) as connection, connection:
            values = (1, ReviewDirection.CHINESE_TO_RUSSIAN.value, NOW.isoformat(), NOW.isoformat())
            connection.execute(
                """INSERT INTO learning_queue(
                       card_id, direction, available_at, created_at
                   ) VALUES (?, ?, ?, ?)""",
                values,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO learning_queue(
                           card_id, direction, available_at, created_at
                       ) VALUES (?, ?, ?, ?)""",
                    values,
                )

    def test_matching_very_hard_sides_wait_for_two_minute_interval(self):
        self.repository.add_card("中国", "zhōng guó", "Китай")
        service = StudySessionService(self.repository, DeterministicRng([0]))
        first = service.next_item(NOW)
        service.answer(first, Rating.VERY_HARD, NOW, 1000)
        second = service.next_item(NOW + timedelta(seconds=1))
        self.assertNotEqual(second.direction, first.direction)
        service.answer(second, Rating.VERY_HARD, NOW + timedelta(seconds=1), 900)
        self.assertIsNone(service.next_item(NOW + timedelta(seconds=2)))
        self.assertEqual(service.progress(NOW + timedelta(seconds=2)), (1, 30))
        third = service.next_item(NOW + timedelta(minutes=2, seconds=1))
        self.assertEqual(third.card.id, first.card.id)

    def test_after_very_hard_next_card_can_be_random_other_daily_card(self):
        self.repository.add_card("猫", "māo", "кошка")
        self.repository.add_card("狗", "gǒu", "собака")
        service = StudySessionService(
            self.repository, DeterministicRng([0, 0, 1])
        )
        failed = service.next_item(NOW)
        service.answer(failed, Rating.VERY_HARD, NOW, 1000)
        next_item = service.next_item(NOW + timedelta(seconds=1))
        self.assertNotEqual(next_item.card.id, failed.card.id)
        service.answer(next_item, Rating.MEDIUM, NOW + timedelta(seconds=1), 800)
        queued_learning = service.next_item(NOW + timedelta(seconds=2))
        self.assertEqual(queued_learning.card.id, failed.card.id)
        self.assertNotEqual(queued_learning.direction, failed.direction)

    def test_direction_stays_stable_during_one_presentation(self):
        self.repository.add_card("学校", "xué xiào", "школа")
        service = StudySessionService(self.repository, DeterministicRng([0, 1]))
        first = service.next_item(NOW)
        rerendered = service.next_item(NOW + timedelta(seconds=5))
        self.assertEqual(first.presentation_id, rerendered.presentation_id)
        self.assertEqual(first.direction, rerendered.direction)
        self.assertEqual(first.shown_at, rerendered.shown_at)

    def test_restored_presentation_keeps_direction_but_restarts_response_timer(self):
        self.repository.add_card("学校", "xué xiào", "школа")
        first_service = StudySessionService(self.repository, DeterministicRng([0]))
        first = first_service.next_item(NOW)

        resumed_at = NOW + timedelta(hours=4)
        resumed_service = StudySessionService(self.repository, DeterministicRng([1]))
        resumed = resumed_service.next_item(resumed_at)
        self.assertEqual(resumed.presentation_id, first.presentation_id)
        self.assertEqual(resumed.direction, first.direction)
        self.assertEqual(resumed.shown_at, resumed_at)

        rerendered = resumed_service.next_item(resumed_at + timedelta(minutes=1))
        self.assertEqual(rerendered.shown_at, resumed_at)

    def test_active_card_stays_visible_when_another_pair_is_pending(self):
        self.repository.add_card("猫", "māo", "кошка")
        self.repository.add_card("狗", "gǒu", "собака")
        service = StudySessionService(self.repository, DeterministicRng([0, 0, 1]))
        first = service.next_item(NOW)
        service.answer(first, Rating.MEDIUM, NOW, 100)
        active = service.next_item(NOW + timedelta(seconds=1))
        self.assertNotEqual(active.card.id, first.card.id)

        # The next random choice would have selected the pending opposite side.
        rerendered = service.next_item(NOW + timedelta(seconds=2))
        self.assertEqual(rerendered.presentation_id, active.presentation_id)
        restarted = StudySessionService(self.repository, DeterministicRng([0]))
        resumed = restarted.next_item(NOW + timedelta(minutes=1))
        self.assertEqual(resumed.presentation_id, active.presentation_id)

    def test_pending_pair_keeps_its_daily_slot_when_limit_is_reduced(self):
        for index in range(3):
            self.repository.add_card(f"词{index}", "cí", "слово")
        self.repository.set_daily_limit(3, NOW)
        service = StudySessionService(self.repository, DeterministicRng([0]))
        service.ensure_daily_session(NOW)
        rows = self.repository.daily_rows(local_date(NOW))
        pending_id = int(rows[-1]["card_id"])
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "UPDATE daily_cards SET position=0 WHERE card_id=?", (pending_id,)
            )
        first = service.next_item(NOW)
        # Force the pair to the end so trimming cannot retain it accidentally.
        service.answer(first, Rating.MEDIUM, NOW, 100)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "UPDATE daily_cards SET position=99 WHERE card_id=?", (first.card.id,)
            )
        self.repository.set_daily_limit(1, NOW)
        retained = self.repository.daily_rows(local_date(NOW))
        self.assertEqual([int(row["card_id"]) for row in retained], [first.card.id])
        second = service.next_item(NOW + timedelta(seconds=1))
        service.answer(second, Rating.MEDIUM, NOW + timedelta(seconds=1), 100)
        self.assertEqual(service.progress(NOW), (1, 1))

    def test_pending_pair_reserves_next_days_slot_before_new_cards(self):
        self.repository.add_card("旧", "jiù", "старый")
        self.repository.set_daily_limit(1, NOW)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "UPDATE cards SET learning_state='REVIEW', next_review_at=?",
                (NOW.isoformat(),),
            )
        service = StudySessionService(self.repository, DeterministicRng([0]))
        first = service.next_item(NOW)
        service.answer(first, Rating.MEDIUM, NOW, 100)
        self.repository.add_card("新", "xīn", "новый")
        tomorrow = NOW + timedelta(days=1)
        restarted = StudySessionService(self.repository, DeterministicRng([0]))
        second = restarted.next_item(tomorrow)
        self.assertEqual(second.card.id, first.card.id)
        self.assertNotEqual(second.direction, first.direction)
        self.assertEqual(
            [int(row["card_id"]) for row in self.repository.daily_rows(local_date(tomorrow))],
            [first.card.id],
        )
        restarted.answer(second, Rating.MEDIUM, tomorrow, 100)
        self.assertEqual(restarted.progress(tomorrow), (1, 1))
        self.assertIsNone(restarted.next_item(tomorrow))

    def test_answer_after_midnight_counts_the_visible_card_in_new_day(self):
        self.repository.add_card("旧", "jiù", "старый")
        self.repository.set_daily_limit(1, NOW)
        service = StudySessionService(self.repository, DeterministicRng([0]))
        first = service.next_item(NOW)
        service.answer(first, Rating.MEDIUM, NOW, 100)
        second = service.next_item(NOW + timedelta(seconds=1))
        tomorrow = NOW + timedelta(days=1)
        service.answer(second, Rating.MEDIUM, tomorrow, 100)
        self.assertEqual(service.progress(tomorrow), (1, 1))

    def test_lower_daily_limit_trims_only_unfinished_unopened_rows(self):
        for index in range(1, 31):
            self.repository.add_card(f"词{index}", f"cí {index}", f"слово {index}")
        self.repository.set_daily_limit(30, NOW)
        service = StudySessionService(self.repository, DeterministicRng([0]))
        active = service.next_item(NOW)

        self.repository.set_daily_limit(10, NOW)
        rows = self.repository.daily_rows(local_date(NOW))
        self.assertEqual(len(rows), 10)
        self.assertIn(active.card.id, {int(row["card_id"]) for row in rows})
        self.assertEqual(service.progress(NOW), (0, 10))

    def test_lower_limit_never_produces_completed_over_total_progress(self):
        for index in range(1, 21):
            self.repository.add_card(f"字{index}", f"zì {index}", f"знак {index}")
        self.repository.set_daily_limit(20, NOW)
        service = StudySessionService(self.repository, DeterministicRng([0]))
        service.ensure_daily_session(NOW)
        rows = self.repository.daily_rows(local_date(NOW))
        completed_ids = [int(row["card_id"]) for row in rows[:12]]
        with closing(self.repository.connect()) as connection, connection:
            connection.executemany(
                """UPDATE daily_cards SET completed=1, completed_at=?
                   WHERE local_date=? AND card_id=?""",
                [(NOW.isoformat(), local_date(NOW), card_id) for card_id in completed_ids],
            )

        self.repository.set_daily_limit(10, NOW)
        self.assertEqual(len(self.repository.daily_rows(local_date(NOW))), 12)
        self.assertEqual(service.progress(NOW), (12, 12))
        self.assertEqual(self.repository.get_daily_queue(local_date(NOW), NOW), [])

    def test_continue_adds_another_batch_without_changing_daily_preference(self):
        for index in range(1, 7):
            self.repository.add_card(
                f"词{index}", f"cí {index}", f"слово {index}"
            )
        self.repository.set_daily_limit(2, NOW)
        service = StudySessionService(self.repository, DeterministicRng([0]))
        service.ensure_daily_session(NOW)
        date_key = local_date(NOW)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "UPDATE daily_cards SET completed=1, completed_at=? WHERE local_date=?",
                (NOW.isoformat(), date_key),
            )

        extension_time = NOW + timedelta(seconds=1)
        self.assertTrue(service.can_continue(extension_time))
        self.assertEqual(service.continue_daily_session(extension_time), 2)
        self.assertEqual(self.repository.get_daily_limit(), 2)
        self.assertEqual(service.progress(extension_time), (0, 2))
        self.assertEqual(len(self.repository.daily_rows(date_key)), 4)

        # Existing extended sessions created before the separate batch counter
        # also infer the beginning of their latest batch correctly.
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "DELETE FROM settings WHERE key GLOB 'daily_batch_start:*'"
            )
        self.assertEqual(service.progress(extension_time), (0, 2))

        # Today's extension survives a new service/application instance.
        resumed = StudySessionService(self.repository, DeterministicRng([0]))
        resumed.ensure_daily_session(extension_time)
        self.assertEqual(resumed.progress(extension_time), (0, 2))
        self.assertEqual(len(self.repository.daily_rows(date_key)), 4)

        with closing(self.repository.connect()) as connection, connection:
            next_card_id = connection.execute(
                """
                SELECT card_id FROM daily_cards
                WHERE local_date=? AND completed=0 ORDER BY position LIMIT 1
                """,
                (date_key,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE daily_cards SET completed=1, completed_at=?
                WHERE local_date=? AND card_id=?
                """,
                (NOW.isoformat(), date_key, next_card_id),
            )
        self.assertEqual(resumed.progress(extension_time), (1, 2))

        # Restarting after part of the extra batch must not turn the remaining
        # one card into a new 0/1 batch (the real-world 0/28 regression).
        restarted = StudySessionService(self.repository, DeterministicRng([0]))
        restarted.ensure_daily_session(extension_time + timedelta(seconds=1))
        self.assertEqual(
            restarted.progress(extension_time + timedelta(seconds=1)),
            (1, 2),
        )

    def test_continue_never_brings_future_review_forward(self):
        self.repository.add_card("今", "jīn", "сейчас")
        self.repository.add_card("后", "hòu", "потом")
        self.repository.set_daily_limit(1, NOW)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                """
                UPDATE cards SET learning_state='REVIEW', review_count=2,
                    stability=10, current_interval_days=10, next_review_at=?
                WHERE hanzi='后'
                """,
                ((NOW + timedelta(days=10)).isoformat(),),
            )
        service = StudySessionService(self.repository, DeterministicRng([0]))
        service.ensure_daily_session(NOW)
        with closing(self.repository.connect()) as connection, connection:
            connection.execute(
                "UPDATE daily_cards SET completed=1, completed_at=?",
                (NOW.isoformat(),),
            )

        self.assertFalse(service.can_continue(NOW))
        self.assertEqual(service.continue_daily_session(NOW), 0)
        self.assertEqual(service.progress(NOW), (1, 1))

    def test_preview_does_not_write_review_history(self):
        self.repository.add_card("工作", "gōng zuò", "работа")
        stored = self.repository.get_card(1)
        preview_ratings(stored, NOW)
        self.assertEqual(self.repository.review_event_count(), 0)

    def test_migration_preserves_mature_interval(self):
        legacy = Path(self.temp.name) / "legacy.db"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE cards(
                id INTEGER PRIMARY KEY, hanzi TEXT UNIQUE, pinyin TEXT,
                translation TEXT, created_at TEXT,
                current_interval_days REAL DEFAULT 0,
                review_count INTEGER DEFAULT 0
            );
            INSERT INTO cards VALUES(1, '老师', 'lǎo shī', 'учитель',
                                     CURRENT_TIMESTAMP, 100, 20);
            """
        )
        connection.close()
        initialize_study_database(legacy)
        migrated = StudyRepository(legacy).get_card(1)
        self.assertEqual(migrated.learning_state, LearningState.REVIEW)
        self.assertEqual(migrated.stability, 100)
        self.assertEqual(migrated.review_count, 20)

    def test_database_from_newer_app_version_is_not_downgraded(self):
        future = Path(self.temp.name) / "future.db"
        connection = sqlite3.connect(future)
        connection.execute("PRAGMA user_version=999")
        connection.close()
        with self.assertRaises(RuntimeError):
            initialize_study_database(future)
        with closing(sqlite3.connect(future)) as check:
            self.assertEqual(check.execute("PRAGMA user_version").fetchone()[0], 999)

    def test_v3_migration_deterministically_closes_duplicate_pending_rows(self):
        legacy = Path(self.temp.name) / "duplicates-v3.db"
        initialize_study_database(legacy)
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.executescript(
                """
                DROP INDEX idx_presentations_one_active;
                DROP INDEX idx_learning_queue_one_pending;
                PRAGMA user_version=3;
                INSERT INTO cards(hanzi, pinyin, translation)
                VALUES ('人', 'rén', 'человек');
                INSERT INTO presentations(card_id, direction, shown_at)
                VALUES (1, 'CHINESE_TO_RUSSIAN', '2026-08-10T09:00:00+00:00');
                INSERT INTO presentations(card_id, direction, shown_at)
                VALUES (1, 'RUSSIAN_TO_CHINESE', '2026-08-10T09:01:00+00:00');
                INSERT INTO learning_queue(card_id, direction, available_at, created_at)
                VALUES (1, 'CHINESE_TO_RUSSIAN',
                        '2026-08-10T09:00:00+00:00', '2026-08-10T09:00:00+00:00');
                INSERT INTO learning_queue(card_id, direction, available_at, created_at)
                VALUES (1, 'RUSSIAN_TO_CHINESE',
                        '2026-08-10T09:01:00+00:00', '2026-08-10T09:01:00+00:00');
                """
            )

        initialize_study_database(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            active = connection.execute(
                "SELECT id FROM presentations WHERE completed_at IS NULL"
            ).fetchall()
            pending = connection.execute(
                "SELECT id FROM learning_queue WHERE completed_at IS NULL"
            ).fetchall()
            self.assertEqual(active, [(2,)])
            self.assertEqual(pending, [(2,)])
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
