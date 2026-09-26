"""Waking the worker when the panel changes something: LISTEN on the relay channels, and a periodic sweep.

A notification is only a wake-up call, delivered when the panel's transaction commits; the
tables stay the source of truth. A lost notification (the listener reconnecting, say) is covered
by the sweep, which wakes the command executor and recomputes the relay mode every few seconds.
"""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from psycopg import sql

from node.notification_channels import NotificationChannel
from worker.single_instance_lock import build_database_connection_parameters
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


class DatabaseListener:
    def __init__(
        self,
        *,
        worker_state: WorkerState,
        timing: WorkerTiming,
        handle_contacts_changed: Callable[[], Awaitable[None]],
    ) -> None:
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._timing = timing
        self._handle_contacts_changed = handle_contacts_changed

    async def run(self) -> None:
        connection = await psycopg.AsyncConnection.connect(**build_database_connection_parameters(), autocommit=True)
        try:
            for channel in NotificationChannel:
                await connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel.value)))
            logger.debug("Listening for the panel's notifications.")
            while not self._signals.is_shutting_down:
                await self._handle_notifications_until_sweep(connection)
                self.sweep()
        finally:
            await connection.close()

    async def _handle_notifications_until_sweep(self, connection: psycopg.AsyncConnection[Any]) -> None:
        async for notification in connection.notifies(timeout=self._timing.database_sweep_seconds):
            await self.handle_notification(notification.channel)

    async def handle_notification(self, channel_name: str) -> None:
        match channel_name:
            case NotificationChannel.NODE_COMMANDS:
                self._signals.commands_available.set()
            case NotificationChannel.SETUP_CHANGED:
                self._signals.relay_mode_changed.set()
                self._signals.commands_available.set()
            case NotificationChannel.CONTACTS_CHANGED:
                self._signals.reconcile_requested.set()
                await self._handle_contacts_changed()

    def sweep(self) -> None:
        self._signals.commands_available.set()
        self._signals.relay_mode_changed.set()
