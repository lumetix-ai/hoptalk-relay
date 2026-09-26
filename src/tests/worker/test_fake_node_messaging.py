"""Direct messages, firmware ACKs, routes and the offline queue behave as in firmware v1.17.1."""

from typing import Any

from meshcore import EventType

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import (
    FakeCompanionFirmware,
    MessageSentReplyOrder,
    ReceptionOutcome,
    TextMessageQueued,
)
from tests.worker.fake_node.firmware_state import FirmwareCapacities
from tests.worker.fake_node.frames import FirmwareErrorCode, ResponseCode
from tests.worker.fake_node.meshcore_events import (
    MeshCoreEventRecorder,
    is_error_with_code,
    is_lost_reply,
    wait_until_earlier_node_frames_are_dispatched,
)
from tests.worker.fake_node.radio_packets import (
    AcknowledgementPacket,
    PathReturnPacket,
    calculate_expected_acknowledgement,
)
from tests.worker.fake_node.simulated_mesh import LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until

DRAIN_EXPECTED_EVENTS = [
    EventType.CONTACT_MSG_RECV,
    EventType.CHANNEL_MSG_RECV,
    EventType.CHANNEL_DATA_RECV,
    EventType.NO_MORE_MSGS,
    EventType.ERROR,
]
SENDER_TIMESTAMP = 1_800_000_000
SLOW_ACKNOWLEDGEMENTS = LinkPolicy(minimum_delay_seconds=0.2, maximum_delay_seconds=0.2)


async def drain_offline_queue(meshcore_client: Any) -> list[Any]:
    """Every queued frame, drained the way the worker does it, datagrams included."""
    drained_events: list[Any] = []
    while True:
        event = await meshcore_client.commands.send(b"\x0a", DRAIN_EXPECTED_EVENTS)
        if event.type == EventType.NO_MORE_MSGS:
            return drained_events
        drained_events.append(event)


async def wait_for_queued_frames(firmware: FakeCompanionFirmware, frame_count: int) -> None:
    await wait_until(
        lambda: firmware.offline_queue_length >= frame_count,
        description=f"{frame_count} frame(s) in the offline queue of {firmware.label}",
    )


def assert_queued(send_result: Any) -> TextMessageQueued:
    assert isinstance(send_result, TextMessageQueued), send_result
    return send_result


async def test_a_message_to_an_unknown_prefix_is_refused_with_not_found(meshcore_client: Any) -> None:
    result = await meshcore_client.commands.send_msg(bytes(6), "hello", timestamp=SENDER_TIMESTAMP)

    assert is_error_with_code(result, FirmwareErrorCode.NOT_FOUND)


async def test_texts_too_long_for_one_packet_are_refused_with_table_full(
    meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")

    longest_text = await meshcore_client.commands.send_msg(device.public_key, "x" * 160, timestamp=SENDER_TIMESTAMP)
    too_long_text = await meshcore_client.commands.send_msg(device.public_key, "x" * 161, timestamp=SENDER_TIMESTAMP)
    extended_attempt_longest = await meshcore_client.commands.send_msg(
        device.public_key, "x" * 158, timestamp=SENDER_TIMESTAMP, attempt=4
    )
    extended_attempt_too_long = await meshcore_client.commands.send_msg(
        device.public_key, "x" * 159, timestamp=SENDER_TIMESTAMP, attempt=4
    )

    assert longest_text.type == EventType.MSG_SENT
    assert is_error_with_code(too_long_text, FirmwareErrorCode.TABLE_FULL)
    assert extended_attempt_longest.type == EventType.MSG_SENT
    assert is_error_with_code(extended_attempt_too_long, FirmwareErrorCode.TABLE_FULL)


async def test_a_full_packet_pool_refuses_sends_with_table_full(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    fake_companion_firmware.occupy_packet_pool(16)

    while_full = await meshcore_client.commands.send_msg(device.public_key, "hello", timestamp=SENDER_TIMESTAMP)
    fake_companion_firmware.release_packet_pool()
    after_release = await meshcore_client.commands.send_msg(device.public_key, "hello", timestamp=SENDER_TIMESTAMP)

    assert is_error_with_code(while_full, FirmwareErrorCode.TABLE_FULL)
    assert after_release.type == EventType.MSG_SENT


async def test_the_expected_ack_is_the_firmware_hash_and_its_confirmation_carries_the_round_trip(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ACK)

    message_sent = await meshcore_client.commands.send_msg(device.public_key, "hello", timestamp=SENDER_TIMESTAMP)
    confirmation = await recorder.wait_for_event(EventType.ACK)

    expected_acknowledgement = calculate_expected_acknowledgement(
        sender_timestamp=SENDER_TIMESTAMP,
        attempt=0,
        text=b"hello",
        sender_public_key=fake_companion_firmware.public_key,
    )
    assert message_sent.payload["expected_ack"] == expected_acknowledgement
    assert confirmation.payload["code"] == expected_acknowledgement.hex()
    assert confirmation.payload["trip_time"] >= 0
    received_messages = device.receive_direct_messages()
    assert [(message.sender_timestamp, message.text) for message in received_messages] == [(SENDER_TIMESTAMP, "hello")]


async def test_msg_sent_says_flood_until_a_route_is_learned_and_direct_afterwards(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.PATH_UPDATE)

    first_send = await meshcore_client.commands.send_msg(device.public_key, "first", timestamp=SENDER_TIMESTAMP)
    await recorder.wait_for_event(EventType.PATH_UPDATE)
    second_send = await meshcore_client.commands.send_msg(device.public_key, "second", timestamp=SENDER_TIMESTAMP)

    assert (first_send.payload["type"], first_send.payload["suggested_timeout"]) == (1, 50)
    assert (second_send.payload["type"], second_send.payload["suggested_timeout"]) == (0, 30)


async def test_no_confirmation_arrives_when_the_message_or_only_its_ack_is_lost(
    meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", downlink=LinkPolicy(loss_probability=1.0))
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ACK)

    await meshcore_client.commands.send_msg(device.public_key, "lost", timestamp=SENDER_TIMESTAMP)
    await simulated_mesh.wait_until_idle()
    received_while_downlink_lossy = device.receive_direct_messages()
    device.downlink = LinkPolicy()
    device.uplink = LinkPolicy(acknowledgement_loss_probability=1.0)
    await meshcore_client.commands.send_msg(device.public_key, "arrives", timestamp=SENDER_TIMESTAMP)
    await simulated_mesh.wait_until_idle()
    await wait_until_earlier_node_frames_are_dispatched(meshcore_client)

    assert received_while_downlink_lossy == []
    assert [message.text for message in device.receive_direct_messages()] == ["arrives"]
    assert recorder.of_type(EventType.ACK) == []


async def test_the_ack_table_forgets_the_oldest_of_nine_outstanding_messages(
    meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", uplink=SLOW_ACKNOWLEDGEMENTS)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ACK)

    sent_codes = []
    for message_number in range(9):
        message_sent = await meshcore_client.commands.send_msg(
            device.public_key, f"message {message_number}", timestamp=SENDER_TIMESTAMP
        )
        sent_codes.append(message_sent.payload["expected_ack"].hex())
    await simulated_mesh.wait_until_idle()
    await wait_until_earlier_node_frames_are_dispatched(meshcore_client)

    assert sorted(event.payload["code"] for event in recorder.of_type(EventType.ACK)) == sorted(sent_codes[1:])


async def test_the_confirmation_can_be_ordered_before_msg_sent(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    fake_companion_firmware.message_sent_reply_order = MessageSentReplyOrder.AFTER_ACKNOWLEDGEMENT
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ACK, EventType.MSG_SENT)

    message_sent = await meshcore_client.commands.send_msg(device.public_key, "hello", timestamp=SENDER_TIMESTAMP)

    assert message_sent.type == EventType.MSG_SENT
    assert recorder.types() == [EventType.ACK, EventType.MSG_SENT]
    assert recorder.events[0].payload["code"] == message_sent.payload["expected_ack"].hex()


async def test_received_messages_use_legacy_frames_until_a_device_query_asks_for_version_3(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")

    device.send_direct_message("before the query")
    await wait_for_queued_frames(fake_companion_firmware, 1)
    before_query = await meshcore_client.commands.get_msg()
    await meshcore_client.commands.send_device_query()
    device.send_direct_message("after the query")
    await wait_for_queued_frames(fake_companion_firmware, 1)
    after_query = await meshcore_client.commands.get_msg()

    assert before_query.payload["text"] == "before the query"
    assert "SNR" not in before_query.payload
    assert after_query.payload["text"] == "after the query"
    assert "SNR" in after_query.payload
    assert after_query.payload["pubkey_prefix"] == device.public_key_prefix.hex()


async def test_every_queued_message_is_announced_and_announcements_are_lost_while_the_link_is_down(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: Any, simulated_mesh: SimulatedMesh
) -> None:
    first_client = await fake_node_connector()
    first_recorder = MeshCoreEventRecorder(first_client, EventType.MESSAGES_WAITING)
    device = simulated_mesh.add_device("tracker")

    device.send_direct_message("one")
    device.send_direct_message("two")
    await first_recorder.wait_for_event(EventType.MESSAGES_WAITING, count=2)
    fake_node_connector.simulate_link_loss()
    device.send_direct_message("three")
    await wait_for_queued_frames(fake_companion_firmware, 3)
    second_client = await fake_node_connector()
    second_recorder = MeshCoreEventRecorder(second_client, EventType.MESSAGES_WAITING)
    drained_events = await drain_offline_queue(second_client)

    assert fake_companion_firmware.frames_lost_without_host >= 1
    assert second_recorder.of_type(EventType.MESSAGES_WAITING) == []
    assert [event.payload["text"] for event in drained_events] == ["one", "two", "three"]
    assert fake_companion_firmware.offline_queue_length == 0


async def test_the_offline_queue_evicts_channel_frames_first_and_then_drops_new_messages(
    fake_node_seed: int,
) -> None:
    firmware = FakeCompanionFirmware(capacities=FirmwareCapacities(offline_queue_frames=3), seed=fake_node_seed)
    firmware.start()
    mesh = SimulatedMesh(firmware, seed=fake_node_seed)
    try:
        device = mesh.add_device("tracker")
        mesh.inject_channel_message(text="public chatter")
        for text in ("one", "two", "three"):
            device.send_direct_message(text)
            await mesh.wait_until_idle()
        queue_after_eviction = firmware.offline_queue_frames()
        dropped_message = assert_queued(device.send_direct_message("four"))
        await mesh.wait_until_idle()

        assert FirmwareCapacities().offline_queue_frames == 256
        assert [frame[0] for frame in queue_after_eviction] == [ResponseCode.CONTACT_MESSAGE_RECEIVED] * 3
        assert firmware.offline_queue_frames() == queue_after_eviction
        # The node acknowledges a message even when its full queue dropped it.
        assert device.acknowledgement_for(dropped_message.expected_acknowledgement) is not None
    finally:
        await mesh.stop()
        await firmware.stop()


async def test_channel_messages_and_datagrams_wait_in_the_queue_between_direct_messages(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    await meshcore_client.commands.send_device_query()

    device.send_direct_message("before")
    await wait_for_queued_frames(fake_companion_firmware, 1)
    datagram_reception = simulated_mesh.inject_channel_datagram(data=b"\x01\x02\x03")
    channel_message_reception = simulated_mesh.inject_channel_message(text="public chatter")
    device.send_direct_message("after")
    await wait_for_queued_frames(fake_companion_firmware, 4)
    drained_events = await drain_offline_queue(meshcore_client)

    assert datagram_reception == channel_message_reception == ReceptionOutcome.ACCEPTED
    assert [event.type for event in drained_events] == [
        EventType.CONTACT_MSG_RECV,
        EventType.CHANNEL_DATA_RECV,
        EventType.CHANNEL_MSG_RECV,
        EventType.CONTACT_MSG_RECV,
    ]
    assert drained_events[1].payload["payload"] == "010203"
    assert drained_events[2].payload["channel_idx"] == 0


async def test_the_librarys_get_msg_times_out_on_a_datagram_it_has_already_consumed(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: Any, simulated_mesh: SimulatedMesh
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=0.2)
    simulated_mesh.inject_channel_datagram(data=b"\x01")

    get_msg_result = await meshcore_client.commands.get_msg()

    assert is_lost_reply(get_msg_result)
    assert fake_companion_firmware.offline_queue_length == 0


async def test_a_flood_exchange_teaches_both_nodes_a_route_and_reports_path_updates(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", repeaters_to_relay=[b"\xa7\x00\x00"])
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.PATH_UPDATE)

    first_send = assert_queued(device.send_direct_message("by flood"))
    path_update = await recorder.wait_for_event(EventType.PATH_UPDATE)
    await wait_until(lambda: bool(device.path_update_times), description="the device to learn its route")
    second_send = assert_queued(device.send_direct_message("direct"))
    await wait_for_queued_frames(fake_companion_firmware, 2)
    received_messages = await drain_offline_queue(meshcore_client)

    relay_contact_of_device = fake_companion_firmware.find_contact(device.public_key)
    device_contact_of_relay = device.stored_relay_contact
    assert path_update.payload == {"public_key": device.public_key.hex()}
    assert relay_contact_of_device is not None
    assert relay_contact_of_device.route_path == b"\xa7"
    assert device_contact_of_relay is not None
    assert device_contact_of_relay.route_path == b"\xa7"
    assert (first_send.sent_by_flood, second_send.sent_by_flood) == (True, False)
    assert [(event.payload["text"], event.payload["path_len"]) for event in received_messages] == [
        ("by flood", 1),
        ("direct", 255),
    ]


async def test_a_second_flood_exchange_along_the_same_path_teaches_the_relay_its_reset_route_again(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", repeaters_to_relay=[b"\xa7\x00\x00"])
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.PATH_UPDATE)
    device.send_direct_message("by flood")
    await recorder.wait_for_event(EventType.PATH_UPDATE)
    await simulated_mesh.wait_until_idle()
    fake_companion_firmware.reset_route(device.public_key)
    device.reset_route_to_relay()

    device.send_direct_message("by flood again")
    await simulated_mesh.wait_until_idle()

    relay_contact_of_device = fake_companion_firmware.find_contact(device.public_key)
    assert relay_contact_of_device is not None
    assert relay_contact_of_device.has_known_route
    assert relay_contact_of_device.route_path == b"\xa7"
    reciprocal_path_returns = simulated_mesh.traffic(sender="tracker", packet_type=PathReturnPacket)
    assert [record.reception for record in reciprocal_path_returns] == [
        ReceptionOutcome.ACCEPTED,
        ReceptionOutcome.ACCEPTED,
    ]


async def test_multi_acks_send_a_second_acknowledgement_that_confirms_nothing_more(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    device.firmware.preferences.multi_acknowledgements = 1
    known_route_to_device = ContactRecord.create(public_key=device.public_key, name="tracker").with_route(
        route_path=b"", path_hash_size=1, last_modified=0
    )
    fake_companion_firmware.add_or_update_contact(known_route_to_device)
    device.trust_relay(fake_companion_firmware.public_key)
    device.firmware.add_or_update_contact(
        ContactRecord.create(public_key=fake_companion_firmware.public_key, name="relay").with_route(
            route_path=b"", path_hash_size=1, last_modified=0
        )
    )
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ACK)

    await meshcore_client.commands.send_msg(device.public_key, "hello", timestamp=SENDER_TIMESTAMP)
    await simulated_mesh.wait_until_idle()
    await wait_until_earlier_node_frames_are_dispatched(meshcore_client)

    acknowledgement_receptions = [
        record.reception for record in simulated_mesh.traffic(sender="tracker", packet_type=AcknowledgementPacket)
    ]
    assert acknowledgement_receptions == [
        ReceptionOutcome.ACCEPTED,
        ReceptionOutcome.ACKNOWLEDGEMENT_NOT_EXPECTED,
    ]
    assert len(recorder.of_type(EventType.ACK)) == 1


async def test_receive_log_pushes_report_every_packet_the_radio_hears_once_enabled(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.RX_LOG_DATA)
    simulated_mesh.inject_foreign_traffic(2)
    fake_companion_firmware.receive_log_pushes_enabled = True

    receptions = simulated_mesh.inject_foreign_traffic(3)
    await recorder.wait_for_event(EventType.RX_LOG_DATA, count=3)
    await wait_until_earlier_node_frames_are_dispatched(meshcore_client)

    assert receptions == [ReceptionOutcome.NOT_ADDRESSED_TO_THIS_NODE] * 3
    assert len(recorder.of_type(EventType.RX_LOG_DATA)) == 3
    assert {event.payload["payload_typename"] for event in recorder.of_type(EventType.RX_LOG_DATA)} == {"TEXT_MSG"}


async def test_a_device_repeating_a_message_with_a_higher_attempt_reaches_the_relay_twice(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device: SimulatedDevice = simulated_mesh.add_device("stock-app")

    device.send_direct_message("HT1 F *", sender_timestamp=SENDER_TIMESTAMP)
    await simulated_mesh.wait_until_idle()
    device.send_direct_message("HT1 F *", sender_timestamp=SENDER_TIMESTAMP, attempt=1)
    await wait_for_queued_frames(fake_companion_firmware, 2)
    drained_events = await drain_offline_queue(meshcore_client)

    assert [(event.payload["sender_timestamp"], event.payload["text"]) for event in drained_events] == [
        (SENDER_TIMESTAMP, "HT1 F *"),
        (SENDER_TIMESTAMP, "HT1 F *"),
    ]
