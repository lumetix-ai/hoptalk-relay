import logging
import sys
from typing import Any

import pytest

from hoptalk_relay.environment import InvalidConfigurationError, parse_environment_file_text
from hoptalk_relay.logging_configuration import EscapingRedactingFormatter, redact_account_request_passwords
from hoptalk_relay.relay_settings import (
    MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
    MAXIMUM_PAIRING_DURATION_SECONDS,
    MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
    MINIMUM_PAIRING_DURATION_SECONDS,
    NodeTransport,
    load_relay_settings,
)

VALID_SECRET_KEY = "a" * 50
VALID_ENVIRONMENT = {
    "SECRET_KEY": VALID_SECRET_KEY,
    "ADMIN_USERNAME": "operator",
    "ADMIN_PASSWORD_HASH": "argon2$argon2id$v=19$m=102400,t=2,p=8$c2FsdA$aGFzaA",
}


def test_the_environment_file_keeps_dollar_signs_and_drops_one_pair_of_quotes() -> None:
    environment_values = parse_environment_file_text(
        "# a comment\n"
        "\n"
        "ADMIN_PASSWORD_HASH=argon2$argon2id$v=19$m=102400,t=2,p=8$c2FsdA$aGFzaA\n"
        "TIME_ZONE='Australia/Melbourne'\n"
        'ADMIN_USERNAME="operator"\n'
        "export RELAY_RETRY_MAXIMUM_ATTEMPTS = 7 \n",
        environment_file_name="src/.env",
    )

    assert environment_values == {
        "ADMIN_PASSWORD_HASH": "argon2$argon2id$v=19$m=102400,t=2,p=8$c2FsdA$aGFzaA",
        "TIME_ZONE": "Australia/Melbourne",
        "ADMIN_USERNAME": "operator",
        "RELAY_RETRY_MAXIMUM_ATTEMPTS": "7",
    }


def test_a_line_without_an_equals_sign_is_reported_by_its_number_without_its_content() -> None:
    with pytest.raises(InvalidConfigurationError, match=r"src/\.env, line 2") as raised:
        parse_environment_file_text("TIME_ZONE=UTC\nsecret-password\n", environment_file_name="src/.env")

    assert "secret-password" not in str(raised.value)


def test_the_defaults_apply_when_only_the_required_values_are_set() -> None:
    relay_settings = load_relay_settings(VALID_ENVIRONMENT)

    assert relay_settings.time_zone == "UTC"
    assert relay_settings.retry_strategy.maximum_attempts == 6
    assert relay_settings.retry_strategy.initial_pause_seconds == 30
    assert relay_settings.retry_strategy.backoff_multiplier == 2.0
    assert relay_settings.retry_strategy.maximum_pause_seconds == 600
    assert relay_settings.retry_strategy.delivered_receipt_delay_seconds == 15
    assert relay_settings.pacing.maximum_packets_awaiting_node_acknowledgement == 4
    assert relay_settings.pacing.minimum_seconds_between_sends == 2.0
    assert relay_settings.pacing.maximum_active_deliveries_per_device == 3
    assert relay_settings.retention.log_retention_days == 30
    assert relay_settings.pairing.default_duration_seconds == 120
    assert relay_settings.pairing.default_advert_interval_seconds == 30
    assert relay_settings.node_connection.transport == NodeTransport.TCP
    assert relay_settings.node_connection.describe() == "tcp host.docker.internal:5055"


def test_every_invalid_variable_is_named_in_one_error() -> None:
    invalid_environment = {
        **VALID_ENVIRONMENT,
        "SECRET_KEY": "",
        "ADMIN_PASSWORD_HASH": "pbkdf2_sha256$1$salt$hash",
        "TIME_ZONE": "Mars/Olympus_Mons",
        "RELAY_RETRY_MAXIMUM_ATTEMPTS": "21",
        "RELAY_RETRY_BACKOFF_MULTIPLIER": "fast",
        "MESHCORE_TRANSPORT": "bluetooth",
    }

    with pytest.raises(InvalidConfigurationError) as raised:
        load_relay_settings(invalid_environment)

    for variable_name in (
        "SECRET_KEY",
        "ADMIN_PASSWORD_HASH",
        "TIME_ZONE",
        "RELAY_RETRY_MAXIMUM_ATTEMPTS",
        "RELAY_RETRY_BACKOFF_MULTIPLIER",
        "MESHCORE_TRANSPORT",
    ):
        assert variable_name in str(raised.value)


def test_the_maximum_pause_may_not_be_shorter_than_the_initial_pause() -> None:
    with pytest.raises(InvalidConfigurationError, match="RELAY_RETRY_MAXIMUM_PAUSE_SECONDS"):
        load_relay_settings(
            {
                **VALID_ENVIRONMENT,
                "RELAY_RETRY_INITIAL_PAUSE_SECONDS": "120",
                "RELAY_RETRY_MAXIMUM_PAUSE_SECONDS": "60",
            }
        )


@pytest.mark.parametrize(
    ("variable_name", "minimum", "maximum"),
    [
        ("RELAY_PAIRING_DEFAULT_DURATION_SECONDS", MINIMUM_PAIRING_DURATION_SECONDS, MAXIMUM_PAIRING_DURATION_SECONDS),
        (
            "RELAY_PAIRING_DEFAULT_ADVERT_INTERVAL_SECONDS",
            MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
            MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
        ),
    ],
)
def test_a_pairing_default_is_accepted_only_within_the_pairing_bounds(
    variable_name: str, minimum: int, maximum: int
) -> None:
    for accepted_value in (minimum, maximum):
        load_relay_settings({**VALID_ENVIRONMENT, variable_name: str(accepted_value)})
    for rejected_value in (minimum - 1, maximum + 1):
        with pytest.raises(InvalidConfigurationError, match=variable_name):
            load_relay_settings({**VALID_ENVIRONMENT, variable_name: str(rejected_value)})


def test_the_secret_values_stay_out_of_the_representation() -> None:
    representation = repr(load_relay_settings(VALID_ENVIRONMENT))

    assert VALID_SECRET_KEY not in representation
    assert VALID_ENVIRONMENT["ADMIN_PASSWORD_HASH"] not in representation


def test_a_sign_in_password_is_redacted_but_the_username_is_kept() -> None:
    assert (
        redact_account_request_passwords("received 'HT1 A ivan correct horse battery' from ivan (e04b1359d374)")
        == "received 'HT1 A ivan ********"
    )
    assert redact_account_request_passwords("HT1 M bob 1 1/1 hello") == "HT1 M bob 1 1/1 hello"


def test_the_log_formatter_redacts_passwords_in_arguments_and_tracebacks() -> None:
    formatter = EscapingRedactingFormatter("%(message)s")
    try:
        raise ValueError("Cannot parse HT1 A ivan hunter2222")
    except ValueError:
        exception_information = sys.exc_info()
    log_record = logging.LogRecord(
        name="worker",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Processing %s failed",
        args=("HT1 A ivan hunter2222",),
        exc_info=exception_information,
    )

    formatted_lines = formatter.format(log_record).splitlines()

    assert formatted_lines[0] == "Processing HT1 A ivan ********"
    assert formatted_lines[1] == "Traceback (most recent call last):"
    assert formatted_lines[-1] == "ValueError: Cannot parse HT1 A ivan ********"
    assert not any("hunter2222" in formatted_line for formatted_line in formatted_lines)


def build_log_record(message: str, *arguments: object, exception_information: Any = None) -> logging.LogRecord:
    return logging.LogRecord(
        name="worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=arguments,
        exc_info=exception_information,
    )


def test_a_name_from_the_radio_cannot_start_a_log_line_or_drive_the_terminal() -> None:
    formatter = EscapingRedactingFormatter("%(levelname)s %(message)s")
    radio_name = "Bob\nINFO forged record\r\x1b[2J\x9b\x7f"

    formatted_output = formatter.format(build_log_record("Heard advert from %s", radio_name))

    assert formatted_output == "INFO Heard advert from Bob\\x0aINFO forged record\\x0d\\x1b[2J\\x9b\\x7f"


def test_a_traceback_keeps_its_lines_and_escapes_the_rest() -> None:
    formatter = EscapingRedactingFormatter("%(message)s")
    try:
        raise ValueError("Cannot add \x1b]0;title\x07 HT1 A ivan hunter2222")
    except ValueError:
        exception_information = sys.exc_info()

    formatted_lines = formatter.format(
        build_log_record("Adding failed", exception_information=exception_information)
    ).splitlines()

    assert formatted_lines[0] == "Adding failed"
    assert formatted_lines[1] == "Traceback (most recent call last):"
    assert formatted_lines[-1] == "ValueError: Cannot add \\x1b]0;title\\x07 HT1 A ivan ********"
