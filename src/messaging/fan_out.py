"""Fan-out: an accepted message gets one delivery per device its recipient has at that moment.

It runs inside the transaction that accepts the message, so a message is never accepted
without its deliveries. A device with an active refresh session with the sender gets the
message queued at the end of that session, so the conversation stays in order on it; every
other device gets a delivery that is due at once. A recipient without devices gets no rows: a
refresh from a device it links later finds the message.
"""

from datetime import datetime

from django.db.models import F

from directory.models import Contact
from messaging.deliveries import create_pending_delivery, create_refresh_owned_delivery
from messaging.models import Message, RefreshSession


def create_deliveries_for_accepted_message(message: Message, now: datetime) -> int:
    """Returns the number of deliveries created; the caller holds the lock of the recipient user."""
    recipient_device_ids = list(
        Contact.objects.filter(user_id=message.recipient_id).order_by("id").values_list("id", flat=True)
    )
    active_session_ids_by_device_id = dict(
        RefreshSession.objects.filter(
            device_id__in=recipient_device_ids,
            peer_id=message.sender_id,
            state=RefreshSession.State.ACTIVE,
        ).values_list("device_id", "id")
    )

    for device_id in recipient_device_ids:
        active_session_id = active_session_ids_by_device_id.get(device_id)
        if active_session_id is None:
            create_pending_delivery(message.pk, device_id, now)
        else:
            create_refresh_owned_delivery(message.pk, device_id, active_session_id, now)

    for active_session_id in sorted(active_session_ids_by_device_id.values()):
        RefreshSession.objects.filter(id=active_session_id).update(messages_total=F("messages_total") + 1)

    return len(recipient_device_ids)
