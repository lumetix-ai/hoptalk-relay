"""Writing worker_status, the panel's only view of the worker: every few seconds, and at once on a change.

The heartbeat is how the panel tells a live worker from a dead one, so it is written even while
the node is away; the gauges come from the worker's memory and from a few cheap counts.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from messaging.outbound_packets import count_pending_route_resets
from messaging.outbound_scheduling import count_due_work
from node.models import WorkerStatus
from node.worker_status import upsert_worker_status
from worker.acknowledgement_tracker import AcknowledgementTracker
from worker.clock import Clock, wait_for_any_event_or_timeout
from worker.database_access import run_in_database_thread
from worker.reply_queue import ReplyQueue
from worker.worker_state import WorkerRuntimeStatus, WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class InMemoryGauges:
    packets_awaiting_node_acknowledgement: int
    replies_queued: int


class StatusReporter:
    def __init__(
        self,
        *,
        worker_state: WorkerState,
        acknowledgement_tracker: AcknowledgementTracker,
        reply_queue: ReplyQueue,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._acknowledgement_tracker = acknowledgement_tracker
        self._reply_queue = reply_queue
        self._clock = clock
        self._timing = timing

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            self._signals.status_changed.clear()
            await self.write_status()
            await wait_for_any_event_or_timeout(
                self._clock,
                [self._signals.status_changed, self._signals.shutdown_requested],
                self._timing.status_interval_seconds,
            )

    async def write_status(self) -> None:
        gauges = InMemoryGauges(
            packets_awaiting_node_acknowledgement=self._acknowledgement_tracker.count_packets_awaiting_acknowledgement(),
            replies_queued=len(self._reply_queue),
        )
        await run_in_database_thread(write_worker_status, self._worker_state.runtime_status, gauges, self._clock.now())

    async def write_final_status(self) -> None:
        """At shutdown: the worker no longer holds the node."""
        runtime_status = self._worker_state.runtime_status
        runtime_status.relay_mode = WorkerStatus.RelayMode.DISCONNECTED
        runtime_status.connection_state = WorkerStatus.ConnectionState.DISCONNECTED
        runtime_status.connected_since = None
        await self.write_status()


def write_worker_status(runtime_status: WorkerRuntimeStatus, gauges: InMemoryGauges, now: datetime) -> None:
    due_work_counts = count_due_work(now)
    connected_node = runtime_status.connected_node
    upsert_worker_status(
        WorkerStatus(
            worker_instance_id=runtime_status.worker_instance_id,
            process_started_at=runtime_status.process_started_at,
            heartbeat_at=now,
            relay_mode=runtime_status.relay_mode,
            connection_state=runtime_status.connection_state,
            connection_generation=runtime_status.connection_generation,
            connected_since=runtime_status.connected_since,
            transport_description=runtime_status.transport_description,
            node_public_key=connected_node.public_key if connected_node else "",
            node_name=connected_node.name if connected_node else "",
            node_firmware_version=connected_node.firmware_version if connected_node else "",
            node_model=connected_node.model if connected_node else "",
            node_protocol_version=connected_node.protocol_version if connected_node else None,
            node_contact_count=runtime_status.node_contact_count,
            node_clock_offset_seconds=runtime_status.node_clock_offset_seconds,
            node_radio_summary=connected_node.radio_summary if connected_node else "",
            settings_drift=list(runtime_status.settings_drift),
            effective_configuration=dict(runtime_status.effective_configuration),
            node_identity_backup_state=runtime_status.node_identity_backup_state,
            packets_awaiting_node_acknowledgement=gauges.packets_awaiting_node_acknowledgement,
            replies_queued=gauges.replies_queued,
            deliveries_due=due_work_counts.deliveries_due,
            receipts_due=due_work_counts.receipts_due,
            pending_route_resets=count_pending_route_resets(),
            consecutive_connect_failures=runtime_status.consecutive_connect_failures,
            last_error_message=runtime_status.last_error_message,
            last_error_at=runtime_status.last_error_at,
        )
    )
