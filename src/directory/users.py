from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q

from directory.contacts import lock_rows, run_with_deadlock_retries
from directory.models import Contact, User
from messaging.models import Message, MessageDelivery, ReceiptNotification, RefreshSession
from node.notification_channels import NotificationChannel, notify_relay_worker


@dataclass(frozen=True, kw_only=True)
class UserDeletionSummary:
    user: User
    device_count: int
    sent_message_count: int
    received_message_count: int


def summarize_user_deletion(user: User) -> UserDeletionSummary:
    return UserDeletionSummary(
        user=user,
        device_count=user.devices.count(),
        sent_message_count=user.sent_messages.count(),
        received_message_count=user.received_messages.count(),
    )


def delete_user(user: User) -> None:
    """Delete the user with its devices, every message it sent or received and every refresh session with it as peer.

    Before deleting, locks the user, its contacts, then every message, delivery, receipt and
    refresh session the deletion deletes or updates, in that order and each table in ascending
    id: the lock order every transaction that locks several rows follows. Notifies
    relay_contacts_changed in the same transaction.
    """
    run_with_deadlock_retries(lambda: delete_user_in_one_transaction(user.pk))


def delete_user_in_one_transaction(user_id: int) -> None:
    with transaction.atomic():
        locked_user = User.objects.select_for_update().filter(id=user_id).first()
        if locked_user is None:
            return

        device_ids = lock_rows(Contact.objects.filter(user_id=user_id))
        lock_rows_that_go_with_user(user_id, device_ids)
        locked_user.delete()
        notify_relay_worker(NotificationChannel.CONTACTS_CHANGED, "")


def lock_rows_that_go_with_user(user_id: int, device_ids: list[int]) -> None:
    """Lock the messages the user sent or received, and every delivery, receipt and refresh session that goes too.

    Those are the rows of its messages and of its devices, and the refresh sessions with the
    user as peer. A message another user sent from one of its devices, before a relink, is
    locked as well: it stays and only loses its sender_device.
    """
    message_ids = lock_rows(
        Message.objects.filter(Q(sender_id=user_id) | Q(recipient_id=user_id) | Q(sender_device_id__in=device_ids))
    )
    lock_rows(MessageDelivery.objects.filter(Q(message_id__in=message_ids) | Q(device_id__in=device_ids)))
    lock_rows(ReceiptNotification.objects.filter(Q(message_id__in=message_ids) | Q(device_id__in=device_ids)))
    lock_rows(RefreshSession.objects.filter(Q(device_id__in=device_ids) | Q(peer_id=user_id)))
