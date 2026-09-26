"""Transactions of the messaging services: one per service call, retried on a deadlock, locking in the global order.

Every transaction that locks several rows locks, each table in ascending id: users, contacts,
messages, message_deliveries, receipt_notifications, refresh_sessions, inbound_direct_messages,
outbound_packets. Django's foreign keys are checked at commit, which takes a KEY SHARE lock on
the referenced row, so a transaction that inserts rows depending on a contact locks that
contact first.
"""

from collections.abc import Callable
from dataclasses import dataclass

from django.db import connection, transaction

from directory.contacts import run_with_deadlock_retries
from hoptalk_relay.relay_settings import RetryStrategy, get_relay_settings


@dataclass(frozen=True, kw_only=True)
class LockedDevice:
    device_id: int
    # None while the contact has not signed in.
    user_id: int | None


def run_in_service_transaction[Result](transaction_function: Callable[[], Result]) -> Result:
    """Run the function in one transaction, again when PostgreSQL aborted it for a deadlock or serialization failure.

    Called inside an outer transaction it becomes a savepoint, and the outer transaction's owner
    retries the whole unit instead.
    """

    def run_atomically() -> Result:
        with transaction.atomic():
            return transaction_function()

    return run_with_deadlock_retries(run_atomically)


def lock_device_for_key_share(device_id: int) -> LockedDevice | None:
    """FOR KEY SHARE: a deletion of the contact waits for this transaction, other updates of it do not.

    Django cannot choose the strength of a row lock per query, hence the SQL.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, user_id FROM contacts WHERE id = %s FOR KEY SHARE", [device_id])
        row = cursor.fetchone()
    if row is None:
        return None
    return LockedDevice(device_id=row[0], user_id=row[1])


def read_retry_strategy() -> RetryStrategy:
    return get_relay_settings().retry_strategy


def read_maximum_active_deliveries_per_device() -> int:
    return get_relay_settings().pacing.maximum_active_deliveries_per_device
