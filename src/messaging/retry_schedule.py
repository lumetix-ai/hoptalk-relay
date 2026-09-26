"""When a delivery or receipt tries again: the operator's retry strategy and the firmware acknowledgement wait.

The same strategy serves deliveries, receipts and refresh heads. A round is due again after the
pause of its attempt, but never before the firmware acknowledgement of its last packet could
have arrived, so the route-reset decision for that packet is taken before the next round.
"""

from datetime import datetime, timedelta

from hoptalk_relay.relay_settings import RetryStrategy, get_relay_settings

# The node's suggested timeout is advisory: its duty-cycle bucket can hold a packet back.
SUGGESTED_TIMEOUT_SAFETY_FACTOR = 1.2


def calculate_retry_pause(attempt_number: int, retry_strategy: RetryStrategy) -> timedelta:
    """The pause after attempt n: initial pause x multiplier ^ (n - 1), at most the maximum pause."""
    exponent = max(attempt_number, 1) - 1
    pause_seconds = retry_strategy.initial_pause_seconds * retry_strategy.backoff_multiplier**exponent
    return timedelta(seconds=min(pause_seconds, retry_strategy.maximum_pause_seconds))


def calculate_acknowledgement_wait(suggested_timeout_milliseconds: int | None) -> timedelta:
    """How long a packet waits for its firmware acknowledgement after MSG_SENT: 1.2 x suggested, within 3 to 60 s."""
    engine_timing = get_relay_settings().engine_timing
    if suggested_timeout_milliseconds is None:
        return timedelta(seconds=engine_timing.unknown_acknowledgement_wait_seconds)
    wait_seconds = suggested_timeout_milliseconds / 1000 * SUGGESTED_TIMEOUT_SAFETY_FACTOR
    clamped_wait_seconds = min(
        max(wait_seconds, engine_timing.minimum_acknowledgement_wait_seconds),
        engine_timing.maximum_acknowledgement_wait_seconds,
    )
    return timedelta(seconds=clamped_wait_seconds)


def calculate_round_completion_due_time(
    *,
    attempt_count: int,
    round_started_at: datetime | None,
    last_sent_at: datetime | None,
    last_packet_suggested_timeout_milliseconds: int | None,
    retry_strategy: RetryStrategy,
    now: datetime,
) -> datetime:
    """The next_attempt_at of a round that has nothing left to send.

    The wait counts from the last send of this round; a round that sent nothing (every part was
    reported before its turn) counts from now.
    """
    was_sent_in_this_round = (
        last_sent_at is not None and round_started_at is not None and last_sent_at >= round_started_at
    )
    base_time = last_sent_at if was_sent_in_this_round and last_sent_at is not None else now
    return base_time + max(
        calculate_retry_pause(attempt_count, retry_strategy),
        calculate_acknowledgement_wait(last_packet_suggested_timeout_milliseconds),
    )


def calculate_missing_parts_round_time(now: datetime) -> datetime:
    return now + timedelta(seconds=get_relay_settings().engine_timing.missing_parts_round_delay_seconds)
