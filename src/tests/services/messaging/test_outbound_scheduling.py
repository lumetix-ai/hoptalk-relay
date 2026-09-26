import threading
from datetime import timedelta

import pytest
from django.db import transaction

from directory.models import Contact, User
from messaging.models import MessageDelivery, OutboundPacket, ReceiptNotification
from messaging.outbound_scheduling import (
    allocate_sender_timestamp,
    calculate_next_eligible_time,
    count_due_work,
    prepare_reply_packet,
)
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, create_device, create_user
from tests.services.node.database_threads import run_concurrently

State = MessageDelivery.State


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("bob", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


@pytest.fixture
def bobs_device(bob: User, manual_clock: ManualClock) -> Contact:
    return create_device(2, manual_clock.now(), user=bob)


def send_single_part(relay: RelayHarness, sender_device: Contact, recipient: str, message_id: int) -> None:
    relay.receive_replies(sender_device, f"HT1 M {recipient} {message_id} 1/1 message {message_id}")


def find_delivery(message_id: int, device: Contact) -> MessageDelivery:
    return MessageDelivery.objects.get(message__client_message_id=message_id, device=device)


@pytest.mark.django_db
def test_due_work_goes_by_next_attempt_time_with_deliveries_first_on_a_tie(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "bob", 1)
    relay.receive_replies(bobs_device, "HT1 R ivan 1")
    send_single_part(relay, ivans_device, "bob", 2)
    manual_clock.advance(seconds=1)
    send_single_part(relay, bobs_device, "ivan", 3)
    ReceiptNotification.objects.update(next_attempt_at=find_delivery(2, bobs_device).next_attempt_at)

    assert relay.send_due_texts() == ["HT1 m ivan 2 1/1 message 2", "HT1 s bob 1 R", "HT1 m bob 3 1/1 message 3"]


@pytest.mark.django_db
def test_a_device_has_at_most_three_deliveries_in_progress_and_the_fourth_waits_for_a_free_place(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    for message_id in (1, 2, 3, 4):
        send_single_part(relay, ivans_device, "bob", message_id)

    assert relay.send_due_texts(bobs_device) == [
        f"HT1 m ivan {message_id} 1/1 message {message_id}" for message_id in (1, 2, 3)
    ]
    waiting = find_delivery(4, bobs_device)
    assert (waiting.attempt_count, waiting.round_started_at) == (0, None)
    assert relay.send_due_texts(bobs_device) == []
    assert calculate_next_eligible_time(manual_clock.now()) == manual_clock.now() + timedelta(seconds=30)

    relay.receive_replies(bobs_device, "HT1 K ivan 2 1")

    assert calculate_next_eligible_time(manual_clock.now()) == waiting.next_attempt_at
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 4 1/1 message 4"]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_new_refresh_head_waits_for_a_free_place_while_a_restarted_head_keeps_its_place(
    relay: RelayHarness, manual_clock: ManualClock, bob: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    carol, dave = create_user("carol", manual_clock.now()), create_user("dave", manual_clock.now())
    carols_device = create_device(3, manual_clock.now(), user=carol)
    daves_device = create_device(4, manual_clock.now(), user=dave)
    send_single_part(relay, carols_device, "bob", 7)
    MessageDelivery.objects.filter(device=bobs_device).update(state=State.FAILED)
    relay.receive_replies(bobs_device, "HT1 F carol")
    assert relay.send_due_texts(bobs_device) == ["HT1 m carol 7 1/1 message 7"]
    for message_id in (1, 2):
        send_single_part(relay, daves_device, "bob", message_id)
    assert len(relay.send_due_texts(bobs_device)) == 2
    send_single_part(relay, ivans_device, "bob", 3)
    MessageDelivery.objects.filter(message__sender__username="ivan").update(state=State.FAILED)
    relay.receive_replies(bobs_device, "HT1 F ivan")

    assert relay.send_due_texts(bobs_device) == []

    manual_clock.advance(seconds=5)
    relay.receive_replies(bobs_device, "HT1 F carol")
    assert relay.send_due_texts(bobs_device) == ["HT1 m carol 7 1/1 message 7"]
    assert find_delivery(3, bobs_device).attempt_count == 0
    assert_all_invariants()


@pytest.mark.django_db
@pytest.mark.parametrize("node_sync_state", [Contact.NodeSyncState.PENDING_ADD, Contact.NodeSyncState.ADD_FAILED])
def test_nothing_is_sent_to_a_device_that_is_not_on_the_node_and_no_attempt_is_spent(
    relay: RelayHarness,
    manual_clock: ManualClock,
    ivans_device: Contact,
    bobs_device: Contact,
    node_sync_state: Contact.NodeSyncState,
) -> None:
    send_single_part(relay, ivans_device, "bob", 1)
    Contact.objects.filter(id=bobs_device.pk).update(node_sync_state=node_sync_state)
    manual_clock.advance(minutes=30)

    assert relay.send_due_texts() == []
    assert calculate_next_eligible_time(manual_clock.now()) is None
    assert find_delivery(1, bobs_device).attempt_count == 0

    Contact.objects.filter(id=bobs_device.pk).update(node_sync_state=Contact.NodeSyncState.ON_NODE)
    assert relay.send_due_texts() == ["HT1 m ivan 1 1/1 message 1"]


@pytest.mark.django_db
def test_the_next_eligible_time_is_the_earliest_due_delivery_or_receipt(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    assert calculate_next_eligible_time(manual_clock.now()) is None
    send_single_part(relay, ivans_device, "bob", 1)
    assert calculate_next_eligible_time(manual_clock.now()) == manual_clock.now()
    relay.send_due_texts()
    assert calculate_next_eligible_time(manual_clock.now()) == manual_clock.now() + timedelta(seconds=30)

    relay.receive_replies(bobs_device, "HT1 K ivan 1 1")

    assert calculate_next_eligible_time(manual_clock.now()) == manual_clock.now() + timedelta(seconds=15)
    assert count_due_work(manual_clock.now() + timedelta(seconds=15)).receipts_due == 1


@pytest.mark.django_db
def test_prepare_next_packet_starts_rounds_and_fails_exhausted_deliveries(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "bob", 1)
    MessageDelivery.objects.update(attempt_count=6, round_started_at=manual_clock.now())

    assert relay.prepare_next_packet() is None

    delivery = find_delivery(1, bobs_device)
    assert delivery.state == State.FAILED
    assert delivery.failed_at == manual_clock.now()


@pytest.mark.django_db
def test_a_reply_is_prepared_as_a_packet_with_its_key_even_while_other_work_is_due(
    manual_clock: ManualClock, relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "bob", 1)

    packet = prepare_reply_packet(
        contact_id=ivans_device.pk,
        reply_key="M:bob:1",
        text="HT1 k bob 1 1",
        now=manual_clock.now(),
        connection_generation=4,
    )

    assert packet is not None
    stored_packet = OutboundPacket.objects.get(id=packet.packet_id)
    assert (stored_packet.purpose, stored_packet.reply_key, stored_packet.state) == (
        OutboundPacket.Purpose.REPLY,
        "M:bob:1",
        OutboundPacket.State.PREPARED,
    )
    assert stored_packet.connection_generation == 4
    assert stored_packet.contact_label == str(ivans_device)
    assert packet.contact_public_key == ivans_device.public_key
    assert find_delivery(1, bobs_device).attempt_count == 0


@pytest.mark.django_db
def test_a_reply_to_a_contact_that_is_gone_or_not_on_the_node_is_dropped(
    manual_clock: ManualClock, ivans_device: Contact
) -> None:
    Contact.objects.filter(id=ivans_device.pk).update(node_sync_state=Contact.NodeSyncState.PENDING_ADD)

    assert (
        prepare_reply_packet(
            contact_id=ivans_device.pk,
            reply_key="Q:bob",
            text="HT1 q bob 1",
            now=manual_clock.now(),
            connection_generation=1,
        )
        is None
    )
    assert (
        prepare_reply_packet(
            contact_id=999_999, reply_key="Q:bob", text="HT1 q bob 1", now=manual_clock.now(), connection_generation=1
        )
        is None
    )
    assert not OutboundPacket.objects.exists()


@pytest.mark.django_db
def test_sender_timestamps_always_increase_even_when_the_clock_steps_backwards(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    for message_id in (1, 2):
        send_single_part(relay, ivans_device, "bob", message_id)
    relay.send_due_texts()
    first_timestamps = list(OutboundPacket.objects.order_by("id").values_list("sender_timestamp", flat=True))
    assert first_timestamps == [int(manual_clock.now().timestamp()), int(manual_clock.now().timestamp()) + 1]

    manual_clock.advance(hours=-1)
    send_single_part(relay, ivans_device, "bob", 3)
    relay.send_due_texts()

    assert OutboundPacket.objects.order_by("-id").values_list("sender_timestamp", flat=True).first() == (
        first_timestamps[-1] + 1
    )
    manual_clock.advance(hours=2)
    assert allocate_sender_timestamp(manual_clock.now()) == int(manual_clock.now().timestamp())
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_the_sender_skips_a_device_that_is_being_deleted_instead_of_waiting_for_it(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "bob", 1)
    deletion_holds_the_device = threading.Event()
    sender_is_done = threading.Event()

    def hold_the_device_like_a_deletion() -> None:
        with transaction.atomic():
            list(Contact.objects.select_for_update().filter(id=bobs_device.pk))
            deletion_holds_the_device.set()
            sender_is_done.wait(timeout=10)

    def prepare_while_the_device_is_held() -> bool:
        deletion_holds_the_device.wait(timeout=10)
        try:
            return relay.prepare_next_packet() is None
        finally:
            sender_is_done.set()

    assert run_concurrently(hold_the_device_like_a_deletion, prepare_while_the_device_is_held) == [None, True]
    assert find_delivery(1, bobs_device).attempt_count == 0
    assert relay.send_due_texts() == ["HT1 m ivan 1 1/1 message 1"]
