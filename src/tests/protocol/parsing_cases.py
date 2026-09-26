"""Direct message texts and what the parser must make of them; exported as vectors/parsing.json.

`expected_error_reply_text` is the error the server answers because of the text itself: a
grammar error, or a broken value rule of a well-formed request. For a part's value rule it
assumes that the device is signed in and the recipient exists, because those checks come
first. None means the text itself earns no error reply: a valid request is answered by its
service, and acknowledgements, lower-case types and other text are never answered.
"""

from dataclasses import dataclass

from protocol.constants import ErrorCode, ReceiptLevel
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
from tests.protocol.formatting_cases import LONGEST_CYRILLIC_PASSWORD, LONGEST_USERNAME
from tests.protocol.splitting_cases import COMBINING_ACUTE_ACCENT, GRINNING_FACE

COMBINING_LOW_LINE = "\N{COMBINING LOW LINE}"
CYRILLIC_SMALL_LETTER_A = "\N{CYRILLIC SMALL LETTER A}"
FULL_WIDTH_BOB = (
    "\N{FULLWIDTH LATIN SMALL LETTER B}\N{FULLWIDTH LATIN SMALL LETTER O}\N{FULLWIDTH LATIN SMALL LETTER B}"
)


@dataclass(frozen=True, kw_only=True)
class ParsingCase:
    description: str
    direct_message_text: str
    expected_result: ParsedDirectMessage
    expected_value_rule_error: ErrorCode | None = None
    expected_error_reply_text: str | None = None
    # For a K: the part count of the message it acknowledges, and whether the server ignores it.
    acknowledged_message_part_count: int | None = None
    expected_acknowledgement_ignored: bool | None = None


PARSING_CASES = (
    # ----- the examples of the protocol's test vector appendix -----
    ParsingCase(
        description="A valid single-part message with Cyrillic text",
        direct_message_text="HT1 M Bob 1790294400123456 1/1 Привет, Боб!",
        expected_result=MessagePartRequest(
            recipient_username="Bob",
            message_id=1_790_294_400_123_456,
            part_number=1,
            part_count=1,
            part_text="Привет, Боб!",
        ),
    ),
    ParsingCase(
        description="A leading zero in the message id is a syntax error; the reference needs both peer and id",
        direct_message_text="HT1 M Bob 01 1/1 x",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob",)),
        expected_error_reply_text="HT1 e SYNTAX M",
    ),
    ParsingCase(
        description="Part number above part count is a value error, not a syntax error",
        direct_message_text="HT1 M Bob 5 2/1 x",
        expected_result=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=2, part_count=1, part_text="x"
        ),
        expected_value_rule_error=ErrorCode.PART_INVALID,
        expected_error_reply_text="HT1 e PART_INVALID M Bob 5",
    ),
    ParsingCase(
        description="Part count above 10 is a value error",
        direct_message_text="HT1 M Bob 5 1/11 x",
        expected_result=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=1, part_count=11, part_text="x"
        ),
        expected_value_rule_error=ErrorCode.PART_INVALID,
        expected_error_reply_text="HT1 e PART_INVALID M Bob 5",
    ),
    ParsingCase(
        description="An empty part text is a value error",
        direct_message_text="HT1 M Bob 5 1/1 ",
        expected_result=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=1, part_count=1, part_text=""
        ),
        expected_value_rule_error=ErrorCode.PART_INVALID,
        expected_error_reply_text="HT1 e PART_INVALID M Bob 5",
    ),
    ParsingCase(
        description="A three-digit part count is a syntax error",
        direct_message_text="HT1 M Bob 5 1/100 x",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="A two-character username is a syntax error",
        direct_message_text="HT1 A ab hunter2222",
        expected_result=ProtocolSyntaxError(request_type="A"),
        expected_error_reply_text="HT1 e SYNTAX A",
    ),
    ParsingCase(
        description="A 5-character password is a value error",
        direct_message_text="HT1 A bob short",
        expected_result=AccountRequest(username="bob", password="short"),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="Two spaces after HT1: no type letter",
        direct_message_text="HT1  Q bob",
        expected_result=ProtocolSyntaxError(request_type="?"),
        expected_error_reply_text="HT1 e SYNTAX ?",
    ),
    ParsingCase(
        description="A delivery acknowledgement whose set length differs from the part count is ignored",
        direct_message_text="HT1 K ivan 5 11",
        expected_result=DeliveryAcknowledgement(sender_username="ivan", message_id=5, received_set="11"),
        acknowledged_message_part_count=3,
        expected_acknowledgement_ignored=True,
    ),
    ParsingCase(
        description="A request in protocol version 2",
        direct_message_text="HT2 Q bob",
        expected_result=UnsupportedVersion(version="2", next_character="Q"),
        expected_error_reply_text="HT1 e VERSION ? 2",
    ),
    ParsingCase(
        description="An unknown upper-case type letter",
        direct_message_text="HT1 X foo",
        expected_result=UnknownMessageType(message_type_letter="X"),
        expected_error_reply_text="HT1 e UNSUPPORTED X",
    ),
    ParsingCase(
        description="Other text is not protocol traffic and is never answered",
        direct_message_text="hello",
        expected_result=OtherText(text="hello"),
    ),
    # ----- every client type -----
    ParsingCase(
        description="A with spaces inside the password",
        direct_message_text="HT1 A ivan correct horse battery",
        expected_result=AccountRequest(username="ivan", password="correct horse battery"),
    ),
    ParsingCase(
        description="A keeps the username's case as sent",
        direct_message_text="HT1 A IVAN correct horse batterx",
        expected_result=AccountRequest(username="IVAN", password="correct horse batterx"),
    ),
    ParsingCase(
        description="Q",
        direct_message_text="HT1 Q bob",
        expected_result=QueryRequest(username="bob"),
    ),
    ParsingCase(
        description="Q with a three-character username of digits",
        direct_message_text="HT1 Q 123",
        expected_result=QueryRequest(username="123"),
    ),
    ParsingCase(
        description="Q with a 16-character username",
        direct_message_text=f"HT1 Q {LONGEST_USERNAME}",
        expected_result=QueryRequest(username=LONGEST_USERNAME),
    ),
    ParsingCase(
        description="M, the largest message id and part 10 of 10",
        direct_message_text="HT1 M bob 9999999999999999 10/10 last part",
        expected_result=MessagePartRequest(
            recipient_username="bob",
            message_id=9_999_999_999_999_999,
            part_number=10,
            part_count=10,
            part_text="last part",
        ),
    ),
    ParsingCase(
        description="M keeps the part text verbatim, trailing space included",
        direct_message_text="HT1 M bob 2 1/2 Hello ",
        expected_result=MessagePartRequest(
            recipient_username="bob", message_id=2, part_number=1, part_count=2, part_text="Hello "
        ),
    ),
    ParsingCase(
        description="M: a second space before the part text belongs to the text",
        direct_message_text="HT1 M bob 2 2/2  again ",
        expected_result=MessagePartRequest(
            recipient_username="bob", message_id=2, part_number=2, part_count=2, part_text=" again "
        ),
    ),
    ParsingCase(
        description="M: a part text may look like a protocol message",
        direct_message_text="HT1 M Bob 5 1/1 HT1 M Bob 6 1/1 x",
        expected_result=MessagePartRequest(
            recipient_username="Bob",
            message_id=5,
            part_number=1,
            part_count=1,
            part_text="HT1 M Bob 6 1/1 x",
        ),
    ),
    ParsingCase(
        description="M: tab and line feed are allowed in a part text",
        direct_message_text="HT1 M Bob 5 1/1 line one\n\tline two",
        expected_result=MessagePartRequest(
            recipient_username="Bob",
            message_id=5,
            part_number=1,
            part_count=1,
            part_text="line one\n\tline two",
        ),
    ),
    ParsingCase(
        description="M: a part text of exactly 104 bytes",
        direct_message_text="HT1 M Bob 5 1/1 " + GRINNING_FACE * 26,
        expected_result=MessagePartRequest(
            recipient_username="Bob",
            message_id=5,
            part_number=1,
            part_count=1,
            part_text=GRINNING_FACE * 26,
        ),
    ),
    ParsingCase(
        description="M: a part text of 105 bytes is a value error, not a syntax error",
        direct_message_text="HT1 M Bob 5 1/1 " + "a" * 105,
        expected_result=MessagePartRequest(
            recipient_username="Bob",
            message_id=5,
            part_number=1,
            part_count=1,
            part_text="a" * 105,
        ),
        expected_value_rule_error=ErrorCode.PART_INVALID,
        expected_error_reply_text="HT1 e PART_INVALID M Bob 5",
    ),
    ParsingCase(
        description="M: a carriage return in a part text is a value error",
        direct_message_text="HT1 M Bob 5 1/1 a\rb",
        expected_result=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=1, part_count=1, part_text="a\rb"
        ),
        expected_value_rule_error=ErrorCode.PART_INVALID,
        expected_error_reply_text="HT1 e PART_INVALID M Bob 5",
    ),
    ParsingCase(
        description="M: part 99 of 99 has the right shape but breaks the part range",
        direct_message_text="HT1 M Bob 5 99/99 x",
        expected_result=MessagePartRequest(
            recipient_username="Bob", message_id=5, part_number=99, part_count=99, part_text="x"
        ),
        expected_value_rule_error=ErrorCode.PART_INVALID,
        expected_error_reply_text="HT1 e PART_INVALID M Bob 5",
    ),
    ParsingCase(
        description="K with a complete three-part set",
        direct_message_text="HT1 K ivan 1790294400123457 111",
        expected_result=DeliveryAcknowledgement(
            sender_username="ivan", message_id=1_790_294_400_123_457, received_set="111"
        ),
        acknowledged_message_part_count=3,
        expected_acknowledgement_ignored=False,
    ),
    ParsingCase(
        description="R",
        direct_message_text="HT1 R ivan 1790294400123456",
        expected_result=ReadRequest(sender_username="ivan", message_id=1_790_294_400_123_456),
    ),
    ParsingCase(
        description="C for a read receipt",
        direct_message_text="HT1 C Bob 1790294400123456 R",
        expected_result=ReceiptAcknowledgement(
            recipient_username="Bob",
            message_id=1_790_294_400_123_456,
            receipt_level=ReceiptLevel.READ,
        ),
    ),
    ParsingCase(
        description="F for one conversation",
        direct_message_text="HT1 F ivan",
        expected_result=RefreshRequest(refresh_target="ivan"),
    ),
    ParsingCase(
        description="F * for every conversation",
        direct_message_text="HT1 F *",
        expected_result=RefreshRequest(refresh_target="*"),
    ),
    # ----- passwords: the rules are value rules, checked after NFC -----
    ParsingCase(
        description="A 7-character password is a value error",
        direct_message_text="HT1 A bob hunter2",
        expected_result=AccountRequest(username="bob", password="hunter2"),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="A control character in a password is a value error",
        direct_message_text="HT1 A bob hunter\x012222",
        expected_result=AccountRequest(username="bob", password="hunter\x012222"),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="An empty password is well-formed and breaks the password rules",
        direct_message_text="HT1 A bob ",
        expected_result=AccountRequest(username="bob", password=""),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="A second space before the password belongs to it, and a leading space is not allowed",
        direct_message_text="HT1 A bob  hunter2222",
        expected_result=AccountRequest(username="bob", password=" hunter2222"),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="A trailing space in a password is not allowed",
        direct_message_text="HT1 A bob hunter2222 ",
        expected_result=AccountRequest(username="bob", password="hunter2222 "),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="32 Cyrillic letters (64 bytes) are the longest Cyrillic password",
        direct_message_text=f"HT1 A bob {LONGEST_CYRILLIC_PASSWORD}",
        expected_result=AccountRequest(username="bob", password=LONGEST_CYRILLIC_PASSWORD),
    ),
    ParsingCase(
        description="33 Cyrillic letters (66 bytes) exceed the 64-byte password limit",
        direct_message_text=f"HT1 A bob {LONGEST_CYRILLIC_PASSWORD}{CYRILLIC_SMALL_LETTER_A}",
        expected_result=AccountRequest(
            username="bob", password=f"{LONGEST_CYRILLIC_PASSWORD}{CYRILLIC_SMALL_LETTER_A}"
        ),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description="8 decomposed e-acute letters (16 code points) are 8 characters after NFC: valid",
        direct_message_text="HT1 A bob " + ("e" + COMBINING_ACUTE_ACCENT) * 8,
        expected_result=AccountRequest(username="bob", password=("e" + COMBINING_ACUTE_ACCENT) * 8),
    ),
    ParsingCase(
        description="7 decomposed e-acute letters (14 code points) are 7 characters after NFC: too short",
        direct_message_text="HT1 A bob " + ("e" + COMBINING_ACUTE_ACCENT) * 7,
        expected_result=AccountRequest(username="bob", password=("e" + COMBINING_ACUTE_ACCENT) * 7),
        expected_value_rule_error=ErrorCode.PASSWORD_INVALID,
        expected_error_reply_text="HT1 e PASSWORD_INVALID A bob",
    ),
    ParsingCase(
        description=(
            "Password characters are Unicode scalar values after NFC, not grapheme clusters: 4 letters with a "
            "combining low line (which NFC cannot compose) are 8 characters, so the password is valid"
        ),
        direct_message_text="HT1 A bob " + ("a" + COMBINING_LOW_LINE) * 4,
        expected_result=AccountRequest(username="bob", password=("a" + COMBINING_LOW_LINE) * 4),
    ),
    # ----- syntax errors: the reference repeats only correlation fields that parsed -----
    ParsingCase(
        description="Two spaces between the type letter and the username",
        direct_message_text="HT1 Q  bob",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="Two spaces between the username and the message id",
        direct_message_text="HT1 R ivan  5",
        expected_result=ProtocolSyntaxError(request_type="R", correlation_fields=("ivan",)),
        expected_error_reply_text="HT1 e SYNTAX R",
    ),
    ParsingCase(
        description="Two spaces before the part field",
        direct_message_text="HT1 M Bob 5  1/1 x",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="A tab instead of the space after the type letter",
        direct_message_text="HT1 Q\tbob",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="A tab between two fields",
        direct_message_text="HT1 R ivan\t5",
        expected_result=ProtocolSyntaxError(request_type="R"),
        expected_error_reply_text="HT1 e SYNTAX R",
    ),
    ParsingCase(
        description="A leading zero in the part number",
        direct_message_text="HT1 M Bob 5 01/1 x",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="A three-digit part number",
        direct_message_text="HT1 M Bob 5 100/1 x",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="Part number 0",
        direct_message_text="HT1 M Bob 5 0/1 x",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="An M without its part text",
        direct_message_text="HT1 M Bob 5 1/1",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="NUL in a part text breaks the grammar",
        direct_message_text="HT1 M Bob 5 1/1 a\x00b",
        expected_result=ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5")),
        expected_error_reply_text="HT1 e SYNTAX M Bob 5",
    ),
    ParsingCase(
        description="A 17-digit message id",
        direct_message_text="HT1 R ivan 12345678901234567",
        expected_result=ProtocolSyntaxError(request_type="R", correlation_fields=("ivan",)),
        expected_error_reply_text="HT1 e SYNTAX R",
    ),
    ParsingCase(
        description="Message id 0",
        direct_message_text="HT1 R ivan 0",
        expected_result=ProtocolSyntaxError(request_type="R", correlation_fields=("ivan",)),
        expected_error_reply_text="HT1 e SYNTAX R",
    ),
    ParsingCase(
        description="A 17-character username",
        direct_message_text=f"HT1 Q {LONGEST_USERNAME}a",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="A Cyrillic username",
        direct_message_text="HT1 Q Иван",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="A username with an accented letter",
        direct_message_text="HT1 A José hunter2222",
        expected_result=ProtocolSyntaxError(request_type="A"),
        expected_error_reply_text="HT1 e SYNTAX A",
    ),
    ParsingCase(
        description="A username with an underscore",
        direct_message_text="HT1 Q bob_1",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="A username in full-width letters",
        direct_message_text=f"HT1 Q {FULL_WIDTH_BOB}",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="A trailing space after the last field of a type without a tail",
        direct_message_text="HT1 Q bob ",
        expected_result=ProtocolSyntaxError(request_type="Q", correlation_fields=("bob",)),
        expected_error_reply_text="HT1 e SYNTAX Q bob",
    ),
    ParsingCase(
        description="A line feed after the username",
        direct_message_text="HT1 Q bob\n",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="An extra field",
        direct_message_text="HT1 R ivan 5 extra",
        expected_result=ProtocolSyntaxError(request_type="R", correlation_fields=("ivan", "5")),
        expected_error_reply_text="HT1 e SYNTAX R ivan 5",
    ),
    ParsingCase(
        description="A missing field",
        direct_message_text="HT1 R ivan",
        expected_result=ProtocolSyntaxError(request_type="R", correlation_fields=("ivan",)),
        expected_error_reply_text="HT1 e SYNTAX R",
    ),
    ParsingCase(
        description="An A without its password",
        direct_message_text="HT1 A bob",
        expected_result=ProtocolSyntaxError(request_type="A", correlation_fields=("bob",)),
        expected_error_reply_text="HT1 e SYNTAX A bob",
    ),
    ParsingCase(
        description="A type letter without fields",
        direct_message_text="HT1 Q",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="A type letter followed by a field without a space",
        direct_message_text="HT1 Qbob",
        expected_result=ProtocolSyntaxError(request_type="Q"),
        expected_error_reply_text="HT1 e SYNTAX Q",
    ),
    ParsingCase(
        description="F with an extra field after *",
        direct_message_text="HT1 F * bob",
        expected_result=ProtocolSyntaxError(request_type="F", correlation_fields=("*",)),
        expected_error_reply_text="HT1 e SYNTAX F *",
    ),
    ParsingCase(
        description="F with a target that is neither a username nor *",
        direct_message_text="HT1 F **",
        expected_result=ProtocolSyntaxError(request_type="F"),
        expected_error_reply_text="HT1 e SYNTAX F",
    ),
    ParsingCase(
        description="A received-set with a character other than 0 and 1: a malformed acknowledgement is not answered",
        direct_message_text="HT1 K ivan 5 12",
        expected_result=ProtocolSyntaxError(request_type="K", correlation_fields=("ivan", "5")),
    ),
    ParsingCase(
        description="An 11-character received-set",
        direct_message_text="HT1 K ivan 5 11111111111",
        expected_result=ProtocolSyntaxError(request_type="K", correlation_fields=("ivan", "5")),
    ),
    ParsingCase(
        description="A receipt level other than D and R",
        direct_message_text="HT1 C Bob 5 X",
        expected_result=ProtocolSyntaxError(request_type="C", correlation_fields=("Bob", "5")),
    ),
    ParsingCase(
        description="A lower-case receipt level",
        direct_message_text="HT1 C Bob 5 d",
        expected_result=ProtocolSyntaxError(request_type="C", correlation_fields=("Bob", "5")),
    ),
    # ----- type letters and versions -----
    ParsingCase(
        description="Nothing after HT1 and its space",
        direct_message_text="HT1 ",
        expected_result=ProtocolSyntaxError(request_type="?"),
        expected_error_reply_text="HT1 e SYNTAX ?",
    ),
    ParsingCase(
        description="A digit where the type letter belongs",
        direct_message_text="HT1 5 x",
        expected_result=ProtocolSyntaxError(request_type="?"),
        expected_error_reply_text="HT1 e SYNTAX ?",
    ),
    ParsingCase(
        description="A non-ASCII letter where the type letter belongs",
        direct_message_text="HT1 Ж x",
        expected_result=ProtocolSyntaxError(request_type="?"),
        expected_error_reply_text="HT1 e SYNTAX ?",
    ),
    ParsingCase(
        description="An unknown upper-case letter without fields",
        direct_message_text="HT1 Z",
        expected_result=UnknownMessageType(message_type_letter="Z"),
        expected_error_reply_text="HT1 e UNSUPPORTED Z",
    ),
    ParsingCase(
        description="An unknown lower-case letter is a newer server type: never answered",
        direct_message_text="HT1 x foo",
        expected_result=UnknownMessageType(message_type_letter="x"),
    ),
    ParsingCase(
        description="A request in protocol version 10",
        direct_message_text="HT10 Q bob",
        expected_result=UnsupportedVersion(version="10", next_character="Q"),
        expected_error_reply_text="HT1 e VERSION ? 10",
    ),
    ParsingCase(
        description="A request in protocol version 123",
        direct_message_text="HT123 A bob hunter2222",
        expected_result=UnsupportedVersion(version="123", next_character="A"),
        expected_error_reply_text="HT1 e VERSION ? 123",
    ),
    ParsingCase(
        description="Version 01 is not version 1",
        direct_message_text="HT01 Q bob",
        expected_result=UnsupportedVersion(version="01", next_character="Q"),
        expected_error_reply_text="HT1 e VERSION ? 01",
    ),
    ParsingCase(
        description="A server type in another version is never answered",
        direct_message_text="HT2 q bob 1",
        expected_result=UnsupportedVersion(version="2", next_character="q"),
    ),
    ParsingCase(
        description="Nothing after another version is never answered",
        direct_message_text="HT2 ",
        expected_result=UnsupportedVersion(version="2", next_character=""),
    ),
    ParsingCase(
        description="Four version digits are not protocol traffic",
        direct_message_text="HT1234 Q bob",
        expected_result=OtherText(text="HT1234 Q bob"),
    ),
    ParsingCase(
        description="A letter instead of the version digits is not protocol traffic",
        direct_message_text="HTX Q bob",
        expected_result=OtherText(text="HTX Q bob"),
    ),
    ParsingCase(
        description="HT1 without a space is not protocol traffic",
        direct_message_text="HT1",
        expected_result=OtherText(text="HT1"),
    ),
    ParsingCase(
        description="HT1 followed by the type letter without a space is not protocol traffic",
        direct_message_text="HT1Q bob",
        expected_result=OtherText(text="HT1Q bob"),
    ),
    ParsingCase(
        description="The magic HT is case-sensitive",
        direct_message_text="ht1 Q bob",
        expected_result=OtherText(text="ht1 Q bob"),
    ),
    ParsingCase(
        description="A space before HT1 is not protocol traffic",
        direct_message_text=" HT1 Q bob",
        expected_result=OtherText(text=" HT1 Q bob"),
    ),
    ParsingCase(
        description="An empty text is not protocol traffic",
        direct_message_text="",
        expected_result=OtherText(text=""),
    ),
    # ----- every server type -----
    ParsingCase(
        description="a",
        direct_message_text="HT1 a ivan",
        expected_result=AccountReply(username="ivan"),
    ),
    ParsingCase(
        description="q: the user exists",
        direct_message_text="HT1 q Bob 1",
        expected_result=QueryReply(username="Bob", user_exists=True),
    ),
    ParsingCase(
        description="q: the user does not exist",
        direct_message_text="HT1 q carol 0",
        expected_result=QueryReply(username="carol", user_exists=False),
    ),
    ParsingCase(
        description="k with a missing part",
        direct_message_text="HT1 k Bob 1790294400123457 101",
        expected_result=SendStatusReply(recipient_username="Bob", message_id=1_790_294_400_123_457, received_set="101"),
    ),
    ParsingCase(
        description="m keeps the part text verbatim",
        direct_message_text="HT1 m ivan 1790294400123456 1/1 Привет, Боб!",
        expected_result=DeliveryPart(
            sender_username="ivan",
            message_id=1_790_294_400_123_456,
            part_number=1,
            part_count=1,
            part_text="Привет, Боб!",
        ),
    ),
    ParsingCase(
        description="r",
        direct_message_text="HT1 r ivan 1790294400123456",
        expected_result=ReadReply(sender_username="ivan", message_id=1_790_294_400_123_456),
    ),
    ParsingCase(
        description="s for a delivered receipt",
        direct_message_text="HT1 s Bob 1790294400123456 D",
        expected_result=ReceiptPush(
            recipient_username="Bob",
            message_id=1_790_294_400_123_456,
            receipt_level=ReceiptLevel.DELIVERED,
        ),
    ),
    ParsingCase(
        description="f for one conversation",
        direct_message_text="HT1 f ivan 3",
        expected_result=RefreshReply(refresh_target="ivan", message_count=3),
    ),
    ParsingCase(
        description="f * with nothing missing",
        direct_message_text="HT1 f * 0",
        expected_result=RefreshReply(refresh_target="*", message_count=0),
    ),
    ParsingCase(
        description="e with an M reference",
        direct_message_text="HT1 e NO_SUCH_USER M carol 1790294400123457",
        expected_result=ErrorReply(
            error_code=ErrorCode.NO_SUCH_USER,
            request_type="M",
            error_reference="carol 1790294400123457",
        ),
    ),
    ParsingCase(
        description="e without a reference",
        direct_message_text="HT1 e SYNTAX ?",
        expected_result=ErrorReply(error_code=ErrorCode.SYNTAX, request_type="?"),
    ),
    ParsingCase(
        description="e with a version reference",
        direct_message_text="HT1 e VERSION ? 2",
        expected_result=ErrorReply(error_code=ErrorCode.VERSION, request_type="?", error_reference="2"),
    ),
    ParsingCase(
        description="e with a refresh target reference",
        direct_message_text="HT1 e NOT_SIGNED_IN F *",
        expected_result=ErrorReply(error_code=ErrorCode.NOT_SIGNED_IN, request_type="F", error_reference="*"),
    ),
    ParsingCase(
        description="e with a code this version does not know: well-formed, and a client treats it as permanent",
        direct_message_text="HT1 e SERVER_BUSY Q bob",
        expected_result=ErrorReply(error_code="SERVER_BUSY", request_type="Q", error_reference="bob"),
    ),
    ParsingCase(
        description="e with a lower-case error code is malformed",
        direct_message_text="HT1 e syntax A bob",
        expected_result=ProtocolSyntaxError(request_type="e"),
    ),
    ParsingCase(
        description="e with a lower-case request type is malformed",
        direct_message_text="HT1 e SYNTAX a",
        expected_result=ProtocolSyntaxError(request_type="e"),
    ),
    ParsingCase(
        description="e without a request type is malformed",
        direct_message_text="HT1 e SYNTAX",
        expected_result=ProtocolSyntaxError(request_type="e"),
    ),
    ParsingCase(
        description="e with a trailing space is malformed",
        direct_message_text="HT1 e SYNTAX ? ",
        expected_result=ProtocolSyntaxError(request_type="e"),
    ),
    ParsingCase(
        description="e with a three-field reference is malformed",
        direct_message_text="HT1 e ID_CONFLICT M bob 5 x",
        expected_result=ProtocolSyntaxError(request_type="e"),
    ),
    ParsingCase(
        description="f with a count above 9999 is malformed",
        direct_message_text="HT1 f ivan 10000",
        expected_result=ProtocolSyntaxError(request_type="f", correlation_fields=("ivan",)),
    ),
    ParsingCase(
        description="q with an existence other than 1 and 0 is malformed",
        direct_message_text="HT1 q Bob yes",
        expected_result=ProtocolSyntaxError(request_type="q", correlation_fields=("Bob",)),
    ),
)
