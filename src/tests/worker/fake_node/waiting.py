import asyncio
import time
from collections.abc import Callable

WAIT_POLL_INTERVAL_SECONDS = 0.002


async def wait_until(
    condition: Callable[[], bool], *, timeout_seconds: float = 5.0, description: str = "the condition"
) -> None:
    """Poll the condition until it holds; fail the test with the description when time runs out."""
    deadline = time.monotonic() + timeout_seconds
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout_seconds} s waiting for {description}")
        await asyncio.sleep(WAIT_POLL_INTERVAL_SECONDS)
