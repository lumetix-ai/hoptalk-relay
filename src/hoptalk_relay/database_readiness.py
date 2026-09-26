"""Waiting for PostgreSQL and for the migrations the web container applies.

Both containers start together: the web entrypoint waits for the database before it migrates,
and the relay worker waits until no migration is left before it touches any table.
"""

import logging
import threading
import time

from django.db import DatabaseError, connection
from django.db.migrations.executor import MigrationExecutor

logger = logging.getLogger(__name__)


def database_accepts_connections() -> bool:
    try:
        connection.ensure_connection()
    except DatabaseError as database_error:
        logger.info("Waiting for the database at %s: %s", describe_database_address(), database_error)
        connection.close()
        return False
    return True


def find_unapplied_migrations() -> list[str]:
    migration_executor = MigrationExecutor(connection)
    migration_targets = migration_executor.loader.graph.leaf_nodes()
    return [
        f"{migration.app_label}.{migration.name}"
        for migration, _is_backwards in migration_executor.migration_plan(migration_targets)
    ]


def wait_for_database_connection(
    *,
    timeout_seconds: float | None,
    poll_interval_seconds: float,
    stop_requested: threading.Event | None = None,
) -> bool:
    """Return True once the database answers, False on timeout or when a stop is requested."""
    stop_event = stop_requested or threading.Event()
    deadline = None if timeout_seconds is None else time.monotonic() + timeout_seconds

    while not database_accepts_connections():
        if deadline is not None and time.monotonic() >= deadline:
            return False
        if stop_event.wait(poll_interval_seconds):
            return False

    return True


def wait_for_applied_migrations(*, poll_interval_seconds: float, stop_requested: threading.Event) -> bool:
    """Return True once every migration is applied, False when a stop is requested first."""
    while unapplied_migrations := find_unapplied_migrations():
        logger.info(
            "Waiting for the app container to apply %d migrations: %s",
            len(unapplied_migrations),
            ", ".join(unapplied_migrations),
        )
        connection.close()
        if stop_requested.wait(poll_interval_seconds):
            return False

    return True


def describe_database_address() -> str:
    database_settings = connection.settings_dict
    return f"{database_settings['HOST']}:{database_settings['PORT']}"
