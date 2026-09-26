"""Reading src/.env, the application's own configuration file.

Compose would expand every "$" of an Argon2 hash, so the file is mounted into the containers
and parsed here instead, without any expansion: everything after the first "=" is the value,
with surrounding whitespace and one pair of matching quotes removed. A variable in the real
process environment wins over the same name in the file, which lets Compose, continuous
integration and one-off commands override single values.
"""

import os
import re
from pathlib import Path

VARIABLE_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
QUOTE_CHARACTERS = ("'", '"')


class InvalidConfigurationError(Exception):
    """The configuration cannot be used; the message names every variable that is wrong.

    It deliberately does not derive from ImproperlyConfigured: Django's command-line entry
    point swallows that one and reports an unrelated "unknown command" instead.
    """


def load_environment_values(environment_file_path: Path) -> dict[str, str]:
    file_values = read_environment_file(environment_file_path) if environment_file_path.is_file() else {}
    return {**file_values, **os.environ}


def read_environment_file(environment_file_path: Path) -> dict[str, str]:
    environment_file_text = environment_file_path.read_text(encoding="utf-8")
    return parse_environment_file_text(environment_file_text, environment_file_name=str(environment_file_path))


def parse_environment_file_text(environment_file_text: str, environment_file_name: str) -> dict[str, str]:
    values: dict[str, str] = {}

    for line_number, line in enumerate(environment_file_text.splitlines(), start=1):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue

        name, separator, raw_value = stripped_line.partition("=")
        name = name.removeprefix("export ").strip()

        # The line itself is left out of the message: it may hold a secret.
        if not separator or not VARIABLE_NAME_PATTERN.match(name):
            raise InvalidConfigurationError(
                f"{environment_file_name}, line {line_number}: expected NAME=value, such as TIME_ZONE=UTC."
            )

        values[name] = remove_matching_quotes(raw_value.strip())

    return values


def remove_matching_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in QUOTE_CHARACTERS:
        return value[1:-1]
    return value
