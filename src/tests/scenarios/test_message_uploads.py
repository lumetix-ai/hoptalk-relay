"""Inbound direct messages end to end: message uploads part by part, and traffic the relay must never answer.

Each scenario runs the real relay worker against the fake node and the simulated mesh. Reference clients,
or the stock MeshCore app typed by hand, sit on the devices; chosen direct messages are lost on the way.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta

import pytest

from messaging.models import InboundDirectMessage, MessageDelivery, OutboundPacket
from protocol.constants import INCOMPLETE_MESSAGE_RETENTION_HOURS, RECEIVED_SET_MISSING
from protocol.formatting import format_message_part_request
from tests.invariants import assert_all_invariants
from tests.scenarios.accounts_and_messages_helpers import (
    CERTAIN_LOSS_PROBABILITY,
    LossPeriod,
    StockMeshCoreApp,
    WallClockBehind,
    count_processed_inbox_rows_from,
    find_sent_direct_messages,
    lose_direct_messages_from,
    lose_direct_messages_to,
    move_worker_clock_to,
    read_contact,
    read_delivery,
    read_inbox_rows_from,
    read_message,
    read_messages_sent_by,
    read_packets_of_delivery,
    read_packets_to,
    read_worker_time,
    start_relay_with_devices,
    text_is,
    text_starts_with,
    until_the_relay_node_took,
    wait_until_the_relay_recorded_everything_from,
)
from tests.scenarios.scenario_settings import (
    SCENARIO_CLIENT_TIMING,
    SCENARIO_RETRY_STRATEGY,
    SCENARIO_WORKER_TIMING,
)
from tests.scenarios.scenario_setup import ClientStarter, sign_in
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.relay_worker.worker_harness import (
    DATABASE_POLL_INTERVAL_SECONDS,
    RelayWorkerHarness,
    in_database,
    wait_for_database,
)
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_records import OutgoingMessageStatus, SimulatedClientStorage
from tests.worker.simulated_hoptalk_client_timing import ClientClock, ScaledClock

pytestmark = pytest.mark.django_db(transaction=True)

# 250 ASCII characters: three parts of 104, 104 and 42 bytes.
THREE_PART_TEXT = (
    "The first part of this message is about the weather, which has been grey and windy all week long here. "
    "The second part says that the ferry runs again from Monday, so the trip to the island is still on for us. "
    "The third part: see you soon, love Alice."
)
STOCK_APP_PASSWORD = "hunter2222"
# How the relay stores the password of a sign-in request.
REDACTED_PASSWORD = "********"
INCOMPLETE_STATUS_COALESCING = timedelta(seconds=SCENARIO_WORKER_TIMING.incomplete_status_coalescing_seconds)
DELIVERED_RECEIPT_HOLD_BACK = timedelta(seconds=SCENARIO_RETRY_STRATEGY.delivered_receipt_delay_seconds)
FIRST_RETRY_PAUSE = timedelta(seconds=SCENARIO_RETRY_STRATEGY.initial_pause_seconds)
# How far past a round's due time the worker's clock is moved: far below any pause of the retry strategy.
WORKER_CLOCK_STEP = timedelta(milliseconds=10)
SLOW_UPLOAD_TIMEOUT_SECONDS = 10.0
ONE_HOUR_IN_SECONDS = 3600.0

# The start_client_with_storage_and_clock fixture: starts a reference client with the given storage and clock.
type ClientWithStorageAndClockStarter = Callable[
    [SimulatedDevice, SimulatedClientStorage, ClientClock], SimulatedHopTalkClient
]


@pytest.fixture
async def start_client_with_storage_and_clock() -> AsyncIterator[ClientWithStorageAndClockStarter]:
    """Like start_client, for a client whose storage and clock the scenario sets up itself."""
    started_clients: list[SimulatedHopTalkClient] = []

    def start_client_on(
        device: SimulatedDevice, storage: SimulatedClientStorage, clock: ClientClock
    ) -> SimulatedHopTalkClient:
        client = SimulatedHopTalkClient(device, storage=storage, timing=SCENARIO_CLIENT_TIMING, clock=clock)
        client.start()
        started_clients.append(client)
        return client

    yield start_client_on
    for client in started_clients:
        await client.stop()
    client_errors = [f"{client!r}: {client.internal_errors!r}" for client in started_clients if client.internal_errors]
    assert client_errors == [], "a simulated client raised internally"


def read_part_rows(device: SimulatedDevice, part_prefix: str) -> list[InboundDirectMessage]:
    return [inbox_row for inbox_row in read_inbox_rows_from(device) if inbox_row.text.startswith(part_prefix)]


def read_packet_texts_to(device: SimulatedDevice, text_prefix: str = "") -> list[str]:
    return [packet.text for packet in read_packets_to(device) if packet.text.startswith(text_prefix)]


def read_part_numbers(inbox_rows: list[InboundDirectMessage], part_prefix: str) -> list[int]:
    """The part number of each "M" row, whose text is "<part_prefix><n>/<c> <text>"."""
    return [int(inbox_row.text.removeprefix(part_prefix).split("/")[0]) for inbox_row in inbox_rows]


def holds_parts(sender_username: str, client_message_id: int, expected_part_texts: list[str | None]) -> bool:
    message = read_message(sender_username, client_message_id)
    return message is not None and message.part_texts == expected_part_texts


def is_delivered(sender_username: str, client_message_id: int, device: SimulatedDevice) -> bool:
    return read_delivery(sender_username, client_message_id, device).state == MessageDelivery.State.DELIVERED


def find_request_answered_by(inbox_rows: list[InboundDirectMessage], packet: OutboundPacket) -> InboundDirectMessage:
    """The latest request, processed before the packet was prepared, whose answer is the packet's text."""
    assert packet.prepared_at is not None
    answered_rows = [
        inbox_row
        for inbox_row in inbox_rows
        if inbox_row.reply_summary == packet.text
        and inbox_row.processed_at is not None
        and inbox_row.processed_at <= packet.prepared_at
    ]
    assert answered_rows, f"No request was answered with {packet.text!r}."
    return answered_rows[-1]


def is_incomplete_status(status_text: str) -> bool:
    received_set = status_text.rsplit(" ", 1)[-1]
    return RECEIVED_SET_MISSING in received_set


def assert_incomplete_statuses_waited_for_more_parts(
    part_rows: list[InboundDirectMessage], status_packets: list[OutboundPacket]
) -> None:
    """A status with zeros goes out only once the coalescing pause passed after the part it answers."""
    for status_packet in status_packets:
        if not is_incomplete_status(status_packet.text):
            continue
        answered_row = find_request_answered_by(part_rows, status_packet)
        assert status_packet.prepared_at is not None
        assert answered_row.processed_at is not None
        waited = status_packet.prepared_at - answered_row.processed_at
        assert waited >= INCOMPLETE_STATUS_COALESCING, f"{status_packet.text!r} went out after only {waited}."


async def fast_forward_until_the_delivery_fails(
    relay_worker: RelayWorkerHarness, sender_username: str, client_message_id: int, device: SimulatedDevice
) -> MessageDelivery:
    """Move the worker's clock over every pause between the delivery's rounds until its attempts run out."""
    deadline = time.monotonic() + SLOW_UPLOAD_TIMEOUT_SECONDS
    while True:
        delivery = await in_database(read_delivery, sender_username, client_message_id, device)
        if delivery.state == MessageDelivery.State.FAILED:
            return delivery
        assert time.monotonic() < deadline, f"The delivery never failed; it is {delivery.state}."
        waits_for_its_next_round = (
            delivery.state == MessageDelivery.State.PENDING
            and delivery.round_pending_parts_mask == 0
            and delivery.next_attempt_at is not None
            and delivery.next_attempt_at > read_worker_time(relay_worker)
        )
        if waits_for_its_next_round and delivery.next_attempt_at is not None:
            move_worker_clock_to(relay_worker, delivery.next_attempt_at + WORKER_CLOCK_STEP)
        await asyncio.sleep(DATABASE_POLL_INTERVAL_SECONDS)


async def test_a_single_part_message_whose_status_was_lost_is_stored_once_and_its_status_is_sent_again(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    alice_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = start_client(alice_device)
    bob = start_client(bob_device)
    await sign_in(alice, "alice")
    await sign_in(bob, "bob")
    # Bob's app reads nothing for now, so no receipt can complete Alice's upload before her retry.
    bob_device.phone_leaves()

    message = alice.send_message("bob", "Hello Bob, this message is stored once.")
    part_prefix = f"HT1 M bob {message.message_id} 1/1 "
    status_text = f"HT1 k bob {message.message_id} 1"
    lost_statuses = lose_direct_messages_to(
        alice_device,
        text_is(status_text),
        applies=until_the_relay_node_took(simulated_mesh, alice_device, part_prefix, count=2),
    )
    await message.wait_for_status(OutgoingMessageStatus.SENT)
    bob_device.phone_returns()
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    await relay_worker.stop()
    assert lost_statuses.lost_texts, "the send status was never lost"
    assert message.retry_rounds >= 1
    assert status_text in alice.received_texts("k")
    part_rows = await in_database(read_part_rows, alice_device, part_prefix)
    assert len(part_rows) >= 2
    assert part_rows[0].outcome_summary == "part 1/1 stored; message accepted with 1 deliveries"
    assert {part_row.outcome_summary for part_row in part_rows[1:]} == {"part 1/1 already held"}
    assert {part_row.reply_summary for part_row in part_rows} == {status_text}
    assert (await in_database(read_packet_texts_to, alice_device, "HT1 k ")).count(status_text) >= 2
    [stored_message] = await in_database(read_messages_sent_by, "alice")
    assert stored_message.client_message_id == message.message_id
    assert stored_message.text == "Hello Bob, this message is stored once."
    assert received_message.text == "Hello Bob, this message is stored once."
    assert [displayed.message_id for displayed in bob.displayed_messages] == [message.message_id]
    await in_database(assert_all_invariants)


async def test_a_part_lost_on_the_way_is_reported_missing_and_sent_again_at_once(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    alice_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = start_client(alice_device)
    bob = start_client(bob_device)
    await sign_in(alice, "alice")
    await sign_in(bob, "bob")

    message = alice.send_message("bob", THREE_PART_TEXT)
    part_prefix = f"HT1 M bob {message.message_id} "
    status_prefix = f"HT1 k bob {message.message_id} "
    lost_parts = lose_direct_messages_from(alice_device, text_starts_with(f"{part_prefix}2/3 "), maximum_losses=1)
    # With scaled timing a status for part 1 alone may go out before part 3 arrives; losing it keeps
    # the exchange of the protocol's chart, and the coalescing is checked on the relay's side below.
    lose_direct_messages_to(alice_device, text_is(f"{status_prefix}100"))
    await message.wait_for_status(OutgoingMessageStatus.SENT)
    [received_message] = await bob.wait_for_received_messages("alice", 1)

    await relay_worker.stop()
    assert lost_parts.lost_texts == [f"{part_prefix}2/3 {message.parts[1]}"]
    assert [sent_part.text for sent_part in find_sent_direct_messages(alice, part_prefix)] == [
        f"{part_prefix}1/3 {message.parts[0]}",
        f"{part_prefix}2/3 {message.parts[1]}",
        f"{part_prefix}3/3 {message.parts[2]}",
        f"{part_prefix}2/3 {message.parts[1]}",
    ]
    assert message.retry_rounds == 0
    assert alice.counters.missing_part_resends == 1
    received_statuses = [text for text in alice.received_texts("k") if text.startswith(status_prefix)]
    assert received_statuses[-2:] == [f"{status_prefix}101", f"{status_prefix}111"]

    part_rows = await in_database(read_part_rows, alice_device, part_prefix)
    assert [part_row.outcome_summary for part_row in part_rows] == [
        "part 1/3 stored",
        "part 3/3 stored",
        "part 2/3 stored; message accepted with 1 deliveries",
    ]
    status_packets = [
        packet for packet in await in_database(read_packets_to, alice_device) if packet.text.startswith(status_prefix)
    ]
    assert f"{status_prefix}101" in [packet.text for packet in status_packets]
    assert_incomplete_statuses_waited_for_more_parts(part_rows, status_packets)
    stored_message = await in_database(read_message, "alice", message.message_id)
    assert stored_message is not None
    assert stored_message.text == THREE_PART_TEXT
    assert received_message.text == THREE_PART_TEXT
    await in_database(assert_all_invariants)


async def test_parts_arriving_reversed_and_repeated_are_assembled_once_and_another_device_only_repeats_held_parts(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    phone_device, tablet_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "alice-tablet", "bob-phone"
    )
    phone = start_client(phone_device)
    tablet = start_client(tablet_device)
    bob = start_client(bob_device)
    await sign_in(phone, "alice")
    await sign_in(tablet, "alice")
    await sign_in(bob, "bob")
    tablet_period = LossPeriod()

    message = phone.send_message("bob", THREE_PART_TEXT)
    message_id = message.message_id
    part_prefix = f"HT1 M bob {message_id} "
    # Part 1 waits until the tablet is done, part 2 is lost once, and the status that would confirm
    # parts 2 and 3 is lost too, so the phone's retry rounds repeat part 2.
    lose_direct_messages_from(phone_device, text_starts_with(f"{part_prefix}1/3 "), applies=tablet_period.lasts)
    lose_direct_messages_from(phone_device, text_starts_with(f"{part_prefix}2/3 "), maximum_losses=1)
    lose_direct_messages_to(phone_device, text_is(f"HT1 k bob {message_id} 011"), applies=tablet_period.lasts)
    await wait_for_database(
        lambda: holds_parts("alice", message_id, [None, message.parts[1], message.parts[2]]),
        description="the relay to hold parts 2 and 3",
    )
    await wait_for_database(
        lambda: count_processed_inbox_rows_from(phone_device, f"{part_prefix}2/3 ") >= 2,
        description="the phone to repeat part 2",
    )

    tablet.send_raw_direct_message(format_message_part_request("bob", message_id, 3, 3, message.parts[2]))
    tablet.send_raw_direct_message(format_message_part_request("bob", message_id, 2, 3, message.parts[1]))
    await wait_for_database(
        lambda: count_processed_inbox_rows_from(tablet_device, part_prefix) >= 2,
        description="the tablet's repeats to be processed",
    )
    await tablet.wait_until(
        lambda: f"HT1 k bob {message_id} 011" in tablet.received_texts("k"),
        description="the tablet to get the relay's status",
    )
    tablet.send_raw_direct_message(format_message_part_request("bob", message_id, 1, 3, message.parts[0]))
    await tablet.wait_until(
        lambda: f"HT1 e ID_CONFLICT M bob {message_id}" in tablet.received_texts("e"),
        description="the tablet's new part to be refused",
    )
    tablet_period.end()
    await message.wait_for_status(OutgoingMessageStatus.SENT, timeout_seconds=SLOW_UPLOAD_TIMEOUT_SECONDS)
    [received_message] = await bob.wait_for_received_messages("alice", 1)

    await relay_worker.stop()
    phone_rows = await in_database(read_part_rows, phone_device, part_prefix)
    phone_part_numbers = read_part_numbers(phone_rows, part_prefix)
    assert list(dict.fromkeys(phone_part_numbers)) == [3, 2, 1], "the parts did not arrive in reverse order"
    assert phone_part_numbers.count(2) >= 2
    assert phone_rows[0].outcome_summary == "part 3/3 stored"
    assert phone_rows[1].outcome_summary == "part 2/3 stored"
    completing_row = phone_rows[phone_part_numbers.index(1)]
    assert completing_row.outcome_summary == "part 1/3 stored; message accepted with 1 deliveries"
    assert completing_row.reply_summary == f"HT1 k bob {message_id} 111"
    repeated_rows = [
        phone_row for row_index, phone_row in enumerate(phone_rows) if row_index > 1 and phone_row is not completing_row
    ]
    assert {repeated_row.outcome_summary for repeated_row in repeated_rows} <= {
        "part 1/3 already held",
        "part 2/3 already held",
    }

    tablet_rows = await in_database(read_part_rows, tablet_device, part_prefix)
    assert [tablet_row.outcome_summary for tablet_row in tablet_rows] == [
        "part 3/3 already held",
        "part 2/3 already held",
        "only the device that sent the first part may add parts to an incomplete message",
    ]
    assert tablet_rows[-1].reply_summary == f"HT1 e ID_CONFLICT M bob {message_id}"

    [stored_message] = await in_database(read_messages_sent_by, "alice")
    phone_contact = await in_database(read_contact, phone_device)
    assert stored_message.client_message_id == message_id
    assert stored_message.sender_device_id == phone_contact.pk
    assert stored_message.text == THREE_PART_TEXT
    assert received_message.text == THREE_PART_TEXT
    assert [displayed.message_id for displayed in bob.displayed_messages] == [message_id]
    await in_database(assert_all_invariants)


async def test_a_message_id_already_used_with_another_text_is_refused_and_the_client_sends_it_under_a_new_id(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    start_client_with_storage_and_clock: ClientWithStorageAndClockStarter,
    client_clock: ScaledClock,
) -> None:
    phone_device, tablet_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "alice-tablet", "bob-phone"
    )
    phone = start_client(phone_device)
    bob = start_client(bob_device)
    await sign_in(phone, "alice")
    await sign_in(bob, "bob")
    first_message = phone.send_message("bob", "Written on the phone.")
    await first_message.wait_for_status(OutgoingMessageStatus.SENT)
    reused_message_id = first_message.message_id

    # The tablet's app was restored from a backup taken just before the phone sent that message,
    # and the tablet's clock is behind: the next id it picks is the one the phone already used.
    restored_storage = SimulatedClientStorage(last_message_id=reused_message_id - 1)
    tablet = start_client_with_storage_and_clock(
        tablet_device, restored_storage, WallClockBehind(client_clock, seconds_behind=ONE_HOUR_IN_SECONDS)
    )
    await sign_in(tablet, "alice")
    second_message = tablet.send_message("bob", "Written on the tablet.")
    assert second_message.message_id == reused_message_id
    await second_message.wait_for_status(OutgoingMessageStatus.SENT)
    received_messages = await bob.wait_for_received_messages("alice", 2)

    await relay_worker.stop()
    assert second_message.replaced_message_ids == [reused_message_id]
    assert second_message.message_id == reused_message_id + 1
    assert tablet.counters.id_conflict_resends == 1
    assert f"HT1 e ID_CONFLICT M bob {reused_message_id}" in tablet.received_texts("e")
    assert [(received.message_id, received.text) for received in received_messages] == [
        (reused_message_id, "Written on the phone."),
        (reused_message_id + 1, "Written on the tablet."),
    ]
    tablet_rows = await in_database(read_part_rows, tablet_device, f"HT1 M bob {reused_message_id} ")
    assert [tablet_row.outcome_summary for tablet_row in tablet_rows] == ["part 1 is held with another text"]
    stored_messages = await in_database(read_messages_sent_by, "alice")
    phone_contact = await in_database(read_contact, phone_device)
    tablet_contact = await in_database(read_contact, tablet_device)
    assert [(stored.client_message_id, stored.text, stored.sender_device_id) for stored in stored_messages] == [
        (reused_message_id, "Written on the phone.", phone_contact.pk),
        (reused_message_id + 1, "Written on the tablet.", tablet_contact.pk),
    ]
    await in_database(assert_all_invariants)


async def test_a_device_that_lost_the_parts_it_had_reported_gets_them_again_and_only_its_complete_status_delivers(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    alice_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = start_client(alice_device)
    bob = start_client(bob_device)
    await sign_in(alice, "alice")
    await sign_in(bob, "bob")
    before_the_reinstall = LossPeriod()

    message = alice.send_message("bob", THREE_PART_TEXT)
    message_id = message.message_id
    lose_direct_messages_to(
        bob_device, text_starts_with(f"HT1 m alice {message_id} 2/3 "), applies=before_the_reinstall.lasts
    )
    await message.wait_for_status(OutgoingMessageStatus.SENT)
    await wait_for_database(
        lambda: read_delivery("alice", message_id, bob_device).parts_received_mask == 0b101,
        description="bob's phone to report parts 1 and 3",
    )
    failed_delivery = await fast_forward_until_the_delivery_fails(relay_worker, "alice", message_id, bob_device)

    # Bob reinstalls the app: its storage, with parts 1 and 3, is gone.
    await bob.stop()
    before_the_reinstall.end()
    reinstalled_bob = start_client(bob_device)
    await sign_in(reinstalled_bob, "bob")
    [received_message] = await reinstalled_bob.wait_for_received_messages("alice", 1)
    await wait_for_database(
        lambda: is_delivered("alice", message_id, bob_device), description="the message to be delivered to bob"
    )

    await relay_worker.stop()
    assert failed_delivery.failure_reason == MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED
    assert failed_delivery.attempt_count == SCENARIO_RETRY_STRATEGY.maximum_attempts
    assert failed_delivery.parts_received_mask == 0b101
    assert bob.received_messages("alice") == []
    assert received_message.text == THREE_PART_TEXT

    delivery = await in_database(read_delivery, "alice", message_id, bob_device)
    stored_message = await in_database(read_message, "alice", message_id)
    assert stored_message is not None
    acknowledgement_prefix = f"HT1 K alice {message_id} "
    bob_rows = await in_database(read_inbox_rows_from, bob_device)
    refresh_row = [bob_row for bob_row in bob_rows if bob_row.text == "HT1 F *"][-1]
    acknowledgements_after_the_reinstall = [
        bob_row
        for bob_row in bob_rows
        if bob_row.pk > refresh_row.pk and bob_row.text.startswith(acknowledgement_prefix)
    ]
    received_sets = [
        bob_row.text.removeprefix(acknowledgement_prefix) for bob_row in acknowledgements_after_the_reinstall
    ]
    assert received_sets[0] == "010"
    assert "111" in received_sets
    first_complete_acknowledgement = acknowledgements_after_the_reinstall[received_sets.index("111")]
    assert delivery.delivered_at == first_complete_acknowledgement.processed_at
    assert stored_message.delivered_at == delivery.delivered_at

    refresh_row_processed_at = refresh_row.processed_at
    assert refresh_row_processed_at is not None
    packets_after_the_reinstall = [
        packet
        for packet in await in_database(read_packets_of_delivery, delivery.pk)
        if packet.prepared_at is not None and packet.prepared_at > refresh_row_processed_at
    ]
    missing_parts_acknowledgement = acknowledgements_after_the_reinstall[0]
    assert missing_parts_acknowledgement.processed_at is not None
    parts_before_the_status = [
        packet
        for packet in packets_after_the_reinstall
        if packet.prepared_at is not None and packet.prepared_at < missing_parts_acknowledgement.processed_at
    ]
    parts_after_the_status = [
        packet
        for packet in packets_after_the_reinstall
        if packet.prepared_at is not None and packet.prepared_at > missing_parts_acknowledgement.processed_at
    ]
    assert [packet.part_number for packet in parts_before_the_status] == [2]
    assert [packet.part_number for packet in parts_after_the_status][:2] == [1, 3]
    first_round_part = parts_before_the_status[0]
    first_resent_part = parts_after_the_status[0]
    assert first_round_part.queued_at is not None
    assert first_resent_part.prepared_at is not None
    assert first_resent_part.prepared_at < first_round_part.queued_at + FIRST_RETRY_PAUSE, (
        "the missing parts waited for the next round of the retry strategy instead of going out soon"
    )
    await in_database(assert_all_invariants)


async def test_an_upload_that_expired_on_the_server_is_completed_by_sending_the_parts_the_server_reports_missing(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    alice_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = start_client(alice_device)
    bob = start_client(bob_device)
    await sign_in(alice, "alice")
    await sign_in(bob, "bob")
    before_the_expiry = LossPeriod()

    message = alice.send_message("bob", THREE_PART_TEXT)
    message_id = message.message_id
    part_prefix = f"HT1 M bob {message_id} "
    status_prefix = f"HT1 k bob {message_id} "
    lose_direct_messages_from(alice_device, text_starts_with(f"{part_prefix}2/3 "), applies=before_the_expiry.lasts)
    await alice.wait_until(lambda: message.confirmed_set == "101", description="the relay to report parts 1 and 3")
    await wait_for_database(
        lambda: holds_parts("alice", message_id, [message.parts[0], None, message.parts[2]]),
        description="the relay to hold parts 1 and 3",
    )
    incomplete_message = await in_database(read_message, "alice", message_id)
    assert incomplete_message is not None
    move_worker_clock_to(
        relay_worker,
        incomplete_message.last_part_at + timedelta(hours=INCOMPLETE_MESSAGE_RETENTION_HOURS, minutes=1),
    )
    await wait_for_database(
        lambda: read_message("alice", message_id) is None, description="the incomplete upload to expire"
    )
    expired_at = read_worker_time(relay_worker)
    before_the_expiry.end()
    await message.wait_for_status(OutgoingMessageStatus.SENT, timeout_seconds=SLOW_UPLOAD_TIMEOUT_SECONDS)
    [received_message] = await bob.wait_for_received_messages("alice", 1)

    await relay_worker.stop()
    statuses = [
        (received.received_at, received.text)
        for received in alice.received_direct_messages
        if received.text.startswith(status_prefix)
    ]
    status_texts = [text for _, text in statuses]
    assert f"{status_prefix}101" in status_texts
    missing_parts_status_index = status_texts.index(f"{status_prefix}010")
    assert f"{status_prefix}101" not in status_texts[missing_parts_status_index:]
    assert status_texts[-1] == f"{status_prefix}111"
    missing_parts_status_received_at = statuses[missing_parts_status_index][0]
    parts_sent_after_the_status = [
        sent_part.text.removeprefix(part_prefix).split(" ")[0]
        for sent_part in find_sent_direct_messages(alice, part_prefix)
        if sent_part.handed_to_node_at >= missing_parts_status_received_at
    ]
    assert parts_sent_after_the_status[:2] == ["1/3", "3/3"]
    assert set(parts_sent_after_the_status) == {"1/3", "3/3"}

    rows_after_the_expiry = [
        part_row
        for part_row in await in_database(read_part_rows, alice_device, part_prefix)
        if part_row.received_at > expired_at
    ]
    assert rows_after_the_expiry[0].text.startswith(f"{part_prefix}2/3 ")
    assert rows_after_the_expiry[0].outcome_summary == "part 2/3 stored"
    assert rows_after_the_expiry[0].reply_summary == f"{status_prefix}010"
    stored_message = await in_database(read_message, "alice", message_id)
    assert stored_message is not None
    assert stored_message.created_at > expired_at
    assert stored_message.text == THREE_PART_TEXT
    assert received_message.text == THREE_PART_TEXT
    assert [displayed.message_id for displayed in bob.displayed_messages] == [message_id]
    await in_database(assert_all_invariants)


IGNORED_TEXTS = (
    "Hello, is anyone there?",
    "ht1 Q bob",
    "HT1 k bob 1790294400123456 1",
    "HT1 m bob 1790294400123456 1/1 hi",
    "HT1 z anything at all",
    "HT1 e WRONG_PASSWORD A alice",
    "HT1 e SYNTAX ?",
    "HT2 e VERSION ? 1",
)
IGNORED_TEXT_CLASSIFICATIONS = [
    InboundDirectMessage.Classification.NOT_PROTOCOL,
    InboundDirectMessage.Classification.NOT_PROTOCOL,
    InboundDirectMessage.Classification.SERVER_TYPE_IGNORED,
    InboundDirectMessage.Classification.SERVER_TYPE_IGNORED,
    InboundDirectMessage.Classification.SERVER_TYPE_IGNORED,
    InboundDirectMessage.Classification.SERVER_TYPE_IGNORED,
    InboundDirectMessage.Classification.SERVER_TYPE_IGNORED,
    InboundDirectMessage.Classification.UNSUPPORTED_VERSION,
]


async def test_other_text_server_types_errors_of_another_relay_and_firmware_repeats_are_never_answered(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    alice_device, bob_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice_app = StockMeshCoreApp(alice_device)
    bob_app = StockMeshCoreApp(bob_device)

    sign_in_line = alice_app.type_line(f"HT1 A alice {STOCK_APP_PASSWORD}")
    await alice_app.wait_for_line("HT1 a alice")
    # Asked before bob exists: were its repeat answered later, the answer would be a new text, "q bob 1".
    query_line = alice_app.type_line("HT1 Q bob")
    await alice_app.wait_for_line("HT1 q bob 0")
    bob_app.type_line(f"HT1 A bob {STOCK_APP_PASSWORD}")
    await bob_app.wait_for_line("HT1 a bob")

    for ignored_text in IGNORED_TEXTS:
        alice_app.type_line(ignored_text)
    alice_app.repeat_at_firmware_level(sign_in_line)
    alice_app.repeat_at_firmware_level(query_line)
    # Answers go out in the order their requests arrived, so every answer to the lines above comes before this one.
    alice_app.type_line("HT1 Q carol")
    await alice_app.wait_for_line("HT1 q carol 0")

    await relay_worker.stop()
    expected_answers = ["HT1 a alice", "HT1 q bob 0", "HT1 q carol 0"]
    assert alice_app.received_texts == expected_answers
    assert await in_database(read_packet_texts_to, alice_device) == expected_answers
    alice_rows = await in_database(read_inbox_rows_from, alice_device)
    assert [(alice_row.text, alice_row.duplicate_count) for alice_row in alice_rows] == [
        (f"HT1 A alice {REDACTED_PASSWORD}", 1),
        ("HT1 Q bob", 1),
        *[(ignored_text, 0) for ignored_text in IGNORED_TEXTS],
        ("HT1 Q carol", 0),
    ]
    ignored_rows = alice_rows[2:-1]
    assert [ignored_row.classification for ignored_row in ignored_rows] == IGNORED_TEXT_CLASSIFICATIONS
    assert {ignored_row.reply_summary for ignored_row in ignored_rows} == {""}
    assert {alice_row.processing_state for alice_row in alice_rows} == {InboundDirectMessage.ProcessingState.PROCESSED}
    await in_database(assert_all_invariants)


def read_sent_at(packets: list[OutboundPacket], text: str) -> datetime:
    [packet] = [packet for packet in packets if packet.text == text]
    assert packet.prepared_at is not None
    return packet.prepared_at


def read_processed_at(inbox_rows: list[InboundDirectMessage], text: str) -> datetime:
    [inbox_row] = [inbox_row for inbox_row in inbox_rows if inbox_row.text == text]
    assert inbox_row.processed_at is not None
    return inbox_row.processed_at


async def test_the_hand_testing_session_typed_in_the_stock_app_gets_every_expected_answer_and_its_repeats_are_ignored(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    phone_a_device, phone_b_device = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "phone-a", "phone-b"
    )
    # The firmware ACKs never reach the phones, so the stock app repeats every line it sends once.
    for phone_device in (phone_a_device, phone_b_device):
        phone_device.downlink.acknowledgement_loss_probability = CERTAIN_LOSS_PROBABILITY
    phone_a = StockMeshCoreApp(phone_a_device, resends_without_acknowledgement=1)
    phone_b = StockMeshCoreApp(phone_b_device, resends_without_acknowledgement=1)

    await phone_a.type_line_and_resend_while_unacknowledged(f"HT1 A alice {STOCK_APP_PASSWORD}")
    await phone_a.wait_for_line("HT1 a alice")
    await phone_b.type_line_and_resend_while_unacknowledged(f"HT1 A bob {STOCK_APP_PASSWORD}")
    await phone_b.wait_for_line("HT1 a bob")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 F *")
    await phone_a.wait_for_line("HT1 f * 0")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 Q bob")
    await phone_a.wait_for_line("HT1 q bob 1")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 M bob 1 1/1 Hello Bob")
    await phone_a.wait_for_line("HT1 k bob 1 1")
    # Until phone B answers, the relay sends the part again.
    await phone_b.wait_for_line("HT1 m alice 1 1/1 Hello Bob", copies=2)
    await phone_b.type_line_and_resend_while_unacknowledged("HT1 K alice 1 1")
    await phone_a.wait_for_line("HT1 s bob 1 D")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 C bob 1 D")
    await phone_b.type_line_and_resend_while_unacknowledged("HT1 R alice 1")
    await phone_b.wait_for_line("HT1 r alice 1")
    await phone_a.wait_for_line("HT1 s bob 1 R")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 C bob 1 R")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 M bob 2 1/2 Hello ")
    await phone_a.wait_for_line("HT1 k bob 2 10")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 M bob 2 2/2 again")
    await phone_a.wait_for_line("HT1 k bob 2 11")
    await phone_b.wait_for_line("HT1 m alice 2 1/2 Hello ")
    await phone_b.wait_for_line("HT1 m alice 2 2/2 again")
    await phone_b.type_line_and_resend_while_unacknowledged("HT1 K alice 2 11")
    await phone_a.wait_for_line("HT1 s bob 2 D")
    await phone_a.type_line_and_resend_while_unacknowledged("HT1 C bob 2 D")
    await wait_for_database(
        lambda: is_delivered("alice", 2, phone_b_device), description="the second message to be delivered"
    )
    await phone_b.type_line_and_resend_while_unacknowledged("HT1 F alice")
    await phone_b.wait_for_line("HT1 f alice 0")
    for phone_device in (phone_a_device, phone_b_device):
        await wait_until_the_relay_recorded_everything_from(simulated_mesh, phone_device)

    await relay_worker.stop()
    expected_answers_to_phone_a = [
        "HT1 a alice",
        "HT1 f * 0",
        "HT1 q bob 1",
        "HT1 k bob 1 1",
        "HT1 s bob 1 D",
        "HT1 s bob 1 R",
        "HT1 k bob 2 10",
        "HT1 k bob 2 11",
        "HT1 s bob 2 D",
    ]
    expected_answers_to_phone_b = [
        "HT1 a bob",
        "HT1 m alice 1 1/1 Hello Bob",
        "HT1 m alice 1 1/1 Hello Bob",
        "HT1 r alice 1",
        "HT1 m alice 2 1/2 Hello ",
        "HT1 m alice 2 2/2 again",
        "HT1 f alice 0",
    ]
    assert phone_a.received_texts == expected_answers_to_phone_a
    assert phone_b.received_texts == expected_answers_to_phone_b
    assert await in_database(read_packet_texts_to, phone_a_device) == expected_answers_to_phone_a
    assert await in_database(read_packet_texts_to, phone_b_device) == expected_answers_to_phone_b

    for phone in (phone_a, phone_b):
        typed_texts = list(dict.fromkeys(typed_line.text for typed_line in phone.typed_lines))
        assert [typed_line.attempt for typed_line in phone.typed_lines] == [0, 1] * len(typed_texts)
        phone_rows = await in_database(read_inbox_rows_from, phone.device)
        assert [phone_row.text for phone_row in phone_rows] == [
            typed_text.replace(STOCK_APP_PASSWORD, REDACTED_PASSWORD) for typed_text in typed_texts
        ]
        assert {phone_row.duplicate_count for phone_row in phone_rows} == {1}
        assert {phone_row.processing_state for phone_row in phone_rows} == {
            InboundDirectMessage.ProcessingState.PROCESSED
        }

    phone_a_rows = await in_database(read_inbox_rows_from, phone_a_device)
    phone_a_packets = await in_database(read_packets_to, phone_a_device)
    first_part_at = read_processed_at(phone_a_rows, "HT1 M bob 2 1/2 Hello ")
    second_part_at = read_processed_at(phone_a_rows, "HT1 M bob 2 2/2 again")
    assert read_sent_at(phone_a_packets, "HT1 k bob 2 10") - first_part_at >= INCOMPLETE_STATUS_COALESCING
    assert read_sent_at(phone_a_packets, "HT1 k bob 2 11") - second_part_at < INCOMPLETE_STATUS_COALESCING
    first_message = await in_database(read_message, "alice", 1)
    assert first_message is not None
    assert first_message.delivered_at is not None
    assert read_sent_at(phone_a_packets, "HT1 s bob 1 D") - first_message.delivered_at >= DELIVERED_RECEIPT_HOLD_BACK
    first_copy, second_copy = [
        packet
        for packet in await in_database(read_packets_to, phone_b_device)
        if packet.text == "HT1 m alice 1 1/1 Hello Bob"
    ]
    assert first_copy.queued_at is not None
    assert second_copy.prepared_at is not None
    assert second_copy.prepared_at - first_copy.queued_at >= FIRST_RETRY_PAUSE
    await in_database(assert_all_invariants)
