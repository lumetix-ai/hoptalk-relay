"""The worker as a whole: start-up, the status row, shutdown, the single-instance lock."""

import asyncio
import io
import logging
from dataclasses import replace
from typing import Any

import psycopg
import pytest
from django.utils import timezone

from directory.models import Contact
from hoptalk_relay.logging_configuration import EscapingRedactingFormatter
from messaging.inbound_log import ReceivedDirectMessageFrame
from messaging.models import InboundDirectMessage
from node.models import NodeCommand, WorkerStatus
from node.node_commands import create_node_command
from node.worker_status import read_worker_status
from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import CommandCode, PushCode
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    DeviceInbox,
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    in_database,
    wait_for_database,
)
from worker.node_event_subscriptions import NodeContactTableFull
from worker.single_instance_lock import RELAY_WORKER_LOCK_KEY, build_database_connection_parameters
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)


async def test_a_configured_node_is_relayed_and_the_heartbeat_describes_it(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(WorkerStatus.RelayMode.RUNNING)

    def status_shows_running_node() -> bool:
        worker_status = read_worker_status()
        return worker_status is not None and worker_status.relay_mode == WorkerStatus.RelayMode.RUNNING

    await wait_for_database(status_shows_running_node, description="a heartbeat in relay mode running")
    worker_status = await in_database(read_worker_status)
    assert worker_status is not None
    assert worker_status.connection_state == WorkerStatus.ConnectionState.CONNECTED
    assert worker_status.node_public_key == fake_companion_firmware.public_key.hex()
    assert worker_status.node_protocol_version == 13
    assert worker_status.connection_generation == 1
    assert worker_status.worker_instance_id == relay_worker.worker.worker_instance_id
    assert worker_status.effective_configuration["attempts_per_delivery"] == 3
    assert worker_status.settings_drift == []


async def test_a_stopped_worker_writes_a_disconnected_status(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(WorkerStatus.RelayMode.RUNNING)

    await relay_worker.stop()

    worker_status = await in_database(read_worker_status)
    assert worker_status is not None
    assert worker_status.relay_mode == WorkerStatus.RelayMode.DISCONNECTED
    assert worker_status.connection_state == WorkerStatus.ConnectionState.DISCONNECTED
    assert not fake_companion_firmware.has_host_link


async def test_the_worker_stops_on_request_while_it_waits_for_the_lock(relay_worker: RelayWorkerHarness) -> None:
    relay_worker.worker.request_shutdown()
    relay_worker.start()

    run_task = relay_worker.run_task
    assert run_task is not None
    await asyncio.wait_for(run_task, 5)


async def test_frames_taken_from_the_node_before_shutdown_are_recorded_before_the_worker_stops(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)
    recorder = relay_worker.worker.inbound_recorder
    record_frame = recorder.record_frame
    shutdown_requested = relay_worker.worker.signals.shutdown_requested

    async def record_frame_only_at_shutdown(frame: ReceivedDirectMessageFrame) -> None:
        await shutdown_requested.wait()
        await record_frame(frame)

    monkeypatch.setattr(recorder, "record_frame", record_frame_only_at_shutdown)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    for query_number in range(4):
        device.send_direct_message(f"HT1 Q user{query_number}")
    await wait_until(
        lambda: relay_worker.worker.inbound_frame_queue.qsize() == 3, description="frames waiting to be recorded"
    )
    assert await in_database(InboundDirectMessage.objects.count) == 0

    await relay_worker.stop()

    assert await in_database(InboundDirectMessage.objects.count) == 4


def count_received_commands(firmware: FakeCompanionFirmware, command_code: int) -> int:
    return sum(1 for received_command in firmware.command_log if received_command.code == command_code)


async def test_a_message_the_node_hands_over_while_the_worker_shuts_down_is_recorded(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)
    relay_worker.timing = replace(relay_worker.timing, drain_poll_seconds=60.0)
    relay_worker.worker = relay_worker.build_worker()
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    # Only the drain this test asks for takes the message from the node.
    fake_companion_firmware.drop_next_push(push_code=PushCode.MESSAGES_WAITING)
    device.send_direct_message("HT1 Q bob")
    await wait_until(lambda: fake_companion_firmware.offline_queue_length == 1, description="the message on the node")

    fake_companion_firmware.delay_next_reply(0.3, command_code=CommandCode.SYNC_NEXT_MESSAGE)
    requests_before_the_drain = count_received_commands(fake_companion_firmware, CommandCode.SYNC_NEXT_MESSAGE)
    relay_worker.worker.signals.drain_requested.set()
    await wait_until(
        lambda: (
            count_received_commands(fake_companion_firmware, CommandCode.SYNC_NEXT_MESSAGE) > requests_before_the_drain
        ),
        description="the node to be asked for the message",
    )
    await relay_worker.stop()

    assert await in_database(InboundDirectMessage.objects.count) == 1
    assert fake_companion_firmware.offline_queue_length == 0


async def test_a_node_command_running_at_shutdown_finishes_before_the_link_is_closed(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    fake_companion_firmware.delay_next_reply(0.3, command_code=CommandCode.SEND_SELF_ADVERT)
    node_command = await in_database(
        create_node_command, NodeCommand.Kind.SEND_ADVERT, {"flood": False}, relay_worker.clock.now()
    )
    await wait_until(
        lambda: count_received_commands(fake_companion_firmware, CommandCode.SEND_SELF_ADVERT) == 1,
        description="the advert command at the node",
    )

    await relay_worker.stop()

    finished_command = await in_database(NodeCommand.objects.get, id=node_command.pk)
    assert finished_command.state == NodeCommand.State.SUCCEEDED
    assert not fake_companion_firmware.has_host_link


async def test_the_lock_is_released_when_the_worker_stops(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)
    assert not await try_to_take_the_relay_lock()

    await relay_worker.stop()

    assert await try_to_take_the_relay_lock()


async def try_to_take_the_relay_lock() -> bool:
    """Takes and at once releases the lock on a connection of its own; False while a worker holds it."""
    connection = await psycopg.AsyncConnection.connect(**build_database_connection_parameters(), autocommit=True)
    try:
        cursor = await connection.execute("SELECT pg_try_advisory_lock(%s)", [RELAY_WORKER_LOCK_KEY])
        row = await cursor.fetchone()
        is_taken = bool(row and row[0])
        if is_taken:
            await connection.execute("SELECT pg_advisory_unlock(%s)", [RELAY_WORKER_LOCK_KEY])
        return is_taken
    finally:
        await connection.close()


async def test_a_second_worker_waits_for_the_lock_and_never_opens_the_node_meanwhile(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)
    second_connector_calls: list[str] = []

    async def second_connector() -> Any:
        second_connector_calls.append("connect")
        return await relay_worker.connector()

    second_worker = RelayWorkerHarness(connector=second_connector)
    second_worker.start()
    try:
        await asyncio.sleep(0.5)
        assert second_connector_calls == []
        assert second_worker.runtime_status.connection_state == WorkerStatus.ConnectionState.DISCONNECTED

        await relay_worker.stop()

        await second_worker.wait_for_connection_generation(1)
        assert second_connector_calls != []
    finally:
        await second_worker.stop()


async def test_losing_the_lock_connection_ends_the_worker(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_connection_generation(1)
    lock_connection = relay_worker.worker.single_instance_lock._connection
    assert lock_connection is not None

    await terminate_backend(lock_connection)

    run_task = relay_worker.run_task
    assert run_task is not None
    with pytest.raises(ExceptionGroup):
        await asyncio.wait_for(run_task, 5)
    assert not fake_companion_firmware.has_host_link
    relay_worker.forget_run_task()


async def terminate_backend(lock_connection: Any) -> None:
    """What a database restart does to the lock's connection."""
    backend_process_id = lock_connection.info.backend_pid
    connection = await psycopg.AsyncConnection.connect(**build_database_connection_parameters(), autocommit=True)
    try:
        await connection.execute("SELECT pg_terminate_backend(%s)", [backend_process_id])
    finally:
        await connection.close()


@pytest.mark.parametrize(
    ("make_the_error_happen", "expected_error"),
    [
        ("drop_the_device_query_reply", "The handshake with the node failed"),
        ("report_a_full_contact_table", "contact table is full"),
        ("keep_the_packet_pool_full", "packet pool was full"),
    ],
)
async def test_each_kind_of_error_becomes_the_last_error(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    make_the_error_happen: str,
    expected_error: str,
) -> None:
    await configure_relay_node(fake_companion_firmware)
    if make_the_error_happen == "drop_the_device_query_reply":
        fake_companion_firmware.drop_next_reply(command_code=CommandCode.DEVICE_QUERY)
    elif make_the_error_happen == "keep_the_packet_pool_full":
        device = simulated_mesh.add_device("tracker")
        await in_database(create_contact_for_device, device)
        device.send_direct_message("HT1 Q bob")
        await simulated_mesh.wait_until_idle()
        fake_companion_firmware.occupy_packet_pool(fake_companion_firmware.capacities.packet_pool_packets)
    relay_worker.start()
    if make_the_error_happen == "report_a_full_contact_table":
        await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
        relay_worker.worker.node_event_queue.put_nowait(NodeContactTableFull())

    await wait_until(
        lambda: expected_error in relay_worker.runtime_status.last_error_message,
        timeout_seconds=10,
        description=f"the last error to mention {expected_error!r}",
    )
    await wait_for_database(
        lambda: expected_error in (read_worker_status() or WorkerStatus()).last_error_message,
        description="the last error in worker_status",
    )


async def test_a_contact_the_node_refuses_becomes_the_last_error(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    refused_contact = await in_database(create_contact_the_full_node_refuses, fake_companion_firmware)

    relay_worker.start()

    await wait_for_database(
        lambda: Contact.objects.get(id=refused_contact.pk).node_sync_state == Contact.NodeSyncState.ADD_FAILED,
        timeout_seconds=10,
        description="the contact refused by the full node",
    )
    assert "could not be added to the node" in relay_worker.runtime_status.last_error_message


def create_contact_the_full_node_refuses(firmware: FakeCompanionFirmware) -> Contact:
    for number in range(firmware.capacities.maximum_contacts):
        public_key = f"{number:012x}" + "cd" * 26
        firmware.add_or_update_contact(
            ContactRecord.create(
                public_key=bytes.fromhex(public_key), name=f"node {number}", last_modified=firmware.clock_time()
            )
        )
        Contact.objects.create(
            public_key=public_key, source=Contact.Source.CARD, added_at=timezone.now(), name=f"node {number}"
        )
    return Contact.objects.create(
        public_key="ff" * 32, source=Contact.Source.CARD, added_at=timezone.now(), name="one too many"
    )


async def test_passwords_never_reach_the_log_even_at_debug_level(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)
    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(EscapingRedactingFormatter("%(name)s: %(message)s"))
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(log_handler)
    root_logger.setLevel(logging.DEBUG)
    try:
        relay_worker.start()
        await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
        device.send_direct_message("HT1 A alice hunter2hunter2")
        await DeviceInbox(device).wait_for_text("HT1 a alice")
    finally:
        root_logger.removeHandler(log_handler)
        root_logger.setLevel(previous_level)

    logged_text = log_output.getvalue()
    assert "HT1 A alice ********" in logged_text
    assert "hunter2hunter2" not in logged_text
