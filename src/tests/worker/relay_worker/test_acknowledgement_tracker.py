"""Firmware ACKs and the route resets they lead to, against the fake node and the simulated mesh."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from pytest_django import Settings

from messaging.models import OutboundPacket
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware, MessageSentReplyOrder
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.simulated_mesh import LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.relay_worker.worker_harness import (
    FAST_RETRY_STRATEGY,
    DeviceInbox,
    RelayWorkerHarness,
    accept_messages,
    configure_relay_node,
    create_contact_for_device,
    create_sending_device,
    create_user,
    in_database,
    wait_for_database,
)
from worker.node_event_subscriptions import PathUpdated
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

RouteResetState = OutboundPacket.RouteResetState
SENDING_DEVICE_NUMBER = 902


async def prepare_bob(
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    *,
    uplink: LinkPolicy | None = None,
    with_direct_route: bool = False,
    message_count: int = 0,
) -> SimulatedDevice:
    """Bob's device, signed in; Alice's accepted messages to Bob are due."""
    await configure_relay_node(fake_companion_firmware)
    bob_device = simulated_mesh.add_device("bob", uplink=uplink)
    bob = await in_database(create_user, "bob")
    await in_database(create_contact_for_device, bob_device, user=bob)
    if with_direct_route:
        give_the_relay_a_direct_route_to(fake_companion_firmware, bob_device)
    if message_count:
        alice = await in_database(create_user, "alice")
        alice_device = await in_database(create_sending_device, SENDING_DEVICE_NUMBER, alice)
        await in_database(accept_messages, alice_device, "bob", message_count=message_count)
    return bob_device


def give_the_device_a_direct_route_to_the_relay(device: SimulatedDevice) -> None:
    """The device's messages then arrive direct and teach the relay no new route (no PATH_UPDATE)."""
    relay_contact = device.stored_relay_contact
    assert relay_contact is not None
    device.firmware.add_or_update_contact(
        relay_contact.with_route(route_path=b"", path_hash_size=1, last_modified=device.firmware.clock_time())
    )


def postpone_the_next_round(settings: Settings) -> None:
    """The next delivery round would settle a pending reset itself; these tests settle it otherwise first."""
    settings.RELAY_SETTINGS = replace(
        settings.RELAY_SETTINGS, retry_strategy=replace(FAST_RETRY_STRATEGY, initial_pause_seconds=5.0)
    )


def give_the_relay_a_direct_route_to(firmware: FakeCompanionFirmware, device: SimulatedDevice) -> None:
    """A zero-hop route, as if an earlier exchange had taught it: the relay's DMs to the device go direct."""
    record = firmware.find_contact(device.public_key)
    assert record is not None
    firmware.add_or_update_contact(
        record.with_route(route_path=b"", path_hash_size=1, last_modified=firmware.clock_time())
    )


def read_packets(**filters: Any) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(**filters).order_by("id"))


def read_packets_handed_to_node(purpose: OutboundPacket.Purpose) -> list[OutboundPacket]:
    """A packet's route is known only once its send outcome is recorded; until then it is only prepared."""
    return [packet for packet in read_packets(purpose=purpose) if packet.state != OutboundPacket.State.PREPARED]


def first_packet_matches(**expected_values: Any) -> bool:
    packets = read_packets()
    if not packets:
        return False
    return all(getattr(packets[0], field_name) == value for field_name, value in expected_values.items())


def count_reset_path_commands(firmware: FakeCompanionFirmware) -> int:
    return sum(1 for received_command in firmware.command_log if received_command.code == CommandCode.RESET_PATH)


async def test_an_ack_is_matched_live_and_frees_its_place(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await prepare_bob(fake_companion_firmware, simulated_mesh, message_count=1)

    relay_worker.start()

    await wait_for_database(
        lambda: first_packet_matches(state=OutboundPacket.State.NODE_ACKNOWLEDGED),
        description="the delivery acknowledged by the firmware",
    )
    packet = (await in_database(read_packets))[0]
    assert packet.round_trip_milliseconds is not None
    assert packet.route_reset_state == RouteResetState.NOT_APPLICABLE
    assert relay_worker.worker.acknowledgement_tracker.count_packets_awaiting_acknowledgement() == 0


async def test_an_ack_that_beats_its_message_sent_reply_is_matched_through_the_buffer(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await prepare_bob(fake_companion_firmware, simulated_mesh, message_count=1)
    fake_companion_firmware.message_sent_reply_order = MessageSentReplyOrder.AFTER_ACKNOWLEDGEMENT

    relay_worker.start()

    await wait_for_database(
        lambda: first_packet_matches(state=OutboundPacket.State.NODE_ACKNOWLEDGED),
        description="the early ACK matched once MSG_SENT was recorded",
    )
    packet = (await in_database(read_packets))[0]
    assert packet.route_reset_state == RouteResetState.NOT_APPLICABLE
    assert relay_worker.worker.acknowledgement_tracker.count_unmatched_acknowledgements() == 0


async def test_a_late_ack_is_matched_through_the_database_and_settles_the_pending_reset(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    postpone_the_next_round(settings)
    await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(minimum_delay_seconds=0.4, maximum_delay_seconds=0.45),
        with_direct_route=True,
        message_count=1,
    )

    relay_worker.start()

    await wait_for_database(
        lambda: first_packet_matches(state=OutboundPacket.State.NODE_ACKNOWLEDGED),
        description="the late ACK matched",
    )
    packet = (await in_database(read_packets))[0]
    assert packet.route == OutboundPacket.Route.DIRECT
    assert packet.acknowledged_at is not None
    assert packet.acknowledgement_deadline_at is not None
    assert packet.acknowledged_at > packet.acknowledgement_deadline_at
    assert packet.route_reset_state == RouteResetState.SKIPPED_LATE_ACKNOWLEDGEMENT


async def test_a_flooded_packet_without_an_ack_needs_no_route_reset(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
        message_count=1,
    )

    relay_worker.start()

    await wait_for_database(
        lambda: first_packet_matches(state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT),
        description="the deadline to pass",
    )
    packet = (await in_database(read_packets))[0]
    assert packet.route == OutboundPacket.Route.FLOOD
    assert packet.route_reset_state == RouteResetState.NOT_APPLICABLE


async def test_a_direct_packet_without_an_ack_or_evidence_resets_the_route_before_the_next_packet(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
        with_direct_route=True,
        message_count=1,
    )

    relay_worker.start()

    await wait_for_database(
        lambda: first_packet_matches(route_reset_state=RouteResetState.PERFORMED),
        description="the route reset before the next round",
    )
    delivery_packets = await in_database(read_packets, purpose=OutboundPacket.Purpose.DELIVERY)
    assert delivery_packets[0].route == OutboundPacket.Route.DIRECT
    await wait_for_database(
        lambda: len(read_packets_handed_to_node(OutboundPacket.Purpose.DELIVERY)) >= 2,
        description="the next round handed to the node",
    )
    delivery_packets = await in_database(read_packets_handed_to_node, OutboundPacket.Purpose.DELIVERY)
    assert delivery_packets[1].route == OutboundPacket.Route.FLOOD
    assert count_reset_path_commands(fake_companion_firmware) >= 1


async def test_a_route_reset_is_skipped_when_the_device_acknowledged_the_part_in_the_protocol(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    postpone_the_next_round(settings)
    bob_device = await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
        with_direct_route=True,
        message_count=1,
    )
    give_the_device_a_direct_route_to_the_relay(bob_device)
    bob_inbox = DeviceInbox(bob_device)
    relay_worker.start()
    await bob_inbox.wait_for_text("HT1 m alice 1 1/1 part 1 of 1")
    await wait_for_database(
        lambda: first_packet_matches(route_reset_state=RouteResetState.PENDING),
        description="the reset decision to wait for the next packet",
    )

    bob_device.send_direct_message("HT1 K alice 1 1")
    bob_device.send_direct_message("HT1 Q alice")
    await bob_inbox.wait_for_text("HT1 q alice 1")

    await wait_for_database(
        lambda: first_packet_matches(route_reset_state=RouteResetState.SKIPPED_APPLICATION_EVIDENCE),
        description="the reset skipped on the device's K",
    )


async def test_a_route_reset_is_skipped_when_the_node_learned_a_new_route(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    postpone_the_next_round(settings)
    bob_device = await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
        with_direct_route=True,
        message_count=1,
    )
    relay_worker.start()
    await wait_for_database(
        lambda: first_packet_matches(route_reset_state=RouteResetState.PENDING),
        description="the reset decision to wait for the next packet",
    )

    relay_worker.worker.node_event_queue.put_nowait(PathUpdated(public_key=bob_device.public_key.hex()))

    await wait_for_database(
        lambda: first_packet_matches(route_reset_state=RouteResetState.SKIPPED_PATH_UPDATE),
        description="the reset skipped after the PATH_UPDATE",
    )
    assert count_reset_path_commands(fake_companion_firmware) == 0


async def test_a_reply_without_an_ack_is_sent_once_more_by_flood_right_after_the_reset(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    bob_device = await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
        with_direct_route=True,
    )
    give_the_device_a_direct_route_to_the_relay(bob_device)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    bob_device.send_direct_message("HT1 Q alice")

    await wait_for_database(
        lambda: len(read_packets_handed_to_node(OutboundPacket.Purpose.REPLY)) == 2,
        description="the reply sent again and handed to the node",
    )
    await relay_worker.clock.sleep(0.5)
    reply_packets = await in_database(read_packets, purpose=OutboundPacket.Purpose.REPLY)
    assert [packet.text for packet in reply_packets] == ["HT1 q alice 0", "HT1 q alice 0"]
    assert [packet.route for packet in reply_packets] == [OutboundPacket.Route.DIRECT, OutboundPacket.Route.FLOOD]
    assert reply_packets[0].route_reset_state == RouteResetState.PERFORMED
    assert reply_packets[1].route_reset_state == RouteResetState.NOT_APPLICABLE


async def test_two_settlements_at_once_reset_the_route_once(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    """The sender settles before its next packet while the tracker settles a reply's timeout; both may overlap."""
    postpone_the_next_round(settings)
    await prepare_bob(
        fake_companion_firmware,
        simulated_mesh,
        uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
        with_direct_route=True,
        message_count=1,
    )
    relay_worker.start()
    await wait_for_database(
        lambda: first_packet_matches(route_reset_state=RouteResetState.PENDING),
        description="the reset decision to wait for the next packet",
    )
    [pending_packet] = await in_database(read_packets)
    assert pending_packet.contact_id is not None
    tracker = relay_worker.worker.acknowledgement_tracker

    await asyncio.gather(
        tracker.settle_route_resets(pending_packet.contact_id),
        tracker.settle_route_resets(pending_packet.contact_id),
    )

    assert count_reset_path_commands(fake_companion_firmware) == 1
    assert await in_database(first_packet_matches, route_reset_state=RouteResetState.PERFORMED)
