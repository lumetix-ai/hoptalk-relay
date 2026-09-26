import random
from collections.abc import Sequence

import pytest

from protocol.constants import ErrorCode
from protocol.error_replies import build_grammar_error_reply, build_request_error_reply
from protocol.formatting import InvalidDirectMessageTextError, format_protocol_message, format_server_message
from protocol.message_types import (
    AccountRequest,
    DeliveryAcknowledgement,
    DeliveryPart,
    MessagePartRequest,
    OtherText,
    ParsedDirectMessage,
    ProtocolSyntaxError,
    UnknownMessageType,
    UnsupportedVersion,
)
from protocol.parsing import parse_direct_message_text
from protocol.passwords import is_valid_password, normalize_password
from protocol.received_sets import is_valid_part_numbering, received_set_matches_part_count
from protocol.text_validation import is_valid_part_text
from tests.protocol.formatting_cases import VALID_FORMATTING_CASES, FormattingCase
from tests.protocol.parsing_cases import PARSING_CASES, ParsingCase

FUZZ_SEED = 20260926
FUZZ_ITERATIONS = 20_000
FUZZ_INSERTED_CHARACTERS = (" ", " ", "\t", "0", "1", "9", "/", "*", "?", "_", "a", "Z", "D", "Ж", "\x00")
FUZZ_MAXIMUM_MUTATIONS = 3


def generate_fuzz_direct_message_text(random_generator: random.Random, base_texts: Sequence[str]) -> str:
    """A known text with a few characters deleted, inserted or replaced, so that it often stays nearly valid."""
    characters = list(random_generator.choice(base_texts))
    for _mutation_index in range(random_generator.randint(0, FUZZ_MAXIMUM_MUTATIONS)):
        position = random_generator.randint(0, len(characters))
        mutation = random_generator.choice(("delete", "insert", "replace"))
        if mutation == "insert" or position == len(characters):
            characters.insert(position, random_generator.choice(FUZZ_INSERTED_CHARACTERS))
        elif mutation == "delete":
            del characters[position]
        else:
            characters[position] = random_generator.choice(FUZZ_INSERTED_CHARACTERS)
    return "".join(characters)


def find_value_rule_error(parsed_direct_message: ParsedDirectMessage) -> ErrorCode | None:
    match parsed_direct_message:
        case MessagePartRequest() | DeliveryPart():
            part_numbering_is_valid = is_valid_part_numbering(
                parsed_direct_message.part_number,
                parsed_direct_message.part_count,
            )
            if not part_numbering_is_valid or not is_valid_part_text(parsed_direct_message.part_text):
                return ErrorCode.PART_INVALID
            return None
        case AccountRequest():
            if not is_valid_password(normalize_password(parsed_direct_message.password)):
                return ErrorCode.PASSWORD_INVALID
            return None
        case _:
            return None


def build_expected_error_reply_text(parsed_direct_message: ParsedDirectMessage) -> str | None:
    grammar_error_reply = build_grammar_error_reply(parsed_direct_message)
    if grammar_error_reply is not None:
        return format_server_message(grammar_error_reply)
    value_rule_error = find_value_rule_error(parsed_direct_message)
    if value_rule_error is None:
        return None
    assert isinstance(parsed_direct_message, AccountRequest | MessagePartRequest)
    return format_server_message(build_request_error_reply(value_rule_error, parsed_direct_message))


@pytest.mark.parametrize("parsing_case", PARSING_CASES, ids=lambda parsing_case: parsing_case.description)
def test_parsing_gives_the_expected_result(parsing_case: ParsingCase) -> None:
    assert parse_direct_message_text(parsing_case.direct_message_text) == parsing_case.expected_result


@pytest.mark.parametrize("parsing_case", PARSING_CASES, ids=lambda parsing_case: parsing_case.description)
def test_a_well_formed_message_breaks_exactly_the_expected_value_rule(parsing_case: ParsingCase) -> None:
    parsed_direct_message = parse_direct_message_text(parsing_case.direct_message_text)
    assert find_value_rule_error(parsed_direct_message) == parsing_case.expected_value_rule_error


@pytest.mark.parametrize("parsing_case", PARSING_CASES, ids=lambda parsing_case: parsing_case.description)
def test_the_text_earns_exactly_the_expected_error_reply(parsing_case: ParsingCase) -> None:
    parsed_direct_message = parse_direct_message_text(parsing_case.direct_message_text)
    assert build_expected_error_reply_text(parsed_direct_message) == parsing_case.expected_error_reply_text


ACKNOWLEDGEMENT_CASES = [
    parsing_case for parsing_case in PARSING_CASES if parsing_case.acknowledged_message_part_count is not None
]


@pytest.mark.parametrize("parsing_case", ACKNOWLEDGEMENT_CASES, ids=lambda parsing_case: parsing_case.description)
def test_an_acknowledgement_is_ignored_exactly_when_its_set_length_differs_from_the_part_count(
    parsing_case: ParsingCase,
) -> None:
    parsed_direct_message = parse_direct_message_text(parsing_case.direct_message_text)
    assert isinstance(parsed_direct_message, DeliveryAcknowledgement)
    assert parsing_case.acknowledged_message_part_count is not None
    set_matches = received_set_matches_part_count(
        parsed_direct_message.received_set,
        parsing_case.acknowledged_message_part_count,
    )
    assert set_matches is not parsing_case.expected_acknowledgement_ignored


ROUND_TRIP_CASES = [
    formatting_case for formatting_case in VALID_FORMATTING_CASES if formatting_case.parses_back_to_the_same_message
]


@pytest.mark.parametrize("formatting_case", ROUND_TRIP_CASES, ids=lambda formatting_case: formatting_case.description)
def test_every_formatted_message_parses_back_to_the_same_value(formatting_case: FormattingCase) -> None:
    assert formatting_case.expected_text is not None
    assert parse_direct_message_text(formatting_case.expected_text) == formatting_case.message


def test_a_lone_surrogate_in_a_part_text_breaks_the_grammar() -> None:
    parsed_direct_message = parse_direct_message_text("HT1 M Bob 5 1/1 a\ud800b")
    assert parsed_direct_message == ProtocolSyntaxError(request_type="M", correlation_fields=("Bob", "5"))


def test_a_lone_surrogate_in_a_password_breaks_the_grammar() -> None:
    parsed_direct_message = parse_direct_message_text("HT1 A bob hunter2222\udfff")
    assert parsed_direct_message == ProtocolSyntaxError(request_type="A", correlation_fields=("bob",))


def test_parsing_classifies_arbitrary_text_without_raising_and_accepts_only_canonical_messages() -> None:
    base_texts = [parsing_case.direct_message_text for parsing_case in PARSING_CASES]
    base_texts += [
        formatting_case.expected_text for formatting_case in ROUND_TRIP_CASES if formatting_case.expected_text
    ]
    random_generator = random.Random(FUZZ_SEED)
    for iteration in range(FUZZ_ITERATIONS):
        direct_message_text = generate_fuzz_direct_message_text(random_generator, base_texts)
        failure_context = f"seed {FUZZ_SEED}, iteration {iteration}, text {direct_message_text!r}"

        parsed_direct_message = parse_direct_message_text(direct_message_text)

        if isinstance(parsed_direct_message, OtherText | UnsupportedVersion | UnknownMessageType | ProtocolSyntaxError):
            continue
        try:
            formatted_text = format_protocol_message(parsed_direct_message)
        except InvalidDirectMessageTextError:
            assert find_value_rule_error(parsed_direct_message) is not None, failure_context
            continue
        assert find_value_rule_error(parsed_direct_message) is None, failure_context
        assert formatted_text == direct_message_text, failure_context
