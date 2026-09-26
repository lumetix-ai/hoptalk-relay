"""The password rules of protocol section 5.2, which the account service checks after the grammar.

A password is checked, hashed and verified in Unicode NFC, so the same password typed on two
keyboards matches. One that breaks a rule is answered "e PASSWORD_INVALID A <username>".
"""

import unicodedata

from protocol.constants import PASSWORD_MAXIMUM_BYTES, PASSWORD_MAXIMUM_CHARACTERS, PASSWORD_MINIMUM_CHARACTERS
from protocol.text_validation import contains_surrogate, count_utf8_bytes, is_control_character

PASSWORD_EDGE_CHARACTER_NOT_ALLOWED = " "


def normalize_password(password: str) -> str:
    """Return the password in NFC, the only form that is checked, hashed or verified."""
    return unicodedata.normalize("NFC", password)


def is_valid_password(normalized_password: str) -> bool:
    """Check a password that normalize_password() returned.

    Valid: 8 to 64 characters and at most 64 bytes of UTF-8, no control character (C0, DEL or
    C1), and no space at the start or the end. A character is one Unicode scalar value, so a
    letter with a combining mark that NFC cannot compose counts as two.
    """
    if contains_surrogate(normalized_password):
        return False
    if any(is_control_character(character) for character in normalized_password):
        return False
    if normalized_password.startswith(PASSWORD_EDGE_CHARACTER_NOT_ALLOWED):
        return False
    if normalized_password.endswith(PASSWORD_EDGE_CHARACTER_NOT_ALLOWED):
        return False
    if not PASSWORD_MINIMUM_CHARACTERS <= len(normalized_password) <= PASSWORD_MAXIMUM_CHARACTERS:
        return False
    return count_utf8_bytes(normalized_password) <= PASSWORD_MAXIMUM_BYTES
