"""Logging to standard output for the panel and the relay worker ("make relay-logs").

A sign-in request carries a password in clear (HT1 A <username> <password>), and such a text
can end up in a log line or in the traceback of an exception. The formatter redacts it from
the complete formatted record, so no handler ever writes it.

Node and contact names come from the radio, so anyone in range chooses them. The formatter
escapes the control characters in them: a newline would start a forged log line, and an
escape sequence would drive the terminal that shows the log.
"""

import logging
import re
from typing import Any, override

REDACTED_PASSWORD = "********"

# Any protocol version and any first token after "A": a malformed sign-in request carries a
# password just as well. Everything after the username up to the end of the line goes.
ACCOUNT_REQUEST_PATTERN = re.compile(r"(HT[0-9]{1,3} A [^ \n]*)[^\n]*")

# The message line of a record keeps no control character at all, newlines included. The
# traceback appended after it needs its newlines and tabs, and keeps nothing else.
MESSAGE_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")
TERMINAL_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def redact_account_request_passwords(text: str) -> str:
    return ACCOUNT_REQUEST_PATTERN.sub(lambda match: f"{match.group(1)} {REDACTED_PASSWORD}", text)


def escape_control_character(match: re.Match[str]) -> str:
    return f"\\x{ord(match.group()):02x}"


class EscapingRedactingFormatter(logging.Formatter):
    @override
    def formatMessage(self, record: logging.LogRecord) -> str:
        return MESSAGE_CONTROL_CHARACTER_PATTERN.sub(escape_control_character, super().formatMessage(record))

    @override
    def format(self, record: logging.LogRecord) -> str:
        escaped_output = TERMINAL_CONTROL_CHARACTER_PATTERN.sub(escape_control_character, super().format(record))
        return redact_account_request_passwords(escaped_output)


def build_logging_configuration(log_level: str = "INFO") -> dict[str, Any]:
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "escaping_redacting": {
                "()": EscapingRedactingFormatter,
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
            },
        },
        "handlers": {
            "standard_output": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "escaping_redacting",
            },
        },
        "root": {
            "handlers": ["standard_output"],
            "level": log_level,
        },
        "loggers": {
            # Django's default configuration gives this logger handlers of its own, which
            # would print every record a second time and without the escaping and redaction.
            "django": {
                "handlers": ["standard_output"],
                "level": log_level,
                "propagate": False,
            },
        },
    }
