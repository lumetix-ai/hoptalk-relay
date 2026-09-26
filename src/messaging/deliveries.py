"""The state machine of one message's delivery to one device.

pending: sent in rounds until the device reports every part. queued_for_refresh: owned by a
refresh session that has not reached it yet. delivered: terminal. failed: its attempts ran out,
or its refresh stopped; a late report or a refresh still revives it. cancelled: the device was
relinked to another user; a refresh after relinking back revives it.

Every transition names the states it may start from and does nothing from any other. It takes
a row the caller locked FOR UPDATE and changes it with a compare-and-set on the state and arm
generation the caller read; counters and masks change through SQL expressions. The arm
generation grows whenever the row is armed again, and a packet copies it, so the outcome of a
packet from an earlier arm can no longer change the row. The session logic around a refresh
head (advancing, stopping) lives in messaging.refresh_sessions.
"""

from collections.abc import Collection
from datetime import datetime
from typing import Any

from django.db.models import F

from messaging.models import PARTS_MASK_MAXIMUM, MessageDelivery, OutboundPacket
from messaging.retry_schedule import calculate_missing_parts_round_time, calculate_round_completion_due_time
from messaging.service_transactions import read_retry_strategy
from protocol.received_sets import calculate_all_parts_mask

PENDING = MessageDelivery.State.PENDING
QUEUED_FOR_REFRESH = MessageDelivery.State.QUEUED_FOR_REFRESH
DELIVERED = MessageDelivery.State.DELIVERED
FAILED = MessageDelivery.State.FAILED
CANCELLED = MessageDelivery.State.CANCELLED
# The states a relink cancels.
NON_TERMINAL_DELIVERY_STATES = (PENDING, QUEUED_FOR_REFRESH, FAILED)
# The states a complete status or a read delivers from; delivered always wins over failed.
UNCONFIRMED_DELIVERY_STATES = (PENDING, QUEUED_FOR_REFRESH, FAILED)


def create_pending_delivery(message_id: int, device_id: int, now: datetime) -> MessageDelivery:
    """A delivery that is due at once, for a device of the recipient without a refresh session with the sender."""
    return MessageDelivery.objects.create(
        message_id=message_id,
        device_id=device_id,
        state=PENDING,
        maximum_attempts=read_retry_strategy().maximum_attempts,
        next_attempt_at=now,
        created_at=now,
    )


def create_refresh_owned_delivery(message_id: int, device_id: int, refresh_session_id: int, now: datetime) -> None:
    """A delivery queued behind the session's head: for a message accepted during the session, or a missed one."""
    MessageDelivery.objects.create(
        message_id=message_id,
        device_id=device_id,
        state=QUEUED_FOR_REFRESH,
        refresh_session_id=refresh_session_id,
        maximum_attempts=read_retry_strategy().maximum_attempts,
        next_attempt_at=None,
        created_at=now,
    )


def update_locked_delivery(
    delivery: MessageDelivery,
    from_states: Collection[MessageDelivery.State],
    **changes: Any,
) -> bool:
    """Apply the changes if the row is in one of from_states and unchanged since the caller read it.

    The instance is refreshed with the stored values of the changed fields.
    """
    if delivery.state not in from_states:
        return False
    updated_row_count = MessageDelivery.objects.filter(
        id=delivery.pk,
        state=delivery.state,
        arm_generation=delivery.arm_generation,
    ).update(**changes)
    if updated_row_count == 0:
        return False
    delivery.refresh_from_db(fields=list(changes))
    return True


def start_delivery_round(delivery: MessageDelivery, part_count: int, now: datetime) -> bool:
    """A round sends every part the device has not reported, one packet per part; starting it counts an attempt."""
    parts_still_missing_mask = calculate_all_parts_mask(part_count) & ~delivery.parts_received_mask
    return update_locked_delivery(
        delivery,
        [PENDING],
        attempt_count=F("attempt_count") + 1,
        round_started_at=now,
        round_pending_parts_mask=parts_still_missing_mask,
    )


def record_delivery_part_sent(
    delivery: MessageDelivery,
    part_number: int,
    suggested_timeout_milliseconds: int | None,
    now: datetime,
) -> bool:
    """The node took the packet of part n (or its outcome is unknown); the round completes with its last part."""
    part_bit = 1 << (part_number - 1)
    round_had_parts_to_send = delivery.round_pending_parts_mask != 0
    was_updated = update_locked_delivery(
        delivery,
        [PENDING],
        round_pending_parts_mask=F("round_pending_parts_mask").bitand(PARTS_MASK_MAXIMUM ^ part_bit),
        last_sent_at=now,
    )
    if was_updated and round_had_parts_to_send and delivery.round_pending_parts_mask == 0:
        complete_delivery_round(delivery, suggested_timeout_milliseconds, now)
    return was_updated


def complete_delivery_round(
    delivery: MessageDelivery,
    last_packet_suggested_timeout_milliseconds: int | None,
    now: datetime,
) -> bool:
    due_time = calculate_round_completion_due_time(
        attempt_count=delivery.attempt_count,
        round_started_at=delivery.round_started_at,
        last_sent_at=delivery.last_sent_at,
        last_packet_suggested_timeout_milliseconds=last_packet_suggested_timeout_milliseconds,
        retry_strategy=read_retry_strategy(),
        now=now,
    )
    return update_locked_delivery(delivery, [PENDING], round_pending_parts_mask=0, next_attempt_at=due_time)


def find_last_round_packet_suggested_timeout(delivery: MessageDelivery) -> int | None:
    return (
        OutboundPacket.objects.filter(
            message_delivery_id=delivery.pk,
            arm_generation=delivery.arm_generation,
            attempt_number=delivery.attempt_count,
        )
        .exclude(state=OutboundPacket.State.PREPARED)
        .order_by("-id")
        .values_list("suggested_timeout_milliseconds", flat=True)
        .first()
    )


def record_delivery_acknowledgement_time(delivery: MessageDelivery, now: datetime) -> None:
    """Every status counts as evidence that the relay's DMs reach the device, whatever the row's state."""
    MessageDelivery.objects.filter(id=delivery.pk).update(last_acknowledgement_received_at=now)
    delivery.last_acknowledgement_received_at = now


def record_incomplete_delivery_status(delivery: MessageDelivery, received_parts_mask: int, now: datetime) -> bool:
    """A status with zeros: its set replaces the device's recorded set, never combined with earlier ones.

    On a pending row it also drops the reported parts from the round in progress; once nothing of
    the round is left, the parts the device still lacks are sent within a few seconds. A
    queued_for_refresh or failed row only records the set, so its next round sends what the
    device lacks by then.
    """
    if delivery.state != PENDING:
        return update_locked_delivery(
            delivery,
            [QUEUED_FOR_REFRESH, FAILED],
            parts_received_mask=received_parts_mask,
        )

    round_had_parts_to_send = delivery.round_pending_parts_mask != 0
    was_updated = update_locked_delivery(
        delivery,
        [PENDING],
        parts_received_mask=received_parts_mask,
        round_pending_parts_mask=F("round_pending_parts_mask").bitand(PARTS_MASK_MAXIMUM ^ received_parts_mask),
    )
    if not was_updated:
        return False

    if round_had_parts_to_send and delivery.round_pending_parts_mask == 0:
        complete_delivery_round(delivery, find_last_round_packet_suggested_timeout(delivery), now)
    if delivery.round_pending_parts_mask == 0 and delivery.attempt_count < delivery.maximum_attempts:
        bring_next_round_forward(delivery, now)
    return True


def bring_next_round_forward(delivery: MessageDelivery, now: datetime) -> None:
    missing_parts_round_time = calculate_missing_parts_round_time(now)
    if delivery.next_attempt_at is None or delivery.next_attempt_at > missing_parts_round_time:
        update_locked_delivery(delivery, [PENDING], next_attempt_at=missing_parts_round_time)


def mark_delivery_delivered(
    delivery: MessageDelivery,
    now: datetime,
    *,
    received_parts_mask: int | None = None,
    is_read: bool = False,
) -> bool:
    """A status that is all ones by itself, or a read from the device: delivered."""
    changes: dict[str, Any] = {"state": DELIVERED, "delivered_at": now}
    if received_parts_mask is not None:
        changes["parts_received_mask"] = received_parts_mask
    if is_read:
        changes["read_at"] = now
    return update_locked_delivery(delivery, UNCONFIRMED_DELIVERY_STATES, **changes)


def record_delivery_read(delivery: MessageDelivery, now: datetime) -> bool:
    """A read of a message this device already confirmed: only the first read time is kept."""
    if delivery.read_at is not None:
        return False
    return update_locked_delivery(delivery, [DELIVERED], read_at=now)


def fail_delivery_after_exhausted_attempts(delivery: MessageDelivery, now: datetime) -> bool:
    return update_locked_delivery(
        delivery,
        [PENDING],
        state=FAILED,
        failed_at=now,
        failure_reason=MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED,
    )


def take_delivery_into_refresh_session(delivery: MessageDelivery, refresh_session_id: int, now: datetime) -> bool:
    """A refresh takes over a pending delivery no session owns, a failed one or a relink-cancelled one.

    Its counters start again when it becomes the head, and it gives up its place under the
    per-device limit of deliveries in progress until then. A relink-cancelled delivery starts
    again with no parts reported, because the client clears its conversations after an account
    switch.
    """
    if delivery.state == PENDING and delivery.refresh_session_id is not None:
        return False
    changes: dict[str, Any] = {
        "state": QUEUED_FOR_REFRESH,
        "refresh_session_id": refresh_session_id,
        "attempt_count": 0,
        "arm_generation": F("arm_generation") + 1,
        "round_pending_parts_mask": 0,
        "round_started_at": None,
        "next_attempt_at": None,
    }
    if delivery.state == FAILED:
        changes["failed_at"] = None
        changes["failure_reason"] = ""
    if delivery.state == CANCELLED:
        changes["cancelled_at"] = None
        changes["parts_received_mask"] = 0
    return update_locked_delivery(delivery, [PENDING, FAILED, CANCELLED], **changes)


def arm_refresh_session_head(delivery: MessageDelivery, now: datetime) -> bool:
    """The session's next message becomes its head: pending, fresh counters, due at once."""
    return update_locked_delivery(
        delivery,
        [QUEUED_FOR_REFRESH],
        state=PENDING,
        attempt_count=0,
        arm_generation=F("arm_generation") + 1,
        round_pending_parts_mask=0,
        maximum_attempts=read_retry_strategy().maximum_attempts,
        next_attempt_at=now,
    )


def restart_refresh_session_head(delivery: MessageDelivery, now: datetime) -> bool:
    """A repeated refresh request restarts the head with fresh counters, at once.

    round_started_at is kept, so the head keeps its place under the per-device limit and goes at
    once. A round in progress is abandoned: its packets' outcomes no longer match the arm
    generation, and the new round sends every part the device has not reported. A head that has
    not started a round yet is already fresh and only becomes due now.
    """
    if delivery.refresh_session_id is None:
        return False
    if delivery.attempt_count == 0:
        if delivery.next_attempt_at is not None and delivery.next_attempt_at <= now:
            return False
        return update_locked_delivery(delivery, [PENDING], next_attempt_at=now)
    return update_locked_delivery(
        delivery,
        [PENDING],
        attempt_count=0,
        arm_generation=F("arm_generation") + 1,
        round_pending_parts_mask=0,
        maximum_attempts=read_retry_strategy().maximum_attempts,
        next_attempt_at=now,
    )


def fail_delivery_of_stopped_refresh(delivery: MessageDelivery, now: datetime) -> bool:
    """The session's head was given up: the messages queued behind it are never sent (the device gets no gap)."""
    return update_locked_delivery(
        delivery,
        [QUEUED_FOR_REFRESH],
        state=FAILED,
        failed_at=now,
        failure_reason=MessageDelivery.FailureReason.REFRESH_STOPPED,
    )


def cancel_device_deliveries_for_relink(device_id: int, now: datetime) -> int:
    """The device now belongs to another user: nothing of the previous user's is sent to it again.

    The rows keep their refresh session for the record.
    """
    locked_delivery_ids = list(
        MessageDelivery.objects.select_for_update()
        .filter(device_id=device_id, state__in=NON_TERMINAL_DELIVERY_STATES)
        .order_by("id")
        .values_list("id", flat=True)
    )
    return MessageDelivery.objects.filter(id__in=locked_delivery_ids, state__in=NON_TERMINAL_DELIVERY_STATES).update(
        state=CANCELLED,
        cancelled_at=now,
    )
