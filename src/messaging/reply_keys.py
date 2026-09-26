"""Reply keys: which request a reply answers, so a newer reply to the same request replaces an older one.

"A:<username>", "Q:<username>", "M:<peer>:<id>", "R:<peer>:<id>", "F:<peer or *>", with the
username lower-cased, since usernames are compared case-insensitively. A reply that carries no
reference ("e SYNTAX ?", "e UNSUPPORTED X", "e VERSION ? <version>") gets "?:<inbox row id>".
"""

from protocol.constants import REQUEST_TYPES, UNKNOWN_REQUEST_TYPE
from protocol.error_replies import (
    CORRELATION_FIELD_COUNTS_BY_REQUEST_TYPE,
    AnswerableRequest,
    is_upper_case_type_letter,
)
from protocol.message_types import (
    AccountRequest,
    MessagePartRequest,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    QueryRequest,
    ReadRequest,
    RefreshRequest,
    UnknownMessageType,
    UnsupportedVersion,
)
from protocol.usernames import normalize_username_for_lookup

REPLY_KEY_SEPARATOR = ":"


def build_request_reply_key(request: AnswerableRequest) -> str:
    match request:
        case AccountRequest() | QueryRequest():
            key_fields = [normalize_username_for_lookup(request.username)]
        case MessagePartRequest():
            key_fields = [normalize_username_for_lookup(request.recipient_username), str(request.message_id)]
        case ReadRequest():
            key_fields = [normalize_username_for_lookup(request.sender_username), str(request.message_id)]
        case RefreshRequest():
            key_fields = [normalize_username_for_lookup(request.refresh_target)]
    return REPLY_KEY_SEPARATOR.join([request.message_type, *key_fields])


def build_unreferenced_reply_key(inbox_row_id: int) -> str:
    return f"{UNKNOWN_REQUEST_TYPE}{REPLY_KEY_SEPARATOR}{inbox_row_id}"


def build_syntax_error_reply_key(syntax_error: ProtocolSyntaxError, inbox_row_id: int) -> str:
    """The answered request's key when the reference parsed, so the reply to a corrected retry replaces it."""
    required_field_count = CORRELATION_FIELD_COUNTS_BY_REQUEST_TYPE.get(syntax_error.request_type)
    if required_field_count is None or len(syntax_error.correlation_fields) < required_field_count:
        return build_unreferenced_reply_key(inbox_row_id)
    key_fields = [field.lower() for field in syntax_error.correlation_fields[:required_field_count]]
    return REPLY_KEY_SEPARATOR.join([syntax_error.request_type, *key_fields])


def build_reply_key_for_direct_message(parsed_direct_message: ParsedDirectMessage, inbox_row_id: int) -> str | None:
    """The key of the reply a received direct message earns, or None when the server never answers it."""
    match parsed_direct_message:
        case AccountRequest() | QueryRequest() | MessagePartRequest() | ReadRequest() | RefreshRequest():
            return build_request_reply_key(parsed_direct_message)
        case ProtocolSyntaxError():
            if parsed_direct_message.request_type in REQUEST_TYPES:
                return build_syntax_error_reply_key(parsed_direct_message, inbox_row_id)
            if parsed_direct_message.request_type == UNKNOWN_REQUEST_TYPE:
                return build_unreferenced_reply_key(inbox_row_id)
            return None
        case UnsupportedVersion():
            if is_upper_case_type_letter(parsed_direct_message.next_character):
                return build_unreferenced_reply_key(inbox_row_id)
            return None
        case UnknownMessageType():
            if is_upper_case_type_letter(parsed_direct_message.message_type_letter):
                return build_unreferenced_reply_key(inbox_row_id)
            return None
        case _:
            return None
