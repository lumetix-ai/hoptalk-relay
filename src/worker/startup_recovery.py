"""What a restart settles before the node is opened: whatever the previous process left half done.

Commands still running were interrupted (a setup run falls back with them) and pending ones past
their expiry are expired; prepared packets become "outcome unknown" (their part is still in its
round and is sent again), packets awaiting a firmware ACK are dropped and so are pending route
decisions; pairing sessions past their end are ended, and one still ahead is resumed. The inbox
rows still "received" are processed afterwards by the inbound processor, in id order.
"""

import logging
from dataclasses import dataclass

from messaging.outbound_packets import OutboundPacketRecovery, recover_outbound_packets_at_startup
from node.node_commands import expire_pending_node_commands, interrupt_running_node_commands
from node.pairing_sessions import end_expired_pairing_sessions, get_active_pairing_session
from worker.clock import Clock
from worker.database_access import run_in_database_thread

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class StartupRecoverySummary:
    interrupted_command_count: int
    expired_command_count: int
    packet_recovery: OutboundPacketRecovery
    ended_pairing_session_count: int
    resumed_pairing_session_id: int | None


async def run_startup_recovery(clock: Clock) -> StartupRecoverySummary:
    now = clock.now()
    interrupted_commands = await run_in_database_thread(interrupt_running_node_commands, now)
    expired_commands = await run_in_database_thread(expire_pending_node_commands, now)
    packet_recovery = await run_in_database_thread(recover_outbound_packets_at_startup, now)
    ended_pairing_sessions = await run_in_database_thread(end_expired_pairing_sessions, now)
    resumed_pairing_session = await run_in_database_thread(get_active_pairing_session)

    summary = StartupRecoverySummary(
        interrupted_command_count=len(interrupted_commands),
        expired_command_count=len(expired_commands),
        packet_recovery=packet_recovery,
        ended_pairing_session_count=len(ended_pairing_sessions),
        resumed_pairing_session_id=resumed_pairing_session.pk if resumed_pairing_session is not None else None,
    )
    log_startup_recovery(summary)
    return summary


def log_startup_recovery(summary: StartupRecoverySummary) -> None:
    for interrupted_count, description in (
        (summary.interrupted_command_count, "node commands were interrupted by the restart"),
        (summary.expired_command_count, "pending node commands had expired"),
        (summary.packet_recovery.prepared_packets_now_unknown, "prepared packets have an unknown outcome"),
        (summary.packet_recovery.queued_packets_dropped, "packets awaiting a firmware ACK were dropped"),
        (summary.packet_recovery.route_resets_dropped, "pending route decisions were dropped"),
        (summary.ended_pairing_session_count, "pairing sessions had ended"),
    ):
        if interrupted_count:
            logger.info("Start-up recovery: %d %s.", interrupted_count, description)
    if summary.resumed_pairing_session_id is not None:
        logger.info("Start-up recovery: pairing session %d resumes.", summary.resumed_pairing_session_id)
