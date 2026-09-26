"""The plain-language meaning of every direct message on the Traffic tab.

It decodes with the worker's own protocol.parsing, so the panel and the worker always agree on
what a text means.
"""

from protocol.message_types import (
    AccountReply,
    AccountRequest,
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
    UnknownMessageType,
    UnsupportedVersion,
)
from protocol.parsing import parse_direct_message_text

RECEIPT_LEVEL_WORDS = {"D": "delivered", "R": "read"}
EVERY_CONVERSATION_TARGET = "*"
SHORTENED_MESSAGE_ID_DIGITS = 3


def decode_direct_message_text(direct_message_text: str, contact_username: str = "") -> str:
    """contact_username is the user of the contact the text came from or went to, "" when it has none."""
    return describe_parsed_direct_message(parse_direct_message_text(direct_message_text), contact_username)


def describe_parsed_direct_message(parsed_direct_message: ParsedDirectMessage, contact_username: str) -> str:
    match parsed_direct_message:
        case AccountRequest(username=username):
            return f"sign in as {username}"
        case QueryRequest(username=username):
            return f"does {username} exist?"
        case MessagePartRequest() as message_part:
            return describe_message_part(
                message_part.part_number,
                message_part.part_count,
                sender_username=contact_username,
                recipient_username=message_part.recipient_username,
                message_id=message_part.message_id,
            )
        case DeliveryAcknowledgement(sender_username=sender_username, message_id=message_id, received_set=received):
            return f"delivery acknowledgement {received} for {sender_username} {shorten_message_id(message_id)}"
        case ReadRequest(sender_username=sender_username, message_id=message_id):
            return f"read {sender_username} {shorten_message_id(message_id)}"
        case ReceiptAcknowledgement() as receipt_acknowledgement:
            return (
                f"receipt acknowledgement ({RECEIPT_LEVEL_WORDS[receipt_acknowledgement.receipt_level]}) for "
                f"{receipt_acknowledgement.recipient_username} "
                f"{shorten_message_id(receipt_acknowledgement.message_id)}"
            )
        case RefreshRequest(refresh_target=refresh_target):
            return f"refresh {describe_refresh_target(refresh_target)}"
        case AccountReply(username=username):
            return f"signed in as {username}"
        case QueryReply(username=username, user_exists=user_exists):
            return f"{username} exists" if user_exists else f"{username} does not exist"
        case SendStatusReply(recipient_username=recipient_username, message_id=message_id, received_set=received):
            return f"send status {received} for {recipient_username} {shorten_message_id(message_id)}"
        case DeliveryPart() as delivery_part:
            return describe_message_part(
                delivery_part.part_number,
                delivery_part.part_count,
                sender_username=delivery_part.sender_username,
                recipient_username=contact_username,
                message_id=delivery_part.message_id,
            )
        case ReadReply(sender_username=sender_username, message_id=message_id):
            return f"read accepted for {sender_username} {shorten_message_id(message_id)}"
        case ReceiptPush() as receipt_push:
            return (
                f"receipt: {receipt_push.recipient_username} {shorten_message_id(receipt_push.message_id)} "
                f"was {RECEIPT_LEVEL_WORDS[receipt_push.receipt_level]}"
            )
        case RefreshReply(refresh_target=refresh_target, message_count=message_count):
            return f"refresh {describe_refresh_target(refresh_target)}: {describe_message_count(message_count)}"
        case ErrorReply() as error_reply:
            return describe_error_reply(error_reply)
        case OtherText():
            return "not protocol"
        case UnsupportedVersion(version=version):
            return f"protocol version {version}, not supported"
        case UnknownMessageType(message_type_letter=message_type_letter):
            return f"unknown message type {message_type_letter}"
        case ProtocolSyntaxError(request_type=request_type, correlation_fields=correlation_fields):
            return " ".join(["syntax error in", request_type, *correlation_fields])


def describe_message_part(
    part_number: int, part_count: int, *, sender_username: str, recipient_username: str, message_id: int
) -> str:
    sender_description = f"of {sender_username} " if sender_username else ""
    recipient_description = f"→ {recipient_username} " if recipient_username else ""
    return (
        f"message part {part_number}/{part_count} {sender_description}{recipient_description}"
        f"{shorten_message_id(message_id)}"
    )


def describe_refresh_target(refresh_target: str) -> str:
    if refresh_target == EVERY_CONVERSATION_TARGET:
        return "of every conversation"
    return f"of {refresh_target}"


def describe_message_count(message_count: int) -> str:
    if message_count == 1:
        return "1 message follows"
    return f"{message_count} messages follow"


def describe_error_reply(error_reply: ErrorReply) -> str:
    description = f"error {error_reply.error_code} answering {error_reply.request_type}"
    if error_reply.error_reference:
        return f"{description} {error_reply.error_reference}"
    return description


def shorten_message_id(message_id: int) -> str:
    """#…457: the last digits tell the messages of one conversation apart on screen."""
    message_id_text = str(message_id)
    if len(message_id_text) <= SHORTENED_MESSAGE_ID_DIGITS:
        return f"#{message_id_text}"
    return f"#…{message_id_text[-SHORTENED_MESSAGE_ID_DIGITS:]}"
