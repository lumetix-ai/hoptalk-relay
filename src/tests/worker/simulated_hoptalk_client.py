"""A reference HopTalk client (docs/protocol.md) that runs as the app of one simulated device.

It follows every client rule of the protocol, so end-to-end tests can put it in front of the relay
and the iOS app can copy its behaviour:

- Sign-in: `sign_in` sends "A" and retries it until "a" or an error. "e RATE_LIMITED" keeps the
  request pending and sends it again once the rate-limit wait has passed; a new sign-in waits
  `wrong_password_wait_seconds` after "e WRONG_PASSWORD" for the same username. Signing in as
  another account sets the current account's pending requests aside first: they are sent again
  if the sign-in fails, and discarded, together with the local conversations, once the new "a"
  arrives. An "a" nobody asked for means a delayed sign-in moved the device: the client signs
  out and, with the remembered password, signs in again. Every successful sign-in sends "F *",
  retried at most three times.
- While no account is signed in, or while a switch to another account is under way, "m" and "s"
  are discarded unanswered: the account they belong to is unknown, and the server sends them
  again (after the sign-in, "F *" brings whatever is still missing).
- Sending: `send_message` splits the text (grapheme clusters kept whole when that costs no extra
  part), stores the exact parts and the new id before the first part leaves, and sends rounds of
  every part the server has not confirmed. Each "k" replaces the confirmed set; one with zeros that
  arrives after a whole round makes the client send the missing parts at once, and only an
  all-ones "k" or any "s" completes the message. The retry timer starts when the last part of a
  round has been handed to the node. "e ID_CONFLICT" sends the message again under a new id.
- Receiving: parts are reassembled by (peer, id), the first copy of each part kept; every part is
  answered with "K" carrying the full set, an incomplete one coalesced until the parts pause, a
  complete one at once. A message is displayed once, when it is complete.
- Reads and receipts: `mark_read` sends "R" once and retries it until "r". Every "s" raises the
  message's status (never lowers it) and is answered with "C", also for an unknown id.
- Refresh: `open_conversation` sends "F <peer>", retried only while the conversation stays open,
  with at most one outstanding per peer.
- The node: pacing, strictly increasing MeshCore timestamps and route resets are `NodeLink`'s job
  (simulated_hoptalk_client_node_link.py).

Only direct messages from the pinned server contact with text type 0 are protocol traffic; a
malformed server message is dropped silently and a lower-case type this client does not know is
ignored.

The client runs as one asyncio task that looks at its node every `poll_interval_seconds`
(`start()`/`stop()`, or `async with`). Durations come from `ClientTiming`, which tests scale down
with `ClientTiming().scaled_by(0.01)`, together with one `ScaledClock(0.01)` shared by every client
of the test. A restarted app is a new client with the same `SimulatedClientStorage`; a reinstalled
app is a new client with a new storage.
"""

import asyncio
import contextlib
import functools
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from protocol.constants import (
    DIRECT_MESSAGE_MAXIMUM_BYTES,
    RECEIVED_SET_MISSING,
    REFRESH_ALL_PEERS_TARGET,
    ClientMessageType,
    ErrorCode,
    ReceiptLevel,
)
from protocol.field_grammar import FIELD_SEPARATOR, NUL_CHARACTER, is_message_id_field
from protocol.formatting import (
    format_account_request,
    format_delivery_acknowledgement,
    format_message_part_request,
    format_query_request,
    format_read_request,
    format_receipt_acknowledgement,
    format_refresh_request,
)
from protocol.message_splitting import split_message_text
from protocol.message_types import (
    AccountReply,
    DeliveryPart,
    ErrorReply,
    OtherText,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    QueryReply,
    ReadReply,
    ReceiptPush,
    RefreshReply,
    SendStatusReply,
)
from protocol.parsing import parse_direct_message_text
from protocol.passwords import is_valid_password, normalize_password
from protocol.received_sets import FIRST_PART_NUMBER, is_valid_part_numbering, received_set_matches_part_count
from protocol.text_validation import count_utf8_bytes, is_valid_part_text
from protocol.usernames import is_valid_username, normalize_username_for_lookup
from tests.protocol.grapheme_clusters import split_into_grapheme_clusters
from tests.worker.fake_node.frames import PUBLIC_KEY_PREFIX_BYTES, TextType
from tests.worker.fake_node.simulated_mesh import ReceivedDirectMessage, SimulatedDevice
from tests.worker.fake_node.waiting import wait_until
from tests.worker.simulated_hoptalk_client_node_link import (
    DirectMessagePriority,
    NodeLink,
    RefusedHandOff,
    RouteHygieneDecision,
    SentDirectMessage,
)
from tests.worker.simulated_hoptalk_client_records import (
    FAILURE_REASON_ACCOUNT_SWITCHED,
    RECEIPT_LEVEL_STATUSES,
    ClientCounters,
    ClientEvent,
    ClientEventKind,
    ClientRequest,
    ConversationRefresh,
    IncomingMessage,
    MessageKey,
    OutgoingMessage,
    OutgoingMessageStatus,
    ReadConfirmationRequest,
    ReceivedDirectMessageHandling,
    ReceivedServerDirectMessage,
    RequestState,
    SignInRequest,
    SimulatedClientStorage,
    UserQuery,
    build_message_key,
    usernames_match,
)
from tests.worker.simulated_hoptalk_client_timing import (
    MICROSECONDS_PER_SECOND,
    ClientClock,
    ClientTiming,
    SystemClock,
)

# Error codes that only a client bug (or a server newer than the client) explains: the client
# validates usernames, passwords and parts before it sends anything.
CLIENT_BUG_ERROR_CODES = frozenset(
    {
        ErrorCode.SYNTAX,
        ErrorCode.VERSION,
        ErrorCode.UNSUPPORTED,
        ErrorCode.PART_INVALID,
        ErrorCode.PASSWORD_INVALID,
    }
)
KNOWN_ERROR_CODES = frozenset(str(error_code) for error_code in ErrorCode)
MESSAGE_REFERENCE_FIELD_COUNT = 2


class ClientNotSignedInError(RuntimeError):
    """The app would show its sign-in screen: no account is signed in, or a switch to another one is under way."""


class MessageDirection(StrEnum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


@dataclass(frozen=True, kw_only=True)
class ConversationEntry:
    direction: MessageDirection
    peer_username: str
    message_id: int
    text: str


def normalize_line_endings(text: str) -> str:
    """CR LF and a lone CR become LF before splitting, as a part text allows no CR."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def require_valid_username(username: str) -> None:
    if not is_valid_username(username):
        raise ValueError(f"A username is 3 to 16 of A-Z, a-z and 0-9, not {username!r}.")


def parse_message_reference(error_reference: str) -> MessageKey | None:
    """The "<peer> <id>" reference of an error for "M" or "R"."""
    reference_fields = error_reference.split(FIELD_SEPARATOR)
    if len(reference_fields) != MESSAGE_REFERENCE_FIELD_COUNT:
        return None
    peer_username, message_id_field = reference_fields
    if not is_valid_username(peer_username) or not is_message_id_field(message_id_field):
        return None
    return build_message_key(peer_username, int(message_id_field))


class SimulatedHopTalkClient:
    """The HopTalk app of one `SimulatedDevice`; see the module docstring for what it does."""

    def __init__(
        self,
        device: SimulatedDevice,
        *,
        storage: SimulatedClientStorage | None = None,
        timing: ClientTiming | None = None,
        clock: ClientClock | None = None,
        grapheme_cluster_segmenter: Callable[[str], list[str]] = split_into_grapheme_clusters,
        signs_in_again_after_unexpected_account_reply: bool = True,
        reads_messages_in_open_conversations: bool = False,
    ) -> None:
        self.device = device
        self.storage = storage if storage is not None else SimulatedClientStorage()
        if self.storage.pinned_server_public_key is None:
            self.storage.pinned_server_public_key = device.relay_public_key
        self.timing = timing or ClientTiming()
        self.clock = clock or SystemClock()
        self.counters = ClientCounters()
        self.node_link = NodeLink(
            device, storage=self.storage, timing=self.timing, clock=self.clock, counters=self.counters
        )
        self.events: list[ClientEvent] = []
        self.received_direct_messages: list[ReceivedServerDirectMessage] = []
        self.displayed_messages: list[IncomingMessage] = []
        self.receipts_for_unknown_messages: list[ReceiptPush] = []
        self.internal_errors: list[Exception] = []
        # Lower-case username of each open conversation, mapped to the spelling it was opened with.
        self.open_conversations: dict[str, str] = {}
        self._grapheme_cluster_segmenter = grapheme_cluster_segmenter
        self._signs_in_again_after_unexpected_account_reply = signs_in_again_after_unexpected_account_reply
        self._reads_messages_in_open_conversations = reads_messages_in_open_conversations
        # Changes whenever the account the acknowledgements belong to may change: "K" and "C" still
        # queued from before are dropped instead of sent.
        self._acknowledgement_generation = 0
        self._node_was_reachable: bool | None = None
        self._run_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return f"SimulatedHopTalkClient(device={self.device.name!r}, signed_in_username={self.signed_in_username!r})"

    # ----- running -----------------------------------------------------------------------------

    def start(self) -> None:
        """Start looking at the node; pending requests of a restored storage are resumed."""
        if self._run_task is not None:
            raise RuntimeError("The client is already running.")
        now = self._now()
        self._resume_requests_after_app_start(now)
        self._node_was_reachable = self.device.app_can_reach_node
        if self._node_was_reachable:
            self._refresh_everything_after_long_server_silence(now)
        self._run_task = asyncio.get_running_loop().create_task(
            self._run_until_stopped(), name=f"simulated HopTalk client of {self.device.name}"
        )

    async def stop(self) -> None:
        """Stop the app; the storage keeps what a restarted client resumes."""
        if self._run_task is None:
            return
        self._run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._run_task
        self._run_task = None

    @property
    def is_running(self) -> bool:
        return self._run_task is not None

    async def __aenter__(self) -> SimulatedHopTalkClient:
        self.start()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()

    async def _run_until_stopped(self) -> None:
        while True:
            self.run_one_pass()
            await asyncio.sleep(self.timing.poll_interval_seconds)

    def run_one_pass(self) -> None:
        """Everything the app does between two looks at its node; the running client calls it every poll interval.

        An exception is recorded in `internal_errors` instead of stopping the app, so a test can
        fail on it with the whole story in hand.
        """
        now = self._now()
        try:
            self._observe_node_connection(now)
            self.node_link.observe_node_pushes(now)
            self._receive_server_direct_messages(now)
            self.node_link.handle_passed_acknowledgement_deadlines(now)
            self._run_due_timers(now)
            self.node_link.hand_direct_messages_to_node(now)
        except Exception as internal_error:
            self.internal_errors.append(internal_error)

    def _now(self) -> float:
        return self.clock.monotonic_seconds()

    # ----- what the user does ------------------------------------------------------------------

    def sign_in(self, username: str, password: str) -> SignInRequest:
        """Send "A"; the password is sent in NFC. Raises ValueError for a username or password the rules forbid."""
        require_valid_username(username)
        normalized_password = normalize_password(password)
        if not is_valid_password(normalized_password):
            raise ValueError("The password breaks the password rules of protocol section 5.2.")
        return self._start_sign_in(username, normalized_password, self._now())

    def check_user(self, username: str) -> UserQuery:
        """Send "Q"; a query for the same username that is still pending is returned instead of a second one."""
        require_valid_username(username)
        account_key = self._require_signed_in_account_key()
        pending_query = self._find_active_request(UserQuery, lambda query: usernames_match(query.username, username))
        if pending_query is not None:
            return pending_query
        now = self._now()
        query = UserQuery(account_key=account_key, created_at=now, username=username)
        self._register_request(query)
        self._start_round(query, now)
        return query

    def send_message(self, recipient_username: str, text: str) -> OutgoingMessage:
        """Split, store and send a message; the returned message is the handle whose `status` tests follow.

        Raises ValueError for an empty text or a forbidden character, and MessageTooLongError for
        a text that needs more than 10 parts.
        """
        require_valid_username(recipient_username)
        account_key = self._require_signed_in_account_key()
        message_text = normalize_line_endings(text)
        parts = split_message_text(message_text, self._grapheme_cluster_segmenter)
        now = self._now()
        message = OutgoingMessage(
            account_key=account_key,
            created_at=now,
            recipient_username=recipient_username,
            text=message_text,
            message_id=self._allocate_message_id(),
            parts=tuple(parts),
            confirmed_set=RECEIVED_SET_MISSING * len(parts),
        )
        self.storage.outgoing_messages[message.key] = message
        self._register_request(message)
        self._start_round(message, now)
        return message

    def open_conversation(self, peer_username: str) -> ConversationRefresh:
        """Open a conversation and send "F <peer>", unless one for that peer is still outstanding."""
        require_valid_username(peer_username)
        account_key = self._require_signed_in_account_key()
        self.open_conversations[normalize_username_for_lookup(peer_username)] = peer_username
        if self._reads_messages_in_open_conversations:
            self.mark_conversation_read(peer_username)
        outstanding_refresh = self._find_outstanding_conversation_refresh(peer_username)
        if outstanding_refresh is not None:
            return outstanding_refresh
        now = self._now()
        refresh = ConversationRefresh(
            account_key=account_key, created_at=now, refresh_target=peer_username, maximum_retries=None
        )
        self._register_request(refresh)
        self._start_round(refresh, now)
        return refresh

    def close_conversation(self, peer_username: str) -> None:
        """Close a conversation: its "F <peer>" is not retried any more."""
        self.open_conversations.pop(normalize_username_for_lookup(peer_username), None)
        now = self._now()
        for refresh in self._unfinished_requests_of_type(ConversationRefresh):
            if not refresh.is_for_all_peers and usernames_match(refresh.refresh_target, peer_username):
                self._finish_request(refresh, RequestState.ABANDONED, now)

    def is_conversation_open(self, peer_username: str) -> bool:
        return normalize_username_for_lookup(peer_username) in self.open_conversations

    def refresh_every_conversation(self) -> ConversationRefresh:
        """Send "F *" (at most three retries), unless one is still outstanding."""
        self._require_signed_in_account_key()
        outstanding_refresh = self._find_active_request(ConversationRefresh, lambda refresh: refresh.is_for_all_peers)
        if outstanding_refresh is not None:
            return outstanding_refresh
        return self._start_refresh_of_every_conversation(self._now())

    def mark_read(self, peer_username: str, message_id: int) -> ReadConfirmationRequest:
        """The user saw a complete incoming message: send "R" once and retry it until "r"."""
        account_key = self._require_signed_in_account_key()
        incoming_message = self.storage.incoming_messages.get(build_message_key(peer_username, message_id))
        if incoming_message is None or not incoming_message.is_complete:
            raise ValueError(f"No complete message {message_id} from {peer_username} to mark read.")
        return self._request_read_confirmation(incoming_message, account_key, self._now())

    def mark_conversation_read(self, peer_username: str) -> list[ReadConfirmationRequest]:
        """Mark every displayed message from the peer read that is not marked read yet."""
        account_key = self._require_signed_in_account_key()
        now = self._now()
        return [
            self._request_read_confirmation(incoming_message, account_key, now)
            for incoming_message in self.received_messages(peer_username)
            if incoming_message.read_request is None
        ]

    def pin_server(self, server_public_key: bytes) -> None:
        """Take a new server contact card (a reconfigured server): only its direct messages count from now on."""
        self.device.trust_relay(server_public_key)
        self.storage.pinned_server_public_key = server_public_key

    def send_raw_direct_message(self, text: str) -> None:
        """Hand any text to the node as the app would a request, paced but never retried or checked.

        For scenarios that need what a correct client never sends, such as an invalid password.
        """
        if NUL_CHARACTER in text or count_utf8_bytes(text) > DIRECT_MESSAGE_MAXIMUM_BYTES:
            raise ValueError(f"A direct message holds no U+0000 and at most {DIRECT_MESSAGE_MAXIMUM_BYTES} bytes.")
        self.node_link.queue_direct_message(text, priority=DirectMessagePriority.REQUEST)

    # ----- what tests look at ------------------------------------------------------------------

    @property
    def signed_in_username(self) -> str | None:
        return self.storage.signed_in_username

    @property
    def is_signed_in(self) -> bool:
        return self.storage.signed_in_username is not None

    @property
    def is_switching_account(self) -> bool:
        latest_sign_in = self.storage.latest_sign_in
        return latest_sign_in is not None and latest_sign_in.is_active and latest_sign_in.is_account_switch

    @property
    def is_signing_in(self) -> bool:
        latest_sign_in = self.storage.latest_sign_in
        return latest_sign_in is not None and latest_sign_in.is_active

    @property
    def sent_direct_messages(self) -> list[SentDirectMessage]:
        return self.node_link.sent_direct_messages

    @property
    def refused_hand_offs(self) -> list[RefusedHandOff]:
        return self.node_link.refused_hand_offs

    @property
    def route_hygiene_decisions(self) -> list[RouteHygieneDecision]:
        return self.node_link.route_hygiene_decisions

    @property
    def unfinished_requests(self) -> list[ClientRequest]:
        return list(self.storage.unfinished_requests)

    @property
    def is_idle(self) -> bool:
        """Nothing left to send: no active request, nothing queued for the node, no "K" waiting to go out."""
        if any(request.is_active for request in self.storage.unfinished_requests):
            return False
        if self.node_link.queued_direct_messages:
            return False
        return all(
            incoming_message.acknowledgement_due_at is None
            for incoming_message in self.storage.incoming_messages.values()
        )

    def sent_texts(self, message_type_letter: str | None = None) -> list[str]:
        """Texts of the direct messages the node took, in order; optionally only one type ("M", "K", ...)."""
        return [
            sent_direct_message.text
            for sent_direct_message in self.node_link.sent_direct_messages
            if message_type_letter is None or sent_direct_message.message_type_letter == message_type_letter
        ]

    def received_texts(self, message_type_letter: str | None = None) -> list[str]:
        """Texts of the direct messages from the server, in order; optionally only one type ("m", "k", ...)."""
        return [
            received_direct_message.text
            for received_direct_message in self.received_direct_messages
            if message_type_letter is None or received_direct_message.message_type_letter == message_type_letter
        ]

    def received_messages(self, peer_username: str) -> list[IncomingMessage]:
        """The complete messages from the peer, in display order: by message id."""
        peer_key = normalize_username_for_lookup(peer_username)
        complete_messages = [
            incoming_message
            for incoming_message in self.storage.incoming_messages.values()
            if incoming_message.is_complete and incoming_message.key[0] == peer_key
        ]
        return sorted(complete_messages, key=lambda incoming_message: incoming_message.message_id)

    def outgoing_messages(self, peer_username: str | None = None) -> list[OutgoingMessage]:
        outgoing_messages = [
            outgoing_message
            for outgoing_message in self.storage.outgoing_messages.values()
            if peer_username is None or usernames_match(outgoing_message.recipient_username, peer_username)
        ]
        return sorted(outgoing_messages, key=lambda outgoing_message: outgoing_message.message_id)

    def conversation(self, peer_username: str) -> list[ConversationEntry]:
        """Both directions of one conversation, ordered by message id as the protocol orders them."""
        incoming_entries = [
            ConversationEntry(
                direction=MessageDirection.INCOMING,
                peer_username=incoming_message.sender_username,
                message_id=incoming_message.message_id,
                text=incoming_message.text,
            )
            for incoming_message in self.received_messages(peer_username)
        ]
        outgoing_entries = [
            ConversationEntry(
                direction=MessageDirection.OUTGOING,
                peer_username=outgoing_message.displayed_recipient_username,
                message_id=outgoing_message.message_id,
                text=outgoing_message.text,
            )
            for outgoing_message in self.outgoing_messages(peer_username)
        ]
        return sorted([*incoming_entries, *outgoing_entries], key=lambda entry: (entry.message_id, entry.direction))

    def events_of_kind(self, kind: ClientEventKind) -> list[ClientEvent]:
        return [event for event in self.events if event.kind is kind]

    async def wait_for_received_messages(
        self, peer_username: str, count: int, *, timeout_seconds: float = 5.0
    ) -> list[IncomingMessage]:
        """Wait until at least `count` complete messages from the peer are held; return them in display order."""
        await self.wait_until(
            lambda: len(self.received_messages(peer_username)) >= count,
            timeout_seconds=timeout_seconds,
            description=f"{count} messages from {peer_username}",
        )
        return self.received_messages(peer_username)

    async def wait_until(
        self, condition: Callable[[], bool], *, timeout_seconds: float = 5.0, description: str = "the condition"
    ) -> None:
        """Wait for a condition, failing at once with the client's own error if its loop raised meanwhile."""

        def condition_or_internal_error() -> bool:
            if self.internal_errors:
                raise AssertionError(f"{self!r} raised internally") from self.internal_errors[0]
            return condition()

        await wait_until(condition_or_internal_error, timeout_seconds=timeout_seconds, description=description)

    # ----- requests and rounds -----------------------------------------------------------------

    def _register_request(self, request: ClientRequest) -> None:
        self.storage.unfinished_requests.append(request)

    def _finish_request(
        self, request: ClientRequest, state: RequestState, now: float, *, error_code: str | None = None
    ) -> None:
        request.cancel_round()
        request.state = state
        request.finished_at = now
        if error_code is not None:
            request.error_code = error_code
        if request in self.storage.unfinished_requests:
            self.storage.unfinished_requests.remove(request)
            self.storage.finished_requests.append(request)

    def _find_active_request[RequestType: ClientRequest](
        self, request_class: type[RequestType], matches: Callable[[RequestType], bool]
    ) -> RequestType | None:
        for request in self._unfinished_requests_of_type(request_class):
            if request.is_active and matches(request):
                return request
        return None

    def _unfinished_requests_of_type[RequestType: ClientRequest](
        self, request_class: type[RequestType]
    ) -> list[RequestType]:
        return [request for request in self.storage.unfinished_requests if isinstance(request, request_class)]

    def _start_request_at(self, request: ClientRequest, start_at: float, now: float) -> None:
        if start_at <= now:
            self._start_round(request, now)
            return
        request.next_round_at = start_at
        request.next_round_schedule_step = request.schedule_step
        request.next_round_is_retry = False

    def _start_round(self, request: ClientRequest, now: float) -> None:
        request.cancel_round()
        request.rounds_started += 1
        request.round_in_progress = True
        if isinstance(request, OutgoingMessage):
            self._queue_message_parts(request, request.missing_part_numbers())
        else:
            self._queue_request_direct_message(request, self._format_request_direct_message(request))

    def _format_request_direct_message(self, request: ClientRequest) -> str:
        match request:
            case SignInRequest():
                return format_account_request(request.username, request.password)
            case UserQuery():
                return format_query_request(request.username)
            case ReadConfirmationRequest():
                return format_read_request(request.sender_username, request.message_id)
            case ConversationRefresh():
                return format_refresh_request(request.refresh_target)
        raise TypeError(f"{request!r} is not sent as one direct message per round.")

    def _queue_request_direct_message(self, request: ClientRequest, text: str) -> None:
        round_token = request.round_token
        request.direct_messages_waiting_for_node = 1
        self.node_link.queue_direct_message(
            text,
            priority=DirectMessagePriority.REQUEST,
            is_still_needed=functools.partial(self._round_is_current, request, round_token),
            server_answered_since=request.server_answered_since,
            on_handed_to_node=functools.partial(self._on_round_direct_message_handed_to_node, request, round_token),
            on_dropped=functools.partial(self._on_round_direct_message_dropped, request, round_token),
        )

    def _queue_message_parts(self, message: OutgoingMessage, part_numbers: list[int]) -> None:
        round_token = message.round_token
        message.direct_messages_waiting_for_node = len(part_numbers)
        for part_number in part_numbers:
            part_text = message.parts[part_number - FIRST_PART_NUMBER]
            self.node_link.queue_direct_message(
                format_message_part_request(
                    message.recipient_username, message.message_id, part_number, message.part_count, part_text
                ),
                priority=DirectMessagePriority.REQUEST,
                is_still_needed=functools.partial(
                    self._message_part_is_still_needed, message, round_token, part_number
                ),
                server_answered_since=message.server_answered_since,
                on_handed_to_node=functools.partial(self._on_round_direct_message_handed_to_node, message, round_token),
                on_dropped=functools.partial(self._on_round_direct_message_dropped, message, round_token),
            )

    @staticmethod
    def _round_is_current(request: ClientRequest, round_token: int) -> bool:
        return request.is_active and request.round_token == round_token

    def _message_part_is_still_needed(self, message: OutgoingMessage, round_token: int, part_number: int) -> bool:
        return self._round_is_current(message, round_token) and not message.is_part_confirmed(part_number)

    def _on_round_direct_message_handed_to_node(self, request: ClientRequest, round_token: int) -> None:
        if request.round_token != round_token:
            return
        now = self._now()
        request.last_direct_message_handed_at = now
        self._count_round_direct_message_done(request, now)

    def _on_round_direct_message_dropped(self, request: ClientRequest, round_token: int) -> None:
        if request.round_token == round_token:
            self._count_round_direct_message_done(request, self._now())

    def _count_round_direct_message_done(self, request: ClientRequest, now: float) -> None:
        request.direct_messages_waiting_for_node -= 1
        if request.direct_messages_waiting_for_node > 0 or not request.is_active:
            return
        request.round_in_progress = False
        request.last_round_handed_to_node_at = now
        self._start_retry_timer(request, now)

    def _start_retry_timer(self, request: ClientRequest, now: float) -> None:
        request.next_round_at = now + self.timing.retry_pause_seconds(request.schedule_step)
        request.next_round_schedule_step = request.schedule_step + 1
        request.next_round_is_retry = True

    def _run_due_timers(self, now: float) -> None:
        for request in list(self.storage.unfinished_requests):
            if request.is_active and request.next_round_at is not None and now >= request.next_round_at:
                self._handle_expired_round_timer(request, now)
        for incoming_message in list(self.storage.incoming_messages.values()):
            acknowledgement_due_at = incoming_message.acknowledgement_due_at
            if acknowledgement_due_at is not None and now >= acknowledgement_due_at:
                incoming_message.acknowledgement_due_at = None
                self._queue_delivery_acknowledgement(incoming_message)

    def _handle_expired_round_timer(self, request: ClientRequest, now: float) -> None:
        request.next_round_at = None
        if isinstance(request, ConversationRefresh) and not self._refresh_may_be_sent_again(request):
            self._finish_request(request, RequestState.ABANDONED, now)
            return
        request.schedule_step = request.next_round_schedule_step
        if request.next_round_is_retry:
            request.retry_rounds += 1
            self.counters.retry_rounds += 1
            self.counters.retry_rounds_by_request_type[request.request_type] += 1
        self._start_round(request, now)

    def _refresh_may_be_sent_again(self, refresh: ConversationRefresh) -> bool:
        if not refresh.is_for_all_peers:
            return self.is_conversation_open(refresh.refresh_target)
        if refresh.maximum_retries is None or not refresh.next_round_is_retry:
            return True
        return refresh.retry_rounds < refresh.maximum_retries

    def _schedule_resumed_round(self, request: ClientRequest, now: float) -> None:
        """Send a resumed request again, but never sooner than its retry pause after its last send."""
        if isinstance(request, ConversationRefresh) and not self._refresh_may_be_sent_again(request):
            self._finish_request(request, RequestState.ABANDONED, now)
            return
        if request.last_direct_message_handed_at is None:
            self._start_round(request, now)
            return
        earliest_round_at = request.last_direct_message_handed_at + self.timing.retry_pause_seconds(
            request.schedule_step
        )
        request.next_round_at = max(now, earliest_round_at)
        request.next_round_schedule_step = request.schedule_step + 1
        request.next_round_is_retry = True

    def _resume_requests_after_app_start(self, now: float) -> None:
        """The previous app's queue died with it: a round it had not finished handing to the node starts again."""
        for request in list(self.storage.unfinished_requests):
            if request.is_active and request.round_in_progress:
                request.cancel_round()
                self.counters.resumed_requests += 1
                self._schedule_resumed_round(request, now)
        for incoming_message in self.storage.incoming_messages.values():
            incoming_message.acknowledgement_due_at = None

    def _fail_request(self, request: ClientRequest, error_code: str, now: float) -> None:
        self._finish_request(request, RequestState.FAILED, now, error_code=error_code)
        if isinstance(request, OutgoingMessage):
            request.status = OutgoingMessageStatus.FAILED
            request.failure_reason = error_code
        if error_code in CLIENT_BUG_ERROR_CODES or error_code not in KNOWN_ERROR_CODES:
            self._record_event(ClientEventKind.CLIENT_BUG_REPORTED_BY_SERVER, now, f"{error_code} for {request!r}")
        if error_code == ErrorCode.NOT_SIGNED_IN:
            self._sign_out_because_server_says_not_signed_in(now)

    # ----- the account -------------------------------------------------------------------------

    def _require_signed_in_account_key(self) -> str:
        signed_in_username = self.storage.signed_in_username
        if signed_in_username is None or self.is_switching_account:
            raise ClientNotSignedInError(f"{self!r} has no account to act for.")
        return normalize_username_for_lookup(signed_in_username)

    def _accepts_server_pushes(self) -> bool:
        return self.storage.signed_in_username is not None and not self.is_switching_account

    def _start_sign_in(self, username: str, password: str, now: float) -> SignInRequest:
        self._abandon_pending_sign_in(now)
        current_username = self.storage.signed_in_username
        is_account_switch = current_username is not None and not usernames_match(current_username, username)
        if current_username is not None and is_account_switch:
            self._record_event(ClientEventKind.ACCOUNT_SWITCH_STARTED, now, f"{current_username} to {username}")
            self._set_aside_requests_of(normalize_username_for_lookup(current_username), now)
            self._acknowledgement_generation += 1
        sign_in = SignInRequest(
            account_key=None,
            created_at=now,
            username=username,
            password=password,
            is_account_switch=is_account_switch,
        )
        self.storage.latest_sign_in = sign_in
        self._register_request(sign_in)
        self._start_request_at(sign_in, self._earliest_sign_in_time(username, now), now)
        return sign_in

    def _abandon_pending_sign_in(self, now: float) -> None:
        latest_sign_in = self.storage.latest_sign_in
        if latest_sign_in is not None and latest_sign_in.is_active:
            self._finish_request(latest_sign_in, RequestState.ABANDONED, now)

    def _earliest_sign_in_time(self, username: str, now: float) -> float:
        """After "e WRONG_PASSWORD" a new "A" for that username waits: the server holds back an equal answer."""
        wrong_password_at = self.storage.wrong_password_times.get(normalize_username_for_lookup(username))
        if wrong_password_at is None:
            return now
        return max(now, wrong_password_at + self.timing.wrong_password_wait_seconds)

    def _handle_account_reply(self, account_reply: AccountReply, now: float) -> ReceivedDirectMessageHandling:
        latest_sign_in = self.storage.latest_sign_in
        if latest_sign_in is not None and usernames_match(latest_sign_in.username, account_reply.username):
            if latest_sign_in.is_active:
                self._complete_sign_in(latest_sign_in, account_reply.username, now)
                return ReceivedDirectMessageHandling.HANDLED
            if latest_sign_in.state is RequestState.FAILED:
                latest_sign_in.was_answered_after_error = True
                self._complete_sign_in(latest_sign_in, account_reply.username, now)
                return ReceivedDirectMessageHandling.HANDLED
        signed_in_username = self.storage.signed_in_username
        if signed_in_username is not None and usernames_match(signed_in_username, account_reply.username):
            return ReceivedDirectMessageHandling.UNMATCHED
        self._handle_unexpected_account_reply(account_reply.username, now)
        return ReceivedDirectMessageHandling.HANDLED

    def _complete_sign_in(self, sign_in: SignInRequest, canonical_username: str, now: float) -> None:
        sign_in.last_answer_at = now
        sign_in.signed_in_username = canonical_username
        sign_in.is_rate_limited = False
        if sign_in.is_finished:
            sign_in.state = RequestState.ANSWERED
        else:
            self._finish_request(sign_in, RequestState.ANSWERED, now)
        self._become_signed_in_as(canonical_username, sign_in.password, now)

    def _become_signed_in_as(self, canonical_username: str, password: str, now: float) -> None:
        account_key = normalize_username_for_lookup(canonical_username)
        previous_conversations_username = self.storage.conversations_username
        self.storage.signed_in_username = canonical_username
        self.storage.remembered_passwords[account_key] = password
        self._discard_requests_of_other_accounts(account_key, now)
        if previous_conversations_username is not None and not usernames_match(
            previous_conversations_username, canonical_username
        ):
            self._clear_conversations(now)
        self.storage.conversations_username = canonical_username
        self._resume_requests_set_aside_for(account_key, now)
        self._record_event(ClientEventKind.SIGNED_IN, now, canonical_username)
        self._start_refresh_of_every_conversation(now)

    def _handle_sign_in_error(self, sign_in: SignInRequest, error_code: str, now: float) -> None:
        if error_code == ErrorCode.RATE_LIMITED:
            self._wait_out_rate_limit(sign_in, now)
            return
        if error_code == ErrorCode.WRONG_PASSWORD:
            self.storage.wrong_password_times[normalize_username_for_lookup(sign_in.username)] = now
        self._finish_request(sign_in, RequestState.FAILED, now, error_code=error_code)
        self._record_event(ClientEventKind.SIGN_IN_FAILED, now, f"{error_code} for {sign_in.username}")
        if error_code in CLIENT_BUG_ERROR_CODES or error_code not in KNOWN_ERROR_CODES:
            self._record_event(ClientEventKind.CLIENT_BUG_REPORTED_BY_SERVER, now, f"{error_code} for {sign_in!r}")
        signed_in_username = self.storage.signed_in_username
        if signed_in_username is not None:
            self._resume_requests_set_aside_for(normalize_username_for_lookup(signed_in_username), now)

    def _wait_out_rate_limit(self, sign_in: SignInRequest, now: float) -> None:
        """Keep the sign-in pending and send it again when the rate-limit window must have ended."""
        sign_in.is_rate_limited = True
        sign_in.cancel_round()
        sign_in.next_round_at = now + self.timing.rate_limited_wait_seconds
        sign_in.next_round_schedule_step = 0
        sign_in.next_round_is_retry = True
        self._record_event(ClientEventKind.SIGN_IN_RATE_LIMITED, now, sign_in.username)

    def _handle_unexpected_account_reply(self, username: str, now: float) -> None:
        """A delayed "A" moved the device to that account: sign out, then sign in again with the remembered password."""
        self.counters.unexpected_account_replies += 1
        previous_username = self.storage.signed_in_username
        if previous_username is None:
            return
        self._record_event(ClientEventKind.SIGNED_OUT_BY_UNEXPECTED_ACCOUNT_REPLY, now, f"a {username}")
        self._sign_out(now)
        if not self._signs_in_again_after_unexpected_account_reply or self.is_signing_in:
            return
        remembered_password = self.storage.remembered_passwords.get(normalize_username_for_lookup(previous_username))
        if remembered_password is not None:
            self._start_sign_in(previous_username, remembered_password, now)

    def _sign_out_because_server_says_not_signed_in(self, now: float) -> None:
        if self.storage.signed_in_username is None:
            return
        self._record_event(ClientEventKind.SIGNED_OUT_BY_SERVER, now, self.storage.signed_in_username)
        self._sign_out(now)

    def _sign_out(self, now: float) -> None:
        """No account from now on; the account's pending requests wait for it to sign in again."""
        signed_in_username = self.storage.signed_in_username
        if signed_in_username is None:
            return
        self.storage.signed_in_username = None
        self._acknowledgement_generation += 1
        self._set_aside_requests_of(normalize_username_for_lookup(signed_in_username), now)

    def _set_aside_requests_of(self, account_key: str, now: float) -> None:
        set_aside_requests = [
            request
            for request in self.storage.unfinished_requests
            if request.is_active and request.account_key == account_key
        ]
        for request in set_aside_requests:
            request.cancel_round()
            request.state = RequestState.SET_ASIDE
        if set_aside_requests:
            self._record_event(ClientEventKind.REQUESTS_SET_ASIDE, now, f"{len(set_aside_requests)} of {account_key}")

    def _resume_requests_set_aside_for(self, account_key: str, now: float) -> None:
        resumed_requests = [
            request
            for request in self.storage.unfinished_requests
            if request.state is RequestState.SET_ASIDE and request.account_key == account_key
        ]
        for request in resumed_requests:
            request.state = RequestState.ACTIVE
            self._schedule_resumed_round(request, now)
        if resumed_requests:
            self._record_event(ClientEventKind.REQUESTS_RESUMED, now, f"{len(resumed_requests)} of {account_key}")

    def _discard_requests_of_other_accounts(self, account_key: str, now: float) -> None:
        discarded_requests = [
            request
            for request in self.storage.unfinished_requests
            if request.account_key is not None and request.account_key != account_key
        ]
        for request in discarded_requests:
            self._finish_request(request, RequestState.DISCARDED, now)
            if isinstance(request, OutgoingMessage):
                request.status = OutgoingMessageStatus.FAILED
                request.failure_reason = FAILURE_REASON_ACCOUNT_SWITCHED
        if discarded_requests:
            self._record_event(ClientEventKind.REQUESTS_DISCARDED, now, str(len(discarded_requests)))

    def _clear_conversations(self, now: float) -> None:
        self.storage.outgoing_messages.clear()
        self.storage.incoming_messages.clear()
        self.open_conversations.clear()
        self._record_event(ClientEventKind.CONVERSATIONS_CLEARED, now, self.storage.conversations_username or "")

    def _start_refresh_of_every_conversation(self, now: float) -> ConversationRefresh:
        for refresh in self._unfinished_requests_of_type(ConversationRefresh):
            if refresh.is_for_all_peers:
                self._finish_request(refresh, RequestState.ABANDONED, now)
        account_key = normalize_username_for_lookup(self._require_signed_in_username())
        refresh = ConversationRefresh(
            account_key=account_key,
            created_at=now,
            refresh_target=REFRESH_ALL_PEERS_TARGET,
            maximum_retries=self.timing.refresh_all_maximum_retries,
        )
        self._register_request(refresh)
        self._start_round(refresh, now)
        return refresh

    def _require_signed_in_username(self) -> str:
        signed_in_username = self.storage.signed_in_username
        if signed_in_username is None:
            raise ClientNotSignedInError(f"{self!r} has no account to act for.")
        return signed_in_username

    def _find_outstanding_conversation_refresh(self, peer_username: str) -> ConversationRefresh | None:
        return self._find_active_request(
            ConversationRefresh,
            lambda refresh: not refresh.is_for_all_peers and usernames_match(refresh.refresh_target, peer_username),
        )

    def _observe_node_connection(self, now: float) -> None:
        node_is_reachable = self.device.app_can_reach_node
        node_was_reachable = self._node_was_reachable
        self._node_was_reachable = node_is_reachable
        if node_is_reachable and node_was_reachable is False:
            self.counters.node_reconnections += 1
            self._record_event(ClientEventKind.NODE_RECONNECTED, now)
            self._refresh_everything_after_long_server_silence(now)

    def _refresh_everything_after_long_server_silence(self, now: float) -> None:
        if not self._accepts_server_pushes():
            return
        last_server_direct_message_at = self.storage.last_server_direct_message_at
        silence_limit_seconds = self.timing.server_silence_before_refresh_all_seconds
        if last_server_direct_message_at is not None and now - last_server_direct_message_at <= silence_limit_seconds:
            return
        if self._find_active_request(ConversationRefresh, lambda refresh: refresh.is_for_all_peers) is None:
            self._start_refresh_of_every_conversation(now)

    # ----- direct messages from the server -----------------------------------------------------

    def _receive_server_direct_messages(self, now: float) -> None:
        if not self.device.app_can_reach_node:
            return
        for received_direct_message in self.device.receive_direct_messages():
            self._handle_received_direct_message(received_direct_message, now)

    def _handle_received_direct_message(self, received_direct_message: ReceivedDirectMessage, now: float) -> None:
        if not self._comes_from_pinned_server(received_direct_message):
            self.counters.direct_messages_not_from_the_server += 1
            return
        self.storage.last_server_direct_message_at = now
        parsed_message = parse_direct_message_text(received_direct_message.text)
        handling = self._handle_server_direct_message(parsed_message, received_direct_message, now)
        if handling is ReceivedDirectMessageHandling.DROPPED_MALFORMED:
            self.counters.malformed_server_direct_messages += 1
        elif handling is ReceivedDirectMessageHandling.UNMATCHED:
            self.counters.unmatched_server_answers += 1
        self.received_direct_messages.append(
            ReceivedServerDirectMessage(
                text=received_direct_message.text,
                parsed_message=parsed_message,
                meshcore_timestamp=received_direct_message.sender_timestamp,
                arrived_by_flood=received_direct_message.arrived_by_flood,
                received_at=now,
                handling=handling,
            )
        )

    def _comes_from_pinned_server(self, received_direct_message: ReceivedDirectMessage) -> bool:
        pinned_server_public_key = self.storage.pinned_server_public_key
        if pinned_server_public_key is None or received_direct_message.text_type != TextType.PLAIN:
            return False
        return received_direct_message.sender_public_key_prefix == pinned_server_public_key[:PUBLIC_KEY_PREFIX_BYTES]

    def _handle_server_direct_message(
        self, parsed_message: ParsedDirectMessage, received_direct_message: ReceivedDirectMessage, now: float
    ) -> ReceivedDirectMessageHandling:
        match parsed_message:
            case AccountReply():
                return self._handle_account_reply(parsed_message, now)
            case QueryReply():
                return self._handle_query_reply(parsed_message, now)
            case SendStatusReply():
                return self._handle_send_status_reply(parsed_message, now)
            case DeliveryPart():
                return self._handle_delivery_part(parsed_message, received_direct_message, now)
            case ReadReply():
                return self._handle_read_reply(parsed_message, now)
            case ReceiptPush():
                return self._handle_receipt_push(parsed_message, received_direct_message, now)
            case RefreshReply():
                return self._handle_refresh_reply(parsed_message, now)
            case ErrorReply():
                return self._handle_error_reply(parsed_message, now)
            case OtherText():
                return ReceivedDirectMessageHandling.OTHER_TEXT
            case ProtocolSyntaxError():
                return ReceivedDirectMessageHandling.DROPPED_MALFORMED
            case _:
                return ReceivedDirectMessageHandling.IGNORED

    def _handle_query_reply(self, query_reply: QueryReply, now: float) -> ReceivedDirectMessageHandling:
        query = self._find_active_request(
            UserQuery, lambda query: usernames_match(query.username, query_reply.username)
        )
        if query is None:
            return ReceivedDirectMessageHandling.UNMATCHED
        query.last_answer_at = now
        query.user_exists = query_reply.user_exists
        query.answered_username = query_reply.username
        self._finish_request(query, RequestState.ANSWERED, now)
        return ReceivedDirectMessageHandling.HANDLED

    def _handle_send_status_reply(self, send_status: SendStatusReply, now: float) -> ReceivedDirectMessageHandling:
        message = self.storage.outgoing_messages.get(
            build_message_key(send_status.recipient_username, send_status.message_id)
        )
        if message is None or not message.is_active:
            return ReceivedDirectMessageHandling.UNMATCHED
        if not received_set_matches_part_count(send_status.received_set, message.part_count):
            return ReceivedDirectMessageHandling.DROPPED_MALFORMED
        message.last_answer_at = now
        message.canonical_recipient_username = send_status.recipient_username
        message.confirmed_set = send_status.received_set
        if message.is_confirmed_by_server:
            message.raise_status(OutgoingMessageStatus.SENT)
            self._finish_request(message, RequestState.ANSWERED, now)
        elif not message.round_in_progress:
            self._send_missing_parts_at_once(message)
        return ReceivedDirectMessageHandling.HANDLED

    def _send_missing_parts_at_once(self, message: OutgoingMessage) -> None:
        """Not a retry: the schedule step stays, and the timer restarts after the last missing part."""
        self.counters.missing_part_resends += 1
        message.cancel_round()
        message.round_in_progress = True
        self._queue_message_parts(message, message.missing_part_numbers())

    def _handle_delivery_part(
        self, delivery_part: DeliveryPart, received_direct_message: ReceivedDirectMessage, now: float
    ) -> ReceivedDirectMessageHandling:
        if not self._accepts_server_pushes():
            self.counters.discarded_server_direct_messages += 1
            return ReceivedDirectMessageHandling.DISCARDED_WITHOUT_ACCOUNT
        if not is_well_formed_delivery_part(delivery_part):
            return ReceivedDirectMessageHandling.DROPPED_MALFORMED
        incoming_message = self._find_or_create_incoming_message(delivery_part, now)
        if incoming_message is None:
            return ReceivedDirectMessageHandling.DROPPED_MALFORMED
        if received_direct_message.arrived_by_flood:
            self.node_link.decide_route_reset_before_answering_flood(received_direct_message.text, now)
        incoming_message.part_copies_received += 1
        incoming_message.parts.setdefault(delivery_part.part_number, delivery_part.part_text)
        if incoming_message.is_complete:
            self._display_once(incoming_message, now)
            incoming_message.acknowledgement_due_at = None
            self._queue_delivery_acknowledgement(incoming_message)
        else:
            coalescing_seconds = self.timing.incomplete_acknowledgement_coalescing_seconds
            incoming_message.acknowledgement_due_at = now + coalescing_seconds
        return ReceivedDirectMessageHandling.HANDLED

    def _find_or_create_incoming_message(self, delivery_part: DeliveryPart, now: float) -> IncomingMessage | None:
        """None for a part whose count disagrees with the parts already held: the server never sends one."""
        message_key = build_message_key(delivery_part.sender_username, delivery_part.message_id)
        incoming_message = self.storage.incoming_messages.get(message_key)
        if incoming_message is None:
            incoming_message = IncomingMessage(
                sender_username=delivery_part.sender_username,
                message_id=delivery_part.message_id,
                part_count=delivery_part.part_count,
                first_part_received_at=now,
            )
            self.storage.incoming_messages[message_key] = incoming_message
            return incoming_message
        if incoming_message.part_count != delivery_part.part_count:
            return None
        return incoming_message

    def _display_once(self, incoming_message: IncomingMessage, now: float) -> None:
        if incoming_message.is_displayed:
            return
        incoming_message.displayed_at = now
        self.displayed_messages.append(incoming_message)
        self._record_event(
            ClientEventKind.MESSAGE_DISPLAYED,
            now,
            f"{incoming_message.message_id} from {incoming_message.sender_username}",
        )
        if self._reads_messages_in_open_conversations and self.is_conversation_open(incoming_message.sender_username):
            self._request_read_confirmation(incoming_message, self._require_signed_in_account_key(), now)

    def _queue_delivery_acknowledgement(self, incoming_message: IncomingMessage) -> None:
        if not self._accepts_server_pushes():
            return
        self.counters.delivery_acknowledgements_queued += 1
        self._queue_acknowledgement(
            format_delivery_acknowledgement(
                incoming_message.sender_username, incoming_message.message_id, incoming_message.received_set
            )
        )

    def _queue_acknowledgement(self, text: str) -> None:
        self.node_link.queue_direct_message(
            text,
            priority=DirectMessagePriority.ACKNOWLEDGEMENT,
            is_still_needed=functools.partial(self._acknowledgement_is_still_needed, self._acknowledgement_generation),
        )

    def _acknowledgement_is_still_needed(self, acknowledgement_generation: int) -> bool:
        return acknowledgement_generation == self._acknowledgement_generation

    def _handle_read_reply(self, read_reply: ReadReply, now: float) -> ReceivedDirectMessageHandling:
        message_key = build_message_key(read_reply.sender_username, read_reply.message_id)
        read_request = self._find_active_request(ReadConfirmationRequest, lambda request: request.key == message_key)
        if read_request is None:
            return ReceivedDirectMessageHandling.UNMATCHED
        read_request.last_answer_at = now
        self._finish_request(read_request, RequestState.ANSWERED, now)
        return ReceivedDirectMessageHandling.HANDLED

    def _request_read_confirmation(
        self, incoming_message: IncomingMessage, account_key: str, now: float
    ) -> ReadConfirmationRequest:
        if incoming_message.read_request is not None:
            return incoming_message.read_request
        read_request = ReadConfirmationRequest(
            account_key=account_key,
            created_at=now,
            sender_username=incoming_message.sender_username,
            message_id=incoming_message.message_id,
        )
        incoming_message.read_request = read_request
        self._register_request(read_request)
        self._start_round(read_request, now)
        return read_request

    def _handle_receipt_push(
        self, receipt_push: ReceiptPush, received_direct_message: ReceivedDirectMessage, now: float
    ) -> ReceivedDirectMessageHandling:
        if not self._accepts_server_pushes():
            self.counters.discarded_server_direct_messages += 1
            return ReceivedDirectMessageHandling.DISCARDED_WITHOUT_ACCOUNT
        if received_direct_message.arrived_by_flood:
            self.node_link.decide_route_reset_before_answering_flood(received_direct_message.text, now)
        message = self.storage.outgoing_messages.get(
            build_message_key(receipt_push.recipient_username, receipt_push.message_id)
        )
        if message is None:
            self.counters.receipts_for_unknown_messages += 1
            self.receipts_for_unknown_messages.append(receipt_push)
        else:
            message.canonical_recipient_username = receipt_push.recipient_username
            self._apply_receipt(message, receipt_push.receipt_level, now)
        self.counters.receipt_acknowledgements_queued += 1
        self._queue_acknowledgement(
            format_receipt_acknowledgement(
                receipt_push.recipient_username, receipt_push.message_id, receipt_push.receipt_level
            )
        )
        return ReceivedDirectMessageHandling.HANDLED

    def _apply_receipt(self, message: OutgoingMessage, receipt_level: ReceiptLevel, now: float) -> None:
        """Any receipt also completes the upload: the server delivered the message, so it holds every part."""
        message.received_receipt_levels.append(receipt_level)
        message.raise_status(RECEIPT_LEVEL_STATUSES[receipt_level])
        if message.is_active:
            message.last_answer_at = now
            self._finish_request(message, RequestState.ANSWERED, now)

    def _handle_refresh_reply(self, refresh_reply: RefreshReply, now: float) -> ReceivedDirectMessageHandling:
        refresh = self._find_active_request(
            ConversationRefresh, lambda refresh: refresh.targets(refresh_reply.refresh_target)
        )
        if refresh is None:
            return ReceivedDirectMessageHandling.UNMATCHED
        refresh.last_answer_at = now
        refresh.reported_message_count = refresh_reply.message_count
        self._finish_request(refresh, RequestState.ANSWERED, now)
        return ReceivedDirectMessageHandling.HANDLED

    def _handle_error_reply(self, error_reply: ErrorReply, now: float) -> ReceivedDirectMessageHandling:
        request = self._find_request_answered_by_error(error_reply)
        if request is None:
            return ReceivedDirectMessageHandling.UNMATCHED
        request.last_answer_at = now
        error_code = str(error_reply.error_code)
        match request:
            case SignInRequest():
                self._handle_sign_in_error(request, error_code, now)
            case OutgoingMessage() if error_code == ErrorCode.ID_CONFLICT:
                self._send_again_under_new_id(request, now)
            case _:
                self._fail_request(request, error_code, now)
        return ReceivedDirectMessageHandling.HANDLED

    def _find_request_answered_by_error(self, error_reply: ErrorReply) -> ClientRequest | None:
        error_reference = error_reply.error_reference
        if error_reference is None:
            return None
        match error_reply.request_type:
            case ClientMessageType.ACCOUNT_REQUEST:
                return self._find_pending_sign_in_for(error_reference)
            case ClientMessageType.QUERY_REQUEST:
                return self._find_active_request(
                    UserQuery, lambda query: usernames_match(query.username, error_reference)
                )
            case ClientMessageType.MESSAGE_PART_REQUEST:
                return self._find_outgoing_message_by_reference(error_reference)
            case ClientMessageType.READ_REQUEST:
                message_key = parse_message_reference(error_reference)
                return self._find_active_request(ReadConfirmationRequest, lambda request: request.key == message_key)
            case ClientMessageType.REFRESH_REQUEST:
                return self._find_active_request(ConversationRefresh, lambda refresh: refresh.targets(error_reference))
        return None

    def _find_pending_sign_in_for(self, username: str) -> SignInRequest | None:
        latest_sign_in = self.storage.latest_sign_in
        if latest_sign_in is None or not latest_sign_in.is_active:
            return None
        if not usernames_match(latest_sign_in.username, username):
            return None
        return latest_sign_in

    def _find_outgoing_message_by_reference(self, error_reference: str) -> OutgoingMessage | None:
        message_key = parse_message_reference(error_reference)
        if message_key is None:
            return None
        message = self.storage.outgoing_messages.get(message_key)
        if message is None or not message.is_active:
            return None
        return message

    def _send_again_under_new_id(self, message: OutgoingMessage, now: float) -> None:
        """The id belongs to another message of the same sender: new id, parts computed for it, a fresh round."""
        self.counters.id_conflict_resends += 1
        self.storage.outgoing_messages.pop(message.key, None)
        message.replaced_message_ids.append(message.message_id)
        message.message_id = self._allocate_message_id()
        message.parts = tuple(split_message_text(message.text, self._grapheme_cluster_segmenter))
        message.confirmed_set = RECEIVED_SET_MISSING * message.part_count
        message.schedule_step = 0
        self.storage.outgoing_messages[message.key] = message
        self._start_round(message, now)

    # ----- helpers -----------------------------------------------------------------------------

    def _allocate_message_id(self) -> int:
        """max(now in microseconds, previous + 1), stored before the id is first used."""
        now_microseconds = int(self.clock.wall_clock_seconds() * MICROSECONDS_PER_SECOND)
        message_id = max(now_microseconds, self.storage.last_message_id + 1)
        self.storage.last_message_id = message_id
        return message_id

    def _record_event(self, kind: ClientEventKind, now: float, detail: str = "") -> None:
        self.events.append(ClientEvent(kind=kind, at=now, detail=detail))


def is_well_formed_delivery_part(delivery_part: DeliveryPart) -> bool:
    """The value rules the parser leaves to the receiver: part numbering and part text."""
    return is_valid_part_numbering(delivery_part.part_number, delivery_part.part_count) and is_valid_part_text(
        delivery_part.part_text
    )
