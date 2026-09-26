"""Converging the node's contact table to the contacts table, only while the node is the configured one.

A pass lists the node's contacts first and reads the database afterwards, so a contact the panel
adds meanwhile is seen now or by the pass its notification triggers. Missing contacts are added
before extra ones are removed, because removals may have to wait: removing a contact shifts the
node's contact array under its pending ACK entries, so it happens only while no packet awaits a
firmware ACK. The sender starts nothing meanwhile, so the wait ends at the latest ACK deadline
already recorded, at most the longest ACK wait after the last packet. Removals happen only on the
connection the node was listed on and while the relay mode is still running. An add the node
refused as full is tried again once the extras are gone, which the capacity limit of the
contacts table guarantees will fit.

A contact already on the node is never written again, since that would wipe the route the node
learned; the relay's own key is never removed; and a pass that cannot list the node changes
nothing.
"""

import asyncio
import logging
from dataclasses import dataclass, field

from directory.node_sync import (
    ContactForNode,
    list_contacts_for_node,
    mark_contact_add_failed,
    mark_contact_on_node,
)
from messaging.outbound_packets import read_latest_acknowledgement_deadline
from worker.clock import Clock, wait_for_any_event_until
from worker.database_access import run_in_database_thread
from worker.node_contact_records import ListedNodeContact, build_node_contact_record
from worker.node_gateway import NodeGateway, NodeGatewayError, NodeRejectedCommandError
from worker.node_quiet_period import wait_until_no_packet_awaits_acknowledgement
from worker.relay_modes import read_configured_public_key
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

ERR_CODE_TABLE_FULL = 3
CONTACT_TABLE_FULL_ERROR = "The node's contact table is full."


class ReconciliationAbortedError(Exception):
    """The pass stopped before it changed anything more; the message says why."""


@dataclass(kw_only=True)
class ReconciliationSummary:
    added_count: int = 0
    removed_count: int = 0
    failed_count: int = 0
    removals_postponed: bool = False
    node_contact_count: int = 0
    failed_public_keys: list[str] = field(default_factory=list)

    @property
    def changed_anything(self) -> bool:
        return bool(self.added_count or self.removed_count or self.failed_count or self.removals_postponed)

    def to_json(self) -> dict[str, int | bool]:
        return {
            "added": self.added_count,
            "removed": self.removed_count,
            "failed": self.failed_count,
            "removals_postponed": self.removals_postponed,
            "node_contact_count": self.node_contact_count,
        }


class ContactReconciler:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._gateway = gateway
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self._reconciliation_lock = asyncio.Lock()
        self._next_pass_at: float | None = None
        self._next_pass_moved = asyncio.Event()

    def request_reconciliation(self, reason: str) -> None:
        logger.debug("Contact reconciliation requested: %s", reason)
        self._signals.reconcile_requested.set()

    async def run(self) -> None:
        self._next_pass_at = self._clock.monotonic() + self._timing.reconciliation_interval_seconds
        while not self._signals.is_shutting_down:
            self._next_pass_moved.clear()
            await wait_for_any_event_until(
                self._clock,
                [self._signals.reconcile_requested, self._signals.shutdown_requested, self._next_pass_moved],
                self._next_pass_at,
            )
            if self._signals.is_shutting_down:
                return
            if not self._is_pass_due():
                continue
            self._signals.reconcile_requested.clear()
            self._next_pass_at = self._clock.monotonic() + self._timing.reconciliation_interval_seconds
            if not self._worker_state.is_running:
                continue
            try:
                await self.reconcile_contacts()
            except ReconciliationAbortedError as aborted_error:
                logger.warning("Contact reconciliation stopped: %s", aborted_error)

    async def reconcile_contacts(self) -> ReconciliationSummary:
        """One full pass; raises ReconciliationAbortedError when it cannot run or the node cannot be listed."""
        async with self._reconciliation_lock:
            if not self._worker_state.is_running:
                raise ReconciliationAbortedError("the relay mode is not running.")
            summary = await self._reconcile_contacts_once()
        if summary.removals_postponed:
            self._schedule_pass_in(self._timing.postponed_removal_retry_seconds)
        self._worker_state.runtime_status.node_contact_count = summary.node_contact_count
        self._worker_state.report_status_change()
        self._signals.sender_wakeup.set()
        self._log_summary(summary)
        return summary

    def _is_pass_due(self) -> bool:
        if self._signals.reconcile_requested.is_set():
            return True
        return self._next_pass_at is None or self._clock.monotonic() >= self._next_pass_at

    def _schedule_pass_in(self, delay_seconds: float) -> None:
        requested_pass_at = self._clock.monotonic() + delay_seconds
        if self._next_pass_at is None or requested_pass_at < self._next_pass_at:
            self._next_pass_at = requested_pass_at
            self._next_pass_moved.set()

    async def _reconcile_contacts_once(self) -> ReconciliationSummary:
        listed_connection_generation = self._worker_state.runtime_status.connection_generation
        try:
            contacts_on_node = await self._gateway.list_contacts()
        except NodeGatewayError as listing_error:
            raise ReconciliationAbortedError(
                f"the node's contacts could not be listed: {listing_error}"
            ) from listing_error
        database_contacts = await run_in_database_thread(list_contacts_for_node)
        own_public_key = await run_in_database_thread(read_configured_public_key)

        summary = ReconciliationSummary(node_contact_count=len(contacts_on_node))
        database_public_keys = {contact.public_key for contact in database_contacts}
        missing_contacts = [contact for contact in database_contacts if contact.public_key not in contacts_on_node]
        extra_public_keys = sorted(set(contacts_on_node) - database_public_keys - {own_public_key})

        contacts_refused_for_full_table = await self._add_missing_contacts(missing_contacts, summary)
        removals_done = await self._remove_extra_contacts(extra_public_keys, listed_connection_generation, summary)
        await self._retry_contacts_refused_for_full_table(
            contacts_refused_for_full_table,
            removals_done=removals_done,
            extra_contacts_remain=bool(extra_public_keys) and not removals_done,
            summary=summary,
        )
        await self._mark_contacts_found_on_node(database_contacts, contacts_on_node)
        return summary

    async def _add_missing_contacts(
        self, missing_contacts: list[ContactForNode], summary: ReconciliationSummary
    ) -> list[ContactForNode]:
        contacts_refused_for_full_table: list[ContactForNode] = []
        for contact in missing_contacts:
            add_error_code = await self._add_contact(contact)
            if add_error_code is None:
                await self._record_contact_on_node(contact)
                summary.added_count += 1
                summary.node_contact_count += 1
            elif add_error_code == ERR_CODE_TABLE_FULL:
                contacts_refused_for_full_table.append(contact)
            else:
                await self._record_add_failed(contact, describe_add_refusal(add_error_code), summary)
        return contacts_refused_for_full_table

    async def _add_contact(self, contact: ContactForNode) -> int | None:
        """None on success, otherwise the node's error code; a lost link aborts the pass."""
        try:
            await self._gateway.add_contact(build_node_contact_record(contact))
        except NodeRejectedCommandError as rejection:
            return rejection.error_code
        except NodeGatewayError as add_error:
            raise ReconciliationAbortedError(f"adding contact {contact.contact_id} failed: {add_error}") from add_error
        return None

    async def _remove_extra_contacts(
        self, extra_public_keys: list[str], listed_connection_generation: int, summary: ReconciliationSummary
    ) -> bool:
        if not extra_public_keys:
            return False
        async with self._worker_state.pause_sending("contacts are being removed from the node"):
            is_quiet = await self._wait_until_no_packet_awaits_acknowledgement()
            if not is_quiet:
                logger.info(
                    "%d contacts wait to be removed from the node: packets still await firmware ACKs.",
                    len(extra_public_keys),
                )
                summary.removals_postponed = True
                return False
            self._require_listed_node_still_running(listed_connection_generation)
            for public_key in extra_public_keys:
                try:
                    await self._gateway.remove_contact(public_key)
                except NodeGatewayError as removal_error:
                    raise ReconciliationAbortedError(f"removing a contact failed: {removal_error}") from removal_error
                summary.removed_count += 1
                summary.node_contact_count -= 1
        return True

    async def _wait_until_no_packet_awaits_acknowledgement(self) -> bool:
        """Sending is paused, so no packet can get a deadline later than the latest one recorded now."""
        wait_seconds = self._timing.minimum_removal_quiet_wait_seconds
        latest_deadline = await run_in_database_thread(read_latest_acknowledgement_deadline, self._clock.now())
        if latest_deadline is not None:
            seconds_until_latest_deadline = (latest_deadline - self._clock.now()).total_seconds()
            wait_seconds = max(wait_seconds, seconds_until_latest_deadline + self._timing.quiet_poll_seconds)
        return await wait_until_no_packet_awaits_acknowledgement(
            self._clock, self._timing, wait_seconds, self._worker_state
        )

    def _require_listed_node_still_running(self, listed_connection_generation: int) -> None:
        """The wait for ACKs can be long enough for the node to be reconnected, or replaced by another board."""
        is_same_connection = self._worker_state.runtime_status.connection_generation == listed_connection_generation
        if not (self._worker_state.is_running and is_same_connection):
            raise ReconciliationAbortedError(
                "the node was reconnected or the relay mode changed while the removals waited."
            )

    async def _retry_contacts_refused_for_full_table(
        self,
        contacts_refused_for_full_table: list[ContactForNode],
        *,
        removals_done: bool,
        extra_contacts_remain: bool,
        summary: ReconciliationSummary,
    ) -> None:
        for contact in contacts_refused_for_full_table:
            if extra_contacts_remain:
                # The pass that retries the postponed removals adds it afterwards.
                continue
            add_error_code = await self._add_contact(contact) if removals_done else ERR_CODE_TABLE_FULL
            if add_error_code is None:
                await self._record_contact_on_node(contact)
                summary.added_count += 1
                summary.node_contact_count += 1
            elif add_error_code == ERR_CODE_TABLE_FULL:
                await self._record_add_failed(contact, CONTACT_TABLE_FULL_ERROR, summary)
            else:
                await self._record_add_failed(contact, describe_add_refusal(add_error_code), summary)

    async def _mark_contacts_found_on_node(
        self, database_contacts: list[ContactForNode], contacts_on_node: dict[str, ListedNodeContact]
    ) -> None:
        now = self._clock.now()
        for contact in database_contacts:
            listed_contact = contacts_on_node.get(contact.public_key)
            if listed_contact is None:
                continue
            await run_in_database_thread(
                mark_contact_on_node,
                contact.contact_id,
                contact.public_key,
                node_name=listed_contact.name,
                node_out_path_length=listed_contact.out_path_length,
                now=now,
            )

    async def _record_contact_on_node(self, contact: ContactForNode) -> None:
        await run_in_database_thread(
            mark_contact_on_node,
            contact.contact_id,
            contact.public_key,
            node_name=contact.name,
            node_out_path_length=-1,
            now=self._clock.now(),
        )

    async def _record_add_failed(
        self, contact: ContactForNode, sync_error: str, summary: ReconciliationSummary
    ) -> None:
        await run_in_database_thread(
            mark_contact_add_failed, contact.contact_id, contact.public_key, sync_error, self._clock.now()
        )
        summary.failed_count += 1
        summary.failed_public_keys.append(contact.public_key)
        error_message = (
            f"Contact {contact.name or contact.public_key[:12]} could not be added to the node: {sync_error}"
        )
        logger.error(error_message)
        self._worker_state.record_error(error_message)

    def _log_summary(self, summary: ReconciliationSummary) -> None:
        if not summary.changed_anything:
            logger.debug("Contact reconciliation found nothing to change.")
            return
        logger.info(
            "Contact reconciliation: %d added, %d removed, %d failed%s; the node holds %d contacts.",
            summary.added_count,
            summary.removed_count,
            summary.failed_count,
            ", removals postponed" if summary.removals_postponed else "",
            summary.node_contact_count,
        )


def describe_add_refusal(error_code: int) -> str:
    return f"The node refused the contact with error {error_code}."
