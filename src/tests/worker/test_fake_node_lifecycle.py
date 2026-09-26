"""Reboot, factory reset and power loss behave as in firmware v1.17.1 on a board without an RTC."""

from typing import Any

import pytest
from meshcore import EventType

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FactoryResetBehaviour, FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector, FakeNodeUnavailableError
from tests.worker.fake_node.frames import FirmwareErrorCode, ResponseCode
from tests.worker.fake_node.meshcore_events import MeshCoreEventRecorder, is_error_with_code, is_lost_reply
from tests.worker.fake_node.simulated_mesh import LinkPolicy, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until

OK_OR_ERROR = [EventType.OK, EventType.ERROR]
FACTORY_RESET_FRAME = b"\x33reset"
VOLATILE_CLOCK_BOOT_TIME = 1715770351
NEWEST_CONTACT_LAST_MODIFIED = 1_800_000_000
SHORT_COMMAND_TIMEOUT_SECONDS = 0.2


async def reconnect_after_the_link_dropped(connector: FakeNodeConnector, recorder: MeshCoreEventRecorder) -> Any:
    await recorder.wait_for_event(EventType.DISCONNECTED)
    await connector.wait_until_node_accepts_connections()
    reconnected_client = await connector()
    assert reconnected_client is not None
    return reconnected_client


async def test_reboot_without_its_suffix_is_answered_unsupported(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    bare_reboot = await meshcore_client.commands.send(b"\x13", OK_OR_ERROR)
    wrong_suffix = await meshcore_client.commands.send(b"\x13rebooX", OK_OR_ERROR)

    assert is_error_with_code(bare_reboot, FirmwareErrorCode.UNSUPPORTED_COMMAND)
    assert is_error_with_code(wrong_suffix, FirmwareErrorCode.UNSUPPORTED_COMMAND)
    assert fake_companion_firmware.is_running


async def test_a_reboot_drops_the_link_and_loses_ram_but_keeps_flash(
    fake_companion_firmware: FakeCompanionFirmware,
    fake_node_connector: FakeNodeConnector,
    meshcore_client: Any,
    simulated_mesh: SimulatedMesh,
) -> None:
    device = simulated_mesh.add_device("tracker", downlink=LinkPolicy(loss_probability=1.0))
    fake_companion_firmware.add_or_update_contact(
        ContactRecord.create(
            public_key=bytes([0x42]) * 32, name="old friend", last_modified=NEWEST_CONTACT_LAST_MODIFIED
        )
    )
    await meshcore_client.commands.set_name("renamed")
    await meshcore_client.commands.send_device_query()
    await meshcore_client.commands.send_msg(device.public_key, "never acknowledged", timestamp=1_800_000_100)
    device.send_direct_message("waiting in the queue")
    await wait_until(lambda: fake_companion_firmware.offline_queue_length == 1, description="a queued message")
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    reboot_result = await meshcore_client.commands.reboot()
    disconnected = await recorder.wait_for_event(EventType.DISCONNECTED)
    reconnected_client = await reconnect_after_the_link_dropped(fake_node_connector, recorder)
    clock_after_reboot = await reconnected_client.commands.get_time()
    self_information = await reconnected_client.commands.send_appstart()

    assert reboot_result.type == EventType.OK
    assert disconnected.payload["reason"] == "serial_disconnect"
    assert fake_companion_firmware.offline_queue_length == 0
    assert fake_companion_firmware.expected_acknowledgement_codes() == []
    assert fake_companion_firmware.app_target_version == 0
    assert clock_after_reboot.payload["time"] - (NEWEST_CONTACT_LAST_MODIFIED + 1) in (0, 1)
    assert self_information.payload["name"] == "renamed"
    assert fake_companion_firmware.find_contact(bytes([0x42]) * 32) is not None


async def test_after_a_reboot_messages_are_queued_in_legacy_frames_until_the_next_device_query(
    fake_companion_firmware: FakeCompanionFirmware,
    fake_node_connector: FakeNodeConnector,
    meshcore_client: Any,
    simulated_mesh: SimulatedMesh,
) -> None:
    device = simulated_mesh.add_device("tracker")
    await meshcore_client.commands.send_device_query()
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    await meshcore_client.commands.reboot()
    await reconnect_after_the_link_dropped(fake_node_connector, recorder)
    device.send_direct_message("after the reboot")
    await wait_until(lambda: fake_companion_firmware.offline_queue_length == 1, description="a queued message")

    assert fake_companion_firmware.offline_queue_frames()[0][0] == ResponseCode.CONTACT_MESSAGE_RECEIVED


async def test_without_contacts_the_clock_restarts_at_the_volatile_boot_time(
    fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    await meshcore_client.commands.set_time(1_800_000_000)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    await meshcore_client.commands.reboot()
    reconnected_client = await reconnect_after_the_link_dropped(fake_node_connector, recorder)
    clock_after_reboot = await reconnected_client.commands.get_time()

    assert clock_after_reboot.payload["time"] - VOLATILE_CLOCK_BOOT_TIME in (0, 1)


async def test_factory_reset_without_its_suffix_is_answered_unsupported(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    public_key_before = fake_companion_firmware.public_key

    bare_reset = await meshcore_client.commands.send(b"\x33", OK_OR_ERROR)

    assert is_error_with_code(bare_reset, FirmwareErrorCode.UNSUPPORTED_COMMAND)
    assert fake_companion_firmware.public_key == public_key_before


async def test_a_factory_reset_answers_nothing_and_the_node_returns_with_a_new_identity_and_defaults(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    public_key_before = fake_companion_firmware.public_key
    await meshcore_client.commands.send(b"\x26\x01\x00\x00\x02", OK_OR_ERROR)
    await meshcore_client.commands.set_channel(0, "Private", bytes(range(16)))
    await meshcore_client.commands.set_radio(869.525, 250.0, 11, 5, 0)
    fake_companion_firmware.add_or_update_contact(ContactRecord.create(public_key=bytes([0x42]) * 32, name="friend"))
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    reset_result = await meshcore_client.commands.send(FACTORY_RESET_FRAME, OK_OR_ERROR)
    reconnected_client = await reconnect_after_the_link_dropped(fake_node_connector, recorder)
    self_information = await reconnected_client.commands.send_appstart()
    auto_add_configuration = await reconnected_client.commands.get_autoadd_config()
    first_channel = await reconnected_client.commands.get_channel(0)

    new_public_key = fake_companion_firmware.public_key
    assert is_lost_reply(reset_result)
    assert new_public_key != public_key_before
    assert self_information.payload["public_key"] == new_public_key.hex()
    assert self_information.payload["name"] == new_public_key[:4].hex().upper()
    assert self_information.payload["manual_add_contacts"] is False
    assert self_information.payload["multi_acks"] == 0
    assert (self_information.payload["radio_freq"], self_information.payload["radio_bw"]) == (869.618, 62.5)
    assert auto_add_configuration.payload == {"config": 0, "max_hops": 0}
    assert first_channel.payload["channel_name"] == "Public"
    assert fake_companion_firmware.contact_records() == []


async def test_a_node_that_stays_off_after_a_reboot_refuses_connections_until_powered_on(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    fake_companion_firmware.stays_off_after_reboot = True
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    await meshcore_client.commands.reboot()
    await recorder.wait_for_event(EventType.DISCONNECTED)
    with pytest.raises(FakeNodeUnavailableError):
        await fake_node_connector()
    fake_companion_firmware.power_on()
    reconnected_client = await fake_node_connector()

    assert reconnected_client is not None


async def test_a_failed_format_leaves_the_node_deaf_until_it_is_power_cycled(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fake_companion_firmware.factory_reset_behaviour = FactoryResetBehaviour.FAILS_TO_FORMAT
    public_key_before = fake_companion_firmware.public_key

    reset_result = await meshcore_client.commands.send(FACTORY_RESET_FRAME, OK_OR_ERROR)
    time_while_deaf = await meshcore_client.commands.get_time()
    fake_companion_firmware.power_off()
    fake_companion_firmware.power_on()
    reconnected_client = await fake_node_connector()
    time_after_power_cycle = await reconnected_client.commands.get_time()

    assert is_lost_reply(reset_result)
    assert meshcore_client.is_connected is False
    assert is_lost_reply(time_while_deaf)
    assert time_after_power_cycle.type == EventType.CURRENT_TIME
    assert fake_companion_firmware.public_key == public_key_before


async def test_a_node_that_ignores_the_reset_keeps_its_identity_and_its_link(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector
) -> None:
    meshcore_client = await fake_node_connector.create_meshcore_client(default_timeout=SHORT_COMMAND_TIMEOUT_SECONDS)
    fake_companion_firmware.factory_reset_behaviour = FactoryResetBehaviour.IGNORES_COMMAND
    public_key_before = fake_companion_firmware.public_key

    reset_result = await meshcore_client.commands.send(FACTORY_RESET_FRAME, OK_OR_ERROR)
    self_information = await meshcore_client.commands.send_appstart()

    assert is_lost_reply(reset_result)
    assert self_information.payload["public_key"] == public_key_before.hex()
    assert meshcore_client.is_connected
