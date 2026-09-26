"""The simulated HopTalk client's side of its own MeshCore node: pacing, timestamps, firmware ACKs and routes.

Every direct message the client sends goes through `NodeLink`, which hands it to the device's node
only when the node can take it:

- at most `maximum_direct_messages_awaiting_acknowledgement` direct messages wait for a firmware ACK;
  one stops waiting when its ACK arrives or when its wait (clamp(1.2 x the node's suggested timeout,
  3 s, 60 s), scaled) has passed;
- at least `minimum_gap_between_direct_messages_seconds` between two direct messages;
- "ERR_CODE_TABLE_FULL" means the node's packet pool is full: the same text goes again after
  `table_full_wait_seconds`, with a new timestamp;
- every direct message, a resend after TABLE_FULL included, gets a new, strictly increasing
  MeshCore timestamp, max(now, previous + 1), and attempt 0: the client never uses the firmware's
  own repeats.

Acknowledgements (K, C) go before requests; within a priority, direct messages keep their order.
A queued direct message is dropped instead of sent when it is no longer needed (its request was
answered or given up, or its part was confirmed meanwhile).

Route hygiene: MeshCore never falls back to flooding by itself, so the client resets its node's
route to the server (a) before answering a server direct message that arrived by flood, unless
its node reported a new route to the server within `path_update_window_seconds`, and (b) when a
direct message its node sent over a stored route gets no firmware ACK in time, unless the server
has answered that direct message since. A reset while the app cannot reach its node is owed and
done as soon as it can.
"""

import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from protocol.constants import DIRECT_MESSAGE_MAXIMUM_BYTES, PROTOCOL_PREFIX
from protocol.text_validation import count_utf8_bytes
from tests.worker.fake_node.fake_companion_firmware import TextMessageQueued, TextMessageRejected
from tests.worker.fake_node.frames import FirmwareErrorCode
from tests.worker.fake_node.simulated_mesh import DeviceUnreachableError, SimulatedDevice
from tests.worker.simulated_hoptalk_client_records import ClientCounters, SimulatedClientStorage
from tests.worker.simulated_hoptalk_client_timing import ClientClock, ClientTiming

MESSAGE_TYPE_LETTER_POSITION = len(PROTOCOL_PREFIX)


class DirectMessagePriority(IntEnum):
    ACKNOWLEDGEMENT = 0
    REQUEST = 1


def never_answered(_moment: float) -> bool:
    return False


def always_needed() -> bool:
    return True


def do_nothing() -> None:
    return None


@dataclass(kw_only=True, eq=False)
class QueuedDirectMessage:
    text: str
    priority: DirectMessagePriority
    queued_at: float
    sequence_number: int
    is_still_needed: Callable[[], bool] = always_needed
    # Whether the server has answered this direct message since the given moment: the evidence that
    # spares a route reset when only the firmware ACK was lost.
    server_answered_since: Callable[[float], bool] = never_answered
    # Called once the node took it, or refused it for a reason other than a full packet pool.
    on_handed_to_node: Callable[[], None] = do_nothing
    on_dropped: Callable[[], None] = do_nothing


@dataclass(kw_only=True, eq=False)
class SentDirectMessage:
    text: str
    meshcore_timestamp: int
    handed_to_node_at: float
    sent_by_flood: bool
    expected_acknowledgement: bytes
    acknowledgement_deadline_at: float
    # How many direct messages were waiting for a firmware ACK, this one included, right after it was sent.
    awaiting_acknowledgement_after_hand_off: int
    server_answered_since: Callable[[float], bool] = field(repr=False)
    acknowledged_at: float | None = None
    acknowledgement_deadline_passed: bool = False

    @property
    def message_type_letter(self) -> str:
        return self.text[MESSAGE_TYPE_LETTER_POSITION : MESSAGE_TYPE_LETTER_POSITION + 1]

    @property
    def was_acknowledged_in_time(self) -> bool:
        return self.acknowledged_at is not None and self.acknowledged_at <= self.acknowledgement_deadline_at

    @property
    def stopped_waiting_at(self) -> float:
        if self.was_acknowledged_in_time and self.acknowledged_at is not None:
            return self.acknowledged_at
        return self.acknowledgement_deadline_at


@dataclass(frozen=True, kw_only=True)
class RefusedHandOff:
    text: str
    meshcore_timestamp: int
    attempted_at: float
    node_error_code: int


class RouteHygieneTrigger(StrEnum):
    FLOOD_ARRIVAL = "flood_arrival"
    MISSING_ACKNOWLEDGEMENT = "missing_acknowledgement"


class RouteHygieneOutcome(StrEnum):
    RESET = "reset"
    # The app could not reach its node: the reset is done as soon as it can.
    RESET_OWED = "reset_owed"
    SKIPPED_RECENT_PATH_UPDATE = "skipped_recent_path_update"
    SKIPPED_SERVER_ANSWERED = "skipped_server_answered"
    # The route was already reset after this direct message was sent (or after the last one sent).
    SKIPPED_ROUTE_ALREADY_RESET = "skipped_route_already_reset"


@dataclass(frozen=True, kw_only=True)
class RouteHygieneDecision:
    trigger: RouteHygieneTrigger
    outcome: RouteHygieneOutcome
    decided_at: float
    direct_message_text: str


class NodeLink:
    """Hands the client's direct messages to its node and watches the node's ACKs and route updates."""

    def __init__(
        self,
        device: SimulatedDevice,
        *,
        storage: SimulatedClientStorage,
        timing: ClientTiming,
        clock: ClientClock,
        counters: ClientCounters,
    ) -> None:
        self.device = device
        self.storage = storage
        self.timing = timing
        self.clock = clock
        self.counters = counters
        self.sent_direct_messages: list[SentDirectMessage] = []
        self.refused_hand_offs: list[RefusedHandOff] = []
        self.route_hygiene_decisions: list[RouteHygieneDecision] = []
        self.route_reset_times: list[float] = []
        self.last_path_update_seen_at: float | None = None
        self._queue: list[QueuedDirectMessage] = []
        self._sequence_numbers = itertools.count()
        self._awaiting_acknowledgement: list[SentDirectMessage] = []
        self._sent_by_expected_acknowledgement: dict[bytes, list[SentDirectMessage]] = {}
        self._firmware_acknowledgements_seen = len(device.firmware_acknowledgements)
        self._path_updates_seen = len(device.path_update_times)
        self._last_hand_off_at: float | None = None
        self._last_route_reset_at: float | None = None
        self._route_reset_is_owed = False
        self._table_full_wait_ends_at: float | None = None

    # ----- queueing ----------------------------------------------------------------------------

    def queue_direct_message(
        self,
        text: str,
        *,
        priority: DirectMessagePriority,
        is_still_needed: Callable[[], bool] = always_needed,
        server_answered_since: Callable[[float], bool] = never_answered,
        on_handed_to_node: Callable[[], None] = do_nothing,
        on_dropped: Callable[[], None] = do_nothing,
    ) -> QueuedDirectMessage:
        queued_direct_message = QueuedDirectMessage(
            text=text,
            priority=priority,
            queued_at=self.clock.monotonic_seconds(),
            sequence_number=next(self._sequence_numbers),
            is_still_needed=is_still_needed,
            server_answered_since=server_answered_since,
            on_handed_to_node=on_handed_to_node,
            on_dropped=on_dropped,
        )
        self._queue.append(queued_direct_message)
        return queued_direct_message

    @property
    def queued_direct_messages(self) -> list[QueuedDirectMessage]:
        """The direct messages still to be sent, in sending order; those no longer needed are left out."""
        return [queued for queued in sorted(self._queue, key=queue_order) if queued.is_still_needed()]

    # ----- the node's pushes -------------------------------------------------------------------

    def observe_node_pushes(self, now: float) -> None:
        self._record_new_firmware_acknowledgements(now)
        self._record_new_path_updates(now)
        self._perform_owed_route_reset(now)

    def _record_new_firmware_acknowledgements(self, now: float) -> None:
        firmware_acknowledgements = self.device.firmware_acknowledgements
        for firmware_acknowledgement in firmware_acknowledgements[self._firmware_acknowledgements_seen :]:
            for sent_direct_message in self._sent_by_expected_acknowledgement.get(firmware_acknowledgement.code, []):
                if sent_direct_message.acknowledged_at is None:
                    sent_direct_message.acknowledged_at = now
        self._firmware_acknowledgements_seen = len(firmware_acknowledgements)
        self._awaiting_acknowledgement = [
            sent_direct_message
            for sent_direct_message in self._awaiting_acknowledgement
            if sent_direct_message.acknowledged_at is None
        ]

    def _record_new_path_updates(self, now: float) -> None:
        path_update_count = len(self.device.path_update_times)
        if path_update_count > self._path_updates_seen:
            self.last_path_update_seen_at = now
        self._path_updates_seen = path_update_count

    # ----- firmware ACK deadlines --------------------------------------------------------------

    def count_direct_messages_awaiting_acknowledgement(self) -> int:
        return len(self._awaiting_acknowledgement)

    def handle_passed_acknowledgement_deadlines(self, now: float) -> None:
        still_awaiting: list[SentDirectMessage] = []
        for sent_direct_message in self._awaiting_acknowledgement:
            if now < sent_direct_message.acknowledgement_deadline_at:
                still_awaiting.append(sent_direct_message)
                continue
            sent_direct_message.acknowledgement_deadline_passed = True
            if not sent_direct_message.sent_by_flood:
                self._decide_route_reset_after_missing_acknowledgement(sent_direct_message, now)
        self._awaiting_acknowledgement = still_awaiting

    def _decide_route_reset_after_missing_acknowledgement(
        self, sent_direct_message: SentDirectMessage, now: float
    ) -> None:
        if sent_direct_message.server_answered_since(sent_direct_message.handed_to_node_at):
            outcome = RouteHygieneOutcome.SKIPPED_SERVER_ANSWERED
        elif self._route_was_reset_since(sent_direct_message.handed_to_node_at):
            outcome = RouteHygieneOutcome.SKIPPED_ROUTE_ALREADY_RESET
        else:
            outcome = self._reset_route_to_server(now)
        self._record_route_hygiene_decision(
            RouteHygieneTrigger.MISSING_ACKNOWLEDGEMENT, outcome, sent_direct_message.text, now
        )

    # ----- route hygiene before answering a flood ----------------------------------------------

    def decide_route_reset_before_answering_flood(self, received_text: str, now: float) -> RouteHygieneOutcome:
        if self._path_update_is_recent(now):
            outcome = RouteHygieneOutcome.SKIPPED_RECENT_PATH_UPDATE
        elif self._route_was_reset_since(self._last_hand_off_at):
            outcome = RouteHygieneOutcome.SKIPPED_ROUTE_ALREADY_RESET
        else:
            outcome = self._reset_route_to_server(now)
        self._record_route_hygiene_decision(RouteHygieneTrigger.FLOOD_ARRIVAL, outcome, received_text, now)
        return outcome

    def _path_update_is_recent(self, now: float) -> bool:
        if self.last_path_update_seen_at is None:
            return False
        return now - self.last_path_update_seen_at <= self.timing.path_update_window_seconds

    def _route_was_reset_since(self, moment: float | None) -> bool:
        if self._route_reset_is_owed:
            return True
        if self._last_route_reset_at is None:
            return False
        return moment is None or self._last_route_reset_at >= moment

    def _reset_route_to_server(self, now: float) -> RouteHygieneOutcome:
        if not self.device.app_can_reach_node:
            self._route_reset_is_owed = True
            return RouteHygieneOutcome.RESET_OWED
        self._perform_route_reset(now)
        return RouteHygieneOutcome.RESET

    def _perform_owed_route_reset(self, now: float) -> None:
        if self._route_reset_is_owed and self.device.app_can_reach_node:
            self._perform_route_reset(now)

    def _perform_route_reset(self, now: float) -> None:
        try:
            self.device.reset_route_to_relay()
        except DeviceUnreachableError:
            self._route_reset_is_owed = True
            return
        self._route_reset_is_owed = False
        self._last_route_reset_at = now
        self.route_reset_times.append(now)
        self.counters.route_resets += 1

    def _record_route_hygiene_decision(
        self, trigger: RouteHygieneTrigger, outcome: RouteHygieneOutcome, direct_message_text: str, now: float
    ) -> None:
        if outcome not in (RouteHygieneOutcome.RESET, RouteHygieneOutcome.RESET_OWED):
            self.counters.route_resets_skipped += 1
        self.route_hygiene_decisions.append(
            RouteHygieneDecision(
                trigger=trigger, outcome=outcome, decided_at=now, direct_message_text=direct_message_text
            )
        )

    # ----- handing direct messages to the node -------------------------------------------------

    def hand_direct_messages_to_node(self, now: float) -> None:
        while self._node_may_take_a_direct_message(now):
            queued_direct_message = self._take_next_needed_direct_message()
            if queued_direct_message is None:
                return
            if not self._hand_off(queued_direct_message, now):
                return

    def _node_may_take_a_direct_message(self, now: float) -> bool:
        if not self._queue or not self.device.app_can_reach_node:
            return False
        if self._table_full_wait_ends_at is not None and now < self._table_full_wait_ends_at:
            return False
        gap_seconds = self.timing.minimum_gap_between_direct_messages_seconds
        if self._last_hand_off_at is not None and now - self._last_hand_off_at < gap_seconds:
            return False
        awaiting_limit = self.timing.maximum_direct_messages_awaiting_acknowledgement
        return self.count_direct_messages_awaiting_acknowledgement() < awaiting_limit

    def _take_next_needed_direct_message(self) -> QueuedDirectMessage | None:
        while self._queue:
            queued_direct_message = min(self._queue, key=queue_order)
            self._queue.remove(queued_direct_message)
            if queued_direct_message.is_still_needed():
                return queued_direct_message
            queued_direct_message.on_dropped()
        return None

    def _hand_off(self, queued_direct_message: QueuedDirectMessage, now: float) -> bool:
        """Give one direct message to the node; False when the node could not take it now."""
        self._perform_owed_route_reset(now)
        meshcore_timestamp = self.allocate_meshcore_timestamp()
        try:
            send_result = self.device.send_direct_message(
                queued_direct_message.text, sender_timestamp=meshcore_timestamp
            )
        except DeviceUnreachableError:
            self._queue.append(queued_direct_message)
            return False
        if isinstance(send_result, TextMessageRejected):
            return self._handle_refused_hand_off(queued_direct_message, send_result, meshcore_timestamp, now)
        self._record_sent_direct_message(queued_direct_message, send_result, meshcore_timestamp, now)
        queued_direct_message.on_handed_to_node()
        return True

    def _handle_refused_hand_off(
        self,
        queued_direct_message: QueuedDirectMessage,
        send_result: TextMessageRejected,
        meshcore_timestamp: int,
        now: float,
    ) -> bool:
        self.refused_hand_offs.append(
            RefusedHandOff(
                text=queued_direct_message.text,
                meshcore_timestamp=meshcore_timestamp,
                attempted_at=now,
                node_error_code=send_result.error_code,
            )
        )
        if is_packet_pool_full(send_result, queued_direct_message.text):
            self.counters.table_full_rejections += 1
            self._table_full_wait_ends_at = now + self.timing.table_full_wait_seconds
            self._queue.append(queued_direct_message)
            return False
        self.counters.direct_messages_refused_by_node += 1
        queued_direct_message.on_handed_to_node()
        return True

    def _record_sent_direct_message(
        self,
        queued_direct_message: QueuedDirectMessage,
        send_result: TextMessageQueued,
        meshcore_timestamp: int,
        now: float,
    ) -> None:
        sent_direct_message = SentDirectMessage(
            text=queued_direct_message.text,
            meshcore_timestamp=meshcore_timestamp,
            handed_to_node_at=now,
            sent_by_flood=send_result.sent_by_flood,
            expected_acknowledgement=send_result.expected_acknowledgement,
            acknowledgement_deadline_at=now
            + self.timing.acknowledgement_wait_seconds(send_result.suggested_timeout_milliseconds),
            awaiting_acknowledgement_after_hand_off=self.count_direct_messages_awaiting_acknowledgement() + 1,
            server_answered_since=queued_direct_message.server_answered_since,
        )
        self.sent_direct_messages.append(sent_direct_message)
        self._awaiting_acknowledgement.append(sent_direct_message)
        self._sent_by_expected_acknowledgement.setdefault(send_result.expected_acknowledgement, []).append(
            sent_direct_message
        )
        self._last_hand_off_at = now
        self._table_full_wait_ends_at = None

    def allocate_meshcore_timestamp(self) -> int:
        """max(now in seconds, previous + 1): strictly increasing, also within one second or after a clock step back."""
        meshcore_timestamp = max(int(self.clock.wall_clock_seconds()), self.storage.last_meshcore_timestamp + 1)
        self.storage.last_meshcore_timestamp = meshcore_timestamp
        return meshcore_timestamp


def queue_order(queued_direct_message: QueuedDirectMessage) -> tuple[int, int]:
    return (queued_direct_message.priority, queued_direct_message.sequence_number)


def is_packet_pool_full(send_result: TextMessageRejected, text: str) -> bool:
    """ERR_CODE_TABLE_FULL means a full pool only for a text that fits; for a longer one it means "too long"."""
    fits_one_direct_message = count_utf8_bytes(text) <= DIRECT_MESSAGE_MAXIMUM_BYTES
    return send_result.error_code == FirmwareErrorCode.TABLE_FULL and fits_one_direct_message
