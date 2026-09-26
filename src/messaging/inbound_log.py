"""Recording every direct message the relay node received, before it has any effect: the inbox.

The worker records each drained frame at once, in arrival order, and processes the recorded
rows afterwards in id order (messaging.request_processing), so a crash between the two loses
nothing. A sign-in request's password is never stored: the row keeps "HT1 A <username>
********", and its text hash is taken over that redacted text, since a fast hash of a password
would be a guessable copy of it.

A firmware-level repeat (the same contact, MeshCore timestamp and text within 24 hours) is only
counted on the original row: MeshCore apps resend a DM with the same timestamp when its
firmware acknowledgement is lost, while a client's own retry always carries a new timestamp.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import F

from directory.models import Contact
from messaging.models import InboundDirectMessage
from messaging.service_transactions import run_in_service_transaction
from protocol.constants import FIRMWARE_REPEAT_WINDOW_HOURS

REDACTED_PASSWORD = "********"
# Any protocol version and any first token after "A": a malformed sign-in request carries a
# password just as well. Only a text that starts like one is redacted; the same characters
# inside a message part are that part's text and are stored as sent.
ACCOUNT_REQUEST_WITH_PASSWORD_PATTERN = re.compile(r"^(HT[0-9]{1,3} A [^ ]*) .*", re.DOTALL)
# MeshCore's path length of a DM that arrived over a stored route; anything else is a flood hop count.
DIRECT_ARRIVAL_PATH_LENGTH = 255


@dataclass(frozen=True, kw_only=True)
class ReceivedDirectMessageFrame:
    """A CONTACT_MSG_RECV frame as the node delivered it."""

    # The first six bytes of the sender's public key, in lower-case hex.
    sender_public_key_prefix: str
    sender_timestamp: int
    text: str
    # 0 plain text, 1 CLI data, 2 signed plain text.
    text_type: int
    path_length: int
    signal_to_noise_ratio: float | None
    received_at: datetime


@dataclass(frozen=True, kw_only=True)
class RecordedInboundFrame:
    """The inbox row that holds the frame; for a firmware-level repeat, the original row that counted it."""

    inbox_row_id: int
    contact_id: int | None
    is_firmware_repeat: bool
    arrived_by_flood: bool


def redact_stored_text(text: str) -> str:
    return ACCOUNT_REQUEST_WITH_PASSWORD_PATTERN.sub(
        lambda match: f"{match.group(1)} {REDACTED_PASSWORD}", text, count=1
    )


def carries_account_request_password(text: str) -> bool:
    """True for a sign-in request with a password field; stored, that field is always the redacted placeholder."""
    return ACCOUNT_REQUEST_WITH_PASSWORD_PATTERN.fullmatch(text) is not None


def calculate_text_sha256(stored_text: str) -> str:
    return hashlib.sha256(stored_text.encode("utf-8")).hexdigest()


def record_inbound_frame(frame: ReceivedDirectMessageFrame, now: datetime) -> RecordedInboundFrame:
    """Persist the frame as an inbox row "received", or count it as a firmware-level repeat of an earlier row.

    One transaction: the contact's last_heard_at is updated first, which locks the contact
    before the inbox row that references it. A frame from an unknown prefix is recorded with
    no contact and never deduplicated.
    """
    return run_in_service_transaction(lambda: record_inbound_frame_in_transaction(frame, now))


def record_inbound_frame_in_transaction(frame: ReceivedDirectMessageFrame, now: datetime) -> RecordedInboundFrame:
    contact = find_sender_and_record_it_was_heard(frame.sender_public_key_prefix, frame.received_at)
    stored_text = redact_stored_text(frame.text)
    text_sha256 = calculate_text_sha256(stored_text)
    arrived_by_flood = frame.path_length != DIRECT_ARRIVAL_PATH_LENGTH

    if contact is not None:
        original_row_id = count_firmware_repeat(contact.pk, frame, text_sha256, now)
        if original_row_id is not None:
            return RecordedInboundFrame(
                inbox_row_id=original_row_id,
                contact_id=contact.pk,
                is_firmware_repeat=True,
                arrived_by_flood=arrived_by_flood,
            )

    inbox_row = InboundDirectMessage.objects.create(
        received_at=frame.received_at,
        sender_public_key_prefix=frame.sender_public_key_prefix,
        contact=contact,
        contact_label=str(contact) if contact is not None else "",
        sender_timestamp=frame.sender_timestamp,
        text_type=frame.text_type,
        path_length=frame.path_length,
        signal_to_noise_ratio=frame.signal_to_noise_ratio,
        text=stored_text,
        text_sha256=text_sha256,
    )
    return RecordedInboundFrame(
        inbox_row_id=inbox_row.pk,
        contact_id=contact.pk if contact is not None else None,
        is_firmware_repeat=False,
        arrived_by_flood=arrived_by_flood,
    )


def find_sender_and_record_it_was_heard(sender_public_key_prefix: str, received_at: datetime) -> Contact | None:
    updated_row_count = Contact.objects.filter(public_key_prefix=sender_public_key_prefix).update(
        last_heard_at=received_at
    )
    if updated_row_count == 0:
        return None
    return Contact.objects.only("id", "name", "public_key").get(public_key_prefix=sender_public_key_prefix)


def count_firmware_repeat(
    contact_id: int,
    frame: ReceivedDirectMessageFrame,
    text_sha256: str,
    now: datetime,
) -> int | None:
    """The id of the original row when the frame repeats one of the last 24 hours, after counting it there."""
    original_row_id = (
        InboundDirectMessage.objects.filter(
            contact_id=contact_id,
            sender_timestamp=frame.sender_timestamp,
            text_sha256=text_sha256,
            received_at__gte=now - timedelta(hours=FIRMWARE_REPEAT_WINDOW_HOURS),
        )
        .order_by("id")
        .values_list("id", flat=True)
        .first()
    )
    if original_row_id is None:
        return None
    InboundDirectMessage.objects.filter(id=original_row_id).update(
        duplicate_count=F("duplicate_count") + 1,
        last_duplicate_at=frame.received_at,
    )
    return original_row_id


def find_unprocessed_inbox_row_ids(limit: int = 100) -> list[int]:
    """Rows still "received", oldest first: the processing order, also after a restart."""
    return list(
        InboundDirectMessage.objects.filter(processing_state=InboundDirectMessage.ProcessingState.RECEIVED)
        .order_by("id")
        .values_list("id", flat=True)[:limit]
    )


def record_flood_arrival_route_reset(inbox_row_id: int) -> None:
    """The worker reset the route to the sender before replying to this flood arrival."""
    InboundDirectMessage.objects.filter(id=inbox_row_id).update(route_reset_performed=True)
