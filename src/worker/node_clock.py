"""Keeping the node's clock in step with the server's, which is its only time source.

A board without a real-time clock boots at a fixed date, and the node stamps its adverts and
contact changes with its own clock. The firmware refuses to move its clock backwards, so a node
behind the server is set forward, while one ahead is only reported: it would have to catch up
by itself.
"""

import logging
from dataclasses import dataclass

from worker.clock import Clock
from worker.node_gateway import NodeGateway
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class NodeClockCheck:
    # The node's clock minus the server's, as read before any correction.
    offset_seconds: int
    was_corrected: bool
    is_far_ahead: bool


async def check_and_correct_node_clock(gateway: NodeGateway, clock: Clock, timing: WorkerTiming) -> NodeClockCheck:
    node_time = await gateway.read_node_clock()
    server_time = int(clock.now().timestamp())
    offset_seconds = node_time - server_time

    if offset_seconds < -timing.node_clock_behind_tolerance_seconds:
        await gateway.set_node_clock(int(clock.now().timestamp()))
        logger.info("The node's clock was %d s behind the server's; it was set forward.", -offset_seconds)
        return NodeClockCheck(offset_seconds=offset_seconds, was_corrected=True, is_far_ahead=False)

    is_far_ahead = offset_seconds > timing.node_clock_ahead_warning_seconds
    if is_far_ahead:
        logger.warning(
            "The node's clock is %d s ahead of the server's; the node cannot be set back, so check the server's clock.",
            offset_seconds,
        )
    return NodeClockCheck(offset_seconds=offset_seconds, was_corrected=False, is_far_ahead=is_far_ahead)
