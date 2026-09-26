"""Processing the inbox rows still "received", in id order, and queueing their replies.

The database is the list of work: the processor reads the unprocessed rows after every wake-up,
so rows recorded before a restart, or left behind by a failed pass, are processed in the same
order as new ones. A sign-in request is stored without its password, so the recorder hands the
frame's own text over in memory; a sign-in row processed after a restart has no password, gets
no answer, and the client's retry is answered instead.

For a new direct message that arrived by flood, the route to its sender is reset before any
reply to it is queued: the node's stored route may be what made the sender's own direct
attempt fail.
"""

import logging
from collections.abc import Callable

from messaging.inbound_log import find_unprocessed_inbox_row_ids, record_flood_arrival_route_reset
from messaging.request_processing import InboundProcessingResult, process_inbound_direct_message
from worker.clock import Clock, wait_for_any_event_or_timeout
from worker.database_access import run_in_database_thread
from worker.node_gateway import NodeGateway, NodeGatewayError
from worker.reply_queue import ReplyQueue
from worker.worker_queries import read_contact_public_key
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

UNPROCESSED_ROWS_PER_PASS = 100


class InboundProcessor:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        reply_queue: ReplyQueue,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
        request_reconciliation: Callable[[str], None],
    ) -> None:
        self._gateway = gateway
        self._reply_queue = reply_queue
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self._request_reconciliation = request_reconciliation
        self._original_texts: dict[int, str] = {}

    def accept_recorded_row(self, inbox_row_id: int, original_text: str) -> None:
        self._original_texts[inbox_row_id] = original_text
        self._signals.inbox_rows_recorded.set()

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            self._signals.inbox_rows_recorded.clear()
            unprocessed_row_ids = await run_in_database_thread(
                find_unprocessed_inbox_row_ids, UNPROCESSED_ROWS_PER_PASS
            )
            for inbox_row_id in unprocessed_row_ids:
                if self._signals.is_shutting_down:
                    return
                await self.process_row(inbox_row_id)
            if len(unprocessed_row_ids) == UNPROCESSED_ROWS_PER_PASS:
                continue
            await wait_for_any_event_or_timeout(
                self._clock,
                [self._signals.inbox_rows_recorded, self._signals.shutdown_requested],
                self._timing.database_sweep_seconds,
            )

    async def process_row(self, inbox_row_id: int) -> None:
        original_text = self._original_texts.pop(inbox_row_id, None)
        processing_result = await run_in_database_thread(
            process_inbound_direct_message, inbox_row_id, self._clock.now(), original_text
        )
        if processing_result.was_already_processed:
            return

        if processing_result.reconciliation_needed:
            self._request_reconciliation(
                f"a direct message came from a node the contacts table does not know (row {inbox_row_id})"
            )
        if processing_result.flood_arrival_route_reset_needed and self._worker_state.is_running:
            await self._reset_route_after_flood_arrival(processing_result)
        self._queue_replies(processing_result)
        # Not only replies change what may be sent: an acknowledgement can bring a round forward,
        # move a refresh on or end a delivery that held a device's place, and a sign-in can cancel
        # or revive deliveries.
        self._signals.sender_wakeup.set()

    async def _reset_route_after_flood_arrival(self, processing_result: InboundProcessingResult) -> None:
        if processing_result.contact_id is None:
            return
        public_key = await run_in_database_thread(read_contact_public_key, processing_result.contact_id)
        if public_key is None:
            return
        try:
            await self._gateway.reset_path(public_key)
        except NodeGatewayError as reset_error:
            logger.warning(
                "The route to contact %d could not be reset after a flood arrival: %s",
                processing_result.contact_id,
                reset_error,
            )
            return
        await run_in_database_thread(record_flood_arrival_route_reset, processing_result.inbox_row_id)

    def _queue_replies(self, processing_result: InboundProcessingResult) -> None:
        for queued_reply in processing_result.replies:
            self._reply_queue.add_reply(
                contact_id=queued_reply.contact_id,
                reply_key=queued_reply.reply_key,
                text=queued_reply.text,
                readiness=queued_reply.readiness,
                now=self._clock.monotonic(),
            )
