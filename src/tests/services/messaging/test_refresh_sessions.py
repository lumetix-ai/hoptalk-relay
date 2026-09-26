from collections import defaultdict
from dataclasses import replace
from datetime import datetime

import pytest
from pytest_django import Settings

from directory.models import Contact, User
from messaging.deliveries import fail_delivery_after_exhausted_attempts
from messaging.models import InboundDirectMessage, MessageDelivery, ReceiptNotification, RefreshSession
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import (
    DEFAULT_RETRY_STRATEGY,
    TEST_PASSWORD,
    RelayHarness,
    configure_engine_settings,
    create_device,
    create_user,
)

State = MessageDelivery.State


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("Bob", manual_clock.now())


@pytest.fixture
def carol(manual_clock: ManualClock) -> User:
    return create_user("carol", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


@pytest.fixture
def bobs_device(bob: User, manual_clock: ManualClock) -> Contact:
    return create_device(2, manual_clock.now(), user=bob)


@pytest.fixture
def carols_device(carol: User, manual_clock: ManualClock) -> Contact:
    return create_device(3, manual_clock.now(), user=carol)


def send_single_part(relay: RelayHarness, sender_device: Contact, recipient: str, message_id: int, text: str) -> None:
    relay.receive_replies(sender_device, f"HT1 M {recipient} {message_id} 1/1 {text}")


def find_delivery(message_id: int, device: Contact) -> MessageDelivery:
    return MessageDelivery.objects.get(message__client_message_id=message_id, device=device)


def fail_every_delivery_to(device: Contact, now: datetime) -> None:
    for delivery in MessageDelivery.objects.filter(device=device, state=State.PENDING):
        assert fail_delivery_after_exhausted_attempts(delivery, now)


def deliver_head_messages_in_order(relay: RelayHarness, device: Contact, peer_username: str) -> list[str]:
    """Answer every part the device receives with its received-set, as a client does, until nothing more comes."""
    received_texts: list[str] = []
    held_part_numbers_by_message_id: dict[str, set[int]] = defaultdict(set)
    while sent_texts := relay.send_due_texts(device):
        for sent_text in sent_texts:
            received_texts.append(sent_text)
            message_id, part_field = sent_text.split(" ")[3:5]
            part_number, part_count = (int(value) for value in part_field.split("/"))
            held_part_numbers = held_part_numbers_by_message_id[message_id]
            held_part_numbers.add(part_number)
            received_set = "".join("1" if number in held_part_numbers else "0" for number in range(1, part_count + 1))
            relay.receive_replies(device, f"HT1 K {peer_username} {message_id} {received_set}")
    return received_texts


@pytest.mark.django_db
def test_a_refresh_takes_over_pending_deliveries_no_session_owns_and_the_oldest_becomes_the_head(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "first")
    send_single_part(relay, ivans_device, "Bob", 2, "second")
    relay.send_due_texts(bobs_device)
    manual_clock.advance(seconds=10)

    assert relay.receive_replies(bobs_device, "HT1 F ivan") == ["HT1 f ivan 2"]

    head, queued = find_delivery(1, bobs_device), find_delivery(2, bobs_device)
    refresh_session = RefreshSession.objects.get()
    assert (head.state, head.refresh_session_id) == (State.PENDING, refresh_session.pk)
    assert (head.attempt_count, head.arm_generation, head.next_attempt_at) == (0, 3, manual_clock.now())
    assert head.round_started_at is None
    assert (queued.state, queued.refresh_session_id) == (State.QUEUED_FOR_REFRESH, refresh_session.pk)
    assert (queued.attempt_count, queued.arm_generation, queued.round_pending_parts_mask) == (0, 2, 0)
    assert queued.next_attempt_at is None
    assert queued.round_started_at is None
    assert refresh_session.messages_total == 2
    assert refresh_session.requested_by_inbound == InboundDirectMessage.objects.order_by("-id").first()
    assert_all_invariants()


@pytest.mark.django_db
def test_a_refresh_takes_over_failed_deliveries_and_clears_their_failure(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "first")
    send_single_part(relay, ivans_device, "Bob", 2, "second")
    fail_every_delivery_to(bobs_device, manual_clock.now())

    assert relay.receive_replies(bobs_device, "HT1 F ivan") == ["HT1 f ivan 2"]

    queued = find_delivery(2, bobs_device)
    assert queued.state == State.QUEUED_FOR_REFRESH
    assert queued.failed_at is None
    assert queued.failure_reason == ""
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 1 1/1 first"]


@pytest.mark.django_db
def test_relinking_away_and_back_lets_a_refresh_revive_the_cancelled_deliveries_oldest_first(
    relay: RelayHarness, manual_clock: ManualClock, bob: User, carol: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "first")
    send_single_part(relay, ivans_device, "Bob", 2, "second")
    assert relay.receive_replies(bobs_device, f"HT1 A carol {TEST_PASSWORD}") == ["HT1 a carol"]
    assert relay.receive_replies(bobs_device, f"HT1 A Bob {TEST_PASSWORD}") == ["HT1 a Bob"]
    assert set(MessageDelivery.objects.values_list("state", flat=True)) == {State.CANCELLED}

    assert relay.receive_replies(bobs_device, "HT1 F *") == ["HT1 f * 2"]

    queued = find_delivery(2, bobs_device)
    assert queued.state == State.QUEUED_FOR_REFRESH
    assert queued.cancelled_at is None
    assert deliver_head_messages_in_order(relay, bobs_device, "ivan") == [
        "HT1 m ivan 1 1/1 first",
        "HT1 m ivan 2 1/1 second",
    ]
    assert RefreshSession.objects.get(state=RefreshSession.State.COMPLETED).finished_at == manual_clock.now()
    assert_all_invariants()


@pytest.mark.django_db
def test_a_delivery_revived_after_relinking_back_sends_every_part_again(
    relay: RelayHarness, manual_clock: ManualClock, bob: User, carol: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 77 1/3 one ")
    relay.receive_replies(ivans_device, "HT1 M Bob 77 2/3 two ")
    relay.receive_replies(ivans_device, "HT1 M Bob 77 3/3 three")
    relay.send_due_texts(bobs_device)
    relay.receive_replies(bobs_device, "HT1 K ivan 77 110")
    assert relay.receive_replies(bobs_device, f"HT1 A carol {TEST_PASSWORD}") == ["HT1 a carol"]
    assert relay.receive_replies(bobs_device, f"HT1 A Bob {TEST_PASSWORD}") == ["HT1 a Bob"]
    cancelled = find_delivery(77, bobs_device)
    assert (cancelled.state, cancelled.parts_received_mask) == (State.CANCELLED, 0b011)

    assert relay.receive_replies(bobs_device, "HT1 F *") == ["HT1 f * 1"]

    assert find_delivery(77, bobs_device).parts_received_mask == 0
    assert relay.send_due_texts(bobs_device) == [
        "HT1 m ivan 77 1/3 one ",
        "HT1 m ivan 77 2/3 two ",
        "HT1 m ivan 77 3/3 three",
    ]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_refresh_creates_deliveries_for_messages_no_device_of_the_user_received(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, carols_device: Contact
) -> None:
    create_user("dave", manual_clock.now())
    send_single_part(relay, ivans_device, "dave", 1, "from ivan")
    send_single_part(relay, carols_device, "dave", 2, "from carol")
    daves_new_device = create_device(4, manual_clock.now())
    relay.receive_replies(daves_new_device, f"HT1 A dave {TEST_PASSWORD}")

    assert relay.receive_replies(daves_new_device, "HT1 F *") == ["HT1 f * 2"]

    assert RefreshSession.objects.filter(device=daves_new_device, requested_for_all_peers=True).count() == 2
    assert set(MessageDelivery.objects.values_list("state", flat=True)) == {State.PENDING}
    assert sorted(relay.send_due_texts(daves_new_device)) == [
        "HT1 m carol 2 1/1 from carol",
        "HT1 m ivan 1 1/1 from ivan",
    ]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_refresh_of_every_peer_leaves_out_messages_another_device_of_the_user_received(
    relay: RelayHarness, manual_clock: ManualClock, bob: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "delivered elsewhere")
    relay.receive_replies(bobs_device, "HT1 K ivan 1 1")
    send_single_part(relay, ivans_device, "Bob", 2, "missed by everybody")
    MessageDelivery.objects.filter(message__client_message_id=2).delete()
    bobs_new_device = create_device(5, manual_clock.now(), user=bob)

    assert relay.receive_replies(bobs_new_device, "HT1 F *") == ["HT1 f * 1"]

    assert relay.send_due_texts(bobs_new_device) == ["HT1 m ivan 2 1/1 missed by everybody"]


@pytest.mark.django_db
def test_a_refresh_of_every_peer_runs_one_ordered_session_per_peer_and_answers_the_total(
    relay: RelayHarness,
    manual_clock: ManualClock,
    ivans_device: Contact,
    bobs_device: Contact,
    carols_device: Contact,
) -> None:
    for message_id in (1, 2, 3):
        send_single_part(relay, ivans_device, "Bob", message_id, f"ivan {message_id}")
    for message_id in (1, 2):
        send_single_part(relay, carols_device, "Bob", message_id, f"carol {message_id}")
    fail_every_delivery_to(bobs_device, manual_clock.now())

    assert relay.receive_replies(bobs_device, "HT1 F *") == ["HT1 f * 5"]

    assert RefreshSession.objects.filter(state=RefreshSession.State.ACTIVE).count() == 2
    assert sorted(relay.send_due_texts(bobs_device)) == ["HT1 m carol 1 1/1 carol 1", "HT1 m ivan 1 1/1 ivan 1"]
    relay.receive_replies(bobs_device, "HT1 K ivan 1 1")
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 2 1/1 ivan 2"]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_refresh_follows_acceptance_order_not_the_order_of_first_parts(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 10 1/2 accepted ")
    manual_clock.advance(seconds=1)
    send_single_part(relay, ivans_device, "Bob", 11, "accepted first")
    manual_clock.advance(seconds=1)
    relay.receive_replies(ivans_device, "HT1 M Bob 10 2/2 second")
    fail_every_delivery_to(bobs_device, manual_clock.now())

    relay.receive_replies(bobs_device, "HT1 F ivan")

    assert deliver_head_messages_in_order(relay, bobs_device, "ivan") == [
        "HT1 m ivan 11 1/1 accepted first",
        "HT1 m ivan 10 1/2 accepted ",
        "HT1 m ivan 10 2/2 second",
    ]


@pytest.mark.django_db
def test_a_message_accepted_during_a_refresh_is_appended_and_delivered_after_the_older_ones(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "old")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")

    send_single_part(relay, ivans_device, "Bob", 2, "new")

    appended = find_delivery(2, bobs_device)
    assert appended.state == State.QUEUED_FOR_REFRESH
    assert RefreshSession.objects.get().messages_total == 2
    assert deliver_head_messages_in_order(relay, bobs_device, "ivan") == [
        "HT1 m ivan 1 1/1 old",
        "HT1 m ivan 2 1/1 new",
    ]
    assert_all_invariants()


@pytest.mark.django_db
def test_an_exhausted_head_stops_its_refresh_and_fails_the_queue_unsent(
    relay: RelayHarness, manual_clock: ManualClock, settings: Settings, ivans_device: Contact, bobs_device: Contact
) -> None:
    configure_engine_settings(settings, retry_strategy=replace(DEFAULT_RETRY_STRATEGY, maximum_attempts=2))
    for message_id in (1, 2, 3):
        send_single_part(relay, ivans_device, "Bob", message_id, f"message {message_id}")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")

    sent_texts: list[str] = []
    refresh_session = RefreshSession.objects.get()
    while refresh_session.state == RefreshSession.State.ACTIVE:
        sent_texts.extend(relay.send_due_texts(bobs_device))
        manual_clock.advance(seconds=30)
        refresh_session.refresh_from_db()

    assert sent_texts == ["HT1 m ivan 1 1/1 message 1", "HT1 m ivan 1 1/1 message 1"]
    assert refresh_session.state == RefreshSession.State.STOPPED
    assert find_delivery(1, bobs_device).failure_reason == MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED
    for message_id in (2, 3):
        stopped = find_delivery(message_id, bobs_device)
        assert (stopped.state, stopped.failure_reason) == (State.FAILED, MessageDelivery.FailureReason.REFRESH_STOPPED)
        assert stopped.refresh_session_id == refresh_session.pk
    manual_clock.advance(hours=1)
    assert relay.send_due_texts(bobs_device) == []

    assert relay.receive_replies(bobs_device, "HT1 F ivan") == ["HT1 f ivan 3"]
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 1 1/1 message 1"]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_repeated_refresh_restarts_the_head_at_once_and_keeps_the_queue_in_order(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    for message_id in (1, 2):
        send_single_part(relay, ivans_device, "Bob", message_id, f"message {message_id}")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")
    relay.send_due_texts(bobs_device)
    manual_clock.advance(seconds=30)
    relay.send_due_texts(bobs_device)
    manual_clock.advance(seconds=10)
    head_before = find_delivery(1, bobs_device)
    assert head_before.attempt_count == 2

    assert relay.receive_replies(bobs_device, "HT1 F ivan") == ["HT1 f ivan 2"]

    head = find_delivery(1, bobs_device)
    assert (head.attempt_count, head.next_attempt_at) == (0, manual_clock.now())
    assert head.arm_generation == head_before.arm_generation + 1
    assert head.round_started_at == head_before.round_started_at
    assert find_delivery(2, bobs_device).state == State.QUEUED_FOR_REFRESH
    assert RefreshSession.objects.count() == 1
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 1 1/1 message 1"]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_repeated_refresh_while_the_head_waits_for_its_final_check_sends_it_again_at_once(
    relay: RelayHarness, manual_clock: ManualClock, settings: Settings, ivans_device: Contact, bobs_device: Contact
) -> None:
    configure_engine_settings(settings, retry_strategy=replace(DEFAULT_RETRY_STRATEGY, maximum_attempts=1))
    send_single_part(relay, ivans_device, "Bob", 1, "hello")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 1 1/1 hello"]
    manual_clock.advance(seconds=10)
    assert find_delivery(1, bobs_device).attempt_count == 1

    relay.receive_replies(bobs_device, "HT1 F ivan")

    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 1 1/1 hello"]
    assert find_delivery(1, bobs_device).state == State.PENDING


@pytest.mark.django_db
def test_a_repeated_refresh_during_a_round_abandons_it_and_its_later_outcomes_change_nothing(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 1 1/2 one ")
    relay.receive_replies(ivans_device, "HT1 M Bob 1 2/2 two")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")
    first_part = relay.prepare_next_packet()
    assert first_part is not None
    relay.record_queued_on_node(first_part)
    abandoned_part = relay.prepare_next_packet()
    assert abandoned_part is not None
    manual_clock.advance(seconds=1)

    relay.receive_replies(bobs_device, "HT1 F ivan")
    relay.record_queued_on_node(abandoned_part)

    head = find_delivery(1, bobs_device)
    assert (head.attempt_count, head.round_pending_parts_mask) == (0, 0)
    assert relay.send_due_texts(bobs_device) == ["HT1 m ivan 1 1/2 one ", "HT1 m ivan 1 2/2 two"]
    assert find_delivery(1, bobs_device).attempt_count == 1


@pytest.mark.django_db
def test_a_refresh_with_nothing_missing_answers_zero_and_starts_nothing(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "hello")
    relay.receive_replies(bobs_device, "HT1 K ivan 1 1")

    assert relay.receive_replies(bobs_device, "HT1 F IVAN") == ["HT1 f ivan 0"]
    assert relay.receive_replies(bobs_device, "HT1 F *") == ["HT1 f * 0"]
    assert not RefreshSession.objects.exists()


@pytest.mark.django_db
def test_a_refresh_of_nobody_of_oneself_or_from_an_unlinked_device_is_refused(
    relay: RelayHarness, manual_clock: ManualClock, bobs_device: Contact
) -> None:
    unlinked_device = create_device(9, manual_clock.now())

    assert relay.receive_replies(bobs_device, "HT1 F nobody") == ["HT1 e NO_SUCH_USER F nobody"]
    assert relay.receive_replies(bobs_device, "HT1 F bob") == ["HT1 e SELF F bob"]
    assert relay.receive_replies(unlinked_device, "HT1 F *") == ["HT1 e NOT_SIGNED_IN F *"]


@pytest.mark.django_db
def test_a_complete_status_for_a_queued_message_delivers_it_and_the_refresh_skips_it(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    for message_id in (1, 2, 3):
        send_single_part(relay, ivans_device, "Bob", message_id, f"message {message_id}")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")

    relay.receive_replies(bobs_device, "HT1 K ivan 2 1")

    assert find_delivery(2, bobs_device).state == State.DELIVERED
    assert deliver_head_messages_in_order(relay, bobs_device, "ivan") == [
        "HT1 m ivan 1 1/1 message 1",
        "HT1 m ivan 3 1/1 message 3",
    ]
    assert RefreshSession.objects.get().state == RefreshSession.State.COMPLETED
    assert_all_invariants()


@pytest.mark.django_db
def test_a_refresh_of_one_peer_restarts_the_receipts_of_messages_this_device_sent_to_that_peer(
    relay: RelayHarness, manual_clock: ManualClock, ivan: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    ivans_other_device = create_device(4, manual_clock.now(), user=ivan)
    send_single_part(relay, ivans_device, "Bob", 1, "from the first device")
    send_single_part(relay, ivans_other_device, "Bob", 2, "from the second device")
    relay.receive_replies(bobs_device, "HT1 K ivan 1 1")
    relay.receive_replies(bobs_device, "HT1 K ivan 2 1")
    for receipt in ReceiptNotification.objects.filter(device=ivans_device):
        ReceiptNotification.objects.filter(id=receipt.pk).update(
            state=ReceiptNotification.State.FAILED, failed_at=manual_clock.now()
        )
    manual_clock.advance(minutes=1)

    assert relay.receive_replies(ivans_device, "HT1 F *") == ["HT1 f * 0"]
    assert set(ReceiptNotification.objects.filter(device=ivans_device).values_list("state", flat=True)) == {
        ReceiptNotification.State.FAILED
    }

    assert relay.receive_replies(ivans_device, "HT1 F bob") == ["HT1 f Bob 0"]

    rearmed = ReceiptNotification.objects.get(device=ivans_device, message__client_message_id=1)
    untouched = ReceiptNotification.objects.get(device=ivans_device, message__client_message_id=2)
    assert (rearmed.state, rearmed.attempt_count, rearmed.next_attempt_at) == (
        ReceiptNotification.State.PENDING,
        0,
        manual_clock.now(),
    )
    assert untouched.state == ReceiptNotification.State.FAILED
    assert_all_invariants()


@pytest.mark.django_db
def test_a_status_with_zeros_on_a_queued_delivery_records_its_set_for_the_round_it_gets_as_head(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_single_part(relay, ivans_device, "Bob", 1, "first")
    relay.receive_replies(ivans_device, "HT1 M Bob 2 1/2 one ")
    relay.receive_replies(ivans_device, "HT1 M Bob 2 2/2 two")
    fail_every_delivery_to(bobs_device, manual_clock.now())
    relay.receive_replies(bobs_device, "HT1 F ivan")

    relay.receive_replies(bobs_device, "HT1 K ivan 2 10")

    queued = find_delivery(2, bobs_device)
    assert (queued.state, queued.parts_received_mask) == (State.QUEUED_FOR_REFRESH, 0b01)
    assert deliver_head_messages_in_order(relay, bobs_device, "ivan") == [
        "HT1 m ivan 1 1/1 first",
        "HT1 m ivan 2 2/2 two",
    ]
