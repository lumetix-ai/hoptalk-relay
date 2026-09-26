from collections.abc import Callable
from datetime import datetime, timedelta

import pytest

from directory.models import Contact, User
from messaging.deliveries import (
    arm_refresh_session_head,
    fail_delivery_after_exhausted_attempts,
    fail_delivery_of_stopped_refresh,
    mark_delivery_delivered,
    record_delivery_part_sent,
    record_delivery_read,
    record_incomplete_delivery_status,
    restart_refresh_session_head,
    start_delivery_round,
    take_delivery_into_refresh_session,
)
from messaging.models import (
    InboundDirectMessage,
    Message,
    MessageDelivery,
    OutboundPacket,
    ReceiptNotification,
    RefreshSession,
)
from messaging.outbound_packets import PacketQueuedOnNode
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, create_device, create_user

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


def send_message(relay: RelayHarness, sender_device: Contact, recipient: str, message_id: int, *parts: str) -> None:
    for part_number, part_text in enumerate(parts, start=1):
        relay.receive_replies(sender_device, f"HT1 M {recipient} {message_id} {part_number}/{len(parts)} {part_text}")


def find_delivery(message_id: int, device: Contact) -> MessageDelivery:
    return MessageDelivery.objects.get(message__client_message_id=message_id, device=device)


def seconds_since(start: datetime, moment: datetime | None) -> float:
    assert moment is not None
    return (moment - start).total_seconds()


@pytest.mark.django_db
def test_a_round_sends_every_part_in_order_and_counts_one_attempt(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")

    assert relay.send_due_texts(bobs_device) == [
        "HT1 m ivan 5 1/3 one ",
        "HT1 m ivan 5 2/3 two ",
        "HT1 m ivan 5 3/3 three",
    ]

    delivery = find_delivery(5, bobs_device)
    assert delivery.state == State.PENDING
    assert delivery.attempt_count == 1
    assert delivery.round_pending_parts_mask == 0
    assert delivery.round_started_at == manual_clock.now()
    assert delivery.last_sent_at == manual_clock.now()
    assert delivery.next_attempt_at == manual_clock.now() + timedelta(seconds=30)
    assert_all_invariants()


@pytest.mark.django_db
def test_a_device_that_never_answers_gets_rounds_on_the_retry_schedule_and_then_fails(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "hello")
    start = manual_clock.now()
    round_offsets: list[float] = []

    delivery = find_delivery(5, bobs_device)
    while delivery.state == State.PENDING:
        if relay.send_due_texts(bobs_device):
            round_offsets.append(seconds_since(start, manual_clock.now()))
        delivery.refresh_from_db()
        if delivery.next_attempt_at is not None:
            manual_clock.current_time = max(manual_clock.now(), delivery.next_attempt_at)

    assert round_offsets == [0, 30, 90, 210, 450, 930]
    assert delivery.state == State.FAILED
    assert delivery.failure_reason == MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED
    assert seconds_since(start, delivery.failed_at) == 1530
    assert delivery.attempt_count == delivery.maximum_attempts == 6
    assert_all_invariants()


@pytest.mark.django_db
def test_every_round_sends_every_part_the_device_has_not_reported(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")
    relay.send_due_texts(bobs_device)
    relay.receive_replies(bobs_device, "HT1 K ivan 5 010")

    manual_clock.advance(seconds=5)
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 1/3 one ", "HT1 m ivan 5 3/3 three"]
    manual_clock.advance(seconds=60)
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 1/3 one ", "HT1 m ivan 5 3/3 three"]
    assert find_delivery(5, bobs_device).attempt_count == 3


@pytest.mark.django_db
def test_a_status_with_zeros_after_a_complete_round_brings_the_next_round_within_five_seconds(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two")
    relay.send_due_texts(bobs_device)
    manual_clock.advance(seconds=10)

    assert relay.receive_replies(bobs_device, "HT1 K ivan 5 10") == []

    delivery = find_delivery(5, bobs_device)
    assert delivery.parts_received_mask == 0b01
    assert delivery.next_attempt_at == manual_clock.now() + timedelta(seconds=5)
    manual_clock.advance(seconds=4)
    assert relay.send_due_texts(bobs_device) == []
    manual_clock.advance(seconds=1)
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 2/2 two"]


@pytest.mark.django_db
def test_a_status_during_a_round_only_drops_the_reported_parts_from_it(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")
    first_packet = relay.prepare_next_packet()
    assert first_packet is not None
    relay.record_queued_on_node(first_packet)

    relay.receive_replies(bobs_device, "HT1 K ivan 5 010")

    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 3/3 three"]
    assert find_delivery(5, bobs_device).next_attempt_at == manual_clock.now() + timedelta(seconds=30)


@pytest.mark.django_db
def test_a_status_that_reports_the_rest_of_a_round_in_progress_completes_it_and_the_gap_follows_soon(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")
    first_packet = relay.prepare_next_packet()
    assert first_packet is not None
    relay.record_queued_on_node(first_packet)
    manual_clock.advance(seconds=1)

    relay.receive_replies(bobs_device, "HT1 K ivan 5 011")

    delivery = find_delivery(5, bobs_device)
    assert delivery.round_pending_parts_mask == 0
    assert delivery.next_attempt_at == manual_clock.now() + timedelta(seconds=5)
    assert relay.send_due_texts(bobs_device) == []
    manual_clock.advance(seconds=5)
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 1/3 one "]
    assert find_delivery(5, bobs_device).attempt_count == 2


@pytest.mark.django_db
def test_a_complete_status_delivers_to_that_device_only_and_starts_the_delivered_receipts(
    relay: RelayHarness, manual_clock: ManualClock, bob: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    bobs_second_device = create_device(3, manual_clock.now(), user=bob)
    send_message(relay, ivans_device, "bob", 5, "hello")
    relay.send_due_texts()
    manual_clock.advance(seconds=3)

    assert relay.receive_replies(bobs_device, "HT1 K ivan 5 1") == []

    delivered = find_delivery(5, bobs_device)
    assert delivered.state == State.DELIVERED
    assert delivered.delivered_at == manual_clock.now()
    assert find_delivery(5, bobs_second_device).state == State.PENDING
    assert Message.objects.get().delivered_at == manual_clock.now()
    receipt = ReceiptNotification.objects.get()
    assert receipt.device == ivans_device
    assert receipt.target_level == ReceiptNotification.TargetLevel.DELIVERED
    assert receipt.next_attempt_at == manual_clock.now() + timedelta(seconds=15)
    manual_clock.advance(seconds=60)
    assert relay.send_due_texts(bobs_device) == []
    assert_all_invariants()


@pytest.mark.django_db
def test_only_a_status_that_is_all_ones_by_itself_delivers(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")
    relay.send_due_texts(bobs_device)

    relay.receive_replies(bobs_device, "HT1 K ivan 5 110")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 011")

    delivery = find_delivery(5, bobs_device)
    assert delivery.state == State.PENDING
    assert delivery.parts_received_mask == 0b110
    relay.receive_replies(bobs_device, "HT1 K ivan 5 111")
    assert find_delivery(5, bobs_device).state == State.DELIVERED


@pytest.mark.django_db
def test_a_device_that_reports_fewer_parts_than_before_gets_them_again(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")
    relay.send_due_texts(bobs_device)
    relay.receive_replies(bobs_device, "HT1 K ivan 5 101")
    manual_clock.advance(seconds=5)
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 2/3 two "]

    relay.receive_replies(bobs_device, "HT1 K ivan 5 001")
    manual_clock.advance(seconds=5)

    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 1/3 one ", "HT1 m ivan 5 2/3 two "]


@pytest.mark.django_db
def test_a_received_set_of_the_wrong_length_is_dropped(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two ", "three")

    processing_result = relay.receive(bobs_device, "HT1 K ivan 5 11")

    assert processing_result is not None
    assert processing_result.replies == ()
    delivery = find_delivery(5, bobs_device)
    assert delivery.parts_received_mask == 0
    assert delivery.last_acknowledgement_received_at is None
    inbox_row = InboundDirectMessage.objects.get(id=processing_result.inbox_row_id)
    assert inbox_row.classification == InboundDirectMessage.Classification.ACKNOWLEDGEMENT
    assert "does not match the part count" in inbox_row.outcome_summary


@pytest.mark.django_db
def test_a_late_complete_status_delivers_a_failed_delivery(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "hello")
    delivery = find_delivery(5, bobs_device)
    fail_delivery_after_exhausted_attempts(delivery, manual_clock.now())
    manual_clock.advance(hours=1)

    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")

    delivery.refresh_from_db()
    assert delivery.state == State.DELIVERED
    assert Message.objects.get().delivered_at == manual_clock.now()
    assert_all_invariants()


@pytest.mark.django_db
def test_a_status_with_zeros_on_a_failed_delivery_only_records_its_set(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two")
    delivery = find_delivery(5, bobs_device)
    fail_delivery_after_exhausted_attempts(delivery, manual_clock.now())

    relay.receive_replies(bobs_device, "HT1 K ivan 5 10")

    delivery.refresh_from_db()
    assert delivery.state == State.FAILED
    assert delivery.parts_received_mask == 0b01
    relay.receive_replies(bobs_device, "HT1 F ivan")
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 5 2/2 two"]


@pytest.mark.django_db
def test_a_read_delivers_the_message_to_that_device_and_records_the_read(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two")
    manual_clock.advance(seconds=3)

    assert relay.receive_replies(bobs_device, "HT1 R ivan 5") == ["HT1 r ivan 5"]

    delivery = find_delivery(5, bobs_device)
    assert delivery.state == State.DELIVERED
    assert delivery.read_at == delivery.delivered_at == manual_clock.now()
    message = Message.objects.get()
    assert message.delivered_at == message.read_at == manual_clock.now()
    receipt = ReceiptNotification.objects.get()
    assert receipt.target_level == ReceiptNotification.TargetLevel.READ
    assert receipt.next_attempt_at == manual_clock.now()
    assert_all_invariants()


@pytest.mark.django_db
def test_a_read_of_a_message_already_delivered_to_the_device_keeps_the_first_read_time(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "hello")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")
    manual_clock.advance(seconds=30)
    first_read_at = manual_clock.now()
    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    manual_clock.advance(seconds=30)

    assert relay.receive_replies(bobs_device, "HT1 R ivan 5") == ["HT1 r ivan 5"]

    assert find_delivery(5, bobs_device).read_at == first_read_at
    assert Message.objects.get().read_at == first_read_at


@pytest.mark.django_db
def test_a_status_for_a_delivered_or_cancelled_delivery_only_records_when_it_came(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two")
    send_message(relay, ivans_device, "bob", 6, "one ", "two")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 11")
    MessageDelivery.objects.filter(message__client_message_id=6).update(
        state=State.CANCELLED, cancelled_at=manual_clock.now()
    )
    manual_clock.advance(seconds=10)

    relay.receive_replies(bobs_device, "HT1 K ivan 5 01")
    relay.receive_replies(bobs_device, "HT1 K ivan 6 11")

    delivered, cancelled = find_delivery(5, bobs_device), find_delivery(6, bobs_device)
    assert (delivered.state, delivered.parts_received_mask) == (State.DELIVERED, 0b11)
    assert (cancelled.state, cancelled.parts_received_mask) == (State.CANCELLED, 0)
    assert delivered.last_acknowledgement_received_at == cancelled.last_acknowledgement_received_at
    assert delivered.last_acknowledgement_received_at == manual_clock.now()
    assert Message.objects.get(client_message_id=6).delivered_at is None


@pytest.mark.django_db
def test_the_outcome_of_a_packet_from_an_earlier_arm_changes_nothing(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two")
    stale_packet = relay.prepare_next_packet()
    assert stale_packet is not None
    delivery = find_delivery(5, bobs_device)
    fail_delivery_after_exhausted_attempts(delivery, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")
    rearmed = find_delivery(5, bobs_device)

    relay.record_queued_on_node(stale_packet)

    after_the_outcome = find_delivery(5, bobs_device)
    assert after_the_outcome.arm_generation == rearmed.arm_generation == delivery.arm_generation + 2
    assert after_the_outcome.round_pending_parts_mask == rearmed.round_pending_parts_mask
    assert after_the_outcome.last_sent_at is None
    assert after_the_outcome.next_attempt_at == rearmed.next_attempt_at


@pytest.mark.django_db
def test_a_part_sent_twice_in_one_round_is_counted_once(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "one ", "two")
    packet = relay.prepare_next_packet()
    assert packet is not None
    relay.record_queued_on_node(packet)
    delivery = find_delivery(5, bobs_device)

    record_delivery_part_sent(delivery, 1, 4000, manual_clock.now())

    assert find_delivery(5, bobs_device).round_pending_parts_mask == 0b10


def create_delivery_in_state(
    state: MessageDelivery.State,
    message: Message,
    device: Contact,
    refresh_session: RefreshSession,
    now: datetime,
) -> MessageDelivery:
    return MessageDelivery.objects.create(
        message=message,
        device=device,
        state=state,
        refresh_session=refresh_session if state in (State.QUEUED_FOR_REFRESH, State.PENDING) else None,
        maximum_attempts=6,
        attempt_count=1,
        next_attempt_at=now if state == State.PENDING else None,
        delivered_at=now if state == State.DELIVERED else None,
        round_started_at=now,
        created_at=now,
    )


type DeliveryTransition = Callable[[MessageDelivery, datetime, RefreshSession], bool]

DELIVERY_TRANSITIONS_AND_FORBIDDEN_STATES: list[tuple[str, DeliveryTransition, list[MessageDelivery.State]]] = [
    (
        "start a round",
        lambda delivery, now, session: start_delivery_round(delivery, 1, now),
        [State.QUEUED_FOR_REFRESH, State.DELIVERED, State.FAILED, State.CANCELLED],
    ),
    (
        "count a part as sent",
        lambda delivery, now, session: record_delivery_part_sent(delivery, 1, 4000, now),
        [State.QUEUED_FOR_REFRESH, State.DELIVERED, State.FAILED, State.CANCELLED],
    ),
    (
        "record a status with zeros",
        lambda delivery, now, session: record_incomplete_delivery_status(delivery, 0, now),
        [State.DELIVERED, State.CANCELLED],
    ),
    (
        "deliver",
        lambda delivery, now, session: mark_delivery_delivered(delivery, now),
        [State.DELIVERED, State.CANCELLED],
    ),
    (
        "record a read",
        lambda delivery, now, session: record_delivery_read(delivery, now),
        [State.PENDING, State.QUEUED_FOR_REFRESH, State.FAILED, State.CANCELLED],
    ),
    (
        "fail after the last attempt",
        lambda delivery, now, session: fail_delivery_after_exhausted_attempts(delivery, now),
        [State.QUEUED_FOR_REFRESH, State.DELIVERED, State.FAILED, State.CANCELLED],
    ),
    (
        "take into a refresh",
        lambda delivery, now, session: take_delivery_into_refresh_session(delivery, session.pk, now),
        [State.PENDING, State.QUEUED_FOR_REFRESH, State.DELIVERED],
    ),
    (
        "become the head",
        lambda delivery, now, session: arm_refresh_session_head(delivery, now),
        [State.PENDING, State.DELIVERED, State.FAILED, State.CANCELLED],
    ),
    (
        "restart the head",
        lambda delivery, now, session: restart_refresh_session_head(delivery, now),
        [State.QUEUED_FOR_REFRESH, State.DELIVERED, State.FAILED, State.CANCELLED],
    ),
    (
        "fail with its stopped refresh",
        lambda delivery, now, session: fail_delivery_of_stopped_refresh(delivery, now),
        [State.PENDING, State.DELIVERED, State.FAILED, State.CANCELLED],
    ),
]


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("transition_name", "transition", "forbidden_state"),
    [
        (transition_name, transition, forbidden_state)
        for transition_name, transition, forbidden_states in DELIVERY_TRANSITIONS_AND_FORBIDDEN_STATES
        for forbidden_state in forbidden_states
    ],
)
def test_a_transition_from_a_state_it_does_not_start_from_changes_nothing(
    manual_clock: ManualClock,
    ivan: User,
    bob: User,
    bobs_device: Contact,
    transition_name: str,
    transition: DeliveryTransition,
    forbidden_state: MessageDelivery.State,
) -> None:
    now = manual_clock.now()
    message = Message.objects.create(
        sender=ivan,
        recipient=bob,
        client_message_id=5,
        part_count=1,
        part_texts=["hello"],
        text="hello",
        created_at=now,
        last_part_at=now,
        accepted_at=now,
    )
    refresh_session = RefreshSession.objects.create(device=bobs_device, peer=ivan, requested_at=now)
    delivery = create_delivery_in_state(forbidden_state, message, bobs_device, refresh_session, now)
    stored_before = MessageDelivery.objects.filter(id=delivery.pk).values().get()

    was_applied = transition(delivery, now + timedelta(seconds=1), refresh_session)

    assert not was_applied, transition_name
    assert MessageDelivery.objects.filter(id=delivery.pk).values().get() == stored_before


@pytest.mark.django_db
def test_a_packet_outcome_for_a_delivery_that_ended_meanwhile_changes_nothing(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message(relay, ivans_device, "bob", 5, "hello")
    packet = relay.prepare_next_packet()
    assert packet is not None
    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")
    delivered = find_delivery(5, bobs_device)
    stored_before = MessageDelivery.objects.filter(id=delivered.pk).values().get()

    relay.record_outcome(
        packet,
        PacketQueuedOnNode(
            route=OutboundPacket.Route.DIRECT,
            expected_acknowledgement_code="0a0b0c0d",
            suggested_timeout_milliseconds=900,
        ),
    )

    assert MessageDelivery.objects.filter(id=delivered.pk).values().get() == stored_before
    assert OutboundPacket.objects.get(id=packet.packet_id).state == OutboundPacket.State.QUEUED_ON_NODE
    assert_all_invariants()
