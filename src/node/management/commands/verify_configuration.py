from typing import Any

from django.contrib.auth.hashers import Argon2PasswordHasher
from django.core.management.base import BaseCommand, CommandError

from hoptalk_relay.relay_settings import RelaySettings, get_relay_settings


class Command(BaseCommand):
    help = (
        "Check the configuration in src/.env and print what the panel and the worker will run with. "
        "Loading the settings already refuses missing or out-of-range values; this also checks that the "
        "operator's password hash is a complete Argon2 hash."
    )

    def handle(self, *arguments: Any, **options: Any) -> None:
        relay_settings = get_relay_settings()
        verify_operator_password_hash(relay_settings.operator_credentials.password_hash)
        self.stdout.write(describe_relay_settings(relay_settings))


def verify_operator_password_hash(operator_password_hash: str) -> None:
    try:
        Argon2PasswordHasher().decode(operator_password_hash)
    except (ValueError, TypeError) as decoding_error:
        raise CommandError(
            "ADMIN_PASSWORD_HASH in src/.env is not a complete Argon2 hash. "
            "Put the line that make generate-admin-password prints in src/.env."
        ) from decoding_error


def describe_relay_settings(relay_settings: RelaySettings) -> str:
    retry_strategy = relay_settings.retry_strategy
    pacing = relay_settings.pacing
    return (
        "The configuration is valid.\n"
        f"  Operator: {relay_settings.operator_credentials.username}\n"
        f"  Time zone: {relay_settings.time_zone}\n"
        f"  Node connection: {relay_settings.node_connection.describe()}\n"
        f"  Retries: {retry_strategy.maximum_attempts} attempts, "
        f"a pause of {retry_strategy.initial_pause_seconds:g} s multiplied by "
        f"{retry_strategy.backoff_multiplier:g} per attempt up to {retry_strategy.maximum_pause_seconds:g} s; "
        f"delivered receipts held back {retry_strategy.delivered_receipt_delay_seconds:g} s\n"
        f"  Pacing: {pacing.maximum_packets_awaiting_node_acknowledgement} packets awaiting an acknowledgement, "
        f"{pacing.minimum_seconds_between_sends:g} s between sends, "
        f"{pacing.maximum_active_deliveries_per_device} deliveries in progress per device\n"
        f"  Traffic log kept for {relay_settings.retention.log_retention_days} days"
    )
