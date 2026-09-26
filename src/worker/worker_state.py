"""What the worker's tasks share in memory: the wake-up events, the live status and the pause of sending.

Only the event loop's thread touches these objects, so they need no locks of their own; the
asyncio events are the tasks' only way of waking each other.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from node.models import WorkerStatus
from worker.clock import Clock

logger = logging.getLogger(__name__)

RelayMode = WorkerStatus.RelayMode
ConnectionState = WorkerStatus.ConnectionState
NodeIdentityBackupState = WorkerStatus.NodeIdentityBackupState


@dataclass
class WorkerSignals:
    """The wake-up calls between tasks; a task clears its own event before it acts on it."""

    sender_wakeup: asyncio.Event = field(default_factory=asyncio.Event)
    drain_requested: asyncio.Event = field(default_factory=asyncio.Event)
    reconcile_requested: asyncio.Event = field(default_factory=asyncio.Event)
    commands_available: asyncio.Event = field(default_factory=asyncio.Event)
    relay_mode_changed: asyncio.Event = field(default_factory=asyncio.Event)
    pairing_changed: asyncio.Event = field(default_factory=asyncio.Event)
    status_changed: asyncio.Event = field(default_factory=asyncio.Event)
    inbox_rows_recorded: asyncio.Event = field(default_factory=asyncio.Event)
    inbound_frame_recorded: asyncio.Event = field(default_factory=asyncio.Event)
    acknowledgement_deadlines_changed: asyncio.Event = field(default_factory=asyncio.Event)
    shutdown_requested: asyncio.Event = field(default_factory=asyncio.Event)
    # The steps of an orderly shutdown after shutdown_requested: nothing more can be taken from
    # the node once inbound_frames_finished is set, and the link closes on link_close_requested.
    inbound_frames_finished: asyncio.Event = field(default_factory=asyncio.Event)
    link_close_requested: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def is_shutting_down(self) -> bool:
        return self.shutdown_requested.is_set()


@dataclass(frozen=True, kw_only=True)
class ConnectedNodeDescription:
    """What the last handshake learned about the attached node, for the status row and the relay mode."""

    public_key: str
    name: str
    firmware_version: str
    model: str
    protocol_version: int
    radio_summary: str


@dataclass
class WorkerRuntimeStatus:
    """The in-memory side of worker_status; the status reporter writes it with the database gauges."""

    worker_instance_id: UUID
    process_started_at: datetime
    transport_description: str
    effective_configuration: dict[str, Any]
    relay_mode: WorkerStatus.RelayMode = RelayMode.DISCONNECTED
    connection_state: WorkerStatus.ConnectionState = ConnectionState.DISCONNECTED
    connection_generation: int = 0
    connected_since: datetime | None = None
    connected_node: ConnectedNodeDescription | None = None
    node_contact_count: int | None = None
    node_clock_offset_seconds: int | None = None
    settings_drift: list[dict[str, Any]] = field(default_factory=list)
    node_identity_backup_state: WorkerStatus.NodeIdentityBackupState = NodeIdentityBackupState.NOT_CHECKED
    consecutive_connect_failures: int = 0
    last_error_message: str = ""
    last_error_at: datetime | None = None


class WorkerState:
    """The live status and the signals, with the few operations every task needs."""

    def __init__(self, *, runtime_status: WorkerRuntimeStatus, signals: WorkerSignals, clock: Clock) -> None:
        self.runtime_status = runtime_status
        self.signals = signals
        self.clock = clock
        self.sending_gate = SendingGate()

    @property
    def relay_mode(self) -> WorkerStatus.RelayMode:
        return self.runtime_status.relay_mode

    @property
    def is_running(self) -> bool:
        """Relay mode running: the identity matches, so the node may be drained, reconciled and sent to."""
        return self.runtime_status.relay_mode == RelayMode.RUNNING

    @property
    def is_node_connected(self) -> bool:
        return self.runtime_status.connection_state == ConnectionState.CONNECTED

    def record_error(self, error_message: str) -> None:
        """Keep the message as the last error the panel shows; the caller has logged it already."""
        self.runtime_status.last_error_message = error_message
        self.runtime_status.last_error_at = self.clock.now()
        self.signals.status_changed.set()

    def report_status_change(self) -> None:
        self.signals.status_changed.set()

    def record_node_identity_backup_state(self, backup_state: WorkerStatus.NodeIdentityBackupState) -> None:
        if self.runtime_status.node_identity_backup_state != backup_state:
            self.runtime_status.node_identity_backup_state = backup_state
            self.report_status_change()

    @asynccontextmanager
    async def pause_sending(self, reason: str) -> AsyncIterator[None]:
        """No packet starts inside the block; the sender is woken when it ends."""
        try:
            async with self.sending_gate.pause_sending(reason):
                yield
        finally:
            self.signals.sender_wakeup.set()


class SendingGate:
    """Holds the sender loop back while contacts are removed or the node is about to restart.

    The sender takes `send_step_lock` for every prepare, send and record step and checks
    `is_paused` inside it. A pause therefore takes effect once the lock was taken after it: from
    then on no packet can start, and none is half sent.
    """

    def __init__(self) -> None:
        self.send_step_lock = asyncio.Lock()
        self._pause_reasons: list[str] = []

    @property
    def is_paused(self) -> bool:
        return bool(self._pause_reasons)

    @asynccontextmanager
    async def pause_sending(self, reason: str) -> AsyncIterator[None]:
        self._pause_reasons.append(reason)
        try:
            async with self.send_step_lock:
                logger.debug("Sending paused: %s", reason)
            yield
        finally:
            self._pause_reasons.remove(reason)
