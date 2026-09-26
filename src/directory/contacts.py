"""Adding and deleting the relay node's contacts.

The contacts table is the source of truth; every change notifies relay_contacts_changed in its
transaction and the worker's reconciler converges the node.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from django.db import IntegrityError, OperationalError, connection, models, transaction
from django.db.models import QuerySet

from directory.models import Contact, User
from messaging.models import Message, MessageDelivery, ReceiptNotification, RefreshSession
from node.contact_cards import ContactCard, MeshCoreNodeType, describe_node_type, truncate_contact_name
from node.models import HeardAdvert, NodeSetupRun, WorkerStatus
from node.node_settings import NodeSettingKey, read_node_setting_value
from node.notification_channels import NotificationChannel, notify_relay_worker
from node.pairing_sessions import ADDING_ALLOWED_AFTER_SESSION_END_MINUTES, is_adding_allowed

# The size of the companion firmware's contact table (MAX_CONTACTS of the XIAO nRF52840 build).
CONTACT_CAPACITY = 350
# pg_advisory_xact_lock key taken by every add, so that concurrent adds cannot pass the
# capacity check together.
CONTACT_CAPACITY_LOCK_KEY = 0x4854_0001
PUBLIC_KEY_PREFIX_LENGTH = 12

# PostgreSQL aborts one of two transactions that deadlock, or that conflict under serializable
# isolation; running the aborted one again gives the same result as running them in turn.
RETRYABLE_SQLSTATES = frozenset({"40P01", "40001"})
MAXIMUM_TRANSACTION_ATTEMPTS = 3

PENDING_DELIVERY_STATES = (MessageDelivery.State.PENDING, MessageDelivery.State.QUEUED_FOR_REFRESH)


class ContactAdditionProblem(StrEnum):
    NOT_A_CHAT_NODE = "not_a_chat_node"
    ALREADY_A_CONTACT = "already_a_contact"
    # The first six bytes of the key equal another contact's.
    PREFIX_COLLISION = "prefix_collision"
    RELAY_NODE_ITSELF = "relay_node_itself"
    CAPACITY_REACHED = "capacity_reached"
    PAIRING_WINDOW_CLOSED = "pairing_window_closed"


@dataclass(frozen=True, kw_only=True)
class ContactAdditionCheck:
    """What would stop a node from becoming a contact, for the card preview and the add itself."""

    problems: tuple[ContactAdditionProblem, ...]
    # The contact whose key or prefix the new one collides with, if any.
    conflicting_contact: Contact | None = None
    node_type: int = MeshCoreNodeType.CHAT

    @property
    def is_allowed(self) -> bool:
        return not self.problems

    def describe_problems(self) -> list[str]:
        return [describe_contact_addition_problem(problem, self) for problem in self.problems]


class ContactAdditionRefusedError(Exception):
    def __init__(self, contact_addition_check: ContactAdditionCheck) -> None:
        super().__init__(" ".join(contact_addition_check.describe_problems()))
        self.contact_addition_check = contact_addition_check


class LockedRowChangedError(Exception):
    """A row changed between reading and locking it (a device was relinked or deleted); the transaction starts over."""


@dataclass(frozen=True, kw_only=True)
class ContactDeletionSummary:
    contact: Contact
    linked_username: str
    pending_delivery_count: int
    pending_receipt_count: int
    active_refresh_session_count: int


def describe_contact_addition_problem(problem: ContactAdditionProblem, check: ContactAdditionCheck) -> str:
    conflicting_contact = check.conflicting_contact
    match problem:
        case ContactAdditionProblem.NOT_A_CHAT_NODE:
            return f"This is a {describe_node_type(check.node_type)}, and only a chat node can be a contact."
        case ContactAdditionProblem.ALREADY_A_CONTACT:
            return f"This node is already a contact ({conflicting_contact})."
        case ContactAdditionProblem.PREFIX_COLLISION:
            return (
                f"The first six bytes of this key equal those of contact {conflicting_contact}, "
                "so the node could not tell their messages apart."
            )
        case ContactAdditionProblem.RELAY_NODE_ITSELF:
            return "This is the relay's own node."
        case ContactAdditionProblem.CAPACITY_REACHED:
            return f"The node already holds {CONTACT_CAPACITY} contacts, its maximum."
        case ContactAdditionProblem.PAIRING_WINDOW_CLOSED:
            return (
                f"The pairing session ended more than {ADDING_ALLOWED_AFTER_SESSION_END_MINUTES} minutes ago. "
                "Start a new one."
            )


def check_contact_addition(public_key: str, node_type: int) -> ContactAdditionCheck:
    """Read-only; the add functions repeat the checks under the capacity lock."""
    problems: list[ContactAdditionProblem] = []
    conflicting_contact: Contact | None = None

    if node_type != MeshCoreNodeType.CHAT:
        problems.append(ContactAdditionProblem.NOT_A_CHAT_NODE)

    existing_contact = Contact.objects.filter(public_key=public_key).first()
    if existing_contact is not None:
        problems.append(ContactAdditionProblem.ALREADY_A_CONTACT)
        conflicting_contact = existing_contact
    else:
        colliding_contact = Contact.objects.filter(public_key_prefix=public_key[:PUBLIC_KEY_PREFIX_LENGTH]).first()
        if colliding_contact is not None:
            problems.append(ContactAdditionProblem.PREFIX_COLLISION)
            conflicting_contact = colliding_contact

    if public_key in find_relay_node_public_keys():
        problems.append(ContactAdditionProblem.RELAY_NODE_ITSELF)

    if Contact.objects.count() >= CONTACT_CAPACITY:
        problems.append(ContactAdditionProblem.CAPACITY_REACHED)

    return ContactAdditionCheck(problems=tuple(problems), conflicting_contact=conflicting_contact, node_type=node_type)


def find_relay_node_public_keys() -> set[str]:
    """The configured node, the node the worker is attached to, and the identity a setup run just created."""
    relay_node_public_keys = {read_node_setting_value(NodeSettingKey.NODE_PUBLIC_KEY)}
    relay_node_public_keys.update(WorkerStatus.objects.values_list("node_public_key", flat=True))
    relay_node_public_keys.update(NodeSetupRun.objects.filter(is_active=True).values_list("new_public_key", flat=True))
    relay_node_public_keys.discard("")
    return relay_node_public_keys


def add_contact_from_card(contact_card: ContactCard, now: datetime) -> Contact:
    """Create a pending_add contact with source card; raises ContactAdditionRefusedError."""
    with transaction.atomic():
        take_contact_capacity_lock()
        return create_contact(
            public_key=contact_card.public_key,
            node_type=contact_card.node_type,
            name=contact_card.name,
            advert_timestamp=contact_card.advert_timestamp,
            latitude_microdegrees=contact_card.latitude_microdegrees,
            longitude_microdegrees=contact_card.longitude_microdegrees,
            source=Contact.Source.CARD,
            card_uri=contact_card.card_uri,
            now=now,
        )


def add_contact_from_heard_advert(heard_advert: HeardAdvert, now: datetime) -> Contact:
    """Create a pending_add contact with source pairing and link it from the heard advert.

    Allowed while the advert's session is active or up to 15 minutes after it ended; raises
    ContactAdditionRefusedError.
    """
    with transaction.atomic():
        take_contact_capacity_lock()
        locked_advert = (
            HeardAdvert.objects.select_for_update(of=("self",))
            .select_related("pairing_session", "added_contact")
            .get(id=heard_advert.pk)
        )
        if locked_advert.added_contact is not None:
            raise ContactAdditionRefusedError(
                ContactAdditionCheck(
                    problems=(ContactAdditionProblem.ALREADY_A_CONTACT,),
                    conflicting_contact=locked_advert.added_contact,
                    node_type=locked_advert.node_type,
                )
            )
        if not is_adding_allowed(locked_advert.pairing_session, now):
            raise ContactAdditionRefusedError(
                ContactAdditionCheck(
                    problems=(ContactAdditionProblem.PAIRING_WINDOW_CLOSED,),
                    node_type=locked_advert.node_type,
                )
            )

        contact = create_contact(
            public_key=locked_advert.public_key,
            node_type=locked_advert.node_type,
            name=locked_advert.name,
            advert_timestamp=locked_advert.advert_timestamp,
            latitude_microdegrees=locked_advert.latitude_microdegrees,
            longitude_microdegrees=locked_advert.longitude_microdegrees,
            source=Contact.Source.PAIRING,
            card_uri="",
            now=now,
        )
        locked_advert.added_contact = contact
        locked_advert.save(update_fields=["added_contact"])
        return contact


def take_contact_capacity_lock() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [CONTACT_CAPACITY_LOCK_KEY])


def create_contact(
    *,
    public_key: str,
    node_type: int,
    name: str,
    advert_timestamp: int,
    latitude_microdegrees: int,
    longitude_microdegrees: int,
    source: Contact.Source,
    card_uri: str,
    now: datetime,
) -> Contact:
    """Runs under the capacity lock, inside the caller's transaction."""
    contact_addition_check = check_contact_addition(public_key, node_type)
    if not contact_addition_check.is_allowed:
        raise ContactAdditionRefusedError(contact_addition_check)

    try:
        with transaction.atomic():
            contact = Contact.objects.create(
                public_key=public_key,
                name=truncate_contact_name(name),
                node_type=node_type,
                # The firmware ignores later adverts that are not newer than the stored one.
                advert_timestamp=min(advert_timestamp, int(now.timestamp())),
                latitude_microdegrees=latitude_microdegrees,
                longitude_microdegrees=longitude_microdegrees,
                source=source,
                card_uri=card_uri,
                added_at=now,
                node_sync_state=Contact.NodeSyncState.PENDING_ADD,
            )
    except IntegrityError as integrity_error:
        raise ContactAdditionRefusedError(
            ContactAdditionCheck(problems=(ContactAdditionProblem.ALREADY_A_CONTACT,), node_type=node_type)
        ) from integrity_error

    notify_relay_worker(NotificationChannel.CONTACTS_CHANGED, str(contact.pk))
    return contact


def summarize_contact_deletion(contact: Contact) -> ContactDeletionSummary:
    return ContactDeletionSummary(
        contact=contact,
        linked_username=contact.user.username if contact.user is not None else "",
        pending_delivery_count=contact.deliveries.filter(state__in=PENDING_DELIVERY_STATES).count(),
        pending_receipt_count=contact.receipt_notifications.filter(state=ReceiptNotification.State.PENDING).count(),
        active_refresh_session_count=contact.refresh_sessions.filter(state=RefreshSession.State.ACTIVE).count(),
    )


def delete_contact(contact: Contact) -> None:
    """Delete the contact with its deliveries, receipts and refresh sessions.

    Before deleting, locks its user, the contact, the messages it sent, then its deliveries,
    receipts and refresh sessions, in that order and each table in ascending id: the lock order
    every transaction that locks several rows follows.

    The traffic log keeps its labels with a NULL contact; the reconciler removes the contact
    from the node once no packet to it awaits a firmware acknowledgement.
    """
    run_with_deadlock_retries(lambda: delete_contact_in_one_transaction(contact.pk))


def delete_contact_in_one_transaction(contact_id: int) -> None:
    with transaction.atomic():
        contact = lock_contact_after_its_user(contact_id)
        if contact is None:
            return

        lock_rows_that_go_with_contact(contact_id)
        contact.delete()
        notify_relay_worker(NotificationChannel.CONTACTS_CHANGED, str(contact_id))


def lock_contact_after_its_user(contact_id: int) -> Contact | None:
    """None when the contact is already gone."""
    contact_values = Contact.objects.filter(id=contact_id).values("user_id").first()
    if contact_values is None:
        return None

    expected_user_id = contact_values["user_id"]
    if expected_user_id is not None:
        lock_rows(User.objects.filter(id=expected_user_id))

    contact = Contact.objects.select_for_update().filter(id=contact_id).first()
    if contact is not None and contact.user_id != expected_user_id:
        raise LockedRowChangedError(f"Contact {contact_id} was relinked while it was being deleted.")
    return contact


def lock_rows_that_go_with_contact(contact_id: int) -> None:
    """The messages it sent lose their sender_device; its deliveries, receipts and refresh sessions are deleted."""
    lock_rows(Message.objects.filter(sender_device_id=contact_id))
    lock_rows(MessageDelivery.objects.filter(device_id=contact_id))
    lock_rows(ReceiptNotification.objects.filter(device_id=contact_id))
    lock_rows(RefreshSession.objects.filter(device_id=contact_id))


def lock_rows[LockedModel: models.Model](queryset: QuerySet[LockedModel]) -> list[int]:
    """SELECT ... FOR UPDATE in ascending id, the order every multi-row transaction locks a table in."""
    return list(queryset.select_for_update().order_by("id").values_list("id", flat=True))


def run_with_deadlock_retries[Result](transaction_function: Callable[[], Result]) -> Result:
    """Run a function that opens its own transaction, again when PostgreSQL aborted it for a deadlock.

    Inside an outer transaction nothing can be retried here: the outer one would already be
    aborted, so its owner has to retry it.
    """
    if connection.in_atomic_block:
        return transaction_function()

    for attempt_number in range(1, MAXIMUM_TRANSACTION_ATTEMPTS + 1):
        try:
            return transaction_function()
        except OperationalError as database_error:
            if attempt_number == MAXIMUM_TRANSACTION_ATTEMPTS or not is_retryable_database_error(database_error):
                raise
        except LockedRowChangedError:
            if attempt_number == MAXIMUM_TRANSACTION_ATTEMPTS:
                raise
    raise AssertionError("The loop either returns or raises.")


def is_retryable_database_error(database_error: OperationalError) -> bool:
    return getattr(database_error.__cause__, "sqlstate", None) in RETRYABLE_SQLSTATES
