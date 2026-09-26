"""Firmware acknowledgements: pacing slots, deadlines, and the route resets they lead to.

A firmware ACK says only that the other node decrypted the packet, never that the app saw it,
so it frees a pacing slot and settles route decisions but never marks anything delivered.

- An ACK is matched through the database, which also finds packets whose deadline passed. One
  that matches nothing is kept for a minute: a fast zero-hop ACK can arrive before the MSG_SENT
  of its own packet has been recorded, and it is matched again once that happens.
- At a packet's deadline the engine decides whether its route needs a reset. That decision is
  settled right before the next packet to the contact, on the evidence the database holds by
  then; for a reply the tracker settles it at once, because the client is waiting for it.
- A reset reply is sent once more, as a new packet that floods, if it is still the newest reply
  for its request.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from messaging.models import OutboundPacket
from messaging.outbound_packets import (
    RecordedSendOutcome,
    record_acknowledgement_deadline_passed,
    record_node_acknowledgement,
)
from messaging.route_reset_evidence import record_route_reset_performed, settle_pending_route_resets
from worker.clock import Clock, convert_wall_time_to_deadline, wait_for_any_event_until
from worker.database_access import run_in_database_thread
from worker.node_event_subscriptions import NodeAcknowledgement
from worker.node_gateway import NodeGateway, NodeGatewayError
from worker.reply_queue import ReplyQueue
from worker.worker_queries import read_contact_public_key
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AwaitedPacket:
    packet_id: int
    contact_id: int | None
    purpose: OutboundPacket.Purpose
    expected_acknowledgement_code: str
    deadline: datetime


@dataclass(frozen=True, kw_only=True)
class UnmatchedAcknowledgement:
    round_trip_milliseconds: int | None
    # The worker clock's monotonic seconds.
    received_at: float


class AcknowledgementTracker:
    def __init__(
        self,
        *,
        gateway: NodeGateway,
        reply_queue: ReplyQueue,
        worker_state: WorkerState,
        clock: Clock,
        timing: WorkerTiming,
    ) -> None:
        self._gateway = gateway
        self._reply_queue = reply_queue
        self._worker_state = worker_state
        self._signals = worker_state.signals
        self._clock = clock
        self._timing = timing
        self._awaited_packets: dict[int, AwaitedPacket] = {}
        self._unmatched_acknowledgements: dict[str, UnmatchedAcknowledgement] = {}
        # A decision stays pending until its reset is recorded, so an overlapping settlement would
        # reset the route and resend the reply a second time.
        self._route_reset_settlement_lock = asyncio.Lock()

    # ----- pacing --------------------------------------------------------------------------

    def count_packets_awaiting_acknowledgement(self) -> int:
        return len(self._awaited_packets)

    @property
    def packets_awaiting_acknowledgement(self) -> tuple[AwaitedPacket, ...]:
        return tuple(self._awaited_packets.values())

    def count_unmatched_acknowledgements(self) -> int:
        return len(self._unmatched_acknowledgements)

    def forget_packets_awaiting_acknowledgement(self) -> None:
        """The link is gone: their ACKs may have been lost with it, and the database marks them dropped."""
        self._awaited_packets.clear()
        self._signals.acknowledgement_deadlines_changed.set()

    # ----- sends and acknowledgements ------------------------------------------------------

    async def register_sent_packet(self, recorded_outcome: RecordedSendOutcome) -> None:
        if not recorded_outcome.awaits_acknowledgement or recorded_outcome.acknowledgement_deadline_at is None:
            return
        code = recorded_outcome.expected_acknowledgement_code
        self._awaited_packets[recorded_outcome.packet_id] = AwaitedPacket(
            packet_id=recorded_outcome.packet_id,
            contact_id=recorded_outcome.contact_id,
            purpose=recorded_outcome.purpose,
            expected_acknowledgement_code=code,
            deadline=recorded_outcome.acknowledgement_deadline_at,
        )
        self._signals.acknowledgement_deadlines_changed.set()

        early_acknowledgement = self._take_unmatched_acknowledgement(code)
        if early_acknowledgement is not None:
            logger.debug("The ACK %s arrived before its MSG_SENT was recorded; matching it now.", code)
            await self.handle_node_acknowledgement(
                NodeAcknowledgement(code=code, round_trip_milliseconds=early_acknowledgement.round_trip_milliseconds)
            )

    async def handle_node_acknowledgement(self, acknowledgement: NodeAcknowledgement) -> None:
        acknowledged_packets = await run_in_database_thread(
            record_node_acknowledgement,
            acknowledgement.code,
            acknowledgement.round_trip_milliseconds,
            self._clock.now(),
        )
        if not acknowledged_packets:
            self._forget_old_unmatched_acknowledgements()
            self._unmatched_acknowledgements[acknowledgement.code] = UnmatchedAcknowledgement(
                round_trip_milliseconds=acknowledgement.round_trip_milliseconds, received_at=self._clock.monotonic()
            )
            return

        for acknowledged_packet in acknowledged_packets:
            self._awaited_packets.pop(acknowledged_packet.packet_id, None)
        self._signals.sender_wakeup.set()
        self._signals.acknowledgement_deadlines_changed.set()

    def _take_unmatched_acknowledgement(self, code: str) -> UnmatchedAcknowledgement | None:
        self._forget_old_unmatched_acknowledgements()
        return self._unmatched_acknowledgements.pop(code, None)

    def _forget_old_unmatched_acknowledgements(self) -> None:
        now = self._clock.monotonic()
        lifetime_seconds = self._timing.unmatched_acknowledgement_lifetime_seconds
        old_codes = [
            code
            for code, unmatched_acknowledgement in self._unmatched_acknowledgements.items()
            if now - unmatched_acknowledgement.received_at >= lifetime_seconds
        ]
        for code in old_codes:
            del self._unmatched_acknowledgements[code]

    # ----- deadlines -----------------------------------------------------------------------

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            self._signals.acknowledgement_deadlines_changed.clear()
            await self.handle_passed_deadlines()
            await wait_for_any_event_until(
                self._clock,
                [self._signals.acknowledgement_deadlines_changed, self._signals.shutdown_requested],
                self._find_next_deadline(),
            )

    def _find_next_deadline(self) -> float | None:
        if not self._awaited_packets:
            return None
        earliest_deadline = min(awaited_packet.deadline for awaited_packet in self._awaited_packets.values())
        return convert_wall_time_to_deadline(self._clock, earliest_deadline)

    async def handle_passed_deadlines(self) -> None:
        now = self._clock.now()
        passed_packets = [
            awaited_packet for awaited_packet in self._awaited_packets.values() if awaited_packet.deadline <= now
        ]
        for awaited_packet in sorted(passed_packets, key=lambda passed_packet: passed_packet.deadline):
            await self._handle_passed_deadline(awaited_packet)

    async def _handle_passed_deadline(self, awaited_packet: AwaitedPacket) -> None:
        acknowledgement_timeout = await run_in_database_thread(
            record_acknowledgement_deadline_passed, awaited_packet.packet_id, self._clock.now()
        )
        self._awaited_packets.pop(awaited_packet.packet_id, None)
        self._signals.sender_wakeup.set()
        if acknowledgement_timeout is None or acknowledgement_timeout.contact_id is None:
            return
        if not acknowledgement_timeout.route_reset_pending:
            return
        logger.info(
            "Packet %d to contact %d got no firmware ACK in time; its route is settled before the next packet.",
            acknowledgement_timeout.packet_id,
            acknowledgement_timeout.contact_id,
        )
        if acknowledgement_timeout.purpose == OutboundPacket.Purpose.REPLY and self._worker_state.is_running:
            await self.settle_route_resets(acknowledgement_timeout.contact_id)

    # ----- route resets --------------------------------------------------------------------

    async def settle_route_resets(self, contact_id: int, public_key: str | None = None) -> None:
        """Decide the contact's pending route resets; reset the route once if any needs it."""
        async with self._route_reset_settlement_lock:
            await self._settle_route_resets_under_lock(contact_id, public_key)

    async def _settle_route_resets_under_lock(self, contact_id: int, public_key: str | None) -> None:
        settlement = await run_in_database_thread(settle_pending_route_resets, contact_id, self._clock.now())
        if not settlement.needs_reset:
            return

        contact_public_key = public_key or await run_in_database_thread(read_contact_public_key, contact_id)
        if contact_public_key is None:
            return
        try:
            await self._gateway.reset_path(contact_public_key)
        except NodeGatewayError as reset_error:
            logger.warning("The route to contact %d could not be reset now: %s", contact_id, reset_error)
            return

        await run_in_database_thread(
            record_route_reset_performed, settlement.packet_ids_needing_reset, self._clock.now()
        )
        logger.info(
            "Reset the route to contact %d: packets %s got no firmware ACK and no sign that they arrived.",
            contact_id,
            ", ".join(str(packet_id) for packet_id in settlement.packet_ids_needing_reset),
        )
        for reply_to_resend in settlement.replies_to_resend:
            if self._reply_queue.has_reply_for(reply_to_resend.contact_id, reply_to_resend.reply_key):
                continue
            self._reply_queue.add_flood_resend(
                contact_id=reply_to_resend.contact_id,
                reply_key=reply_to_resend.reply_key,
                text=reply_to_resend.text,
                now=self._clock.monotonic(),
            )
            self._signals.sender_wakeup.set()
