import pytest

from protocol.constants import (
    DIRECT_MESSAGE_MAXIMUM_BYTES,
    PART_HEADER_MAXIMUM_BYTES,
    PART_TEXT_MAXIMUM_BYTES,
    ErrorCode,
)
from protocol.formatting import (
    InvalidDirectMessageTextError,
    ensure_direct_message_text_is_sendable,
    format_account_request,
    format_delivery_part,
    format_error_reply,
    format_message_part_request,
    format_protocol_message,
    format_refresh_reply,
)
from protocol.text_validation import count_utf8_bytes
from tests.protocol.formatting_cases import (
    INVALID_FORMATTING_CASES,
    LONGEST_USERNAME,
    VALID_FORMATTING_CASES,
    FormattingCase,
)

# The worst case of every type, as the protocol's message tables list them.
WORST_CASE_UTF8_BYTE_LENGTHS = {
    "A": 87,
    "Q": 22,
    "M": 150,
    "K": 50,
    "R": 39,
    "C": 41,
    "F": 22,
    "a": 22,
    "q": 24,
    "k": 50,
    "m": 150,
    "r": 39,
    "s": 41,
    "f": 27,
    "e": 58,
}


@pytest.mark.parametrize(
    "formatting_case",
    VALID_FORMATTING_CASES,
    ids=lambda formatting_case: formatting_case.description,
)
def test_formatting_gives_the_exact_text_and_byte_length(formatting_case: FormattingCase) -> None:
    direct_message_text = format_protocol_message(formatting_case.message)

    assert direct_message_text == formatting_case.expected_text
    assert count_utf8_bytes(direct_message_text) == formatting_case.expected_utf8_byte_length


@pytest.mark.parametrize(
    "formatting_case",
    INVALID_FORMATTING_CASES,
    ids=lambda formatting_case: formatting_case.description,
)
def test_formatting_refuses_a_message_that_breaks_the_protocol(formatting_case: FormattingCase) -> None:
    with pytest.raises(InvalidDirectMessageTextError):
        format_protocol_message(formatting_case.message)


def test_the_cases_reach_the_worst_case_byte_length_of_every_type_and_never_more() -> None:
    longest_utf8_byte_lengths: dict[str, int] = {}
    for formatting_case in VALID_FORMATTING_CASES:
        message_type_letter = str(formatting_case.message.message_type)
        assert formatting_case.expected_utf8_byte_length is not None
        longest_utf8_byte_lengths[message_type_letter] = max(
            longest_utf8_byte_lengths.get(message_type_letter, 0),
            formatting_case.expected_utf8_byte_length,
        )

    assert longest_utf8_byte_lengths == WORST_CASE_UTF8_BYTE_LENGTHS


def test_the_longest_part_header_leaves_exactly_the_part_text_budget() -> None:
    longest_part_header = f"HT1 M {LONGEST_USERNAME} 9999999999999999 10/10 "

    assert count_utf8_bytes(longest_part_header) == PART_HEADER_MAXIMUM_BYTES
    assert PART_HEADER_MAXIMUM_BYTES + PART_TEXT_MAXIMUM_BYTES == DIRECT_MESSAGE_MAXIMUM_BYTES


def test_a_direct_message_of_150_bytes_is_sendable_and_one_of_151_bytes_is_not() -> None:
    ensure_direct_message_text_is_sendable("HT1 " + "x" * 146)

    with pytest.raises(InvalidDirectMessageTextError, match="151"):
        ensure_direct_message_text_is_sendable("HT1 " + "x" * 147)


def test_multi_byte_characters_count_in_bytes_toward_the_limit() -> None:
    ensure_direct_message_text_is_sendable("HT1 " + "\U0001f600" * 36 + "xx")

    with pytest.raises(InvalidDirectMessageTextError):
        ensure_direct_message_text_is_sendable("HT1 " + "\U0001f600" * 37)


def test_a_direct_message_with_nul_is_never_sendable() -> None:
    with pytest.raises(InvalidDirectMessageTextError, match="U\\+0000"):
        ensure_direct_message_text_is_sendable("HT1 a bob\x00")


def test_a_direct_message_with_a_lone_surrogate_is_never_sendable() -> None:
    with pytest.raises(InvalidDirectMessageTextError, match="surrogate"):
        ensure_direct_message_text_is_sendable("HT1 a bob\ud800")


def test_a_direct_message_must_start_with_the_version_one_prefix() -> None:
    with pytest.raises(InvalidDirectMessageTextError):
        ensure_direct_message_text_is_sendable("HT2 a bob")


def test_a_part_text_with_nul_is_refused() -> None:
    with pytest.raises(InvalidDirectMessageTextError):
        format_delivery_part("ivan", 5, 1, 1, "before\x00after")


def test_a_part_text_with_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(InvalidDirectMessageTextError):
        format_message_part_request("Bob", 5, 1, 1, "before\udc80after")


def test_a_password_with_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(InvalidDirectMessageTextError):
        format_account_request("bob", "hunter2222\ud83d")


def test_a_refused_password_is_never_quoted_in_the_error() -> None:
    with pytest.raises(InvalidDirectMessageTextError) as raised_error:
        format_account_request("bob", "secret\x00password")

    assert "secret" not in str(raised_error.value)


def test_a_refresh_reply_shows_at_most_9999_messages() -> None:
    assert format_refresh_reply("ivan", 10_000) == "HT1 f ivan 9999"


def test_an_error_code_unknown_to_this_version_is_formatted_when_it_has_the_right_shape() -> None:
    assert format_error_reply("SERVER_BUSY", "Q", "bob") == "HT1 e SERVER_BUSY Q bob"


def test_an_error_code_longer_than_16_letters_is_refused() -> None:
    with pytest.raises(InvalidDirectMessageTextError):
        format_error_reply("PASSWORD_INVALIDX", "A", "bob")


def test_every_error_code_fits_the_error_code_field() -> None:
    for error_code in ErrorCode:
        assert format_error_reply(error_code, "A", "bob") == f"HT1 e {error_code} A bob"
