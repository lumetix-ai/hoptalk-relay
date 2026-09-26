"""Periodic clean-up: incomplete uploads, the traffic log, commands, heard adverts, login attempts and sessions."""

import logging

from messaging.maintenance import run_messaging_maintenance
from node.node_commands import delete_old_node_commands
from node.pairing_sessions import delete_old_heard_adverts
from panel.operator_authentication import delete_expired_operator_sessions, delete_old_login_attempts
from worker.clock import Clock, wait_for_any_event_or_timeout
from worker.database_access import run_in_database_thread
from worker.node_gateway import NodeGateway
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


class MaintenanceRunner:
    def __init__(self, *, gateway: NodeGateway, worker_state: WorkerState, clock: Clock, timing: WorkerTiming) -> None:
        self._gateway = gateway
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            await self.run_maintenance()
            await wait_for_any_event_or_timeout(
                self._clock, [self._signals.shutdown_requested], self._timing.maintenance_interval_seconds
            )

    async def run_maintenance(self) -> None:
        now = self._clock.now()
        messaging_summary = await run_in_database_thread(run_messaging_maintenance, now)
        deleted_command_count = await run_in_database_thread(delete_old_node_commands, now)
        deleted_advert_count = await run_in_database_thread(delete_old_heard_adverts, now)
        deleted_login_attempt_count = await run_in_database_thread(delete_old_login_attempts, now)
        deleted_session_count = await run_in_database_thread(delete_expired_operator_sessions, now)
        self._gateway.flush_library_contact_caches()
        logger.debug(
            "Maintenance: %d incomplete messages, %d inbox rows, %d packets, %d commands, %d heard adverts, "
            "%d login attempts and %d expired operator sessions deleted.",
            messaging_summary.expired_incomplete_messages,
            messaging_summary.pruned_inbox_rows,
            messaging_summary.pruned_outbound_packets,
            deleted_command_count,
            deleted_advert_count,
            deleted_login_attempt_count,
            deleted_session_count,
        )
