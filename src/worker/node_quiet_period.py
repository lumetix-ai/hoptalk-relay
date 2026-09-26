"""Waiting until no packet awaits a firmware ACK, before the node's contact array may shift or its RAM is lost.

Removing a contact shifts the node's contact array under its pending ACK entries, and a restart
loses the ACK table. The database decides, so packets sent before a worker restart count too.
The caller pauses sending first, so the wait cannot be prolonged by new packets.
"""

from messaging.outbound_packets import is_any_packet_awaiting_node_acknowledgement
from worker.clock import Clock
from worker.database_access import run_in_database_thread
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming


async def wait_until_no_packet_awaits_acknowledgement(
    clock: Clock, timing: WorkerTiming, wait_seconds: float, worker_state: WorkerState
) -> bool:
    """False when packets still await an ACK after wait_seconds, or the worker is shutting down.

    The last check reads the database as of the end of the wait or later, so a wait that lasts
    past the latest acknowledgement deadline always ends quiet.
    """
    monotonic_deadline = clock.monotonic() + wait_seconds
    while True:
        wait_has_ended = clock.monotonic() >= monotonic_deadline
        if not await run_in_database_thread(is_any_packet_awaiting_node_acknowledgement, clock.now()):
            return True
        if wait_has_ended or worker_state.signals.is_shutting_down:
            return False
        await clock.sleep(timing.quiet_poll_seconds)
