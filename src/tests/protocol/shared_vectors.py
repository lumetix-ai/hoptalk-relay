"""The test vectors shared with clients: tests/protocol/vectors/*.json, generated from the protocol test cases.

Regenerate them after changing a case:

    make --directory=src container-execute command="python -m tests.protocol.shared_vectors"
"""

import dataclasses
import json
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from protocol.constants import MAXIMUM_PART_COUNT, PART_TEXT_MAXIMUM_BYTES
from protocol.message_types import (
    ClientMessage,
    OtherText,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    ServerMessage,
    UnknownMessageType,
    UnsupportedVersion,
)
from protocol.text_validation import count_utf8_bytes
from tests.protocol.formatting_cases import FORMATTING_CASES, FormattingCase
from tests.protocol.parsing_cases import PARSING_CASES, ParsingCase
from tests.protocol.splitting_cases import SPLITTING_CASES, SplittingCase

VECTORS_DIRECTORY = Path(__file__).parent / "vectors"
REGENERATION_COMMAND = 'make --directory=src container-execute command="python -m tests.protocol.shared_vectors"'
GENERATED_BY = f"src/tests/protocol/shared_vectors.py; do not edit by hand, regenerate with: {REGENERATION_COMMAND}"
FIELD_NOTES = (
    "Field names are those of the server's message types. message_id is a string of decimal digits, because a "
    "16-digit id can exceed 2^53, the largest integer that many JSON decoders keep exactly; every other number is a "
    "JSON number."
)

type JsonValue = str | int | bool | list[JsonValue] | dict[str, JsonValue] | None


# ----------------------------------------------------------------------------------------------------------------------
# splitting.json
# ----------------------------------------------------------------------------------------------------------------------


def build_splitting_vectors() -> dict[str, JsonValue]:
    return {
        "description": (
            "Reference splitting of a message text into parts (protocol section 8.1): greedy packing into parts of at "
            "most part_text_maximum_bytes bytes, keeping extended grapheme clusters (UAX #29) whole when that needs no "
            "extra part. A case has either parts (with their UTF-8 byte counts) or an error: invalid_text (empty, or a "
            "character no part text allows) or message_too_long (more than maximum_part_count parts)."
        ),
        "generated_by": GENERATED_BY,
        "part_text_maximum_bytes": PART_TEXT_MAXIMUM_BYTES,
        "maximum_part_count": MAXIMUM_PART_COUNT,
        "cases": [describe_splitting_case(splitting_case) for splitting_case in SPLITTING_CASES],
    }


def describe_splitting_case(splitting_case: SplittingCase) -> dict[str, JsonValue]:
    described_case: dict[str, JsonValue] = {
        "description": splitting_case.description,
        "text": splitting_case.message_text,
        "text_utf8_byte_length": count_utf8_bytes(splitting_case.message_text),
    }
    if splitting_case.expected_failure is not None:
        described_case["error"] = str(splitting_case.expected_failure)
        return described_case
    described_case["parts"] = list(splitting_case.expected_parts)
    described_case["part_utf8_byte_lengths"] = [count_utf8_bytes(part) for part in splitting_case.expected_parts]
    return described_case


# ----------------------------------------------------------------------------------------------------------------------
# formatting.json
# ----------------------------------------------------------------------------------------------------------------------


def build_formatting_vectors() -> dict[str, JsonValue]:
    return {
        "description": (
            "Message fields and the exact direct message text they format to, with its UTF-8 byte count, including "
            "the worst case of every type. A case with error invalid_direct_message must be refused: its fields "
            "break the grammar or a value rule, or the text would exceed 150 bytes."
        ),
        "generated_by": GENERATED_BY,
        "field_notes": FIELD_NOTES,
        "cases": [describe_formatting_case(formatting_case) for formatting_case in FORMATTING_CASES],
    }


def describe_formatting_case(formatting_case: FormattingCase) -> dict[str, JsonValue]:
    described_case: dict[str, JsonValue] = {
        "description": formatting_case.description,
        "message_type": str(formatting_case.message.message_type),
        "fields": describe_message_fields(formatting_case.message),
    }
    if formatting_case.expected_text is None:
        described_case["error"] = "invalid_direct_message"
        return described_case
    described_case["text"] = formatting_case.expected_text
    described_case["utf8_byte_length"] = formatting_case.expected_utf8_byte_length
    return described_case


# ----------------------------------------------------------------------------------------------------------------------
# parsing.json
# ----------------------------------------------------------------------------------------------------------------------


def build_parsing_vectors() -> dict[str, JsonValue]:
    return {
        "description": (
            "Direct message texts and what a strict parser makes of them. result.kind is message (with message_type "
            "and fields), syntax_error (with request_type, the letter after 'HT1 ' or '?', and correlation_fields, "
            "the leading usernames, ids or refresh targets that parsed), unsupported_version, unknown_message_type or "
            "other_text. value_rule_error names the rule a well-formed request breaks. error_reply is the error the "
            "server answers because of the text itself (for a part's value rule, once the device is signed in and "
            "the recipient exists); null means the text earns no error reply. A K case with "
            "acknowledged_message_part_count says whether the server ignores it for a message of that many parts."
        ),
        "generated_by": GENERATED_BY,
        "field_notes": FIELD_NOTES,
        "cases": [describe_parsing_case(parsing_case) for parsing_case in PARSING_CASES],
    }


def describe_parsing_case(parsing_case: ParsingCase) -> dict[str, JsonValue]:
    described_case: dict[str, JsonValue] = {
        "description": parsing_case.description,
        "text": parsing_case.direct_message_text,
        "utf8_byte_length": count_utf8_bytes(parsing_case.direct_message_text),
        "result": describe_parsed_direct_message(parsing_case.expected_result),
        "value_rule_error": describe_optional_text(parsing_case.expected_value_rule_error),
        "error_reply": parsing_case.expected_error_reply_text,
    }
    if parsing_case.acknowledged_message_part_count is not None:
        described_case["acknowledged_message_part_count"] = parsing_case.acknowledged_message_part_count
        described_case["acknowledgement_ignored"] = parsing_case.expected_acknowledgement_ignored
    return described_case


def describe_parsed_direct_message(parsed_direct_message: ParsedDirectMessage) -> dict[str, JsonValue]:
    match parsed_direct_message:
        case OtherText():
            return {"kind": "other_text"}
        case UnsupportedVersion():
            return {
                "kind": "unsupported_version",
                "version": parsed_direct_message.version,
                "next_character": parsed_direct_message.next_character,
            }
        case UnknownMessageType():
            return {"kind": "unknown_message_type", "message_type_letter": parsed_direct_message.message_type_letter}
        case ProtocolSyntaxError():
            return {
                "kind": "syntax_error",
                "request_type": parsed_direct_message.request_type,
                "correlation_fields": list(parsed_direct_message.correlation_fields),
            }
        case _:
            return {
                "kind": "message",
                "message_type": str(parsed_direct_message.message_type),
                "fields": describe_message_fields(parsed_direct_message),
            }


# ----------------------------------------------------------------------------------------------------------------------
# Shared helpers and the files
# ----------------------------------------------------------------------------------------------------------------------


def describe_message_fields(message: ClientMessage | ServerMessage) -> dict[str, JsonValue]:
    described_fields: dict[str, JsonValue] = {}
    for message_field in dataclasses.fields(message):
        field_value = getattr(message, message_field.name)
        if message_field.name == "message_id" or isinstance(field_value, StrEnum):
            described_fields[message_field.name] = str(field_value)
        else:
            described_fields[message_field.name] = field_value
    return described_fields


def describe_optional_text(value: StrEnum | None) -> str | None:
    return None if value is None else str(value)


def render_vector_file(vector_document: dict[str, JsonValue]) -> str:
    return json.dumps(vector_document, ensure_ascii=False, indent=2) + "\n"


VECTOR_FILE_BUILDERS: dict[str, Callable[[], dict[str, JsonValue]]] = {
    "splitting.json": build_splitting_vectors,
    "formatting.json": build_formatting_vectors,
    "parsing.json": build_parsing_vectors,
}


def write_shared_vector_files() -> None:
    VECTORS_DIRECTORY.mkdir(exist_ok=True)
    for file_name, build_vector_document in VECTOR_FILE_BUILDERS.items():
        vector_file_path = VECTORS_DIRECTORY / file_name
        vector_file_path.write_text(render_vector_file(build_vector_document()), encoding="utf-8")


if __name__ == "__main__":
    write_shared_vector_files()
