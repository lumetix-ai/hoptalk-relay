import pytest

from protocol.constants import ErrorCode, ReceiptLevel
from protocol.error_replies import (
    AnswerableRequest,
    build_grammar_error_reply,
    build_request_error_reply,
    build_syntax_error_reply,
)
from protocol.formatting import format_server_message
from protocol.message_types import (
    AccountReply,
    AccountRequest,
    DeliveryAcknowledgement,
    MessagePartRequest,
    OtherText,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    QueryRequest,
    ReadRequest,
    ReceiptAcknowledgement,
    RefreshRequest,
)


@pytest.mark.parametrize(
    ("error_code", "answerable_request", "expected_error_reply_text"),
    [
        (ErrorCode.WRONG_PASSWORD, AccountRequest(username="IVAN", password="x"), "HT1 e WRONG_PASSWORD A IVAN"),
        (ErrorCode.NOT_SIGNED_IN, QueryRequest(username="bob"), "HT1 e NOT_SIGNED_IN Q bob"),
        (
            ErrorCode.NO_SUCH_USER,
            MessagePartRequest(
                recipient_username="carol",
                message_id=1_790_294_400_123_457,
                part_number=1,
                part_count=1,
                part_text="x",
            ),
            "HT1 e NO_SUCH_USER M carol 1790294400123457",
        ),
        (ErrorCode.NOT_FOUND, ReadRequest(sender_username="ivan", message_id=5), "HT1 e NOT_FOUND R ivan 5"),
        (ErrorCode.SELF, RefreshRequest(refresh_target="ivan"), "HT1 e SELF F ivan"),
        (ErrorCode.NOT_SIGNED_IN, RefreshRequest(refresh_target="*"), "HT1 e NOT_SIGNED_IN F *"),
    ],
)
def test_a_service_error_repeats_the_request_fields_as_sent(
    error_code: ErrorCode,
    answerable_request: AnswerableRequest,
    expected_error_reply_text: str,
) -> None:
    assert format_server_message(build_request_error_reply(error_code, answerable_request)) == expected_error_reply_text


@pytest.mark.parametrize(
    "parsed_direct_message",
    [
        OtherText(text="hello"),
        AccountReply(username="ivan"),
        DeliveryAcknowledgement(sender_username="ivan", message_id=5, received_set="1"),
        ReceiptAcknowledgement(recipient_username="Bob", message_id=5, receipt_level=ReceiptLevel.READ),
        ProtocolSyntaxError(request_type="K", correlation_fields=("ivan", "5")),
        ProtocolSyntaxError(request_type="C"),
        ProtocolSyntaxError(request_type="m", correlation_fields=("ivan",)),
        ProtocolSyntaxError(request_type="e"),
    ],
)
def test_other_text_acknowledgements_and_server_types_never_earn_an_error_reply(
    parsed_direct_message: ParsedDirectMessage,
) -> None:
    assert build_grammar_error_reply(parsed_direct_message) is None


def test_a_syntax_error_reference_keeps_only_the_correlation_fields_of_the_request_type() -> None:
    syntax_error = ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5"))

    error_reply = build_syntax_error_reply(syntax_error)

    assert error_reply is not None
    assert format_server_message(error_reply) == "HT1 e SYNTAX M Bob 5"


def test_a_syntax_error_without_every_correlation_field_has_no_reference() -> None:
    error_reply = build_syntax_error_reply(ProtocolSyntaxError(request_type="R", correlation_fields=("ivan",)))

    assert error_reply is not None
    assert format_server_message(error_reply) == "HT1 e SYNTAX R"
