"""Every direct message in both directions, merged by time for the Traffic tab.

Inbound rows (inbound_direct_messages) and outbound packets (outbound_packets) are two tables.
A page takes the newest rows of each before the cursor, merges them and keeps PAGE_SIZE. Rows
are ordered by (time, direction, id), newest first, and the cursor is that triple of the last
row shown, so paging never skips or repeats a row even when times are equal.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from django.db.models import Q, QuerySet

from directory.models import Contact
from messaging.models import InboundDirectMessage, OutboundPacket
from panel.traffic_decoding import decode_direct_message_text

PAGE_SIZE = 50
CURSOR_SEPARATOR = "~"

PROBLEM_CLASSIFICATIONS = (
    InboundDirectMessage.Classification.NOT_PROTOCOL,
    InboundDirectMessage.Classification.SYNTAX_ERROR,
    InboundDirectMessage.Classification.UNSUPPORTED_VERSION,
    InboundDirectMessage.Classification.UNKNOWN_SENDER,
    InboundDirectMessage.Classification.UNSUPPORTED_TEXT_TYPE,
)
PROBLEM_PACKET_STATES = (
    OutboundPacket.State.REJECTED_BY_NODE,
    OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT,
    OutboundPacket.State.OUTCOME_UNKNOWN,
)


class TrafficDirection(IntEnum):
    """Also the tie-breaker between an inbound and an outbound row of the same time."""

    OUTBOUND = 0
    INBOUND = 1


@dataclass(frozen=True, kw_only=True)
class TrafficCursor:
    occurred_at: datetime
    direction: TrafficDirection
    row_id: int

    def encode(self) -> str:
        return CURSOR_SEPARATOR.join([self.occurred_at.isoformat(), str(self.direction.value), str(self.row_id)])

    @classmethod
    def decode(cls, encoded_cursor: str) -> TrafficCursor | None:
        """None for an empty or malformed cursor, which shows the newest page."""
        cursor_parts = encoded_cursor.split(CURSOR_SEPARATOR)
        if len(cursor_parts) != 3:
            return None
        try:
            return cls(
                occurred_at=datetime.fromisoformat(cursor_parts[0]),
                direction=TrafficDirection(int(cursor_parts[1])),
                row_id=int(cursor_parts[2]),
            )
        except ValueError:
            return None


@dataclass(frozen=True, kw_only=True)
class TrafficFilter:
    contact_id: int | None = None
    direction: TrafficDirection | None = None
    classification: str = ""
    only_problems: bool = False


@dataclass(frozen=True, kw_only=True)
class TrafficRow:
    direction: TrafficDirection
    occurred_at: datetime
    row_id: int
    contact_label: str
    contact_id: int | None
    text: str
    decoded_meaning: str
    inbound_direct_message: InboundDirectMessage | None
    outbound_packet: OutboundPacket | None

    @property
    def is_inbound(self) -> bool:
        return self.direction == TrafficDirection.INBOUND

    @property
    def cursor(self) -> TrafficCursor:
        return TrafficCursor(occurred_at=self.occurred_at, direction=self.direction, row_id=self.row_id)


@dataclass(frozen=True, kw_only=True)
class TrafficPage:
    rows: list[TrafficRow]
    next_cursor: TrafficCursor | None


def read_traffic_page(traffic_filter: TrafficFilter, before: TrafficCursor | None) -> TrafficPage:
    candidate_rows = [
        *read_inbound_rows(traffic_filter, before),
        *read_outbound_rows(traffic_filter, before),
    ]
    candidate_rows.sort(key=lambda row: (row.occurred_at, row.direction, row.row_id), reverse=True)
    page_rows = candidate_rows[:PAGE_SIZE]
    has_more_rows = len(candidate_rows) > PAGE_SIZE
    return TrafficPage(rows=page_rows, next_cursor=page_rows[-1].cursor if has_more_rows else None)


def build_before_cursor_condition(time_field: str, direction: TrafficDirection, before: TrafficCursor) -> Q:
    """Rows whose (time, direction, id) sorts below the cursor's."""
    earlier_time = Q(**{f"{time_field}__lt": before.occurred_at})
    same_time = Q(**{time_field: before.occurred_at})
    if direction < before.direction:
        return earlier_time | same_time
    if direction == before.direction:
        return earlier_time | (same_time & Q(id__lt=before.row_id))
    return earlier_time


def read_inbound_rows(traffic_filter: TrafficFilter, before: TrafficCursor | None) -> list[TrafficRow]:
    if traffic_filter.direction == TrafficDirection.OUTBOUND:
        return []

    inbound_messages: QuerySet[InboundDirectMessage] = InboundDirectMessage.objects.select_related("contact__user")
    if traffic_filter.contact_id is not None:
        inbound_messages = inbound_messages.filter(contact_id=traffic_filter.contact_id)
    if traffic_filter.classification:
        inbound_messages = inbound_messages.filter(classification=traffic_filter.classification)
    if traffic_filter.only_problems:
        inbound_messages = inbound_messages.filter(
            Q(classification__in=PROBLEM_CLASSIFICATIONS)
            | Q(processing_state=InboundDirectMessage.ProcessingState.FAILED)
        )
    if before is not None:
        inbound_messages = inbound_messages.filter(
            build_before_cursor_condition("received_at", TrafficDirection.INBOUND, before)
        )

    return [
        build_inbound_row(inbound_message)
        for inbound_message in inbound_messages.order_by("-received_at", "-id")[: PAGE_SIZE + 1]
    ]


def build_inbound_row(inbound_message: InboundDirectMessage) -> TrafficRow:
    return TrafficRow(
        direction=TrafficDirection.INBOUND,
        occurred_at=inbound_message.received_at,
        row_id=inbound_message.pk,
        contact_label=inbound_message.contact_label or inbound_message.sender_public_key_prefix,
        contact_id=inbound_message.contact_id,
        text=inbound_message.text,
        decoded_meaning=decode_direct_message_text(
            inbound_message.text, find_contact_username(inbound_message.contact)
        ),
        inbound_direct_message=inbound_message,
        outbound_packet=None,
    )


def read_outbound_rows(traffic_filter: TrafficFilter, before: TrafficCursor | None) -> list[TrafficRow]:
    # A classification belongs to received messages only.
    if traffic_filter.direction == TrafficDirection.INBOUND or traffic_filter.classification:
        return []

    outbound_packets: QuerySet[OutboundPacket] = OutboundPacket.objects.select_related("contact__user").filter(
        prepared_at__isnull=False
    )
    if traffic_filter.contact_id is not None:
        outbound_packets = outbound_packets.filter(contact_id=traffic_filter.contact_id)
    if traffic_filter.only_problems:
        outbound_packets = outbound_packets.filter(state__in=PROBLEM_PACKET_STATES)
    if before is not None:
        outbound_packets = outbound_packets.filter(
            build_before_cursor_condition("prepared_at", TrafficDirection.OUTBOUND, before)
        )

    return [
        build_outbound_row(outbound_packet, prepared_at=outbound_packet.prepared_at)
        for outbound_packet in outbound_packets.order_by("-prepared_at", "-id")[: PAGE_SIZE + 1]
        if outbound_packet.prepared_at is not None
    ]


def build_outbound_row(outbound_packet: OutboundPacket, prepared_at: datetime) -> TrafficRow:
    return TrafficRow(
        direction=TrafficDirection.OUTBOUND,
        occurred_at=prepared_at,
        row_id=outbound_packet.pk,
        contact_label=outbound_packet.contact_label,
        contact_id=outbound_packet.contact_id,
        text=outbound_packet.text,
        decoded_meaning=decode_direct_message_text(
            outbound_packet.text, find_contact_username(outbound_packet.contact)
        ),
        inbound_direct_message=None,
        outbound_packet=outbound_packet,
    )


def find_contact_username(contact: Contact | None) -> str:
    if contact is None or contact.user is None:
        return ""
    return contact.user.username
