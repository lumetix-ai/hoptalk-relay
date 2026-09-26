"""At most one worker owns the node: a PostgreSQL advisory lock on a connection of its own.

A second worker (a misconfiguration) blocks on the lock and never opens the node. The holder
keeps its connection alive and checks it regularly; if the connection breaks, the lock is gone,
so the check raises, the task group ends the process, and Compose starts it again. The lock is
released when its connection closes, also when the process dies.
"""

import logging
from typing import Any

import psycopg
from django.db import connections

from worker.clock import Clock

logger = logging.getLogger(__name__)

# "HT" and 2; the contacts table's capacity lock uses "HT" and 1.
RELAY_WORKER_LOCK_KEY = 0x4854_0002


def build_database_connection_parameters() -> dict[str, Any]:
    """The default database's parameters, for connections outside Django's pool; the test database in tests."""
    database_settings = connections["default"].settings_dict
    return {
        "host": database_settings["HOST"],
        "port": database_settings["PORT"],
        "dbname": database_settings["NAME"],
        "user": database_settings["USER"],
        "password": database_settings["PASSWORD"],
        "application_name": "hoptalk-relay-worker",
    }


class SingleInstanceLock:
    def __init__(self, *, clock: Clock, keepalive_seconds: float) -> None:
        self._clock = clock
        self._keepalive_seconds = keepalive_seconds
        self._connection: psycopg.AsyncConnection[Any] | None = None

    @property
    def is_held(self) -> bool:
        return self._connection is not None

    async def acquire(self) -> None:
        """Blocks until no other worker holds the lock."""
        connection = await psycopg.AsyncConnection.connect(**build_database_connection_parameters(), autocommit=True)
        try:
            await connection.execute("SELECT pg_advisory_lock(%s)", [RELAY_WORKER_LOCK_KEY])
        except BaseException:
            await connection.close()
            raise
        self._connection = connection
        logger.info("This worker holds the relay lock; no other worker can open the node.")

    async def hold(self) -> None:
        """Runs until the lock's connection fails, and then raises."""
        while True:
            await self._clock.sleep(self._keepalive_seconds)
            if self._connection is None:
                raise RuntimeError("The relay lock is not held.")
            await self._connection.execute("SELECT 1")

    async def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            await connection.execute("SELECT pg_advisory_unlock(%s)", [RELAY_WORKER_LOCK_KEY])
        finally:
            await connection.close()
