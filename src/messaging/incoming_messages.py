"""Receiving a message part by part (the M request), and dropping uploads that were never completed.

A message row exists from its first part. It is incomplete while some part is missing and
accepted once every part is held; the part that completes it creates its deliveries in the same
transaction. While a message is incomplete, only the device that sent its first part may add
parts: another device of the same user may repeat a part the server holds, and anything else
from it is an id conflict, so two messages that share an id by mistake are never spliced.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction

from directory.contacts import LockedRowChangedError
from directory.models import Contact, User
from messaging.fan_out import create_deliveries_for_accepted_message
from messaging.models import Message
from messaging.service_transactions import run_in_service_transaction
from protocol.constants import INCOMPLETE_MESSAGE_RETENTION_HOURS, ErrorCode
from protocol.error_replies import build_request_error_reply
from protocol.message_types import ErrorReply, MessagePartRequest, SendStatusReply
from protocol.received_sets import convert_parts_mask_to_received_set, is_valid_part_numbering
from protocol.text_validation import is_valid_part_text
from protocol.usernames import normalize_username_for_lookup


@dataclass(frozen=True, kw_only=True)
class MessagePartOutcome:
    reply: SendStatusReply | ErrorReply
    outcome_summary: str
    # A status with zeros goes out only once no new part of the message arrived for 5 s, so one
    # status covers a burst of parts; every other answer goes out at once.
    is_incomplete_status: bool = False


def accept_message_part(device: Contact, part: MessagePartRequest, now: datetime) -> MessagePartOutcome:
    """Store one part and answer with the server's received-set for the message.

    Answers "k <recipient> <id> <received-set>", or "e NOT_SIGNED_IN / NO_SUCH_USER / SELF /
    PART_INVALID / ID_CONFLICT M <recipient> <id>". A repeated part stores nothing and gets the
    current set again.
    """
    return run_in_service_transaction(lambda: accept_message_part_in_transaction(device.pk, part, now))


def accept_message_part_in_transaction(device_id: int, part: MessagePartRequest, now: datetime) -> MessagePartOutcome:
    sender_id = Contact.objects.filter(id=device_id).values_list("user_id", flat=True).first()
    if sender_id is None:
        return build_part_error_outcome(ErrorCode.NOT_SIGNED_IN, part, "the device is not signed in")
    recipient = User.objects.filter(username_lookup=normalize_username_for_lookup(part.recipient_username)).first()
    if recipient is None:
        return build_part_error_outcome(ErrorCode.NO_SUCH_USER, part, "no such recipient")
    if recipient.pk == sender_id:
        return build_part_error_outcome(ErrorCode.SELF, part, "the recipient is the sender")

    lock_sender_recipient_and_device(sender_id, recipient.pk, device_id)

    if not is_valid_part_numbering(part.part_number, part.part_count) or not is_valid_part_text(part.part_text):
        return build_part_error_outcome(ErrorCode.PART_INVALID, part, "invalid part number, count or text")

    message = lock_or_create_message(sender_id, recipient.pk, device_id, part, now)
    conflict_reason = find_part_conflict(message, recipient.pk, device_id, part)
    if conflict_reason:
        return build_part_error_outcome(ErrorCode.ID_CONFLICT, part, conflict_reason)

    part_was_new = store_part(message, part, now)
    part_summary = f"part {part.part_number}/{part.part_count} {'stored' if part_was_new else 'already held'}"

    if message.accepted_at is None and all(part_text is not None for part_text in message.part_texts):
        delivery_count = accept_message(message, now)
        part_summary += f"; message accepted with {delivery_count} deliveries"

    received_set = build_received_set(message)
    return MessagePartOutcome(
        reply=SendStatusReply(
            recipient_username=recipient.username,
            message_id=part.message_id,
            received_set=received_set,
        ),
        outcome_summary=part_summary,
        is_incomplete_status=message.accepted_at is None,
    )


def build_part_error_outcome(error_code: ErrorCode, part: MessagePartRequest, reason: str) -> MessagePartOutcome:
    return MessagePartOutcome(reply=build_request_error_reply(error_code, part), outcome_summary=reason)


def lock_sender_recipient_and_device(sender_id: int, recipient_id: int, device_id: int) -> None:
    """Lock both users in ascending id, then the device; start over if either user or the link changed meanwhile."""
    locked_user_ids = list(
        User.objects.select_for_update()
        .filter(id__in=[sender_id, recipient_id])
        .order_by("id")
        .values_list("id", flat=True)
    )
    locked_device_user_id = (
        Contact.objects.select_for_update().filter(id=device_id).values_list("user_id", flat=True).first()
    )
    if len(locked_user_ids) != 2 or locked_device_user_id != sender_id:
        raise LockedRowChangedError(f"A user or device {device_id} changed while a message part was being accepted.")


def lock_or_create_message(
    sender_id: int,
    recipient_id: int,
    device_id: int,
    part: MessagePartRequest,
    now: datetime,
) -> Message:
    existing_message = (
        Message.objects.select_for_update().filter(sender_id=sender_id, client_message_id=part.message_id).first()
    )
    if existing_message is not None:
        return existing_message

    try:
        with transaction.atomic():
            return Message.objects.create(
                sender_id=sender_id,
                sender_device_id=device_id,
                recipient_id=recipient_id,
                client_message_id=part.message_id,
                part_count=part.part_count,
                part_texts=[None] * part.part_count,
                created_at=now,
                last_part_at=now,
            )
    except IntegrityError:
        # Another device of the same user created the message in the meantime.
        return Message.objects.select_for_update().get(sender_id=sender_id, client_message_id=part.message_id)


def find_part_conflict(message: Message, recipient_id: int, device_id: int, part: MessagePartRequest) -> str:
    """The reason the part cannot belong to this message, or "" when it can."""
    if message.recipient_id != recipient_id:
        return "the id is used by a message to another recipient"
    if message.part_count != part.part_count:
        return "the id is used by a message with another part count"

    held_part_text = message.part_texts[part.part_number - 1]
    if held_part_text is not None and held_part_text != part.part_text:
        return f"part {part.part_number} is held with another text"
    if message.accepted_at is None and held_part_text is None and message.sender_device_id != device_id:
        return "only the device that sent the first part may add parts to an incomplete message"
    return ""


def store_part(message: Message, part: MessagePartRequest, now: datetime) -> bool:
    """Returns whether the part was new. A repeated part of an incomplete message still keeps the upload alive."""
    part_index = part.part_number - 1
    part_was_new = message.part_texts[part_index] is None
    if part_was_new:
        message.part_texts[part_index] = part.part_text
    if message.accepted_at is None:
        message.last_part_at = now
    message.save(update_fields=["part_texts", "last_part_at"])
    return part_was_new


def accept_message(message: Message, now: datetime) -> int:
    """Every part is held: the text is set, and the deliveries are created in the same transaction."""
    message.text = "".join(part_text for part_text in message.part_texts if part_text is not None)
    message.accepted_at = now
    message.save(update_fields=["text", "accepted_at"])
    return create_deliveries_for_accepted_message(message, now)


def build_received_set(message: Message) -> str:
    held_parts_mask = 0
    for part_index, part_text in enumerate(message.part_texts):
        if part_text is not None:
            held_parts_mask |= 1 << part_index
    return convert_parts_mask_to_received_set(held_parts_mask, message.part_count)


def delete_expired_incomplete_messages(now: datetime) -> int:
    """Drop an upload whose last part arrived 24 hours ago; a client that resumes later is told what is missing."""
    expiry_time = now - timedelta(hours=INCOMPLETE_MESSAGE_RETENTION_HOURS)

    def delete_in_transaction() -> int:
        _, deleted_row_counts_by_model = Message.objects.filter(
            accepted_at__isnull=True,
            last_part_at__lte=expiry_time,
        ).delete()
        return deleted_row_counts_by_model.get(Message._meta.label, 0)

    return run_in_service_transaction(delete_in_transaction)
