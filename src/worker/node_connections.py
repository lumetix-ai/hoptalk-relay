"""Building a connected meshcore client for the configured transport: the supervisor's default factory.

Every connection gets a new MeshCore object with a new transport, so no half-read frame of an
earlier link survives, and auto_reconnect stays off: the connection supervisor reconnects with a
back-off of its own and runs the full handshake every time.

This does what MeshCore.create_tcp and create_serial do (the library's own TCPConnection or
SerialConnection, a MeshCore around it, connect and app start) and also cleans up after a
failure. When the connection itself fails, create_tcp and create_serial leave the client's event
dispatcher task running and return nothing to stop it with, which would leak a task on every
attempt while the node is away. The TCP connect has no timeout of its own, so it is bounded here.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from meshcore import MeshCore
from meshcore.serial_cx import SerialConnection
from meshcore.tcp_cx import TCPConnection

from hoptalk_relay.relay_settings import NodeConnectionSettings, NodeTransport
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

SERIAL_BAUD_RATE = 115200
# create_serial tries a second time with DTR inverted: some boards reset or stay silent otherwise.
SERIAL_DATA_TERMINAL_READY_LEVELS = (True, False)

type NodeClientFactory = Callable[[], Awaitable[Any | None]]


def build_node_client_factory(node_connection: NodeConnectionSettings, timing: WorkerTiming) -> NodeClientFactory:
    async def create_node_client() -> Any | None:
        if node_connection.transport == NodeTransport.TCP:
            return await connect_over_tcp(node_connection.tcp_host, node_connection.tcp_port, timing)
        return await connect_over_serial(node_connection.serial_device, timing)

    return create_node_client


async def connect_over_tcp(host: str, port: int, timing: WorkerTiming) -> Any | None:
    connection = TCPConnection(host, port)
    return await asyncio.wait_for(
        connect_meshcore_client(connection, timing), timeout=timing.tcp_connect_timeout_seconds
    )


async def connect_over_serial(serial_device: str, timing: WorkerTiming) -> Any | None:
    """The device path is opened again on every attempt, so a symlink follows a node that re-enumerated."""
    for data_terminal_ready in SERIAL_DATA_TERMINAL_READY_LEVELS:
        connection = SerialConnection(serial_device, SERIAL_BAUD_RATE, dtr=data_terminal_ready)
        meshcore_client = await asyncio.wait_for(
            connect_meshcore_client(connection, timing), timeout=timing.tcp_connect_timeout_seconds
        )
        if meshcore_client is not None:
            return meshcore_client
        logger.info("The node on %s did not answer the app start with DTR %s.", serial_device, data_terminal_ready)
    return None


async def connect_meshcore_client(connection: Any, timing: WorkerTiming) -> Any | None:
    """A connected client, None when the node did not answer the app start; raises when the link failed."""
    meshcore_client = MeshCore(connection, default_timeout=timing.node_command_timeout_seconds, auto_reconnect=False)
    try:
        app_start_result = await meshcore_client.connect()
    except BaseException:
        stop_client_and_close_transport(meshcore_client, connection)
        raise
    if app_start_result is None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(meshcore_client.disconnect(), timing.client_disconnect_timeout_seconds)
        stop_client_and_close_transport(meshcore_client, connection)
        return None
    return meshcore_client


def stop_client_and_close_transport(meshcore_client: Any, connection: Any) -> None:
    """Synchronous, so it also runs while the attempt is being cancelled by its timeout."""
    meshcore_client.stop()
    close_connection_transport(connection)


def close_connection_transport(connection: Any) -> None:
    """Close the socket or port directly: the library leaves a TCP socket open after its own disconnect heuristic."""
    transport = getattr(connection, "transport", None)
    if transport is not None and not transport.is_closing():
        transport.close()
