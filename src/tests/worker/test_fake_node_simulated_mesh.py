"""The simulated mesh applies its link policies and device conditions as the documentation says."""

import pytest

from tests.worker.fake_node.fake_companion_firmware import (
    FakeCompanionFirmware,
    ReceptionOutcome,
    SendTextMessageResult,
    TextMessageQueued,
    TextMessageRejected,
)
from tests.worker.fake_node.frames import FirmwareErrorCode, TextType
from tests.worker.fake_node.node_identity import parse_contact_card_uri
from tests.worker.fake_node.radio_packets import DirectMessagePacket
from tests.worker.fake_node.simulated_mesh import (
    DeliveryOutcome,
    DeviceUnreachableError,
    LinkPolicy,
    SimulatedDevice,
    SimulatedMesh,
    parse_received_direct_message,
)
from tests.worker.fake_node.waiting import wait_until

SENDER_TIMESTAMP = 1_800_000_000


def relay_sends(relay_firmware: FakeCompanionFirmware, device: SimulatedDevice, text: str) -> TextMessageQueued:
    """What the worker's send_msg makes the relay's node do."""
    send_result = relay_firmware.send_text_message(
        text_type=TextType.PLAIN,
        attempt=0,
        sender_timestamp=SENDER_TIMESTAMP,
        recipient_public_key_prefix=device.public_key_prefix,
        text=text.encode(),
    )
    return assert_queued(send_result)


def assert_queued(send_result: SendTextMessageResult) -> TextMessageQueued:
    assert isinstance(send_result, TextMessageQueued), send_result
    return send_result


def queued_texts(firmware: FakeCompanionFirmware) -> list[bytes]:
    """The texts of the direct messages waiting in a node's queue."""
    queued_messages = [parse_received_direct_message(frame) for frame in firmware.offline_queue_frames()]
    return [message.text_bytes for message in queued_messages if message is not None]


def direct_message_outcomes(mesh: SimulatedMesh, *, sender: str) -> list[DeliveryOutcome]:
    return [record.outcome for record in mesh.traffic(sender=sender, packet_type=DirectMessagePacket)]


async def test_a_lossy_uplink_loses_every_message_of_the_device(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", uplink=LinkPolicy(loss_probability=1.0))

    sent_message = assert_queued(device.send_direct_message("into the void"))
    await simulated_mesh.wait_until_idle()

    assert fake_companion_firmware.offline_queue_length == 0
    assert device.acknowledgement_for(sent_message.expected_acknowledgement) is None
    assert direct_message_outcomes(simulated_mesh, sender="tracker") == [DeliveryOutcome.LOST_BY_POLICY]


async def test_a_packet_takes_at_least_the_minimum_delay_of_its_link(simulated_mesh: SimulatedMesh) -> None:
    device = simulated_mesh.add_device(
        "tracker", uplink=LinkPolicy(minimum_delay_seconds=0.05, maximum_delay_seconds=0.06)
    )

    device.send_direct_message("slow")
    await simulated_mesh.wait_until_idle()

    delivered_record = simulated_mesh.traffic(sender="tracker", packet_type=DirectMessagePacket)[0]
    assert delivered_record.recorded_at - delivered_record.transmitted_at >= 0.05


async def test_a_duplicated_packet_is_dropped_by_the_node_unless_it_forgot_the_original(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", uplink=LinkPolicy(duplicate_probability=1.0))

    device.send_direct_message("once")
    await simulated_mesh.wait_until_idle()
    queue_length_with_deduplication = fake_companion_firmware.offline_queue_length
    device.uplink = LinkPolicy(duplicate_probability=1.0, duplicates_bypass_deduplication=True)
    device.send_direct_message("twice")
    await simulated_mesh.wait_until_idle()

    first_message_receptions = {
        record.reception for record in simulated_mesh.traffic(sender="tracker", packet_type=DirectMessagePacket)[:2]
    }
    assert queue_length_with_deduplication == 1
    assert first_message_receptions == {ReceptionOutcome.ACCEPTED, ReceptionOutcome.ALREADY_SEEN}
    assert queued_texts(fake_companion_firmware) == [b"once", b"twice", b"twice"]


async def test_a_reordered_packet_is_overtaken_by_the_next_one(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", uplink=LinkPolicy(reorder_probability=1.0))

    device.send_direct_message("sent first")
    await wait_until(lambda: bool(device.firmware.transmitted_packets), description="the first packet to be sent")
    device.uplink = LinkPolicy()
    device.send_direct_message("sent second")
    await simulated_mesh.wait_until_idle()

    assert queued_texts(fake_companion_firmware) == [b"sent second", b"sent first"]


async def test_an_asymmetric_link_loses_only_the_firmware_acknowledgements(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", uplink=LinkPolicy(acknowledgement_loss_probability=1.0))

    relay_message = relay_sends(fake_companion_firmware, device, "HT1 m ivan 1 1/1 hello")
    await simulated_mesh.wait_until_idle()
    device.send_direct_message("HT1 K ivan 1 1")
    await simulated_mesh.wait_until_idle()

    assert [message.text for message in device.receive_direct_messages()] == ["HT1 m ivan 1 1/1 hello"]
    assert relay_message.expected_acknowledgement in fake_companion_firmware.expected_acknowledgement_codes()
    assert queued_texts(fake_companion_firmware) == [b"HT1 K ivan 1 1"]


async def test_a_stale_route_loses_direct_sends_until_the_route_is_reset(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", repeaters_to_relay=1)
    device.send_direct_message("learn the routes")
    await simulated_mesh.wait_until_idle()
    device.change_route_to_relay([b"\x9c\x00\x00"])

    lost_message = relay_sends(fake_companion_firmware, device, "direct on a stale route")
    await simulated_mesh.wait_until_idle()
    fake_companion_firmware.reset_route(device.public_key)
    flooded_message = relay_sends(fake_companion_firmware, device, "flooded after the reset")
    await simulated_mesh.wait_until_idle()

    relay_contact_of_device = fake_companion_firmware.find_contact(device.public_key)
    assert (lost_message.sent_by_flood, flooded_message.sent_by_flood) == (False, True)
    assert direct_message_outcomes(simulated_mesh, sender="relay") == [
        DeliveryOutcome.LOST_ON_STALE_ROUTE,
        DeliveryOutcome.DELIVERED,
    ]
    assert fake_companion_firmware.expected_acknowledgement_codes() == [lost_message.expected_acknowledgement]
    assert relay_contact_of_device is not None
    assert relay_contact_of_device.route_path == b"\x9c"


async def test_the_device_resets_its_own_stale_route_to_reach_the_relay_again(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", repeaters_to_relay=2)
    device.send_direct_message("learn the routes")
    await simulated_mesh.wait_until_idle()
    device.change_route_to_relay(2)

    device.send_direct_message("direct on a stale route")
    await simulated_mesh.wait_until_idle()
    device.reset_route_to_relay()
    device.send_direct_message("flooded after the reset")
    await simulated_mesh.wait_until_idle()

    assert direct_message_outcomes(simulated_mesh, sender="tracker") == [
        DeliveryOutcome.DELIVERED,
        DeliveryOutcome.LOST_ON_STALE_ROUTE,
        DeliveryOutcome.DELIVERED,
    ]
    assert queued_texts(fake_companion_firmware) == [b"learn the routes", b"flooded after the reset"]


async def test_with_the_phone_away_the_node_acknowledges_and_queues_but_the_app_reads_nothing(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    device.phone_leaves()

    relay_message = relay_sends(fake_companion_firmware, device, "while you were away")
    await simulated_mesh.wait_until_idle()
    with pytest.raises(DeviceUnreachableError):
        device.receive_direct_messages()
    with pytest.raises(DeviceUnreachableError):
        device.send_direct_message("from a phone that is not there")
    waiting_while_away = device.waiting_frame_count
    device.phone_returns()

    assert relay_message.expected_acknowledgement not in fake_companion_firmware.expected_acknowledgement_codes()
    assert waiting_while_away == 1
    assert [message.text for message in device.receive_direct_messages()] == ["while you were away"]


async def test_a_switched_off_device_hears_nothing_and_comes_back_without_its_queue(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    device.phone_leaves()
    relay_sends(fake_companion_firmware, device, "queued before the power went")
    await simulated_mesh.wait_until_idle()
    device.phone_returns()

    device.switch_off()
    unheard_message = relay_sends(fake_companion_firmware, device, "sent while off")
    await simulated_mesh.wait_until_idle()
    device.switch_on()

    receptions = [
        record.reception for record in simulated_mesh.traffic(sender="relay", packet_type=DirectMessagePacket)
    ]
    assert receptions == [ReceptionOutcome.ACCEPTED, ReceptionOutcome.NODE_NOT_RUNNING]
    assert fake_companion_firmware.expected_acknowledgement_codes() == [unheard_message.expected_acknowledgement]
    assert device.receive_direct_messages() == []
    assert device.firmware.app_target_version == 3


async def test_a_device_node_refuses_to_send_while_its_packet_pool_is_full(simulated_mesh: SimulatedMesh) -> None:
    device = simulated_mesh.add_device("tracker")
    device.firmware.occupy_packet_pool(16)

    while_full = device.send_direct_message("too much traffic")
    device.firmware.release_packet_pool()
    after_release = device.send_direct_message("room again")

    assert while_full == TextMessageRejected(error_code=FirmwareErrorCode.TABLE_FULL)
    assert isinstance(after_release, TextMessageQueued)


async def test_the_device_app_sees_its_firmware_acknowledgements_and_route_updates(
    simulated_mesh: SimulatedMesh,
) -> None:
    device = simulated_mesh.add_device("tracker", repeaters_to_relay=1)

    sent_message = assert_queued(device.send_direct_message("hello relay"))
    acknowledgement = await device.wait_for_acknowledgement(sent_message.expected_acknowledgement, timeout_seconds=2)
    await simulated_mesh.wait_until_idle()

    stored_relay_contact = device.stored_relay_contact
    assert acknowledgement is not None
    assert acknowledgement.round_trip_milliseconds >= 0
    assert device.last_path_update_at is not None
    assert stored_relay_contact is not None
    assert stored_relay_contact.has_known_route


async def test_the_messages_the_app_reads_say_how_they_arrived(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", repeaters_to_relay=2)

    relay_sends(fake_companion_firmware, device, "flooded")
    await simulated_mesh.wait_until_idle()
    relay_sends(fake_companion_firmware, device, "direct")
    await simulated_mesh.wait_until_idle()

    received_messages = device.receive_direct_messages()
    assert [(message.text, message.arrived_by_flood, message.path_length) for message in received_messages] == [
        ("flooded", True, 2),
        ("direct", False, 0xFF),
    ]
    assert received_messages[0].sender_public_key_prefix == fake_companion_firmware.public_key[:6]
    assert received_messages[0].sender_timestamp == SENDER_TIMESTAMP


async def test_a_device_card_is_signed_by_the_devices_real_identity(simulated_mesh: SimulatedMesh) -> None:
    device = simulated_mesh.add_device("tracker")

    parsed_card = parse_contact_card_uri(device.contact_card_uri())

    assert parsed_card is not None
    assert parsed_card.signature_is_valid
    assert parsed_card.public_key == device.public_key
    assert parsed_card.name == b"tracker"


async def test_a_device_the_relay_does_not_know_is_not_heard(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("stranger", relay_knows_device=False)

    device.send_direct_message("HT1 A alice hunter2222")
    await simulated_mesh.wait_until_idle()

    receptions = [record.reception for record in simulated_mesh.traffic(sender="stranger")]
    assert receptions == [ReceptionOutcome.UNKNOWN_SENDER]
    assert fake_companion_firmware.offline_queue_length == 0


async def test_after_the_relay_gets_a_new_identity_devices_reach_it_only_once_they_trust_the_new_key(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    old_relay_public_key = fake_companion_firmware.public_key
    fake_companion_firmware.factory_reset()
    await wait_until(
        lambda: fake_companion_firmware.is_running and fake_companion_firmware.public_key != old_relay_public_key,
        description="the relay's node to come back with a new identity",
    )
    fake_companion_firmware.add_or_update_contact(device.contact_record())

    device.send_direct_message("to the old key")
    relay_sends(fake_companion_firmware, device, "from the new key")
    await simulated_mesh.wait_until_idle()
    device.trust_relay(fake_companion_firmware.public_key)
    device.send_direct_message("to the new key")
    await simulated_mesh.wait_until_idle()

    message_to_the_old_key = simulated_mesh.traffic(sender="tracker")[0]
    message_from_the_new_key = simulated_mesh.traffic(sender="relay")[0]
    assert message_to_the_old_key.outcome == DeliveryOutcome.LOST_NO_SUCH_NODE
    assert message_from_the_new_key.reception == ReceptionOutcome.UNKNOWN_SENDER
    assert queued_texts(fake_companion_firmware) == [b"to the new key"]


async def test_the_same_seed_draws_the_same_identities_and_routes(fake_node_seed: int) -> None:
    public_keys_and_routes = []
    for _ in range(2):
        relay_firmware = FakeCompanionFirmware(seed=fake_node_seed)
        relay_firmware.start()
        mesh = SimulatedMesh(relay_firmware, seed=fake_node_seed)
        device = mesh.add_device("tracker", repeaters_to_relay=2)
        public_keys_and_routes.append((relay_firmware.public_key, device.public_key, device.route_to_relay))
        await mesh.stop()
        await relay_firmware.stop()

    assert public_keys_and_routes[0] == public_keys_and_routes[1]
