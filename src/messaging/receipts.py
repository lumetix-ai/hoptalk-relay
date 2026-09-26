"""The state machine of one message's delivered and read receipts to one device of its sender.

pending: an "s" with the current target level is sent in attempts until the device confirms it
with a "C". confirmed: the device confirmed the target level; a later read raises the target and
makes it pending again, and a receipt confirmed at "read" is terminal. failed: its attempts ran
out; a read or a refresh from the device that sent the message re-arms it. cancelled: the
device was relinked to another user.

target_level and confirmed_level only grow, and confirmed_level never passes target_level. A
receipt that is not cancelled is confirmed exactly when both levels are equal (a check
constraint), so leaving cancelled chooses the state by that rule.

Every function takes rows the caller locked FOR UPDATE, or locks them itself in ascending id,
and changes them with a compare-and-set on state and arm generation.
"""

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from django.db.models import F
from django.db.models.functions import Greatest

from directory.models import Contact
from messaging.models import Message, ReceiptNotification
from messaging.retry_schedule import calculate_round_completion_due_time
from messaging.service_transactions import read_retry_strategy

PENDING = ReceiptNotification.State.PENDING
CONFIRMED = ReceiptNotification.State.CONFIRMED
FAILED = ReceiptNotification.State.FAILED
CANCELLED = ReceiptNotification.State.CANCELLED
DELIVERED_LEVEL = ReceiptNotification.TargetLevel.DELIVERED
READ_LEVEL = ReceiptNotification.TargetLevel.READ
RECEIPT_STATES_A_REFRESH_REARMS = (PENDING, FAILED, CANCELLED)


def read_message_level(message: Message) -> int:
    """2 once the message was read, 1 once it was delivered, 0 before."""
    if message.read_at is not None:
        return READ_LEVEL
    if message.delivered_at is not None:
        return DELIVERED_LEVEL
    return 0


def update_locked_receipt(
    receipt: ReceiptNotification,
    from_states: Collection[ReceiptNotification.State],
    now: datetime,
    **changes: Any,
) -> bool:
    """Apply the changes if the row is in one of from_states and unchanged since the caller read it.

    The instance is refreshed with the stored values of the changed fields.
    """
    if receipt.state not in from_states:
        return False
    changes["updated_at"] = now
    updated_row_count = ReceiptNotification.objects.filter(
        id=receipt.pk,
        state=receipt.state,
        arm_generation=receipt.arm_generation,
    ).update(**changes)
    if updated_row_count == 0:
        return False
    receipt.refresh_from_db(fields=list(changes))
    return True


def build_fresh_attempt_changes(now: datetime) -> dict[str, Any]:
    """Pending again with fresh counters, due at once; a packet of the earlier arm can no longer change the row."""
    return {
        "state": PENDING,
        "attempt_count": 0,
        "arm_generation": F("arm_generation") + 1,
        "round_pending": False,
        "maximum_attempts": read_retry_strategy().maximum_attempts,
        "next_attempt_at": now,
    }


def raise_receipts_for_message(message: Message, now: datetime) -> None:
    """The message was just delivered or read: tell every current device of its sender.

    A device without a receipt row gets one at the message's level; a delivered receipt waits
    for the hold-back, so a quick read sends only the read receipt. An existing row is raised to
    "read", and a row cancelled by an earlier relink of a device that belongs to the sender again
    is revived first.
    """
    message_level = read_message_level(message)
    sender_device_ids = list(
        Contact.objects.filter(user_id=message.sender_id).order_by("id").values_list("id", flat=True)
    )
    existing_receipts_by_device_id = {
        receipt.device_id: receipt
        for receipt in ReceiptNotification.objects.select_for_update()
        .filter(message_id=message.pk, device_id__in=sender_device_ids)
        .order_by("id")
    }

    for device_id in sender_device_ids:
        existing_receipt = existing_receipts_by_device_id.get(device_id)
        if existing_receipt is None:
            create_receipt(message.pk, device_id, message_level, now)
        elif message_level == READ_LEVEL:
            raise_receipt_to_read(existing_receipt, now)


def create_receipt(message_id: int, device_id: int, target_level: int, now: datetime) -> None:
    if target_level == DELIVERED_LEVEL:
        next_attempt_at = now + timedelta(seconds=read_retry_strategy().delivered_receipt_delay_seconds)
    else:
        next_attempt_at = now
    ReceiptNotification.objects.create(
        message_id=message_id,
        device_id=device_id,
        target_level=target_level,
        maximum_attempts=read_retry_strategy().maximum_attempts,
        next_attempt_at=next_attempt_at,
        created_at=now,
        updated_at=now,
    )


def raise_receipt_to_read(receipt: ReceiptNotification, now: datetime) -> bool:
    """The target rises from delivered to read; a pending delivered attempt is never sent again.

    A receipt confirmed at "read" is terminal. A relink-cancelled one is revived by the same
    change: its confirmed level is below "read", so it becomes pending.
    """
    if receipt.confirmed_level == READ_LEVEL:
        return False
    return update_locked_receipt(
        receipt,
        [PENDING, CONFIRMED, FAILED, CANCELLED],
        now,
        target_level=READ_LEVEL,
        cancelled_at=None,
        **build_fresh_attempt_changes(now),
    )


def start_receipt_attempt(receipt: ReceiptNotification, now: datetime) -> bool:
    """One attempt is one packet; starting it counts the attempt."""
    return update_locked_receipt(
        receipt,
        [PENDING],
        now,
        attempt_count=F("attempt_count") + 1,
        round_pending=True,
        round_started_at=now,
    )


def record_receipt_packet_sent(
    receipt: ReceiptNotification,
    suggested_timeout_milliseconds: int | None,
    now: datetime,
) -> bool:
    """The attempt's packet went to the node (or its outcome is unknown); the next attempt waits for the pause."""
    next_attempt_at = calculate_round_completion_due_time(
        attempt_count=receipt.attempt_count,
        round_started_at=receipt.round_started_at,
        last_sent_at=now,
        last_packet_suggested_timeout_milliseconds=suggested_timeout_milliseconds,
        retry_strategy=read_retry_strategy(),
        now=now,
    )
    return update_locked_receipt(
        receipt,
        [PENDING],
        now,
        last_sent_at=now,
        round_pending=False,
        next_attempt_at=next_attempt_at,
    )


def record_receipt_confirmation(receipt: ReceiptNotification, confirmed_level: int, now: datetime) -> bool:
    """A "C" from the device: the confirmed level rises, capped at the target, since "read" implies "delivered".

    The confirmation time is always kept, as evidence that DMs reach the device; the levels
    change only on a pending or failed receipt.
    """
    ReceiptNotification.objects.filter(id=receipt.pk).update(last_confirmation_received_at=now)
    receipt.last_confirmation_received_at = now
    new_confirmed_level = max(receipt.confirmed_level, min(confirmed_level, receipt.target_level))
    if new_confirmed_level == receipt.confirmed_level:
        return False
    changes: dict[str, Any] = {"confirmed_level": Greatest(F("confirmed_level"), new_confirmed_level)}
    if new_confirmed_level == receipt.target_level:
        changes["state"] = CONFIRMED
    return update_locked_receipt(receipt, [PENDING, FAILED], now, **changes)


def fail_receipt_after_exhausted_attempts(receipt: ReceiptNotification, now: datetime) -> bool:
    return update_locked_receipt(receipt, [PENDING], now, state=FAILED, failed_at=now)


def rearm_receipts_for_refresh(device_id: int, sender_id: int, recipient_id: int, now: datetime) -> int:
    """A refresh of the conversation with the recipient restarts this device's unconfirmed receipts.

    Only receipts of messages this device sent: the others carry ids it does not know. A
    receipt cancelled by an earlier relink is revived and ends confirmed when the device already
    confirmed the message's current level.
    """
    receipts = list(
        ReceiptNotification.objects.select_for_update(of=("self",))
        .select_related("message")
        .filter(
            device_id=device_id,
            state__in=RECEIPT_STATES_A_REFRESH_REARMS,
            message__sender_id=sender_id,
            message__recipient_id=recipient_id,
            message__sender_device_id=device_id,
        )
        .order_by("id")
    )
    rearmed_receipt_count = 0
    for receipt in receipts:
        if receipt.state == CANCELLED:
            was_changed = revive_cancelled_receipt(receipt, receipt.message, now)
        else:
            was_changed = update_locked_receipt(receipt, [PENDING, FAILED], now, **build_fresh_attempt_changes(now))
        rearmed_receipt_count += int(was_changed)
    return rearmed_receipt_count


def revive_cancelled_receipt(receipt: ReceiptNotification, message: Message, now: datetime) -> bool:
    """The device belongs to the message's sender again: the target becomes the message's current level.

    confirmed when the device already confirmed that level, otherwise pending with fresh counters.
    """
    target_level = max(receipt.target_level, read_message_level(message))
    if receipt.confirmed_level == target_level:
        return update_locked_receipt(
            receipt,
            [CANCELLED],
            now,
            state=CONFIRMED,
            target_level=target_level,
            cancelled_at=None,
        )
    return update_locked_receipt(
        receipt,
        [CANCELLED],
        now,
        target_level=target_level,
        cancelled_at=None,
        **build_fresh_attempt_changes(now),
    )


def cancel_device_receipts_for_relink(device_id: int, now: datetime) -> int:
    """The device now belongs to another user; a receipt it confirmed at "read" is terminal and stays."""
    receipts = ReceiptNotification.objects.select_for_update().filter(device_id=device_id).exclude(state=CANCELLED)
    receipts_to_cancel = receipts.exclude(state=CONFIRMED, confirmed_level=READ_LEVEL)
    locked_receipt_ids = list(receipts_to_cancel.order_by("id").values_list("id", flat=True))
    return ReceiptNotification.objects.filter(id__in=locked_receipt_ids).update(
        state=CANCELLED,
        cancelled_at=now,
        updated_at=now,
    )
