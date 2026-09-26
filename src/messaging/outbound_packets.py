"""The life of an outbound packet after it was prepared: the node's answer, its firmware acknowledgement, restarts.

prepared -> queued_on_node (MSG_SENT) -> node_acknowledged (the ACK push), or
acknowledgement_timed_out at its deadline (a late ACK still matches); prepared ->
rejected_by_node (an ERR reply) or outcome_unknown (a command timeout, or found prepared at
start-up).

A firmware acknowledgement only says that the other node decrypted the packet, not that the app
saw it: it frees a pacing slot and settles route decisions, and never marks anything delivered.
The outcome is recorded in a second transaction after the send; it also counts the part or
receipt as sent, guarded by the packet's copies of the arm generation and attempt number, so
the outcome of a packet from an earlier arm or round changes nothing.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from django.db.models import Max, QuerySet

from directory.models import Contact
from messaging.deliveries import record_delivery_part_sent
from messaging.models import MessageDelivery, OutboundPacket, ReceiptNotification
from messaging.receipts import record_receipt_packet_sent
from messaging.retry_schedule import calculate_acknowledgement_wait
from messaging.route_reset_evidence import decide_route_reset_at_deadline
from messaging.service_transactions import run_in_service_transaction

# RESP_CODE_ERR codes of the companion firmware.
ERR_CODE_NOT_FOUND = 2
# For a text of at most 150 bytes: the node's packet pool is exhausted.
ERR_CODE_TABLE_FULL = 3
# The node's table of expected acknowledgements holds the last 8 sends; after 10 minutes a late
# acknowledgement cannot match a packet any more.
LATE_ACKNOWLEDGEMENT_MATCH_MINUTES = 10
PACKET_STATES_AWAITING_ACKNOWLEDGEMENT = (
    OutboundPacket.State.QUEUED_ON_NODE,
    OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT,
)


@dataclass(frozen=True, kw_only=True)
class PacketQueuedOnNode:
    """MSG_SENT: the node queued the packet (not yet transmitted); the expected code is lower-case hex."""

    route: OutboundPacket.Route
    expected_acknowledgement_code: str
    suggested_timeout_milliseconds: int


@dataclass(frozen=True, kw_only=True)
class PacketRejectedByNode:
    node_error_code: int


@dataclass(frozen=True, kw_only=True)
class PacketOutcomeUnknown:
    """The command timed out: the node may have queued the packet."""


type NodeSendOutcome = PacketQueuedOnNode | PacketRejectedByNode | PacketOutcomeUnknown


@dataclass(frozen=True, kw_only=True)
class RecordedSendOutcome:
    packet_id: int
    contact_id: int | None
    purpose: OutboundPacket.Purpose
    packet_state: OutboundPacket.State
    route: str
    expected_acknowledgement_code: str
    queued_at: datetime | None
    # Set while the packet awaits its firmware acknowledgement.
    acknowledgement_deadline_at: datetime | None
    # ERR_CODE_TABLE_FULL: the whole sender backs off; the part stays in its round and no attempt is spent.
    node_packet_pool_full: bool = False
    # ERR_CODE_NOT_FOUND: the node does not hold the contact, which is pending_add again until reconciled.
    contact_needs_reconciliation: bool = False

    @property
    def awaits_acknowledgement(self) -> bool:
        return self.packet_state == OutboundPacket.State.QUEUED_ON_NODE


@dataclass(frozen=True, kw_only=True)
class AcknowledgedPacket:
    packet_id: int
    contact_id: int | None
    # False for a late acknowledgement of a packet whose deadline had passed.
    was_awaiting_acknowledgement: bool


@dataclass(frozen=True, kw_only=True)
class AcknowledgementTimeout:
    packet_id: int
    contact_id: int | None
    purpose: OutboundPacket.Purpose
    # The packet went direct and nothing explains the silence yet: settle it before the next packet to the contact.
    route_reset_pending: bool


@dataclass(frozen=True, kw_only=True)
class OutboundPacketRecovery:
    prepared_packets_now_unknown: int
    queued_packets_dropped: int
    route_resets_dropped: int


def record_send_outcome(packet_id: int, outcome: NodeSendOutcome, now: datetime) -> RecordedSendOutcome | None:
    """Record the node's answer to a send, and count the part or receipt as sent when it may have gone out.

    Queued, an unknown outcome, and any rejection other than "pool full" or "not found" count as
    sent. A "pool full" or "not found" part stays in its round and is sent again later as a new
    packet. Returns None for a packet that does not exist; a packet whose outcome was already
    recorded is left alone.
    """
    return run_in_service_transaction(lambda: record_send_outcome_in_transaction(packet_id, outcome, now))


def record_send_outcome_in_transaction(
    packet_id: int,
    outcome: NodeSendOutcome,
    now: datetime,
) -> RecordedSendOutcome | None:
    packet = OutboundPacket.objects.filter(id=packet_id).first()
    if packet is None:
        return None
    if packet.state != OutboundPacket.State.PREPARED:
        return describe_recorded_outcome(packet)

    is_contact_missing_on_node = (
        isinstance(outcome, PacketRejectedByNode) and outcome.node_error_code == ERR_CODE_NOT_FOUND
    )
    if is_contact_missing_on_node and packet.contact_id is not None:
        mark_contact_missing_on_node(packet.contact_id)
    if counts_as_sent(outcome):
        suggested_timeout = outcome.suggested_timeout_milliseconds if isinstance(outcome, PacketQueuedOnNode) else None
        count_packet_as_sent(packet, suggested_timeout, now)

    packet_changes = build_packet_outcome_changes(outcome, now)
    OutboundPacket.objects.filter(id=packet_id, state=OutboundPacket.State.PREPARED).update(**packet_changes)
    packet.refresh_from_db()

    is_node_packet_pool_full = (
        isinstance(outcome, PacketRejectedByNode) and outcome.node_error_code == ERR_CODE_TABLE_FULL
    )
    return replace(
        describe_recorded_outcome(packet),
        node_packet_pool_full=is_node_packet_pool_full,
        contact_needs_reconciliation=is_contact_missing_on_node,
    )


def counts_as_sent(outcome: NodeSendOutcome) -> bool:
    if isinstance(outcome, PacketRejectedByNode):
        return outcome.node_error_code not in (ERR_CODE_NOT_FOUND, ERR_CODE_TABLE_FULL)
    return True


def mark_contact_missing_on_node(contact_id: int) -> None:
    """The contact is locked before the delivery or receipt and the packet, as the lock order wants."""
    Contact.objects.filter(id=contact_id).update(
        node_sync_state=Contact.NodeSyncState.PENDING_ADD,
        node_sync_error="The node did not know this contact when a message was sent to it.",
    )


def count_packet_as_sent(packet: OutboundPacket, suggested_timeout_milliseconds: int | None, now: datetime) -> None:
    if packet.arm_generation is None or packet.attempt_number is None:
        return
    if packet.message_delivery_id is not None and packet.part_number is not None:
        delivery = (
            MessageDelivery.objects.select_for_update()
            .filter(
                id=packet.message_delivery_id,
                state=MessageDelivery.State.PENDING,
                arm_generation=packet.arm_generation,
                attempt_count=packet.attempt_number,
            )
            .first()
        )
        if delivery is not None:
            record_delivery_part_sent(delivery, packet.part_number, suggested_timeout_milliseconds, now)
    elif packet.receipt_notification_id is not None:
        receipt = (
            ReceiptNotification.objects.select_for_update()
            .filter(
                id=packet.receipt_notification_id,
                state=ReceiptNotification.State.PENDING,
                arm_generation=packet.arm_generation,
                attempt_count=packet.attempt_number,
                round_pending=True,
            )
            .first()
        )
        if receipt is not None:
            record_receipt_packet_sent(receipt, suggested_timeout_milliseconds, now)


def build_packet_outcome_changes(outcome: NodeSendOutcome, now: datetime) -> dict[str, object]:
    match outcome:
        case PacketQueuedOnNode():
            return {
                "state": OutboundPacket.State.QUEUED_ON_NODE,
                "route": outcome.route,
                "expected_acknowledgement_code": outcome.expected_acknowledgement_code.lower(),
                "suggested_timeout_milliseconds": outcome.suggested_timeout_milliseconds,
                "queued_at": now,
                "acknowledgement_deadline_at": now
                + calculate_acknowledgement_wait(outcome.suggested_timeout_milliseconds),
            }
        case PacketRejectedByNode():
            return {"state": OutboundPacket.State.REJECTED_BY_NODE, "node_error_code": outcome.node_error_code}
        case PacketOutcomeUnknown():
            return {"state": OutboundPacket.State.OUTCOME_UNKNOWN}


def describe_recorded_outcome(packet: OutboundPacket) -> RecordedSendOutcome:
    return RecordedSendOutcome(
        packet_id=packet.pk,
        contact_id=packet.contact_id,
        purpose=OutboundPacket.Purpose(packet.purpose),
        packet_state=OutboundPacket.State(packet.state),
        route=packet.route,
        expected_acknowledgement_code=packet.expected_acknowledgement_code,
        queued_at=packet.queued_at,
        acknowledgement_deadline_at=(
            packet.acknowledgement_deadline_at if packet.state == OutboundPacket.State.QUEUED_ON_NODE else None
        ),
    )


def record_node_acknowledgement(
    acknowledgement_code: str,
    round_trip_milliseconds: int | None,
    now: datetime,
) -> tuple[AcknowledgedPacket, ...]:
    """Match a firmware ACK push through the database, also after its deadline, and mark every match acknowledged.

    Several matches are a 32-bit code collision; all are marked. A pending route reset of a
    matched packet is settled as skipped: the route works. An empty result means no packet
    matched yet: the worker keeps the code for a minute and calls this again after it recorded
    the next MSG_SENT, since a fast zero-hop acknowledgement can arrive before it.
    """
    return run_in_service_transaction(
        lambda: record_node_acknowledgement_in_transaction(acknowledgement_code.lower(), round_trip_milliseconds, now)
    )


def record_node_acknowledgement_in_transaction(
    acknowledgement_code: str,
    round_trip_milliseconds: int | None,
    now: datetime,
) -> tuple[AcknowledgedPacket, ...]:
    matching_packets = list(
        OutboundPacket.objects.select_for_update()
        .filter(
            expected_acknowledgement_code=acknowledgement_code,
            state__in=PACKET_STATES_AWAITING_ACKNOWLEDGEMENT,
            queued_at__gte=now - timedelta(minutes=LATE_ACKNOWLEDGEMENT_MATCH_MINUTES),
        )
        .order_by("id")
    )
    acknowledged_packets: list[AcknowledgedPacket] = []
    for packet in matching_packets:
        packet_changes: dict[str, object] = {
            "state": OutboundPacket.State.NODE_ACKNOWLEDGED,
            "acknowledged_at": now,
            "round_trip_milliseconds": round_trip_milliseconds,
        }
        if packet.route_reset_state == OutboundPacket.RouteResetState.PENDING:
            packet_changes["route_reset_state"] = OutboundPacket.RouteResetState.SKIPPED_LATE_ACKNOWLEDGEMENT
            packet_changes["route_reset_decided_at"] = now
        OutboundPacket.objects.filter(id=packet.pk, state=packet.state).update(**packet_changes)
        acknowledged_packets.append(
            AcknowledgedPacket(
                packet_id=packet.pk,
                contact_id=packet.contact_id,
                was_awaiting_acknowledgement=packet.state == OutboundPacket.State.QUEUED_ON_NODE,
            )
        )
    return tuple(acknowledged_packets)


def record_acknowledgement_deadline_passed(packet_id: int, now: datetime) -> AcknowledgementTimeout | None:
    """The packet's firmware acknowledgement did not come in time; None when it came meanwhile.

    A direct packet then needs a route reset unless the node learned a new route to the contact
    after the packet was queued; the reset itself is decided right before the next packet to
    the contact (messaging.route_reset_evidence.settle_pending_route_resets).
    """
    return run_in_service_transaction(lambda: record_acknowledgement_deadline_passed_in_transaction(packet_id, now))


def record_acknowledgement_deadline_passed_in_transaction(
    packet_id: int, now: datetime
) -> AcknowledgementTimeout | None:
    packet = (
        OutboundPacket.objects.select_for_update()
        .filter(id=packet_id, state=OutboundPacket.State.QUEUED_ON_NODE)
        .first()
    )
    if packet is None:
        return None

    last_path_update_at = (
        Contact.objects.filter(id=packet.contact_id).values_list("last_path_update_at", flat=True).first()
        if packet.contact_id is not None
        else None
    )
    route_reset_state = decide_route_reset_at_deadline(packet.route, packet.queued_at, last_path_update_at)
    is_route_reset_decided = route_reset_state == OutboundPacket.RouteResetState.SKIPPED_PATH_UPDATE
    OutboundPacket.objects.filter(id=packet_id, state=OutboundPacket.State.QUEUED_ON_NODE).update(
        state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT,
        route_reset_state=route_reset_state,
        route_reset_decided_at=now if is_route_reset_decided else None,
    )
    return AcknowledgementTimeout(
        packet_id=packet.pk,
        contact_id=packet.contact_id,
        purpose=OutboundPacket.Purpose(packet.purpose),
        route_reset_pending=route_reset_state == OutboundPacket.RouteResetState.PENDING,
    )


def mark_packets_awaiting_acknowledgement_dropped(now: datetime) -> int:
    """At teardown and start-up: the ACK push may have been lost with the link, so no route decision is taken.

    The packets keep their deadline, which still holds back contact removals, and a late
    acknowledgement still matches them.
    """
    return run_in_service_transaction(lambda: drop_packets_awaiting_acknowledgement(now))


def drop_packets_awaiting_acknowledgement(now: datetime) -> int:
    return OutboundPacket.objects.filter(state=OutboundPacket.State.QUEUED_ON_NODE).update(
        state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT,
        route_reset_state=OutboundPacket.RouteResetState.DROPPED_BY_RESTART,
        route_reset_decided_at=now,
    )


def drop_pending_route_resets(now: datetime) -> int:
    """Route decisions that were waiting for the next packet to their contact, lost with the worker's memory."""
    return OutboundPacket.objects.filter(route_reset_state=OutboundPacket.RouteResetState.PENDING).update(
        route_reset_state=OutboundPacket.RouteResetState.DROPPED_BY_RESTART,
        route_reset_decided_at=now,
    )


def mark_prepared_packets_outcome_unknown() -> int:
    """The worker died between preparing and recording: the part is still in its round and is sent again."""
    return OutboundPacket.objects.filter(state=OutboundPacket.State.PREPARED).update(
        state=OutboundPacket.State.OUTCOME_UNKNOWN
    )


def recover_outbound_packets_at_startup(now: datetime) -> OutboundPacketRecovery:
    """Start-up recovery of the packet table, before the node is opened."""

    def recover_in_transaction() -> OutboundPacketRecovery:
        return OutboundPacketRecovery(
            prepared_packets_now_unknown=mark_prepared_packets_outcome_unknown(),
            queued_packets_dropped=drop_packets_awaiting_acknowledgement(now),
            route_resets_dropped=drop_pending_route_resets(now),
        )

    return run_in_service_transaction(recover_in_transaction)


def count_pending_route_resets() -> int:
    """Timed-out direct packets whose route decision waits for the next packet to their contact.

    A packet whose contact was deleted never gets a next packet, so it is left out.
    """
    return OutboundPacket.objects.filter(
        route_reset_state=OutboundPacket.RouteResetState.PENDING, contact__isnull=False
    ).count()


def select_packets_awaiting_node_acknowledgement(now: datetime) -> QuerySet[OutboundPacket]:
    """The packets the contact-removal guard waits for.

    Removing a contact shifts the node's contact table under its pending ACK entries. The guard
    reads the database, so packets sent before a worker restart count too.
    """
    return OutboundPacket.objects.filter(acknowledgement_deadline_at__gt=now, acknowledged_at__isnull=True)


def is_any_packet_awaiting_node_acknowledgement(now: datetime) -> bool:
    return select_packets_awaiting_node_acknowledgement(now).exists()


def read_latest_acknowledgement_deadline(now: datetime) -> datetime | None:
    """The last moment one of the packets the contact-removal guard waits for may still be acknowledged."""
    deadline_summary = select_packets_awaiting_node_acknowledgement(now).aggregate(
        latest_deadline=Max("acknowledgement_deadline_at")
    )
    latest_deadline: datetime | None = deadline_summary["latest_deadline"]
    return latest_deadline
