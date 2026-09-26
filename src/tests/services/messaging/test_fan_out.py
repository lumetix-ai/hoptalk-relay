import pytest

from directory.models import Contact
from messaging.models import MessageDelivery, RefreshSession
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import TEST_PASSWORD, RelayHarness, create_device, create_user

State = MessageDelivery.State


@pytest.mark.django_db
def test_an_accepted_message_is_due_at_once_on_every_device_the_recipient_has_at_that_moment(
    relay: RelayHarness, manual_clock: ManualClock
) -> None:
    ivan, bob, carol = (create_user(username, manual_clock.now()) for username in ("ivan", "bob", "carol"))
    ivans_device = create_device(1, manual_clock.now(), user=ivan)
    bobs_devices = [create_device(device_number, manual_clock.now(), user=bob) for device_number in (2, 3)]
    create_device(4, manual_clock.now(), user=carol)
    device_signed_in_later = create_device(5, manual_clock.now())

    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(device_signed_in_later, f"HT1 A bob {TEST_PASSWORD}")

    deliveries = list(MessageDelivery.objects.order_by("device_id"))
    assert [delivery.device_id for delivery in deliveries] == [device.pk for device in bobs_devices]
    for delivery in deliveries:
        assert (delivery.state, delivery.attempt_count, delivery.arm_generation) == (State.PENDING, 0, 1)
        assert delivery.maximum_attempts == 6
        assert delivery.next_attempt_at == manual_clock.now()
        assert delivery.refresh_session is None
    assert_all_invariants()


@pytest.mark.django_db
def test_a_device_refreshing_the_senders_conversation_gets_the_message_queued_in_that_refresh(
    relay: RelayHarness, manual_clock: ManualClock
) -> None:
    ivan, bob = create_user("ivan", manual_clock.now()), create_user("bob", manual_clock.now())
    ivans_device = create_device(1, manual_clock.now(), user=ivan)
    refreshing_device = create_device(2, manual_clock.now(), user=bob)
    other_device: Contact = create_device(3, manual_clock.now(), user=bob)
    relay.receive_replies(ivans_device, "HT1 M bob 1 1/1 missed")
    MessageDelivery.objects.filter(device=refreshing_device).update(state=State.FAILED)
    relay.receive_replies(refreshing_device, "HT1 F ivan")
    refresh_session = RefreshSession.objects.get()

    relay.receive_replies(ivans_device, "HT1 M bob 2 1/1 new")

    queued = MessageDelivery.objects.get(device=refreshing_device, message__client_message_id=2)
    assert (queued.state, queued.refresh_session_id, queued.next_attempt_at) == (
        State.QUEUED_FOR_REFRESH,
        refresh_session.pk,
        None,
    )
    assert MessageDelivery.objects.get(device=other_device, message__client_message_id=2).state == State.PENDING
    refresh_session.refresh_from_db()
    assert refresh_session.messages_total == 2
    assert_all_invariants()
