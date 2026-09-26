"""One immutable value per HT1 message type, and the other outcomes of parsing a direct message.

Field values are exactly what travelled on the wire: usernames in the case they were sent,
received-sets as their "0"/"1" strings, tails (passwords and part texts) verbatim. Value rules
(part ranges, part text, passwords) are checked by the services, not by the parser, because
each has an error code of its own, and a part's comes after the sign-in and user checks.
"""

from dataclasses import dataclass
from typing import ClassVar

from protocol.constants import REFRESH_ALL_PEERS_TARGET, ClientMessageType, ErrorCode, ReceiptLevel, ServerMessageType

# ----------------------------------------------------------------------------------------------------------------------
# Client to server
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountRequest:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.ACCOUNT_REQUEST

    username: str
    password: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryRequest:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.QUERY_REQUEST

    username: str


@dataclass(frozen=True, slots=True, kw_only=True)
class MessagePartRequest:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.MESSAGE_PART_REQUEST

    recipient_username: str
    message_id: int
    part_number: int
    part_count: int
    part_text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryAcknowledgement:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.DELIVERY_ACKNOWLEDGEMENT

    sender_username: str
    message_id: int
    received_set: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadRequest:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.READ_REQUEST

    sender_username: str
    message_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptAcknowledgement:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.RECEIPT_ACKNOWLEDGEMENT

    recipient_username: str
    message_id: int
    receipt_level: ReceiptLevel


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshRequest:
    message_type: ClassVar[ClientMessageType] = ClientMessageType.REFRESH_REQUEST

    refresh_target: str

    @property
    def is_for_all_peers(self) -> bool:
        return self.refresh_target == REFRESH_ALL_PEERS_TARGET


# ----------------------------------------------------------------------------------------------------------------------
# Server to client
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class AccountReply:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.ACCOUNT_REPLY

    username: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QueryReply:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.QUERY_REPLY

    username: str
    user_exists: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class SendStatusReply:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.SEND_STATUS_REPLY

    recipient_username: str
    message_id: int
    received_set: str


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryPart:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.DELIVERY_PART

    sender_username: str
    message_id: int
    part_number: int
    part_count: int
    part_text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ReadReply:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.READ_REPLY

    sender_username: str
    message_id: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ReceiptPush:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.RECEIPT_PUSH

    recipient_username: str
    message_id: int
    receipt_level: ReceiptLevel


@dataclass(frozen=True, slots=True, kw_only=True)
class RefreshReply:
    message_type: ClassVar[ServerMessageType] = ServerMessageType.REFRESH_REPLY

    refresh_target: str
    message_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class ErrorReply:
    """`request_type` is the answered request's letter, or "?" when it could not be determined.

    `error_reference` repeats the request's correlation fields as they were sent (protocol
    section 10.1): "<username>", "<peer> <id>", "<peer or *>" or "<version>"; None leaves it out.
    A parsed code that this version does not know (from a newer server) stays a plain string:
    a client must still treat it as a permanent error.
    """

    message_type: ClassVar[ServerMessageType] = ServerMessageType.ERROR_REPLY

    error_code: ErrorCode | str
    request_type: str
    error_reference: str | None = None


# ----------------------------------------------------------------------------------------------------------------------
# Direct messages that are not a well-formed HT1 message
# ----------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class OtherText:
    """Fails "^HT[0-9]{1,3} ": not protocol traffic, never answered."""

    text: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UnsupportedVersion:
    """Protocol traffic of another version, such as "HT2 Q bob".

    Only a request (an upper-case `next_character`) is answered, with "e VERSION ? <version>";
    `version` keeps the digits as sent, and `next_character` is "" at the end of the text.
    """

    version: str
    next_character: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UnknownMessageType:
    """A letter after "HT1 " that is no known type; an upper-case one is answered "e UNSUPPORTED X"."""

    message_type_letter: str


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtocolSyntaxError:
    """An HT1 message that breaks the grammar (protocol section 4).

    `request_type` is the letter after "HT1 ", or "?" when there is none. `correlation_fields`
    holds the leading fields that did parse, in order, for the "e SYNTAX" reference: for
    example ("bob", "1790294400123456") for an M whose part field is malformed.
    """

    request_type: str
    correlation_fields: tuple[str, ...] = ()


type ClientMessage = (
    AccountRequest
    | QueryRequest
    | MessagePartRequest
    | DeliveryAcknowledgement
    | ReadRequest
    | ReceiptAcknowledgement
    | RefreshRequest
)

type ServerMessage = (
    AccountReply | QueryReply | SendStatusReply | DeliveryPart | ReadReply | ReceiptPush | RefreshReply | ErrorReply
)

type ParsedDirectMessage = (
    ClientMessage | ServerMessage | OtherText | UnsupportedVersion | UnknownMessageType | ProtocolSyntaxError
)
