"""Run service calls on several database connections at once, for tests of concurrent transactions.

Such tests need @pytest.mark.django_db(transaction=True): every thread has a connection of its
own, outside pytest-django's rollback.
"""

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from django.db import connection, connections

LOCK_WAIT_TIMEOUT_SECONDS = 10
LOCK_WAIT_POLL_SECONDS = 0.02


def run_on_own_connection[Result](function: Callable[[], Result]) -> Callable[[], Result]:
    def run_and_close_connection() -> Result:
        try:
            return function()
        finally:
            connections.close_all()

    return run_and_close_connection


def run_concurrently[Result](*functions: Callable[[], Result]) -> list[Result]:
    with ThreadPoolExecutor(max_workers=len(functions)) as executor:
        futures = [executor.submit(run_on_own_connection(function)) for function in functions]
        return [future.result(timeout=LOCK_WAIT_TIMEOUT_SECONDS * 3) for future in futures]


def wait_until_a_transaction_waits_for_a_lock() -> None:
    """Returns once another connection is blocked on a row or advisory lock."""
    deadline = time.monotonic() + LOCK_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            # Other test runs may share the server, so only this database's sessions count.
            cursor.execute(
                "SELECT count(*) FROM pg_locks WHERE NOT granted AND pid IN "
                "(SELECT pid FROM pg_stat_activity WHERE datname = current_database())"
            )
            waiting_lock_count = cursor.fetchone()[0]
        if waiting_lock_count:
            return
        time.sleep(LOCK_WAIT_POLL_SECONDS)
    raise AssertionError("No transaction started waiting for a lock.")
