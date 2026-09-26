"""Routes, firmware ACKs, the nodes' packet pools and the relay's pacing, end to end."""

import asyncio
import functools
import itertools
from collections import Counter
from datetime import timedelta

import pytest

from messaging.models import InboundDirectMessage, MessageDelivery, OutboundPacket, ReceiptNotification
from tests.invariants import assert_all_invariants
from tests.scenarios.delivery_receipts_routes_helpers import (
    CERTAIN_LOSS_PROBABILITY,
    calculate_expected_packet_pool_back_off,
    count_packets_awaiting_acknowledgement_at,
    count_route_resets,
    find_largest_overlap,
    find_receipt,
    find_sent_text_command,
    give_direct_routes_between_relay_and,
    has_delivery_completed_round,
    inject_channel_traffic_after_each_direct_message,
    list_route_reset_times,
    list_sent_text_commands,
    read_contact,
    read_contact_id,
    read_delivery,
    read_delivery_packets,
    read_highest_packet_id,
    read_inbox_rows_from,
    read_message,
    read_packets,
    record_acknowledgement_lookups,
    record_drain_outcomes,
    relay_knows_route_to,
    start_relay_with_devices,
    start_signed_in_client,
    wait_until_relay_is_quiet,
    wait_until_worker_clock_passes,
)
from tests.scenarios.scenario_settings import SCENARIO_CLIENT_TIMING, SCENARIO_ENGINE_TIMING, SCENARIO_PACING
from tests.scenarios.scenario_setup import ClientStarter
from tests.worker.fake_node.fake_companion_firmware import (
    FakeCompanionFirmware,
    MessageSentReplyOrder,
    ReceptionOutcome,
)
from tests.worker.fake_node.frames import DIRECT_ARRIVAL_PATH_LENGTH, FirmwareErrorCode, PushCode
from tests.worker.fake_node.radio_packets import DirectMessagePacket
from tests.worker.fake_node.simulated_mesh import DeliveryOutcome, LinkPolicy, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database, wait_for_database
from tests.worker.simulated_hoptalk_client_records import OutgoingMessageStatus
from worker.node_gateway import NextMessageOutcome

pytestmark = pytest.mark.django_db(transaction=True)

RouteResetState = OutboundPacket.RouteResetState
PacketState = OutboundPacket.State
PacketRoute = OutboundPacket.Route
PacketPurpose = OutboundPacket.Purpose

FIRST_TEXT = "Morning! The boat leaves at nine."
SECOND_TEXT = "Bring the red life jacket, please."
THIRD_TEXT = "And sandwiches for four."
# 224 bytes: three parts of at most 104 bytes.
THREE_PART_TEXT = (
    "The survey team reached the ridge at noon. Visibility is poor, so we are waiting for the fog "
    "to lift before crossing the saddle. Batteries are at sixty percent and the spare radio works. "
    "Next check-in at three, from the hut."
)
# On the air in each direction, as a real hop takes: longer than the worker needs to process a direct message.
RADIO_HOP_MINIMUM_DELAY_SECONDS = 0.02
RADIO_HOP_MAXIMUM_DELAY_SECONDS = 0.03
# Longer than a direct packet waits for its firmware ACK, shorter than the pause before the next round.
LATE_ACKNOWLEDGEMENT_MINIMUM_DELAY_SECONDS = 0.18
LATE_ACKNOWLEDGEMENT_MAXIMUM_DELAY_SECONDS = 0.22
FIRST_REPEATER_IDENTIFIER = bytes.fromhex("a1a1a1")
SECOND_REPEATER_IDENTIFIER = bytes.fromhex("b2b2b2")
MINIMUM_REFUSED_HAND_OFFS = 3
REJECTIONS_PER_BURST = 4
CHANNEL_DATAGRAMS_PER_DIRECT_MESSAGE = 3
BURST_RECIPIENT_USERNAMES = ("bob", "carol", "dave", "erin", "frank")
MESSAGES_PER_BURST_RECIPIENT = 4
BURST_TIMEOUT_SECONDS = 20.0
MAXIMUM_PACKETS_AWAITING_ACKNOWLEDGEMENT = SCENARIO_PACING.maximum_packets_awaiting_node_acknowledgement
MAXIMUM_ACTIVE_DELIVERIES_PER_DEVICE = SCENARIO_PACING.maximum_active_deliveries_per_device
MINIMUM_GAP_BETWEEN_SENDS_SECONDS = SCENARIO_PACING.minimum_seconds_between_sends
RECENT_PATH_UPDATE_WINDOW = timedelta(seconds=SCENARIO_ENGINE_TIMING.recent_path_update_seconds)


def is_packet_in_state(packet_id: int, state: OutboundPacket.State) -> bool:
    return OutboundPacket.objects.filter(id=packet_id, state=state).exists()


def has_rejected_delivery_packets(message_id: int, device_id: int, minimum_count: int) -> bool:
    rejected_packet_count = OutboundPacket.objects.filter(
        message_delivery__message_id=message_id,
        message_delivery__device_id=device_id,
        state=PacketState.REJECTED_BY_NODE,
    ).count()
    return rejected_packet_count >= minimum_count


def is_receipt_confirmed(message_id: int, device_id: int) -> bool:
    receipt = find_receipt(message_id, device_id)
    return receipt is not None and receipt.state == ReceiptNotification.State.CONFIRMED


def is_every_receipt_confirmed() -> bool:
    return not ReceiptNotification.objects.exclude(state=ReceiptNotification.State.CONFIRMED).exists()


def is_waiting_for_a_place(device_id: int) -> bool:
    """Every place for a delivery in progress to the device is taken, and one more delivery to it waits."""
    pending_deliveries = MessageDelivery.objects.filter(device_id=device_id, state=MessageDelivery.State.PENDING)
    deliveries_in_progress = pending_deliveries.filter(round_started_at__isnull=False).count()
    deliveries_waiting = pending_deliveries.filter(round_started_at__isnull=True).count()
    return deliveries_in_progress == MAXIMUM_ACTIVE_DELIVERIES_PER_DEVICE and deliveries_waiting == 1


def read_deliveries_to(device_id: int) -> list[MessageDelivery]:
    return list(MessageDelivery.objects.filter(device_id=device_id).select_related("message").order_by("id"))


def read_inbound_rows_from_unknown_senders() -> list[InboundDirectMessage]:
    return list(InboundDirectMessage.objects.filter(classification=InboundDirectMessage.Classification.UNKNOWN_SENDER))


def takes_channel_traffic_between_direct_messages(drain_pass: list[NextMessageOutcome]) -> bool:
    direct_message_positions = [
        position
        for position, next_message_outcome in enumerate(drain_pass)
        if next_message_outcome == NextMessageOutcome.DIRECT_MESSAGE
    ]
    if len(direct_message_positions) < 2:
        return False
    between_direct_messages = drain_pass[direct_message_positions[0] + 1 : direct_message_positions[-1]]
    return NextMessageOutcome.CHANNEL_TRAFFIC in between_direct_messages


async def test_when_only_the_acks_back_are_lost_the_devices_status_spares_the_route_and_later_messages_stay_direct(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_device = devices["bob-phone"]
    bob_device_id = await in_database(read_contact_id, bob_device.public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    give_direct_routes_between_relay_and(fake_companion_firmware, bob_device)
    route_resets_before = list_route_reset_times(fake_companion_firmware, bob_device)
    highest_packet_id_before = await in_database(read_highest_packet_id)

    bob_device.uplink = LinkPolicy(acknowledgement_loss_probability=CERTAIN_LOSS_PROBABILITY)
    first_message = alice.send_message("bob", FIRST_TEXT)
    await first_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    second_message = alice.send_message("bob", SECOND_TEXT)
    await second_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    bob_device.uplink = LinkPolicy()
    third_message = alice.send_message("bob", THIRD_TEXT)
    await third_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    await relay_worker.stop()

    packets_to_bob = await in_database(read_packets, contact_id=bob_device_id, id__gt=highest_packet_id_before)
    assert [packet.purpose for packet in packets_to_bob] == [PacketPurpose.DELIVERY] * 3
    assert [packet.route for packet in packets_to_bob] == [PacketRoute.DIRECT] * 3
    assert [(packet.state, packet.route_reset_state) for packet in packets_to_bob] == [
        (PacketState.ACKNOWLEDGEMENT_TIMED_OUT, RouteResetState.SKIPPED_APPLICATION_EVIDENCE),
        (PacketState.ACKNOWLEDGEMENT_TIMED_OUT, RouteResetState.SKIPPED_APPLICATION_EVIDENCE),
        (PacketState.NODE_ACKNOWLEDGED, RouteResetState.NOT_APPLICABLE),
    ]
    assert list_route_reset_times(fake_companion_firmware, bob_device) == route_resets_before
    status_rows = await in_database(read_inbox_rows_from, bob_device_id, "HT1 K alice ")
    assert [row.path_length for row in status_rows] == [DIRECT_ARRIVAL_PATH_LENGTH] * 3
    received_messages = bob.received_messages("alice")
    assert [received.text for received in received_messages] == [FIRST_TEXT, SECOND_TEXT, THIRD_TEXT]
    assert [received.part_copies_received for received in received_messages] == [1, 1, 1]
    for message in (first_message, second_message, third_message):
        stored_message = await in_database(read_message, "alice", message.message_id)
        delivery = await in_database(read_delivery, stored_message.pk, bob_device_id)
        assert (delivery.state, delivery.attempt_count) == (MessageDelivery.State.DELIVERED, 1)
    await in_database(assert_all_invariants)


async def test_a_status_that_floods_while_the_acks_back_are_lost_teaches_the_route_again_and_later_messages_go_direct(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_device = devices["bob-phone"]
    bob_device_id = await in_database(read_contact_id, bob_device.public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    give_direct_routes_between_relay_and(fake_companion_firmware, bob_device)
    highest_packet_id_before = await in_database(read_highest_packet_id)
    bob_contact = await in_database(read_contact, bob_device.public_key)
    if bob_contact.last_path_update_at is not None:
        await wait_until_worker_clock_passes(relay_worker, bob_contact.last_path_update_at + RECENT_PATH_UPDATE_WINDOW)

    # Bob's node forgets its route to the relay, so its firmware ACK and its status both flood.
    assert bob_device.reset_route_to_relay()
    bob_device.uplink = LinkPolicy(
        acknowledgement_loss_probability=CERTAIN_LOSS_PROBABILITY,
        minimum_delay_seconds=RADIO_HOP_MINIMUM_DELAY_SECONDS,
        maximum_delay_seconds=RADIO_HOP_MAXIMUM_DELAY_SECONDS,
    )
    bob_device.downlink = LinkPolicy(
        minimum_delay_seconds=RADIO_HOP_MINIMUM_DELAY_SECONDS, maximum_delay_seconds=RADIO_HOP_MAXIMUM_DELAY_SECONDS
    )
    first_message = alice.send_message("bob", FIRST_TEXT)
    await first_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    second_message = alice.send_message("bob", SECOND_TEXT)
    await second_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    await relay_worker.stop()

    packets_to_bob = await in_database(read_packets, contact_id=bob_device_id, id__gt=highest_packet_id_before)
    first_status_row = (await in_database(read_inbox_rows_from, bob_device_id, "HT1 K alice "))[0]
    assert first_status_row.path_length != DIRECT_ARRIVAL_PATH_LENGTH
    assert [packet.purpose for packet in packets_to_bob] == [PacketPurpose.DELIVERY] * 2
    assert [packet.route for packet in packets_to_bob] == [PacketRoute.DIRECT] * 2
    first_packet = packets_to_bob[0]
    assert first_packet.state == PacketState.ACKNOWLEDGEMENT_TIMED_OUT
    # The flood exchange made Bob's node send its route back, which the relay's node stored as new.
    assert first_packet.route_reset_state == RouteResetState.SKIPPED_PATH_UPDATE
    assert not any(packet.route_reset_state == RouteResetState.PERFORMED for packet in packets_to_bob)
    assert relay_knows_route_to(fake_companion_firmware, bob_device)
    assert [received.text for received in bob.received_messages("alice")] == [FIRST_TEXT, SECOND_TEXT]
    for message in (first_message, second_message):
        stored_message = await in_database(read_message, "alice", message.message_id)
        delivery = await in_database(read_delivery, stored_message.pk, bob_device_id)
        assert (delivery.state, delivery.attempt_count) == (MessageDelivery.State.DELIVERED, 1)
    await in_database(assert_all_invariants)


async def test_a_direct_send_over_a_stale_route_is_followed_by_a_route_reset_and_a_flooded_round_that_is_acknowledged(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_device = devices["bob-phone"]
    bob_device_id = await in_database(read_contact_id, bob_device.public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    bob_device.change_route_to_relay([FIRST_REPEATER_IDENTIFIER])
    give_direct_routes_between_relay_and(fake_companion_firmware, bob_device)
    route_resets_before = list_route_reset_times(fake_companion_firmware, bob_device)

    # Bob moved behind another repeater: the route both nodes stored leads nowhere now.
    bob_device.change_route_to_relay([SECOND_REPEATER_IDENTIFIER])
    message = alice.send_message("bob", FIRST_TEXT)
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    await relay_worker.stop()

    stored_message = await in_database(read_message, "alice", message.message_id)
    delivery = await in_database(read_delivery, stored_message.pk, bob_device_id)
    [stale_packet, flooded_packet] = await in_database(read_delivery_packets, delivery.pk)
    assert (stale_packet.attempt_number, stale_packet.route) == (1, PacketRoute.DIRECT)
    assert stale_packet.state == PacketState.ACKNOWLEDGEMENT_TIMED_OUT
    assert stale_packet.route_reset_state == RouteResetState.PERFORMED
    assert stale_packet.acknowledgement_deadline_at is not None
    assert stale_packet.route_reset_decided_at is not None
    assert flooded_packet.queued_at is not None
    assert stale_packet.acknowledgement_deadline_at <= stale_packet.route_reset_decided_at <= flooded_packet.queued_at
    assert (flooded_packet.attempt_number, flooded_packet.route) == (2, PacketRoute.FLOOD)
    assert flooded_packet.state == PacketState.NODE_ACKNOWLEDGED
    assert flooded_packet.acknowledged_at is not None
    assert flooded_packet.acknowledgement_deadline_at is not None
    assert flooded_packet.acknowledged_at <= flooded_packet.acknowledgement_deadline_at
    assert flooded_packet.route_reset_state == RouteResetState.NOT_APPLICABLE

    [stale_packet_traffic] = [
        traffic_record
        for traffic_record in simulated_mesh.traffic(
            sender=fake_companion_firmware.label, recipient=bob_device.name, packet_type=DirectMessagePacket
        )
        if isinstance(traffic_record.packet, DirectMessagePacket)
        and traffic_record.packet.sender_timestamp == stale_packet.sender_timestamp
    ]
    assert stale_packet_traffic.outcome == DeliveryOutcome.LOST_ON_STALE_ROUTE
    stale_packet_sent_at = find_sent_text_command(fake_companion_firmware, stale_packet).received_at
    flooded_packet_sent_at = find_sent_text_command(fake_companion_firmware, flooded_packet).received_at
    route_resets_after = list_route_reset_times(fake_companion_firmware, bob_device)[len(route_resets_before) :]
    assert all(reset_time > stale_packet_sent_at for reset_time in route_resets_after)
    assert [
        reset_time for reset_time in route_resets_after if stale_packet_sent_at < reset_time < flooded_packet_sent_at
    ] == route_resets_after[:1]

    assert (delivery.state, delivery.attempt_count) == (MessageDelivery.State.DELIVERED, 2)
    assert received_message.text == FIRST_TEXT
    assert received_message.part_copies_received == 1
    await in_database(assert_all_invariants)


async def test_acks_that_arrive_before_their_message_sent_reply_are_matched_later_without_timeouts_resets_or_resends(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    for device in devices.values():
        give_direct_routes_between_relay_and(fake_companion_firmware, device)
    route_reset_count_before = count_route_resets(fake_companion_firmware)
    highest_packet_id_before = await in_database(read_highest_packet_id)
    acknowledgement_lookups = record_acknowledgement_lookups(monkeypatch)

    fake_companion_firmware.message_sent_reply_order = MessageSentReplyOrder.AFTER_ACKNOWLEDGEMENT
    message = alice.send_message("bob", FIRST_TEXT)
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    read_confirmation = bob.mark_read("alice", received_message.message_id)
    await read_confirmation.wait_until_finished()
    await message.wait_for_status(OutgoingMessageStatus.READ)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    unmatched_acknowledgement_count = relay_worker.worker.acknowledgement_tracker.count_unmatched_acknowledgements()
    await relay_worker.stop()

    packets = await in_database(read_packets, id__gt=highest_packet_id_before)
    assert {PacketPurpose.REPLY, PacketPurpose.DELIVERY, PacketPurpose.RECEIPT} <= {
        packet.purpose for packet in packets
    }
    for packet in packets:
        assert packet.route == PacketRoute.DIRECT, packet
        assert packet.state == PacketState.NODE_ACKNOWLEDGED, packet
        assert packet.acknowledged_at is not None
        assert packet.acknowledgement_deadline_at is not None
        assert packet.acknowledged_at <= packet.acknowledgement_deadline_at, packet
        assert packet.route_reset_state == RouteResetState.NOT_APPLICABLE, packet
        matched_packet_ids_per_lookup = [
            lookup.matched_packet_ids
            for lookup in acknowledgement_lookups
            if lookup.code == packet.expected_acknowledgement_code
        ]
        assert matched_packet_ids_per_lookup[0] == (), f"{packet} was matched before its MSG_SENT was recorded"
        assert (packet.pk,) in matched_packet_ids_per_lookup[1:], f"{packet} was never matched from the buffer"
    reply_packet_counts = Counter(packet.reply_key for packet in packets if packet.purpose == PacketPurpose.REPLY)
    assert set(reply_packet_counts.values()) == {1}
    assert count_route_resets(fake_companion_firmware) == route_reset_count_before
    assert unmatched_acknowledgement_count == 0
    await in_database(assert_all_invariants)


async def test_a_firmware_ack_after_its_deadline_marks_the_packet_acknowledged_and_settles_its_route_reset_as_skipped(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_device = devices["bob-phone"]
    bob_device_id = await in_database(read_contact_id, bob_device.public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    give_direct_routes_between_relay_and(fake_companion_firmware, bob_device)
    route_resets_before = list_route_reset_times(fake_companion_firmware, bob_device)
    highest_packet_id_before = await in_database(read_highest_packet_id)

    bob_device.uplink = LinkPolicy(
        minimum_delay_seconds=LATE_ACKNOWLEDGEMENT_MINIMUM_DELAY_SECONDS,
        maximum_delay_seconds=LATE_ACKNOWLEDGEMENT_MAXIMUM_DELAY_SECONDS,
    )
    first_message = alice.send_message("bob", FIRST_TEXT)
    await first_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    [late_packet] = await in_database(read_packets, contact_id=bob_device_id, id__gt=highest_packet_id_before)
    await wait_for_database(
        lambda: is_packet_in_state(late_packet.pk, PacketState.NODE_ACKNOWLEDGED),
        description="the late firmware ACK to be matched",
    )
    bob_device.uplink = LinkPolicy()
    second_message = alice.send_message("bob", SECOND_TEXT)
    await second_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    await relay_worker.stop()

    [late_packet, timely_packet] = await in_database(
        read_packets, contact_id=bob_device_id, id__gt=highest_packet_id_before
    )
    assert late_packet.route == PacketRoute.DIRECT
    assert late_packet.state == PacketState.NODE_ACKNOWLEDGED
    assert late_packet.acknowledged_at is not None
    assert late_packet.acknowledgement_deadline_at is not None
    assert late_packet.acknowledged_at > late_packet.acknowledgement_deadline_at
    assert late_packet.route_reset_state == RouteResetState.SKIPPED_LATE_ACKNOWLEDGEMENT
    assert late_packet.route_reset_decided_at == late_packet.acknowledged_at
    assert timely_packet.route == PacketRoute.DIRECT
    assert timely_packet.state == PacketState.NODE_ACKNOWLEDGED
    assert timely_packet.acknowledged_at is not None
    assert timely_packet.acknowledgement_deadline_at is not None
    assert timely_packet.acknowledged_at <= timely_packet.acknowledgement_deadline_at
    assert timely_packet.route_reset_state == RouteResetState.NOT_APPLICABLE
    late_packet_sent_at = find_sent_text_command(fake_companion_firmware, late_packet).received_at
    timely_packet_sent_at = find_sent_text_command(fake_companion_firmware, timely_packet).received_at
    route_resets_after = list_route_reset_times(fake_companion_firmware, bob_device)[len(route_resets_before) :]
    assert not [reset_time for reset_time in route_resets_after if reset_time < timely_packet_sent_at], (
        "the relay reset its route to Bob before the next packet, although the late ACK showed the route works"
    )
    assert late_packet_sent_at < timely_packet_sent_at
    assert [received.text for received in bob.received_messages("alice")] == [FIRST_TEXT, SECOND_TEXT]
    for message in (first_message, second_message):
        stored_message = await in_database(read_message, "alice", message.message_id)
        delivery = await in_database(read_delivery, stored_message.pk, bob_device_id)
        assert (delivery.state, delivery.attempt_count) == (MessageDelivery.State.DELIVERED, 1)
    await in_database(assert_all_invariants)


async def test_a_client_whose_node_packet_pool_is_full_waits_and_resends_with_new_timestamps_until_all_is_delivered(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    alice_device_id = await in_database(read_contact_id, devices["alice-phone"].public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])

    # Heavy traffic around Alice's node holds every packet buffer it has.
    alice_firmware = devices["alice-phone"].firmware
    alice_firmware.occupy_packet_pool(alice_firmware.capacities.packet_pool_packets)
    short_message = alice.send_message("bob", FIRST_TEXT)
    long_message = alice.send_message("bob", THREE_PART_TEXT)
    await alice.wait_until(
        lambda: alice.counters.table_full_rejections >= MINIMUM_REFUSED_HAND_OFFS,
        description="Alice's node to refuse several hand-offs",
    )
    alice_firmware.release_packet_pool()
    await bob.wait_for_received_messages("alice", 2)
    await short_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await long_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    await relay_worker.stop()

    part_texts = [
        f"HT1 M bob {message.message_id} {part_number}/{message.part_count} {part_text}"
        for message in (short_message, long_message)
        for part_number, part_text in enumerate(message.parts, start=1)
    ]
    refused_hand_offs = alice.refused_hand_offs
    assert len(refused_hand_offs) >= MINIMUM_REFUSED_HAND_OFFS
    assert {refused.node_error_code for refused in refused_hand_offs} == {FirmwareErrorCode.TABLE_FULL}
    assert {refused.text for refused in refused_hand_offs} == {part_texts[0]}
    for earlier_refusal, later_refusal in itertools.pairwise(refused_hand_offs):
        assert (
            later_refusal.attempted_at >= earlier_refusal.attempted_at + SCENARIO_CLIENT_TIMING.table_full_wait_seconds
        )
    hand_off_attempts = sorted(
        [(refused.attempted_at, refused.meshcore_timestamp) for refused in refused_hand_offs]
        + [(sent.handed_to_node_at, sent.meshcore_timestamp) for sent in alice.sent_direct_messages]
    )
    meshcore_timestamps = [meshcore_timestamp for _, meshcore_timestamp in hand_off_attempts]
    assert meshcore_timestamps == sorted(set(meshcore_timestamps))
    assert alice.sent_texts("M") == part_texts
    assert (short_message.retry_rounds, long_message.retry_rounds) == (0, 0)
    part_rows = await in_database(read_inbox_rows_from, alice_device_id, "HT1 M bob ")
    assert [row.text for row in part_rows] == part_texts
    assert [row.duplicate_count for row in part_rows] == [0] * len(part_texts)
    assert [received.text for received in bob.received_messages("alice")] == [FIRST_TEXT, THREE_PART_TEXT]
    await in_database(assert_all_invariants)


async def test_packet_pool_bursts_on_the_relay_node_make_it_back_off_without_spending_attempts_and_all_is_delivered(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_device = devices["bob-phone"]
    alice_device_id = await in_database(read_contact_id, devices["alice-phone"].public_key)
    bob_device_id = await in_database(read_contact_id, bob_device.public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])

    burst_messages = []
    for burst_text in (FIRST_TEXT, SECOND_TEXT):
        bob_device.switch_off()
        message = alice.send_message("bob", burst_text)
        await message.wait_for_status(OutgoingMessageStatus.SENT)
        stored_message = await in_database(read_message, "alice", message.message_id)
        await wait_for_database(
            functools.partial(has_delivery_completed_round, stored_message.pk, bob_device_id, 1),
            description="the first round to the switched-off device",
        )
        # Heavy traffic around the relay's node holds every packet buffer it has.
        fake_companion_firmware.occupy_packet_pool(fake_companion_firmware.capacities.packet_pool_packets)
        bob_device.switch_on()
        await wait_for_database(
            functools.partial(has_rejected_delivery_packets, stored_message.pk, bob_device_id, REJECTIONS_PER_BURST),
            description="the relay's node to refuse the next round several times",
        )
        fake_companion_firmware.release_packet_pool()
        await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
        await wait_for_database(
            functools.partial(is_receipt_confirmed, stored_message.pk, alice_device_id),
            description="Alice's phone to confirm the delivered receipt",
        )
        burst_messages.append(message)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    await relay_worker.stop()

    for message in burst_messages:
        stored_message = await in_database(read_message, "alice", message.message_id)
        delivery = await in_database(read_delivery, stored_message.pk, bob_device_id)
        delivery_packets = await in_database(read_delivery_packets, delivery.pk)
        rejected_packets = [packet for packet in delivery_packets if packet.state == PacketState.REJECTED_BY_NODE]
        assert (delivery.state, delivery.attempt_count) == (MessageDelivery.State.DELIVERED, 2)
        assert len(rejected_packets) >= REJECTIONS_PER_BURST
        assert [packet.state for packet in delivery_packets] == [
            PacketState.ACKNOWLEDGEMENT_TIMED_OUT,
            *[PacketState.REJECTED_BY_NODE] * len(rejected_packets),
            PacketState.NODE_ACKNOWLEDGED,
        ]
        assert [packet.attempt_number for packet in delivery_packets] == [1] + [2] * (len(delivery_packets) - 1)
        assert {packet.arm_generation for packet in delivery_packets} == {delivery.arm_generation}
        assert {packet.node_error_code for packet in rejected_packets} == {FirmwareErrorCode.TABLE_FULL}
        assert all(packet.queued_at is None for packet in rejected_packets)
        assert {packet.part_number for packet in delivery_packets} == {1}

    all_packets = await in_database(read_packets)
    consecutive_full_packet_pools = 0
    for packet, next_packet in itertools.pairwise(all_packets):
        if packet.state != PacketState.REJECTED_BY_NODE:
            consecutive_full_packet_pools = 0
            continue
        consecutive_full_packet_pools += 1
        assert packet.prepared_at is not None
        assert next_packet.prepared_at is not None
        assert next_packet.prepared_at - packet.prepared_at >= calculate_expected_packet_pool_back_off(
            consecutive_full_packet_pools
        ), f"the send after {consecutive_full_packet_pools} full packet pools in a row did not wait"
    assert [received.text for received in bob.received_messages("alice")] == [FIRST_TEXT, SECOND_TEXT]
    assert all(message.status is OutgoingMessageStatus.DELIVERED for message in burst_messages)
    await in_database(assert_all_invariants)


async def test_a_burst_of_twenty_deliveries_to_five_devices_keeps_the_ack_places_the_send_gap_and_the_per_device_cap(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    recipient_device_names = {username: f"{username}-phone" for username in BURST_RECIPIENT_USERNAMES}
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", *recipient_device_names.values()
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    recipients = {
        username: await start_signed_in_client(start_client, devices[device_name], username)
        for username, device_name in recipient_device_names.items()
    }
    recipient_device_ids = {
        username: await in_database(read_contact_id, devices[device_name].public_key)
        for username, device_name in recipient_device_names.items()
    }
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, *recipients.values()])

    # The recipients' firmware ACKs are lost and their apps answer nothing, so deliveries pile up and every
    # packet to them holds its place until its deadline.
    for device_name in recipient_device_names.values():
        devices[device_name].uplink = LinkPolicy(acknowledgement_loss_probability=CERTAIN_LOSS_PROBABILITY)
        devices[device_name].phone_leaves()
    burst_texts = {
        username: [
            f"Burst message {message_number} for {username}"
            for message_number in range(1, MESSAGES_PER_BURST_RECIPIENT + 1)
        ]
        for username in BURST_RECIPIENT_USERNAMES
    }
    sent_messages = [
        alice.send_message(username, burst_texts[username][message_index])
        for message_index in range(MESSAGES_PER_BURST_RECIPIENT)
        for username in BURST_RECIPIENT_USERNAMES
    ]
    for message in sent_messages:
        await message.wait_for_status(OutgoingMessageStatus.SENT, timeout_seconds=BURST_TIMEOUT_SECONDS)
    for username, device_id in recipient_device_ids.items():
        await wait_for_database(
            functools.partial(is_waiting_for_a_place, device_id),
            timeout_seconds=BURST_TIMEOUT_SECONDS,
            description=f"a delivery to {username} to wait for a place",
        )
    for device_name in recipient_device_names.values():
        devices[device_name].uplink = LinkPolicy()
        devices[device_name].phone_returns()
    for recipient in recipients.values():
        await recipient.wait_for_received_messages(
            "alice", MESSAGES_PER_BURST_RECIPIENT, timeout_seconds=BURST_TIMEOUT_SECONDS
        )
    for message in sent_messages:
        await message.wait_for_status(OutgoingMessageStatus.DELIVERED, timeout_seconds=BURST_TIMEOUT_SECONDS)
    await wait_for_database(
        is_every_receipt_confirmed, timeout_seconds=BURST_TIMEOUT_SECONDS, description="every receipt confirmed"
    )
    await wait_until_relay_is_quiet(
        relay_worker, simulated_mesh, [alice, *recipients.values()], timeout_seconds=BURST_TIMEOUT_SECONDS
    )
    await relay_worker.stop()

    all_packets = await in_database(read_packets)
    packets_other_than_replies = [packet for packet in all_packets if packet.purpose != PacketPurpose.REPLY]
    for packet in all_packets:
        assert packet.prepared_at is not None
        packets_awaiting = count_packets_awaiting_acknowledgement_at(all_packets, packet.prepared_at)
        # The last place is kept for replies: anything else starts only while two places are free.
        packets_awaiting_limit = (
            MAXIMUM_PACKETS_AWAITING_ACKNOWLEDGEMENT
            if packet.purpose == PacketPurpose.REPLY
            else MAXIMUM_PACKETS_AWAITING_ACKNOWLEDGEMENT - 1
        )
        assert packets_awaiting < packets_awaiting_limit, f"{packet} was sent while {packets_awaiting} awaited an ACK"
    largest_awaiting_before_a_delivery_or_receipt = max(
        count_packets_awaiting_acknowledgement_at(packets_other_than_replies, packet.prepared_at)
        for packet in packets_other_than_replies
        if packet.prepared_at is not None
    )
    assert largest_awaiting_before_a_delivery_or_receipt == MAXIMUM_PACKETS_AWAITING_ACKNOWLEDGEMENT - 2, (
        "the burst never filled every place that packets other than replies may take"
    )

    sent_text_commands = list_sent_text_commands(fake_companion_firmware)
    assert len(sent_text_commands) == len(all_packets)
    for earlier_command, later_command in itertools.pairwise(sent_text_commands):
        assert later_command.received_at - earlier_command.received_at >= MINIMUM_GAP_BETWEEN_SENDS_SECONDS

    for username, device_id in recipient_device_ids.items():
        deliveries = await in_database(read_deliveries_to, device_id)
        assert len(deliveries) == MESSAGES_PER_BURST_RECIPIENT
        spans_in_progress = []
        for delivery in deliveries:
            first_packet = (await in_database(read_delivery_packets, delivery.pk))[0]
            assert delivery.state == MessageDelivery.State.DELIVERED
            assert first_packet.prepared_at is not None
            assert delivery.delivered_at is not None
            spans_in_progress.append((first_packet.prepared_at, delivery.delivered_at))
        assert find_largest_overlap(spans_in_progress) == MAXIMUM_ACTIVE_DELIVERIES_PER_DEVICE, username
        spans_in_progress.sort()
        last_started_delivery_start = spans_in_progress[-1][0]
        earliest_end_of_the_others = min(span_end for _, span_end in spans_in_progress[:-1])
        assert last_started_delivery_start >= earliest_end_of_the_others, username
        received_texts = [received.text for received in recipients[username].received_messages("alice")]
        assert received_texts == burst_texts[username]
    assert all(message.status is OutgoingMessageStatus.DELIVERED for message in sent_messages)
    await in_database(assert_all_invariants)


async def test_channel_datagrams_between_direct_messages_in_the_node_queue_are_drained_without_timeouts_or_reconnects(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    alice_device_id = await in_database(read_contact_id, devices["alice-phone"].public_key)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    connection_generation = relay_worker.runtime_status.connection_generation
    drain_outcome_recorder = record_drain_outcomes(monkeypatch, relay_worker)
    highest_packet_id_before = await in_database(read_highest_packet_id)

    part_count = 3
    channel_frames_per_direct_message = CHANNEL_DATAGRAMS_PER_DIRECT_MESSAGE + 1
    # The node's MESSAGES_WAITING pushes for these frames are lost, so the frames wait in its queue together
    # until the push for one more frame gets through.
    fake_companion_firmware.drop_next_push(
        push_code=PushCode.MESSAGES_WAITING, count=part_count * (1 + channel_frames_per_direct_message)
    )
    channel_traffic = asyncio.create_task(
        inject_channel_traffic_after_each_direct_message(
            simulated_mesh,
            devices["alice-phone"],
            direct_message_count=part_count,
            datagrams_per_direct_message=CHANNEL_DATAGRAMS_PER_DIRECT_MESSAGE,
        )
    )
    message = alice.send_message("bob", THREE_PART_TEXT)
    assert len(message.parts) == part_count
    channel_receptions = await channel_traffic
    announced_datagram_reception = simulated_mesh.inject_channel_datagram(data=b"announced datagram")
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_until(lambda: fake_companion_firmware.offline_queue_length == 0, description="an empty node queue")
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice, bob])
    last_error_message = relay_worker.runtime_status.last_error_message
    final_connection_generation = relay_worker.runtime_status.connection_generation
    await relay_worker.stop()

    assert channel_receptions == [ReceptionOutcome.ACCEPTED] * (part_count * channel_frames_per_direct_message)
    assert announced_datagram_reception == ReceptionOutcome.ACCEPTED
    drain_outcomes = drain_outcome_recorder.outcomes
    assert NextMessageOutcome.REPLY_LOST not in drain_outcomes
    assert drain_outcomes.count(NextMessageOutcome.CHANNEL_TRAFFIC) == len(channel_receptions) + 1
    assert any(
        takes_channel_traffic_between_direct_messages(drain_pass)
        for drain_pass in drain_outcome_recorder.list_drain_passes()
    ), f"no drain pass took channel traffic between direct messages: {drain_outcomes}"
    assert final_connection_generation == connection_generation
    assert last_error_message == ""
    packets = await in_database(read_packets, id__gt=highest_packet_id_before)
    assert PacketState.OUTCOME_UNKNOWN not in {packet.state for packet in packets}
    part_rows = await in_database(read_inbox_rows_from, alice_device_id, "HT1 M bob ")
    assert len(part_rows) == part_count
    assert {row.processing_state for row in part_rows} == {InboundDirectMessage.ProcessingState.PROCESSED}
    assert await in_database(read_inbound_rows_from_unknown_senders) == []
    [received_message] = bob.received_messages("alice")
    assert received_message.text == THREE_PART_TEXT
    await in_database(assert_all_invariants)
