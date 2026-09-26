"""Keeping every worker task alive: a failed task is logged and started again, and nothing else stops.

An exception that escapes a task (a deadlock that survived its retries, a database restart, a
bug) would otherwise end the task group and with it the process, losing the frames that wait to
be recorded and interrupting a running node command. Instead the task restarts after a pause
that doubles while it keeps failing, and starts from the shortest pause again after a stretch of
health.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from worker.clock import Clock, wait_for_any_event_or_timeout
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


async def run_supervised_task(
    task_function: Callable[[], Awaitable[None]],
    task_name: str,
    *,
    worker_state: WorkerState,
    clock: Clock,
    timing: WorkerTiming,
) -> None:
    restart_delay_seconds = timing.task_restart_initial_seconds
    signals = worker_state.signals
    while not signals.is_shutting_down:
        started_at = clock.monotonic()
        try:
            await task_function()
            return
        except asyncio.CancelledError:
            raise
        except Exception as task_error:
            logger.exception("The worker task %s failed; it restarts in %.1f s.", task_name, restart_delay_seconds)
            worker_state.record_error(f"Internal error in the worker task {task_name}: {task_error}")

        if clock.monotonic() - started_at >= timing.task_healthy_after_seconds:
            restart_delay_seconds = timing.task_restart_initial_seconds
        await wait_for_any_event_or_timeout(clock, [signals.shutdown_requested], restart_delay_seconds)
        restart_delay_seconds = min(restart_delay_seconds * 2, timing.task_restart_maximum_seconds)
