"""The worker's sense of time: wall time for the database, monotonic time for its own timers, and sleeping.

Every task takes the clock it is given instead of calling timezone.now(), time.monotonic() or
asyncio.sleep() itself, so a test can move time forward: pairing sessions, retry rounds and
acknowledgement deadlines then pass without the test waiting for them. Tasks wait until a
deadline on the clock's monotonic time rather than for a duration, so a move that happens
while a task is still working out how long to wait counts too.
"""

import asyncio
import time
from collections.abc import Collection
from datetime import datetime
from typing import Any, Protocol

from django.utils import timezone


class Clock(Protocol):
    def now(self) -> datetime:
        """The current wall time, timezone-aware, as the services expect it."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never goes backwards, for intervals inside the process."""
        ...

    async def sleep(self, seconds: float) -> None: ...

    async def sleep_until(self, monotonic_deadline: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return timezone.now()

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))

    async def sleep_until(self, monotonic_deadline: float) -> None:
        await self.sleep(monotonic_deadline - self.monotonic())


def convert_wall_time_to_deadline(clock: Clock, wall_time: datetime) -> float:
    """The monotonic deadline at which the clock's wall time reaches wall_time."""
    return clock.monotonic() + (wall_time - clock.now()).total_seconds()


async def wait_for_any_event_or_timeout(
    clock: Clock, events: list[asyncio.Event], timeout_seconds: float | None
) -> bool:
    """Wait until one of the events is set or the timeout passes; True when an event is set."""
    monotonic_deadline = None if timeout_seconds is None else clock.monotonic() + timeout_seconds
    return await wait_for_any_event_until(clock, events, monotonic_deadline)


async def wait_for_any_event_until(clock: Clock, events: list[asyncio.Event], monotonic_deadline: float | None) -> bool:
    """Wait until one of the events is set or the clock reaches the deadline; True when an event is set."""
    if any(event.is_set() for event in events):
        return True

    waiting_tasks: set[asyncio.Future[Any]] = {asyncio.ensure_future(event.wait()) for event in events}
    if monotonic_deadline is not None:
        waiting_tasks.add(asyncio.ensure_future(clock.sleep_until(monotonic_deadline)))
    try:
        await asyncio.wait(waiting_tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await cancel_and_wait(waiting_tasks)
    return any(event.is_set() for event in events)


async def cancel_and_wait(tasks: Collection[asyncio.Future[Any]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
