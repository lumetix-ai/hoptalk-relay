"""Handling the node's pushes that the subscription callbacks queued, one at a time and in order."""

import asyncio
import logging
from collections.abc import Callable

from directory.node_sync import return_contact_to_pending_add
from messaging.route_reset_evidence import record_path_update
from worker.acknowledgement_tracker import AcknowledgementTracker
from worker.clock import Clock
from worker.database_access import run_in_database_thread
from worker.node_event_subscriptions import (
    AdvertHeard,
    KnownContactAdvertised,
    NodeAcknowledgement,
    NodeContactTableFull,
    NodeDeletedContact,
    NodeEvent,
    PathUpdated,
)
from worker.pairing_advertiser import PairingAdvertiser
from worker.worker_state import WorkerState

logger = logging.getLogger(__name__)

CONTACT_DELETED_BY_NODE_ERROR = "The node deleted this contact by itself; it is added again."
CONTACT_TABLE_FULL_ERROR = "The node reported that its contact table is full."


class NodeEventRouter:
    def __init__(
        self,
        *,
        node_event_queue: asyncio.Queue[NodeEvent],
        acknowledgement_tracker: AcknowledgementTracker,
        pairing_advertiser: PairingAdvertiser,
        worker_state: WorkerState,
        clock: Clock,
        request_reconciliation: Callable[[str], None],
    ) -> None:
        self._node_event_queue = node_event_queue
        self._acknowledgement_tracker = acknowledgement_tracker
        self._pairing_advertiser = pairing_advertiser
        self._worker_state = worker_state
        self._clock = clock
        self._request_reconciliation = request_reconciliation

    async def run(self) -> None:
        while True:
            node_event = await self._node_event_queue.get()
            await self.handle_node_event(node_event)

    async def handle_node_event(self, node_event: NodeEvent) -> None:
        match node_event:
            case NodeAcknowledgement():
                await self._acknowledgement_tracker.handle_node_acknowledgement(node_event)
            case PathUpdated():
                await run_in_database_thread(record_path_update, node_event.public_key, self._clock.now())
            case AdvertHeard():
                await self._pairing_advertiser.capture_heard_advert(node_event.contact_record)
            case KnownContactAdvertised():
                logger.debug("Contact %s advertised.", node_event.public_key[:12])
            case NodeDeletedContact():
                await self._handle_deleted_contact(node_event)
            case NodeContactTableFull():
                logger.error(CONTACT_TABLE_FULL_ERROR)
                self._worker_state.record_error(CONTACT_TABLE_FULL_ERROR)

    async def _handle_deleted_contact(self, node_event: NodeDeletedContact) -> None:
        """It should never happen, since overwriting the oldest contact is switched off."""
        contact_id = await run_in_database_thread(
            return_contact_to_pending_add, node_event.public_key, CONTACT_DELETED_BY_NODE_ERROR
        )
        error_message = f"The node deleted contact {node_event.public_key[:12]} by itself."
        logger.error(error_message)
        self._worker_state.record_error(error_message)
        if contact_id is not None:
            self._request_reconciliation(error_message)
