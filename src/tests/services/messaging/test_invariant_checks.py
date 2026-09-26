"""The invariant checker must catch what it claims to catch, or every test ending with it proves less than it says."""

from datetime import UTC, datetime

import pytest

from directory.models import Contact, User
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket, ReceiptNotification
from node.node_identity_backups import encrypt_node_identity_backup, write_node_identity_backup
from node.node_settings import replace_node_configuration
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.node_key_pairs import generate_node_key_pair
from tests.services.messaging.engine_builders import TEST_PASSWORD, RelayHarness, create_device, create_user
from tests.services.node.node_builders import build_node_configuration


@pytest.fixture
def ivans_device(manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=create_user("ivan", manual_clock.now()))


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("bob", manual_clock.now())


@pytest.fixture
def bobs_device(bob: User, manual_clock: ManualClock) -> Contact:
    return create_device(2, manual_clock.now(), user=bob)


def assert_invariants_are_broken(expected_violation: str) -> None:
    with pytest.raises(AssertionError) as broken_invariants:
        assert_all_invariants()
    assert expected_violation in str(broken_invariants.value)


@pytest.mark.django_db
def test_a_live_delivery_to_a_device_of_another_user_is_caught(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    assert_all_invariants()
    carol = create_user("carol", manual_clock.now(), password=TEST_PASSWORD)

    Contact.objects.filter(id=bobs_device.pk).update(user=carol)

    assert_invariants_are_broken("is live, but its device is not linked to the recipient")


@pytest.mark.django_db
def test_an_active_refresh_session_without_a_head_is_caught(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    MessageDelivery.objects.update(state=MessageDelivery.State.FAILED)
    relay.receive_replies(bobs_device, "HT1 F ivan")
    assert_all_invariants()

    MessageDelivery.objects.update(state=MessageDelivery.State.QUEUED_FOR_REFRESH)

    assert_invariants_are_broken("has 0 heads")


@pytest.mark.django_db
def test_a_receipt_ahead_of_its_message_is_caught(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 K ivan 5 1")
    assert_all_invariants()

    ReceiptNotification.objects.update(target_level=ReceiptNotification.TargetLevel.READ)

    assert_invariants_are_broken("for a message at level 1")


@pytest.mark.django_db
def test_a_packet_timestamp_that_does_not_increase_is_caught(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(ivans_device, "HT1 M bob 6 1/1 again")
    relay.send_due_texts()
    assert_all_invariants()
    last_packet = OutboundPacket.objects.order_by("-id").first()
    assert last_packet is not None

    OutboundPacket.objects.filter(id=last_packet.pk).update(sender_timestamp=1)

    assert_invariants_are_broken("has a timestamp that does not increase")


@pytest.mark.django_db
def test_a_firmware_repeat_stored_as_a_row_of_its_own_is_caught(relay: RelayHarness, ivans_device: Contact) -> None:
    relay.receive(ivans_device, "HT1 Q ivan", sender_timestamp=1_790_000_000)
    relay.receive(ivans_device, "HT1 Q ivan", sender_timestamp=1_790_000_001)
    assert_all_invariants()

    InboundDirectMessage.objects.update(sender_timestamp=1_790_000_000)

    assert_invariants_are_broken("is a firmware-level repeat")


@pytest.mark.django_db
def test_an_incomplete_message_with_deliveries_is_caught(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")

    Message.objects.update(accepted_at=None, text=None)

    assert_invariants_are_broken("is incomplete but has deliveries or receipts")


@pytest.mark.django_db
def test_an_identity_backup_of_another_node_than_the_configured_one_is_caught() -> None:
    other_node_key_pair = generate_node_key_pair(20260926)
    replace_node_configuration(build_node_configuration(public_key="aa" * 32))
    write_node_identity_backup(
        encrypt_node_identity_backup(
            other_node_key_pair.public_key, other_node_key_pair.private_key, datetime(2026, 9, 26, tzinfo=UTC)
        )
    )

    assert_invariants_are_broken(f"The identity backup belongs to key {other_node_key_pair.public_key[:12]}")
