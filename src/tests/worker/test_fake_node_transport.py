"""The link between meshcore and the fake node behaves like a USB serial link to real firmware."""

from typing import Any

import pytest
from meshcore import EventType

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import (
    ConnectRaises,
    ConnectReturnsNothing,
    FakeNodeConnector,
    FakeNodeUnavailableError,
)
from tests.worker.fake_node.frames import CommandCode, FirmwareErrorCode, frame_host_to_node
from tests.worker.fake_node.meshcore_events import MeshCoreEventRecorder, is_error_with_code, is_lost_reply
from tests.worker.fake_node.waiting import wait_until

SHORT_COMMAND_TIMEOUT_SECONDS = 0.2


def add_contacts(firmware: FakeCompanionFirmware, contact_count: int) -> list[ContactRecord]:
    records = [
        ContactRecord.create(
            public_key=bytes([0x10 + index]) * 32, name=f"contact {index}", last_modified=1_800_000_000 + index
        )
        for index in range(contact_count)
    ]
    for record in records:
        firmware.add_or_update_contact(record)
    return records


async def test_the_node_answers_through_the_library_deframer_in_random_chunks(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    add_contacts(fake_companion_firmware, 20)

    self_information = await meshcore_client.commands.send_appstart()
    contacts_recorder = MeshCoreEventRecorder(meshcore_client, EventType.CONTACTS)
    await meshcore_client.commands.get_contacts_async()
    contacts_event = await contacts_recorder.wait_for_event(EventType.CONTACTS)

    assert self_information.payload["public_key"] == fake_companion_firmware.public_key.hex()
    assert len(contacts_event.payload) == 20
    transport = fake_node_connector.current_transport
    assert transport is not None
    assert transport.delivered_chunk_count != transport.delivered_frame_count


async def test_commands_reach_the_node_as_host_frames(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    await meshcore_client.commands.get_time()

    transport = fake_node_connector.current_transport
    assert transport is not None
    assert transport.sent_payloads[-1] == bytes([CommandCode.GET_DEVICE_TIME])
    assert fake_companion_firmware.command_log[-1].frame == bytes([CommandCode.GET_DEVICE_TIME])
    assert frame_host_to_node(b"\x05") == b"\x3c\x01\x00\x05"


async def test_a_lost_link_is_reported_and_later_commands_get_no_reply(
    fake_node_connector: FakeNodeConnector,
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    fake_node_connector.simulate_link_loss("serial_disconnect")
    disconnected_event = await recorder.wait_for_event(EventType.DISCONNECTED)
    time_result = await meshcore_client.commands.get_time()

    assert disconnected_event.payload == {"reason": "serial_disconnect", "reconnect_failed": False}
    assert not meshcore_client.is_connected
    assert is_lost_reply(time_result)


async def test_a_send_on_a_closed_link_reports_the_transport_lost(fake_node_connector: FakeNodeConnector) -> None:
    transport = fake_node_connector.create_transport()
    reported_reasons: list[str] = []

    async def record_disconnect_reason(reason: str) -> None:
        reported_reasons.append(reason)

    transport.set_disconnect_callback(record_disconnect_reason)
    await transport.send(b"\x05")

    assert reported_reasons == ["serial_transport_lost"]
    assert transport.sent_payloads == []


async def test_scripted_connect_failures_come_first_then_the_node_connects(
    fake_node_connector: FakeNodeConnector,
) -> None:
    fake_node_connector.script_connect_results(ConnectReturnsNothing(), ConnectRaises(OSError("resource busy")))

    with pytest.raises(ConnectionError):
        await fake_node_connector()
    with pytest.raises(OSError, match="resource busy"):
        await fake_node_connector()
    meshcore_client = await fake_node_connector()

    assert meshcore_client is not None
    assert meshcore_client.is_connected


async def test_connecting_fails_while_the_node_is_off_and_works_once_it_is_back(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    fake_companion_firmware.power_off()

    with pytest.raises(FakeNodeUnavailableError):
        await fake_node_connector()
    fake_companion_firmware.power_on()
    meshcore_client = await fake_node_connector()

    assert meshcore_client is not None


async def test_the_factory_returns_none_when_the_app_start_is_not_answered(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.APP_START)

    meshcore_client = await fake_node_connector()

    assert meshcore_client is None


async def test_a_dropped_reply_times_the_command_out_but_the_command_took_effect(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.SET_ADVERT_NAME)

    name_result = await meshcore_client.commands.set_name("renamed")
    time_result = await meshcore_client.commands.get_time()

    assert is_lost_reply(name_result)
    assert fake_companion_firmware.preferences.node_name == b"renamed"
    assert time_result.type == EventType.CURRENT_TIME


async def test_a_lost_command_never_reaches_the_node(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fake_companion_firmware.lose_next_command(command_code=CommandCode.SET_ADVERT_NAME)

    name_result = await meshcore_client.commands.set_name("renamed")

    assert is_lost_reply(name_result)
    assert fake_companion_firmware.preferences.node_name == b"hoptalk-relay"
    assert CommandCode.SET_ADVERT_NAME not in [command.code for command in fake_companion_firmware.command_log]


async def test_a_late_reply_is_taken_by_the_next_command_waiting_for_the_same_type(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fake_companion_firmware.delay_next_reply(0.3, command_code=CommandCode.SET_DEVICE_TIME)

    backwards_time_result = await meshcore_client.commands.set_time(1_000)
    name_result = await meshcore_client.commands.set_name("renamed")

    assert is_lost_reply(backwards_time_result)
    # The set_time refusal arrived while set_name was waiting for OK or ERROR.
    assert is_error_with_code(name_result, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert fake_companion_firmware.preferences.node_name == b"renamed"


async def test_a_link_cut_mid_frame_corrupts_the_next_frame_of_a_reused_deframer(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    stale_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fake_node_connector.cut_link_during_next_frame()

    cut_result = await stale_client.commands.get_time()
    await wait_until(lambda: not stale_client.is_connected, description="the cut link to be reported")
    await stale_client.connection_manager.connect()
    stale_deframer_result = await stale_client.commands.get_time()
    fresh_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fresh_result = await fresh_client.commands.get_time()

    assert is_lost_reply(cut_result)
    # The old deframer still held "09" of the cut frame and completed it with the new frame's
    # header "3E 05 00 09": a time that the node never sent.
    assert stale_deframer_result.payload["time"] == int.from_bytes(b"\x3e\x05\x00\x09", "little")
    assert abs(fresh_result.payload["time"] - fake_companion_firmware.clock_time()) <= 1


async def test_a_new_client_after_a_link_loss_talks_to_the_same_node(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    fake_node_connector.simulate_link_loss()
    reconnected_client = await fake_node_connector()
    device_information = await reconnected_client.commands.send_device_query()

    assert reconnected_client is not meshcore_client
    assert device_information.type == EventType.DEVICE_INFO
    assert len(fake_node_connector.transports) == 2


async def test_closing_the_stream_transport_directly_ends_the_link(
    fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)
    stream_transport = meshcore_client.connection_manager.connection.transport

    stream_transport.close()
    disconnected = await recorder.wait_for_event(EventType.DISCONNECTED)

    assert stream_transport.was_closed
    assert disconnected.payload["reason"] == "serial_disconnect"
