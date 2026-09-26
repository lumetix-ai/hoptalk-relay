import random

import pytest

from protocol.constants import MAXIMUM_PART_COUNT, PART_TEXT_MAXIMUM_BYTES
from protocol.message_splitting import MessageTooLongError, pack_units_into_parts, split_message_text
from protocol.text_validation import count_utf8_bytes
from tests.protocol.grapheme_clusters import split_into_grapheme_clusters
from tests.protocol.splitting_cases import (
    COMBINING_ACUTE_ACCENT,
    CYRILLIC_LETTER_ZHE,
    FAMILY,
    FLAG_OF_UKRAINE,
    GRINNING_FACE,
    SPLITTING_CASES,
    SplittingCase,
    SplittingFailure,
)

FUZZ_SEED = 20260925
FUZZ_ITERATIONS = 2_000
FUZZ_UNITS = (
    "a",
    "Z",
    "7",
    " ",
    "\n",
    "\t",
    ".",
    CYRILLIC_LETTER_ZHE,
    "ё",
    "漢",
    "字",
    GRINNING_FACE,
    FAMILY,
    FLAG_OF_UKRAINE,
    "e" + COMBINING_ACUTE_ACCENT,
    "a" + COMBINING_ACUTE_ACCENT * 5,
    "\N{DEVANAGARI LETTER NA}\N{DEVANAGARI VOWEL SIGN I}",
    "\N{HANGUL CHOSEONG KIYEOK}\N{HANGUL JUNGSEONG A}\N{HANGUL JONGSEONG KIYEOK}",
    "\U0001f44d\U0001f3fd",
)
FAILURE_EXCEPTIONS = {
    SplittingFailure.INVALID_TEXT: ValueError,
    SplittingFailure.MESSAGE_TOO_LONG: MessageTooLongError,
}

SUCCESSFUL_CASES = [splitting_case for splitting_case in SPLITTING_CASES if splitting_case.expected_failure is None]
FAILING_CASES = [splitting_case for splitting_case in SPLITTING_CASES if splitting_case.expected_failure is not None]


@pytest.mark.parametrize("splitting_case", SUCCESSFUL_CASES, ids=lambda splitting_case: splitting_case.description)
def test_splitting_gives_the_expected_parts(splitting_case: SplittingCase) -> None:
    parts = split_message_text(splitting_case.message_text, split_into_grapheme_clusters)

    assert tuple(parts) == splitting_case.expected_parts


@pytest.mark.parametrize("splitting_case", FAILING_CASES, ids=lambda splitting_case: splitting_case.description)
def test_splitting_refuses_an_invalid_or_too_long_text(splitting_case: SplittingCase) -> None:
    assert splitting_case.expected_failure is not None
    expected_exception = FAILURE_EXCEPTIONS[splitting_case.expected_failure]

    with pytest.raises(expected_exception) as raised_error:
        split_message_text(splitting_case.message_text, split_into_grapheme_clusters)

    if splitting_case.expected_failure is SplittingFailure.INVALID_TEXT:
        assert not isinstance(raised_error.value, MessageTooLongError)


def test_both_packings_of_five_families_need_two_parts_with_different_cut_points() -> None:
    grapheme_parts = pack_units_into_parts(split_into_grapheme_clusters(FAMILY * 5), PART_TEXT_MAXIMUM_BYTES)
    code_point_parts = pack_units_into_parts(FAMILY * 5, PART_TEXT_MAXIMUM_BYTES)

    assert [count_utf8_bytes(part) for part in grapheme_parts] == [100, 25]
    assert [count_utf8_bytes(part) for part in code_point_parts] == [104, 21]


def test_a_too_long_message_reports_its_size_and_the_parts_it_would_need() -> None:
    with pytest.raises(MessageTooLongError) as raised_error:
        split_message_text("a" * 1041, split_into_grapheme_clusters)

    assert raised_error.value.message_text_bytes == 1041
    assert raised_error.value.required_part_count == 11


def test_a_segmenter_that_loses_characters_is_refused() -> None:
    with pytest.raises(ValueError, match="segmenter"):
        split_message_text("hello", lambda text: [text[:-1]])


def test_a_part_budget_smaller_than_the_longest_code_point_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 4 bytes"):
        pack_units_into_parts("abc", 3)


def test_random_mixed_script_texts_split_into_valid_parts_of_the_fewest_count() -> None:
    random_generator = random.Random(FUZZ_SEED)
    for iteration in range(FUZZ_ITERATIONS):
        unit_count = random_generator.randint(1, 400)
        message_text = "".join(random_generator.choices(FUZZ_UNITS, k=unit_count))
        failure_context = f"seed {FUZZ_SEED}, iteration {iteration}, text {message_text!r}"
        fewest_part_count = len(pack_units_into_parts(message_text, PART_TEXT_MAXIMUM_BYTES))

        try:
            parts = split_message_text(message_text, split_into_grapheme_clusters)
        except MessageTooLongError:
            assert fewest_part_count > MAXIMUM_PART_COUNT, failure_context
            continue

        assert "".join(parts) == message_text, failure_context
        assert len(parts) == fewest_part_count <= MAXIMUM_PART_COUNT, failure_context
        for part in parts:
            assert part != "", failure_context
            assert count_utf8_bytes(part) <= PART_TEXT_MAXIMUM_BYTES, failure_context
            part.encode("utf-8").decode("utf-8")
        grapheme_parts = pack_units_into_parts(split_into_grapheme_clusters(message_text), PART_TEXT_MAXIMUM_BYTES)
        if len(grapheme_parts) == fewest_part_count:
            assert parts == grapheme_parts, failure_context
