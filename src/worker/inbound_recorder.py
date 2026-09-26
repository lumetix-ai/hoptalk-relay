"""Recording every drained direct message in the inbox at once, in arrival order.

A frame the node handed over is gone from the node, so it lives only in memory until it is
recorded: the recorder does nothing else, the drainer stops pulling while ten frames wait, and
at shutdown the recorder finishes the queue before it stops. A frame that cannot be recorded
after a few attempts is logged and given up; the client retries its request anyway.
"""

import asyncio
import logging
from collections.abc import Callable

from messaging.inbound_log import ReceivedDirectMessageFrame, record_inbound_frame
from worker.clock import Clock, cancel_and_wait
from worker.database_access import run_in_database_thread
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


class InboundRecorder:
    def __init__(
        self,
        *,
        inbound_frame_queue: asyncio.Queue[ReceivedDirectMessageFrame],
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
        hand_over_recorded_row: Callable[[int, str], None],
    ) -> None:
        self._inbound_frame_queue = inbound_frame_queue
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self._hand_over_recorded_row = hand_over_recorded_row

    async def run(self) -> None:
        """Returns once no more frames can arrive at shutdown and every waiting frame is recorded."""
        while True:
            frame = await self._take_next_frame()
            if frame is None:
                return
            await self.record_frame(frame)

    async def _take_next_frame(self) -> ReceivedDirectMessageFrame | None:
        if not self._inbound_frame_queue.empty():
            return self._inbound_frame_queue.get_nowait()
        inbound_frames_finished = self._signals.inbound_frames_finished
        if inbound_frames_finished.is_set():
            return None

        frame_getter = asyncio.ensure_future(self._inbound_frame_queue.get())
        finished_waiter = asyncio.ensure_future(inbound_frames_finished.wait())
        try:
            await asyncio.wait({frame_getter, finished_waiter}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            await cancel_and_wait({finished_waiter})
            if not frame_getter.done():
                await cancel_and_wait({frame_getter})
        if frame_getter.done() and not frame_getter.cancelled():
            return frame_getter.result()
        return None

    async def record_frame(self, frame: ReceivedDirectMessageFrame) -> None:
        for attempt_number in range(1, self._timing.inbound_recording_attempts + 1):
            try:
                recorded_frame = await run_in_database_thread(record_inbound_frame, frame, self._clock.now())
            except Exception:
                logger.exception(
                    "Recording a direct message from %s failed (attempt %d).",
                    frame.sender_public_key_prefix,
                    attempt_number,
                )
                await self._clock.sleep(self._timing.inbound_recording_retry_seconds)
                continue

            self._signals.inbound_frame_recorded.set()
            if recorded_frame.is_firmware_repeat:
                logger.debug(
                    "A firmware-level repeat from %s was counted on inbox row %d.",
                    frame.sender_public_key_prefix,
                    recorded_frame.inbox_row_id,
                )
                return
            self._hand_over_recorded_row(recorded_frame.inbox_row_id, frame.text)
            return

        error_message = (
            f"A direct message from {frame.sender_public_key_prefix} could not be recorded and was lost; "
            "the client will retry it."
        )
        logger.error(error_message)
        self._worker_state.record_error(error_message)
        self._signals.inbound_frame_recorded.set()
