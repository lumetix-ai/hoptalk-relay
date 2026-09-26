"""Character and byte rules of the protocol, and the part text rules of protocol section 5.5.

A part text that breaks them is answered "e PART_INVALID", never "e SYNTAX". A valid one is
stored and forwarded byte for byte.
"""

from protocol.constants import (
    CONTROL_CHARACTER_RANGES,
    PART_TEXT_ALLOWED_CONTROL_CHARACTERS,
    PART_TEXT_MAXIMUM_BYTES,
    PART_TEXT_MINIMUM_BYTES,
)

SURROGATE_CODE_POINT_RANGE = (0xD800, 0xDFFF)


def count_utf8_bytes(text: str) -> int:
    """Raises UnicodeEncodeError for a text that holds a lone surrogate, which UTF-8 cannot encode."""
    return len(text.encode("utf-8"))


def is_control_character(character: str) -> bool:
    """C0 (U+0000 to U+001F), DEL (U+007F) and C1 (U+0080 to U+009F)."""
    code_point = ord(character)
    return any(range_start <= code_point <= range_end for range_start, range_end in CONTROL_CHARACTER_RANGES)


def is_surrogate(character: str) -> bool:
    """A lone surrogate is a code point but not a Unicode scalar value; it cannot travel as UTF-8."""
    range_start, range_end = SURROGATE_CODE_POINT_RANGE
    return range_start <= ord(character) <= range_end


def contains_surrogate(text: str) -> bool:
    return any(is_surrogate(character) for character in text)


def is_allowed_part_text_character(character: str) -> bool:
    """Tab, line feed, U+0020 to U+007E, and every Unicode scalar value from U+00A0 on."""
    if character in PART_TEXT_ALLOWED_CONTROL_CHARACTERS:
        return True
    return not is_control_character(character) and not is_surrogate(character)


def is_valid_part_text(part_text: str) -> bool:
    """Valid: 1 to 104 bytes of UTF-8, Unicode scalar values only, no control character except tab and line feed."""
    if not all(is_allowed_part_text_character(character) for character in part_text):
        return False
    return PART_TEXT_MINIMUM_BYTES <= count_utf8_bytes(part_text) <= PART_TEXT_MAXIMUM_BYTES
