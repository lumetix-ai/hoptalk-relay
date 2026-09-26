"""The one loop that sends every outbound direct message, paced to what the node can take.

A packet may start only when all of these hold: relay mode running with the node connected;
fewer packets await a firmware ACK than allowed, with the last place kept for replies; the
minimum gap since the previous send completed has passed; no back-off after a full packet pool
runs; and sending is not paused for a contact removal or a node restart.

Replies go first, then the due delivery or receipt the engine picks. Each packet is prepared
(its row and timestamp committed), sent, and its outcome recorded before the next one is
prepared: a round sends its next part only after the previous one left the round. Right before
a packet, the contact's pending route decisions are settled, so a stale route is reset before
it is used again.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass

from hoptalk_relay.relay_settings import PacingSettings
from messaging.outbound_packets import (
    NodeSendOutcome,
    PacketOutcomeUnknown,
    PacketRejectedByNode,
    RecordedSendOutcome,
    record_send_outcome,
)
from messaging.outbound_scheduling import (
    PacketDescriptor,
    calculate_next_eligible_time,
    prepare_next_packet,
    prepare_reply_packet,
)
from worker.acknowledgement_tracker import AcknowledgementTracker
from worker.clock import Clock, convert_wall_time_to_deadline, wait_for_any_event_until
from worker.database_access import run_in_database_thread
from worker.node_gateway import NodeGateway, NodeGatewayError
from worker.reply_queue import PendingReply, ReplyQueue
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SendAttempt:
    """What one pass of the loop did: sent a packet, or nothing and when to look again (a monotonic deadline)."""

    packet_was_sent: bool
    next_look_at: float | None = None


PACKET_SENT = SendAttempt(packet_was_sent=True)


class SenderLoop:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        reply_queue: ReplyQueue,
        acknowledgement_tracker: AcknowledgementTracker,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
        pacing: PacingSettings,
        request_reconciliation: Callable[[str], None],
    ) -> None:
        self._gateway = gateway
        self._reply_queue = reply_queue
        self._acknowledgement_tracker = acknowledgement_tracker
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self._pacing = pacing
        self._request_reconciliation = request_reconciliation
        self._last_send_completed_at: float | None = None
        self._packet_pool_back_off_until: float | None = None
        self._consecutive_full_packet_pools = 0

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            self._signals.sender_wakeup.clear()
            send_attempt = await self.send_next_packet()
            if send_attempt.packet_was_sent:
                continue
            await wait_for_any_event_until(
                self._clock,
                [self._signals.sender_wakeup, self._signals.shutdown_requested],
                send_attempt.next_look_at,
            )

    async def send_next_packet(self) -> SendAttempt:
        waiting_reason = self._find_pacing_wait()
        if waiting_reason is not None:
            return waiting_reason

        sending_gate = self._worker_state.sending_gate
        async with sending_gate.send_step_lock:
            if sending_gate.is_paused or not self._may_send_now():
                return self._wait_for_wakeup()
            return await self._prepare_and_send_next_packet()

    def _may_send_now(self) -> bool:
        return self._worker_state.is_running and self._worker_state.is_node_connected

    def _wait_for_wakeup(self) -> SendAttempt:
        """Nothing will change before an event wakes the loop; the longest sleep only guards against a lost one."""
        return SendAttempt(
            packet_was_sent=False,
            next_look_at=self._clock.monotonic() + self._timing.maximum_sender_sleep_seconds,
        )

    def _find_pacing_wait(self) -> SendAttempt | None:
        if not self._may_send_now() or self._worker_state.sending_gate.is_paused:
            return self._wait_for_wakeup()

        now = self._clock.monotonic()
        if self._packet_pool_back_off_until is not None and now < self._packet_pool_back_off_until:
            return SendAttempt(packet_was_sent=False, next_look_at=self._packet_pool_back_off_until)
        if self._last_send_completed_at is not None:
            gap_ends_at = self._last_send_completed_at + self._pacing.minimum_seconds_between_sends
            if gap_ends_at > now:
                return SendAttempt(packet_was_sent=False, next_look_at=gap_ends_at)
        if self._count_packets_awaiting_acknowledgement() >= self._pacing.maximum_packets_awaiting_node_acknowledgement:
            return self._wait_for_wakeup()
        return None

    def _count_packets_awaiting_acknowledgement(self) -> int:
        return self._acknowledgement_tracker.count_packets_awaiting_acknowledgement()

    def _may_send_other_than_replies(self) -> bool:
        """The last place for a packet awaiting an ACK is kept for replies, which clients wait for."""
        maximum_other_packets = self._pacing.maximum_packets_awaiting_node_acknowledgement - 1
        return self._count_packets_awaiting_acknowledgement() < maximum_other_packets

    async def _prepare_and_send_next_packet(self) -> SendAttempt:
        connection_generation = self._worker_state.runtime_status.connection_generation
        pending_reply = self._reply_queue.take_next_ready_reply(self._clock.monotonic())
        if pending_reply is not None:
            reply_packet = await run_in_database_thread(
                prepare_reply_packet,
                contact_id=pending_reply.contact_id,
                reply_key=pending_reply.reply_key,
                text=pending_reply.text,
                now=self._clock.now(),
                connection_generation=connection_generation,
            )
            if reply_packet is None:
                logger.info(
                    "A reply to contact %d was dropped: the contact is gone or not on the node.",
                    pending_reply.contact_id,
                )
                return SendAttempt(packet_was_sent=False, next_look_at=self._clock.monotonic())
            await self._send_packet(reply_packet, pending_reply)
            return PACKET_SENT

        if not self._may_send_other_than_replies():
            return self._calculate_idle_wait(may_send_other_than_replies=False)
        next_packet = await run_in_database_thread(prepare_next_packet, self._clock.now(), connection_generation)
        if next_packet is None:
            return await self._calculate_idle_wait_with_due_work()
        await self._send_packet(next_packet, None)
        return PACKET_SENT

    async def _send_packet(self, packet: PacketDescriptor, pending_reply: PendingReply | None) -> None:
        await self._acknowledgement_tracker.settle_route_resets(packet.contact_id, packet.contact_public_key)
        outcome = await self._send_to_node(packet)
        recorded_outcome = await run_in_database_thread(
            record_send_outcome, packet.packet_id, outcome, self._clock.now()
        )
        self._last_send_completed_at = self._clock.monotonic()
        if pending_reply is not None and not isinstance(outcome, PacketRejectedByNode):
            self._reply_queue.record_reply_sent(pending_reply, self._clock.monotonic())
        if recorded_outcome is None:
            return
        self._handle_recorded_outcome(recorded_outcome, pending_reply)
        await self._acknowledgement_tracker.register_sent_packet(recorded_outcome)

    async def _send_to_node(self, packet: PacketDescriptor) -> NodeSendOutcome:
        try:
            return await self._gateway.send_text_message(
                packet.contact_public_key, packet.text, packet.sender_timestamp
            )
        except NodeGatewayError as send_error:
            logger.warning("Packet %d may not have reached the node: %s", packet.packet_id, send_error)
            return PacketOutcomeUnknown()

    def _handle_recorded_outcome(
        self, recorded_outcome: RecordedSendOutcome, pending_reply: PendingReply | None
    ) -> None:
        if recorded_outcome.node_packet_pool_full:
            self._back_off_after_full_packet_pool()
            if pending_reply is not None and self._packet_pool_back_off_until is not None:
                self._reply_queue.put_back(pending_reply, ready_at=self._packet_pool_back_off_until)
        else:
            self._consecutive_full_packet_pools = 0
        if recorded_outcome.contact_needs_reconciliation:
            self._request_reconciliation(
                f"the node did not know contact {recorded_outcome.contact_id} when a packet was sent to it"
            )

    def _back_off_after_full_packet_pool(self) -> None:
        """The node's shared packet pool was exhausted: every send waits, longer while it stays full."""
        self._consecutive_full_packet_pools += 1
        back_off_seconds = min(
            self._timing.table_full_backoff_initial_seconds * 2 ** (self._consecutive_full_packet_pools - 1),
            self._timing.table_full_backoff_maximum_seconds,
        )
        self._packet_pool_back_off_until = self._clock.monotonic() + back_off_seconds
        if self._consecutive_full_packet_pools == self._timing.table_full_streak_before_error:
            error_message = (
                f"The node refused {self._consecutive_full_packet_pools} sends in a row because its packet pool "
                "was full; the mesh around it may be congested."
            )
            logger.error(error_message)
            self._worker_state.record_error(error_message)
        else:
            logger.info("The node's packet pool is full; sending waits %.1f s.", back_off_seconds)

    def _calculate_idle_wait(
        self, *, may_send_other_than_replies: bool, next_eligible_at: float | None = None
    ) -> SendAttempt:
        now = self._clock.monotonic()
        candidate_deadlines = [now + self._timing.maximum_sender_sleep_seconds]
        next_reply_ready_at = self._reply_queue.next_ready_time(now)
        if next_reply_ready_at is not None:
            candidate_deadlines.append(next_reply_ready_at)
        if may_send_other_than_replies and next_eligible_at is not None:
            candidate_deadlines.append(next_eligible_at)
        next_look_at = max(min(candidate_deadlines), now + self._timing.minimum_sender_sleep_seconds)
        return SendAttempt(packet_was_sent=False, next_look_at=next_look_at)

    async def _calculate_idle_wait_with_due_work(self) -> SendAttempt:
        """Sleep until the next reply or due round, but at least the minimum sleep.

        Work that is due but held back (a contact locked by a deletion, a round that just
        completed) would otherwise keep the loop preparing without end.
        """
        next_eligible_time = await run_in_database_thread(calculate_next_eligible_time, self._clock.now())
        next_eligible_at = (
            None if next_eligible_time is None else convert_wall_time_to_deadline(self._clock, next_eligible_time)
        )
        return self._calculate_idle_wait(may_send_other_than_replies=True, next_eligible_at=next_eligible_at)
