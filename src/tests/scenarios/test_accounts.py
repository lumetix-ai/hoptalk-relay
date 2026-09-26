"""Accounts end to end: registering, a second device, the wrong-password throttle, and moving a device between accounts.

Each scenario runs the real relay worker against the fake node and the simulated mesh, with reference
clients on the devices, and loses chosen direct messages on the way.
"""

import functools
from dataclasses import dataclass
from datetime import datetime, timedelta

import pytest

from directory.models import User
from messaging.models import InboundDirectMessage, MessageDelivery, OutboundPacket, ReceiptNotification
from protocol.constants import SIGN_IN_FAILURE_WINDOW_MINUTES, SIGN_IN_FAILURES_BEFORE_RATE_LIMIT, ErrorCode
from tests.invariants import assert_all_invariants
from tests.scenarios.accounts_and_messages_helpers import (
    LossPeriod,
    count_processed_inbox_rows_from,
    deliver_delayed_retry,
    find_refreshes_of_every_conversation,
    find_sent_direct_messages,
    has_receipt_in_state,
    has_receipt_sent_at_least_once,
    lose_direct_messages_to,
    move_worker_clock_to,
    read_contact,
    read_delivery,
    read_inbox_rows_from,
    read_messages_sent_by,
    read_packets_of_delivery,
    read_packets_of_receipt,
    read_receipt,
    read_user,
    start_relay_with_devices,
    text_is,
    text_starts_with,
    until_the_relay_node_took,
    wait_for_answered_refresh_of_every_conversation,
    wait_until_worker_time_passes,
)
from tests.scenarios.scenario_settings import SCENARIO_WORKER_TIMING
from tests.scenarios.scenario_setup import DEFAULT_PASSWORD, ClientStarter, sign_in
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database, wait_for_database
from tests.worker.simulated_hoptalk_client_records import (
    FAILURE_REASON_ACCOUNT_SWITCHED,
    ClientEventKind,
    OutgoingMessageStatus,
    RequestState,
)

pytestmark = pytest.mark.django_db(transaction=True)

WRONG_PASSWORD = "correct horse batterx"
# Twice the longest sleep of the worker's sender loop: by then it has sent whatever was due.
SENDER_LOOP_CATCH_UP_TIME = timedelta(seconds=2 * SCENARIO_WORKER_TIMING.maximum_sender_sleep_seconds)
ACCOUNT_EVENT_KINDS = frozenset(
    {
        ClientEventKind.ACCOUNT_SWITCH_STARTED,
        ClientEventKind.REQUESTS_SET_ASIDE,
        ClientEventKind.SIGN_IN_FAILED,
        ClientEventKind.REQUESTS_RESUMED,
        ClientEventKind.REQUESTS_DISCARDED,
        ClientEventKind.CONVERSATIONS_CLEARED,
        ClientEventKind.SIGNED_IN,
    }
)


@dataclass(frozen=True, kw_only=True)
class AccountAndLink:
    user_id: int
    username: str
    password_hash: str
    created_at: datetime
    device_user_id: int | None
    device_linked_at: datetime | None


def read_account_and_link(username: str, device: SimulatedDevice) -> AccountAndLink:
    user = read_user(username)
    contact = read_contact(device)
    return AccountAndLink(
        user_id=user.pk,
        username=user.username,
        password_hash=user.password_hash,
        created_at=user.created_at,
        device_user_id=contact.user_id,
        device_linked_at=contact.linked_at,
    )


def read_account_request_outcomes_from(device: SimulatedDevice) -> list[str]:
    return [inbox_row.outcome_summary for inbox_row in read_inbox_rows_from(device) if inbox_row.request_type == "A"]


def read_username_of_device(device: SimulatedDevice) -> str | None:
    contact = read_contact(device)
    if contact.user_id is None:
        return None
    return User.objects.values_list("username", flat=True).get(id=contact.user_id)


def is_owned_by_a_refresh_of_every_peer(delivery: MessageDelivery) -> bool:
    return delivery.refresh_session is not None and delivery.refresh_session.requested_for_all_peers


def is_delivered_by_a_refresh_of_every_peer(
    sender_username: str, client_message_id: int, device: SimulatedDevice
) -> bool:
    delivery = read_delivery(sender_username, client_message_id, device)
    return delivery.state == MessageDelivery.State.DELIVERED and is_owned_by_a_refresh_of_every_peer(delivery)


def has_delivery_sent_at_least_once(sender_username: str, client_message_id: int, device: SimulatedDevice) -> bool:
    return read_delivery(sender_username, client_message_id, device).attempt_count >= 1


def read_refresh_start(device: SimulatedDevice) -> datetime:
    """When the latest refresh of the device was requested."""
    refresh_request_row = (
        InboundDirectMessage.objects.filter(contact__public_key=device.public_key.hex(), request_type="F")
        .order_by("-id")
        .first()
    )
    assert refresh_request_row is not None
    assert refresh_request_row.processed_at is not None
    return refresh_request_row.processed_at


def find_packets_prepared_after(packets: list[OutboundPacket], moment: datetime) -> list[OutboundPacket]:
    return [packet for packet in packets if packet.prepared_at is not None and packet.prepared_at > moment]


async def test_a_retried_registration_whose_answer_was_lost_gets_the_answer_and_changes_nothing(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    [alice_device] = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone"
    )
    lost_account_replies = lose_direct_messages_to(
        alice_device,
        text_is("HT1 a alice"),
        applies=until_the_relay_node_took(simulated_mesh, alice_device, "HT1 A alice ", count=2),
    )
    alice = start_client(alice_device)

    registration = alice.sign_in("alice", DEFAULT_PASSWORD)
    await wait_for_database(
        lambda: count_processed_inbox_rows_from(alice_device, "HT1 A alice ") >= 1,
        description="the registration to be processed",
    )
    account_after_registration = await in_database(read_account_and_link, "alice", alice_device)
    await registration.wait_until_finished()
    refresh = await wait_for_answered_refresh_of_every_conversation(alice)

    await relay_worker.stop()
    assert lost_account_replies.lost_texts, "the answer to the registration was never lost"
    assert registration.is_signed_in
    assert registration.signed_in_username == "alice"
    assert registration.retry_rounds >= 1
    assert refresh.reported_message_count == 0
    assert alice.received_texts("f") == ["HT1 f * 0"]

    assert await in_database(read_account_and_link, "alice", alice_device) == account_after_registration
    assert await in_database(User.objects.count) == 1
    account_rows = [row for row in await in_database(read_inbox_rows_from, alice_device) if row.request_type == "A"]
    assert len(account_rows) >= 2
    assert account_rows[0].outcome_summary == "account created; device linked"
    assert {row.outcome_summary for row in account_rows[1:]} == {"signed in; the device was already linked"}
    assert {row.reply_summary for row in account_rows} == {"HT1 a alice"}
    assert {row.text for row in account_rows} == {"HT1 A alice ********"}
    await in_database(assert_all_invariants)


async def test_wrong_passwords_count_per_device_retries_included_and_rate_limit_only_that_device_for_its_window(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    phone_device, tablet_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "ivan-phone", "ivan-tablet"
    )
    phone = start_client(phone_device)
    await sign_in(phone, "ivan")
    lost_wrong_password_errors = lose_direct_messages_to(
        tablet_device,
        text_is("HT1 e WRONG_PASSWORD A ivan"),
        applies=until_the_relay_node_took(simulated_mesh, tablet_device, "HT1 A ivan ", count=2),
    )
    tablet = start_client(tablet_device)

    retried_wrong_sign_in = tablet.sign_in("ivan", WRONG_PASSWORD)
    await retried_wrong_sign_in.wait_until_finished()
    assert lost_wrong_password_errors.lost_texts, "the first wrong-password error was never lost"
    assert retried_wrong_sign_in.error_code == ErrorCode.WRONG_PASSWORD
    assert retried_wrong_sign_in.retry_rounds == 1
    assert (await in_database(read_contact, tablet_device)).failed_sign_in_count == 2

    for _ in range(SIGN_IN_FAILURES_BEFORE_RATE_LIMIT - 2):
        wrong_sign_in = tablet.sign_in("ivan", WRONG_PASSWORD)
        await wrong_sign_in.wait_until_finished()
        assert wrong_sign_in.error_code == ErrorCode.WRONG_PASSWORD
    tablet_after_five_failures = await in_database(read_contact, tablet_device)
    assert tablet_after_five_failures.failed_sign_in_count == SIGN_IN_FAILURES_BEFORE_RATE_LIMIT
    window_started_at = tablet_after_five_failures.failed_sign_in_window_started_at
    assert window_started_at is not None
    window_ends_at = window_started_at + timedelta(minutes=SIGN_IN_FAILURE_WINDOW_MINUTES)

    right_sign_in_while_limited = tablet.sign_in("ivan", DEFAULT_PASSWORD)
    await tablet.wait_until(
        lambda: right_sign_in_while_limited.is_rate_limited, description="the right password to be rate limited"
    )
    phone_sign_in = phone.sign_in("ivan", DEFAULT_PASSWORD)
    await phone_sign_in.wait_until_finished()
    assert phone_sign_in.is_signed_in, "the rate limit of the tablet reached the phone"
    await wait_for_answered_refresh_of_every_conversation(phone)

    await simulated_mesh.wait_until_idle()
    move_worker_clock_to(relay_worker, window_ends_at - timedelta(minutes=1))
    sign_in_near_the_window_end = tablet.sign_in("ivan", DEFAULT_PASSWORD)
    await tablet.wait_until(
        lambda: sign_in_near_the_window_end.is_rate_limited, description="a sign-in near the window's end"
    )
    tablet_near_the_window_end = await in_database(read_contact, tablet_device)
    assert tablet_near_the_window_end.user_id is None
    assert tablet_near_the_window_end.failed_sign_in_count == SIGN_IN_FAILURES_BEFORE_RATE_LIMIT

    await simulated_mesh.wait_until_idle()
    move_worker_clock_to(relay_worker, window_ends_at + timedelta(minutes=1))
    wrong_sign_in_after_the_window = tablet.sign_in("ivan", WRONG_PASSWORD)
    await wrong_sign_in_after_the_window.wait_until_finished()
    assert wrong_sign_in_after_the_window.error_code == ErrorCode.WRONG_PASSWORD
    tablet_after_the_window = await in_database(read_contact, tablet_device)
    assert tablet_after_the_window.failed_sign_in_count == 1
    assert tablet_after_the_window.failed_sign_in_window_started_at is not None
    assert tablet_after_the_window.failed_sign_in_window_started_at > window_ends_at

    right_sign_in = tablet.sign_in("ivan", DEFAULT_PASSWORD)
    await right_sign_in.wait_until_finished()
    await wait_for_answered_refresh_of_every_conversation(tablet)

    await relay_worker.stop()
    assert right_sign_in.is_signed_in
    tablet_contact = await in_database(read_contact, tablet_device)
    assert tablet_contact.failed_sign_in_count == 0
    assert tablet_contact.failed_sign_in_window_started_at is None
    assert await in_database(read_username_of_device, tablet_device) == "ivan"
    phone_contact = await in_database(read_contact, phone_device)
    assert phone_contact.failed_sign_in_count == 0
    assert await in_database(read_account_request_outcomes_from, tablet_device) == [
        "wrong password (1 in the current window)",
        "wrong password (2 in the current window)",
        "wrong password (3 in the current window)",
        "wrong password (4 in the current window)",
        "wrong password (5 in the current window)",
        "rate limited: too many wrong passwords from this device",
        "rate limited: too many wrong passwords from this device",
        "wrong password (1 in the current window)",
        "signed in; device linked",
    ]
    assert await in_database(read_account_request_outcomes_from, phone_device) == [
        "account created; device linked",
        "signed in; the device was already linked",
    ]
    await in_database(assert_all_invariants)


async def test_switching_accounts_sets_the_old_requests_aside_cancels_the_old_deliveries_and_refreshes_the_new_account(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    zoe_device, yvonne_device, shared_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "zoe-phone", "yvonne-phone", "shared-phone"
    )
    zoe = start_client(zoe_device)
    yvonne = start_client(yvonne_device)
    shared = start_client(shared_device)
    await sign_in(zoe, "zoe")
    await sign_in(yvonne, "yvonne")
    await sign_in(shared, "xavier")

    yvonne_device.switch_off()
    message_for_yvonne = zoe.send_message("yvonne", "Yvonne, this waits for you.")
    message_for_xavier = zoe.send_message("xavier", "Xavier, this never reaches your phone.")
    lose_direct_messages_to(shared_device, text_starts_with(f"HT1 m zoe {message_for_xavier.message_id} "))
    await message_for_yvonne.wait_for_status(OutgoingMessageStatus.SENT)
    await message_for_xavier.wait_for_status(OutgoingMessageStatus.SENT)
    await wait_for_database(
        lambda: has_delivery_sent_at_least_once("zoe", message_for_xavier.message_id, shared_device),
        description="the message for xavier to be sent to the shared phone",
    )

    message_from_xavier = shared.send_message("zoe", "Zoe, did you get this?")
    xavier_message_id = message_from_xavier.message_id
    lose_direct_messages_to(shared_device, text_starts_with(f"HT1 k zoe {xavier_message_id} "))
    lose_direct_messages_to(shared_device, text_starts_with(f"HT1 s zoe {xavier_message_id} "))
    await zoe.wait_for_received_messages("xavier", 1)
    await wait_for_database(
        lambda: has_receipt_sent_at_least_once("xavier", xavier_message_id, shared_device),
        description="the delivered receipt of xavier's message to be sent to the shared phone",
    )
    await shared.wait_until(lambda: message_from_xavier.retry_rounds >= 1, description="xavier's message to be retried")

    failed_switch = shared.sign_in("yvonne", WRONG_PASSWORD)
    await failed_switch.wait_until_finished()
    assert failed_switch.error_code == ErrorCode.WRONG_PASSWORD
    [resumed_after_the_failed_switch] = shared.events_of_kind(ClientEventKind.REQUESTS_RESUMED)
    xavier_part_prefix = f"HT1 M zoe {xavier_message_id} "
    await shared.wait_until(
        lambda: any(
            sent_part.handed_to_node_at > resumed_after_the_failed_switch.at
            for sent_part in find_sent_direct_messages(shared, xavier_part_prefix)
        ),
        description="xavier's message to be sent again after the failed switch",
    )
    parts_sent_as_xavier = len(find_sent_direct_messages(shared, xavier_part_prefix))
    await wait_for_database(
        lambda: count_processed_inbox_rows_from(shared_device, xavier_part_prefix) >= parts_sent_as_xavier,
        description="every copy of xavier's message to be processed as xavier's",
    )

    switch = shared.sign_in("yvonne", DEFAULT_PASSWORD)
    await switch.wait_until_finished()
    refresh = await wait_for_answered_refresh_of_every_conversation(shared)
    [received_for_yvonne] = await shared.wait_for_received_messages("zoe", 1)
    await wait_for_database(
        lambda: is_delivered_by_a_refresh_of_every_peer("zoe", message_for_yvonne.message_id, shared_device),
        description="yvonne's missed message to be delivered to the shared phone by the refresh",
    )

    cancelled_delivery = await in_database(read_delivery, "zoe", message_for_xavier.message_id, shared_device)
    cancelled_receipt = await in_database(read_receipt, "xavier", xavier_message_id, shared_device)
    assert cancelled_delivery.state == MessageDelivery.State.CANCELLED
    assert cancelled_receipt.state == ReceiptNotification.State.CANCELLED
    assert cancelled_delivery.cancelled_at is not None
    assert cancelled_receipt.cancelled_at is not None
    assert cancelled_delivery.next_attempt_at is not None
    assert cancelled_receipt.next_attempt_at is not None
    await wait_until_worker_time_passes(
        relay_worker,
        max(cancelled_delivery.next_attempt_at, cancelled_receipt.next_attempt_at) + SENDER_LOOP_CATCH_UP_TIME,
    )

    await relay_worker.stop()
    assert failed_switch.is_account_switch
    assert switch.is_signed_in
    assert shared.signed_in_username == "yvonne"
    first_switch = shared.events_of_kind(ClientEventKind.ACCOUNT_SWITCH_STARTED)[0]
    account_events_since_the_first_switch = [
        event.kind for event in shared.events if event.at >= first_switch.at and event.kind in ACCOUNT_EVENT_KINDS
    ]
    assert account_events_since_the_first_switch == [
        ClientEventKind.ACCOUNT_SWITCH_STARTED,
        ClientEventKind.REQUESTS_SET_ASIDE,
        ClientEventKind.SIGN_IN_FAILED,
        ClientEventKind.REQUESTS_RESUMED,
        ClientEventKind.ACCOUNT_SWITCH_STARTED,
        ClientEventKind.REQUESTS_SET_ASIDE,
        ClientEventKind.REQUESTS_DISCARDED,
        ClientEventKind.CONVERSATIONS_CLEARED,
        ClientEventKind.SIGNED_IN,
    ]
    first_set_aside, second_set_aside = shared.events_of_kind(ClientEventKind.REQUESTS_SET_ASIDE)
    [failed_switch_request, switch_request] = find_sent_direct_messages(shared, "HT1 A yvonne ")
    assert failed_switch_request.handed_to_node_at >= first_set_aside.at
    assert switch_request.handed_to_node_at >= second_set_aside.at
    parts_sent_while_set_aside = [
        sent_part
        for sent_part in find_sent_direct_messages(shared, xavier_part_prefix)
        if first_set_aside.at <= sent_part.handed_to_node_at <= resumed_after_the_failed_switch.at
        or sent_part.handed_to_node_at >= second_set_aside.at
    ]
    assert parts_sent_while_set_aside == []
    assert message_from_xavier.state is RequestState.DISCARDED
    assert message_from_xavier.status is OutgoingMessageStatus.FAILED
    assert message_from_xavier.failure_reason == FAILURE_REASON_ACCOUNT_SWITCHED

    assert refresh.reported_message_count == 1
    assert received_for_yvonne.message_id == message_for_yvonne.message_id
    assert received_for_yvonne.text == "Yvonne, this waits for you."
    assert await in_database(read_username_of_device, shared_device) == "yvonne"
    assert (await in_database(read_account_request_outcomes_from, shared_device))[-2:] == [
        "wrong password (1 in the current window)",
        "signed in; device relinked from xavier",
    ]
    assert await in_database(read_messages_sent_by, "yvonne") == []
    assert len(await in_database(read_messages_sent_by, "xavier")) == 1
    delivery_packets = await in_database(read_packets_of_delivery, cancelled_delivery.pk)
    receipt_packets = await in_database(read_packets_of_receipt, cancelled_receipt.pk)
    assert delivery_packets, "the message for xavier was never sent before the switch"
    assert receipt_packets, "the receipt of xavier's message was never sent before the switch"
    assert find_packets_prepared_after(delivery_packets, cancelled_delivery.cancelled_at) == []
    assert find_packets_prepared_after(receipt_packets, cancelled_receipt.cancelled_at) == []
    await in_database(assert_all_invariants)


async def test_a_delayed_sign_in_as_the_previous_account_moves_the_device_back_and_its_client_signs_in_again(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    yvonne_device, shared_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "yvonne-phone", "shared-phone"
    )
    yvonne = start_client(yvonne_device)
    await sign_in(yvonne, "yvonne")
    shared = start_client(shared_device)
    await sign_in(shared, "xavier")
    [sign_in_as_xavier] = find_sent_direct_messages(shared, "HT1 A xavier ")
    await sign_in(shared, "yvonne")
    sign_ins_before_the_delayed_retry = len(shared.events_of_kind(ClientEventKind.SIGNED_IN))

    deliver_delayed_retry(shared_device, sign_in_as_xavier.text)
    await shared.wait_until(
        lambda: len(shared.events_of_kind(ClientEventKind.SIGNED_IN)) > sign_ins_before_the_delayed_retry,
        description="the client to sign in again after the unexpected answer",
    )
    refresh = await wait_for_answered_refresh_of_every_conversation(shared)

    await relay_worker.stop()
    [signed_out] = shared.events_of_kind(ClientEventKind.SIGNED_OUT_BY_UNEXPECTED_ACCOUNT_REPLY)
    assert signed_out.detail == "a xavier"
    assert shared.events_of_kind(ClientEventKind.SIGNED_IN)[-1].detail == "yvonne"
    assert shared.signed_in_username == "yvonne"
    assert refresh.created_at > signed_out.at
    sign_ins_after_signing_out = [
        sent_direct_message.text
        for sent_direct_message in find_sent_direct_messages(shared, "HT1 A ")
        if sent_direct_message.handed_to_node_at >= signed_out.at
    ]
    assert set(sign_ins_after_signing_out) == {f"HT1 A yvonne {DEFAULT_PASSWORD}"}
    assert shared.received_texts("a") == ["HT1 a xavier", "HT1 a yvonne", "HT1 a xavier", "HT1 a yvonne"]
    assert await in_database(read_username_of_device, shared_device) == "yvonne"
    assert await in_database(read_account_request_outcomes_from, shared_device) == [
        "account created; device linked",
        "signed in; device relinked from xavier",
        "signed in; device relinked from yvonne",
        "signed in; device relinked from xavier",
    ]
    await in_database(assert_all_invariants)


async def test_relinking_a_device_back_revives_its_cancelled_deliveries_oldest_first_and_its_receipts(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    zoe_device, shared_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "zoe-phone", "shared-phone"
    )
    zoe = start_client(zoe_device)
    shared = start_client(shared_device)
    await sign_in(zoe, "zoe")
    await sign_in(shared, "xavier")
    away_period = LossPeriod()

    message_read_later = shared.send_message("zoe", "Delivered before the relinks, read after them.")
    await message_read_later.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await wait_for_database(
        lambda: has_receipt_in_state(
            "xavier", message_read_later.message_id, shared_device, ReceiptNotification.State.CONFIRMED
        ),
        description="the delivered receipt to be confirmed by the shared phone",
    )

    message_with_lost_receipt = shared.send_message("zoe", "Delivered, but the phone never hears about it.")
    lose_direct_messages_to(
        shared_device,
        text_starts_with(f"HT1 s zoe {message_with_lost_receipt.message_id} "),
        applies=away_period.lasts,
    )
    await zoe.wait_for_received_messages("xavier", 2)
    await wait_for_database(
        lambda: has_receipt_sent_at_least_once("xavier", message_with_lost_receipt.message_id, shared_device),
        description="the lost receipt to be sent at least once",
    )

    first_missed_message = zoe.send_message("xavier", "First message while you were away.")
    lose_direct_messages_to(
        shared_device, text_starts_with(f"HT1 m zoe {first_missed_message.message_id} "), applies=away_period.lasts
    )
    second_missed_message = zoe.send_message("xavier", "Second message while you were away.")
    lose_direct_messages_to(
        shared_device, text_starts_with(f"HT1 m zoe {second_missed_message.message_id} "), applies=away_period.lasts
    )
    for missed_message in (first_missed_message, second_missed_message):
        await missed_message.wait_for_status(OutgoingMessageStatus.SENT)
        await wait_for_database(
            functools.partial(has_delivery_sent_at_least_once, "zoe", missed_message.message_id, shared_device),
            description="the missed message to be sent to the shared phone at least once",
        )

    await sign_in(shared, "yvonne")
    cancelled_deliveries = [
        await in_database(read_delivery, "zoe", missed_message.message_id, shared_device)
        for missed_message in (first_missed_message, second_missed_message)
    ]
    cancelled_receipts = [
        await in_database(read_receipt, "xavier", sent_message.message_id, shared_device)
        for sent_message in (message_read_later, message_with_lost_receipt)
    ]
    assert {delivery.state for delivery in cancelled_deliveries} == {MessageDelivery.State.CANCELLED}
    assert {receipt.state for receipt in cancelled_receipts} == {ReceiptNotification.State.CANCELLED}

    away_period.end()
    await sign_in(shared, "xavier")
    refresh_after_relinking_back = find_refreshes_of_every_conversation(shared)[-1]
    relinked_back_at = await in_database(read_refresh_start, shared_device)
    await shared.wait_for_received_messages("zoe", 2)
    await wait_for_database(
        lambda: (
            {
                read_delivery("zoe", missed_message.message_id, shared_device).state
                for missed_message in (first_missed_message, second_missed_message)
            }
            == {MessageDelivery.State.DELIVERED}
        ),
        description="both missed messages to be delivered to the shared phone",
    )
    conversation_refresh = shared.open_conversation("zoe")
    await conversation_refresh.wait_until_finished()
    await wait_for_database(
        lambda: has_receipt_in_state(
            "xavier", message_with_lost_receipt.message_id, shared_device, ReceiptNotification.State.CONFIRMED
        ),
        description="the revived receipt to be confirmed",
    )
    receipt_after_the_refresh = await in_database(read_receipt, "xavier", message_read_later.message_id, shared_device)
    [message_read_later_at_zoe] = [
        received_message
        for received_message in zoe.received_messages("xavier")
        if received_message.message_id == message_read_later.message_id
    ]
    read_confirmation = zoe.mark_read("xavier", message_read_later_at_zoe.message_id)
    await read_confirmation.wait_until_finished()
    await wait_for_database(
        lambda: has_receipt_in_state(
            "xavier",
            message_read_later.message_id,
            shared_device,
            ReceiptNotification.State.CONFIRMED,
            confirmed_level=ReceiptNotification.ConfirmedLevel.READ,
        ),
        description="the read receipt to be confirmed by the shared phone",
    )

    await relay_worker.stop()
    assert refresh_after_relinking_back.reported_message_count == 2
    displayed_from_zoe = [
        displayed_message.message_id
        for displayed_message in shared.displayed_messages
        if displayed_message.sender_username == "zoe"
    ]
    assert displayed_from_zoe == [first_missed_message.message_id, second_missed_message.message_id]
    revived_deliveries = [
        await in_database(read_delivery, "zoe", missed_message.message_id, shared_device)
        for missed_message in (first_missed_message, second_missed_message)
    ]
    first_revived_delivery, second_revived_delivery = revived_deliveries
    for cancelled_delivery, revived_delivery in zip(cancelled_deliveries, revived_deliveries, strict=True):
        assert revived_delivery.refresh_session_id is not None
        assert revived_delivery.arm_generation > cancelled_delivery.arm_generation
        assert cancelled_delivery.cancelled_at is not None
        packets = await in_database(read_packets_of_delivery, revived_delivery.pk)
        packets_while_cancelled = [
            packet
            for packet in find_packets_prepared_after(packets, cancelled_delivery.cancelled_at)
            if packet.prepared_at is not None and packet.prepared_at < relinked_back_at
        ]
        assert packets_while_cancelled == []
    assert first_revived_delivery.delivered_at is not None
    second_delivery_packets_after_relinking_back = find_packets_prepared_after(
        await in_database(read_packets_of_delivery, second_revived_delivery.pk), relinked_back_at
    )
    assert second_delivery_packets_after_relinking_back
    assert all(
        packet.prepared_at is not None and packet.prepared_at > first_revived_delivery.delivered_at
        for packet in second_delivery_packets_after_relinking_back
    ), "the second missed message was sent before the first one was delivered"

    assert conversation_refresh.state is RequestState.ANSWERED
    assert conversation_refresh.reported_message_count == 0
    assert receipt_after_the_refresh.state == ReceiptNotification.State.CONFIRMED
    assert receipt_after_the_refresh.confirmed_level == 1
    read_later_receipt = await in_database(read_receipt, "xavier", message_read_later.message_id, shared_device)
    read_later_receipt_packets = await in_database(read_packets_of_receipt, read_later_receipt.pk)
    assert [packet.text for packet in find_packets_prepared_after(read_later_receipt_packets, relinked_back_at)] == [
        f"HT1 s zoe {message_read_later.message_id} R"
    ]
    assert read_later_receipt.state == ReceiptNotification.State.CONFIRMED
    lost_receipt = await in_database(read_receipt, "xavier", message_with_lost_receipt.message_id, shared_device)
    lost_receipt_packets = await in_database(read_packets_of_receipt, lost_receipt.pk)
    assert find_packets_prepared_after(lost_receipt_packets, relinked_back_at), "the revived receipt was not sent"
    assert lost_receipt.confirmed_level == 1
    assert message_with_lost_receipt.message_id in [
        receipt.message_id for receipt in shared.receipts_for_unknown_messages
    ]
    assert f"HT1 s zoe {message_read_later.message_id} R" in shared.received_texts("s")
    await in_database(assert_all_invariants)
