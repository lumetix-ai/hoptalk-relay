"""Parsing a received direct message text into one value of protocol.message_types."""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from protocol.constants import (
    PROTOCOL_TRAFFIC_PATTERN,
    PROTOCOL_VERSION,
    UNKNOWN_REQUEST_TYPE,
    USER_EXISTS,
    ClientMessageType,
    ErrorCode,
    ReceiptLevel,
    ServerMessageType,
)
from protocol.field_grammar import (
    FIELD_SEPARATOR,
    is_error_code_field,
    is_error_reference_field,
    is_existence_field,
    is_message_count_field,
    is_message_id_field,
    is_part_field,
    is_receipt_level_field,
    is_received_set_field,
    is_refresh_target_field,
    is_request_type_field,
    is_tail_field,
    read_part_field,
)
from protocol.message_types import (
    AccountReply,
    AccountRequest,
    ClientMessage,
    DeliveryAcknowledgement,
    DeliveryPart,
    ErrorReply,
    MessagePartRequest,
    OtherText,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    QueryReply,
    QueryRequest,
    ReadReply,
    ReadRequest,
    ReceiptAcknowledgement,
    ReceiptPush,
    RefreshReply,
    RefreshRequest,
    SendStatusReply,
    ServerMessage,
    UnknownMessageType,
    UnsupportedVersion,
)
from protocol.usernames import is_valid_username

PROTOCOL_TRAFFIC_REGULAR_EXPRESSION = re.compile(PROTOCOL_TRAFFIC_PATTERN)
MESSAGE_TYPE_LETTER_REGULAR_EXPRESSION = re.compile(r"[A-Za-z]")
ERROR_REPLY_FIXED_FIELD_COUNT = 2
ERROR_REFERENCE_MAXIMUM_FIELD_COUNT = 2


@dataclass(frozen=True, kw_only=True)
class HeaderField:
    matches_grammar: Callable[[str], bool]
    # Usernames, message ids and refresh targets: an "e SYNTAX" reply repeats them when they parsed.
    is_correlation_field: bool


USERNAME_FIELD = HeaderField(matches_grammar=is_valid_username, is_correlation_field=True)
MESSAGE_ID_FIELD = HeaderField(matches_grammar=is_message_id_field, is_correlation_field=True)
REFRESH_TARGET_FIELD = HeaderField(matches_grammar=is_refresh_target_field, is_correlation_field=True)
PART_FIELD = HeaderField(matches_grammar=is_part_field, is_correlation_field=False)
RECEIVED_SET_FIELD = HeaderField(matches_grammar=is_received_set_field, is_correlation_field=False)
RECEIPT_LEVEL_FIELD = HeaderField(matches_grammar=is_receipt_level_field, is_correlation_field=False)
EXISTENCE_FIELD = HeaderField(matches_grammar=is_existence_field, is_correlation_field=False)
MESSAGE_COUNT_FIELD = HeaderField(matches_grammar=is_message_count_field, is_correlation_field=False)


@dataclass(frozen=True, kw_only=True)
class MessageLayout:
    """The fields after "HT1 <letter> ", in order.

    A layout with a tail (a password or part text) takes everything after its last header
    field verbatim. `build_message` receives the field values, the tail last, once every one
    of them matched the grammar.
    """

    header_fields: tuple[HeaderField, ...]
    has_tail: bool
    build_message: Callable[[Sequence[str]], ClientMessage | ServerMessage]


def parse_direct_message_text(direct_message_text: str) -> ParsedDirectMessage:
    """Classify and parse any direct message text, never raising for bad input.

    Grammar first, strictly (protocol section 4): a text that fails "^HT[0-9]{1,3} " is
    OtherText; another version is UnsupportedVersion; a letter that is no known type is
    UnknownMessageType; anything else that breaks the grammar, usernames included, is
    ProtocolSyntaxError with the correlation fields that did parse. Every other text becomes
    the message type's dataclass, client and server types alike, with its tail (password or
    part text) verbatim. Value rules are not checked here (see protocol.message_types).
    """
    protocol_traffic_match = PROTOCOL_TRAFFIC_REGULAR_EXPRESSION.match(direct_message_text)
    if protocol_traffic_match is None:
        return OtherText(text=direct_message_text)

    version = protocol_traffic_match.group(1)
    message_body = direct_message_text[protocol_traffic_match.end() :]
    if version != PROTOCOL_VERSION:
        return UnsupportedVersion(version=version, next_character=message_body[:1])
    return parse_version_one_message_body(message_body)


def parse_version_one_message_body(message_body: str) -> ParsedDirectMessage:
    """Parse what follows "HT1 ": the type letter, one space, then the type's fields."""
    message_type_letter = message_body[:1]
    if MESSAGE_TYPE_LETTER_REGULAR_EXPRESSION.fullmatch(message_type_letter) is None:
        return ProtocolSyntaxError(request_type=UNKNOWN_REQUEST_TYPE)

    is_error_reply = message_type_letter == ServerMessageType.ERROR_REPLY
    if not is_error_reply and message_type_letter not in MESSAGE_LAYOUTS_BY_TYPE_LETTER:
        return UnknownMessageType(message_type_letter=message_type_letter)

    letter_and_separator = message_body[:2]
    if letter_and_separator != message_type_letter + FIELD_SEPARATOR:
        return ProtocolSyntaxError(request_type=message_type_letter)

    fields_text = message_body[2:]
    if is_error_reply:
        return parse_error_reply_fields(fields_text)
    return parse_fields_by_layout(message_type_letter, fields_text, MESSAGE_LAYOUTS_BY_TYPE_LETTER[message_type_letter])


def parse_fields_by_layout(
    message_type_letter: str,
    fields_text: str,
    message_layout: MessageLayout,
) -> ParsedDirectMessage:
    header_field_count = len(message_layout.header_fields)
    if message_layout.has_tail:
        field_values = fields_text.split(FIELD_SEPARATOR, header_field_count)
        expected_field_count = header_field_count + 1
    else:
        field_values = fields_text.split(FIELD_SEPARATOR)
        expected_field_count = header_field_count

    correlation_fields: list[str] = []
    for header_field, field_value in zip(message_layout.header_fields, field_values, strict=False):
        if not header_field.matches_grammar(field_value):
            return ProtocolSyntaxError(request_type=message_type_letter, correlation_fields=tuple(correlation_fields))
        if header_field.is_correlation_field:
            correlation_fields.append(field_value)

    if len(field_values) != expected_field_count:
        return ProtocolSyntaxError(request_type=message_type_letter, correlation_fields=tuple(correlation_fields))
    if message_layout.has_tail and not is_tail_field(field_values[-1]):
        return ProtocolSyntaxError(request_type=message_type_letter, correlation_fields=tuple(correlation_fields))
    return message_layout.build_message(field_values)


def parse_error_reply_fields(fields_text: str) -> ErrorReply | ProtocolSyntaxError:
    """Parse "<code> <request-type> [<reference>]", where the reference is one or two fields."""
    field_values = fields_text.split(FIELD_SEPARATOR)
    syntax_error = ProtocolSyntaxError(request_type=ServerMessageType.ERROR_REPLY)
    maximum_field_count = ERROR_REPLY_FIXED_FIELD_COUNT + ERROR_REFERENCE_MAXIMUM_FIELD_COUNT
    if not ERROR_REPLY_FIXED_FIELD_COUNT <= len(field_values) <= maximum_field_count:
        return syntax_error

    error_code, request_type, *reference_fields = field_values
    if not is_error_code_field(error_code) or not is_request_type_field(request_type):
        return syntax_error
    error_reference = FIELD_SEPARATOR.join(reference_fields) if reference_fields else None
    if error_reference is not None and not is_error_reference_field(error_reference):
        return syntax_error
    return ErrorReply(
        error_code=read_error_code(error_code),
        request_type=request_type,
        error_reference=error_reference,
    )


def read_error_code(error_code: str) -> ErrorCode | str:
    """A code this version does not know (from a newer server) stays a plain string."""
    if error_code in ErrorCode:
        return ErrorCode(error_code)
    return error_code


# ----------------------------------------------------------------------------------------------------------------------
# Building a message from field values that matched the grammar
# ----------------------------------------------------------------------------------------------------------------------


def build_account_request(field_values: Sequence[str]) -> AccountRequest:
    username, password = field_values
    return AccountRequest(username=username, password=password)


def build_query_request(field_values: Sequence[str]) -> QueryRequest:
    (username,) = field_values
    return QueryRequest(username=username)


def build_message_part_request(field_values: Sequence[str]) -> MessagePartRequest:
    recipient_username, message_id, part_field, part_text = field_values
    part_number, part_count = read_part_field(part_field)
    return MessagePartRequest(
        recipient_username=recipient_username,
        message_id=int(message_id),
        part_number=part_number,
        part_count=part_count,
        part_text=part_text,
    )


def build_delivery_acknowledgement(field_values: Sequence[str]) -> DeliveryAcknowledgement:
    sender_username, message_id, received_set = field_values
    return DeliveryAcknowledgement(
        sender_username=sender_username, message_id=int(message_id), received_set=received_set
    )


def build_read_request(field_values: Sequence[str]) -> ReadRequest:
    sender_username, message_id = field_values
    return ReadRequest(sender_username=sender_username, message_id=int(message_id))


def build_receipt_acknowledgement(field_values: Sequence[str]) -> ReceiptAcknowledgement:
    recipient_username, message_id, receipt_level = field_values
    return ReceiptAcknowledgement(
        recipient_username=recipient_username,
        message_id=int(message_id),
        receipt_level=ReceiptLevel(receipt_level),
    )


def build_refresh_request(field_values: Sequence[str]) -> RefreshRequest:
    (refresh_target,) = field_values
    return RefreshRequest(refresh_target=refresh_target)


def build_account_reply(field_values: Sequence[str]) -> AccountReply:
    (username,) = field_values
    return AccountReply(username=username)


def build_query_reply(field_values: Sequence[str]) -> QueryReply:
    username, existence = field_values
    return QueryReply(username=username, user_exists=existence == USER_EXISTS)


def build_send_status_reply(field_values: Sequence[str]) -> SendStatusReply:
    recipient_username, message_id, received_set = field_values
    return SendStatusReply(recipient_username=recipient_username, message_id=int(message_id), received_set=received_set)


def build_delivery_part(field_values: Sequence[str]) -> DeliveryPart:
    sender_username, message_id, part_field, part_text = field_values
    part_number, part_count = read_part_field(part_field)
    return DeliveryPart(
        sender_username=sender_username,
        message_id=int(message_id),
        part_number=part_number,
        part_count=part_count,
        part_text=part_text,
    )


def build_read_reply(field_values: Sequence[str]) -> ReadReply:
    sender_username, message_id = field_values
    return ReadReply(sender_username=sender_username, message_id=int(message_id))


def build_receipt_push(field_values: Sequence[str]) -> ReceiptPush:
    recipient_username, message_id, receipt_level = field_values
    return ReceiptPush(
        recipient_username=recipient_username,
        message_id=int(message_id),
        receipt_level=ReceiptLevel(receipt_level),
    )


def build_refresh_reply(field_values: Sequence[str]) -> RefreshReply:
    refresh_target, message_count = field_values
    return RefreshReply(refresh_target=refresh_target, message_count=int(message_count))


# The error reply ("e") is parsed by parse_error_reply_fields(): its reference has a variable shape.
MESSAGE_LAYOUTS_BY_TYPE_LETTER: dict[str, MessageLayout] = {
    ClientMessageType.ACCOUNT_REQUEST: MessageLayout(
        header_fields=(USERNAME_FIELD,),
        has_tail=True,
        build_message=build_account_request,
    ),
    ClientMessageType.QUERY_REQUEST: MessageLayout(
        header_fields=(USERNAME_FIELD,),
        has_tail=False,
        build_message=build_query_request,
    ),
    ClientMessageType.MESSAGE_PART_REQUEST: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD, PART_FIELD),
        has_tail=True,
        build_message=build_message_part_request,
    ),
    ClientMessageType.DELIVERY_ACKNOWLEDGEMENT: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD, RECEIVED_SET_FIELD),
        has_tail=False,
        build_message=build_delivery_acknowledgement,
    ),
    ClientMessageType.READ_REQUEST: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD),
        has_tail=False,
        build_message=build_read_request,
    ),
    ClientMessageType.RECEIPT_ACKNOWLEDGEMENT: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD, RECEIPT_LEVEL_FIELD),
        has_tail=False,
        build_message=build_receipt_acknowledgement,
    ),
    ClientMessageType.REFRESH_REQUEST: MessageLayout(
        header_fields=(REFRESH_TARGET_FIELD,),
        has_tail=False,
        build_message=build_refresh_request,
    ),
    ServerMessageType.ACCOUNT_REPLY: MessageLayout(
        header_fields=(USERNAME_FIELD,),
        has_tail=False,
        build_message=build_account_reply,
    ),
    ServerMessageType.QUERY_REPLY: MessageLayout(
        header_fields=(USERNAME_FIELD, EXISTENCE_FIELD),
        has_tail=False,
        build_message=build_query_reply,
    ),
    ServerMessageType.SEND_STATUS_REPLY: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD, RECEIVED_SET_FIELD),
        has_tail=False,
        build_message=build_send_status_reply,
    ),
    ServerMessageType.DELIVERY_PART: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD, PART_FIELD),
        has_tail=True,
        build_message=build_delivery_part,
    ),
    ServerMessageType.READ_REPLY: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD),
        has_tail=False,
        build_message=build_read_reply,
    ),
    ServerMessageType.RECEIPT_PUSH: MessageLayout(
        header_fields=(USERNAME_FIELD, MESSAGE_ID_FIELD, RECEIPT_LEVEL_FIELD),
        has_tail=False,
        build_message=build_receipt_push,
    ),
    ServerMessageType.REFRESH_REPLY: MessageLayout(
        header_fields=(REFRESH_TARGET_FIELD, MESSAGE_COUNT_FIELD),
        has_tail=False,
        build_message=build_refresh_reply,
    ),
}
