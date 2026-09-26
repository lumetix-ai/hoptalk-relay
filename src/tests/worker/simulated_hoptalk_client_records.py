"""What the simulated HopTalk client keeps: its requests, its messages, and the storage that survives a restart.

Every request (A, Q, M, R, F) is one `ClientRequest` object that the client retries until it is
answered, fails or is given up. The same objects are the handles a test holds: `OutgoingMessage`
for a sent message, `SignInRequest`, `UserQuery`, `ReadConfirmationRequest` and
`ConversationRefresh`. Times are the client clock's monotonic seconds.
"""

from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import ClassVar

from protocol.constants import (
    RECEIVED_SET_MISSING,
    RECEIVED_SET_RECEIVED,
    REFRESH_ALL_PEERS_TARGET,
    ClientMessageType,
    ReceiptLevel,
)
from protocol.message_types import ParsedDirectMessage
from protocol.received_sets import FIRST_PART_NUMBER, is_complete_received_set
from protocol.usernames import normalize_username_for_lookup
from tests.worker.fake_node.waiting import wait_until

type MessageKey = tuple[str, int]


def build_message_key(username: str, message_id: int) -> MessageKey:
    """Messages of one direction are told apart by (peer, id), the peer compared case-insensitively."""
    return (normalize_username_for_lookup(username), message_id)


def usernames_match(first_username: str, second_username: str) -> bool:
    return normalize_username_for_lookup(first_username) == normalize_username_for_lookup(second_username)


class RequestState(StrEnum):
    ACTIVE = "active"
    # The device is switching (or was moved) to another account: the request waits, unsent, until
    # the device is signed in as its account again.
    SET_ASIDE = "set_aside"
    ANSWERED = "answered"
    FAILED = "failed"
    # Given up by the client: a closed conversation, a retry limit reached, a newer sign-in.
    ABANDONED = "abandoned"
    # The device now belongs to another account, so the request can never be sent again.
    DISCARDED = "discarded"


UNFINISHED_REQUEST_STATES = frozenset({RequestState.ACTIVE, RequestState.SET_ASIDE})


@dataclass(kw_only=True, eq=False)
class ClientRequest:
    """One request and its retry state.

    A round sends the request's direct messages; the retry timer (`next_round_at`) starts once the
    last of them has been handed to the node. `schedule_step` picks the retry pause of the current
    round. `round_token` changes whenever a round starts or is cancelled, so direct messages still
    queued for an older round are dropped instead of sent.
    """

    request_type: ClassVar[ClientMessageType]

    # The lower-case username of the account the request belongs to; None for a sign-in.
    account_key: str | None
    created_at: float
    state: RequestState = RequestState.ACTIVE
    schedule_step: int = 0
    rounds_started: int = 0
    retry_rounds: int = 0
    round_token: int = 0
    round_in_progress: bool = False
    direct_messages_waiting_for_node: int = 0
    last_direct_message_handed_at: float | None = None
    last_round_handed_to_node_at: float | None = None
    next_round_at: float | None = None
    next_round_schedule_step: int = 0
    next_round_is_retry: bool = False
    last_answer_at: float | None = None
    error_code: str | None = None
    finished_at: float | None = None

    @property
    def is_active(self) -> bool:
        return self.state is RequestState.ACTIVE

    @property
    def is_finished(self) -> bool:
        return self.state not in UNFINISHED_REQUEST_STATES

    def server_answered_since(self, moment: float) -> bool:
        return self.last_answer_at is not None and self.last_answer_at >= moment

    def cancel_round(self) -> None:
        """Forget the current round and its timer; direct messages still queued for it are dropped."""
        self.round_token += 1
        self.round_in_progress = False
        self.direct_messages_waiting_for_node = 0
        self.next_round_at = None

    async def wait_until_finished(self, *, timeout_seconds: float = 5.0) -> None:
        await wait_until(lambda: self.is_finished, timeout_seconds=timeout_seconds, description=f"{self!r} to finish")


@dataclass(kw_only=True, eq=False)
class SignInRequest(ClientRequest):
    request_type: ClassVar[ClientMessageType] = ClientMessageType.ACCOUNT_REQUEST

    username: str
    password: str = field(repr=False)
    # Another account was signed in when the request was made: its requests were set aside.
    is_account_switch: bool
    signed_in_username: str | None = None
    is_rate_limited: bool = False
    # The "a" came after an "e" had already finished the request: a delayed answer to an earlier copy.
    was_answered_after_error: bool = False

    @property
    def is_signed_in(self) -> bool:
        return self.signed_in_username is not None


@dataclass(kw_only=True, eq=False)
class UserQuery(ClientRequest):
    request_type: ClassVar[ClientMessageType] = ClientMessageType.QUERY_REQUEST

    username: str
    user_exists: bool | None = None
    # The server's spelling; for a user that does not exist, the spelling that was sent.
    answered_username: str | None = None


@dataclass(kw_only=True, eq=False)
class ReadConfirmationRequest(ClientRequest):
    request_type: ClassVar[ClientMessageType] = ClientMessageType.READ_REQUEST

    sender_username: str
    message_id: int

    @property
    def key(self) -> MessageKey:
        return build_message_key(self.sender_username, self.message_id)


@dataclass(kw_only=True, eq=False)
class ConversationRefresh(ClientRequest):
    request_type: ClassVar[ClientMessageType] = ClientMessageType.REFRESH_REQUEST

    # A username for one conversation, or "*" for every conversation.
    refresh_target: str
    # None: retried for as long as its conversation stays open.
    maximum_retries: int | None
    reported_message_count: int | None = None

    @property
    def is_for_all_peers(self) -> bool:
        return self.refresh_target == REFRESH_ALL_PEERS_TARGET

    def targets(self, refresh_target: str) -> bool:
        if self.is_for_all_peers or refresh_target == REFRESH_ALL_PEERS_TARGET:
            return self.refresh_target == refresh_target
        return usernames_match(self.refresh_target, refresh_target)


class OutgoingMessageStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


# Receipts only ever raise a status; a receipt for a message the client had failed still raises it,
# because the server delivered that message.
STATUS_PROGRESS = {
    OutgoingMessageStatus.PENDING: 0,
    OutgoingMessageStatus.FAILED: 0,
    OutgoingMessageStatus.SENT: 1,
    OutgoingMessageStatus.DELIVERED: 2,
    OutgoingMessageStatus.READ: 3,
}
RECEIPT_LEVEL_STATUSES = {
    ReceiptLevel.DELIVERED: OutgoingMessageStatus.DELIVERED,
    ReceiptLevel.READ: OutgoingMessageStatus.READ,
}
FAILURE_REASON_ACCOUNT_SWITCHED = "discarded: the device signed in as another account"


@dataclass(kw_only=True, eq=False)
class OutgoingMessage(ClientRequest):
    """A message this client sends, and the handle a test follows it with.

    `parts` are the exact part texts, stored before the first part is sent; every resend of a part
    is byte-identical. `confirmed_set` is the latest received-set the server reported, which
    replaces the earlier one. After "e ID_CONFLICT" the message goes out again under a new id and
    the old one moves to `replaced_message_ids`.
    """

    request_type: ClassVar[ClientMessageType] = ClientMessageType.MESSAGE_PART_REQUEST

    recipient_username: str
    text: str
    message_id: int
    parts: tuple[str, ...]
    confirmed_set: str
    status: OutgoingMessageStatus = OutgoingMessageStatus.PENDING
    # The server's spelling of the recipient, learned from its first "k" or "s".
    canonical_recipient_username: str | None = None
    failure_reason: str | None = None
    replaced_message_ids: list[int] = field(default_factory=list)
    received_receipt_levels: list[ReceiptLevel] = field(default_factory=list)

    @property
    def key(self) -> MessageKey:
        return build_message_key(self.recipient_username, self.message_id)

    @property
    def part_count(self) -> int:
        return len(self.parts)

    @property
    def displayed_recipient_username(self) -> str:
        return self.canonical_recipient_username or self.recipient_username

    @property
    def is_confirmed_by_server(self) -> bool:
        return is_complete_received_set(self.confirmed_set)

    def is_part_confirmed(self, part_number: int) -> bool:
        return self.confirmed_set[part_number - FIRST_PART_NUMBER] == RECEIVED_SET_RECEIVED

    def missing_part_numbers(self) -> list[int]:
        return [
            part_index + FIRST_PART_NUMBER
            for part_index, part_state in enumerate(self.confirmed_set)
            if part_state == RECEIVED_SET_MISSING
        ]

    def raise_status(self, new_status: OutgoingMessageStatus) -> None:
        if STATUS_PROGRESS[new_status] > STATUS_PROGRESS[self.status]:
            self.status = new_status

    async def wait_for_status(self, status: OutgoingMessageStatus, *, timeout_seconds: float = 5.0) -> None:
        await wait_until(
            lambda: self.status is status,
            timeout_seconds=timeout_seconds,
            description=f"message {self.message_id} to {self.recipient_username} to become {status}",
        )


@dataclass(kw_only=True, eq=False)
class IncomingMessage:
    """A message from a peer, reassembled from its "m" parts; the first copy of every part is kept."""

    sender_username: str
    message_id: int
    part_count: int
    first_part_received_at: float
    parts: dict[int, str] = field(default_factory=dict)
    part_copies_received: int = 0
    displayed_at: float | None = None
    # When the coalesced, incomplete "K" goes out; None when none is waiting.
    acknowledgement_due_at: float | None = None
    read_request: ReadConfirmationRequest | None = None

    @property
    def key(self) -> MessageKey:
        return build_message_key(self.sender_username, self.message_id)

    @property
    def is_complete(self) -> bool:
        return len(self.parts) == self.part_count

    @property
    def is_displayed(self) -> bool:
        return self.displayed_at is not None

    @property
    def received_set(self) -> str:
        part_states = [
            RECEIVED_SET_RECEIVED if part_number in self.parts else RECEIVED_SET_MISSING
            for part_number in range(FIRST_PART_NUMBER, self.part_count + FIRST_PART_NUMBER)
        ]
        return "".join(part_states)

    @property
    def text(self) -> str:
        if not self.is_complete:
            raise ValueError(f"Message {self.message_id} from {self.sender_username} is not complete yet.")
        return "".join(self.parts[part_number] for part_number in sorted(self.parts))


@dataclass(kw_only=True)
class SimulatedClientStorage:
    """What the app keeps on the phone: a restarted app gets the same object, a reinstalled one a new one."""

    pinned_server_public_key: bytes | None = None
    signed_in_username: str | None = None
    # The account whose conversations the app holds. It differs from the signed-in account only
    # while the app is signed out; a sign-in as another account clears the conversations.
    conversations_username: str | None = None
    # For signing in again on its own after a delayed "a" moved the device to another account.
    remembered_passwords: dict[str, str] = field(default_factory=dict, repr=False)
    last_message_id: int = 0
    last_meshcore_timestamp: int = 0
    last_server_direct_message_at: float | None = None
    wrong_password_times: dict[str, float] = field(default_factory=dict)
    latest_sign_in: SignInRequest | None = None
    unfinished_requests: list[ClientRequest] = field(default_factory=list)
    finished_requests: list[ClientRequest] = field(default_factory=list)
    outgoing_messages: dict[MessageKey, OutgoingMessage] = field(default_factory=dict)
    incoming_messages: dict[MessageKey, IncomingMessage] = field(default_factory=dict)


class ReceivedDirectMessageHandling(StrEnum):
    HANDLED = "handled"
    # An "m" or "s" while no account is signed in or an account switch is under way: not answered.
    DISCARDED_WITHOUT_ACCOUNT = "discarded_without_account"
    # An answer that matches no pending request, or a status for a message that is not being sent.
    UNMATCHED = "unmatched"
    OTHER_TEXT = "other_text"
    # Another protocol version, a type this client does not know, or an upper-case (client) type.
    IGNORED = "ignored"
    DROPPED_MALFORMED = "dropped_malformed"


@dataclass(frozen=True, kw_only=True)
class ReceivedServerDirectMessage:
    text: str
    parsed_message: ParsedDirectMessage
    meshcore_timestamp: int
    arrived_by_flood: bool
    received_at: float
    handling: ReceivedDirectMessageHandling

    @property
    def message_type_letter(self) -> str:
        return str(getattr(self.parsed_message, "message_type", ""))


class ClientEventKind(StrEnum):
    SIGNED_IN = "signed_in"
    SIGN_IN_FAILED = "sign_in_failed"
    SIGN_IN_RATE_LIMITED = "sign_in_rate_limited"
    ACCOUNT_SWITCH_STARTED = "account_switch_started"
    REQUESTS_SET_ASIDE = "requests_set_aside"
    REQUESTS_RESUMED = "requests_resumed"
    REQUESTS_DISCARDED = "requests_discarded"
    CONVERSATIONS_CLEARED = "conversations_cleared"
    # "a" for an account the app did not ask for: the device was moved; the user must sign in again.
    SIGNED_OUT_BY_UNEXPECTED_ACCOUNT_REPLY = "signed_out_by_unexpected_account_reply"
    # "e NOT_SIGNED_IN": the server does not know the device as signed in; the user must sign in.
    SIGNED_OUT_BY_SERVER = "signed_out_by_server"
    MESSAGE_DISPLAYED = "message_displayed"
    # A permanent error that only a client bug or a newer server explains: SYNTAX, UNSUPPORTED,
    # VERSION, PART_INVALID, or a code this client does not know.
    CLIENT_BUG_REPORTED_BY_SERVER = "client_bug_reported_by_server"
    NODE_RECONNECTED = "node_reconnected"


@dataclass(frozen=True, kw_only=True)
class ClientEvent:
    kind: ClientEventKind
    at: float
    detail: str = ""


@dataclass(kw_only=True)
class ClientCounters:
    retry_rounds: int = 0
    retry_rounds_by_request_type: Counter[str] = field(default_factory=Counter)
    resumed_requests: int = 0
    missing_part_resends: int = 0
    id_conflict_resends: int = 0
    delivery_acknowledgements_queued: int = 0
    receipt_acknowledgements_queued: int = 0
    discarded_server_direct_messages: int = 0
    malformed_server_direct_messages: int = 0
    unmatched_server_answers: int = 0
    unexpected_account_replies: int = 0
    receipts_for_unknown_messages: int = 0
    # From a contact other than the pinned server, or not plain text: never protocol traffic.
    direct_messages_not_from_the_server: int = 0
    node_reconnections: int = 0
    route_resets: int = 0
    route_resets_skipped: int = 0
    table_full_rejections: int = 0
    direct_messages_refused_by_node: int = 0
