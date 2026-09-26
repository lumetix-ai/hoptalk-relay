import re
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket
from tests.panel.panel_client import get_page, get_partial
from tests.services.directory.row_builders import (
    create_accepted_message,
    create_contact,
    create_delivery,
    create_inbound_direct_message,
    create_outbound_packet,
    create_pending_receipt,
    create_refresh_session,
    create_user,
)

pytestmark = pytest.mark.django_db

MESSAGE_ID = 1790294400123457


def create_conversation() -> Message:
    """ivan sent bob a two-part message; bob has two devices, one delivered and one failed."""
    ivan, bob = create_user("ivan"), create_user("bob")
    ivans_device = create_contact(1, user=ivan, name="Ivan's tracker")
    bobs_first_device = create_contact(2, user=bob, name="Bob one")
    bobs_second_device = create_contact(3, user=bob, name="Bob two")
    message = Message.objects.create(
        sender=ivan,
        sender_device=ivans_device,
        recipient=bob,
        client_message_id=MESSAGE_ID,
        part_count=2,
        part_texts=["Привет, ", "Боб!"],
        text="Привет, Боб!",
        created_at=timezone.now(),
        last_part_at=timezone.now(),
        accepted_at=timezone.now(),
        delivered_at=timezone.now(),
    )
    delivered = create_delivery(message, bobs_first_device, state=MessageDelivery.State.DELIVERED)
    MessageDelivery.objects.filter(id=delivered.pk).update(parts_received_mask=0b11)
    failed = create_delivery(message, bobs_second_device, state=MessageDelivery.State.FAILED)
    MessageDelivery.objects.filter(id=failed.pk).update(
        attempt_count=6, failed_at=timezone.now(), failure_reason="attempts_exhausted", parts_received_mask=0b01
    )
    create_pending_receipt(message, ivans_device)
    create_outbound_packet(
        bobs_first_device,
        f"HT1 m ivan {MESSAGE_ID} 1/2 Привет, ",
        sender_timestamp=1_790_000_010,
        purpose=OutboundPacket.Purpose.DELIVERY,
        message_delivery=delivered,
    )
    return message


def test_the_messages_tab_lists_messages_with_a_chip_per_device_delivery(signed_in_client: Client) -> None:
    create_conversation()

    page = get_page(signed_in_client, "/messages").content.decode()

    assert "ivan" in page
    assert "Привет, Боб!" in page
    assert str(MESSAGE_ID) in page
    assert "B1 delivered" in page
    assert "B2 failed 6/6" in page
    assert "2 parts" in page
    assert 'hx-get="/messages/' in page
    assert 'hx-trigger="intersect once"' in page


def test_an_incomplete_message_shows_how_many_parts_arrived(signed_in_client: Client) -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    Message.objects.create(
        sender=ivan,
        recipient=bob,
        client_message_id=5,
        part_count=3,
        part_texts=["a", None, None],
        created_at=timezone.now(),
        last_part_at=timezone.now(),
    )

    assert "receiving 1/3 parts" in get_page(signed_in_client, "/messages").content.decode()


def test_the_details_show_parts_deliveries_receipts_and_packets(signed_in_client: Client) -> None:
    message = create_conversation()

    details = get_partial(signed_in_client, f"/messages/{message.pk}/partials/details").content.decode()

    assert "encodes 2026-09-25" in details
    assert "14 B" in details
    assert ">11<" in details
    assert ">10<" in details
    assert "attempts exhausted" in details
    assert "nothing" in details
    assert "0a1b2c3d" in details
    assert "1200 ms" in details


UNDELIVERED_MESSAGE_ID = 1790294400999999


@pytest.mark.parametrize(
    ("query", "expected_client_message_ids"),
    [
        pytest.param("user=IVAN", {MESSAGE_ID, UNDELIVERED_MESSAGE_ID}, id="by user, case-insensitively"),
        pytest.param("user=carol", set(), id="by a user with no messages"),
        pytest.param("status=failed", {MESSAGE_ID}, id="with a failed delivery"),
        pytest.param("status=undelivered", {UNDELIVERED_MESSAGE_ID}, id="not delivered yet"),
        pytest.param("status=delivered", {MESSAGE_ID}, id="delivered and not read"),
        pytest.param("status=read", set(), id="read"),
    ],
)
def test_the_filters_narrow_the_messages(
    signed_in_client: Client, query: str, expected_client_message_ids: set[int]
) -> None:
    message = create_conversation()
    create_accepted_message(message.sender, message.recipient, UNDELIVERED_MESSAGE_ID)

    table = get_partial(signed_in_client, f"/messages/partials/table?{query}").content.decode()

    shown_client_message_ids = {
        client_message_id
        for client_message_id in (MESSAGE_ID, UNDELIVERED_MESSAGE_ID)
        if f">{client_message_id}</span>" in table
    }
    assert shown_client_message_ids == expected_client_message_ids


def test_a_filter_change_from_htmx_returns_only_the_table(signed_in_client: Client) -> None:
    create_conversation()

    response = get_page(signed_in_client, "/messages?status=failed", htmx_target="messages-table")

    assert "<html" not in response.content.decode()
    assert response.content.decode().strip().startswith('<div id="messages-table"')


def test_auto_refresh_polls_the_table_only_while_it_is_on(signed_in_client: Client) -> None:
    create_conversation()

    assert 'hx-trigger="every 5s"' not in get_partial(signed_in_client, "/messages/partials/table").content.decode()
    live_table = get_partial(signed_in_client, "/messages/partials/table?live=on").content.decode()
    assert 'hx-trigger="every 5s"' in live_table
    assert 'hx-get="/messages/partials/table?live=on"' in live_table


def test_the_messages_are_paged_fifty_at_a_time_by_id(signed_in_client: Client) -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    for client_message_id in range(1, 56):
        create_accepted_message(ivan, bob, client_message_id)
    oldest_shown_id = Message.objects.order_by("-id")[49].pk

    first_page = get_page(signed_in_client, "/messages").content.decode()
    second_page = get_page(signed_in_client, f"/messages?before={oldest_shown_id}").content.decode()

    assert f"before={oldest_shown_id}" in first_page
    assert len(re.findall(r'id="message-details-', first_page)) == 50
    assert len(re.findall(r'id="message-details-', second_page)) == 5
    assert "Newest" in second_page


def test_the_traffic_tab_merges_both_directions_by_time_and_decodes_them(signed_in_client: Client) -> None:
    message = create_conversation()
    ivans_device = message.sender_device
    now = timezone.now()
    create_inbound_direct_message(
        ivans_device, f"HT1 M bob {MESSAGE_ID} 1/2 Привет, ", received_at=now - timedelta(seconds=30)
    )
    OutboundPacket.objects.filter(contact__isnull=False).update(prepared_at=now - timedelta(seconds=20))
    create_inbound_direct_message(
        None,
        "hello?",
        received_at=now - timedelta(seconds=10),
        classification=InboundDirectMessage.Classification.UNKNOWN_SENDER,
    )

    rows = get_partial(signed_in_client, "/messages/traffic/partials/rows").content.decode()

    assert rows.index("hello?") < rows.index("HT1 m ivan") < rows.index("HT1 M bob")
    assert rows.count("message part 1/2 of ivan → bob #…457") == 2
    assert "not protocol" in rows
    assert "firmware ACK after 1200 ms" in rows
    assert "direct" in rows
    assert "SNR 7.5 dB" in rows


def test_the_traffic_filters_and_the_problem_filter(signed_in_client: Client) -> None:
    message = create_conversation()
    create_inbound_direct_message(message.sender_device, "HT1 Q bob")
    create_inbound_direct_message(None, "spam", classification=InboundDirectMessage.Classification.NOT_PROTOCOL)

    only_problems = get_partial(signed_in_client, "/messages/traffic/partials/rows?only_problems=on").content.decode()
    only_sent = get_partial(signed_in_client, "/messages/traffic/partials/rows?direction=outbound").content.decode()
    by_classification = get_partial(
        signed_in_client, "/messages/traffic/partials/rows?classification=not_protocol"
    ).content.decode()

    assert "spam" in only_problems
    assert "HT1 Q bob" not in only_problems
    assert "HT1 m ivan" in only_sent
    assert "HT1 Q bob" not in only_sent
    assert "spam" in by_classification
    assert "HT1 m ivan" not in by_classification


def test_traffic_pages_continue_exactly_where_the_previous_one_ended(signed_in_client: Client) -> None:
    contact = create_contact(1)
    same_time = timezone.now()
    for row_number in range(30):
        create_inbound_direct_message(contact, f"HT1 Q in{row_number:02d}", received_at=same_time)
        create_outbound_packet(
            contact, f"HT1 q out{row_number:02d} 0", sender_timestamp=1_790_000_100 + row_number, prepared_at=same_time
        )

    first_page = get_page(signed_in_client, "/messages/traffic").content.decode()
    cursor = re.search(r"before=([^\"&]+)", first_page)
    assert cursor is not None
    second_page = get_partial(
        signed_in_client, f"/messages/traffic/partials/rows?before={cursor.group(1)}"
    ).content.decode()

    first_texts = re.findall(r"HT1 [Qq] (?:in|out)\d\d", first_page)
    second_texts = re.findall(r"HT1 [Qq] (?:in|out)\d\d", second_page)
    assert len(first_texts) == 50
    assert len(second_texts) == 10
    assert set(first_texts).isdisjoint(second_texts)
    assert len(set(first_texts) | set(second_texts)) == 60


def test_live_traffic_polls_every_three_seconds(signed_in_client: Client) -> None:
    live_rows = get_partial(signed_in_client, "/messages/traffic/partials/rows?live=on").content.decode()
    still_rows = get_partial(signed_in_client, "/messages/traffic/partials/rows").content.decode()

    assert 'hx-trigger="every 3s"' in live_rows
    assert 'hx-trigger="every 3s"' not in still_rows


def test_the_refresh_sessions_tab_shows_each_session_with_its_head_and_queue(signed_in_client: Client) -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    bobs_device = create_contact(2, user=bob, name="Bob one")
    refresh_session = create_refresh_session(bobs_device, peer=ivan)
    older_message = create_accepted_message(ivan, bob, 1111, accepted_at=timezone.now() - timedelta(minutes=2))
    newer_message = create_accepted_message(ivan, bob, 2222, accepted_at=timezone.now())
    create_delivery(older_message, bobs_device, refresh_session=refresh_session)
    create_delivery(
        newer_message, bobs_device, state=MessageDelivery.State.QUEUED_FOR_REFRESH, refresh_session=refresh_session
    )

    page = get_page(signed_in_client, "/messages/refresh-sessions").content.decode()

    assert "Bob one" in page
    assert "@ivan" in page
    assert "head #…111 0/6" in page
    assert page.index(">1111<") < page.index(">2222<")
