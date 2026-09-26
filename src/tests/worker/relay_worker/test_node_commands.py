"""The panel's node commands as the worker claims and runs them against the fake node."""

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from django.db import OperationalError

from messaging.models import InboundDirectMessage
from node.contact_cards import parse_contact_card_uri
from node.models import NodeCommand
from node.node_commands import (
    INTERRUPTED_COMMAND_ERROR_MESSAGE,
    claim_next_node_command,
    create_node_command,
    finish_node_command,
)
from node.node_information import NodeInformation
from node.node_settings import load_node_configuration
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.radio_packets import AdvertPacket
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    in_database,
    wait_for_database,
)
from worker import node_command_executor
from worker.node_command_executor import CLAIMED_AT_SHUTDOWN_ERROR_MESSAGE, COMMAND_END_UNKNOWN_ERROR_MESSAGE
from worker.relay_modes import NOT_ALLOWED_BEFORE_SETUP
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

TERMINAL_STATES = [state.value for state in NodeCommand.TERMINAL_STATES]


def read_command(node_command_id: int) -> NodeCommand:
    return NodeCommand.objects.get(id=node_command_id)


async def queue_command(
    relay_worker: RelayWorkerHarness, kind: NodeCommand.Kind, arguments: dict[str, Any] | None = None
) -> NodeCommand:
    return await in_database(create_node_command, kind, arguments or {}, relay_worker.clock.now())


async def wait_for_command_to_finish(node_command: NodeCommand, *, timeout_seconds: float = 5.0) -> NodeCommand:
    await wait_for_database(
        lambda: read_command(node_command.pk).state in TERMINAL_STATES,
        timeout_seconds=timeout_seconds,
        description=f"command {node_command.kind} to finish",
    )
    return await in_database(read_command, node_command.pk)


async def start_running_relay(relay_worker: RelayWorkerHarness, firmware: FakeCompanionFirmware) -> None:
    await configure_relay_node(firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)


async def test_reading_the_node_information_reports_what_the_setup_wizard_shows(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    simulated_mesh.add_device("tracker")
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)

    node_command = await queue_command(relay_worker, NodeCommand.Kind.READ_NODE_INFORMATION)
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.SUCCEEDED
    assert finished_command.result is not None
    node_information = NodeInformation.from_json(finished_command.result)
    assert node_information.public_key == fake_companion_firmware.public_key.hex()
    assert node_information.contact_count == 1
    assert node_information.protocol_version == 13
    assert node_information.maximum_contacts == 350
    assert node_information.channel_zero_is_public
    assert node_information.channel_zero_name == "Public"
    assert node_information.radio_frequency_kilohertz == fake_companion_firmware.preferences.frequency_kilohertz
    assert [step["state"] for step in finished_command.progress] == ["done"]


async def test_a_command_the_relay_mode_does_not_allow_fails_with_a_readable_reason(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)

    node_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.FAILED
    assert finished_command.error_message == NOT_ALLOWED_BEFORE_SETUP
    assert not any(isinstance(packet, AdvertPacket) for packet in fake_companion_firmware.transmitted_packets)


async def test_applying_the_configured_settings_restores_a_drifted_node(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    configured_name = fake_companion_firmware.preferences.node_name
    fake_companion_firmware.preferences.transmit_power_dbm = 5
    fake_companion_firmware.preferences.node_name = b"renamed elsewhere"
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert {drift["key"] for drift in relay_worker.runtime_status.settings_drift} == {
        "node.name",
        "radio.transmit_power_dbm",
    }

    node_command = await queue_command(relay_worker, NodeCommand.Kind.APPLY_CONFIGURED_SETTINGS)
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.SUCCEEDED
    assert finished_command.result == {"remaining_drift": []}
    assert fake_companion_firmware.preferences.transmit_power_dbm == 22
    assert fake_companion_firmware.preferences.node_name == configured_name
    assert relay_worker.runtime_status.settings_drift == []
    assert fake_companion_firmware.protocol_violations == []


async def test_a_reboot_drains_the_node_first_and_ends_once_the_node_is_back(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)
    monkeypatch.setattr(relay_worker.worker.message_drainer, "run", wait_forever)
    await start_running_relay(relay_worker, fake_companion_firmware)
    device.send_direct_message("HT1 Q bob")
    await simulated_mesh.wait_until_idle()
    assert fake_companion_firmware.offline_queue_length == 1

    node_command = await queue_command(relay_worker, NodeCommand.Kind.REBOOT_NODE)
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.SUCCEEDED
    assert finished_command.result == {"connection_generation": 2, "relay_mode": "running"}
    assert await in_database(InboundDirectMessage.objects.count) == 1


async def wait_forever() -> None:
    """Stands in for the drainer's loop, so only the reboot command drains the node."""
    await asyncio.Event().wait()


async def test_a_zero_hop_and_a_flood_advert_go_out_after_a_clock_check(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)

    zero_hop_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    assert (await wait_for_command_to_finish(zero_hop_command)).state == NodeCommand.State.SUCCEEDED
    flood_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": True})
    assert (await wait_for_command_to_finish(flood_command)).state == NodeCommand.State.SUCCEEDED

    await wait_until(
        lambda: (
            len([packet for packet in fake_companion_firmware.transmitted_packets if isinstance(packet, AdvertPacket)])
            == 2
        ),
        description="two adverts on the air",
    )
    adverts = [packet for packet in fake_companion_firmware.transmitted_packets if isinstance(packet, AdvertPacket)]
    assert [advert.route.is_flood for advert in adverts] == [False, True]


async def test_an_advert_refused_for_a_full_packet_pool_is_tried_once_more(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    fake_companion_firmware.occupy_packet_pool(fake_companion_firmware.capacities.packet_pool_packets)
    asyncio.get_running_loop().call_later(0.03, fake_companion_firmware.release_packet_pool)

    node_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.SUCCEEDED


async def test_exporting_the_contact_card_stores_the_verified_card(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)

    node_command = await queue_command(relay_worker, NodeCommand.Kind.EXPORT_CONTACT_CARD)
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.SUCCEEDED
    assert finished_command.result is not None
    contact_card_uri = finished_command.result["contact_card_uri"]
    assert parse_contact_card_uri(contact_card_uri).public_key == fake_companion_firmware.public_key.hex()
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_contact_card_uri == contact_card_uri


async def test_reconciling_on_request_reports_its_counts(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    await wait_until(
        lambda: relay_worker.runtime_status.node_contact_count is not None, description="the first reconciliation"
    )
    stranger = simulated_mesh.add_device("stranger")
    await wait_until(lambda: fake_companion_firmware.find_contact(stranger.public_key) is not None)

    node_command = await queue_command(relay_worker, NodeCommand.Kind.RECONCILE_CONTACTS)
    finished_command = await wait_for_command_to_finish(node_command)

    assert finished_command.state == NodeCommand.State.SUCCEEDED
    assert finished_command.result == {
        "added": 0,
        "removed": 1,
        "failed": 0,
        "removals_postponed": False,
        "node_contact_count": 0,
    }


async def test_pending_commands_expire_by_their_kind_while_the_node_is_away(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    fake_companion_firmware.power_off()
    relay_worker.start()
    reboot_command = await queue_command(relay_worker, NodeCommand.Kind.REBOOT_NODE)
    advert_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})

    relay_worker.clock.advance(seconds=61)
    expired_reboot = await wait_for_command_to_finish(reboot_command)
    assert expired_reboot.state == NodeCommand.State.EXPIRED
    assert (await in_database(read_command, advert_command.pk)).state == NodeCommand.State.PENDING

    relay_worker.clock.advance(seconds=240)
    expired_advert = await wait_for_command_to_finish(advert_command)
    assert expired_advert.state == NodeCommand.State.EXPIRED


async def test_a_command_left_running_by_a_dead_worker_is_interrupted_at_the_next_start(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    orphaned_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    await in_database(
        NodeCommand.objects.filter(id=orphaned_command.pk).update,
        state=NodeCommand.State.RUNNING,
        claimed_at=relay_worker.clock.now(),
        claimed_by_worker_instance=uuid4(),
    )

    relay_worker.start()

    interrupted_command = await wait_for_command_to_finish(orphaned_command)
    assert interrupted_command.state == NodeCommand.State.INTERRUPTED
    assert interrupted_command.error_message == INTERRUPTED_COMMAND_ERROR_MESSAGE


async def test_a_command_whose_end_could_not_be_written_is_interrupted_and_blocks_no_later_command(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, monkeypatch: pytest.MonkeyPatch
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    failed_writes: list[int] = []

    def fail_the_first_write(node_command_id: int, *arguments: Any) -> bool:
        if not failed_writes:
            failed_writes.append(node_command_id)
            raise OperationalError("the database connection was reset")
        return finish_node_command(node_command_id, *arguments)

    monkeypatch.setattr(node_command_executor, "finish_node_command", fail_the_first_write)

    first_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    interrupted_command = await wait_for_command_to_finish(first_command)
    second_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    finished_second_command = await wait_for_command_to_finish(second_command)

    assert failed_writes == [first_command.pk]
    assert interrupted_command.state == NodeCommand.State.INTERRUPTED
    assert interrupted_command.error_message == COMMAND_END_UNKNOWN_ERROR_MESSAGE
    assert finished_second_command.state == NodeCommand.State.SUCCEEDED


async def test_a_command_claimed_just_as_the_worker_shuts_down_is_not_started(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, monkeypatch: pytest.MonkeyPatch
) -> None:
    await start_running_relay(relay_worker, fake_companion_firmware)
    event_loop = asyncio.get_running_loop()

    def claim_while_the_worker_shuts_down(worker_instance_id: UUID, now: datetime) -> NodeCommand | None:
        claimed_command = claim_next_node_command(worker_instance_id, now)
        if claimed_command is not None:
            event_loop.call_soon_threadsafe(relay_worker.worker.request_shutdown)
        return claimed_command

    monkeypatch.setattr(node_command_executor, "claim_next_node_command", claim_while_the_worker_shuts_down)

    node_command = await queue_command(relay_worker, NodeCommand.Kind.SEND_ADVERT, {"flood": False})
    finished_command = await wait_for_command_to_finish(node_command)
    await relay_worker.stop()

    assert finished_command.state == NodeCommand.State.INTERRUPTED
    assert finished_command.error_message == CLAIMED_AT_SHUTDOWN_ERROR_MESSAGE
    assert not any(isinstance(packet, AdvertPacket) for packet in fake_companion_firmware.transmitted_packets)
