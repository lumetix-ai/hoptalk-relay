from django.conf import settings
from django.db import connections
from django.db.backends.postgresql.base import DatabaseWrapper as PostgresqlDatabaseWrapper


def close_database_pools() -> None:
    """Close the psycopg connection pools while the interpreter can still join their threads.

    A pool that is still open when the process ends is closed by its finaliser during
    interpreter shutdown, where Python 3.14 can no longer join threads, and every command then
    ends with a PythonFinalizationError traceback. Entry points call this on their way out.
    """
    # Settings that failed to load have opened no pool, and touching them would raise again.
    if not settings.configured:
        return

    for database_connection in connections.all(initialized_only=True):
        if isinstance(database_connection, PostgresqlDatabaseWrapper):
            database_connection.close_pool()
