"""Every fixed value of the HT1 protocol (docs/protocol.md): limits, type letters, error codes.

The retry strategy and the delivered-receipt hold-back are not here: the operator configures
them in src/.env (hoptalk_relay/relay_settings.py).
"""

from enum import StrEnum

PROTOCOL_VERSION = "1"
PROTOCOL_MAGIC = "HT"
PROTOCOL_PREFIX = f"{PROTOCOL_MAGIC}{PROTOCOL_VERSION} "
VERSION_NUMBER_MAXIMUM_DIGITS = 3
# Rule 3 of protocol section 3: "HT", one to three version digits, one space.
PROTOCOL_TRAFFIC_PATTERN = f"^HT([0-9]{{1,{VERSION_NUMBER_MAXIMUM_DIGITS}}}) "

# Byte budget (protocol section 7). Every length is in bytes of UTF-8.
DIRECT_MESSAGE_MAXIMUM_BYTES = 150
PART_TEXT_MAXIMUM_BYTES = 104
PART_TEXT_MINIMUM_BYTES = 1
MAXIMUM_PART_COUNT = 10
PART_HEADER_MAXIMUM_BYTES = 46

# Usernames (protocol section 5.1): A-Z, a-z, 0-9, compared case-insensitively.
USERNAME_MINIMUM_LENGTH = 3
USERNAME_MAXIMUM_LENGTH = 16
USERNAME_PATTERN = f"[A-Za-z0-9]{{{USERNAME_MINIMUM_LENGTH},{USERNAME_MAXIMUM_LENGTH}}}"

# Passwords (protocol section 5.2), counted after NFC normalisation.
PASSWORD_MINIMUM_CHARACTERS = 8
PASSWORD_MAXIMUM_CHARACTERS = 64
PASSWORD_MAXIMUM_BYTES = 64

# Message ids (protocol section 5.3): 1 to 16 decimal digits, no leading zero.
MESSAGE_ID_MINIMUM = 1
MESSAGE_ID_MAXIMUM = 9_999_999_999_999_999
MESSAGE_ID_MAXIMUM_DIGITS = 16

# Control characters (sections 5.2 and 5.5): C0, DEL and C1, as inclusive code point ranges.
# Passwords allow none of them; a part text allows only tab and line feed.
CONTROL_CHARACTER_RANGES = ((0x00, 0x1F), (0x7F, 0x9F))
PART_TEXT_ALLOWED_CONTROL_CHARACTERS = frozenset({"\t", "\n"})

# Refresh (protocol section 6.1): "F *" refreshes every conversation; "f" reports at most 9999.
REFRESH_ALL_PEERS_TARGET = "*"
REFRESH_REPLY_MAXIMUM_MESSAGE_COUNT = 9999

# The request type of an error whose request letter could not be determined (section 10.1).
UNKNOWN_REQUEST_TYPE = "?"
ERROR_CODE_MAXIMUM_LENGTH = 16

RECEIVED_SET_RECEIVED = "1"
RECEIVED_SET_MISSING = "0"
USER_EXISTS = "1"
USER_DOES_NOT_EXIST = "0"

# Server behaviour a client relies on (protocol section 13.2), fixed rather than configured.
INCOMPLETE_SEND_STATUS_COALESCING_SECONDS = 5
INCOMPLETE_MESSAGE_RETENTION_HOURS = 24
IDENTICAL_REPLY_SUPPRESSION_SECONDS = 10
FIRMWARE_REPEAT_WINDOW_HOURS = 24
SIGN_IN_FAILURES_BEFORE_RATE_LIMIT = 5
SIGN_IN_FAILURE_WINDOW_MINUTES = 15


class ClientMessageType(StrEnum):
    """Upper-case letters: from a client to the server."""

    ACCOUNT_REQUEST = "A"
    QUERY_REQUEST = "Q"
    MESSAGE_PART_REQUEST = "M"
    DELIVERY_ACKNOWLEDGEMENT = "K"
    READ_REQUEST = "R"
    RECEIPT_ACKNOWLEDGEMENT = "C"
    REFRESH_REQUEST = "F"


class ServerMessageType(StrEnum):
    """Lower-case letters: from the server to a client."""

    ACCOUNT_REPLY = "a"
    QUERY_REPLY = "q"
    SEND_STATUS_REPLY = "k"
    DELIVERY_PART = "m"
    READ_REPLY = "r"
    RECEIPT_PUSH = "s"
    REFRESH_REPLY = "f"
    ERROR_REPLY = "e"


# Requests are retried by the client until answered; acknowledgements are never answered.
REQUEST_TYPES = frozenset(
    {
        ClientMessageType.ACCOUNT_REQUEST,
        ClientMessageType.QUERY_REQUEST,
        ClientMessageType.MESSAGE_PART_REQUEST,
        ClientMessageType.READ_REQUEST,
        ClientMessageType.REFRESH_REQUEST,
    }
)
ACKNOWLEDGEMENT_TYPES = frozenset(
    {
        ClientMessageType.DELIVERY_ACKNOWLEDGEMENT,
        ClientMessageType.RECEIPT_ACKNOWLEDGEMENT,
    }
)


class ReceiptLevel(StrEnum):
    """R implies D (protocol section 5.6)."""

    DELIVERED = "D"
    READ = "R"


# The receipt levels as the database stores them (receipt_notifications.target_level).
RECEIPT_LEVEL_NUMBERS = {ReceiptLevel.DELIVERED: 1, ReceiptLevel.READ: 2}
RECEIPT_LEVELS_BY_NUMBER = {
    level_number: receipt_level for receipt_level, level_number in RECEIPT_LEVEL_NUMBERS.items()
}


class ErrorCode(StrEnum):
    """The twelve error codes of protocol section 10.2."""

    SYNTAX = "SYNTAX"
    VERSION = "VERSION"
    UNSUPPORTED = "UNSUPPORTED"
    NOT_SIGNED_IN = "NOT_SIGNED_IN"
    PASSWORD_INVALID = "PASSWORD_INVALID"
    WRONG_PASSWORD = "WRONG_PASSWORD"
    RATE_LIMITED = "RATE_LIMITED"
    NO_SUCH_USER = "NO_SUCH_USER"
    SELF = "SELF"
    ID_CONFLICT = "ID_CONFLICT"
    PART_INVALID = "PART_INVALID"
    NOT_FOUND = "NOT_FOUND"
