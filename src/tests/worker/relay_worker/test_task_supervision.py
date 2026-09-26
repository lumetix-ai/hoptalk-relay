"""Supervised worker tasks: a failure restarts the task alone, after a pause that doubles and later resets."""

import asyncio
from uuid import uuid4

import pytest
from django.utils import timezone

from worker.clock import SystemClock
from worker.task_supervision import run_supervised_task
from worker.worker_state import WorkerRuntimeStatus, WorkerSignals, WorkerState
from worker.worker_timing import WorkerTiming

TIMING = WorkerTiming(
    task_restart_initial_seconds=1.0, task_restart_maximum_seconds=4.0, task_healthy_after_seconds=60.0
)


class RecordingClock(SystemClock):
    """Sleeps no time at all, but records every pause the supervisor asked for; its time can be moved."""

    def __init__(self) -> None:
        self.offset_seconds = 0.0
        self.requested_pauses: list[float] = []

    def monotonic(self) -> float:
        return super().monotonic() + self.offset_seconds

    async def sleep_until(self, monotonic_deadline: float) -> None:
        self.requested_pauses.append(round(monotonic_deadline - self.monotonic(), 3))


def build_worker_state(clock: RecordingClock) -> WorkerState:
    return WorkerState(
        runtime_status=WorkerRuntimeStatus(
            worker_instance_id=uuid4(),
            process_started_at=timezone.now(),
            transport_description="",
            effective_configuration={},
        ),
        signals=WorkerSignals(),
        clock=clock,
    )


async def test_a_failing_task_restarts_after_pauses_that_double_up_to_the_maximum() -> None:
    clock = RecordingClock()
    worker_state = build_worker_state(clock)
    failures_left = [5]

    async def fail_five_times() -> None:
        if failures_left[0]:
            failures_left[0] -= 1
            raise RuntimeError("the database went away")

    await run_supervised_task(fail_five_times, "flaky", worker_state=worker_state, clock=clock, timing=TIMING)

    assert clock.requested_pauses == [1.0, 2.0, 4.0, 4.0, 4.0]
    assert "flaky" in worker_state.runtime_status.last_error_message
    assert "the database went away" in worker_state.runtime_status.last_error_message


async def test_a_task_that_was_healthy_for_a_minute_restarts_after_the_shortest_pause_again() -> None:
    clock = RecordingClock()
    worker_state = build_worker_state(clock)
    run_count = [0]

    async def fail_quickly_twice_then_after_a_long_run() -> None:
        run_count[0] += 1
        if run_count[0] == 3:
            clock.offset_seconds += 61
        if run_count[0] <= 3:
            raise RuntimeError("failed")

    await run_supervised_task(
        fail_quickly_twice_then_after_a_long_run, "sometimes", worker_state=worker_state, clock=clock, timing=TIMING
    )

    assert clock.requested_pauses == [1.0, 2.0, 1.0]


async def test_a_task_is_not_restarted_once_shutdown_was_requested() -> None:
    clock = RecordingClock()
    worker_state = build_worker_state(clock)
    run_count = [0]

    async def fail_and_request_shutdown() -> None:
        run_count[0] += 1
        worker_state.signals.shutdown_requested.set()
        raise RuntimeError("failed during shutdown")

    await run_supervised_task(
        fail_and_request_shutdown, "stopping", worker_state=worker_state, clock=clock, timing=TIMING
    )

    assert run_count == [1]


async def test_cancelling_a_supervised_task_is_not_taken_for_a_failure() -> None:
    clock = RecordingClock()
    worker_state = build_worker_state(clock)

    async def be_cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_supervised_task(be_cancelled, "cancelled", worker_state=worker_state, clock=clock, timing=TIMING)
    assert worker_state.runtime_status.last_error_message == ""
