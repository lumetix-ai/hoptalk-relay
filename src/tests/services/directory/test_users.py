import threading

import pytest
from django.db import transaction

from directory.models import Contact, User
from directory.users import delete_user, summarize_user_deletion
from messaging.models import (
    InboundDirectMessage,
    Message,
    MessageDelivery,
    OutboundPacket,
    ReceiptNotification,
    RefreshSession,
)
from tests.services.directory.row_builders import (
    create_accepted_message,
    create_contact,
    create_delivery,
    create_inbound_direct_message,
    create_outbound_packet,
    create_pending_receipt,
    create_refresh_session,
    create_user,
)
from tests.services.node.database_threads import run_concurrently, wait_until_a_transaction_waits_for_a_lock


@pytest.mark.django_db
def test_deleting_a_user_takes_its_devices_and_every_message_it_sent_or_received() -> None:
    ivan, bob, carol = create_user("ivan"), create_user("bob"), create_user("carol")
    ivans_device = create_contact(1, user=ivan)
    bobs_device = create_contact(2, user=bob)
    carols_device = create_contact(3, user=carol)
    message_from_ivan = create_accepted_message(ivan, bob, 1, sender_device=ivans_device)
    message_to_ivan = create_accepted_message(carol, ivan, 2, sender_device=carols_device)
    message_between_others = create_accepted_message(bob, carol, 3, sender_device=bobs_device)
    delivery_to_bob = create_delivery(message_from_ivan, bobs_device)
    create_delivery(message_to_ivan, ivans_device)
    create_delivery(message_between_others, carols_device)
    create_pending_receipt(message_to_ivan, carols_device)
    create_inbound_direct_message(ivans_device, "HT1 M bob 1 1/1 Hello")
    create_outbound_packet(
        bobs_device,
        "HT1 m ivan 1 1/1 Hello",
        sender_timestamp=1_790_000_001,
        purpose=OutboundPacket.Purpose.DELIVERY,
        message_delivery=delivery_to_bob,
    )

    delete_user(ivan)

    assert list(User.objects.order_by("username").values_list("username", flat=True)) == ["bob", "carol"]
    assert set(Contact.objects.values_list("id", flat=True)) == {bobs_device.pk, carols_device.pk}
    assert list(Message.objects.values_list("id", flat=True)) == [message_between_others.pk]
    assert MessageDelivery.objects.count() == 1
    assert not ReceiptNotification.objects.exists()
    inbound_row = InboundDirectMessage.objects.get()
    assert (inbound_row.contact, inbound_row.contact_label) == (None, str(ivans_device))
    outbound_packet = OutboundPacket.objects.get()
    assert outbound_packet.message_delivery is None
    assert outbound_packet.contact == bobs_device


@pytest.mark.django_db
def test_deleting_the_peer_of_another_devices_refresh_takes_the_session_and_its_deliveries() -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    bobs_device = create_contact(2, user=bob)
    refresh_session = create_refresh_session(bobs_device, peer=ivan)
    older_message = create_accepted_message(ivan, bob, 1)
    newer_message = create_accepted_message(ivan, bob, 2)
    create_delivery(older_message, bobs_device, refresh_session=refresh_session)
    create_delivery(
        newer_message, bobs_device, state=MessageDelivery.State.QUEUED_FOR_REFRESH, refresh_session=refresh_session
    )

    delete_user(ivan)

    assert not RefreshSession.objects.filter(id=refresh_session.pk).exists()
    assert not MessageDelivery.objects.exists()
    assert Contact.objects.filter(id=bobs_device.pk).exists()


@pytest.mark.django_db
def test_a_message_another_user_sent_from_a_relinked_device_only_loses_its_device() -> None:
    ivan, bob, carol = create_user("ivan"), create_user("bob"), create_user("carol")
    device_now_ivans = create_contact(1, user=ivan)
    message_bob_sent_from_it = create_accepted_message(bob, carol, 1, sender_device=device_now_ivans)

    delete_user(ivan)

    message_bob_sent_from_it.refresh_from_db()
    assert message_bob_sent_from_it.sender_device is None


@pytest.mark.django_db
def test_the_deletion_summary_counts_devices_and_messages() -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    create_contact(1, user=ivan)
    create_contact(2, user=ivan)
    create_accepted_message(ivan, bob, 1)
    create_accepted_message(bob, ivan, 2)
    create_accepted_message(bob, ivan, 3)

    deletion_summary = summarize_user_deletion(ivan)

    assert deletion_summary.device_count == 2
    assert deletion_summary.sent_message_count == 1
    assert deletion_summary.received_message_count == 2


@pytest.mark.django_db(transaction=True)
def test_a_message_accepted_while_its_recipient_is_being_deleted_waits_and_then_finds_no_user() -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    deletion_holds_its_locks = threading.Event()
    recipients_found: list[bool] = []

    def delete_slowly() -> None:
        with transaction.atomic():
            delete_user(ivan)
            deletion_holds_its_locks.set()
            wait_until_a_transaction_waits_for_a_lock()

    def accept_a_message_to_ivan() -> None:
        deletion_holds_its_locks.wait(timeout=10)
        with transaction.atomic():
            locked_users = list(User.objects.select_for_update().filter(id__in=[bob.pk, ivan.pk]).order_by("id"))
            recipients_found.append(any(user.pk == ivan.pk for user in locked_users))

    run_concurrently(delete_slowly, accept_a_message_to_ivan)

    assert recipients_found == [False]
    assert not User.objects.filter(id=ivan.pk).exists()


@pytest.mark.django_db(transaction=True)
def test_a_deletion_that_waits_for_an_accepted_message_deletes_that_message_too() -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    users_are_locked = threading.Event()

    def accept_a_message_to_ivan_slowly() -> None:
        with transaction.atomic():
            list(User.objects.select_for_update().filter(id__in=[ivan.pk, bob.pk]).order_by("id"))
            users_are_locked.set()
            wait_until_a_transaction_waits_for_a_lock()
            create_accepted_message(bob, ivan, 1)

    def delete_during_the_acceptance() -> None:
        users_are_locked.wait(timeout=10)
        delete_user(ivan)

    run_concurrently(accept_a_message_to_ivan_slowly, delete_during_the_acceptance)

    assert not User.objects.filter(id=ivan.pk).exists()
    assert not Message.objects.exists()
