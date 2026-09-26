import pytest

from protocol.passwords import is_valid_password, normalize_password
from protocol.received_sets import (
    calculate_all_parts_mask,
    convert_parts_mask_to_received_set,
    convert_received_set_to_parts_mask,
    is_complete_received_set,
    is_valid_part_numbering,
    received_set_matches_part_count,
)
from protocol.text_validation import count_utf8_bytes, is_valid_part_text
from protocol.usernames import is_valid_username, normalize_username_for_lookup
from tests.protocol.parsing_cases import FULL_WIDTH_BOB

COMPOSED_E_WITH_ACUTE = "\N{LATIN SMALL LETTER E WITH ACUTE}"
DECOMPOSED_E_WITH_ACUTE = "e\N{COMBINING ACUTE ACCENT}"
GRINNING_FACE = "\U0001f600"
CYRILLIC_LETTER_ZHE = "Ж"

# ----------------------------------------------------------------------------------------------------------------------
# Usernames
# ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("username", ["abc", "Bob", "ivan", "123", "KonstantinIvanov", "a1B2c3"])
def test_a_username_of_3_to_16_ascii_letters_and_digits_is_valid(username: str) -> None:
    assert is_valid_username(username)


@pytest.mark.parametrize(
    "username",
    ["", "ab", "KonstantinIvanovv", "bob_1", "bo.b", "bo b", "Иван", "José", FULL_WIDTH_BOB, "bob\n", "٣٣٣", "b\x00b"],
)
def test_a_username_that_breaks_the_rules_is_invalid(username: str) -> None:
    assert not is_valid_username(username)


def test_usernames_are_looked_up_case_insensitively() -> None:
    assert normalize_username_for_lookup("Bob") == normalize_username_for_lookup("BOB") == "bob"


# ----------------------------------------------------------------------------------------------------------------------
# Passwords
# ----------------------------------------------------------------------------------------------------------------------


def test_composed_and_decomposed_letters_normalize_to_the_same_password() -> None:
    assert normalize_password(DECOMPOSED_E_WITH_ACUTE * 8) == COMPOSED_E_WITH_ACUTE * 8


def test_the_password_length_is_counted_after_normalization() -> None:
    assert is_valid_password(normalize_password(DECOMPOSED_E_WITH_ACUTE * 8))
    assert not is_valid_password(normalize_password(DECOMPOSED_E_WITH_ACUTE * 7))


@pytest.mark.parametrize(
    "password",
    [
        "hunter22",
        "correct horse battery staple",
        "x" * 64,
        CYRILLIC_LETTER_ZHE * 32,
        GRINNING_FACE * 16,
        "\N{NO-BREAK SPACE}nonbreaking\N{NO-BREAK SPACE}",
        "a\N{COMBINING LOW LINE}" * 4,
    ],
)
def test_a_password_within_the_rules_is_valid(password: str) -> None:
    assert is_valid_password(normalize_password(password))


@pytest.mark.parametrize(
    "password",
    [
        "",
        "hunter2",
        "x" * 65,
        CYRILLIC_LETTER_ZHE * 33,
        GRINNING_FACE * 17,
        " hunter2222",
        "hunter2222 ",
        "hunter\t2222",
        "hunter\n2222",
        "hunter\x002222",
        "hunter\x1f2222",
        "hunter\x7f2222",
        "hunter\x802222",
        "hunter\x9f2222",
        "hunter2222\ud800",
    ],
)
def test_a_password_that_breaks_the_rules_is_invalid(password: str) -> None:
    assert not is_valid_password(normalize_password(password))


# ----------------------------------------------------------------------------------------------------------------------
# Part texts
# ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "part_text",
    [
        "x",
        "a" * 104,
        GRINNING_FACE * 26,
        GRINNING_FACE * 25 + "abcd",
        "tab\tand line feed\n",
        "\N{NO-BREAK SPACE} and every character from U+00A0 on: \N{LINE SEPARATOR}\N{ZERO WIDTH NO-BREAK SPACE}",
        "the last scalar value: \U0010ffff",
    ],
)
def test_a_part_text_within_the_rules_is_valid(part_text: str) -> None:
    assert is_valid_part_text(part_text)


@pytest.mark.parametrize(
    "part_text",
    [
        "",
        "a" * 105,
        GRINNING_FACE * 26 + "a",
        GRINNING_FACE * 27,
        "carriage\rreturn",
        "nul\x00",
        "escape\x1b",
        "delete\x7f",
        "next line\x85",
        "lone surrogate\ud800",
    ],
)
def test_a_part_text_that_breaks_the_rules_is_invalid(part_text: str) -> None:
    assert not is_valid_part_text(part_text)


def test_a_four_byte_character_counts_as_four_bytes() -> None:
    assert count_utf8_bytes(GRINNING_FACE) == 4
    assert count_utf8_bytes(CYRILLIC_LETTER_ZHE) == 2


# ----------------------------------------------------------------------------------------------------------------------
# Part numbers and received-sets
# ----------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("part_number", "part_count"), [(1, 1), (1, 10), (10, 10), (3, 5)])
def test_part_numbering_within_one_to_ten_is_valid(part_number: int, part_count: int) -> None:
    assert is_valid_part_numbering(part_number, part_count)


@pytest.mark.parametrize(("part_number", "part_count"), [(2, 1), (0, 1), (1, 0), (1, 11), (11, 11), (-1, 5)])
def test_part_numbering_outside_one_to_ten_or_above_the_count_is_invalid(part_number: int, part_count: int) -> None:
    assert not is_valid_part_numbering(part_number, part_count)


def test_a_received_set_must_have_one_character_per_part() -> None:
    assert received_set_matches_part_count("101", 3)
    assert not received_set_matches_part_count("11", 3)
    assert not received_set_matches_part_count("1111", 3)


@pytest.mark.parametrize(
    ("received_set", "parts_mask"),
    [("1", 0b1), ("0", 0b0), ("101", 0b101), ("0010", 0b0100), ("1100000000", 0b11), ("1111111111", 0b1111111111)],
)
def test_received_sets_and_parts_masks_convert_both_ways(received_set: str, parts_mask: int) -> None:
    assert convert_received_set_to_parts_mask(received_set) == parts_mask
    assert convert_parts_mask_to_received_set(parts_mask, len(received_set)) == received_set


@pytest.mark.parametrize("received_set", ["", "12", "1 1", "1" * 11])
def test_a_malformed_received_set_is_not_converted(received_set: str) -> None:
    with pytest.raises(ValueError, match="received-set"):
        convert_received_set_to_parts_mask(received_set)


@pytest.mark.parametrize(("parts_mask", "part_count"), [(0b1000, 3), (-1, 3), (0, 0), (0, 11)])
def test_a_parts_mask_that_does_not_fit_the_part_count_is_not_converted(parts_mask: int, part_count: int) -> None:
    with pytest.raises(ValueError, match="parts"):
        convert_parts_mask_to_received_set(parts_mask, part_count)


def test_the_all_parts_mask_has_one_bit_per_part() -> None:
    assert calculate_all_parts_mask(1) == 0b1
    assert calculate_all_parts_mask(3) == 0b111
    assert calculate_all_parts_mask(10) == 0b1111111111


@pytest.mark.parametrize(("received_set", "is_complete"), [("1", True), ("111", True), ("101", False), ("0", False)])
def test_only_all_ones_is_a_complete_received_set(received_set: str, is_complete: bool) -> None:
    assert is_complete_received_set(received_set) is is_complete
