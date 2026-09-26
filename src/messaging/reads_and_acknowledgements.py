"""What a device reports about the messages it received and the receipts it got: R, K and C.

K carries the device's complete received-set for a message: it replaces the recorded set,
and only a set that is all ones by itself (or a read) delivers the message to that device.
C confirms a receipt level. Neither is ever answered: an unknown or malformed one is dropped.
R records a read, which also confirms delivery to that device, and is answered "r".

Each runs in one transaction that takes a KEY SHARE lock on the device first, so a deletion of
the device waits for it, then locks the message and the rows it changes.
"""

from dataclasses import dataclass
from datetime import datetime

from directory.models import Contact
from messaging.deliveries import (
    mark_delivery_delivered,
    record_delivery_acknowledgement_time,
    record_delivery_read,
    record_incomplete_delivery_status,
)
from messaging.models import Message, MessageDelivery, ReceiptNotification
from messaging.receipts import raise_receipts_for_message, record_receipt_confirmation
from messaging.refresh_sessions import (
    arm_next_refresh_session_head,
    complete_refresh_session,
    find_active_refresh_session_id,
)
from messaging.service_transactions import lock_device_for_key_share, run_in_service_transaction
from protocol.constants import RECEIPT_LEVEL_NUMBERS, ErrorCode
from protocol.error_replies import build_request_error_reply
from protocol.message_types import (
    DeliveryAcknowledgement,
    ErrorReply,
    ReadReply,
    ReadRequest,
    ReceiptAcknowledgement,
)
from protocol.received_sets import (
    calculate_all_parts_mask,
    convert_received_set_to_parts_mask,
    received_set_matches_part_count,
)
from protocol.usernames import normalize_username_for_lookup

DELIVERY_STATES_A_CONFIRMATION_DELIVERS = (
    MessageDelivery.State.PENDING,
    MessageDelivery.State.QUEUED_FOR_REFRESH,
    MessageDelivery.State.FAILED,
)


@dataclass(frozen=True, kw_only=True)
class ReadRequestOutcome:
    reply: ReadReply | ErrorReply
    outcome_summary: str


@dataclass(frozen=True, kw_only=True)
class AcknowledgementOutcome:
    """An acknowledgement is never answered; the summary says what it changed, for the traffic log."""

    outcome_summary: str


@dataclass(frozen=True, kw_only=True)
class DeliveryConfirmation:
    """A delivery that was just confirmed, and the refresh session that must complete once the receipts are done."""

    outcome_summary: str
    refresh_session_id_to_complete: int | None = None


def record_read(device: Contact, request: ReadRequest, now: datetime) -> ReadRequestOutcome:
    """Record the read on the message and on this device's delivery, which it also confirms.

    Answers "r <sender> <id>", or "e NOT_SIGNED_IN / NOT_FOUND R <sender> <id>". The first read
    of the message starts read receipts to the sender's devices.
    """
    return run_in_service_transaction(lambda: record_read_in_transaction(device.pk, request, now))


def record_read_in_transaction(device_id: int, request: ReadRequest, now: datetime) -> ReadRequestOutcome:
    locked_device = lock_device_for_key_share(device_id)
    if locked_device is None or locked_device.user_id is None:
        return ReadRequestOutcome(
            reply=build_request_error_reply(ErrorCode.NOT_SIGNED_IN, request),
            outcome_summary="the device is not signed in",
        )
    message = lock_incoming_message(locked_device.user_id, request.sender_username, request.message_id)
    if message is None:
        return ReadRequestOutcome(
            reply=build_request_error_reply(ErrorCode.NOT_FOUND, request),
            outcome_summary="no such incoming message",
        )

    delivery = lock_delivery(message.pk, device_id)
    delivery_confirmation = confirm_delivery_by_read(delivery, now)
    message_level_rose = record_message_confirmation(message, now, is_read=True)
    if message_level_rose:
        raise_receipts_for_message(message, now)
    complete_confirmed_head_session(delivery_confirmation, now)

    return ReadRequestOutcome(
        reply=ReadReply(sender_username=message.sender.username, message_id=message.client_message_id),
        outcome_summary=delivery_confirmation.outcome_summary,
    )


def confirm_delivery_by_read(delivery: MessageDelivery | None, now: datetime) -> DeliveryConfirmation:
    if delivery is None:
        return DeliveryConfirmation(outcome_summary="read recorded; no delivery to this device")
    if delivery.state in DELIVERY_STATES_A_CONFIRMATION_DELIVERS:
        return deliver(delivery, now, is_read=True)
    if delivery.state == MessageDelivery.State.DELIVERED:
        was_first_read = record_delivery_read(delivery, now)
        return DeliveryConfirmation(outcome_summary="read recorded" if was_first_read else "read already recorded")
    return DeliveryConfirmation(outcome_summary="read recorded; the delivery to this device was cancelled")


def record_delivery_acknowledgement(
    device: Contact,
    acknowledgement: DeliveryAcknowledgement,
    now: datetime,
) -> AcknowledgementOutcome:
    """The set becomes the delivery's received set; a set of all ones delivers the message to the device."""
    return run_in_service_transaction(
        lambda: record_delivery_acknowledgement_in_transaction(device.pk, acknowledgement, now)
    )


def record_delivery_acknowledgement_in_transaction(
    device_id: int,
    acknowledgement: DeliveryAcknowledgement,
    now: datetime,
) -> AcknowledgementOutcome:
    locked_device = lock_device_for_key_share(device_id)
    if locked_device is None or locked_device.user_id is None:
        return AcknowledgementOutcome(outcome_summary="dropped: the device is not signed in")
    message = lock_incoming_message(locked_device.user_id, acknowledgement.sender_username, acknowledgement.message_id)
    if message is None:
        return AcknowledgementOutcome(outcome_summary="dropped: no such incoming message")
    if not received_set_matches_part_count(acknowledgement.received_set, message.part_count):
        return AcknowledgementOutcome(outcome_summary="dropped: the received-set does not match the part count")
    delivery = lock_delivery(message.pk, device_id)
    if delivery is None:
        return AcknowledgementOutcome(outcome_summary="dropped: no delivery of this message to this device")

    record_delivery_acknowledgement_time(delivery, now)
    if delivery.state not in DELIVERY_STATES_A_CONFIRMATION_DELIVERS:
        return AcknowledgementOutcome(outcome_summary=f"no change: the delivery is {delivery.state}")

    received_parts_mask = convert_received_set_to_parts_mask(acknowledgement.received_set)
    if received_parts_mask != calculate_all_parts_mask(message.part_count):
        record_incomplete_delivery_status(delivery, received_parts_mask, now)
        return AcknowledgementOutcome(outcome_summary=f"received-set {acknowledgement.received_set} recorded")

    delivery_confirmation = deliver(delivery, now, received_parts_mask=received_parts_mask)
    if record_message_confirmation(message, now, is_read=False):
        raise_receipts_for_message(message, now)
    complete_confirmed_head_session(delivery_confirmation, now)
    return AcknowledgementOutcome(outcome_summary=delivery_confirmation.outcome_summary)


def deliver(
    delivery: MessageDelivery,
    now: datetime,
    *,
    received_parts_mask: int | None = None,
    is_read: bool = False,
) -> DeliveryConfirmation:
    """Delivered, from pending, queued_for_refresh or failed; a delivered head hands its session to the next message."""
    previous_state = delivery.state
    head_session_id = (
        find_active_refresh_session_id(delivery) if previous_state == MessageDelivery.State.PENDING else None
    )
    mark_delivery_delivered(delivery, now, received_parts_mask=received_parts_mask, is_read=is_read)

    outcome_summary = f"delivered to this device (was {previous_state})"
    if head_session_id is None:
        return DeliveryConfirmation(outcome_summary=outcome_summary)
    if arm_next_refresh_session_head(head_session_id, now) is not None:
        return DeliveryConfirmation(outcome_summary=f"{outcome_summary}; the refresh moved to its next message")
    return DeliveryConfirmation(
        outcome_summary=f"{outcome_summary}; the refresh is complete",
        refresh_session_id_to_complete=head_session_id,
    )


def complete_confirmed_head_session(delivery_confirmation: DeliveryConfirmation, now: datetime) -> None:
    """Refresh sessions come after receipts in the lock order, so completing one is the transaction's last step."""
    if delivery_confirmation.refresh_session_id_to_complete is not None:
        complete_refresh_session(delivery_confirmation.refresh_session_id_to_complete, now)


def record_message_confirmation(message: Message, now: datetime, *, is_read: bool) -> bool:
    """Set the message's first delivery time, and its first read time for a read; True when either was new."""
    changes: dict[str, datetime] = {}
    if message.delivered_at is None:
        changes["delivered_at"] = now
    if is_read and message.read_at is None:
        changes["read_at"] = now
    if not changes:
        return False

    Message.objects.filter(id=message.pk).update(**changes)
    for field_name, value in changes.items():
        setattr(message, field_name, value)
    return True


def record_receipt_acknowledgement(
    device: Contact,
    acknowledgement: ReceiptAcknowledgement,
    now: datetime,
) -> AcknowledgementOutcome:
    """Raise the confirmed level of this device's receipt; levels only rise, capped at the receipt's target."""
    return run_in_service_transaction(
        lambda: record_receipt_acknowledgement_in_transaction(device.pk, acknowledgement, now)
    )


def record_receipt_acknowledgement_in_transaction(
    device_id: int,
    acknowledgement: ReceiptAcknowledgement,
    now: datetime,
) -> AcknowledgementOutcome:
    locked_device = lock_device_for_key_share(device_id)
    if locked_device is None or locked_device.user_id is None:
        return AcknowledgementOutcome(outcome_summary="dropped: the device is not signed in")
    message_id = (
        Message.objects.filter(
            sender_id=locked_device.user_id,
            client_message_id=acknowledgement.message_id,
            recipient__username_lookup=normalize_username_for_lookup(acknowledgement.recipient_username),
            accepted_at__isnull=False,
        )
        .values_list("id", flat=True)
        .first()
    )
    if message_id is None:
        return AcknowledgementOutcome(outcome_summary="dropped: no such outgoing message")
    receipt = ReceiptNotification.objects.select_for_update().filter(message_id=message_id, device_id=device_id).first()
    if receipt is None:
        return AcknowledgementOutcome(outcome_summary="dropped: no receipt of this message to this device")

    confirmed_level = RECEIPT_LEVEL_NUMBERS[acknowledgement.receipt_level]
    if record_receipt_confirmation(receipt, confirmed_level, now):
        return AcknowledgementOutcome(
            outcome_summary=f"receipt confirmed at level {receipt.confirmed_level} of {receipt.target_level}"
        )
    return AcknowledgementOutcome(outcome_summary=f"no change: the receipt is {receipt.state}")


def lock_incoming_message(recipient_id: int, sender_username: str, client_message_id: int) -> Message | None:
    """The accepted message (sender, id) addressed to the user; an incomplete message is never found."""
    return (
        Message.objects.select_for_update(of=("self",))
        .select_related("sender")
        .filter(
            recipient_id=recipient_id,
            client_message_id=client_message_id,
            sender__username_lookup=normalize_username_for_lookup(sender_username),
            accepted_at__isnull=False,
        )
        .first()
    )


def lock_delivery(message_id: int, device_id: int) -> MessageDelivery | None:
    return MessageDelivery.objects.select_for_update().filter(message_id=message_id, device_id=device_id).first()
