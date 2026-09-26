"""Building the text of every direct message: the server's, and the client's for tests and shared vectors.

Every formatter checks its fields against the grammar and the value rules, so it only ever
builds a direct message that a strict parser accepts and a service would not reject for its
content. A test that needs an invalid direct message writes its text by hand.

Every formatter raises InvalidDirectMessageTextError before anything could reach the node if
its result is longer than 150 bytes of UTF-8, contains U+0000 or a lone surrogate, or does not
start with "HT1 ".
"""

from collections.abc import Sequence
from typing import assert_never

from protocol.constants import (
    DIRECT_MESSAGE_MAXIMUM_BYTES,
    MESSAGE_ID_MAXIMUM,
    MESSAGE_ID_MINIMUM,
    PROTOCOL_PREFIX,
    REFRESH_REPLY_MAXIMUM_MESSAGE_COUNT,
    USER_DOES_NOT_EXIST,
    USER_EXISTS,
    ClientMessageType,
    ErrorCode,
    ReceiptLevel,
    ServerMessageType,
)
from protocol.field_grammar import (
    FIELD_SEPARATOR,
    NUL_CHARACTER,
    PART_FIELD_SEPARATOR,
    is_error_code_field,
    is_error_reference_field,
    is_receipt_level_field,
    is_received_set_field,
    is_refresh_target_field,
    is_request_type_field,
)
from protocol.message_types import (
    AccountReply,
    AccountRequest,
    ClientMessage,
    DeliveryAcknowledgement,
    DeliveryPart,
    ErrorReply,
    MessagePartRequest,
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
)
from protocol.passwords import is_valid_password, normalize_password
from protocol.received_sets import is_valid_part_numbering
from protocol.text_validation import contains_surrogate, count_utf8_bytes, is_valid_part_text
from protocol.usernames import is_valid_username


class InvalidDirectMessageTextError(ValueError):
    """A formatted text breaks the byte budget, the grammar or a value rule; it is never sent."""


def format_protocol_message(message: ClientMessage | ServerMessage) -> str:
    match message:
        case (
            AccountRequest()
            | QueryRequest()
            | MessagePartRequest()
            | DeliveryAcknowledgement()
            | ReadRequest()
            | ReceiptAcknowledgement()
            | RefreshRequest()
        ):
            return format_client_message(message)
        case _:
            return format_server_message(message)


# ----------------------------------------------------------------------------------------------------------------------
# Server to client
# ----------------------------------------------------------------------------------------------------------------------


def format_server_message(server_message: ServerMessage) -> str:
    """Format any server message value; the typed formatters below do the work."""
    match server_message:
        case AccountReply():
            return format_account_reply(server_message.username)
        case QueryReply():
            return format_query_reply(server_message.username, server_message.user_exists)
        case SendStatusReply():
            return format_send_status_reply(
                server_message.recipient_username,
                server_message.message_id,
                server_message.received_set,
            )
        case DeliveryPart():
            return format_delivery_part(
                server_message.sender_username,
                server_message.message_id,
                server_message.part_number,
                server_message.part_count,
                server_message.part_text,
            )
        case ReadReply():
            return format_read_reply(server_message.sender_username, server_message.message_id)
        case ReceiptPush():
            return format_receipt_push(
                server_message.recipient_username,
                server_message.message_id,
                server_message.receipt_level,
            )
        case RefreshReply():
            return format_refresh_reply(server_message.refresh_target, server_message.message_count)
        case ErrorReply():
            return format_error_reply(
                server_message.error_code,
                server_message.request_type,
                server_message.error_reference,
            )
        case _:
            assert_never(server_message)


def format_account_reply(username: str) -> str:
    return build_direct_message_text(ServerMessageType.ACCOUNT_REPLY, [convert_username_to_field(username)])


def format_query_reply(username: str, user_exists: bool) -> str:
    existence = USER_EXISTS if user_exists else USER_DOES_NOT_EXIST
    return build_direct_message_text(ServerMessageType.QUERY_REPLY, [convert_username_to_field(username), existence])


def format_send_status_reply(recipient_username: str, message_id: int, received_set: str) -> str:
    return build_direct_message_text(
        ServerMessageType.SEND_STATUS_REPLY,
        [
            convert_username_to_field(recipient_username),
            convert_message_id_to_field(message_id),
            convert_received_set_to_field(received_set),
        ],
    )


def format_delivery_part(
    sender_username: str,
    message_id: int,
    part_number: int,
    part_count: int,
    part_text: str,
) -> str:
    return build_direct_message_text(
        ServerMessageType.DELIVERY_PART,
        [
            convert_username_to_field(sender_username),
            convert_message_id_to_field(message_id),
            convert_part_numbering_to_field(part_number, part_count),
            convert_part_text_to_field(part_text),
        ],
    )


def format_read_reply(sender_username: str, message_id: int) -> str:
    return build_direct_message_text(
        ServerMessageType.READ_REPLY,
        [convert_username_to_field(sender_username), convert_message_id_to_field(message_id)],
    )


def format_receipt_push(recipient_username: str, message_id: int, receipt_level: ReceiptLevel) -> str:
    return build_direct_message_text(
        ServerMessageType.RECEIPT_PUSH,
        [
            convert_username_to_field(recipient_username),
            convert_message_id_to_field(message_id),
            convert_receipt_level_to_field(receipt_level),
        ],
    )


def format_refresh_reply(refresh_target: str, message_count: int) -> str:
    """`message_count` above 9999 is sent as 9999 (protocol section 6.1)."""
    if message_count < 0:
        raise InvalidDirectMessageTextError(f"A refresh reply cannot announce {message_count} messages.")
    shown_message_count = min(message_count, REFRESH_REPLY_MAXIMUM_MESSAGE_COUNT)
    return build_direct_message_text(
        ServerMessageType.REFRESH_REPLY,
        [convert_refresh_target_to_field(refresh_target), str(shown_message_count)],
    )


def format_error_reply(error_code: ErrorCode | str, request_type: str, error_reference: str | None = None) -> str:
    if not is_error_code_field(error_code):
        raise InvalidDirectMessageTextError(f"Not an error code: {error_code!r}")
    if not is_request_type_field(request_type):
        raise InvalidDirectMessageTextError(f"Not a request type (an upper-case letter or '?'): {request_type!r}")
    fields = [str(error_code), request_type]
    if error_reference is not None:
        if not is_error_reference_field(error_reference):
            raise InvalidDirectMessageTextError(f"Not an error reference: {error_reference!r}")
        fields.append(error_reference)
    return build_direct_message_text(ServerMessageType.ERROR_REPLY, fields)


# ----------------------------------------------------------------------------------------------------------------------
# Client to server
# ----------------------------------------------------------------------------------------------------------------------


def format_client_message(client_message: ClientMessage) -> str:
    match client_message:
        case AccountRequest():
            return format_account_request(client_message.username, client_message.password)
        case QueryRequest():
            return format_query_request(client_message.username)
        case MessagePartRequest():
            return format_message_part_request(
                client_message.recipient_username,
                client_message.message_id,
                client_message.part_number,
                client_message.part_count,
                client_message.part_text,
            )
        case DeliveryAcknowledgement():
            return format_delivery_acknowledgement(
                client_message.sender_username,
                client_message.message_id,
                client_message.received_set,
            )
        case ReadRequest():
            return format_read_request(client_message.sender_username, client_message.message_id)
        case ReceiptAcknowledgement():
            return format_receipt_acknowledgement(
                client_message.recipient_username,
                client_message.message_id,
                client_message.receipt_level,
            )
        case RefreshRequest():
            return format_refresh_request(client_message.refresh_target)
        case _:
            assert_never(client_message)


def format_account_request(username: str, password: str) -> str:
    """The password is sent as given; a client should give it in NFC, the form the server checks."""
    if not is_valid_password(normalize_password(password)):
        raise InvalidDirectMessageTextError("The password breaks the password rules.")
    return build_direct_message_text(ClientMessageType.ACCOUNT_REQUEST, [convert_username_to_field(username), password])


def format_query_request(username: str) -> str:
    return build_direct_message_text(ClientMessageType.QUERY_REQUEST, [convert_username_to_field(username)])


def format_message_part_request(
    recipient_username: str,
    message_id: int,
    part_number: int,
    part_count: int,
    part_text: str,
) -> str:
    return build_direct_message_text(
        ClientMessageType.MESSAGE_PART_REQUEST,
        [
            convert_username_to_field(recipient_username),
            convert_message_id_to_field(message_id),
            convert_part_numbering_to_field(part_number, part_count),
            convert_part_text_to_field(part_text),
        ],
    )


def format_delivery_acknowledgement(sender_username: str, message_id: int, received_set: str) -> str:
    return build_direct_message_text(
        ClientMessageType.DELIVERY_ACKNOWLEDGEMENT,
        [
            convert_username_to_field(sender_username),
            convert_message_id_to_field(message_id),
            convert_received_set_to_field(received_set),
        ],
    )


def format_read_request(sender_username: str, message_id: int) -> str:
    return build_direct_message_text(
        ClientMessageType.READ_REQUEST,
        [convert_username_to_field(sender_username), convert_message_id_to_field(message_id)],
    )


def format_receipt_acknowledgement(recipient_username: str, message_id: int, receipt_level: ReceiptLevel) -> str:
    return build_direct_message_text(
        ClientMessageType.RECEIPT_ACKNOWLEDGEMENT,
        [
            convert_username_to_field(recipient_username),
            convert_message_id_to_field(message_id),
            convert_receipt_level_to_field(receipt_level),
        ],
    )


def format_refresh_request(refresh_target: str) -> str:
    return build_direct_message_text(
        ClientMessageType.REFRESH_REQUEST, [convert_refresh_target_to_field(refresh_target)]
    )


# ----------------------------------------------------------------------------------------------------------------------
# Fields and the finished text
# ----------------------------------------------------------------------------------------------------------------------


def convert_username_to_field(username: str) -> str:
    if not is_valid_username(username):
        raise InvalidDirectMessageTextError(f"Not a username (3 to 16 of A-Z, a-z, 0-9): {username!r}")
    return username


def convert_message_id_to_field(message_id: int) -> str:
    if not MESSAGE_ID_MINIMUM <= message_id <= MESSAGE_ID_MAXIMUM:
        raise InvalidDirectMessageTextError(f"A message id is 1 to 16 digits without a leading zero, not {message_id}.")
    return str(message_id)


def convert_part_numbering_to_field(part_number: int, part_count: int) -> str:
    if not is_valid_part_numbering(part_number, part_count):
        raise InvalidDirectMessageTextError(
            f"Part {part_number} of {part_count} is outside 1 <= number <= count <= 10."
        )
    return f"{part_number}{PART_FIELD_SEPARATOR}{part_count}"


def convert_part_text_to_field(part_text: str) -> str:
    if not is_valid_part_text(part_text):
        raise InvalidDirectMessageTextError(
            "A part text is 1 to 104 bytes of Unicode scalar values without control characters other than tab and "
            "line feed."
        )
    return part_text


def convert_received_set_to_field(received_set: str) -> str:
    if not is_received_set_field(received_set):
        raise InvalidDirectMessageTextError(f"A received-set is 1 to 10 of '0' and '1', not {received_set!r}.")
    return received_set


def convert_receipt_level_to_field(receipt_level: ReceiptLevel) -> str:
    receipt_level_field = str(receipt_level)
    if not is_receipt_level_field(receipt_level_field):
        raise InvalidDirectMessageTextError(f"Not a receipt level: {receipt_level_field!r}")
    return receipt_level_field


def convert_refresh_target_to_field(refresh_target: str) -> str:
    if not is_refresh_target_field(refresh_target):
        raise InvalidDirectMessageTextError(f"A refresh target is a username or '*', not {refresh_target!r}.")
    return refresh_target


def build_direct_message_text(message_type_letter: str, fields: Sequence[str]) -> str:
    direct_message_text = PROTOCOL_PREFIX + FIELD_SEPARATOR.join([message_type_letter, *fields])
    ensure_direct_message_text_is_sendable(direct_message_text)
    return direct_message_text


def ensure_direct_message_text_is_sendable(direct_message_text: str) -> None:
    """Raise InvalidDirectMessageTextError unless the text may go to the node as one HT1 direct message.

    The error never quotes the text, which may hold a password.
    """
    if not direct_message_text.startswith(PROTOCOL_PREFIX):
        raise InvalidDirectMessageTextError(f"A direct message starts with {PROTOCOL_PREFIX!r}.")
    if NUL_CHARACTER in direct_message_text:
        raise InvalidDirectMessageTextError("A direct message never contains U+0000: the firmware would cut it there.")
    if contains_surrogate(direct_message_text):
        raise InvalidDirectMessageTextError("A direct message never contains a lone surrogate: UTF-8 cannot encode it.")
    direct_message_bytes = count_utf8_bytes(direct_message_text)
    if direct_message_bytes > DIRECT_MESSAGE_MAXIMUM_BYTES:
        raise InvalidDirectMessageTextError(
            f"A direct message has at most {DIRECT_MESSAGE_MAXIMUM_BYTES} bytes of UTF-8, not {direct_message_bytes}."
        )
