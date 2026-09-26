from dataclasses import replace
from datetime import timedelta

import pytest
from pytest_django import Settings

from directory.models import Contact, User
from messaging.models import MessageDelivery, OutboundPacket, ReceiptNotification
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

State = ReceiptNotification.State
DELIVERED_LEVEL = ReceiptNotification.TargetLevel.DELIVERED
READ_LEVEL = ReceiptNotification.TargetLevel.READ


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("bob", manual_clock.now())


@pytest.fixture
def carol(manual_clock: ManualClock) -> User:
    return create_user("carol", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


@pytest.fixture
def ivans_second_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(2, manual_clock.now(), user=ivan)


@pytest.fixture
def bobs_device(bob: User, manual_clock: ManualClock) -> Contact:
    return create_device(3, manual_clock.now(), user=bob)


@pytest.fixture
def delivered_message(relay: RelayHarness, ivans_device: Contact, bobs_device: Contact) -> None:
    """Message 5 from ivan's first device, delivered to bob's device."""
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")


def find_receipt(device: Contact) -> ReceiptNotification:
    return ReceiptNotification.objects.get(device=device)


def read_receipt_packet_texts() -> list[str]:
    return list(
        OutboundPacket.objects.filter(purpose=OutboundPacket.Purpose.RECEIPT)
        .order_by("id")
        .values_list("text", flat=True)
    )


@pytest.mark.django_db
def test_a_delivered_message_gets_a_held_back_delivered_receipt_on_every_device_of_its_sender(
    relay: RelayHarness,
    manual_clock: ManualClock,
    ivans_device: Contact,
    ivans_second_device: Contact,
    bobs_device: Contact,
    delivered_message: None,
) -> None:
    receipts = list(ReceiptNotification.objects.order_by("device_id"))
    assert [receipt.device_id for receipt in receipts] == [ivans_device.pk, ivans_second_device.pk]
    for receipt in receipts:
        assert (receipt.state, receipt.target_level, receipt.confirmed_level) == (State.PENDING, DELIVERED_LEVEL, 0)
        assert (receipt.attempt_count, receipt.arm_generation) == (0, 1)
        assert receipt.next_attempt_at == manual_clock.now() + timedelta(seconds=15)

    manual_clock.advance(seconds=14)
    assert relay.send_due_texts() == []
    manual_clock.advance(seconds=1)
    assert relay.send_due_texts() == ["HT1 s bob 5 D", "HT1 s bob 5 D"]
    assert_all_invariants()


@pytest.mark.django_db
def test_without_a_hold_back_the_delivered_receipt_goes_at_once(
    relay: RelayHarness, settings: Settings, ivans_device: Contact, bobs_device: Contact
) -> None:
    configure_engine_settings(
        settings, retry_strategy=replace(DEFAULT_RETRY_STRATEGY, delivered_receipt_delay_seconds=0)
    )
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")

    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 D"]


@pytest.mark.django_db
def test_a_read_within_the_hold_back_means_only_the_read_receipt_is_ever_sent(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact, delivered_message: None
) -> None:
    manual_clock.advance(seconds=5)

    relay.receive_replies(bobs_device, "HT1 R ivan 5")

    receipt = find_receipt(ivans_device)
    assert (receipt.target_level, receipt.arm_generation, receipt.next_attempt_at) == (
        READ_LEVEL,
        2,
        manual_clock.now(),
    )
    manual_clock.advance(seconds=60)
    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 R"]
    assert read_receipt_packet_texts() == ["HT1 s bob 5 R"]


@pytest.mark.django_db
def test_a_read_before_any_delivery_status_creates_read_receipts_directly(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")

    relay.receive_replies(bobs_device, "HT1 R ivan 5")

    receipt = find_receipt(ivans_device)
    assert (receipt.target_level, receipt.next_attempt_at) == (READ_LEVEL, manual_clock.now())
    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 R"]


@pytest.mark.django_db
def test_receipts_go_only_to_the_devices_of_the_messages_sender(
    relay: RelayHarness, manual_clock: ManualClock, bob: User, carol: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    create_device(4, manual_clock.now(), user=bob)
    create_device(5, manual_clock.now(), user=carol)

    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 R ivan 5")

    assert list(ReceiptNotification.objects.values_list("device_id", flat=True)) == [ivans_device.pk]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_confirmation_confirms_the_receipt_and_its_attempts_stop(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, delivered_message: None
) -> None:
    manual_clock.advance(seconds=15)
    relay.send_due_texts(ivans_device)

    assert relay.receive_replies(ivans_device, "HT1 C bob 5 D") == []

    receipt = find_receipt(ivans_device)
    assert (receipt.state, receipt.confirmed_level) == (State.CONFIRMED, DELIVERED_LEVEL)
    assert receipt.last_confirmation_received_at == manual_clock.now()
    manual_clock.advance(hours=1)
    assert relay.send_due_texts(ivans_device) == []


@pytest.mark.django_db
def test_a_read_confirmation_for_a_delivered_target_confirms_it_within_the_levels_rule(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, delivered_message: None
) -> None:
    relay.receive_replies(ivans_device, "HT1 C bob 5 R")

    receipt = find_receipt(ivans_device)
    assert (receipt.state, receipt.confirmed_level, receipt.target_level) == (State.CONFIRMED, 1, 1)
    assert_all_invariants()


@pytest.mark.django_db
def test_a_late_delivered_confirmation_after_a_read_confirmation_changes_nothing(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact, delivered_message: None
) -> None:
    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    relay.receive_replies(ivans_device, "HT1 C bob 5 R")

    relay.receive_replies(ivans_device, "HT1 C bob 5 D")

    receipt = find_receipt(ivans_device)
    assert (receipt.state, receipt.confirmed_level, receipt.target_level) == (State.CONFIRMED, 2, 2)


@pytest.mark.django_db
def test_a_confirmation_for_an_unknown_message_is_ignored(
    relay: RelayHarness, ivans_device: Contact, delivered_message: None
) -> None:
    processing_result = relay.receive(ivans_device, "HT1 C bob 999 D")

    assert processing_result is not None
    assert processing_result.replies == ()
    assert find_receipt(ivans_device).state == State.PENDING


@pytest.mark.django_db
def test_a_receipt_nobody_confirms_fails_after_its_attempts_and_a_read_restarts_it(
    relay: RelayHarness,
    manual_clock: ManualClock,
    settings: Settings,
    ivans_device: Contact,
    bobs_device: Contact,
) -> None:
    configure_engine_settings(settings, retry_strategy=replace(DEFAULT_RETRY_STRATEGY, maximum_attempts=2))
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")
    sent_texts: list[str] = []
    receipt = find_receipt(ivans_device)
    while receipt.state == State.PENDING:
        sent_texts.extend(relay.send_due_texts(ivans_device))
        manual_clock.advance(seconds=15)
        receipt.refresh_from_db()

    assert sent_texts == ["HT1 s bob 5 D", "HT1 s bob 5 D"]
    assert receipt.state == State.FAILED
    assert receipt.failed_at is not None

    relay.receive_replies(bobs_device, "HT1 R ivan 5")

    receipt.refresh_from_db()
    assert (receipt.state, receipt.target_level, receipt.attempt_count) == (State.PENDING, READ_LEVEL, 0)
    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 R"]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_receipt_packet_carries_the_target_level_of_the_moment_it_is_prepared(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact, delivered_message: None
) -> None:
    manual_clock.advance(seconds=15)
    delivered_packet = relay.prepare_next_packet()
    assert delivered_packet is not None
    assert delivered_packet.text == "HT1 s bob 5 D"

    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    relay.record_queued_on_node(delivered_packet)

    receipt = find_receipt(ivans_device)
    assert (receipt.round_pending, receipt.attempt_count, receipt.last_sent_at) == (False, 0, None)
    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 R"]


@pytest.mark.django_db
def test_relinking_cancels_open_receipts_and_keeps_one_confirmed_at_read(
    relay: RelayHarness,
    manual_clock: ManualClock,
    carol: User,
    ivans_device: Contact,
    ivans_second_device: Contact,
    bobs_device: Contact,
    delivered_message: None,
) -> None:
    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    relay.receive_replies(ivans_device, "HT1 C bob 5 R")

    relay.receive_replies(ivans_device, f"HT1 A carol {TEST_PASSWORD}")
    relay.receive_replies(ivans_second_device, f"HT1 A carol {TEST_PASSWORD}")

    assert find_receipt(ivans_device).state == State.CONFIRMED
    assert find_receipt(ivans_second_device).state == State.CANCELLED
    assert_all_invariants()


@pytest.mark.django_db
@pytest.mark.parametrize("confirmation", ["D", "R"])
def test_relinking_away_and_back_then_refreshing_keeps_a_confirmed_receipt_confirmed(
    relay: RelayHarness,
    manual_clock: ManualClock,
    carol: User,
    ivans_device: Contact,
    bobs_device: Contact,
    delivered_message: None,
    confirmation: str,
) -> None:
    if confirmation == "R":
        relay.receive_replies(bobs_device, "HT1 R ivan 5")
    relay.receive_replies(ivans_device, f"HT1 C bob 5 {confirmation}")
    relay.receive_replies(ivans_device, f"HT1 A carol {TEST_PASSWORD}")
    relay.receive_replies(ivans_device, f"HT1 A ivan {TEST_PASSWORD}")

    assert relay.receive_replies(ivans_device, "HT1 F bob") == ["HT1 f bob 0"]

    receipt = find_receipt(ivans_device)
    assert receipt.state == State.CONFIRMED
    assert receipt.confirmed_level == receipt.target_level
    assert receipt.cancelled_at is None
    assert relay.send_due_texts(ivans_device) == []
    assert_all_invariants()


@pytest.mark.django_db
def test_a_read_after_relinking_away_and_back_revives_the_cancelled_receipt_and_sends_it(
    relay: RelayHarness,
    manual_clock: ManualClock,
    carol: User,
    ivans_device: Contact,
    bobs_device: Contact,
    delivered_message: None,
) -> None:
    relay.receive_replies(ivans_device, f"HT1 A carol {TEST_PASSWORD}")
    relay.receive_replies(ivans_device, f"HT1 A ivan {TEST_PASSWORD}")
    assert find_receipt(ivans_device).state == State.CANCELLED

    relay.receive_replies(bobs_device, "HT1 R ivan 5")

    receipt = find_receipt(ivans_device)
    assert (receipt.state, receipt.target_level, receipt.cancelled_at) == (State.PENDING, READ_LEVEL, None)
    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 R"]
    assert_all_invariants()


@pytest.mark.django_db
def test_a_refresh_after_relinking_back_revives_an_unconfirmed_receipt_as_pending(
    relay: RelayHarness,
    manual_clock: ManualClock,
    carol: User,
    ivans_device: Contact,
    bobs_device: Contact,
    delivered_message: None,
) -> None:
    relay.receive_replies(ivans_device, f"HT1 A carol {TEST_PASSWORD}")
    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    relay.receive_replies(ivans_device, f"HT1 A ivan {TEST_PASSWORD}")

    relay.receive_replies(ivans_device, "HT1 F bob")

    receipt = find_receipt(ivans_device)
    assert (receipt.state, receipt.target_level, receipt.attempt_count) == (State.PENDING, READ_LEVEL, 0)
    assert relay.send_due_texts(ivans_device) == ["HT1 s bob 5 R"]
    assert MessageDelivery.objects.get().state == MessageDelivery.State.DELIVERED
    assert_all_invariants()
