"""Message texts and the parts the reference splitter must produce; exported as vectors/splitting.json."""

from dataclasses import dataclass
from enum import StrEnum


class SplittingFailure(StrEnum):
    INVALID_TEXT = "invalid_text"
    MESSAGE_TOO_LONG = "message_too_long"


@dataclass(frozen=True, kw_only=True)
class SplittingCase:
    description: str
    message_text: str
    expected_parts: tuple[str, ...] = ()
    expected_failure: SplittingFailure | None = None


MAN = "\U0001f468"
ZERO_WIDTH_JOINER = "\N{ZERO WIDTH JOINER}"
# Seven code points, 25 bytes: man, woman, girl and boy joined by zero width joiners.
FAMILY = MAN + ZERO_WIDTH_JOINER + "\U0001f469" + ZERO_WIDTH_JOINER + "\U0001f467" + ZERO_WIDTH_JOINER + "\U0001f466"
FAMILY_AFTER_ITS_FIRST_CODE_POINT = FAMILY.removeprefix(MAN)
# Two regional indicator symbols, 8 bytes.
FLAG_OF_UKRAINE = "\U0001f1fa\U0001f1e6"
GRINNING_FACE = "\U0001f600"
COMBINING_ACUTE_ACCENT = "\N{COMBINING ACUTE ACCENT}"
DECOMPOSED_E_WITH_ACUTE = "e" + COMBINING_ACUTE_ACCENT
CYRILLIC_LETTER_ZHE = "Ж"
CJK_IDEOGRAPH = "漢"

SPLITTING_CASES = (
    SplittingCase(
        description="250 ASCII letters: parts of 104, 104 and 42 bytes",
        message_text="a" * 250,
        expected_parts=("a" * 104, "a" * 104, "a" * 42),
    ),
    SplittingCase(
        description="60 Cyrillic letters (120 bytes): 52 letters (104 bytes) and 8 letters (16 bytes)",
        message_text=CYRILLIC_LETTER_ZHE * 60,
        expected_parts=(CYRILLIC_LETTER_ZHE * 52, CYRILLIC_LETTER_ZHE * 8),
    ),
    SplittingCase(
        description=(
            "5 family emoji (7 code points and 25 bytes each): grapheme packing gives 100 + 25 bytes and code-point "
            "packing 104 + 21 bytes; both need 2 parts, so the grapheme split is used"
        ),
        message_text=FAMILY * 5,
        expected_parts=(FAMILY * 4, FAMILY),
    ),
    SplittingCase(
        description="A short sentence with Cyrillic letters is one part",
        message_text="Привет, Боб!",
        expected_parts=("Привет, Боб!",),
    ),
    SplittingCase(
        description="Tabs, line feeds and leading and trailing spaces are kept",
        message_text=" Hello,\n\tworld  ",
        expected_parts=(" Hello,\n\tworld  ",),
    ),
    SplittingCase(
        description="Exactly 104 bytes is one part",
        message_text="a" * 104,
        expected_parts=("a" * 104,),
    ),
    SplittingCase(
        description="105 bytes need a second part of 1 byte",
        message_text="a" * 105,
        expected_parts=("a" * 104, "a"),
    ),
    SplittingCase(
        description="1 040 bytes fill exactly 10 parts",
        message_text="a" * 1040,
        expected_parts=("a" * 104,) * 10,
    ),
    SplittingCase(
        description="1 041 bytes need 11 parts: too long",
        message_text="a" * 1041,
        expected_failure=SplittingFailure.MESSAGE_TOO_LONG,
    ),
    SplittingCase(
        description="520 Cyrillic letters (1 040 bytes) fill exactly 10 parts",
        message_text=CYRILLIC_LETTER_ZHE * 520,
        expected_parts=(CYRILLIC_LETTER_ZHE * 52,) * 10,
    ),
    SplittingCase(
        description="521 Cyrillic letters (1 042 bytes) are too long",
        message_text=CYRILLIC_LETTER_ZHE * 521,
        expected_failure=SplittingFailure.MESSAGE_TOO_LONG,
    ),
    SplittingCase(
        description="27 four-byte emoji: 26 fit a part (104 bytes)",
        message_text=GRINNING_FACE * 27,
        expected_parts=(GRINNING_FACE * 26, GRINNING_FACE),
    ),
    SplittingCase(
        description="35 three-byte CJK ideographs: 34 fit a part (102 bytes)",
        message_text=CJK_IDEOGRAPH * 35,
        expected_parts=(CJK_IDEOGRAPH * 34, CJK_IDEOGRAPH),
    ),
    SplittingCase(
        description=(
            "A letter and 13 flags (105 bytes): a flag is not cut, because keeping it whole still needs 2 parts "
            "(97 + 8 bytes instead of 101 + 4)"
        ),
        message_text="a" + FLAG_OF_UKRAINE * 13,
        expected_parts=("a" + FLAG_OF_UKRAINE * 12, FLAG_OF_UKRAINE),
    ),
    SplittingCase(
        description=(
            "35 decomposed e-acute letters (3 bytes each): the combining mark stays with its letter (102 + 3 bytes "
            "instead of 103 + 2)"
        ),
        message_text=DECOMPOSED_E_WITH_ACUTE * 35,
        expected_parts=(DECOMPOSED_E_WITH_ACUTE * 34, DECOMPOSED_E_WITH_ACUTE),
    ),
    SplittingCase(
        description=(
            "One grapheme cluster longer than a part (a letter and 60 combining marks, 121 bytes) is cut between "
            "code points: 103 + 18 bytes"
        ),
        message_text="a" + COMBINING_ACUTE_ACCENT * 60,
        expected_parts=("a" + COMBINING_ACUTE_ACCENT * 51, COMBINING_ACUTE_ACCENT * 9),
    ),
    SplittingCase(
        description=(
            "5 family emoji and 83 letters (208 bytes): grapheme packing needs 3 parts and code-point packing 2, so "
            "the code-point split is used and the fifth family is cut after its first code point"
        ),
        message_text=FAMILY * 5 + "a" * 83,
        expected_parts=(FAMILY * 4 + MAN, FAMILY_AFTER_ITS_FIRST_CODE_POINT + "a" * 83),
    ),
    SplittingCase(
        description=(
            "100 letters, a family emoji and 915 letters (1 040 bytes) fit 10 parts only by cutting the family "
            "(grapheme packing would need 11)"
        ),
        message_text="a" * 100 + FAMILY + "a" * 915,
        expected_parts=(
            "a" * 100 + MAN,
            FAMILY_AFTER_ITS_FIRST_CODE_POINT + "a" * 83,
            *(("a" * 104,) * 8),
        ),
    ),
    SplittingCase(
        description="An empty text cannot be sent",
        message_text="",
        expected_failure=SplittingFailure.INVALID_TEXT,
    ),
    SplittingCase(
        description="A carriage return must be turned into a line feed before splitting",
        message_text="line one\r\nline two",
        expected_failure=SplittingFailure.INVALID_TEXT,
    ),
    SplittingCase(
        description="NUL is not allowed",
        message_text="a\x00b",
        expected_failure=SplittingFailure.INVALID_TEXT,
    ),
    SplittingCase(
        description="The escape control character is not allowed",
        message_text="\x1b[1mbold",
        expected_failure=SplittingFailure.INVALID_TEXT,
    ),
    SplittingCase(
        description="A C1 control character (U+0085, next line) is not allowed",
        message_text="one\x85two",
        expected_failure=SplittingFailure.INVALID_TEXT,
    ),
)
