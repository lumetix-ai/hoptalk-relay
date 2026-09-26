"""Operator deletions racing the worker's services on two database connections, in both orders.

A deletion locks the user, then its contacts, then every row that goes with them, before it
deletes; the worker's services lock in the same order, so whichever transaction comes second
waits and then finds a consistent database. Two worker paths lock a row out of that order (the
receipts an acknowledgement creates for the sender's devices, and the queue a refresh stop
fails); PostgreSQL then aborts one of the two transactions for a deadlock, and its retry gives
the same result as running them one after the other.
"""

import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

import pytest
from django.db import transaction
from pytest_django import Settings

from directory import users
from directory.contacts import delete_contact
from directory.models import Contact, User
from directory.users import delete_user
from messaging import outbound_scheduling, request_processing
from messaging.inbound_log import DIRECT_ARRIVAL_PATH_LENGTH, ReceivedDirectMessageFrame, record_inbound_frame
from messaging.models import (
    InboundDirectMessage,
    Message,
    MessageDelivery,
    OutboundPacket,
    ReceiptNotification,
    RefreshSession,
)
from messaging.outbound_scheduling import PacketDescriptor
from messaging.receipts import raise_receipts_for_message
from messaging.refresh_sessions import stop_refresh_session
from messaging.request_processing import InboundProcessingResult, process_inbound_direct_message
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import (
    DEFAULT_RETRY_STRATEGY,
    TEST_PASSWORD,
    RelayHarness,
    configure_engine_settings,
    create_device,
    create_user,
)
from tests.services.node.database_threads import run_concurrently, wait_until_a_transaction_waits_for_a_lock

Classification = InboundDirectMessage.Classification
EVENT_TIMEOUT_SECONDS = 10
ACKNOWLEDGEMENT_OF_MESSAGE_1 = "HT1 K ivan 1 1"


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("bob", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


@pytest.fixture
def bobs_device(bob: User, manual_clock: ManualClock) -> Contact:
    return create_device(2, manual_clock.now(), user=bob)


def commit_once_the_other_transaction_waits(work: Callable[[], None], work_holds_its_locks: threading.Event) -> None:
    """Do the work in a transaction that commits only after the other connection started waiting for its locks."""
    with transaction.atomic():
        work()
        work_holds_its_locks.set()
        wait_until_a_transaction_waits_for_a_lock()


def run_after(event: threading.Event, work: Callable[[], None]) -> Callable[[], None]:
    def wait_then_work() -> None:
        assert event.wait(timeout=EVENT_TIMEOUT_SECONDS)
        work()

    return wait_then_work


def record_frame_without_processing(relay: RelayHarness, device: Contact, text: str) -> int:
    frame = ReceivedDirectMessageFrame(
        sender_public_key_prefix=device.public_key[:12],
        sender_timestamp=relay.next_sender_timestamp,
        text=text,
        text_type=0,
        path_length=DIRECT_ARRIVAL_PATH_LENGTH,
        signal_to_noise_ratio=6.5,
        received_at=relay.clock.now(),
    )
    relay.next_sender_timestamp += 1
    return record_inbound_frame(frame, relay.clock.now()).inbox_row_id


def send_message_1_from_ivan_to_bob(relay: RelayHarness, ivans_device: Contact) -> None:
    assert relay.receive_replies(ivans_device, "HT1 M bob 1 1/1 hello") == ["HT1 k bob 1 1"]


def count_calls[**Parameters, Result](
    function: Callable[Parameters, Result], calls: list[int]
) -> Callable[Parameters, Result]:
    def count_and_call(*arguments: Parameters.args, **keyword_arguments: Parameters.kwargs) -> Result:
        calls.append(len(calls) + 1)
        return function(*arguments, **keyword_arguments)

    return count_and_call


@pytest.mark.django_db(transaction=True)
def test_a_part_to_a_user_being_deleted_waits_for_the_deletion_and_is_answered_no_such_user(
    relay: RelayHarness, bob: User, ivans_device: Contact
) -> None:
    deletion_holds_its_locks = threading.Event()
    replies: list[str] = []

    def send_a_part_to_bob() -> None:
        replies.extend(relay.receive_replies(ivans_device, "HT1 M bob 1 1/1 hello"))

    run_concurrently(
        lambda: commit_once_the_other_transaction_waits(lambda: delete_user(bob), deletion_holds_its_locks),
        run_after(deletion_holds_its_locks, send_a_part_to_bob),
    )

    assert replies == ["HT1 e NO_SUCH_USER M bob 1"]
    assert not Message.objects.exists()
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_deleting_the_recipient_while_a_message_is_accepted_waits_and_deletes_the_message_too(
    relay: RelayHarness, ivans_device: Contact, bob: User, bobs_device: Contact
) -> None:
    message_is_accepted = threading.Event()
    replies: list[str] = []

    def accept_a_message_to_bob() -> None:
        replies.extend(relay.receive_replies(ivans_device, "HT1 M bob 1 1/1 hello"))

    run_concurrently(
        lambda: commit_once_the_other_transaction_waits(accept_a_message_to_bob, message_is_accepted),
        run_after(message_is_accepted, lambda: delete_user(bob)),
    )

    assert replies == ["HT1 k bob 1 1"]
    assert not User.objects.filter(id=bob.pk).exists()
    assert not Contact.objects.filter(id=bobs_device.pk).exists()
    assert not Message.objects.exists()
    assert not MessageDelivery.objects.exists()
    assert InboundDirectMessage.objects.get().processing_state == InboundDirectMessage.ProcessingState.PROCESSED
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_a_request_from_a_device_deleted_while_it_is_processed_is_dropped_as_from_an_unknown_sender(
    relay: RelayHarness, manual_clock: ManualClock, bobs_device: Contact
) -> None:
    sign_in_text = f"HT1 A bob {TEST_PASSWORD}"
    inbox_row_id = record_frame_without_processing(relay, bobs_device, sign_in_text)
    deletion_holds_its_locks = threading.Event()
    processing_results: list[InboundProcessingResult] = []

    def process_the_sign_in() -> None:
        processing_results.append(
            process_inbound_direct_message(inbox_row_id, manual_clock.now(), original_text=sign_in_text)
        )

    run_concurrently(
        lambda: commit_once_the_other_transaction_waits(lambda: delete_contact(bobs_device), deletion_holds_its_locks),
        run_after(deletion_holds_its_locks, process_the_sign_in),
    )

    [processing_result] = processing_results
    assert processing_result.processing_state == InboundDirectMessage.ProcessingState.PROCESSED
    assert processing_result.classification == Classification.UNKNOWN_SENDER
    assert processing_result.replies == ()
    assert not Contact.objects.exists()
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_an_acknowledgement_recorded_while_its_device_is_deleted_waits_and_changes_nothing(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message_1_from_ivan_to_bob(relay, ivans_device)
    relay.send_due_packets()
    deletion_holds_its_locks = threading.Event()
    processing_results: list[InboundProcessingResult | None] = []

    def acknowledge_message_1() -> None:
        processing_results.append(relay.receive(bobs_device, ACKNOWLEDGEMENT_OF_MESSAGE_1))

    run_concurrently(
        lambda: commit_once_the_other_transaction_waits(lambda: delete_contact(bobs_device), deletion_holds_its_locks),
        run_after(deletion_holds_its_locks, acknowledge_message_1),
    )

    [processing_result] = processing_results
    assert processing_result is not None
    assert processing_result.classification == Classification.UNKNOWN_SENDER
    assert processing_result.replies == ()
    message = Message.objects.get()
    assert message.delivered_at is None
    assert not MessageDelivery.objects.exists()
    assert not ReceiptNotification.objects.exists()
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_deleting_a_device_whose_acknowledgement_is_being_processed_waits_and_the_message_stays_delivered(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message_1_from_ivan_to_bob(relay, ivans_device)
    relay.send_due_packets()
    acknowledgement_is_processed = threading.Event()

    def acknowledge_message_1() -> None:
        relay.receive(bobs_device, ACKNOWLEDGEMENT_OF_MESSAGE_1)

    run_concurrently(
        lambda: commit_once_the_other_transaction_waits(acknowledge_message_1, acknowledgement_is_processed),
        run_after(acknowledgement_is_processed, lambda: delete_contact(bobs_device)),
    )

    assert not Contact.objects.filter(id=bobs_device.pk).exists()
    assert Message.objects.get().delivered_at == manual_clock.now()
    assert not MessageDelivery.objects.exists()
    assert list(ReceiptNotification.objects.values_list("device_id", flat=True)) == [ivans_device.pk]
    acknowledgement_row = InboundDirectMessage.objects.get(text=ACKNOWLEDGEMENT_OF_MESSAGE_1)
    assert acknowledgement_row.contact is None
    assert acknowledgement_row.classification == Classification.ACKNOWLEDGEMENT
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_the_sender_skips_a_device_being_deleted_and_never_sends_to_it(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message_1_from_ivan_to_bob(relay, ivans_device)
    deletion_holds_its_locks = threading.Event()
    sender_is_done = threading.Event()
    prepared_packets: list[PacketDescriptor | None] = []

    def delete_bobs_device_until_the_sender_is_done() -> None:
        with transaction.atomic():
            delete_contact(bobs_device)
            deletion_holds_its_locks.set()
            assert sender_is_done.wait(timeout=EVENT_TIMEOUT_SECONDS)

    def prepare_the_next_packet() -> None:
        try:
            prepared_packets.append(relay.prepare_next_packet())
        finally:
            sender_is_done.set()

    run_concurrently(
        delete_bobs_device_until_the_sender_is_done, run_after(deletion_holds_its_locks, prepare_the_next_packet)
    )

    assert prepared_packets == [None]
    assert relay.prepare_next_packet() is None
    assert not OutboundPacket.objects.exists()
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_deleting_a_device_while_a_part_to_it_is_prepared_waits_and_the_late_send_outcome_changes_nothing(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    send_message_1_from_ivan_to_bob(relay, ivans_device)
    part_is_prepared = threading.Event()
    prepared_packets: list[PacketDescriptor | None] = []

    def prepare_the_next_packet() -> None:
        prepared_packets.append(relay.prepare_next_packet())

    run_concurrently(
        lambda: commit_once_the_other_transaction_waits(prepare_the_next_packet, part_is_prepared),
        run_after(part_is_prepared, lambda: delete_contact(bobs_device)),
    )
    [packet_descriptor] = prepared_packets
    assert packet_descriptor is not None
    relay.record_queued_on_node(packet_descriptor)

    packet = OutboundPacket.objects.get()
    assert packet.contact is None
    assert packet.message_delivery is None
    assert packet.contact_label == str(bobs_device)
    assert packet.state == OutboundPacket.State.QUEUED_ON_NODE
    assert not MessageDelivery.objects.exists()
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_deleting_the_sender_while_an_acknowledgement_creates_its_receipts_survives_the_deadlock(
    relay: RelayHarness,
    monkeypatch: pytest.MonkeyPatch,
    ivan: User,
    ivans_device: Contact,
    bobs_device: Contact,
) -> None:
    send_message_1_from_ivan_to_bob(relay, ivans_device)
    relay.send_due_packets()
    receipts_are_created = threading.Event()
    processing_attempts: list[int] = []
    deletion_attempts: list[int] = []

    def create_receipts_then_let_the_deletion_lock_the_senders_devices(message: Message, now: datetime) -> None:
        raise_receipts_for_message(message, now)
        if not receipts_are_created.is_set():
            receipts_are_created.set()
            wait_until_a_transaction_waits_for_a_lock()

    monkeypatch.setattr(
        "messaging.reads_and_acknowledgements.raise_receipts_for_message",
        create_receipts_then_let_the_deletion_lock_the_senders_devices,
    )
    monkeypatch.setattr(
        request_processing,
        "process_in_transaction",
        count_calls(request_processing.process_in_transaction, processing_attempts),
    )
    monkeypatch.setattr(
        users, "delete_user_in_one_transaction", count_calls(users.delete_user_in_one_transaction, deletion_attempts)
    )

    def acknowledge_message_1() -> None:
        relay.receive(bobs_device, ACKNOWLEDGEMENT_OF_MESSAGE_1)

    run_concurrently(
        acknowledge_message_1,
        run_after(receipts_are_created, lambda: delete_user(ivan)),
    )

    assert len(processing_attempts) + len(deletion_attempts) == 3
    assert not User.objects.filter(id=ivan.pk).exists()
    assert not Message.objects.exists()
    assert not ReceiptNotification.objects.exists()
    assert Contact.objects.filter(id=bobs_device.pk).exists()
    acknowledgement_row = InboundDirectMessage.objects.get(text=ACKNOWLEDGEMENT_OF_MESSAGE_1)
    assert acknowledgement_row.processing_state == InboundDirectMessage.ProcessingState.PROCESSED
    assert_all_invariants()


@pytest.mark.django_db(transaction=True)
def test_deleting_the_peer_while_a_refresh_stop_fails_its_queue_survives_the_deadlock(
    relay: RelayHarness,
    manual_clock: ManualClock,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    ivan: User,
    ivans_device: Contact,
    bob: User,
) -> None:
    configure_engine_settings(settings, retry_strategy=replace(DEFAULT_RETRY_STRATEGY, maximum_attempts=1))
    # Message 1 reaches bob while he has no device, so the refresh creates its delivery last and
    # the head has a higher id than the queued deliveries of messages 2 and 3, which the
    # deletion then locks first.
    send_message_1_from_ivan_to_bob(relay, ivans_device)
    bobs_device = create_device(2, manual_clock.now(), user=bob, node_sync_state=Contact.NodeSyncState.PENDING_ADD)
    assert relay.receive_replies(ivans_device, "HT1 M bob 2 1/1 second") == ["HT1 k bob 2 1"]
    assert relay.receive_replies(ivans_device, "HT1 M bob 3 1/1 third") == ["HT1 k bob 3 1"]
    assert relay.receive_replies(bobs_device, "HT1 F ivan") == ["HT1 f ivan 3"]
    Contact.objects.filter(id=bobs_device.pk).update(node_sync_state=Contact.NodeSyncState.ON_NODE)
    assert relay.send_due_texts() == ["HT1 m ivan 1 1/1 hello"]
    manual_clock.advance(minutes=5)

    head_has_failed = threading.Event()
    scheduling_attempts: list[int] = []
    deletion_attempts: list[int] = []

    def let_the_deletion_lock_the_queue_then_stop(refresh_session_id: int, now: datetime) -> None:
        if not head_has_failed.is_set():
            head_has_failed.set()
            wait_until_a_transaction_waits_for_a_lock()
        stop_refresh_session(refresh_session_id, now)

    monkeypatch.setattr("messaging.outbound_scheduling.stop_refresh_session", let_the_deletion_lock_the_queue_then_stop)
    monkeypatch.setattr(
        outbound_scheduling,
        "prepare_next_packet_in_transaction",
        count_calls(outbound_scheduling.prepare_next_packet_in_transaction, scheduling_attempts),
    )
    monkeypatch.setattr(
        users, "delete_user_in_one_transaction", count_calls(users.delete_user_in_one_transaction, deletion_attempts)
    )

    def fail_the_exhausted_head() -> None:
        assert relay.prepare_next_packet() is None

    run_concurrently(
        fail_the_exhausted_head,
        run_after(head_has_failed, lambda: delete_user(ivan)),
    )

    assert len(scheduling_attempts) + len(deletion_attempts) == 3
    assert not User.objects.filter(id=ivan.pk).exists()
    assert not Message.objects.exists()
    assert not MessageDelivery.objects.exists()
    assert not RefreshSession.objects.exists()
    assert Contact.objects.filter(id=bobs_device.pk).exists()
    assert_all_invariants()
