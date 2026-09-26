"""The Messages debug views: messages with their deliveries, the merged traffic log and refresh sessions."""

from dataclasses import dataclass
from typing import Any

from django.db.models import Prefetch, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from directory.models import Contact
from messaging.models import Message, MessageDelivery, OutboundPacket, ReceiptNotification, RefreshSession
from panel.forms import MessageFilterForm, MessageStatusFilter, TrafficDirectionFilter, TrafficFilterForm
from panel.htmx_request import HtmxHttpRequest
from panel.presenters import find_badge_tone
from panel.traffic_log import TrafficCursor, TrafficDirection, TrafficFilter, read_traffic_page

MESSAGES_TEMPLATE = "panel/messages.html"
TRAFFIC_TEMPLATE = "panel/traffic.html"
REFRESH_SESSIONS_TEMPLATE = "panel/refresh_sessions.html"
PAGE_SIZE = 50
MESSAGE_TABLE_TARGET = "messages-table"
TRAFFIC_ROWS_TARGET = "traffic-rows"


@dataclass(frozen=True, kw_only=True)
class DeliveryChip:
    """One device's delivery of a message in a few words: "B1 delivered", "B2 failed 6/6"."""

    device_label: str
    device_description: str
    text: str
    tone: str


@dataclass(frozen=True, kw_only=True)
class MessageRow:
    message: Message
    delivery_chips: list[DeliveryChip]
    received_part_count: int


def label_devices_of_message(message: Message, deliveries: list[MessageDelivery]) -> dict[int, str]:
    """B1, B2, … by device id: the recipient's initial and the device's place among the message's deliveries."""
    initial = message.recipient.username[:1].upper()
    device_ids = sorted({delivery.device_id for delivery in deliveries})
    return {device_id: f"{initial}{position}" for position, device_id in enumerate(device_ids, start=1)}


def describe_delivery(delivery: MessageDelivery) -> str:
    attempts = f"{delivery.attempt_count}/{delivery.maximum_attempts}"
    match delivery.state:
        case MessageDelivery.State.DELIVERED:
            return "read" if delivery.read_at else "delivered"
        case MessageDelivery.State.FAILED:
            return f"failed {attempts}"
        case MessageDelivery.State.QUEUED_FOR_REFRESH:
            return f"refresh #{delivery.refresh_session_id} queued"
        case MessageDelivery.State.CANCELLED:
            return "cancelled"
        case _ if delivery.refresh_session_id is not None:
            return f"refresh #{delivery.refresh_session_id} head {attempts}"
        case _:
            return f"pending {attempts}"


def build_message_row(message: Message) -> MessageRow:
    deliveries = list(message.deliveries.all())
    device_labels = label_devices_of_message(message, deliveries)
    return MessageRow(
        message=message,
        delivery_chips=[
            DeliveryChip(
                device_label=device_labels[delivery.device_id],
                device_description=str(delivery.device),
                text=describe_delivery(delivery),
                tone=find_badge_tone(delivery.state),
            )
            for delivery in sorted(deliveries, key=lambda delivery: delivery.device_id)
        ],
        received_part_count=sum(1 for part_text in message.part_texts if part_text is not None),
    )


def filter_messages(message_filter: dict[str, Any]) -> QuerySet[Message]:
    messages = Message.objects.select_related("sender", "recipient").prefetch_related(
        Prefetch("deliveries", queryset=MessageDelivery.objects.select_related("device"))
    )
    username = (message_filter.get("user") or "").strip().lower()
    if username:
        messages = messages.filter(
            Q(sender__username_lookup__contains=username) | Q(recipient__username_lookup__contains=username)
        )
    match message_filter.get("status") or MessageStatusFilter.ALL:
        case MessageStatusFilter.INCOMPLETE:
            messages = messages.filter(accepted_at__isnull=True)
        case MessageStatusFilter.UNDELIVERED:
            messages = messages.filter(accepted_at__isnull=False, delivered_at__isnull=True)
        case MessageStatusFilter.DELIVERED:
            messages = messages.filter(delivered_at__isnull=False, read_at__isnull=True)
        case MessageStatusFilter.READ:
            messages = messages.filter(read_at__isnull=False)
        case MessageStatusFilter.WITH_FAILED_DELIVERY:
            messages = messages.filter(deliveries__state=MessageDelivery.State.FAILED).distinct()
    if message_filter.get("before"):
        messages = messages.filter(id__lt=message_filter["before"])
    return messages.order_by("-id")


def build_message_table_context(request: HttpRequest) -> dict[str, Any]:
    message_filter_form = MessageFilterForm(request.GET)
    message_filter = message_filter_form.cleaned_data if message_filter_form.is_valid() else {}
    page_messages = list(filter_messages(message_filter)[: PAGE_SIZE + 1])
    has_older_messages = len(page_messages) > PAGE_SIZE
    shown_messages = page_messages[:PAGE_SIZE]
    return {
        "message_filter_form": message_filter_form,
        "message_rows": [build_message_row(message) for message in shown_messages],
        "is_live": bool(message_filter.get("live")),
        "older_messages_before": shown_messages[-1].pk if has_older_messages else None,
        "is_first_page": not message_filter.get("before"),
        "filter_query": build_query_without(request, "before"),
        "table_query": request.GET.urlencode(),
    }


def build_query_without(request: HttpRequest, parameter_name: str) -> str:
    query = request.GET.copy()
    query.pop(parameter_name, None)
    return query.urlencode()


@require_GET
def show_messages(request: HtmxHttpRequest) -> HttpResponse:
    context = build_message_table_context(request)
    if request.htmx and request.htmx.target == MESSAGE_TABLE_TARGET:
        return render(request, f"{MESSAGES_TEMPLATE}#message_table", context)
    return render(request, MESSAGES_TEMPLATE, context)


@require_GET
def show_message_table(request: HttpRequest) -> HttpResponse:
    return render(request, f"{MESSAGES_TEMPLATE}#message_table", build_message_table_context(request))


@dataclass(frozen=True, kw_only=True)
class LabelledDelivery:
    device_label: str
    delivery: MessageDelivery


@dataclass(frozen=True, kw_only=True)
class DisplayedPart:
    part_number: int
    text: str | None

    @property
    def byte_count(self) -> int:
        return len(self.text.encode("utf-8")) if self.text is not None else 0


@require_GET
def show_message_details(request: HttpRequest, message_id: int) -> HttpResponse:
    message = get_object_or_404(Message.objects.select_related("sender", "recipient", "sender_device"), id=message_id)
    deliveries = list(message.deliveries.select_related("device").order_by("device_id"))
    device_labels = label_devices_of_message(message, deliveries)
    receipts = list(ReceiptNotification.objects.filter(message=message).select_related("device").order_by("device_id"))
    outbound_packets = OutboundPacket.objects.filter(
        Q(message_delivery__message=message) | Q(receipt_notification__message=message)
    ).order_by("prepared_at", "id")
    return render(
        request,
        f"{MESSAGES_TEMPLATE}#message_details",
        {
            "message": message,
            "parts": [
                DisplayedPart(part_number=part_number, text=part_text)
                for part_number, part_text in enumerate(message.part_texts, start=1)
            ],
            "labelled_deliveries": [
                LabelledDelivery(device_label=device_labels[delivery.device_id], delivery=delivery)
                for delivery in deliveries
            ],
            "receipts": receipts,
            "outbound_packets": outbound_packets,
        },
    )


def list_contact_choices() -> list[tuple[int, str]]:
    return [(contact.pk, str(contact)) for contact in Contact.objects.order_by("name", "id")]


TRAFFIC_DIRECTIONS_BY_CHOICE: dict[str, TrafficDirection] = {
    TrafficDirectionFilter.INBOUND: TrafficDirection.INBOUND,
    TrafficDirectionFilter.OUTBOUND: TrafficDirection.OUTBOUND,
}


def build_traffic_context(request: HttpRequest) -> dict[str, Any]:
    traffic_filter_form = TrafficFilterForm(request.GET, contact_choices=list_contact_choices())
    submitted_filter = traffic_filter_form.cleaned_data if traffic_filter_form.is_valid() else {}
    traffic_filter = TrafficFilter(
        contact_id=submitted_filter.get("contact"),
        direction=TRAFFIC_DIRECTIONS_BY_CHOICE.get(submitted_filter.get("direction") or ""),
        classification=submitted_filter.get("classification") or "",
        only_problems=bool(submitted_filter.get("only_problems")),
    )
    before = TrafficCursor.decode(submitted_filter.get("before") or "")
    traffic_page = read_traffic_page(traffic_filter, before)
    return {
        "traffic_filter_form": traffic_filter_form,
        "traffic_rows": traffic_page.rows,
        "older_rows_before": traffic_page.next_cursor.encode() if traffic_page.next_cursor else None,
        "is_first_page": before is None,
        "is_live": bool(submitted_filter.get("live")),
        "filter_query": build_query_without(request, "before"),
        "rows_query": request.GET.urlencode(),
    }


@require_GET
def show_traffic(request: HtmxHttpRequest) -> HttpResponse:
    context = build_traffic_context(request)
    if request.htmx and request.htmx.target == TRAFFIC_ROWS_TARGET:
        return render(request, f"{TRAFFIC_TEMPLATE}#traffic_rows", context)
    return render(request, TRAFFIC_TEMPLATE, context)


@require_GET
def show_traffic_rows(request: HttpRequest) -> HttpResponse:
    return render(request, f"{TRAFFIC_TEMPLATE}#traffic_rows", build_traffic_context(request))


@require_GET
def show_refresh_sessions(request: HttpRequest) -> HttpResponse:
    refresh_sessions = RefreshSession.objects.select_related("device__user", "peer").prefetch_related(
        Prefetch(
            "deliveries",
            queryset=MessageDelivery.objects.select_related("message").order_by("message__accepted_at", "message_id"),
        )
    )
    before_text = request.GET.get("before", "")
    if before_text.isdigit():
        refresh_sessions = refresh_sessions.filter(id__lt=int(before_text))
    page_sessions = list(refresh_sessions.order_by("-id")[: PAGE_SIZE + 1])
    shown_sessions = page_sessions[:PAGE_SIZE]
    return render(
        request,
        REFRESH_SESSIONS_TEMPLATE,
        {
            "refresh_sessions": shown_sessions,
            "older_sessions_before": shown_sessions[-1].pk if len(page_sessions) > PAGE_SIZE else None,
            "is_first_page": not before_text,
        },
    )
