"""The error replies of protocol section 10: which error a text earns by itself, and the reference an error repeats.

The reference repeats a request's correlation fields as they were sent, so the client can find
its pending request: "<username>" for A and Q, "<peer> <id>" for M and R, "<peer>" or "*" for
F. It is present only when those fields were syntactically valid.
"""

from typing import assert_never

from protocol.constants import (
    REQUEST_TYPES,
    UNKNOWN_REQUEST_TYPE,
    ClientMessageType,
    ErrorCode,
)
from protocol.field_grammar import FIELD_SEPARATOR
from protocol.message_types import (
    AccountRequest,
    ErrorReply,
    MessagePartRequest,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    QueryRequest,
    ReadRequest,
    RefreshRequest,
    UnknownMessageType,
    UnsupportedVersion,
)

type AnswerableRequest = AccountRequest | QueryRequest | MessagePartRequest | ReadRequest | RefreshRequest

CORRELATION_FIELD_COUNTS_BY_REQUEST_TYPE: dict[str, int] = {
    ClientMessageType.ACCOUNT_REQUEST: 1,
    ClientMessageType.QUERY_REQUEST: 1,
    ClientMessageType.MESSAGE_PART_REQUEST: 2,
    ClientMessageType.READ_REQUEST: 2,
    ClientMessageType.REFRESH_REQUEST: 1,
}


def build_grammar_error_reply(parsed_direct_message: ParsedDirectMessage) -> ErrorReply | None:
    """Return the error a direct message earns by its text alone, before any service looks at it.

    That is "e SYNTAX" for a malformed request (or no letter after "HT1 "), "e VERSION ?
    <version>" for a request in another version, and "e UNSUPPORTED X" for an unknown
    upper-case letter X. Anything else gets None: a well-formed message goes to its service,
    and other text, acknowledgements, lower-case types and another version's non-requests are
    never answered.
    """
    match parsed_direct_message:
        case ProtocolSyntaxError():
            return build_syntax_error_reply(parsed_direct_message)
        case UnsupportedVersion():
            return build_version_error_reply(parsed_direct_message)
        case UnknownMessageType():
            return build_unsupported_type_error_reply(parsed_direct_message)
        case _:
            return None


def build_syntax_error_reply(syntax_error: ProtocolSyntaxError) -> ErrorReply | None:
    """None for a malformed acknowledgement or server type, which is dropped without an answer."""
    is_request = syntax_error.request_type in REQUEST_TYPES
    if not is_request and syntax_error.request_type != UNKNOWN_REQUEST_TYPE:
        return None
    return ErrorReply(
        error_code=ErrorCode.SYNTAX,
        request_type=syntax_error.request_type,
        error_reference=build_syntax_error_reference(syntax_error),
    )


def build_syntax_error_reference(syntax_error: ProtocolSyntaxError) -> str | None:
    """Every correlation field of the request type must have parsed, or the reference is left out."""
    required_field_count = CORRELATION_FIELD_COUNTS_BY_REQUEST_TYPE.get(syntax_error.request_type)
    if required_field_count is None or len(syntax_error.correlation_fields) < required_field_count:
        return None
    return FIELD_SEPARATOR.join(syntax_error.correlation_fields[:required_field_count])


def build_version_error_reply(unsupported_version: UnsupportedVersion) -> ErrorReply | None:
    """Only a request, an upper-case letter after "HT<version> ", is answered: two servers never answer each other."""
    if not is_upper_case_type_letter(unsupported_version.next_character):
        return None
    return ErrorReply(
        error_code=ErrorCode.VERSION,
        request_type=UNKNOWN_REQUEST_TYPE,
        error_reference=unsupported_version.version,
    )


def build_unsupported_type_error_reply(unknown_message_type: UnknownMessageType) -> ErrorReply | None:
    """A lower-case letter the parser does not know is a newer server type, which is never answered."""
    if not is_upper_case_type_letter(unknown_message_type.message_type_letter):
        return None
    return ErrorReply(error_code=ErrorCode.UNSUPPORTED, request_type=unknown_message_type.message_type_letter)


def build_request_error_reply(error_code: ErrorCode, request: AnswerableRequest) -> ErrorReply:
    """The error a service answers for a well-formed request, with the request's reference as sent."""
    return ErrorReply(
        error_code=error_code,
        request_type=request.message_type,
        error_reference=build_request_error_reference(request),
    )


def build_request_error_reference(request: AnswerableRequest) -> str:
    match request:
        case AccountRequest() | QueryRequest():
            return request.username
        case MessagePartRequest():
            return f"{request.recipient_username}{FIELD_SEPARATOR}{request.message_id}"
        case ReadRequest():
            return f"{request.sender_username}{FIELD_SEPARATOR}{request.message_id}"
        case RefreshRequest():
            return request.refresh_target
        case _:
            assert_never(request)


def is_upper_case_type_letter(character: str) -> bool:
    return len(character) == 1 and "A" <= character <= "Z"
