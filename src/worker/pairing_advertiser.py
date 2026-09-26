"""Pairing mode: adverts at an interval while the operator waits for a new user's node to be heard.

The node reports an advert of a node it does not store as NEW_CONTACT, after verifying its
signature; while a session is active every such advert, except the relay's own, is kept for the
panel. Adverts go out only in relay mode running, zero-hop unless the operator chose flood, each
after a check of the node's clock, which stamps them. A session ends at its end time; the
operator can stop it; leaving relay mode running stops it too. A shutdown leaves it active, and
the next start resumes it if its end time is still ahead.
"""

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from node.models import NodeCommand, PairingSession
from node.pairing_sessions import (
    end_expired_pairing_sessions,
    get_active_pairing_session,
    record_heard_advert,
    record_pairing_advert_sent,
    start_pairing_session,
    stop_pairing_session,
)
from worker.clock import Clock, convert_wall_time_to_deadline, wait_for_any_event_until
from worker.database_access import run_in_database_thread
from worker.node_clock import check_and_correct_node_clock
from worker.node_gateway import NodeGateway, NodeGatewayError
from worker.relay_modes import read_configured_public_key
from worker.worker_state import WorkerState
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)


class PairingRefusedError(Exception):
    pass


class PairingAdvertiser:
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
        # The session this process has advertised in relay mode running; leaving that mode stops it.
        self._advertised_session_id: int | None = None

    async def start_pairing(self, start_command: NodeCommand) -> int:
        """Create the session and send its first advert, so its countdown starts when the node advertises."""
        arguments = start_command.arguments
        active_session = await run_in_database_thread(get_active_pairing_session)
        if active_session is not None:
            raise PairingRefusedError("A pairing session is already active.")
        try:
            pairing_session = await run_in_database_thread(
                start_pairing_session,
                int(arguments["duration_seconds"]),
                int(arguments["advert_interval_seconds"]),
                bool(arguments.get("advert_flood", False)),
                start_command,
                self._clock.now(),
            )
        except ValueError as refusal:
            raise PairingRefusedError(str(refusal)) from refusal

        try:
            await self._send_advert(pairing_session)
        except NodeGatewayError:
            await run_in_database_thread(stop_pairing_session, pairing_session.pk, self._clock.now())
            raise
        self._advertised_session_id = pairing_session.pk
        self._signals.pairing_changed.set()
        logger.info(
            "Pairing started: an advert every %d s for %d s.",
            pairing_session.advert_interval_seconds,
            int(arguments["duration_seconds"]),
        )
        return pairing_session.pk

    async def run(self) -> None:
        while not self._signals.is_shutting_down:
            self._signals.pairing_changed.clear()
            next_look_at = await self.advertise_active_session()
            monotonic_deadline = (
                None if next_look_at is None else convert_wall_time_to_deadline(self._clock, next_look_at)
            )
            await wait_for_any_event_until(
                self._clock,
                [self._signals.pairing_changed, self._signals.shutdown_requested],
                monotonic_deadline,
            )

    async def advertise_active_session(self) -> datetime | None:
        """Send the advert that is due, end or stop the session; returns when to look again."""
        now = self._clock.now()
        for ended_session in await run_in_database_thread(end_expired_pairing_sessions, now):
            logger.info("Pairing session %d ended after %d adverts.", ended_session.pk, ended_session.adverts_sent)
        pairing_session = await run_in_database_thread(get_active_pairing_session)
        if pairing_session is None:
            self._advertised_session_id = None
            return None

        if not self._worker_state.is_running:
            await self._stop_session_after_leaving_running(pairing_session)
            return pairing_session.ends_at

        self._advertised_session_id = pairing_session.pk
        next_advert_at = calculate_next_advert_time(pairing_session)
        if next_advert_at is not None and next_advert_at > now:
            return min(next_advert_at, pairing_session.ends_at)
        try:
            advert_sent_at = await self._send_advert(pairing_session)
        except NodeGatewayError as advert_error:
            logger.warning("A pairing advert could not be sent: %s", advert_error)
            advert_sent_at = self._clock.now()
        advert_interval = timedelta(seconds=pairing_session.advert_interval_seconds)
        return min(advert_sent_at + advert_interval, pairing_session.ends_at)

    async def _stop_session_after_leaving_running(self, pairing_session: PairingSession) -> None:
        """A session this process never advertised is waiting for the node, as after a restart."""
        if self._advertised_session_id != pairing_session.pk:
            return
        await run_in_database_thread(stop_pairing_session, pairing_session.pk, self._clock.now())
        self._advertised_session_id = None
        logger.info("Pairing session %d stopped: the relay mode is no longer running.", pairing_session.pk)

    async def _send_advert(self, pairing_session: PairingSession) -> datetime:
        """Returns when the advert went out."""
        await check_and_correct_node_clock(self._gateway, self._clock, self._timing)
        await self._gateway.send_advert(flood=pairing_session.advert_flood)
        advert_sent_at = self._clock.now()
        await run_in_database_thread(record_pairing_advert_sent, pairing_session.pk, advert_sent_at)
        return advert_sent_at

    async def capture_heard_advert(self, contact_record: Mapping[str, Any]) -> None:
        pairing_session = await run_in_database_thread(get_active_pairing_session)
        if pairing_session is None:
            return
        public_key = str(contact_record.get("public_key", "")).lower()
        if not public_key or public_key == await run_in_database_thread(read_configured_public_key):
            return
        await run_in_database_thread(record_heard_advert, pairing_session.pk, contact_record, self._clock.now())
        logger.info(
            "Pairing heard %s (%s).", contact_record.get("adv_name") or "a node without a name", public_key[:12]
        )


def calculate_next_advert_time(pairing_session: PairingSession) -> datetime | None:
    if pairing_session.last_advert_at is None:
        return None
    return pairing_session.last_advert_at + timedelta(seconds=pairing_session.advert_interval_seconds)
