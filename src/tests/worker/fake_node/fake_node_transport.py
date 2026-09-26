"""The connection object meshcore.MeshCore drives, and the factory that builds connected clients from it.

`FakeNodeTransport` implements the interface MeshCore needs from its connection (`connect`,
`disconnect`, `send`, `set_reader`, `set_disconnect_callback`). Node-to-host bytes pass through a
real `meshcore.serial_cx.SerialConnection` that is never opened: its `3E len16` deframer gets them in
random chunk sizes from `loop.call_later`, as it would get them from a USB port, and it hands every
complete frame to the library's MessageReader as a task of its own. Host-to-node payloads are framed
`3C len16` and parsed by the fake node's own deframer. Like meshcore's own connections it keeps a
`transport` while connected; closing that directly ends the link.

`FakeNodeConnector` is the injectable factory: `await connector()` builds a new transport and a new
`MeshCore(transport, auto_reconnect=False, default_timeout=...)`, connects it, and returns it, or
None when the node did not answer the app start, or raises when `connect()` failed; the same
outcomes as `MeshCore.create_tcp`/`create_serial`. Call it again after a link loss or a reboot to
reconnect: every call gets a fresh deframer, as a fresh `create_serial` would.
"""

import asyncio
import contextlib
import errno
import random
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any

from meshcore import MeshCore
from meshcore.serial_cx import SerialConnection

from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import frame_host_to_node, frame_node_to_host
from tests.worker.fake_node.waiting import wait_until

NEVER_OPENED_PORT_NAME = "fake-node-port-never-opened"
SERIAL_BAUD_RATE = 115200
CONNECTION_INFORMATION = "fake-node"
# The reasons meshcore's SerialConnection gives when the port goes away or a write finds it closed.
SERIAL_DISCONNECT_REASON = "serial_disconnect"
SERIAL_TRANSPORT_LOST_REASON = "serial_transport_lost"
CLIENT_DISCONNECT_TIMEOUT_SECONDS = 2.0

type DisconnectCallback = Callable[[str], Coroutine[Any, Any, None]]


class FakeNodeUnavailableError(OSError):
    """The port cannot be opened: the node is rebooting, re-enumerating or powered off."""


@dataclass(frozen=True)
class ConnectSucceeds:
    """connect() behaves as the node's state dictates: it succeeds while the node is running."""


@dataclass(frozen=True)
class ConnectReturnsNothing:
    """connect() returns None, which MeshCore.connect turns into ConnectionError."""


@dataclass(frozen=True)
class ConnectRaises:
    exception: BaseException


type ScriptedConnectResult = ConnectSucceeds | ConnectReturnsNothing | ConnectRaises


@dataclass(frozen=True, kw_only=True)
class SerialLinkTiming:
    maximum_chunk_bytes: int = 48
    # Before the first chunk of bytes the node wrote while the host was reading nothing; later
    # chunks follow on the next event-loop turns, since loop timers wait a millisecond at least.
    maximum_first_chunk_delay_seconds: float = 0.0005


class FakeByteStreamTransport:
    """Stands for the asyncio transport meshcore's TCPConnection and SerialConnection keep as
    `transport`, which a worker may close directly on teardown."""

    def __init__(self, owner: FakeNodeTransport) -> None:
        self._owner = owner
        self.was_closed = False

    def close(self) -> None:
        """Closing the socket or port ends the link: the disconnect callback runs if it was open."""
        self.was_closed = True
        self._owner.simulate_link_loss()

    def is_closing(self) -> bool:
        return self.was_closed


class ConnectScript:
    """The outcomes of the next connect() calls, consumed in order; afterwards connect() just works."""

    def __init__(self) -> None:
        self._results: deque[ScriptedConnectResult] = deque()

    def append(self, *results: ScriptedConnectResult) -> None:
        self._results.extend(results)

    def take_next(self) -> ScriptedConnectResult:
        if self._results:
            return self._results.popleft()
        return ConnectSucceeds()


class FakeNodeTransport:
    def __init__(
        self,
        firmware: FakeCompanionFirmware,
        *,
        connect_script: ConnectScript | None = None,
        random_generator: random.Random | None = None,
        link_timing: SerialLinkTiming | None = None,
    ) -> None:
        self.firmware = firmware
        self._connect_script = connect_script or ConnectScript()
        self._random = random_generator or random.Random(0)
        self._link_timing = link_timing or SerialLinkTiming()
        self._deframer = SerialConnection(NEVER_OPENED_PORT_NAME, SERIAL_BAUD_RATE)
        self._disconnect_callback: DisconnectCallback | None = None
        self._is_open = False
        self._undelivered_bytes = bytearray()
        self._delivery_timer: asyncio.TimerHandle | None = None
        self._cut_is_requested_for_next_frame = False
        self._bytes_left_before_cut: int | None = None
        self._cut_reason = SERIAL_DISCONNECT_REASON
        self._disconnect_callback_tasks: set[asyncio.Task[None]] = set()
        self.transport: FakeByteStreamTransport | None = None
        self.sent_payloads: list[bytes] = []
        self.delivered_frame_count = 0
        self.delivered_chunk_count = 0

    @property
    def is_open(self) -> bool:
        return self._is_open

    # ----- the connection interface MeshCore uses ----------------------------------------------

    async def connect(self) -> str | None:
        match self._connect_script.take_next():
            case ConnectReturnsNothing():
                return None
            case ConnectRaises(exception=exception):
                raise exception
        if not self.firmware.accepts_host_connections:
            raise FakeNodeUnavailableError(
                errno.ENOENT, f"could not open port: node {self.firmware.label} is not there"
            )
        self._is_open = True
        self.transport = FakeByteStreamTransport(self)
        self.firmware.attach_host_link(self)
        return CONNECTION_INFORMATION

    async def disconnect(self) -> None:
        self._close()
        if self.transport is not None:
            self.transport.was_closed = True
            self.transport = None

    async def send(self, data: bytes) -> None:
        """Like SerialConnection.send: with the port gone, the frame is dropped and the loss reported."""
        if not self._is_open:
            if self._disconnect_callback is not None:
                await self._disconnect_callback(SERIAL_TRANSPORT_LOST_REASON)
            return
        payload = bytes(data)
        self.sent_payloads.append(payload)
        self.firmware.receive_host_bytes(frame_host_to_node(payload))

    def set_reader(self, reader: Any) -> None:
        self._deframer.set_reader(reader)

    def set_disconnect_callback(self, callback: DisconnectCallback) -> None:
        self._disconnect_callback = callback

    # ----- the node's side of the link ---------------------------------------------------------

    def deliver_frame_to_host(self, frame: bytes) -> None:
        if not self._is_open:
            return
        framed_bytes = frame_node_to_host(frame)
        self.delivered_frame_count += 1
        if self._cut_is_requested_for_next_frame:
            self._cut_is_requested_for_next_frame = False
            self._bytes_left_before_cut = len(self._undelivered_bytes) + len(framed_bytes) // 2
        self._undelivered_bytes += framed_bytes
        self._schedule_next_chunk(
            delay_seconds=self._random.uniform(0, self._link_timing.maximum_first_chunk_delay_seconds)
        )

    def node_dropped_link(self, reason: str) -> None:
        self._lose_link(reason)

    # ----- test controls -----------------------------------------------------------------------

    def simulate_link_loss(self, reason: str = SERIAL_DISCONNECT_REASON) -> None:
        """The port disappears: bytes not yet read are lost and the disconnect callback runs."""
        self._lose_link(reason)

    def cut_link_during_next_frame(self, reason: str = SERIAL_DISCONNECT_REASON) -> None:
        """Deliver only the first half of the next frame, then lose the link: the deframer keeps
        the half frame, and reusing this transport after connect() corrupts the next frame."""
        self._cut_is_requested_for_next_frame = True
        self._cut_reason = reason

    async def wait_for_disconnect_callbacks(self) -> None:
        if self._disconnect_callback_tasks:
            await asyncio.gather(*self._disconnect_callback_tasks, return_exceptions=True)

    # ----- delivery in chunks ------------------------------------------------------------------

    def _schedule_next_chunk(self, *, delay_seconds: float) -> None:
        if self._delivery_timer is not None or not self._undelivered_bytes:
            return
        self._delivery_timer = asyncio.get_running_loop().call_later(delay_seconds, self._deliver_next_chunk)

    def _deliver_next_chunk(self) -> None:
        self._delivery_timer = None
        if not self._is_open:
            return
        chunk_size = self._random.randint(1, self._link_timing.maximum_chunk_bytes)
        if self._bytes_left_before_cut is not None:
            chunk_size = min(chunk_size, self._bytes_left_before_cut)
        chunk = bytes(self._undelivered_bytes[:chunk_size])
        del self._undelivered_bytes[:chunk_size]
        if chunk:
            self.delivered_chunk_count += 1
            self._deframer.handle_rx(chunk)
        if self._bytes_left_before_cut is not None:
            self._bytes_left_before_cut -= len(chunk)
            if self._bytes_left_before_cut <= 0:
                self._lose_link(self._cut_reason)
                return
        self._schedule_next_chunk(delay_seconds=0)

    def _lose_link(self, reason: str) -> None:
        link_was_open = self._is_open
        self._close()
        if link_was_open and self._disconnect_callback is not None:
            callback_task = asyncio.get_running_loop().create_task(self._disconnect_callback(reason))
            self._disconnect_callback_tasks.add(callback_task)
            callback_task.add_done_callback(self._disconnect_callback_tasks.discard)

    def _close(self) -> None:
        self._is_open = False
        self.firmware.detach_host_link(self)
        if self._delivery_timer is not None:
            self._delivery_timer.cancel()
            self._delivery_timer = None
        self._undelivered_bytes.clear()
        self._cut_is_requested_for_next_frame = False
        self._bytes_left_before_cut = None


class FakeNodeConnector:
    """Builds connected meshcore.MeshCore clients for one fake node; see the module docstring."""

    def __init__(
        self,
        firmware: FakeCompanionFirmware,
        *,
        default_timeout: float | None = 10.0,
        seed: int = 0,
        link_timing: SerialLinkTiming | None = None,
    ) -> None:
        self.firmware = firmware
        self.default_timeout = default_timeout
        self._random = random.Random(seed)
        self._link_timing = link_timing or SerialLinkTiming()
        self._connect_script = ConnectScript()
        self.transports: list[FakeNodeTransport] = []
        self.clients: list[Any] = []

    async def __call__(self) -> Any:
        return await self.create_meshcore_client()

    async def create_meshcore_client(self, *, default_timeout: float | None = None) -> Any:
        """A connected MeshCore, or None when the app start got no answer; raises when connect() failed."""
        transport = self.create_transport()
        client = MeshCore(
            transport,
            default_timeout=self.default_timeout if default_timeout is None else default_timeout,
            auto_reconnect=False,
        )
        self.clients.append(client)
        try:
            app_start_result = await client.connect()
        except BaseException:
            # MeshCore.connect leaves its dispatcher task running when the connection raised.
            await client.dispatcher.stop()
            raise
        if app_start_result is None:
            await client.disconnect()
            return None
        return client

    def create_transport(self) -> FakeNodeTransport:
        transport = FakeNodeTransport(
            self.firmware,
            connect_script=self._connect_script,
            random_generator=random.Random(self._random.getrandbits(64)),
            link_timing=self._link_timing,
        )
        self.transports.append(transport)
        return transport

    def script_connect_results(self, *results: ScriptedConnectResult) -> None:
        self._connect_script.append(*results)

    @property
    def current_transport(self) -> FakeNodeTransport | None:
        return self.transports[-1] if self.transports else None

    def simulate_link_loss(self, reason: str = SERIAL_DISCONNECT_REASON) -> None:
        if self.current_transport is not None:
            self.current_transport.simulate_link_loss(reason)

    def cut_link_during_next_frame(self, reason: str = SERIAL_DISCONNECT_REASON) -> None:
        if self.current_transport is not None:
            self.current_transport.cut_link_during_next_frame(reason)

    async def wait_until_node_accepts_connections(self, *, timeout_seconds: float = 5.0) -> None:
        await wait_until(
            lambda: self.firmware.accepts_host_connections,
            timeout_seconds=timeout_seconds,
            description=f"node {self.firmware.label} to accept connections",
        )

    async def close(self) -> None:
        """Disconnect every client this connector built and wait for pending disconnect callbacks."""
        for client in self.clients:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), CLIENT_DISCONNECT_TIMEOUT_SECONDS)
            client.stop()
        for transport in self.transports:
            await transport.disconnect()
            await transport.wait_for_disconnect_callbacks()
