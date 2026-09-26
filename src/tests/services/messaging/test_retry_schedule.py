from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from messaging.retry_schedule import (
    calculate_acknowledgement_wait,
    calculate_retry_pause,
    calculate_round_completion_due_time,
)
from tests.services.messaging.engine_builders import DEFAULT_RETRY_STRATEGY

ROUND_STARTED_AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def test_the_default_pauses_double_from_thirty_seconds_up_to_ten_minutes() -> None:
    pauses = [
        calculate_retry_pause(attempt_number, DEFAULT_RETRY_STRATEGY).total_seconds() for attempt_number in range(1, 8)
    ]

    assert pauses == [30, 60, 120, 240, 480, 600, 600]


def test_a_multiplier_of_one_gives_a_fixed_pause() -> None:
    fixed_strategy = replace(DEFAULT_RETRY_STRATEGY, backoff_multiplier=1.0)

    assert {calculate_retry_pause(attempt_number, fixed_strategy) for attempt_number in range(1, 7)} == {
        timedelta(seconds=30)
    }


def test_the_maximum_pause_caps_a_steep_backoff() -> None:
    steep_strategy = replace(
        DEFAULT_RETRY_STRATEGY, initial_pause_seconds=100, backoff_multiplier=4.0, maximum_pause_seconds=1000
    )

    pauses = [calculate_retry_pause(attempt_number, steep_strategy).total_seconds() for attempt_number in range(1, 5)]

    assert pauses == [100, 400, 1000, 1000]


@pytest.mark.parametrize(
    ("suggested_timeout_milliseconds", "expected_wait_seconds"),
    [(None, 10), (1000, 3), (2500, 3), (5000, 6), (40_000, 48), (100_000, 60)],
)
def test_the_acknowledgement_wait_is_the_suggested_timeout_with_a_margin_within_three_and_sixty_seconds(
    suggested_timeout_milliseconds: int | None, expected_wait_seconds: float
) -> None:
    assert calculate_acknowledgement_wait(suggested_timeout_milliseconds) == timedelta(seconds=expected_wait_seconds)


def test_a_round_that_sent_something_is_due_again_a_pause_after_its_last_send() -> None:
    last_sent_at = ROUND_STARTED_AT + timedelta(seconds=4)

    due_time = calculate_round_completion_due_time(
        attempt_count=2,
        round_started_at=ROUND_STARTED_AT,
        last_sent_at=last_sent_at,
        last_packet_suggested_timeout_milliseconds=4000,
        retry_strategy=DEFAULT_RETRY_STRATEGY,
        now=last_sent_at + timedelta(seconds=20),
    )

    assert due_time == last_sent_at + timedelta(seconds=60)


def test_a_round_that_sent_nothing_is_due_again_a_pause_after_now() -> None:
    now = ROUND_STARTED_AT + timedelta(seconds=3)

    due_time = calculate_round_completion_due_time(
        attempt_count=1,
        round_started_at=ROUND_STARTED_AT,
        last_sent_at=ROUND_STARTED_AT - timedelta(minutes=5),
        last_packet_suggested_timeout_milliseconds=None,
        retry_strategy=DEFAULT_RETRY_STRATEGY,
        now=now,
    )

    assert due_time == now + timedelta(seconds=30)


def test_the_next_round_never_comes_before_the_last_packets_acknowledgement_could_arrive() -> None:
    short_strategy = replace(DEFAULT_RETRY_STRATEGY, initial_pause_seconds=5)

    due_time = calculate_round_completion_due_time(
        attempt_count=1,
        round_started_at=ROUND_STARTED_AT,
        last_sent_at=ROUND_STARTED_AT,
        last_packet_suggested_timeout_milliseconds=20_000,
        retry_strategy=short_strategy,
        now=ROUND_STARTED_AT,
    )

    assert due_time == ROUND_STARTED_AT + timedelta(seconds=24)
