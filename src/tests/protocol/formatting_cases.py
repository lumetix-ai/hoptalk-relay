"""Message values and the exact direct message text they format to; exported as vectors/formatting.json.

The byte counts are the examples and the worst cases of the protocol's message tables, typed
in by hand rather than computed, so a formatter that drifts from the specification fails.
"""

from dataclasses import dataclass

from protocol.constants import ErrorCode, ReceiptLevel
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
from tests.protocol.splitting_cases import GRINNING_FACE


@dataclass(frozen=True, kw_only=True)
class FormattingCase:
    """`expected_text` None: the formatters must refuse the message."""

    description: str
    message: ClientMessage | ServerMessage
    expected_text: str | None
    expected_utf8_byte_length: int | None = None
    # False where formatting changes a value, so parsing the text gives another value back.
    parses_back_to_the_same_message: bool = True


LONGEST_USERNAME = "KonstantinIvanov"
ANOTHER_LONGEST_USERNAME = "AlexandraPetrova"
LONGEST_MESSAGE_ID = 9_999_999_999_999_999
EXAMPLE_MESSAGE_ID = 1_790_294_400_123_456
LONGEST_ASCII_PASSWORD = "correct horse battery staple, correct horse battery staple, abcd"
LONGEST_CYRILLIC_PASSWORD = "пароль" * 5 + "да"
LONGEST_ASCII_PART_TEXT = (
    "The longest part text is 104 bytes of UTF-8, whatever the two usernames are, so any M part fits as an m."
)
LONGEST_CYRILLIC_PART_TEXT = "Я" * 52

VALID_FORMATTING_CASES = (
    # ----- client to server: the examples and the worst cases of the client message table -----
    FormattingCase(
        description="A: register or sign in, with spaces inside the password",
        message=AccountRequest(username="ivan", password="correct horse battery"),
        expected_text="HT1 A ivan correct horse battery",
        expected_utf8_byte_length=32,
    ),
    FormattingCase(
        description="A, worst case: a 16-character username and a 64-byte password",
        message=AccountRequest(username=LONGEST_USERNAME, password=LONGEST_ASCII_PASSWORD),
        expected_text=f"HT1 A {LONGEST_USERNAME} {LONGEST_ASCII_PASSWORD}",
        expected_utf8_byte_length=87,
    ),
    FormattingCase(
        description="A, worst case with 32 Cyrillic letters, the longest Cyrillic password (64 bytes)",
        message=AccountRequest(username=LONGEST_USERNAME, password=LONGEST_CYRILLIC_PASSWORD),
        expected_text=f"HT1 A {LONGEST_USERNAME} {LONGEST_CYRILLIC_PASSWORD}",
        expected_utf8_byte_length=87,
    ),
    FormattingCase(
        description="Q: does this user exist?",
        message=QueryRequest(username="bob"),
        expected_text="HT1 Q bob",
        expected_utf8_byte_length=9,
    ),
    FormattingCase(
        description="Q, worst case",
        message=QueryRequest(username=LONGEST_USERNAME),
        expected_text=f"HT1 Q {LONGEST_USERNAME}",
        expected_utf8_byte_length=22,
    ),
    FormattingCase(
        description="M: one part of a message with Cyrillic text",
        message=MessagePartRequest(
            recipient_username="Bob",
            message_id=EXAMPLE_MESSAGE_ID,
            part_number=1,
            part_count=1,
            part_text="Привет, Боб!",
        ),
        expected_text="HT1 M Bob 1790294400123456 1/1 Привет, Боб!",
        expected_utf8_byte_length=52,
    ),
    FormattingCase(
        description="M, worst case: 46-byte header (16-character username, 16-digit id, 10/10) and 104 bytes of text",
        message=MessagePartRequest(
            recipient_username=LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            part_number=10,
            part_count=10,
            part_text=LONGEST_ASCII_PART_TEXT,
        ),
        expected_text=f"HT1 M {LONGEST_USERNAME} 9999999999999999 10/10 {LONGEST_ASCII_PART_TEXT}",
        expected_utf8_byte_length=150,
    ),
    FormattingCase(
        description="M, worst case with 52 Cyrillic letters (104 bytes)",
        message=MessagePartRequest(
            recipient_username=LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            part_number=10,
            part_count=10,
            part_text=LONGEST_CYRILLIC_PART_TEXT,
        ),
        expected_text=f"HT1 M {LONGEST_USERNAME} 9999999999999999 10/10 {LONGEST_CYRILLIC_PART_TEXT}",
        expected_utf8_byte_length=150,
    ),
    FormattingCase(
        description="M: the part text keeps its trailing space verbatim",
        message=MessagePartRequest(
            recipient_username="bob",
            message_id=2,
            part_number=1,
            part_count=2,
            part_text="Hello ",
        ),
        expected_text="HT1 M bob 2 1/2 Hello ",
        expected_utf8_byte_length=22,
    ),
    FormattingCase(
        description="K: delivery acknowledgement of a single-part message",
        message=DeliveryAcknowledgement(sender_username="ivan", message_id=EXAMPLE_MESSAGE_ID, received_set="1"),
        expected_text="HT1 K ivan 1790294400123456 1",
        expected_utf8_byte_length=29,
    ),
    FormattingCase(
        description="K, worst case: a 10-part received-set",
        message=DeliveryAcknowledgement(
            sender_username=LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            received_set="1111111111",
        ),
        expected_text=f"HT1 K {LONGEST_USERNAME} 9999999999999999 1111111111",
        expected_utf8_byte_length=50,
    ),
    FormattingCase(
        description="R: I have read this message",
        message=ReadRequest(sender_username="ivan", message_id=EXAMPLE_MESSAGE_ID),
        expected_text="HT1 R ivan 1790294400123456",
        expected_utf8_byte_length=27,
    ),
    FormattingCase(
        description="R, worst case",
        message=ReadRequest(sender_username=LONGEST_USERNAME, message_id=LONGEST_MESSAGE_ID),
        expected_text=f"HT1 R {LONGEST_USERNAME} 9999999999999999",
        expected_utf8_byte_length=39,
    ),
    FormattingCase(
        description="C: receipt acknowledgement of a delivered receipt",
        message=ReceiptAcknowledgement(
            recipient_username="Bob",
            message_id=EXAMPLE_MESSAGE_ID,
            receipt_level=ReceiptLevel.DELIVERED,
        ),
        expected_text="HT1 C Bob 1790294400123456 D",
        expected_utf8_byte_length=28,
    ),
    FormattingCase(
        description="C, worst case",
        message=ReceiptAcknowledgement(
            recipient_username=LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            receipt_level=ReceiptLevel.READ,
        ),
        expected_text=f"HT1 C {LONGEST_USERNAME} 9999999999999999 R",
        expected_utf8_byte_length=41,
    ),
    FormattingCase(
        description="F: refresh one conversation",
        message=RefreshRequest(refresh_target="ivan"),
        expected_text="HT1 F ivan",
        expected_utf8_byte_length=10,
    ),
    FormattingCase(
        description="F *: refresh every conversation",
        message=RefreshRequest(refresh_target="*"),
        expected_text="HT1 F *",
        expected_utf8_byte_length=7,
    ),
    FormattingCase(
        description="F, worst case",
        message=RefreshRequest(refresh_target=LONGEST_USERNAME),
        expected_text=f"HT1 F {LONGEST_USERNAME}",
        expected_utf8_byte_length=22,
    ),
    # ----- server to client: the examples and the worst cases of the server message table -----
    FormattingCase(
        description="a: signed in",
        message=AccountReply(username="ivan"),
        expected_text="HT1 a ivan",
        expected_utf8_byte_length=10,
    ),
    FormattingCase(
        description="a, worst case",
        message=AccountReply(username=LONGEST_USERNAME),
        expected_text=f"HT1 a {LONGEST_USERNAME}",
        expected_utf8_byte_length=22,
    ),
    FormattingCase(
        description="q: the user exists",
        message=QueryReply(username="Bob", user_exists=True),
        expected_text="HT1 q Bob 1",
        expected_utf8_byte_length=11,
    ),
    FormattingCase(
        description="q: the user does not exist",
        message=QueryReply(username="carol", user_exists=False),
        expected_text="HT1 q carol 0",
        expected_utf8_byte_length=13,
    ),
    FormattingCase(
        description="q, worst case",
        message=QueryReply(username=LONGEST_USERNAME, user_exists=True),
        expected_text=f"HT1 q {LONGEST_USERNAME} 1",
        expected_utf8_byte_length=24,
    ),
    FormattingCase(
        description="k: the server holds the whole single-part message",
        message=SendStatusReply(recipient_username="Bob", message_id=EXAMPLE_MESSAGE_ID, received_set="1"),
        expected_text="HT1 k Bob 1790294400123456 1",
        expected_utf8_byte_length=28,
    ),
    FormattingCase(
        description="k: the server lacks part 2 of 3",
        message=SendStatusReply(recipient_username="Bob", message_id=1_790_294_400_123_457, received_set="101"),
        expected_text="HT1 k Bob 1790294400123457 101",
        expected_utf8_byte_length=30,
    ),
    FormattingCase(
        description="k, worst case",
        message=SendStatusReply(
            recipient_username=LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            received_set="1111111111",
        ),
        expected_text=f"HT1 k {LONGEST_USERNAME} 9999999999999999 1111111111",
        expected_utf8_byte_length=50,
    ),
    FormattingCase(
        description="m: one part of a delivered message, the sender's bytes unchanged",
        message=DeliveryPart(
            sender_username="ivan",
            message_id=EXAMPLE_MESSAGE_ID,
            part_number=1,
            part_count=1,
            part_text="Привет, Боб!",
        ),
        expected_text="HT1 m ivan 1790294400123456 1/1 Привет, Боб!",
        expected_utf8_byte_length=53,
    ),
    FormattingCase(
        description="m, worst case: 46-byte header and 104 bytes of text",
        message=DeliveryPart(
            sender_username=ANOTHER_LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            part_number=10,
            part_count=10,
            part_text=LONGEST_ASCII_PART_TEXT,
        ),
        expected_text=f"HT1 m {ANOTHER_LONGEST_USERNAME} 9999999999999999 10/10 {LONGEST_ASCII_PART_TEXT}",
        expected_utf8_byte_length=150,
    ),
    FormattingCase(
        description="m, worst case with 26 four-byte emoji (104 bytes)",
        message=DeliveryPart(
            sender_username=ANOTHER_LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            part_number=10,
            part_count=10,
            part_text=GRINNING_FACE * 26,
        ),
        expected_text=f"HT1 m {ANOTHER_LONGEST_USERNAME} 9999999999999999 10/10 {GRINNING_FACE * 26}",
        expected_utf8_byte_length=150,
    ),
    FormattingCase(
        description="m: tabs, line feeds and leading and trailing spaces are forwarded verbatim",
        message=DeliveryPart(
            sender_username="alice",
            message_id=2,
            part_number=2,
            part_count=2,
            part_text=" line one\n\tline two ",
        ),
        expected_text="HT1 m alice 2 2/2  line one\n\tline two ",
        expected_utf8_byte_length=38,
    ),
    FormattingCase(
        description="r: read accepted",
        message=ReadReply(sender_username="ivan", message_id=EXAMPLE_MESSAGE_ID),
        expected_text="HT1 r ivan 1790294400123456",
        expected_utf8_byte_length=27,
    ),
    FormattingCase(
        description="r, worst case",
        message=ReadReply(sender_username=LONGEST_USERNAME, message_id=LONGEST_MESSAGE_ID),
        expected_text=f"HT1 r {LONGEST_USERNAME} 9999999999999999",
        expected_utf8_byte_length=39,
    ),
    FormattingCase(
        description="s: read receipt",
        message=ReceiptPush(recipient_username="Bob", message_id=EXAMPLE_MESSAGE_ID, receipt_level=ReceiptLevel.READ),
        expected_text="HT1 s Bob 1790294400123456 R",
        expected_utf8_byte_length=28,
    ),
    FormattingCase(
        description="s, worst case",
        message=ReceiptPush(
            recipient_username=LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            receipt_level=ReceiptLevel.DELIVERED,
        ),
        expected_text=f"HT1 s {LONGEST_USERNAME} 9999999999999999 D",
        expected_utf8_byte_length=41,
    ),
    FormattingCase(
        description="f: three missed messages follow",
        message=RefreshReply(refresh_target="ivan", message_count=3),
        expected_text="HT1 f ivan 3",
        expected_utf8_byte_length=12,
    ),
    FormattingCase(
        description="f *: five missed messages follow",
        message=RefreshReply(refresh_target="*", message_count=5),
        expected_text="HT1 f * 5",
        expected_utf8_byte_length=9,
    ),
    FormattingCase(
        description="f *: nothing is missing",
        message=RefreshReply(refresh_target="*", message_count=0),
        expected_text="HT1 f * 0",
        expected_utf8_byte_length=9,
    ),
    FormattingCase(
        description="f, worst case",
        message=RefreshReply(refresh_target=LONGEST_USERNAME, message_count=9999),
        expected_text=f"HT1 f {LONGEST_USERNAME} 9999",
        expected_utf8_byte_length=27,
    ),
    FormattingCase(
        description="f: a count above 9999 is shown as 9999",
        message=RefreshReply(refresh_target="*", message_count=12345),
        expected_text="HT1 f * 9999",
        expected_utf8_byte_length=12,
        parses_back_to_the_same_message=False,
    ),
    FormattingCase(
        description="e: the recipient of an M does not exist",
        message=ErrorReply(
            error_code=ErrorCode.NO_SUCH_USER,
            request_type="M",
            error_reference="carol 1790294400123457",
        ),
        expected_text="HT1 e NO_SUCH_USER M carol 1790294400123457",
        expected_utf8_byte_length=43,
    ),
    FormattingCase(
        description="e, worst case: a 16-letter code and an M reference with a 16-character username and a 16-digit id",
        message=ErrorReply(
            error_code=ErrorCode.PASSWORD_INVALID,
            request_type="M",
            error_reference=f"{LONGEST_USERNAME} 9999999999999999",
        ),
        expected_text=f"HT1 e PASSWORD_INVALID M {LONGEST_USERNAME} 9999999999999999",
        expected_utf8_byte_length=58,
    ),
    FormattingCase(
        description="e: a password that breaks the password rules",
        message=ErrorReply(error_code=ErrorCode.PASSWORD_INVALID, request_type="A", error_reference="bob"),
        expected_text="HT1 e PASSWORD_INVALID A bob",
        expected_utf8_byte_length=28,
    ),
    FormattingCase(
        description="e: a syntax error in a text without a type letter has type ? and no reference",
        message=ErrorReply(error_code=ErrorCode.SYNTAX, request_type="?"),
        expected_text="HT1 e SYNTAX ?",
        expected_utf8_byte_length=14,
    ),
    FormattingCase(
        description="e: another protocol version has type ? and the received version as its reference",
        message=ErrorReply(error_code=ErrorCode.VERSION, request_type="?", error_reference="2"),
        expected_text="HT1 e VERSION ? 2",
        expected_utf8_byte_length=17,
    ),
    FormattingCase(
        description="e: an unknown upper-case letter has that letter as its type and no reference",
        message=ErrorReply(error_code=ErrorCode.UNSUPPORTED, request_type="X"),
        expected_text="HT1 e UNSUPPORTED X",
        expected_utf8_byte_length=19,
    ),
    FormattingCase(
        description="e: a refresh of every conversation from a device that is not signed in",
        message=ErrorReply(error_code=ErrorCode.NOT_SIGNED_IN, request_type="F", error_reference="*"),
        expected_text="HT1 e NOT_SIGNED_IN F *",
        expected_utf8_byte_length=23,
    ),
)

INVALID_FORMATTING_CASES = (
    FormattingCase(
        description="m: a 105-byte part text would make a 151-byte direct message",
        message=DeliveryPart(
            sender_username=ANOTHER_LONGEST_USERNAME,
            message_id=LONGEST_MESSAGE_ID,
            part_number=10,
            part_count=10,
            part_text=LONGEST_ASCII_PART_TEXT + ".",
        ),
        expected_text=None,
    ),
    FormattingCase(
        description="M: a part text of 105 bytes is refused even under a short header",
        message=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=1, part_count=1, part_text="a" * 105
        ),
        expected_text=None,
    ),
    FormattingCase(
        description="M: NUL in a part text",
        message=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=1, part_count=1, part_text="a\x00b"
        ),
        expected_text=None,
    ),
    FormattingCase(
        description="M: a carriage return in a part text",
        message=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=1, part_count=1, part_text="a\rb"
        ),
        expected_text=None,
    ),
    FormattingCase(
        description="M: an empty part text",
        message=MessagePartRequest(recipient_username="Bob", message_id=5, part_number=1, part_count=1, part_text=""),
        expected_text=None,
    ),
    FormattingCase(
        description="M: part number above part count",
        message=MessagePartRequest(recipient_username="Bob", message_id=5, part_number=2, part_count=1, part_text="x"),
        expected_text=None,
    ),
    FormattingCase(
        description="m: part count above 10",
        message=DeliveryPart(sender_username="ivan", message_id=5, part_number=1, part_count=11, part_text="x"),
        expected_text=None,
    ),
    FormattingCase(
        description="a: a two-character username",
        message=AccountReply(username="ab"),
        expected_text=None,
    ),
    FormattingCase(
        description="a: a 17-character username",
        message=AccountReply(username=LONGEST_USERNAME + "a"),
        expected_text=None,
    ),
    FormattingCase(
        description="Q: a username with an underscore",
        message=QueryRequest(username="bob_1"),
        expected_text=None,
    ),
    FormattingCase(
        description="r: message id 0",
        message=ReadReply(sender_username="ivan", message_id=0),
        expected_text=None,
    ),
    FormattingCase(
        description="R: a 17-digit message id",
        message=ReadRequest(sender_username="ivan", message_id=LONGEST_MESSAGE_ID + 1),
        expected_text=None,
    ),
    FormattingCase(
        description="k: a received-set with a character other than 0 and 1",
        message=SendStatusReply(recipient_username="Bob", message_id=5, received_set="12"),
        expected_text=None,
    ),
    FormattingCase(
        description="K: an 11-character received-set",
        message=DeliveryAcknowledgement(sender_username="ivan", message_id=5, received_set="1" * 11),
        expected_text=None,
    ),
    FormattingCase(
        description="K: an empty received-set",
        message=DeliveryAcknowledgement(sender_username="ivan", message_id=5, received_set=""),
        expected_text=None,
    ),
    FormattingCase(
        description="A: a 7-character password",
        message=AccountRequest(username="bob", password="hunter2"),
        expected_text=None,
    ),
    FormattingCase(
        description="A: a password that starts with a space",
        message=AccountRequest(username="bob", password=" hunter2222"),
        expected_text=None,
    ),
    FormattingCase(
        description="A: a 65-byte password",
        message=AccountRequest(username="bob", password=LONGEST_ASCII_PASSWORD + "e"),
        expected_text=None,
    ),
    FormattingCase(
        description="F: a refresh target that is neither a username nor *",
        message=RefreshRequest(refresh_target="**"),
        expected_text=None,
    ),
    FormattingCase(
        description="f: a negative message count",
        message=RefreshReply(refresh_target="*", message_count=-1),
        expected_text=None,
    ),
    FormattingCase(
        description="e: a lower-case error code",
        message=ErrorReply(error_code="syntax", request_type="A", error_reference="bob"),
        expected_text=None,
    ),
    FormattingCase(
        description="e: a lower-case request type",
        message=ErrorReply(error_code=ErrorCode.SYNTAX, request_type="a"),
        expected_text=None,
    ),
    FormattingCase(
        description="e: a reference with a leading zero in its message id",
        message=ErrorReply(error_code=ErrorCode.ID_CONFLICT, request_type="M", error_reference="bob 01"),
        expected_text=None,
    ),
)

FORMATTING_CASES = VALID_FORMATTING_CASES + INVALID_FORMATTING_CASES
