"""The shape of every field of protocol section 4.1, shared by the parser and the formatters.

These checks are the grammar only. Value rules (part ranges, part text, passwords) live in
received_sets, text_validation and passwords, because they have error codes of their own.
"""

import re

from protocol.constants import (
    ERROR_CODE_MAXIMUM_LENGTH,
    MAXIMUM_PART_COUNT,
    MESSAGE_ID_MAXIMUM_DIGITS,
    REFRESH_ALL_PEERS_TARGET,
    VERSION_NUMBER_MAXIMUM_DIGITS,
)
from protocol.text_validation import is_surrogate
from protocol.usernames import is_valid_username

MESSAGE_ID_REGULAR_EXPRESSION = re.compile(f"[1-9][0-9]{{0,{MESSAGE_ID_MAXIMUM_DIGITS - 1}}}")
PART_FIELD_REGULAR_EXPRESSION = re.compile(r"(?P<part_number>[1-9][0-9]?)/(?P<part_count>[1-9][0-9]?)")
RECEIVED_SET_REGULAR_EXPRESSION = re.compile(f"[01]{{1,{MAXIMUM_PART_COUNT}}}")
RECEIPT_LEVEL_REGULAR_EXPRESSION = re.compile(r"[DR]")
EXISTENCE_REGULAR_EXPRESSION = re.compile(r"[01]")
MESSAGE_COUNT_REGULAR_EXPRESSION = re.compile(r"0|[1-9][0-9]{0,3}")
ERROR_CODE_REGULAR_EXPRESSION = re.compile(f"[A-Z_]{{1,{ERROR_CODE_MAXIMUM_LENGTH}}}")
REQUEST_TYPE_REGULAR_EXPRESSION = re.compile(r"[A-Z?]")
VERSION_NUMBER_REGULAR_EXPRESSION = re.compile(f"[0-9]{{1,{VERSION_NUMBER_MAXIMUM_DIGITS}}}")

FIELD_SEPARATOR = " "
PART_FIELD_SEPARATOR = "/"
NUL_CHARACTER = "\x00"


def is_message_id_field(field: str) -> bool:
    return MESSAGE_ID_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_part_field(field: str) -> bool:
    """The "<number>/<count>" shape: one or two digits each, no leading zero; the range 1-10 is a value rule."""
    return PART_FIELD_REGULAR_EXPRESSION.fullmatch(field) is not None


def read_part_field(field: str) -> tuple[int, int]:
    """Return (part_number, part_count) of a field that is_part_field() accepted."""
    part_field_match = PART_FIELD_REGULAR_EXPRESSION.fullmatch(field)
    if part_field_match is None:
        raise ValueError(f"Not a part field: {field!r}")
    return int(part_field_match.group("part_number")), int(part_field_match.group("part_count"))


def is_received_set_field(field: str) -> bool:
    return RECEIVED_SET_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_receipt_level_field(field: str) -> bool:
    return RECEIPT_LEVEL_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_existence_field(field: str) -> bool:
    return EXISTENCE_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_message_count_field(field: str) -> bool:
    return MESSAGE_COUNT_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_refresh_target_field(field: str) -> bool:
    return field == REFRESH_ALL_PEERS_TARGET or is_valid_username(field)


def is_error_code_field(field: str) -> bool:
    return ERROR_CODE_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_request_type_field(field: str) -> bool:
    return REQUEST_TYPE_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_version_number_field(field: str) -> bool:
    return VERSION_NUMBER_REGULAR_EXPRESSION.fullmatch(field) is not None


def is_error_reference_field(error_reference: str) -> bool:
    """One of "<username> <message id>", "<username>", "*" or a version number of 1 to 3 digits."""
    reference_fields = error_reference.split(FIELD_SEPARATOR)
    if len(reference_fields) == 2:
        username, message_id = reference_fields
        return is_valid_username(username) and is_message_id_field(message_id)
    if len(reference_fields) == 1:
        (single_field,) = reference_fields
        return is_refresh_target_field(single_field) or is_version_number_field(single_field)
    return False


def is_tail_field(tail: str) -> bool:
    """A password or part text: any Unicode scalar values except NUL, spaces included, possibly empty."""
    return not any(character == NUL_CHARACTER or is_surrogate(character) for character in tail)
