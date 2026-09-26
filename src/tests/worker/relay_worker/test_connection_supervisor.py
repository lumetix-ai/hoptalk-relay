"""The connection supervisor against the fake node: back-off, handshake, relay modes, watchdog, teardown."""

import itertools
import time
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from directory.models import Contact
from messaging.models import OutboundPacket
from node.models import NodeSetupRun, WorkerStatus
from node.setup_runs import cancel_setup_run
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import (
    ConnectRaises,
    ConnectReturnsNothing,
    FakeNodeConnector,
    FakeNodeUnavailableError,
)
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    in_database,
    wait_for_database,
)
from worker.node_gateway import NodeReplyLostError

pytestmark = pytest.mark.django_db(transaction=True)

RelayMode = WorkerStatus.RelayMode
CONFIGURED_ELSEWHERE_KEY = "ee" * 32


class RecordingConnector:
    """Wraps the fake node's connector and records when each connection attempt started."""

    def __init__(self, connector: FakeNodeConnector) -> None:
        self.connector = connector
        self.attempt_times: list[float] = []

    async def __call__(self) -> Any:
        self.attempt_times.append(time.monotonic())
        return await self.connector()


async def reset_to_a_new_identity(firmware: FakeCompanionFirmware) -> None:
    """A factory reset outside the panel, finished: the node boots with a new key."""
    original_public_key = firmware.public_key
    firmware.factory_reset()
    await wait_until(
        lambda: firmware.is_running and firmware.public_key != original_public_key,
        description="the node to boot with a new identity",
    )


def commands_after_the_app_start(firmware: FakeCompanionFirmware) -> list[int]:
    """The handshake's commands: everything after the library's own app start."""
    command_codes = [received_command.code for received_command in firmware.command_log]
    return command_codes[command_codes.index(CommandCode.APP_START) + 1 :]


async def test_failed_connection_attempts_back_off_until_the_node_answers(
    relay_worker: RelayWorkerHarness, fake_node_connector: FakeNodeConnector
) -> None:
    recording_connector = RecordingConnector(fake_node_connector)
    relay_worker.connector = recording_connector
    relay_worker.worker = relay_worker.build_worker()
    fake_node_connector.script_connect_results(
        ConnectRaises(FakeNodeUnavailableError(2, "no such port")),
        ConnectReturnsNothing(),
        ConnectRaises(FakeNodeUnavailableError(2, "no such port")),
    )

    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)

    attempt_gaps = [later - earlier for earlier, later in itertools.pairwise(recording_connector.attempt_times)]
    assert len(recording_connector.attempt_times) == 4
    assert attempt_gaps[1] > attempt_gaps[0] * 1.5
    assert attempt_gaps[2] > attempt_gaps[1] * 1.5
    assert relay_worker.runtime_status.consecutive_connect_failures == 0
    assert "Connecting to the node over" in relay_worker.runtime_status.last_error_message


async def test_the_handshake_queries_the_device_first_and_sets_a_clock_that_is_behind(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)

    assert commands_after_the_app_start(fake_companion_firmware)[:4] == [
        CommandCode.DEVICE_QUERY,
        CommandCode.APP_START,
        CommandCode.GET_DEVICE_TIME,
        CommandCode.SET_DEVICE_TIME,
    ]
    assert fake_companion_firmware.app_target_version == 3
    assert abs(fake_companion_firmware.clock_time() - int(relay_worker.clock.now().timestamp())) <= 2


async def test_a_node_clock_in_step_or_ahead_is_left_alone(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    fake_companion_firmware.force_clock_time(int(time.time()) + 120)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)

    assert CommandCode.SET_DEVICE_TIME not in commands_after_the_app_start(fake_companion_firmware)
    assert relay_worker.runtime_status.node_clock_offset_seconds >= 115


async def test_the_configured_node_runs_and_is_drained_reconciled_and_sent_to(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    await wait_until(
        lambda: (
            CommandCode.SYNC_NEXT_MESSAGE in commands_after_the_app_start(fake_companion_firmware)
            and CommandCode.GET_CONTACTS in commands_after_the_app_start(fake_companion_firmware)
        ),
        description="the drain and the reconciliation after the handshake",
    )


async def test_another_node_than_the_configured_one_is_an_identity_mismatch_and_is_left_untouched(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    await reset_to_a_new_identity(fake_companion_firmware)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.IDENTITY_MISMATCH)
    await relay_worker.wait_for_connection_generation(1)
    await relay_worker.clock.sleep(0.3)

    handshake_commands = commands_after_the_app_start(fake_companion_firmware)
    assert CommandCode.SYNC_NEXT_MESSAGE not in handshake_commands
    assert CommandCode.GET_CONTACTS not in handshake_commands
    assert CommandCode.GET_AUTO_ADD_CONFIGURATION not in handshake_commands


async def test_a_setup_run_that_owns_the_node_puts_it_in_setup_mode(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await in_database(
        NodeSetupRun.objects.create,
        purpose=NodeSetupRun.Purpose.INITIAL,
        state=NodeSetupRun.State.RESETTING,
        started_at=timezone.now(),
    )

    relay_worker.start()

    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)


async def test_cancelling_the_run_after_a_reset_shows_the_mismatch_without_a_reconnect(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    await reset_to_a_new_identity(fake_companion_firmware)
    setup_run = await in_database(
        NodeSetupRun.objects.create,
        purpose=NodeSetupRun.Purpose.RECONFIGURE,
        state=NodeSetupRun.State.AWAITING_CONFIGURATION,
        started_at=timezone.now(),
        new_public_key=fake_companion_firmware.public_key.hex(),
    )
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)

    await in_database(cancel_setup_run, setup_run.pk, timezone.now())

    await relay_worker.wait_for_relay_mode(RelayMode.IDENTITY_MISMATCH)
    assert relay_worker.runtime_status.connection_generation == 1


async def test_a_dead_event_dispatcher_is_noticed_and_the_node_reconnected(
    relay_worker: RelayWorkerHarness, fake_node_connector: FakeNodeConnector
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)

    first_client = fake_node_connector.clients[-1]
    first_client.dispatcher._task.cancel()

    await relay_worker.wait_for_connection_generation(2)
    assert fake_node_connector.clients[-1] is not first_client


async def test_the_watchdog_still_checks_while_the_database_sweep_wakes_the_supervisor_as_often(
    relay_worker: RelayWorkerHarness, fake_node_connector: FakeNodeConnector
) -> None:
    """In production both run every five seconds; each sweep recomputes the relay mode."""
    relay_worker.timing = replace(relay_worker.timing, watchdog_interval_seconds=0.3, database_sweep_seconds=0.3)
    relay_worker.worker = relay_worker.build_worker()
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)

    first_client = fake_node_connector.clients[-1]
    first_client.dispatcher._task.cancel()

    await relay_worker.wait_for_connection_generation(2, timeout_seconds=3.0)


async def test_the_socket_is_closed_directly_when_the_link_is_lost(
    relay_worker: RelayWorkerHarness, fake_node_connector: FakeNodeConnector
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)
    lost_transport = fake_node_connector.current_transport
    assert lost_transport is not None
    lost_socket = lost_transport.transport
    assert lost_socket is not None

    fake_node_connector.simulate_link_loss("tcp_no_response")

    await relay_worker.wait_for_connection_generation(2)
    assert lost_socket.was_closed
    assert "tcp_no_response" in relay_worker.runtime_status.last_error_message


async def test_packets_awaiting_a_firmware_ack_are_dropped_when_the_link_goes(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    fake_node_connector: FakeNodeConnector,
    simulated_mesh: SimulatedMesh,
) -> None:
    await configure_relay_node(fake_companion_firmware)
    contact = await in_database(create_contact_for_device, simulated_mesh.add_device("tracker"))
    packet = await in_database(create_packet_awaiting_acknowledgement, contact)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    fake_node_connector.simulate_link_loss()

    def packet_was_dropped() -> bool:
        packet.refresh_from_db()
        return packet.route_reset_state == OutboundPacket.RouteResetState.DROPPED_BY_RESTART

    await wait_for_database(packet_was_dropped, description="the packet to be dropped with the link")
    assert packet.state == OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT
    assert packet.acknowledgement_deadline_at is not None


def create_packet_awaiting_acknowledgement(contact: Contact) -> OutboundPacket:
    now = timezone.now()
    return OutboundPacket.objects.create(
        contact=contact,
        contact_label=str(contact),
        purpose=OutboundPacket.Purpose.REPLY,
        reply_key="q:bob",
        text="HT1 q bob 1",
        sender_timestamp=int(now.timestamp()),
        state=OutboundPacket.State.QUEUED_ON_NODE,
        route=OutboundPacket.Route.DIRECT,
        expected_acknowledgement_code="0badc0de",
        suggested_timeout_milliseconds=60_000,
        prepared_at=now,
        queued_at=now,
        acknowledgement_deadline_at=now + timedelta(minutes=5),
        connection_generation=1,
    )


async def test_the_node_is_found_again_after_it_rebooted(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    fake_companion_firmware.reboot()

    await relay_worker.wait_for_connection_generation(2)
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert fake_companion_firmware.app_target_version == 3
    assert abs(fake_companion_firmware.clock_time() - int(relay_worker.clock.now().timestamp())) <= 2


async def test_a_link_cut_in_the_middle_of_a_frame_leaves_no_half_frame_behind(
    relay_worker: RelayWorkerHarness, fake_node_connector: FakeNodeConnector, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)

    fake_node_connector.cut_link_during_next_frame()
    device.send_direct_message("HT1 Q bob")

    await relay_worker.wait_for_connection_generation(2)
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)


async def test_contact_safety_drift_is_corrected_at_once_and_other_drift_only_reported(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    fake_companion_firmware.preferences.manual_add_contacts = 0
    fake_companion_firmware.preferences.auto_add_configuration = 0x03
    fake_companion_firmware.preferences.transmit_power_dbm = 10

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    assert fake_companion_firmware.preferences.manual_add_contacts == 1
    assert fake_companion_firmware.preferences.auto_add_configuration == 0
    assert fake_companion_firmware.preferences.transmit_power_dbm == 10
    drift_by_key = {drift["key"]: drift for drift in relay_worker.runtime_status.settings_drift}
    assert drift_by_key["contacts.manual_add"]["corrected"] is True
    assert drift_by_key["contacts.auto_add_configuration"]["corrected"] is True
    assert drift_by_key["radio.transmit_power_dbm"] == {
        "key": "radio.transmit_power_dbm",
        "expected": 22,
        "actual": 10,
        "corrected": False,
    }
    assert fake_companion_firmware.protocol_violations == []


async def test_a_node_that_keeps_losing_replies_is_reconnected(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)

    fake_companion_firmware.drop_next_reply(command_code=CommandCode.GET_DEVICE_TIME, count=3)
    for _ in range(3):
        with pytest.raises(NodeReplyLostError):
            await relay_worker.worker.gateway.read_node_clock()

    await relay_worker.wait_for_connection_generation(2)
