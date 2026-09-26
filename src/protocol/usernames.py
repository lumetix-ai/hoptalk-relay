"""The username rules of protocol section 5.1: 3 to 16 of A-Z, a-z and 0-9, compared case-insensitively.

The parser rejects a username that breaks them as a syntax error, so the services receive only
valid usernames and look them up by normalize_username_for_lookup().
"""

import re

from protocol.constants import USERNAME_PATTERN

USERNAME_REGULAR_EXPRESSION = re.compile(USERNAME_PATTERN)


def is_valid_username(username: str) -> bool:
    return USERNAME_REGULAR_EXPRESSION.fullmatch(username) is not None


def normalize_username_for_lookup(username: str) -> str:
    """Return the value users.username_lookup holds for the same username, in whatever case it was sent.

    The column is PostgreSQL's lower(username); for the ASCII letters and digits of a valid
    username, str.lower() gives exactly the same value.
    """
    return username.lower()
