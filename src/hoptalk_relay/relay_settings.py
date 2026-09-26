"""The validated application configuration: src/.env plus the node connection from Compose.

settings.py builds RelaySettings once at start-up; both the panel and the relay worker refuse to
start while any value is missing or out of range, and the error names every such variable.
Code reads the result through get_relay_settings().
"""

import zoneinfo
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from django.conf import settings

from hoptalk_relay.environment import InvalidConfigurationError

MINIMUM_SECRET_KEY_LENGTH = 50
ARGON2_PASSWORD_HASH_PREFIX = "argon2$argon2id$"

# The bounds of a pairing session, for both the defaults in src/.env and the panel's pairing form.
MINIMUM_PAIRING_DURATION_SECONDS = 60
MAXIMUM_PAIRING_DURATION_SECONDS = 600
MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS = 10
MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS = 120


@dataclass(frozen=True, kw_only=True)
class OperatorCredentials:
    username: str
    password_hash: str = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class RetryStrategy:
    maximum_attempts: int
    initial_pause_seconds: float
    backoff_multiplier: float
    maximum_pause_seconds: float
    delivered_receipt_delay_seconds: float


@dataclass(frozen=True, kw_only=True)
class PacingSettings:
    maximum_packets_awaiting_node_acknowledgement: int
    minimum_seconds_between_sends: float
    maximum_active_deliveries_per_device: int


@dataclass(frozen=True, kw_only=True)
class PairingSettings:
    default_duration_seconds: int
    default_advert_interval_seconds: int


@dataclass(frozen=True, kw_only=True)
class RetentionSettings:
    log_retention_days: int


@dataclass(frozen=True, kw_only=True)
class EngineTimingSettings:
    """The delivery engine's fixed timing floors and windows; src/.env does not set them.

    They follow from the firmware and the mesh rather than from an operator's choice. The worker's
    tests shorten them, so that acknowledgement deadlines and retry rounds pass in milliseconds.
    """

    minimum_acknowledgement_wait_seconds: float = 3.0
    maximum_acknowledgement_wait_seconds: float = 60.0
    # When the node suggested no timeout: a command timeout or a rejected send.
    unknown_acknowledgement_wait_seconds: float = 10.0
    # A status with zeros after a complete round brings the next round this close.
    missing_parts_round_delay_seconds: float = 5.0
    # A route the node learned this recently came from the flood exchange that just happened.
    recent_path_update_seconds: float = 30.0
    # A frame processed later than this (after a restart) may have taught the node a route whose
    # PATH_UPDATE push was lost; the timeout-based reset repairs a route that is really stale.
    flood_arrival_reset_maximum_age_seconds: float = 60.0
    # A reply sent again by flood after a route reset must still be worth waiting for.
    reply_resend_maximum_age_seconds: float = 60.0


class NodeTransport(StrEnum):
    TCP = "tcp"
    SERIAL = "serial"


@dataclass(frozen=True, kw_only=True)
class NodeConnectionSettings:
    transport: NodeTransport
    tcp_host: str
    tcp_port: int
    serial_device: str

    def describe(self) -> str:
        if self.transport == NodeTransport.TCP:
            return f"tcp {self.tcp_host}:{self.tcp_port}"
        return f"serial {self.serial_device}"


@dataclass(frozen=True, kw_only=True)
class RelaySettings:
    secret_key: str = field(repr=False)
    operator_credentials: OperatorCredentials
    time_zone: str
    retry_strategy: RetryStrategy
    pacing: PacingSettings
    pairing: PairingSettings
    retention: RetentionSettings
    node_connection: NodeConnectionSettings
    engine_timing: EngineTimingSettings = field(default_factory=EngineTimingSettings)


class EnvironmentValueReader:
    """Reads typed values and collects every problem, so that one start-up reports them all.

    A value that fails its check is replaced by its default (or an empty string) and recorded
    in `problems`; the caller raises once everything has been read.
    """

    def __init__(self, environment_values: Mapping[str, str]) -> None:
        self.environment_values = environment_values
        self.problems: list[str] = []

    def read_text(self, variable_name: str, default: str) -> str:
        return self.environment_values.get(variable_name, "").strip() or default

    def read_required_text(self, variable_name: str, how_to_fix: str) -> str:
        value = self.environment_values.get(variable_name, "").strip()
        if not value:
            self.problems.append(f"{variable_name} is empty. {how_to_fix}")
        return value

    def read_integer(self, variable_name: str, default: int, minimum: int, maximum: int) -> int:
        raw_value = self.read_text(variable_name, str(default))
        try:
            value = int(raw_value)
        except ValueError:
            self.problems.append(
                f"{variable_name} must be a whole number from {minimum} to {maximum}, not {raw_value!r}."
            )
            return default

        if not minimum <= value <= maximum:
            self.problems.append(f"{variable_name} must be from {minimum} to {maximum}, not {value}.")
            return default

        return value

    def read_decimal_number(self, variable_name: str, default: float, minimum: float, maximum: float) -> float:
        raw_value = self.read_text(variable_name, str(default))
        try:
            value = float(raw_value)
        except ValueError:
            self.problems.append(f"{variable_name} must be a number from {minimum} to {maximum}, not {raw_value!r}.")
            return default

        if not minimum <= value <= maximum:
            self.problems.append(f"{variable_name} must be from {minimum} to {maximum}, not {value}.")
            return default

        return value


def load_relay_settings(environment_values: Mapping[str, str]) -> RelaySettings:
    reader = EnvironmentValueReader(environment_values)

    relay_settings = RelaySettings(
        secret_key=read_secret_key(reader),
        operator_credentials=read_operator_credentials(reader),
        time_zone=read_time_zone(reader),
        retry_strategy=read_retry_strategy(reader),
        pacing=read_pacing_settings(reader),
        pairing=read_pairing_settings(reader),
        retention=read_retention_settings(reader),
        node_connection=read_node_connection_settings(reader),
    )

    if reader.problems:
        raise InvalidConfigurationError(
            "The configuration in src/.env cannot be used:\n" + "\n".join(f"- {problem}" for problem in reader.problems)
        )

    return relay_settings


def read_secret_key(reader: EnvironmentValueReader) -> str:
    secret_key = reader.read_required_text(
        "SECRET_KEY", "Put the line that make generate-secret-key prints in src/.env."
    )
    if secret_key and len(secret_key) < MINIMUM_SECRET_KEY_LENGTH:
        reader.problems.append(
            f"SECRET_KEY is shorter than {MINIMUM_SECRET_KEY_LENGTH} characters. Use make generate-secret-key."
        )
    return secret_key


def read_operator_credentials(reader: EnvironmentValueReader) -> OperatorCredentials:
    username = reader.read_required_text("ADMIN_USERNAME", "Choose the operator's username for the admin panel.")
    password_hash = reader.read_required_text(
        "ADMIN_PASSWORD_HASH", "Put the line that make generate-admin-password prints in src/.env."
    )
    if password_hash and not password_hash.startswith(ARGON2_PASSWORD_HASH_PREFIX):
        reader.problems.append(
            f"ADMIN_PASSWORD_HASH must be an Argon2 hash starting with {ARGON2_PASSWORD_HASH_PREFIX}. "
            "Use make generate-admin-password."
        )
    return OperatorCredentials(username=username, password_hash=password_hash)


def read_time_zone(reader: EnvironmentValueReader) -> str:
    time_zone = reader.read_text("TIME_ZONE", "UTC")
    try:
        zoneinfo.ZoneInfo(time_zone)
    except zoneinfo.ZoneInfoNotFoundError, ValueError:
        reader.problems.append(f"TIME_ZONE must be an IANA time zone such as Australia/Melbourne, not {time_zone!r}.")
        return "UTC"
    return time_zone


def read_retry_strategy(reader: EnvironmentValueReader) -> RetryStrategy:
    initial_pause_seconds = reader.read_decimal_number(
        "RELAY_RETRY_INITIAL_PAUSE_SECONDS", default=30, minimum=5, maximum=3600
    )
    maximum_pause_seconds = reader.read_decimal_number(
        "RELAY_RETRY_MAXIMUM_PAUSE_SECONDS", default=600, minimum=5, maximum=86_400
    )
    if maximum_pause_seconds < initial_pause_seconds:
        reader.problems.append(
            f"RELAY_RETRY_MAXIMUM_PAUSE_SECONDS ({maximum_pause_seconds:g}) must not be shorter than "
            f"RELAY_RETRY_INITIAL_PAUSE_SECONDS ({initial_pause_seconds:g})."
        )

    return RetryStrategy(
        maximum_attempts=reader.read_integer("RELAY_RETRY_MAXIMUM_ATTEMPTS", default=6, minimum=1, maximum=20),
        initial_pause_seconds=initial_pause_seconds,
        backoff_multiplier=reader.read_decimal_number(
            "RELAY_RETRY_BACKOFF_MULTIPLIER", default=2.0, minimum=1.0, maximum=4.0
        ),
        maximum_pause_seconds=maximum_pause_seconds,
        delivered_receipt_delay_seconds=reader.read_decimal_number(
            "RELAY_DELIVERED_RECEIPT_DELAY_SECONDS", default=15, minimum=0, maximum=300
        ),
    )


def read_pacing_settings(reader: EnvironmentValueReader) -> PacingSettings:
    return PacingSettings(
        maximum_packets_awaiting_node_acknowledgement=reader.read_integer(
            "RELAY_MAXIMUM_PACKETS_AWAITING_NODE_ACKNOWLEDGEMENT", default=4, minimum=2, maximum=6
        ),
        minimum_seconds_between_sends=reader.read_decimal_number(
            "RELAY_MINIMUM_SECONDS_BETWEEN_SENDS", default=2.0, minimum=1.0, maximum=30.0
        ),
        maximum_active_deliveries_per_device=reader.read_integer(
            "RELAY_MAXIMUM_ACTIVE_DELIVERIES_PER_DEVICE", default=3, minimum=1, maximum=10
        ),
    )


def read_pairing_settings(reader: EnvironmentValueReader) -> PairingSettings:
    return PairingSettings(
        default_duration_seconds=reader.read_integer(
            "RELAY_PAIRING_DEFAULT_DURATION_SECONDS",
            default=120,
            minimum=MINIMUM_PAIRING_DURATION_SECONDS,
            maximum=MAXIMUM_PAIRING_DURATION_SECONDS,
        ),
        default_advert_interval_seconds=reader.read_integer(
            "RELAY_PAIRING_DEFAULT_ADVERT_INTERVAL_SECONDS",
            default=30,
            minimum=MINIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
            maximum=MAXIMUM_PAIRING_ADVERT_INTERVAL_SECONDS,
        ),
    )


def read_retention_settings(reader: EnvironmentValueReader) -> RetentionSettings:
    return RetentionSettings(
        log_retention_days=reader.read_integer("RELAY_LOG_RETENTION_DAYS", default=30, minimum=1, maximum=3650),
    )


def read_node_connection_settings(reader: EnvironmentValueReader) -> NodeConnectionSettings:
    raw_transport = reader.read_text("MESHCORE_TRANSPORT", NodeTransport.TCP.value)
    try:
        transport = NodeTransport(raw_transport)
    except ValueError:
        reader.problems.append(f"MESHCORE_TRANSPORT must be tcp or serial, not {raw_transport!r}.")
        transport = NodeTransport.TCP

    return NodeConnectionSettings(
        transport=transport,
        tcp_host=reader.read_text("MESHCORE_TCP_HOST", "host.docker.internal"),
        tcp_port=reader.read_integer("MESHCORE_TCP_PORT", default=5055, minimum=1, maximum=65_535),
        serial_device=reader.read_text("MESHCORE_SERIAL_DEVICE", "/dev/meshcore-node"),
    )


def describe_effective_configuration(relay_settings: RelaySettings) -> dict[str, int | float]:
    """The tunable delivery values under the keys the worker reports in worker_status.

    The worker reports the values it runs with, which src/.env may have changed since the panel
    started; the panel shows its own until the worker has reported.
    """
    retry_strategy = relay_settings.retry_strategy
    pacing = relay_settings.pacing
    return {
        "attempts_per_delivery": retry_strategy.maximum_attempts,
        "first_pause_seconds": retry_strategy.initial_pause_seconds,
        "pause_multiplier": retry_strategy.backoff_multiplier,
        "longest_pause_seconds": retry_strategy.maximum_pause_seconds,
        "delivered_receipt_hold_back_seconds": retry_strategy.delivered_receipt_delay_seconds,
        "packets_awaiting_a_firmware_ack": pacing.maximum_packets_awaiting_node_acknowledgement,
        "gap_between_sends_seconds": pacing.minimum_seconds_between_sends,
        "deliveries_in_progress_per_device": pacing.maximum_active_deliveries_per_device,
        "traffic_log_kept_for_days": relay_settings.retention.log_retention_days,
    }


def get_relay_settings() -> RelaySettings:
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    return relay_settings
