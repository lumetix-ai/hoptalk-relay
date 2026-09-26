"""Rows of the directory and messaging tables for service and panel tests, with plausible defaults."""

import hashlib
from datetime import UTC, datetime

from directory.models import Contact, User
from messaging.models import (
    InboundDirectMessage,
    Message,
    MessageDelivery,
    OutboundPacket,
    ReceiptNotification,
    RefreshSession,
)

ROW_CREATION_TIME = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
DEFAULT_MAXIMUM_ATTEMPTS = 6


def build_public_key(contact_number: int) -> str:
    """A distinct key for every number, with a distinct six-byte prefix."""
    return f"{contact_number:012x}" + "ab" * 26


def create_user(username: str, created_at: datetime = ROW_CREATION_TIME) -> User:
    return User.objects.create(username=username, password_hash="unused", created_at=created_at)


def create_contact(
    contact_number: int,
    user: User | None = None,
    name: str = "",
    node_sync_state: Contact.NodeSyncState = Contact.NodeSyncState.ON_NODE,
    added_at: datetime = ROW_CREATION_TIME,
) -> Contact:
    return Contact.objects.create(
        public_key=build_public_key(contact_number),
        name=name or f"node {contact_number}",
        source=Contact.Source.CARD,
        added_at=added_at,
        user=user,
        linked_at=added_at if user is not None else None,
        node_sync_state=node_sync_state,
    )


def create_accepted_message(
    sender: User,
    recipient: User,
    client_message_id: int,
    sender_device: Contact | None = None,
    text: str = "Hello",
    accepted_at: datetime = ROW_CREATION_TIME,
) -> Message:
    return Message.objects.create(
        sender=sender,
        sender_device=sender_device,
        recipient=recipient,
        client_message_id=client_message_id,
        part_count=1,
        part_texts=[text],
        text=text,
        created_at=accepted_at,
        last_part_at=accepted_at,
        accepted_at=accepted_at,
    )


def create_delivery(
    message: Message,
    device: Contact,
    state: MessageDelivery.State = MessageDelivery.State.PENDING,
    refresh_session: RefreshSession | None = None,
    created_at: datetime = ROW_CREATION_TIME,
) -> MessageDelivery:
    return MessageDelivery.objects.create(
        message=message,
        device=device,
        state=state,
        refresh_session=refresh_session,
        maximum_attempts=DEFAULT_MAXIMUM_ATTEMPTS,
        next_attempt_at=created_at if state == MessageDelivery.State.PENDING else None,
        delivered_at=created_at if state == MessageDelivery.State.DELIVERED else None,
        created_at=created_at,
    )


def create_pending_receipt(
    message: Message, device: Contact, created_at: datetime = ROW_CREATION_TIME
) -> ReceiptNotification:
    return ReceiptNotification.objects.create(
        message=message,
        device=device,
        target_level=ReceiptNotification.TargetLevel.DELIVERED,
        maximum_attempts=DEFAULT_MAXIMUM_ATTEMPTS,
        next_attempt_at=created_at,
        created_at=created_at,
        updated_at=created_at,
    )


def create_refresh_session(device: Contact, peer: User, requested_at: datetime = ROW_CREATION_TIME) -> RefreshSession:
    return RefreshSession.objects.create(device=device, peer=peer, requested_at=requested_at, messages_total=1)


def create_inbound_direct_message(
    contact: Contact | None,
    text: str,
    received_at: datetime = ROW_CREATION_TIME,
    sender_timestamp: int = 1_790_000_000,
    classification: InboundDirectMessage.Classification = InboundDirectMessage.Classification.REQUEST,
    sender_public_key_prefix: str = "",
) -> InboundDirectMessage:
    return InboundDirectMessage.objects.create(
        received_at=received_at,
        sender_public_key_prefix=sender_public_key_prefix or (contact.public_key[:12] if contact else "000000000000"),
        contact=contact,
        contact_label=str(contact) if contact else "",
        sender_timestamp=sender_timestamp,
        text_type=0,
        path_length=255,
        signal_to_noise_ratio=7.5,
        text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(),
        classification=classification,
        request_type=text[4:5] if text.startswith("HT1 ") else "",
        processing_state=InboundDirectMessage.ProcessingState.PROCESSED,
        processed_at=received_at,
    )


def create_outbound_packet(
    contact: Contact | None,
    text: str,
    sender_timestamp: int,
    prepared_at: datetime = ROW_CREATION_TIME,
    purpose: OutboundPacket.Purpose = OutboundPacket.Purpose.REPLY,
    message_delivery: MessageDelivery | None = None,
    state: OutboundPacket.State = OutboundPacket.State.NODE_ACKNOWLEDGED,
) -> OutboundPacket:
    return OutboundPacket.objects.create(
        contact=contact,
        contact_label=str(contact) if contact else "deleted (000000000000)",
        purpose=purpose,
        message_delivery=message_delivery,
        part_number=1 if message_delivery else None,
        text=text,
        sender_timestamp=sender_timestamp,
        state=state,
        route=OutboundPacket.Route.DIRECT,
        expected_acknowledgement_code="0a1b2c3d",
        suggested_timeout_milliseconds=4000,
        prepared_at=prepared_at,
        queued_at=prepared_at,
        acknowledged_at=prepared_at if state == OutboundPacket.State.NODE_ACKNOWLEDGED else None,
        round_trip_milliseconds=1200 if state == OutboundPacket.State.NODE_ACKNOWLEDGED else None,
        connection_generation=1,
    )
