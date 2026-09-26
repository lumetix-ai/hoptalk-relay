"""The worker over meshcore's real TCP transport: a local TCP server hands the framed bytes to the fake node.

This is the path the development setup uses (the macOS bridge on port 5055): the library's own
TCPConnection and deframer, the worker's connect with its timeout and clean-up, link loss and
reconnection.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest

from hoptalk_relay.relay_settings import NodeConnectionSettings, NodeTransport
from node.models import WorkerStatus
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import frame_node_to_host
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    FAST_WORKER_TIMING,
    AdjustableClock,
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    in_database,
)
from worker.node_connections import build_node_client_factory, connect_over_tcp

pytestmark = pytest.mark.django_db(transaction=True)

LOCAL_HOST = "127.0.0.1"


class TcpNodeBridge:
    """What socat does on the Mac: one TCP client at a time, its bytes to the node and the node's bytes back."""

    def __init__(self, firmware: FakeCompanionFirmware) -> None:
        self.firmware = firmware
        self.server: asyncio.Server | None = None
        self.port = 0
        self.accepted_connection_count = 0
        self._current_writer: asyncio.StreamWriter | None = None
        self._connection_tasks: set[asyncio.Task[None]] = set()

    async def start(self, port: int = 0) -> None:
        self.server = await asyncio.start_server(self._serve_client, LOCAL_HOST, port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        self.drop_client()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for connection_task in list(self._connection_tasks):
            connection_task.cancel()
        await asyncio.gather(*self._connection_tasks, return_exceptions=True)

    def drop_client(self) -> None:
        if self._current_writer is not None:
            self._current_writer.close()
            self._current_writer = None

    async def _serve_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection_task = asyncio.current_task()
        if connection_task is not None:
            self._connection_tasks.add(connection_task)
        self.accepted_connection_count += 1
        self._current_writer = writer
        host_link = TcpHostLink(writer)
        self.firmware.attach_host_link(host_link)
        try:
            while data := await reader.read(256):
                self.firmware.receive_host_bytes(data)
        except ConnectionError:
            pass
        finally:
            self.firmware.detach_host_link(host_link)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            if connection_task is not None:
                self._connection_tasks.discard(connection_task)


class TcpHostLink:
    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self._writer = writer

    def deliver_frame_to_host(self, frame: bytes) -> None:
        if not self._writer.is_closing():
            self._writer.write(frame_node_to_host(frame))

    def node_dropped_link(self, _reason: str) -> None:
        self._writer.close()


@contextlib.asynccontextmanager
async def tcp_node_bridge(firmware: FakeCompanionFirmware) -> AsyncIterator[TcpNodeBridge]:
    bridge = TcpNodeBridge(firmware)
    await bridge.start()
    try:
        yield bridge
    finally:
        await bridge.stop()


def build_tcp_harness(port: int) -> RelayWorkerHarness:
    node_connection = NodeConnectionSettings(
        transport=NodeTransport.TCP, tcp_host=LOCAL_HOST, tcp_port=port, serial_device="/dev/null"
    )
    return RelayWorkerHarness(
        connector=build_node_client_factory(node_connection, FAST_WORKER_TIMING), clock=AdjustableClock()
    )


async def test_the_worker_relays_over_the_tcp_transport(
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    close_sync_to_async_thread_connections: None,
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)

    async with tcp_node_bridge(fake_companion_firmware) as bridge:
        harness = build_tcp_harness(bridge.port)
        harness.start()
        try:
            await harness.wait_for_relay_mode(WorkerStatus.RelayMode.RUNNING)
            device.send_direct_message("HT1 A alice hunter2hunter2")

            await wait_until(
                lambda: any(message.text == "HT1 a alice" for message in device.receive_direct_messages()),
                description="the sign-in reply over TCP",
            )
        finally:
            await harness.stop()


async def test_a_lost_tcp_link_is_reconnected_with_a_new_socket(
    fake_companion_firmware: FakeCompanionFirmware, close_sync_to_async_thread_connections: None
) -> None:
    async with tcp_node_bridge(fake_companion_firmware) as bridge:
        harness = build_tcp_harness(bridge.port)
        harness.start()
        try:
            await harness.wait_for_connection_generation(1)
            first_client = harness.worker.gateway.meshcore_client
            assert first_client is not None
            first_socket = first_client.connection_manager.connection.transport

            bridge.drop_client()

            await harness.wait_for_connection_generation(2)
            assert bridge.accepted_connection_count == 2
            assert first_socket.is_closing()
            assert "tcp_disconnect" in harness.runtime_status.last_error_message
        finally:
            await harness.stop()


async def test_the_worker_waits_for_the_bridge_and_connects_once_it_listens(
    fake_companion_firmware: FakeCompanionFirmware, close_sync_to_async_thread_connections: None
) -> None:
    bridge = TcpNodeBridge(fake_companion_firmware)
    await bridge.start()
    port = bridge.port
    await bridge.stop()
    harness = build_tcp_harness(port)
    harness.start()
    try:
        await wait_until(
            lambda: harness.runtime_status.consecutive_connect_failures >= 3,
            description="failed attempts while nothing listens",
        )
        assert harness.runtime_status.connection_state != WorkerStatus.ConnectionState.CONNECTED
        assert "Connecting to the node over" in harness.runtime_status.last_error_message

        await bridge.start(port)
        await harness.wait_for_connection_generation(1)
    finally:
        await harness.stop()
        await bridge.stop()


async def test_failed_tcp_connections_leave_no_task_behind() -> None:
    bridge_port = await find_closed_port()
    tasks_before = len(asyncio.all_tasks())

    for _ in range(5):
        with pytest.raises(ConnectionRefusedError):
            await connect_over_tcp(LOCAL_HOST, bridge_port, FAST_WORKER_TIMING)
    await asyncio.sleep(0.01)

    assert len(asyncio.all_tasks()) == tasks_before


async def find_closed_port() -> int:
    server = await asyncio.start_server(lambda reader, writer: None, LOCAL_HOST, 0)
    port: int = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()
    return port
