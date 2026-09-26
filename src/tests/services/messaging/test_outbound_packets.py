from datetime import timedelta

import pytest

from directory.models import Contact, User
from messaging.models import Message, MessageDelivery, OutboundPacket
from messaging.outbound_packets import (
    ERR_CODE_NOT_FOUND,
    ERR_CODE_TABLE_FULL,
    PacketOutcomeUnknown,
    PacketQueuedOnNode,
    PacketRejectedByNode,
    is_any_packet_awaiting_node_acknowledgement,
    mark_packets_awaiting_acknowledgement_dropped,
    read_latest_acknowledgement_deadline,
    record_acknowledgement_deadline_passed,
    record_node_acknowledgement,
    record_send_outcome,
    recover_outbound_packets_at_startup,
)
from messaging.outbound_scheduling import PacketDescriptor
from messaging.route_reset_evidence import record_path_update
from tests.invariants import assert_all_invariants
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


def prepare_part_of_a_new_message(relay: RelayHarness, ivans_device: Contact, part_count: int = 2) -> PacketDescriptor:
    for part_number in range(1, part_count + 1):
        relay.receive_replies(ivans_device, f"HT1 M bob 5 {part_number}/{part_count} part {part_number}")
    packet = relay.prepare_next_packet()
    assert packet is not None
    return packet


def queue_on_node(
    relay: RelayHarness,
    packet: PacketDescriptor,
    acknowledgement_code: str = "0a0b0c0d",
    route: OutboundPacket.Route = OutboundPacket.Route.DIRECT,
) -> None:
    relay.record_outcome(
        packet,
        PacketQueuedOnNode(
            route=route, expected_acknowledgement_code=acknowledgement_code, suggested_timeout_milliseconds=5000
        ),
    )


def read_packet(packet: PacketDescriptor) -> OutboundPacket:
    return OutboundPacket.objects.get(id=packet.packet_id)


def read_delivery() -> MessageDelivery:
    return MessageDelivery.objects.get()


@pytest.mark.django_db
def test_a_packet_queued_by_the_node_waits_for_its_acknowledgement_until_its_deadline(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)

    recorded_outcome = record_send_outcome(
        packet.packet_id,
        PacketQueuedOnNode(
            route=OutboundPacket.Route.DIRECT,
            expected_acknowledgement_code="0A0B0C0D",
            suggested_timeout_milliseconds=5000,
        ),
        manual_clock.now(),
    )

    assert recorded_outcome is not None
    assert recorded_outcome.awaits_acknowledgement
    assert recorded_outcome.acknowledgement_deadline_at == manual_clock.now() + timedelta(seconds=6)
    stored_packet = read_packet(packet)
    assert stored_packet.state == OutboundPacket.State.QUEUED_ON_NODE
    assert stored_packet.expected_acknowledgement_code == "0a0b0c0d"
    assert (stored_packet.queued_at, stored_packet.suggested_timeout_milliseconds) == (manual_clock.now(), 5000)
    assert read_delivery().round_pending_parts_mask == 0b10
    assert is_any_packet_awaiting_node_acknowledgement(manual_clock.now())


@pytest.mark.django_db
def test_an_outcome_recorded_twice_changes_nothing_the_second_time(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)
    queue_on_node(relay, packet)
    delivery_before = MessageDelivery.objects.values().get()

    recorded_outcome = record_send_outcome(packet.packet_id, PacketOutcomeUnknown(), manual_clock.now())

    assert recorded_outcome is not None
    assert recorded_outcome.packet_state == OutboundPacket.State.QUEUED_ON_NODE
    assert MessageDelivery.objects.values().get() == delivery_before
    assert record_send_outcome(999_999, PacketOutcomeUnknown(), manual_clock.now()) is None


@pytest.mark.django_db
def test_an_acknowledgement_matches_its_packet_while_it_waits(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)
    queue_on_node(relay, packet)
    manual_clock.advance(seconds=2)

    [acknowledged_packet] = record_node_acknowledgement("0A0B0C0D", 1800, manual_clock.now())

    assert acknowledged_packet.packet_id == packet.packet_id
    assert acknowledged_packet.was_awaiting_acknowledgement
    stored_packet = read_packet(packet)
    assert stored_packet.state == OutboundPacket.State.NODE_ACKNOWLEDGED
    assert (stored_packet.acknowledged_at, stored_packet.round_trip_milliseconds) == (manual_clock.now(), 1800)
    assert read_delivery().state == MessageDelivery.State.PENDING
    assert not is_any_packet_awaiting_node_acknowledgement(manual_clock.now())


@pytest.mark.django_db
def test_a_late_acknowledgement_still_matches_through_the_database_and_settles_the_route_reset(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)
    queue_on_node(relay, packet)
    manual_clock.advance(seconds=7)
    acknowledgement_timeout = record_acknowledgement_deadline_passed(packet.packet_id, manual_clock.now())
    assert acknowledgement_timeout is not None
    assert acknowledgement_timeout.route_reset_pending
    manual_clock.advance(seconds=20)

    [acknowledged_packet] = record_node_acknowledgement("0a0b0c0d", 25_000, manual_clock.now())

    assert not acknowledged_packet.was_awaiting_acknowledgement
    stored_packet = read_packet(packet)
    assert stored_packet.state == OutboundPacket.State.NODE_ACKNOWLEDGED
    assert stored_packet.route_reset_state == RouteResetState.SKIPPED_LATE_ACKNOWLEDGEMENT
    assert stored_packet.route_reset_decided_at == manual_clock.now()


@pytest.mark.django_db
def test_an_acknowledgement_that_arrives_before_the_send_outcome_matches_once_it_is_recorded(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)

    assert record_node_acknowledgement("0a0b0c0d", 300, manual_clock.now()) == ()
    queue_on_node(relay, packet)
    [acknowledged_packet] = record_node_acknowledgement("0a0b0c0d", 300, manual_clock.now())

    assert acknowledged_packet.packet_id == packet.packet_id
    assert record_acknowledgement_deadline_passed(packet.packet_id, manual_clock.now() + timedelta(minutes=1)) is None
    assert read_packet(packet).route_reset_state == RouteResetState.NOT_APPLICABLE


@pytest.mark.django_db
def test_every_packet_sharing_an_acknowledgement_code_is_marked(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 first")
    relay.receive_replies(ivans_device, "HT1 M bob 6 1/1 second")
    first_packet = relay.prepare_next_packet()
    assert first_packet is not None
    queue_on_node(relay, first_packet, acknowledgement_code="deadbeef")
    second_packet = relay.prepare_next_packet()
    assert second_packet is not None
    queue_on_node(relay, second_packet, acknowledgement_code="deadbeef")

    acknowledged_packets = record_node_acknowledgement("deadbeef", 900, manual_clock.now())

    assert {acknowledged.packet_id for acknowledged in acknowledged_packets} == {
        first_packet.packet_id,
        second_packet.packet_id,
    }


@pytest.mark.django_db
def test_an_acknowledgement_older_than_ten_minutes_no_longer_matches(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)
    queue_on_node(relay, packet)
    manual_clock.advance(minutes=10, seconds=1)

    assert record_node_acknowledgement("0a0b0c0d", 900, manual_clock.now()) == ()


@pytest.mark.django_db
def test_a_contact_the_node_does_not_know_is_added_again_and_its_part_stays_in_the_round(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)

    recorded_outcome = record_send_outcome(
        packet.packet_id, PacketRejectedByNode(node_error_code=ERR_CODE_NOT_FOUND), manual_clock.now()
    )

    assert recorded_outcome is not None
    assert recorded_outcome.contact_needs_reconciliation
    assert Contact.objects.get(id=bobs_device.pk).node_sync_state == Contact.NodeSyncState.PENDING_ADD
    assert read_packet(packet).state == OutboundPacket.State.REJECTED_BY_NODE
    assert read_packet(packet).node_error_code == ERR_CODE_NOT_FOUND
    assert read_delivery().round_pending_parts_mask == 0b11
    assert relay.send_due_texts() == []

    Contact.objects.filter(id=bobs_device.pk).update(node_sync_state=Contact.NodeSyncState.ON_NODE)
    assert relay.send_due_texts() == ["HT1 m ivan 5 1/2 part 1", "HT1 m ivan 5 2/2 part 2"]
    assert read_delivery().attempt_count == 1


@pytest.mark.django_db
def test_a_full_packet_pool_keeps_the_part_in_its_round_without_spending_an_attempt(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device)

    recorded_outcome = record_send_outcome(
        packet.packet_id, PacketRejectedByNode(node_error_code=ERR_CODE_TABLE_FULL), manual_clock.now()
    )

    assert recorded_outcome is not None
    assert recorded_outcome.node_packet_pool_full
    assert not recorded_outcome.contact_needs_reconciliation
    manual_clock.advance(seconds=5)
    assert relay.send_due_texts() == ["HT1 m ivan 5 1/2 part 1", "HT1 m ivan 5 2/2 part 2"]
    assert read_delivery().attempt_count == 1
    assert_all_invariants()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "outcome",
    [PacketOutcomeUnknown(), PacketRejectedByNode(node_error_code=1)],
    ids=["a command timeout", "another node error"],
)
def test_an_unknown_outcome_or_another_error_counts_the_part_as_sent_without_a_deadline(
    relay: RelayHarness,
    manual_clock: ManualClock,
    ivans_device: Contact,
    bobs_device: Contact,
    outcome: PacketOutcomeUnknown | PacketRejectedByNode,
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device, part_count=1)

    recorded_outcome = record_send_outcome(packet.packet_id, outcome, manual_clock.now())

    assert recorded_outcome is not None
    assert not recorded_outcome.awaits_acknowledgement
    assert read_packet(packet).acknowledgement_deadline_at is None
    delivery = read_delivery()
    assert (delivery.round_pending_parts_mask, delivery.last_sent_at) == (0, manual_clock.now())
    assert delivery.next_attempt_at == manual_clock.now() + timedelta(seconds=30)


@pytest.mark.django_db
def test_a_missed_deadline_needs_a_route_reset_only_for_a_direct_packet_whose_route_did_not_change(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/3 one")
    relay.receive_replies(ivans_device, "HT1 M bob 5 2/3 two")
    relay.receive_replies(ivans_device, "HT1 M bob 5 3/3 three")
    flooded, direct, direct_with_new_route = (relay.prepare_next_packet_and_queue() for _ in range(3))
    OutboundPacket.objects.filter(id=flooded.packet_id).update(route=OutboundPacket.Route.FLOOD)
    manual_clock.advance(seconds=3)
    record_path_update(bobs_device.public_key, manual_clock.now())
    OutboundPacket.objects.filter(id__in=[flooded.packet_id, direct.packet_id]).update(
        queued_at=manual_clock.now() + timedelta(seconds=1)
    )
    manual_clock.advance(seconds=10)

    outcomes = [
        record_acknowledgement_deadline_passed(packet.packet_id, manual_clock.now())
        for packet in (flooded, direct, direct_with_new_route)
    ]

    assert [outcome.route_reset_pending if outcome else None for outcome in outcomes] == [False, True, False]
    assert [read_packet(packet).route_reset_state for packet in (flooded, direct, direct_with_new_route)] == [
        RouteResetState.NOT_APPLICABLE,
        RouteResetState.PENDING,
        RouteResetState.SKIPPED_PATH_UPDATE,
    ]
    assert {read_packet(packet).state for packet in (flooded, direct, direct_with_new_route)} == {
        OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT
    }


@pytest.mark.django_db
def test_after_a_restart_a_prepared_part_is_sent_again_as_a_new_packet_with_the_attempt_unchanged(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device, part_count=1)
    manual_clock.advance(seconds=30)

    recovery = recover_outbound_packets_at_startup(manual_clock.now())

    assert recovery.prepared_packets_now_unknown == 1
    assert read_packet(packet).state == OutboundPacket.State.OUTCOME_UNKNOWN
    assert relay.send_due_texts() == ["HT1 m ivan 5 1/1 part 1"]
    assert OutboundPacket.objects.count() == 2
    assert Message.objects.count() == 1
    assert read_delivery().attempt_count == 1
    assert_all_invariants()


@pytest.mark.django_db
def test_a_restart_drops_the_acknowledgement_waits_and_pending_route_resets_but_keeps_the_deadlines(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/2 one")
    relay.receive_replies(ivans_device, "HT1 M bob 5 2/2 two")
    timed_out_packet = relay.prepare_next_packet_and_queue()
    waiting_packet = relay.prepare_next_packet_and_queue()
    manual_clock.advance(seconds=1)
    record_acknowledgement_deadline_passed(timed_out_packet.packet_id, manual_clock.now())

    recovery = recover_outbound_packets_at_startup(manual_clock.now())

    assert (recovery.queued_packets_dropped, recovery.route_resets_dropped) == (1, 1)
    for packet in (timed_out_packet, waiting_packet):
        stored_packet = read_packet(packet)
        assert stored_packet.state == OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT
        assert stored_packet.route_reset_state == RouteResetState.DROPPED_BY_RESTART
    assert is_any_packet_awaiting_node_acknowledgement(manual_clock.now())
    assert record_node_acknowledgement(f"{waiting_packet.sender_timestamp:08x}", 700, manual_clock.now())


@pytest.mark.django_db
def test_a_lost_link_drops_the_acknowledgement_waits_without_route_decisions(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    packet = prepare_part_of_a_new_message(relay, ivans_device, part_count=1)
    queue_on_node(relay, packet)

    assert mark_packets_awaiting_acknowledgement_dropped(manual_clock.now()) == 1

    assert read_packet(packet).route_reset_state == RouteResetState.DROPPED_BY_RESTART
    assert record_acknowledgement_deadline_passed(packet.packet_id, manual_clock.now()) is None
    assert is_any_packet_awaiting_node_acknowledgement(manual_clock.now())
    assert not is_any_packet_awaiting_node_acknowledgement(manual_clock.now() + timedelta(seconds=7))


@pytest.mark.django_db
def test_the_latest_acknowledgement_deadline_counts_only_the_packets_still_awaiting_one(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/2 one")
    relay.receive_replies(ivans_device, "HT1 M bob 5 2/2 two")
    earlier_packet = relay.prepare_next_packet_and_queue()
    manual_clock.advance(seconds=2)
    later_packet = relay.prepare_next_packet_and_queue()
    later_deadline = read_packet(later_packet).acknowledgement_deadline_at

    assert read_latest_acknowledgement_deadline(manual_clock.now()) == later_deadline

    record_node_acknowledgement(f"{later_packet.sender_timestamp:08x}", 900, manual_clock.now())

    earlier_deadline = read_packet(earlier_packet).acknowledgement_deadline_at
    assert earlier_deadline is not None
    assert read_latest_acknowledgement_deadline(manual_clock.now()) == earlier_deadline
    assert read_latest_acknowledgement_deadline(earlier_deadline) is None
