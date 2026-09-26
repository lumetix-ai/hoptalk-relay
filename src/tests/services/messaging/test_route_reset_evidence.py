from datetime import timedelta

import pytest

from directory.models import Contact, User
from messaging.models import OutboundPacket
from messaging.outbound_packets import PacketQueuedOnNode, record_acknowledgement_deadline_passed, record_send_outcome
from messaging.outbound_scheduling import PacketDescriptor, prepare_reply_packet
from messaging.route_reset_evidence import (
    needs_flood_arrival_route_reset,
    record_path_update,
    record_route_reset_performed,
    settle_pending_route_resets,
)
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, create_device, create_user

RouteResetState = OutboundPacket.RouteResetState


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


def time_out(packet: PacketDescriptor, manual_clock: ManualClock) -> None:
    manual_clock.advance(seconds=5)
    acknowledgement_timeout = record_acknowledgement_deadline_passed(packet.packet_id, manual_clock.now())
    assert acknowledgement_timeout is not None
    assert acknowledgement_timeout.route_reset_pending


def send_reply(contact: Contact, reply_key: str, text: str, manual_clock: ManualClock) -> PacketDescriptor:
    packet = prepare_reply_packet(
        contact_id=contact.pk, reply_key=reply_key, text=text, now=manual_clock.now(), connection_generation=1
    )
    assert packet is not None
    record_send_outcome(
        packet.packet_id,
        PacketQueuedOnNode(
            route=OutboundPacket.Route.DIRECT,
            expected_acknowledgement_code=f"{packet.sender_timestamp:08x}",
            suggested_timeout_milliseconds=3000,
        ),
        manual_clock.now(),
    )
    return packet


def read_route_reset_state(packet: PacketDescriptor) -> str:
    return OutboundPacket.objects.values_list("route_reset_state", flat=True).get(id=packet.packet_id)


@pytest.mark.django_db
def test_a_status_covering_the_part_proves_the_delivery_arrived_so_the_route_is_kept(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/2 one ")
    relay.receive_replies(ivans_device, "HT1 M bob 5 2/2 two")
    first_part = relay.prepare_next_packet_and_queue()
    time_out(first_part, manual_clock)
    manual_clock.advance(seconds=1)
    relay.receive_replies(bobs_device, "HT1 K ivan 5 10")

    settlement = settle_pending_route_resets(bobs_device.pk, manual_clock.now())

    assert not settlement.needs_reset
    assert read_route_reset_state(first_part) == RouteResetState.SKIPPED_APPLICATION_EVIDENCE


@pytest.mark.django_db
def test_a_status_that_misses_the_part_is_no_evidence_and_the_route_is_reset(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/2 one ")
    relay.receive_replies(ivans_device, "HT1 M bob 5 2/2 two")
    first_part = relay.prepare_next_packet_and_queue()
    time_out(first_part, manual_clock)
    relay.receive_replies(bobs_device, "HT1 K ivan 5 01")

    settlement = settle_pending_route_resets(bobs_device.pk, manual_clock.now())

    assert settlement.packet_ids_needing_reset == (first_part.packet_id,)
    assert settlement.replies_to_resend == ()
    assert record_route_reset_performed(settlement.packet_ids_needing_reset, manual_clock.now()) == 1
    assert read_route_reset_state(first_part) == RouteResetState.PERFORMED
    assert not settle_pending_route_resets(bobs_device.pk, manual_clock.now()).needs_reset


@pytest.mark.django_db
def test_a_confirmation_of_the_receipt_level_proves_the_receipt_arrived(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    receipt_packet = relay.prepare_next_packet_and_queue()
    assert receipt_packet.text == "HT1 s bob 5 R"
    time_out(receipt_packet, manual_clock)
    relay.receive_replies(ivans_device, "HT1 C bob 5 R")

    assert not settle_pending_route_resets(ivans_device.pk, manual_clock.now()).needs_reset
    assert read_route_reset_state(receipt_packet) == RouteResetState.SKIPPED_APPLICATION_EVIDENCE


@pytest.mark.django_db
def test_a_confirmation_below_the_level_the_receipt_carried_is_no_evidence(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 hello")
    relay.receive_replies(bobs_device, "HT1 R ivan 5")
    receipt_packet = relay.prepare_next_packet_and_queue()
    time_out(receipt_packet, manual_clock)
    relay.receive_replies(ivans_device, "HT1 C bob 5 D")

    assert settle_pending_route_resets(ivans_device.pk, manual_clock.now()).needs_reset


@pytest.mark.django_db
def test_a_later_request_of_another_kind_proves_the_reply_arrived(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    reply = send_reply(ivans_device, "Q:bob", "HT1 q bob 1", manual_clock)
    time_out(reply, manual_clock)
    relay.receive_replies(ivans_device, "HT1 Q carol")

    assert not settle_pending_route_resets(ivans_device.pk, manual_clock.now()).needs_reset
    assert read_route_reset_state(reply) == RouteResetState.SKIPPED_APPLICATION_EVIDENCE


@pytest.mark.django_db
def test_a_retry_of_the_same_request_is_no_evidence_and_the_young_reply_is_offered_for_a_flood_resend(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    reply = send_reply(ivans_device, "Q:bob", "HT1 q bob 1", manual_clock)
    time_out(reply, manual_clock)
    relay.receive_replies(ivans_device, "HT1 Q BOB")

    settlement = settle_pending_route_resets(ivans_device.pk, manual_clock.now())

    assert settlement.packet_ids_needing_reset == (reply.packet_id,)
    [resend_candidate] = settlement.replies_to_resend
    assert (resend_candidate.reply_key, resend_candidate.text, resend_candidate.contact_id) == (
        "Q:bob",
        "HT1 q bob 1",
        ivans_device.pk,
    )


@pytest.mark.django_db
def test_a_reply_is_not_resent_when_it_is_old_or_a_newer_reply_to_the_same_request_exists(
    manual_clock: ManualClock, ivans_device: Contact
) -> None:
    old_reply = send_reply(ivans_device, "Q:bob", "HT1 q bob 1", manual_clock)
    time_out(old_reply, manual_clock)
    manual_clock.advance(seconds=60)
    assert settle_pending_route_resets(ivans_device.pk, manual_clock.now()).replies_to_resend == ()

    superseded_reply = send_reply(ivans_device, "F:*", "HT1 f * 0", manual_clock)
    time_out(superseded_reply, manual_clock)
    send_reply(ivans_device, "F:*", "HT1 f * 1", manual_clock)

    settlement = settle_pending_route_resets(ivans_device.pk, manual_clock.now())
    assert settlement.needs_reset
    assert settlement.replies_to_resend == ()


@pytest.mark.django_db
def test_a_new_route_learned_after_the_packet_was_queued_settles_it_without_a_reset(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    reply = send_reply(ivans_device, "Q:bob", "HT1 q bob 1", manual_clock)
    time_out(reply, manual_clock)

    assert record_path_update(ivans_device.public_key, manual_clock.now()) == ivans_device.pk

    assert not settle_pending_route_resets(ivans_device.pk, manual_clock.now()).needs_reset
    assert read_route_reset_state(reply) == RouteResetState.SKIPPED_PATH_UPDATE
    assert record_path_update("ff" * 32, manual_clock.now()) is None


@pytest.mark.django_db
def test_only_the_contacts_own_packets_are_settled(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    reply = send_reply(ivans_device, "Q:bob", "HT1 q bob 1", manual_clock)
    time_out(reply, manual_clock)

    assert not settle_pending_route_resets(bobs_device.pk, manual_clock.now()).needs_reset
    assert read_route_reset_state(reply) == RouteResetState.PENDING


@pytest.mark.parametrize(
    ("arrived_by_flood", "seconds_since_path_update", "seconds_since_arrival", "expected_reset"),
    [
        (True, None, 0, True),
        (False, None, 0, False),
        (True, 29, 0, False),
        (True, 31, 0, True),
        (True, None, 61, False),
    ],
    ids=[
        "a new flood arrival",
        "a direct arrival",
        "a route learned seconds ago",
        "a route learned more than thirty seconds ago",
        "a frame processed a minute after it arrived",
    ],
)
def test_a_flood_arrival_resets_the_route_unless_a_route_was_just_learned_or_the_frame_is_old(
    manual_clock: ManualClock,
    arrived_by_flood: bool,
    seconds_since_path_update: int | None,
    seconds_since_arrival: int,
    expected_reset: bool,
) -> None:
    now = manual_clock.now()
    last_path_update_at = (
        now - timedelta(seconds=seconds_since_path_update) if seconds_since_path_update is not None else None
    )

    assert (
        needs_flood_arrival_route_reset(
            arrived_by_flood=arrived_by_flood,
            received_at=now - timedelta(seconds=seconds_since_arrival),
            last_path_update_at=last_path_update_at,
            now=now,
        )
        == expected_reset
    )
