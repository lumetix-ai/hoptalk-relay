"""Route resets (CMD_RESET_PATH) decided from evidence in the database.

MeshCore never falls back to flooding by itself when a stored route stops working, so the
relay resets the route to a contact (the next DM floods and the reply teaches both nodes a new
route) in two cases: a new DM from the contact arrived by flood, and a direct packet to the
contact got no firmware acknowledgement in time. On an asymmetric link only the acknowledgement
is lost, and a reset would turn every later DM into a flood for nothing. So a timed-out packet
is settled on its own right before the next packet to its contact: no reset when its
acknowledgement came late, when the node learned a new route after it was queued, or when the
device proved in the protocol that the DM arrived.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from directory.models import Contact
from hoptalk_relay.relay_settings import get_relay_settings
from messaging.models import InboundDirectMessage, MessageDelivery, OutboundPacket, ReceiptNotification
from messaging.reply_keys import build_reply_key_for_direct_message
from messaging.service_transactions import run_in_service_transaction
from protocol.parsing import parse_direct_message_text

# The requests that may prove a reply arrived: a handful is enough, the client sends few.
REPLY_EVIDENCE_REQUEST_LIMIT = 20


@dataclass(frozen=True, kw_only=True)
class ReplyResendCandidate:
    """A reply whose route was reset: sent once more, as a new packet that floods, if the reply queue agrees."""

    packet_id: int
    contact_id: int
    reply_key: str
    text: str


@dataclass(frozen=True, kw_only=True)
class RouteResetSettlement:
    """The pending route resets of one contact, settled right before its next packet.

    When packet_ids_needing_reset is not empty, the worker sends reset_path once and then
    calls record_route_reset_performed with these ids.
    """

    contact_id: int
    packet_ids_needing_reset: tuple[int, ...]
    replies_to_resend: tuple[ReplyResendCandidate, ...]

    @property
    def needs_reset(self) -> bool:
        return bool(self.packet_ids_needing_reset)


def record_path_update(public_key: str, now: datetime) -> int | None:
    """A PATH_UPDATE push: the node learned a new route to the contact. Returns the contact's id, if known."""
    updated_row_count = Contact.objects.filter(public_key=public_key).update(last_path_update_at=now)
    if updated_row_count == 0:
        return None
    return Contact.objects.filter(public_key=public_key).values_list("id", flat=True).first()


def is_path_update_recent(last_path_update_at: datetime | None, now: datetime) -> bool:
    if last_path_update_at is None:
        return False
    recent_path_update_seconds = get_relay_settings().engine_timing.recent_path_update_seconds
    return now - last_path_update_at <= timedelta(seconds=recent_path_update_seconds)


def needs_flood_arrival_route_reset(
    *,
    arrived_by_flood: bool,
    received_at: datetime,
    last_path_update_at: datetime | None,
    now: datetime,
) -> bool:
    """A new DM that arrived by flood means the node's stored route to the sender may be stale.

    The reset is sent even when no route may be stored: it costs one serial round trip and no
    airtime.
    """
    if not arrived_by_flood or is_path_update_recent(last_path_update_at, now):
        return False
    maximum_age_seconds = get_relay_settings().engine_timing.flood_arrival_reset_maximum_age_seconds
    return now - received_at <= timedelta(seconds=maximum_age_seconds)


def decide_route_reset_at_deadline(
    route: str,
    queued_at: datetime | None,
    last_path_update_at: datetime | None,
) -> OutboundPacket.RouteResetState:
    """A flooded packet needs no reset; a direct one does, unless the route changed after it was queued."""
    if route != OutboundPacket.Route.DIRECT:
        return OutboundPacket.RouteResetState.NOT_APPLICABLE
    if has_path_update_after(last_path_update_at, queued_at):
        return OutboundPacket.RouteResetState.SKIPPED_PATH_UPDATE
    return OutboundPacket.RouteResetState.PENDING


def has_path_update_after(last_path_update_at: datetime | None, queued_at: datetime | None) -> bool:
    return last_path_update_at is not None and queued_at is not None and last_path_update_at > queued_at


def settle_pending_route_resets(contact_id: int, now: datetime) -> RouteResetSettlement:
    """Decide every pending route reset of the contact's timed-out packets, each on its own evidence."""
    return run_in_service_transaction(lambda: settle_pending_route_resets_in_transaction(contact_id, now))


def settle_pending_route_resets_in_transaction(contact_id: int, now: datetime) -> RouteResetSettlement:
    last_path_update_at = Contact.objects.filter(id=contact_id).values_list("last_path_update_at", flat=True).first()
    pending_packets = list(
        OutboundPacket.objects.select_for_update()
        .filter(contact_id=contact_id, route_reset_state=OutboundPacket.RouteResetState.PENDING)
        .order_by("id")
    )

    packets_needing_reset: list[OutboundPacket] = []
    for packet in pending_packets:
        if has_path_update_after(last_path_update_at, packet.queued_at):
            settle_route_reset(packet.pk, OutboundPacket.RouteResetState.SKIPPED_PATH_UPDATE, now)
        elif has_application_evidence(packet):
            settle_route_reset(packet.pk, OutboundPacket.RouteResetState.SKIPPED_APPLICATION_EVIDENCE, now)
        else:
            packets_needing_reset.append(packet)

    return RouteResetSettlement(
        contact_id=contact_id,
        packet_ids_needing_reset=tuple(packet.pk for packet in packets_needing_reset),
        replies_to_resend=tuple(find_replies_to_resend(packets_needing_reset, now)),
    )


def settle_route_reset(packet_id: int, route_reset_state: OutboundPacket.RouteResetState, now: datetime) -> bool:
    return (
        OutboundPacket.objects.filter(id=packet_id, route_reset_state=OutboundPacket.RouteResetState.PENDING).update(
            route_reset_state=route_reset_state,
            route_reset_decided_at=now,
        )
        == 1
    )


def record_route_reset_performed(packet_ids: Iterable[int], now: datetime) -> int:
    """The worker sent reset_path for these packets' contact; the next packet to it floods."""
    return OutboundPacket.objects.filter(
        id__in=list(packet_ids),
        route_reset_state=OutboundPacket.RouteResetState.PENDING,
    ).update(route_reset_state=OutboundPacket.RouteResetState.PERFORMED, route_reset_decided_at=now)


def has_application_evidence(packet: OutboundPacket) -> bool:
    """The device showed in the protocol that this DM arrived after it was queued; only the acknowledgement was lost."""
    if packet.queued_at is None:
        return False
    match packet.purpose:
        case OutboundPacket.Purpose.DELIVERY:
            return has_delivery_evidence(packet)
        case OutboundPacket.Purpose.RECEIPT:
            return has_receipt_evidence(packet)
        case _:
            return has_reply_evidence(packet)


def has_delivery_evidence(packet: OutboundPacket) -> bool:
    """A status received after the send that reports the packet's part."""
    if packet.message_delivery_id is None or packet.part_number is None:
        return False
    parts_received_mask = (
        MessageDelivery.objects.filter(
            id=packet.message_delivery_id,
            last_acknowledgement_received_at__gt=packet.queued_at,
        )
        .values_list("parts_received_mask", flat=True)
        .first()
    )
    return parts_received_mask is not None and bool(parts_received_mask & (1 << (packet.part_number - 1)))


def has_receipt_evidence(packet: OutboundPacket) -> bool:
    """A confirmation received after the send, of at least the level the packet carried."""
    if packet.receipt_notification_id is None or packet.receipt_level is None:
        return False
    return ReceiptNotification.objects.filter(
        id=packet.receipt_notification_id,
        last_confirmation_received_at__gt=packet.queued_at,
        confirmed_level__gte=packet.receipt_level,
    ).exists()


def has_reply_evidence(packet: OutboundPacket) -> bool:
    """A later request of another kind from the device: the client moved on, so it got the answer."""
    if packet.contact_id is None:
        return False
    later_requests = (
        InboundDirectMessage.objects.filter(contact_id=packet.contact_id, received_at__gt=packet.queued_at)
        .order_by("id")
        .values_list("id", "text")[:REPLY_EVIDENCE_REQUEST_LIMIT]
    )
    for inbox_row_id, stored_text in later_requests:
        reply_key = build_reply_key_for_direct_message(parse_direct_message_text(stored_text), inbox_row_id)
        if reply_key is not None and reply_key != packet.reply_key:
            return True
    return False


def find_replies_to_resend(packets_needing_reset: list[OutboundPacket], now: datetime) -> list[ReplyResendCandidate]:
    """A reply is sent again only while it is the newest one for its request and still young."""
    maximum_resend_age = timedelta(seconds=get_relay_settings().engine_timing.reply_resend_maximum_age_seconds)
    replies_to_resend: list[ReplyResendCandidate] = []
    for packet in packets_needing_reset:
        if packet.purpose != OutboundPacket.Purpose.REPLY or packet.contact_id is None or packet.prepared_at is None:
            continue
        if now - packet.prepared_at >= maximum_resend_age:
            continue
        has_newer_reply = OutboundPacket.objects.filter(
            contact_id=packet.contact_id,
            purpose=OutboundPacket.Purpose.REPLY,
            reply_key=packet.reply_key,
            id__gt=packet.pk,
        ).exists()
        if not has_newer_reply:
            replies_to_resend.append(
                ReplyResendCandidate(
                    packet_id=packet.pk,
                    contact_id=packet.contact_id,
                    reply_key=packet.reply_key,
                    text=packet.text,
                )
            )
    return replies_to_resend
