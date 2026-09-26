"""The reference splitter of protocol section 8.1, used only to generate the test vectors shared with clients.

The server never splits text: a client does, and the server forwards every part unchanged. The
grapheme cluster segmenter is passed in (the tests use the regex package's \\X, a development
dependency), so the protocol package itself needs no third-party library.
"""

from collections.abc import Callable, Iterable

from protocol.constants import MAXIMUM_PART_COUNT, PART_TEXT_MAXIMUM_BYTES
from protocol.text_validation import count_utf8_bytes, is_allowed_part_text_character

LONGEST_CODE_POINT_BYTES = 4


class MessageTooLongError(ValueError):
    """The text needs more than 10 parts of 104 bytes."""

    def __init__(self, *, message_text_bytes: int, required_part_count: int) -> None:
        super().__init__(
            f"A message of {message_text_bytes} bytes needs {required_part_count} parts; at most "
            f"{MAXIMUM_PART_COUNT} parts of {PART_TEXT_MAXIMUM_BYTES} bytes are allowed."
        )
        self.message_text_bytes = message_text_bytes
        self.required_part_count = required_part_count


class PartPacker:
    """Fills parts greedily: a unit that does not fit into the current part starts the next one."""

    def __init__(self, part_byte_budget: int) -> None:
        self.part_byte_budget = part_byte_budget
        self.finished_parts: list[str] = []
        self.current_part_units: list[str] = []
        self.current_part_bytes = 0

    def add_unit(self, unit: str) -> None:
        unit_bytes = count_utf8_bytes(unit)
        if self.current_part_bytes + unit_bytes > self.part_byte_budget:
            self.finish_current_part()
        self.current_part_units.append(unit)
        self.current_part_bytes += unit_bytes

    def finish_current_part(self) -> None:
        if self.current_part_units:
            self.finished_parts.append("".join(self.current_part_units))
        self.current_part_units = []
        self.current_part_bytes = 0

    def finish(self) -> list[str]:
        self.finish_current_part()
        return self.finished_parts


def split_message_text(
    message_text: str,
    split_into_grapheme_clusters: Callable[[str], list[str]],
) -> list[str]:
    """Split a text into the fewest parts, keeping grapheme clusters whole when that costs no extra part.

    The text must be non-empty, hold only characters a part text allows and have its line
    endings already turned into line feeds; anything else raises ValueError. A text that needs
    more than 10 parts raises MessageTooLongError.
    """
    require_splittable_message_text(message_text)
    grapheme_clusters = split_into_grapheme_clusters(message_text)
    if "".join(grapheme_clusters) != message_text:
        raise ValueError("The grapheme cluster segmenter must return pieces that join back into the text.")

    code_point_parts = pack_units_into_parts(message_text, PART_TEXT_MAXIMUM_BYTES)
    grapheme_parts = pack_units_into_parts(grapheme_clusters, PART_TEXT_MAXIMUM_BYTES)
    parts = grapheme_parts if len(grapheme_parts) == len(code_point_parts) else code_point_parts
    if len(parts) > MAXIMUM_PART_COUNT:
        raise MessageTooLongError(message_text_bytes=count_utf8_bytes(message_text), required_part_count=len(parts))
    return parts


def pack_units_into_parts(units: Iterable[str], part_byte_budget: int) -> list[str]:
    """Pack units (code points or grapheme clusters) greedily, cutting only a unit longer than a whole part.

    Iterating a str packs its code points. A unit longer than the budget (a grapheme cluster of
    more than 104 bytes) is packed code point by code point, so no code point is ever cut and
    no part is ever empty.
    """
    if part_byte_budget < LONGEST_CODE_POINT_BYTES:
        raise ValueError(f"A part must have room for any code point: at least {LONGEST_CODE_POINT_BYTES} bytes.")
    part_packer = PartPacker(part_byte_budget)
    for unit in units:
        if count_utf8_bytes(unit) > part_byte_budget:
            for code_point in unit:
                part_packer.add_unit(code_point)
        else:
            part_packer.add_unit(unit)
    return part_packer.finish()


def require_splittable_message_text(message_text: str) -> None:
    if message_text == "":
        raise ValueError("An empty text has no parts.")
    if not all(is_allowed_part_text_character(character) for character in message_text):
        raise ValueError(
            "The text holds a character no part text allows: a control character other than tab and line feed "
            "(turn CR LF and CR into LF first) or a lone surrogate."
        )
