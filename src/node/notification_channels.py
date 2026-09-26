"""The PostgreSQL LISTEN/NOTIFY channels that wake the relay worker.

A notification is only a wake-up call; the tables stay the source of truth, and the worker also
sweeps every 5 seconds in case one is lost. Sent inside a transaction, a notification is
delivered only when that transaction commits, so the worker is never woken for a row it cannot
see yet: call notify_relay_worker() in the same transaction as the change it announces.
"""

from enum import StrEnum

from django.db import connection


class NotificationChannel(StrEnum):
    # A new node_commands row; the payload is its id.
    NODE_COMMANDS = "relay_node_commands"
    # The contacts table changed: the reconciler converges the node, and the worker drops
    # reply-queue entries and pending route resets of contacts that no longer exist.
    CONTACTS_CHANGED = "relay_contacts_changed"
    # A setup run moved: the worker recomputes the relay mode at once.
    SETUP_CHANGED = "relay_setup_changed"


def notify_relay_worker(channel: NotificationChannel, payload: str = "") -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_notify(%s, %s)", [channel.value, payload])
