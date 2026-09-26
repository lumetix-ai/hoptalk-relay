"""What the relay node holds of the contacts table, as the worker's reconciler finds it.

The contacts table is the source of truth and the reconciler converges the node to it: it lists
the node's contacts, reads the table, then adds and removes contacts over several awaited node
commands. Every write here is therefore a compare-and-set on the contact's id and public key, so
a contact that was deleted, or deleted and added again, meanwhile is left alone.
"""

from dataclasses import dataclass
from datetime import datetime

from django.db import transaction

from directory.models import Contact


@dataclass(frozen=True, kw_only=True)
class ContactForNode:
    """The fields of a contact the node's contact record is built from."""

    contact_id: int
    public_key: str
    name: str
    advert_timestamp: int
    latitude_microdegrees: int
    longitude_microdegrees: int
    node_sync_state: Contact.NodeSyncState


def list_contacts_for_node() -> list[ContactForNode]:
    """Every contact, in id order: the order in which missing contacts are added to the node."""
    contact_rows = Contact.objects.order_by("id").values(
        "id",
        "public_key",
        "name",
        "advert_timestamp",
        "latitude_microdegrees",
        "longitude_microdegrees",
        "node_sync_state",
    )
    return [
        ContactForNode(
            contact_id=contact_row["id"],
            public_key=contact_row["public_key"],
            name=contact_row["name"],
            advert_timestamp=contact_row["advert_timestamp"],
            latitude_microdegrees=contact_row["latitude_microdegrees"],
            longitude_microdegrees=contact_row["longitude_microdegrees"],
            node_sync_state=Contact.NodeSyncState(contact_row["node_sync_state"]),
        )
        for contact_row in contact_rows
    ]


def mark_contact_on_node(
    contact_id: int,
    public_key: str,
    *,
    node_name: str,
    node_out_path_length: int | None,
    now: datetime,
) -> bool:
    """The node holds the contact: on_node, and the display mirrors of its name and route refreshed.

    node_synced_at records when the worker first found the contact on the node, so a contact
    already known to be there keeps it. Returns False for a contact that no longer exists.
    """
    with transaction.atomic():
        Contact.objects.filter(id=contact_id, public_key=public_key).exclude(
            node_sync_state=Contact.NodeSyncState.ON_NODE, node_synced_at__isnull=False
        ).update(node_sync_state=Contact.NodeSyncState.ON_NODE, node_sync_error="", node_synced_at=now)
        updated_row_count = Contact.objects.filter(id=contact_id, public_key=public_key).update(
            node_name=node_name, node_out_path_length=node_out_path_length
        )
    return updated_row_count == 1


def mark_contact_add_failed(contact_id: int, public_key: str, sync_error: str, now: datetime) -> bool:
    """The node refused the contact; the next reconciliation pass tries again. False when it no longer exists."""
    updated_row_count = Contact.objects.filter(id=contact_id, public_key=public_key).update(
        node_sync_state=Contact.NodeSyncState.ADD_FAILED, node_sync_error=sync_error, node_synced_at=now
    )
    return updated_row_count == 1


def return_contact_to_pending_add(public_key: str, sync_error: str) -> int | None:
    """The node no longer holds the contact (it deleted it itself); returns its id, or None when it is unknown."""
    with transaction.atomic():
        contact_id = Contact.objects.filter(public_key=public_key).values_list("id", flat=True).first()
        if contact_id is None:
            return None
        Contact.objects.filter(id=contact_id, public_key=public_key).update(
            node_sync_state=Contact.NodeSyncState.PENDING_ADD, node_sync_error=sync_error
        )
    return contact_id
