"""Pulling waiting messages out of the node's offline queue.

The node queues every direct message and channel message it receives, and hands them over one
per request. The queue lives in the node's RAM, and a MESSAGES_WAITING push sent while the link
was down is lost, so the drainer also pulls on a timer, after every handshake in relay mode
running, and before the node restarts. It pulls only while few frames wait to be recorded: a
pulled frame is gone from the node, so the fewer of them live only in memory, the better.
"""

import asyncio
import logging

from messaging.inbound_log import ReceivedDirectMessageFrame
from worker.clock import Clock, wait_for_any_event_or_timeout
from worker.node_gateway import NextMessageOutcome, NodeGateway, NodeGatewayError
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


class MessageDrainer:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        inbound_frame_queue: asyncio.Queue[ReceivedDirectMessageFrame],
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._gateway = gateway
        self._inbound_frame_queue = inbound_frame_queue
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            await wait_for_any_event_or_timeout(
                self._clock,
                [self._signals.drain_requested, self._signals.shutdown_requested],
                self._timing.drain_poll_seconds,
            )
            self._signals.drain_requested.clear()
            if self._worker_state.is_running and not self._signals.is_shutting_down:
                await self.drain_offline_queue(only_while_running=True)

    async def drain_offline_queue(self, *, only_while_running: bool) -> int:
        """Pull until the node has nothing left; returns how many frames it handed over.

        Node commands drain outside relay mode running too: before a reboot or a factory reset,
        whatever waits in the node's RAM would be lost.
        """
        pulled_frame_count = 0
        consecutive_lost_replies = 0
        while not self._signals.is_shutting_down:
            if only_while_running and not self._worker_state.is_running:
                break
            await self._wait_until_few_frames_wait_to_be_recorded()
            try:
                next_message_outcome = await self._gateway.get_next_message()
            except NodeGatewayError as drain_error:
                logger.warning("Draining the node's waiting messages stopped: %s", drain_error)
                break

            if next_message_outcome == NextMessageOutcome.NO_MORE_MESSAGES:
                break
            if next_message_outcome == NextMessageOutcome.REPLY_LOST:
                consecutive_lost_replies += 1
                if consecutive_lost_replies >= self._timing.lost_drain_replies_before_stop:
                    logger.warning(
                        "Draining stopped: the node did not answer %d times in a row.", consecutive_lost_replies
                    )
                    break
                continue
            consecutive_lost_replies = 0
            pulled_frame_count += 1
        return pulled_frame_count

    async def _wait_until_few_frames_wait_to_be_recorded(self) -> None:
        while self._inbound_frame_queue.qsize() >= self._timing.maximum_unrecorded_frames:
            self._signals.inbound_frame_recorded.clear()
            if self._inbound_frame_queue.qsize() < self._timing.maximum_unrecorded_frames:
                return
            await wait_for_any_event_or_timeout(
                self._clock,
                [self._signals.inbound_frame_recorded, self._signals.shutdown_requested],
                self._timing.drain_poll_seconds,
            )
            if self._signals.is_shutting_down:
                return
