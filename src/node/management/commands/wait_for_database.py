from argparse import ArgumentParser
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from hoptalk_relay.database_readiness import describe_database_address, wait_for_database_connection


class Command(BaseCommand):
    help = "Wait until the database accepts connections, and fail after the timeout."

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--timeout-seconds", type=float, default=120.0)
        parser.add_argument("--poll-interval-seconds", type=float, default=2.0)

    def handle(self, *arguments: Any, **options: Any) -> None:
        timeout_seconds: float = options["timeout_seconds"]
        database_is_ready = wait_for_database_connection(
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=options["poll_interval_seconds"],
        )
        if not database_is_ready:
            raise CommandError(
                f"The database at {describe_database_address()} did not accept connections "
                f"within {timeout_seconds:g} seconds."
            )
        self.stdout.write(f"The database at {describe_database_address()} accepts connections.")
