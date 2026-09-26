"""What the sender loop sends next: every outbound DM becomes an outbound packet row before it is sent.

Replies go first (the worker's reply queue, prepared by prepare_reply_packet). Then the due
delivery or receipt with the earliest next_attempt_at, deliveries first on a tie: a delivery
round sends one packet per part the device has not reported, a receipt attempt one packet. A
delivery that has not started a round since it became pending also needs a free place: its
device may have only a few deliveries in progress at a time (pending with a round started), new
ones and refresh heads together, which bounds what waits in that device's node. Nothing is
sent to a contact that is not on the node, and waiting spends no attempt.

Every packet gets a MeshCore timestamp larger than every earlier one, persisted with the packet
before the send, and the MeshCore attempt is always 0: the firmware's acknowledgement hash
leaves out the recipient, so reusing a timestamp would give two packets the same code.

The sender loop calls these functions one at a time and records each packet's outcome
(messaging.outbound_packets.record_send_outcome) before it prepares the next one, since a round
sends its next part only once the previous one left the round.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, connection, transaction
from django.db.models import Max

from directory.models import Contact
from messaging.deliveries import (
    complete_delivery_round,
    fail_delivery_after_exhausted_attempts,
    find_last_round_packet_suggested_timeout,
    start_delivery_round,
)
from messaging.models import MessageDelivery, OutboundPacket, ReceiptNotification
from messaging.receipts import fail_receipt_after_exhausted_attempts, start_receipt_attempt
from messaging.refresh_sessions import find_active_refresh_session_id, stop_refresh_session
from messaging.service_transactions import read_maximum_active_deliveries_per_device, run_in_service_transaction
from protocol.constants import RECEIPT_LEVELS_BY_NUMBER
from protocol.formatting import ensure_direct_message_text_is_sendable, format_delivery_part, format_receipt_push

CANDIDATE_LIMIT_PER_KIND = 20
type QueryParameters = dict[str, datetime | int | str]
DELIVERY_CANDIDATE_ORDER = 0
RECEIPT_CANDIDATE_ORDER = 1

# A pending delivery is eligible on an on_node device once its round started, or while its
# device has a free place for a delivery in progress. Row locks: the delivery FOR UPDATE, its
# contact FOR KEY SHARE, both SKIP LOCKED, so a contact being deleted is skipped, never waited for.
DUE_DELIVERY_CANDIDATES_SQL = """
    SELECT delivery.id
    FROM message_deliveries AS delivery
    JOIN contacts AS device ON device.id = delivery.device_id
    WHERE delivery.state = %(pending_state)s
      AND delivery.next_attempt_at <= %(now)s
      AND device.node_sync_state = %(on_node_state)s
      AND (
          delivery.round_started_at IS NOT NULL
          OR (
              SELECT count(*)
              FROM message_deliveries AS delivery_in_progress
              WHERE delivery_in_progress.device_id = delivery.device_id
                AND delivery_in_progress.state = %(pending_state)s
                AND delivery_in_progress.round_started_at IS NOT NULL
          ) < %(maximum_active_deliveries_per_device)s
      )
    ORDER BY delivery.next_attempt_at, delivery.id
    LIMIT %(candidate_limit)s
    FOR UPDATE OF delivery SKIP LOCKED
    FOR KEY SHARE OF device SKIP LOCKED
"""
DUE_RECEIPT_CANDIDATES_SQL = """
    SELECT receipt.id
    FROM receipt_notifications AS receipt
    JOIN contacts AS device ON device.id = receipt.device_id
    WHERE receipt.state = %(pending_state)s
      AND receipt.next_attempt_at <= %(now)s
      AND device.node_sync_state = %(on_node_state)s
    ORDER BY receipt.next_attempt_at, receipt.id
    LIMIT %(candidate_limit)s
    FOR UPDATE OF receipt SKIP LOCKED
    FOR KEY SHARE OF device SKIP LOCKED
"""
EARLIEST_ELIGIBLE_DELIVERY_SQL = """
    SELECT min(delivery.next_attempt_at)
    FROM message_deliveries AS delivery
    JOIN contacts AS device ON device.id = delivery.device_id
    WHERE delivery.state = %(pending_state)s
      AND device.node_sync_state = %(on_node_state)s
      AND (
          delivery.round_started_at IS NOT NULL
          OR (
              SELECT count(*)
              FROM message_deliveries AS delivery_in_progress
              WHERE delivery_in_progress.device_id = delivery.device_id
                AND delivery_in_progress.state = %(pending_state)s
                AND delivery_in_progress.round_started_at IS NOT NULL
          ) < %(maximum_active_deliveries_per_device)s
      )
"""
EARLIEST_ELIGIBLE_RECEIPT_SQL = """
    SELECT min(receipt.next_attempt_at)
    FROM receipt_notifications AS receipt
    JOIN contacts AS device ON device.id = receipt.device_id
    WHERE receipt.state = %(pending_state)s
      AND device.node_sync_state = %(on_node_state)s
"""
LOCK_CONTACT_FOR_KEY_SHARE_SQL = "SELECT id FROM contacts WHERE id = %s AND node_sync_state = %s FOR KEY SHARE"


@dataclass(frozen=True, kw_only=True)
class PacketDescriptor:
    """Everything the sender loop needs to send one prepared packet."""

    packet_id: int
    contact_id: int
    contact_public_key: str
    text: str
    sender_timestamp: int
    purpose: OutboundPacket.Purpose
    # For replies: the answered request's key; "" otherwise.
    reply_key: str


@dataclass(frozen=True, kw_only=True)
class DueWorkCounts:
    deliveries_due: int
    receipts_due: int


def prepare_next_packet(now: datetime, connection_generation: int) -> PacketDescriptor | None:
    """Prepare the next delivery part or receipt to send, or return None when nothing may be sent now.

    One transaction. It locks up to 20 due deliveries and 20 due receipts and walks them by due
    time. On the way it completes rounds that have nothing left to send, fails deliveries and
    receipts whose attempts ran out (stopping the refresh of a failed head), and starts the
    round or attempt of the row it prepares a packet for. Call it only in relay mode running,
    with the node connected.
    """
    return run_in_service_transaction(lambda: prepare_next_packet_in_transaction(now, connection_generation))


def prepare_next_packet_in_transaction(now: datetime, connection_generation: int) -> PacketDescriptor | None:
    maximum_active_deliveries_per_device = read_maximum_active_deliveries_per_device()
    for candidate in lock_due_candidates(now, maximum_active_deliveries_per_device):
        if isinstance(candidate, MessageDelivery):
            packet_descriptor = prepare_delivery_packet(
                candidate, now, connection_generation, maximum_active_deliveries_per_device
            )
        else:
            packet_descriptor = prepare_receipt_packet(candidate, now, connection_generation)
        if packet_descriptor is not None:
            return packet_descriptor
    return None


def lock_due_candidates(
    now: datetime,
    maximum_active_deliveries_per_device: int,
) -> list[MessageDelivery | ReceiptNotification]:
    query_parameters: QueryParameters = {
        "now": now,
        "on_node_state": Contact.NodeSyncState.ON_NODE,
        "maximum_active_deliveries_per_device": maximum_active_deliveries_per_device,
        "candidate_limit": CANDIDATE_LIMIT_PER_KIND,
    }
    delivery_ids = select_ids(
        DUE_DELIVERY_CANDIDATES_SQL, {**query_parameters, "pending_state": MessageDelivery.State.PENDING}
    )
    receipt_ids = select_ids(
        DUE_RECEIPT_CANDIDATES_SQL, {**query_parameters, "pending_state": ReceiptNotification.State.PENDING}
    )
    deliveries = MessageDelivery.objects.select_related("message__sender", "device").filter(id__in=delivery_ids)
    receipts = ReceiptNotification.objects.select_related("message__recipient", "device").filter(id__in=receipt_ids)

    def order_candidate(candidate: MessageDelivery | ReceiptNotification) -> tuple[datetime | None, int, int]:
        kind_order = DELIVERY_CANDIDATE_ORDER if isinstance(candidate, MessageDelivery) else RECEIPT_CANDIDATE_ORDER
        return candidate.next_attempt_at, kind_order, candidate.pk

    return sorted([*deliveries, *receipts], key=order_candidate)


def select_ids(sql: str, query_parameters: QueryParameters) -> list[int]:
    with connection.cursor() as cursor:
        cursor.execute(sql, query_parameters)
        return [row[0] for row in cursor.fetchall()]


def prepare_delivery_packet(
    delivery: MessageDelivery,
    now: datetime,
    connection_generation: int,
    maximum_active_deliveries_per_device: int,
) -> PacketDescriptor | None:
    is_round_finished = find_parts_left_in_round(delivery) == 0
    if is_round_finished and not advance_delivery_to_new_round(delivery, now, maximum_active_deliveries_per_device):
        return None

    part_number = find_lowest_part_number(find_parts_left_in_round(delivery))
    if part_number is None:
        return None
    message = delivery.message
    text = format_delivery_part(
        message.sender.username,
        message.client_message_id,
        part_number,
        message.part_count,
        message.part_texts[part_number - 1],
    )
    packet = insert_prepared_packet(
        contact=delivery.device,
        purpose=OutboundPacket.Purpose.DELIVERY,
        text=text,
        now=now,
        connection_generation=connection_generation,
        message_delivery_id=delivery.pk,
        part_number=part_number,
        arm_generation=delivery.arm_generation,
        attempt_number=delivery.attempt_count,
    )
    return build_packet_descriptor(packet, delivery.device)


def find_parts_left_in_round(delivery: MessageDelivery) -> int:
    """The parts of the current round still to send, leaving out what the device reported meanwhile."""
    return delivery.round_pending_parts_mask & ~delivery.parts_received_mask


def advance_delivery_to_new_round(
    delivery: MessageDelivery,
    now: datetime,
    maximum_active_deliveries_per_device: int,
) -> bool:
    """Nothing of the current round is left: complete it, fail an exhausted delivery, or start the next round.

    Returns whether a new round started.
    """
    if delivery.round_pending_parts_mask != 0:
        complete_delivery_round(delivery, find_last_round_packet_suggested_timeout(delivery), now)
        return False
    if delivery.attempt_count >= delivery.maximum_attempts:
        fail_exhausted_delivery(delivery, now)
        return False
    if delivery.round_started_at is None:
        deliveries_in_progress = MessageDelivery.objects.filter(
            device_id=delivery.device_id,
            state=MessageDelivery.State.PENDING,
            round_started_at__isnull=False,
        ).count()
        if deliveries_in_progress >= maximum_active_deliveries_per_device:
            return False
    return start_delivery_round(delivery, delivery.message.part_count, now)


def fail_exhausted_delivery(delivery: MessageDelivery, now: datetime) -> None:
    """A failed refresh head stops its session in the same transaction: the messages behind it fail unsent."""
    head_session_id = find_active_refresh_session_id(delivery)
    fail_delivery_after_exhausted_attempts(delivery, now)
    if head_session_id is not None:
        stop_refresh_session(head_session_id, now)


def find_lowest_part_number(parts_mask: int) -> int | None:
    """Bit n - 1 stands for part n; rounds send parts in ascending order."""
    part_number = 1
    while parts_mask:
        if parts_mask & 1:
            return part_number
        parts_mask >>= 1
        part_number += 1
    return None


def prepare_receipt_packet(
    receipt: ReceiptNotification,
    now: datetime,
    connection_generation: int,
) -> PacketDescriptor | None:
    """The packet always carries the target level read in this transaction, so a raised target is never undercut."""
    if not receipt.round_pending:
        if receipt.attempt_count >= receipt.maximum_attempts:
            fail_receipt_after_exhausted_attempts(receipt, now)
            return None
        if not start_receipt_attempt(receipt, now):
            return None

    message = receipt.message
    text = format_receipt_push(
        message.recipient.username,
        message.client_message_id,
        RECEIPT_LEVELS_BY_NUMBER[receipt.target_level],
    )
    packet = insert_prepared_packet(
        contact=receipt.device,
        purpose=OutboundPacket.Purpose.RECEIPT,
        text=text,
        now=now,
        connection_generation=connection_generation,
        receipt_notification_id=receipt.pk,
        receipt_level=receipt.target_level,
        arm_generation=receipt.arm_generation,
        attempt_number=receipt.attempt_count,
    )
    return build_packet_descriptor(packet, receipt.device)


def prepare_reply_packet(
    *,
    contact_id: int,
    reply_key: str,
    text: str,
    now: datetime,
    connection_generation: int,
) -> PacketDescriptor | None:
    """Prepare a reply from the worker's reply queue; None drops it, for a contact that is gone or not on the node."""
    ensure_direct_message_text_is_sendable(text)
    return run_in_service_transaction(
        lambda: prepare_reply_packet_in_transaction(contact_id, reply_key, text, now, connection_generation)
    )


def prepare_reply_packet_in_transaction(
    contact_id: int,
    reply_key: str,
    text: str,
    now: datetime,
    connection_generation: int,
) -> PacketDescriptor | None:
    with connection.cursor() as cursor:
        cursor.execute(LOCK_CONTACT_FOR_KEY_SHARE_SQL, [contact_id, Contact.NodeSyncState.ON_NODE])
        if cursor.fetchone() is None:
            return None
    contact = Contact.objects.only("id", "name", "public_key").get(id=contact_id)
    packet = insert_prepared_packet(
        contact=contact,
        purpose=OutboundPacket.Purpose.REPLY,
        text=text,
        now=now,
        connection_generation=connection_generation,
        reply_key=reply_key,
    )
    return build_packet_descriptor(packet, contact)


def insert_prepared_packet(
    *,
    contact: Contact,
    purpose: OutboundPacket.Purpose,
    text: str,
    now: datetime,
    connection_generation: int,
    reply_key: str = "",
    message_delivery_id: int | None = None,
    part_number: int | None = None,
    receipt_notification_id: int | None = None,
    receipt_level: int | None = None,
    arm_generation: int | None = None,
    attempt_number: int | None = None,
) -> OutboundPacket:
    """The packet row, with its allocated timestamp, is committed before anything is sent."""
    ensure_direct_message_text_is_sendable(text)
    packet = OutboundPacket(
        contact_id=contact.pk,
        contact_label=str(contact),
        purpose=purpose,
        message_delivery_id=message_delivery_id,
        part_number=part_number,
        receipt_notification_id=receipt_notification_id,
        receipt_level=receipt_level,
        arm_generation=arm_generation,
        attempt_number=attempt_number,
        reply_key=reply_key,
        text=text,
        state=OutboundPacket.State.PREPARED,
        prepared_at=now,
        connection_generation=connection_generation,
    )
    try:
        with transaction.atomic():
            packet.sender_timestamp = allocate_sender_timestamp(now)
            packet.save()
    except IntegrityError:
        # Only a bug can reuse a timestamp; allocating once more from the stored maximum repairs it.
        with transaction.atomic():
            packet.pk = None
            packet.sender_timestamp = allocate_sender_timestamp(now)
            packet.save()
    return packet


def allocate_sender_timestamp(now: datetime) -> int:
    """max(now in Unix seconds, the largest timestamp ever sent + 1).

    The largest stored timestamp is the high-water mark, so allocation needs no in-memory seed,
    survives restarts and keeps increasing when the server clock steps backwards. At one packet
    every two seconds or slower it never runs ahead of the clock.
    """
    current_unix_seconds = int(now.timestamp())
    highest_allocated_timestamp: int | None = OutboundPacket.objects.aggregate(highest=Max("sender_timestamp"))[
        "highest"
    ]
    if highest_allocated_timestamp is None:
        return current_unix_seconds
    return max(current_unix_seconds, highest_allocated_timestamp + 1)


def build_packet_descriptor(packet: OutboundPacket, contact: Contact) -> PacketDescriptor:
    return PacketDescriptor(
        packet_id=packet.pk,
        contact_id=contact.pk,
        contact_public_key=contact.public_key,
        text=packet.text,
        sender_timestamp=packet.sender_timestamp,
        purpose=OutboundPacket.Purpose(packet.purpose),
        reply_key=packet.reply_key,
    )


def calculate_next_eligible_time(now: datetime) -> datetime | None:
    """The earliest next_attempt_at of a delivery or receipt the sender may send, or None when there is none.

    It applies the filters of prepare_next_packet (a contact on the node, a free place for a
    delivery that has not started), so work that may not be sent does not wake the sender. A
    time at or before now means something is due.
    """
    query_parameters: QueryParameters = {
        "on_node_state": Contact.NodeSyncState.ON_NODE,
        "maximum_active_deliveries_per_device": read_maximum_active_deliveries_per_device(),
    }
    earliest_times = [
        select_earliest_time(
            EARLIEST_ELIGIBLE_DELIVERY_SQL, {**query_parameters, "pending_state": MessageDelivery.State.PENDING}
        ),
        select_earliest_time(
            EARLIEST_ELIGIBLE_RECEIPT_SQL, {**query_parameters, "pending_state": ReceiptNotification.State.PENDING}
        ),
    ]
    known_times = [earliest_time for earliest_time in earliest_times if earliest_time is not None]
    return min(known_times) if known_times else None


def select_earliest_time(sql: str, query_parameters: QueryParameters) -> datetime | None:
    with connection.cursor() as cursor:
        cursor.execute(sql, query_parameters)
        row = cursor.fetchone()
    earliest_time: datetime | None = row[0] if row is not None else None
    return earliest_time


def count_due_work(now: datetime) -> DueWorkCounts:
    """The status gauges: pending deliveries and receipts whose next attempt is due."""
    return DueWorkCounts(
        deliveries_due=MessageDelivery.objects.filter(
            state=MessageDelivery.State.PENDING, next_attempt_at__lte=now
        ).count(),
        receipts_due=ReceiptNotification.objects.filter(
            state=ReceiptNotification.State.PENDING, next_attempt_at__lte=now
        ).count(),
    )
