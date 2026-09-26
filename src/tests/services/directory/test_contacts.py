import threading
from datetime import UTC, datetime, timedelta

import pytest
from django.db import OperationalError, transaction

from directory.contacts import (
    CONTACT_CAPACITY,
    ContactAdditionProblem,
    ContactAdditionRefusedError,
    add_contact_from_card,
    add_contact_from_heard_advert,
    check_contact_addition,
    delete_contact,
    run_with_deadlock_retries,
    summarize_contact_deletion,
)
from directory.models import Contact
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket, ReceiptNotification
from node.contact_cards import ContactCard, parse_contact_card_uri
from node.models import HeardAdvert, WorkerStatus
from node.node_settings import replace_node_configuration
from node.pairing_sessions import record_heard_advert, start_pairing_session, stop_pairing_session
from tests.services.directory.row_builders import (
    build_public_key,
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
from tests.services.node.node_builders import (
    SAMPLE_CONTACT_CARD_PUBLIC_KEY,
    SAMPLE_CONTACT_CARD_URI,
    ContactCardSigner,
    build_node_configuration,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def build_card(
    public_key: str, node_type: int = 1, name: str = "Alice", advert_timestamp: int = 1_790_000_000
) -> ContactCard:
    return ContactCard(
        public_key=public_key,
        advert_timestamp=advert_timestamp,
        node_type=node_type,
        name=name,
        latitude_microdegrees=0,
        longitude_microdegrees=0,
        card_uri=f"meshcore://11{public_key}",
    )


def refused_problems(contact_card: ContactCard) -> tuple[ContactAdditionProblem, ...]:
    with pytest.raises(ContactAdditionRefusedError) as refusal:
        add_contact_from_card(contact_card, NOW)
    return refusal.value.contact_addition_check.problems


@pytest.mark.django_db
def test_a_card_becomes_a_contact_waiting_to_be_added_to_the_node() -> None:
    contact = add_contact_from_card(parse_contact_card_uri(SAMPLE_CONTACT_CARD_URI), NOW)

    assert contact.public_key == SAMPLE_CONTACT_CARD_PUBLIC_KEY
    assert contact.public_key_prefix == SAMPLE_CONTACT_CARD_PUBLIC_KEY[:12]
    assert contact.name == "Liam Cottle 🤠"
    assert contact.source == Contact.Source.CARD
    assert contact.card_uri == SAMPLE_CONTACT_CARD_URI
    assert contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD
    assert contact.user is None
    assert contact.added_at == NOW


@pytest.mark.django_db
def test_the_same_card_added_twice_is_refused_as_already_a_contact() -> None:
    contact_card = parse_contact_card_uri(SAMPLE_CONTACT_CARD_URI)
    existing_contact = add_contact_from_card(contact_card, NOW)

    with pytest.raises(ContactAdditionRefusedError, match="already a contact") as refusal:
        add_contact_from_card(contact_card, NOW)

    assert refusal.value.contact_addition_check.problems == (ContactAdditionProblem.ALREADY_A_CONTACT,)
    assert refusal.value.contact_addition_check.conflicting_contact == existing_contact
    assert Contact.objects.count() == 1


@pytest.mark.django_db
def test_a_key_whose_first_six_bytes_equal_another_contacts_is_refused() -> None:
    existing_contact = create_contact(7)
    colliding_key = existing_contact.public_key[:12] + "cd" * 26

    contact_addition_check = check_contact_addition(colliding_key, node_type=1)

    assert contact_addition_check.problems == (ContactAdditionProblem.PREFIX_COLLISION,)
    assert contact_addition_check.conflicting_contact == existing_contact
    assert "could not tell their messages apart" in contact_addition_check.describe_problems()[0]
    assert refused_problems(build_card(colliding_key)) == (ContactAdditionProblem.PREFIX_COLLISION,)


@pytest.mark.django_db
@pytest.mark.parametrize("where_the_key_is_known", ["node_setting", "worker_status"])
def test_the_relay_nodes_own_card_is_refused(where_the_key_is_known: str) -> None:
    if where_the_key_is_known == "node_setting":
        replace_node_configuration(build_node_configuration(public_key=SAMPLE_CONTACT_CARD_PUBLIC_KEY))
    else:
        WorkerStatus.objects.create(node_public_key=SAMPLE_CONTACT_CARD_PUBLIC_KEY)

    assert refused_problems(parse_contact_card_uri(SAMPLE_CONTACT_CARD_URI)) == (
        ContactAdditionProblem.RELAY_NODE_ITSELF,
    )


@pytest.mark.django_db
def test_a_node_that_is_not_a_chat_node_is_refused_with_a_clear_message() -> None:
    repeater_card = parse_contact_card_uri(ContactCardSigner().build_card_uri(name="Hilltop", flags=0x82))

    with pytest.raises(ContactAdditionRefusedError) as refusal:
        add_contact_from_card(repeater_card, NOW)

    assert refusal.value.contact_addition_check.problems == (ContactAdditionProblem.NOT_A_CHAT_NODE,)
    assert str(refusal.value) == "This is a repeater, and only a chat node can be a contact."


@pytest.mark.django_db
def test_the_three_hundred_fifty_first_contact_is_refused() -> None:
    Contact.objects.bulk_create(
        Contact(public_key=build_public_key(contact_number), source=Contact.Source.CARD, added_at=NOW)
        for contact_number in range(CONTACT_CAPACITY)
    )

    assert refused_problems(build_card(build_public_key(1000))) == (ContactAdditionProblem.CAPACITY_REACHED,)


@pytest.mark.django_db(transaction=True)
def test_two_concurrent_adds_cannot_both_take_the_last_place() -> None:
    Contact.objects.bulk_create(
        Contact(public_key=build_public_key(contact_number), source=Contact.Source.CARD, added_at=NOW)
        for contact_number in range(CONTACT_CAPACITY - 1)
    )
    start_together = threading.Barrier(2)

    def add_card(contact_number: int) -> bool:
        start_together.wait(timeout=10)
        try:
            add_contact_from_card(build_card(build_public_key(contact_number)), NOW)
        except ContactAdditionRefusedError:
            return False
        return True

    outcomes = run_concurrently(lambda: add_card(1001), lambda: add_card(1002))

    assert sorted(outcomes) == [False, True]
    assert Contact.objects.count() == CONTACT_CAPACITY


@pytest.mark.django_db
def test_an_advert_time_ahead_of_the_server_clock_is_stored_as_now() -> None:
    future_timestamp = int((NOW + timedelta(days=30)).timestamp())

    contact = add_contact_from_card(build_card(build_public_key(1), advert_timestamp=future_timestamp), NOW)

    assert contact.advert_timestamp == int(NOW.timestamp())


def hear_advert(public_key: str, node_type: int = 1, session_start: datetime = NOW) -> HeardAdvert:
    pairing_session = start_pairing_session(
        duration_seconds=120, advert_interval_seconds=30, advert_flood=False, start_command=None, now=session_start
    )
    return record_heard_advert(
        pairing_session.pk,
        {"public_key": public_key, "type": node_type, "adv_name": "Bob's tracker", "last_advert": 1_790_000_000},
        session_start,
    )


@pytest.mark.django_db
def test_a_heard_advert_becomes_a_pairing_contact_linked_from_the_advert() -> None:
    heard_advert = hear_advert(build_public_key(5))

    contact = add_contact_from_heard_advert(heard_advert, NOW + timedelta(seconds=30))

    heard_advert.refresh_from_db()
    assert heard_advert.added_contact == contact
    assert contact.source == Contact.Source.PAIRING
    assert contact.name == "Bob's tracker"
    assert contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD
    with pytest.raises(ContactAdditionRefusedError, match="already a contact"):
        add_contact_from_heard_advert(heard_advert, NOW + timedelta(seconds=40))


@pytest.mark.django_db
def test_a_heard_advert_can_be_added_only_until_fifteen_minutes_after_its_session() -> None:
    heard_advert = hear_advert(build_public_key(5))
    stop_pairing_session(heard_advert.pairing_session_id, NOW)

    with pytest.raises(ContactAdditionRefusedError, match="more than 15 minutes ago") as refusal:
        add_contact_from_heard_advert(heard_advert, NOW + timedelta(minutes=16))

    assert refusal.value.contact_addition_check.problems == (ContactAdditionProblem.PAIRING_WINDOW_CLOSED,)
    assert add_contact_from_heard_advert(heard_advert, NOW + timedelta(minutes=14)).source == Contact.Source.PAIRING


@pytest.mark.django_db
def test_a_heard_repeater_cannot_be_added() -> None:
    heard_advert = hear_advert(build_public_key(5), node_type=2)

    with pytest.raises(ContactAdditionRefusedError, match="This is a repeater"):
        add_contact_from_heard_advert(heard_advert, NOW)


def create_device_with_traffic() -> tuple[Contact, Message, Message]:
    """A device of bob that sent a message to ivan and has a pending delivery of one from ivan."""
    ivan, bob = create_user("ivan"), create_user("bob")
    ivans_device = create_contact(1, user=ivan)
    bobs_device = create_contact(2, user=bob, name="Bob's tracker")
    message_from_bob = create_accepted_message(bob, ivan, 1, sender_device=bobs_device)
    message_to_bob = create_accepted_message(ivan, bob, 2, sender_device=ivans_device)
    create_delivery(message_to_bob, bobs_device)
    create_delivery(message_from_bob, ivans_device)
    create_pending_receipt(message_from_bob, bobs_device)
    create_refresh_session(bobs_device, ivan)
    create_inbound_direct_message(bobs_device, "HT1 K ivan 2 1")
    create_outbound_packet(bobs_device, "HT1 m ivan 2 1/1 Hello", sender_timestamp=1_790_000_001)
    return bobs_device, message_from_bob, message_to_bob


@pytest.mark.django_db
def test_deleting_a_device_takes_its_deliveries_receipts_and_sessions_and_keeps_the_traffic_log() -> None:
    bobs_device, message_from_bob, message_to_bob = create_device_with_traffic()
    heard_advert = hear_advert(build_public_key(9))
    HeardAdvert.objects.filter(id=heard_advert.pk).update(added_contact=bobs_device)

    delete_contact(bobs_device)

    assert not Contact.objects.filter(id=bobs_device.pk).exists()
    assert not MessageDelivery.objects.filter(device_id=bobs_device.pk).exists()
    assert not ReceiptNotification.objects.exists()
    assert MessageDelivery.objects.count() == 1
    message_from_bob.refresh_from_db()
    assert message_from_bob.sender_device is None
    assert Message.objects.filter(id=message_to_bob.pk).exists()
    inbound_row = InboundDirectMessage.objects.get()
    assert inbound_row.contact is None
    assert inbound_row.contact_label == str(bobs_device)
    outbound_packet = OutboundPacket.objects.get()
    assert outbound_packet.contact is None
    assert outbound_packet.contact_label == str(bobs_device)
    heard_advert.refresh_from_db()
    assert heard_advert.added_contact is None


@pytest.mark.django_db
def test_deleting_a_contact_that_is_already_gone_changes_nothing() -> None:
    contact = create_contact(1)
    Contact.objects.filter(id=contact.pk).delete()

    delete_contact(contact)


@pytest.mark.django_db
def test_the_deletion_summary_counts_what_goes_with_a_device() -> None:
    bobs_device, _message_from_bob, _message_to_bob = create_device_with_traffic()

    deletion_summary = summarize_contact_deletion(bobs_device)

    assert deletion_summary.linked_username == "bob"
    assert deletion_summary.pending_delivery_count == 1
    assert deletion_summary.pending_receipt_count == 1
    assert deletion_summary.active_refresh_session_count == 1


@pytest.mark.django_db(transaction=True)
def test_a_deletion_waits_for_a_worker_transaction_that_holds_the_contact_and_then_deletes() -> None:
    contact = create_contact(1)
    contact_is_locked = threading.Event()

    def record_a_frame_from_the_contact() -> None:
        with transaction.atomic():
            Contact.objects.filter(id=contact.pk).update(last_heard_at=NOW)
            contact_is_locked.set()
            wait_until_a_transaction_waits_for_a_lock()
            create_inbound_direct_message(contact, "HT1 Q bob")

    def delete_while_the_frame_is_recorded() -> None:
        contact_is_locked.wait(timeout=10)
        delete_contact(contact)

    run_concurrently(record_a_frame_from_the_contact, delete_while_the_frame_is_recorded)

    assert not Contact.objects.exists()
    inbound_row = InboundDirectMessage.objects.get()
    assert inbound_row.contact is None
    assert inbound_row.contact_label == str(contact)


@pytest.mark.django_db(transaction=True)
def test_a_worker_transaction_that_waits_for_a_deletion_finds_the_contact_gone() -> None:
    bobs_device = create_contact(2, user=create_user("bob"))
    rows_are_locked = threading.Event()
    updated_row_counts: list[int] = []

    def delete_slowly() -> None:
        with transaction.atomic():
            delete_contact(bobs_device)
            rows_are_locked.set()
            wait_until_a_transaction_waits_for_a_lock()

    def mark_the_contact_heard() -> None:
        rows_are_locked.wait(timeout=10)
        updated_row_counts.append(Contact.objects.filter(id=bobs_device.pk).update(last_heard_at=NOW))

    run_concurrently(delete_slowly, mark_the_contact_heard)

    assert updated_row_counts == [0]
    assert not Contact.objects.exists()


class DeadlockDetectedError(Exception):
    sqlstate = "40P01"


def test_a_transaction_aborted_by_a_deadlock_runs_again() -> None:
    attempts: list[int] = []

    def deadlock_once() -> str:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OperationalError("deadlock detected") from DeadlockDetectedError()
        return "done"

    assert run_with_deadlock_retries(deadlock_once) == "done"
    assert attempts == [1, 2]


def test_other_database_errors_and_a_third_deadlock_are_not_retried() -> None:
    def always_deadlock() -> None:
        raise OperationalError("deadlock detected") from DeadlockDetectedError()

    with pytest.raises(OperationalError):
        run_with_deadlock_retries(always_deadlock)

    connection_lost_attempts: list[int] = []

    def lose_the_connection() -> None:
        connection_lost_attempts.append(1)
        raise OperationalError("server closed the connection")

    with pytest.raises(OperationalError):
        run_with_deadlock_retries(lose_the_connection)
    assert connection_lost_attempts == [1]
