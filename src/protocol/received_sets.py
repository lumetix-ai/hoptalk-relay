"""Part numbers and received-sets (protocol section 5.4).

A received-set has one character per part of the message, left to right: "1" when that part is
held, "0" when it is missing; all ones means the whole message is held. The database keeps the
same information as a mask in which bit n-1 stands for part n.
"""

from protocol.constants import MAXIMUM_PART_COUNT, RECEIVED_SET_MISSING, RECEIVED_SET_RECEIVED
from protocol.field_grammar import is_received_set_field

FIRST_PART_NUMBER = 1


def is_valid_part_numbering(part_number: int, part_count: int) -> bool:
    """Valid: 1 <= part_number <= part_count <= 10. A request that breaks it is answered "e PART_INVALID"."""
    return FIRST_PART_NUMBER <= part_number <= part_count <= MAXIMUM_PART_COUNT


def received_set_matches_part_count(received_set: str, part_count: int) -> bool:
    """The grammar allows 1 to 10 characters; an acknowledgement whose length differs from part_count is dropped."""
    return len(received_set) == part_count


def calculate_all_parts_mask(part_count: int) -> int:
    """The mask of a message whose every part is held: bits 0 to part_count - 1 set."""
    require_valid_part_count(part_count)
    return (1 << part_count) - 1


def convert_received_set_to_parts_mask(received_set: str) -> int:
    if not is_received_set_field(received_set):
        raise ValueError(f"Not a received-set: {received_set!r}")
    parts_mask = 0
    for part_index, part_state in enumerate(received_set):
        if part_state == RECEIVED_SET_RECEIVED:
            parts_mask |= 1 << part_index
    return parts_mask


def convert_parts_mask_to_received_set(parts_mask: int, part_count: int) -> str:
    if not 0 <= parts_mask <= calculate_all_parts_mask(part_count):
        raise ValueError(f"The parts mask {parts_mask} does not fit a message of {part_count} parts.")
    part_states = [
        RECEIVED_SET_RECEIVED if parts_mask & (1 << part_index) else RECEIVED_SET_MISSING
        for part_index in range(part_count)
    ]
    return "".join(part_states)


def is_complete_received_set(received_set: str) -> bool:
    return received_set != "" and all(part_state == RECEIVED_SET_RECEIVED for part_state in received_set)


def require_valid_part_count(part_count: int) -> None:
    if not FIRST_PART_NUMBER <= part_count <= MAXIMUM_PART_COUNT:
        raise ValueError(f"A message has 1 to {MAXIMUM_PART_COUNT} parts, not {part_count}.")
