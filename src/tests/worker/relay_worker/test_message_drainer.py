"""Draining the node's offline queue: everything, past channel traffic and lost replies, with back-pressure."""

import asyncio
from dataclasses import replace

import pytest

from messaging.inbound_log import ReceivedDirectMessageFrame
from messaging.models import InboundDirectMessage
from node.models import WorkerStatus
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.frames import CommandCode, PushCode
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    FAST_WORKER_TIMING,
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    in_database,
    point_node_setting_at_another_node,
    wait_for_database,
)

pytestmark = pytest.mark.django_db(transaction=True)

RelayMode = WorkerStatus.RelayMode


def count_inbox_rows() -> int:
    return InboundDirectMessage.objects.count()


# The device's own node holds 16 packets; waiting for the mesh after a few keeps every send accepted.
QUERIES_PER_BURST = 8


async def send_queries(device: SimulatedDevice, simulated_mesh: SimulatedMesh, query_count: int) -> None:
    for query_number in range(query_count):
        device.send_direct_message(f"HT1 Q user{query_number:03d}")
        if query_number % QUERIES_PER_BURST == QUERIES_PER_BURST - 1:
            await simulated_mesh.wait_until_idle()
    await simulated_mesh.wait_until_idle()


async def prepare_running_relay_with_device(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> SimulatedDevice:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)
    return device


def count_commands(firmware: FakeCompanionFirmware, command_code: int) -> int:
    return sum(1 for received_command in firmware.command_log if received_command.code == command_code)


async def test_every_waiting_message_is_drained_and_recorded(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = await prepare_running_relay_with_device(fake_companion_firmware, simulated_mesh)
    await send_queries(device, simulated_mesh, 12)

    relay_worker.start()

    await wait_for_database(lambda: count_inbox_rows() == 12, description="twelve inbox rows")
    assert fake_companion_firmware.offline_queue_length == 0


async def test_channel_datagrams_between_messages_cause_no_timeout_and_no_reconnect(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = await prepare_running_relay_with_device(fake_companion_firmware, simulated_mesh)
    await send_queries(device, simulated_mesh, 2)
    for datagram_number in range(4):
        simulated_mesh.inject_channel_datagram(data=bytes([datagram_number]) * 12)
    simulated_mesh.inject_channel_message(text="chatter on the public channel")
    await send_queries(device, simulated_mesh, 2)

    relay_worker.start()

    await wait_for_database(lambda: count_inbox_rows() == 4, description="four inbox rows")
    await wait_until(lambda: fake_companion_firmware.offline_queue_length == 0, description="an empty queue")
    assert relay_worker.runtime_status.connection_generation == 1
    assert count_commands(fake_companion_firmware, CommandCode.SYNC_NEXT_MESSAGE) >= 10


async def test_a_lost_reply_does_not_end_the_drain(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = await prepare_running_relay_with_device(fake_companion_firmware, simulated_mesh)
    await send_queries(device, simulated_mesh, 5)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.SYNC_NEXT_MESSAGE)

    relay_worker.start()

    await wait_until(lambda: fake_companion_firmware.offline_queue_length == 0, description="an empty queue")
    await wait_for_database(lambda: count_inbox_rows() == 4, description="the four frames that arrived")


async def test_the_drain_stops_pulling_while_ten_frames_wait_to_be_recorded(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await prepare_running_relay_with_device(fake_companion_firmware, simulated_mesh)
    await send_queries(device, simulated_mesh, 25)
    recording_allowed = asyncio.Event()
    recorder = relay_worker.worker.inbound_recorder
    record_frame = recorder.record_frame

    async def record_frame_when_allowed(frame: ReceivedDirectMessageFrame) -> None:
        await recording_allowed.wait()
        await record_frame(frame)

    monkeypatch.setattr(recorder, "record_frame", record_frame_when_allowed)
    relay_worker.start()

    await wait_until(
        lambda: relay_worker.worker.inbound_frame_queue.qsize() == FAST_WORKER_TIMING.maximum_unrecorded_frames,
        description="ten frames waiting to be recorded",
    )
    await relay_worker.clock.sleep(0.2)
    assert relay_worker.worker.inbound_frame_queue.qsize() == FAST_WORKER_TIMING.maximum_unrecorded_frames
    assert fake_companion_firmware.offline_queue_length >= 25 - FAST_WORKER_TIMING.maximum_unrecorded_frames - 1

    recording_allowed.set()
    await wait_for_database(lambda: count_inbox_rows() == 25, description="all 25 frames recorded")


async def test_the_periodic_drain_finds_messages_whose_push_was_lost(
    fake_node_connector: FakeNodeConnector,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    close_sync_to_async_thread_connections: None,
) -> None:
    device = await prepare_running_relay_with_device(fake_companion_firmware, simulated_mesh)
    harness = RelayWorkerHarness(
        connector=fake_node_connector, timing=replace(FAST_WORKER_TIMING, drain_poll_seconds=30.0)
    )
    harness.start()
    try:
        await harness.wait_for_relay_mode(RelayMode.RUNNING)
        await wait_until(lambda: count_commands(fake_companion_firmware, CommandCode.SYNC_NEXT_MESSAGE) >= 1)
        fake_companion_firmware.drop_next_push(push_code=PushCode.MESSAGES_WAITING)

        await send_queries(device, simulated_mesh, 1)
        await harness.clock.sleep(0.2)
        assert fake_companion_firmware.offline_queue_length == 1

        harness.clock.advance(seconds=30)
        await wait_for_database(lambda: count_inbox_rows() == 1, description="the message found by the timer")
    finally:
        await harness.stop()


@pytest.mark.parametrize("relay_mode", [RelayMode.NOT_CONFIGURED, RelayMode.IDENTITY_MISMATCH])
async def test_nothing_is_drained_while_the_node_is_not_the_configured_one(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    relay_mode: WorkerStatus.RelayMode,
) -> None:
    if relay_mode == RelayMode.IDENTITY_MISMATCH:
        await configure_relay_node(fake_companion_firmware)
        await in_database(point_node_setting_at_another_node)
    device = simulated_mesh.add_device("tracker")
    await send_queries(device, simulated_mesh, 3)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(relay_mode)
    relay_worker.clock.advance(seconds=FAST_WORKER_TIMING.drain_poll_seconds)
    await relay_worker.clock.sleep(0.3)

    assert fake_companion_firmware.offline_queue_length == 3
    assert count_commands(fake_companion_firmware, CommandCode.SYNC_NEXT_MESSAGE) == 0
