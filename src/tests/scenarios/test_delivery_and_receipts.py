"""Delivery to every device of the recipient, and the delivered and read receipts to every device of the sender."""

import functools
from datetime import timedelta

import pytest

from messaging.models import MessageDelivery, ReceiptNotification
from protocol.constants import ReceiptLevel
from protocol.message_types import ReceiptPush
from tests.invariants import assert_all_invariants
from tests.scenarios.delivery_receipts_routes_helpers import (
    GIVE_UP_TIMEOUT_SECONDS,
    ROUND_START_TOLERANCE,
    WORKER_REACTION_ALLOWANCE,
    DirectMessageLossPolicy,
    calculate_expected_retry_pause,
    describe_round_spacing,
    find_delivery,
    find_receipt,
    has_delivery_completed_round,
    read_contact_id,
    read_delivery,
    read_delivery_packets,
    read_inbox_rows_from,
    read_message,
    read_packets,
    read_receipt,
    read_receipt_packets,
    start_relay_with_devices,
    start_signed_in_client,
    wait_until_relay_is_quiet,
    wait_until_worker_clock_passes,
)
from tests.scenarios.scenario_settings import SCENARIO_ENGINE_TIMING, SCENARIO_RETRY_STRATEGY
from tests.scenarios.scenario_setup import ClientStarter
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database, wait_for_database
from tests.worker.simulated_hoptalk_client_records import OutgoingMessageStatus, RequestState

pytestmark = pytest.mark.django_db(transaction=True)

GREETING_TEXT = "Hi Bob, are you coming to the lake on Saturday?"
# 363 bytes: four parts of at most 104 bytes.
FOUR_PART_TEXT = (
    "Directions to the cabin: take the forest road north past the old sawmill, then turn left at the "
    "second bridge. The gravel track climbs for about three kilometres; keep right at every fork. "
    "Park beside the woodshed, because the last stretch is too steep for cars after rain. The key "
    "is under the blue flower pot by the back door, and the fuse box is in the pantry."
)
DELIVERED_RECEIPT_HOLD_BACK = timedelta(seconds=SCENARIO_RETRY_STRATEGY.delivered_receipt_delay_seconds)
MISSING_PARTS_ROUND_DELAY = timedelta(seconds=SCENARIO_ENGINE_TIMING.missing_parts_round_delay_seconds)
MAXIMUM_ATTEMPTS = SCENARIO_RETRY_STRATEGY.maximum_attempts
DELIVERED_LEVEL = ReceiptNotification.TargetLevel.DELIVERED
READ_LEVEL = ReceiptNotification.TargetLevel.READ


def is_delivery_in_state(message_id: int, device_id: int, state: MessageDelivery.State) -> bool:
    delivery = find_delivery(message_id, device_id)
    return delivery is not None and delivery.state == state


def is_receipt_in_state(
    message_id: int, device_id: int, state: ReceiptNotification.State, confirmed_level: int
) -> bool:
    receipt = find_receipt(message_id, device_id)
    return receipt is not None and receipt.state == state and receipt.confirmed_level == confirmed_level


async def test_a_message_is_delivered_to_the_switched_on_device_and_given_up_per_the_strategy_for_the_switched_off_one(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "alice-tablet", "bob-phone", "bob-tablet"
    )
    alice_phone = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    alice_tablet = await start_signed_in_client(start_client, devices["alice-tablet"], "alice")
    bob_phone = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_tablet = await start_signed_in_client(start_client, devices["bob-tablet"], "bob")
    device_ids = {name: await in_database(read_contact_id, device.public_key) for name, device in devices.items()}
    devices["bob-tablet"].switch_off()

    message = alice_phone.send_message("bob", GREETING_TEXT)
    [received_message] = await bob_phone.wait_for_received_messages("alice", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await alice_tablet.wait_until(
        lambda: bool(alice_tablet.receipts_for_unknown_messages), description="the tablet to get the receipt"
    )
    stored_message = await in_database(read_message, "alice", message.message_id)
    await wait_for_database(
        lambda: is_delivery_in_state(stored_message.pk, device_ids["bob-tablet"], MessageDelivery.State.FAILED),
        timeout_seconds=GIVE_UP_TIMEOUT_SECONDS,
        description="the switched-off tablet to be given up",
    )
    await relay_worker.stop()

    message_id = message.message_id
    stored_message = await in_database(read_message, "alice", message_id)
    phone_delivery = await in_database(read_delivery, stored_message.pk, device_ids["bob-phone"])
    tablet_delivery = await in_database(read_delivery, stored_message.pk, device_ids["bob-tablet"])
    tablet_packets = await in_database(read_delivery_packets, tablet_delivery.pk)
    assert received_message.text == GREETING_TEXT
    assert stored_message.delivered_at is not None
    assert stored_message.read_at is None
    assert phone_delivery.state == MessageDelivery.State.DELIVERED
    assert phone_delivery.attempt_count == 1
    assert bob_tablet.received_messages("alice") == []

    assert tablet_delivery.state == MessageDelivery.State.FAILED
    assert tablet_delivery.failure_reason == MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED
    assert tablet_delivery.attempt_count == MAXIMUM_ATTEMPTS
    assert [packet.attempt_number for packet in tablet_packets] == list(range(1, MAXIMUM_ATTEMPTS + 1))
    assert {packet.text for packet in tablet_packets} == {f"HT1 m alice {message_id} 1/1 {GREETING_TEXT}"}
    assert all(packet.acknowledged_at is None for packet in tablet_packets)
    assert describe_round_spacing(tablet_packets) == []
    last_packet_queued_at = tablet_packets[-1].queued_at
    assert last_packet_queued_at is not None
    assert tablet_delivery.failed_at is not None
    earliest_failure = last_packet_queued_at + calculate_expected_retry_pause(MAXIMUM_ATTEMPTS)
    assert earliest_failure <= tablet_delivery.failed_at <= earliest_failure + ROUND_START_TOLERANCE

    delivered_receipt_text = f"HT1 s bob {message_id} D"
    receipt_start_times = []
    for sender_device_name in ("alice-phone", "alice-tablet"):
        receipt = await in_database(read_receipt, stored_message.pk, device_ids[sender_device_name])
        receipt_packets = await in_database(read_receipt_packets, receipt.pk)
        assert receipt.state == ReceiptNotification.State.CONFIRMED, sender_device_name
        assert (receipt.target_level, receipt.confirmed_level) == (DELIVERED_LEVEL, DELIVERED_LEVEL)
        assert [packet.text for packet in receipt_packets] == [delivered_receipt_text], sender_device_name
        receipt_prepared_at = receipt_packets[0].prepared_at
        assert receipt_prepared_at is not None
        assert receipt_prepared_at - stored_message.delivered_at >= DELIVERED_RECEIPT_HOLD_BACK, sender_device_name
        receipt_start_times.append(receipt_prepared_at)
    first_receipt_delay = min(receipt_start_times) - stored_message.delivered_at
    assert first_receipt_delay <= DELIVERED_RECEIPT_HOLD_BACK + WORKER_REACTION_ALLOWANCE
    assert message.status is OutgoingMessageStatus.DELIVERED
    assert message.received_receipt_levels == [ReceiptLevel.DELIVERED]
    assert alice_phone.sent_texts("C") == [f"HT1 C bob {message_id} D"]
    assert alice_tablet.receipts_for_unknown_messages == [
        ReceiptPush(recipient_username="bob", message_id=message_id, receipt_level=ReceiptLevel.DELIVERED)
    ]
    assert alice_tablet.sent_texts("C") == [f"HT1 C bob {message_id} D"]
    await in_database(assert_all_invariants)


async def test_a_lost_read_and_its_lost_answer_are_retried_and_the_receipt_fails_only_for_the_switched_off_sender(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "alice-tablet", "bob-phone"
    )
    alice_phone = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    alice_tablet = await start_signed_in_client(start_client, devices["alice-tablet"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    device_ids = {name: await in_database(read_contact_id, device.public_key) for name, device in devices.items()}
    message = alice_phone.send_message("bob", GREETING_TEXT)
    message_id = message.message_id
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    stored_message = await in_database(read_message, "alice", message_id)
    for sender_device_name in ("alice-phone", "alice-tablet"):
        await wait_for_database(
            functools.partial(
                is_receipt_in_state,
                stored_message.pk,
                device_ids[sender_device_name],
                ReceiptNotification.State.CONFIRMED,
                DELIVERED_LEVEL,
            ),
            description=f"{sender_device_name} to confirm the delivered receipt",
        )
    devices["alice-tablet"].switch_off()
    read_request_text = f"HT1 R alice {message_id}"
    read_reply_text = f"HT1 r alice {message_id}"
    bob_uplink = DirectMessageLossPolicy(text_prefixes_to_lose=[read_request_text])
    bob_downlink = DirectMessageLossPolicy(text_prefixes_to_lose=[read_reply_text])
    devices["bob-phone"].uplink = bob_uplink
    devices["bob-phone"].downlink = bob_downlink

    read_confirmation = bob.mark_read("alice", received_message.message_id)
    await read_confirmation.wait_until_finished()
    await message.wait_for_status(OutgoingMessageStatus.READ)
    await wait_for_database(
        lambda: is_receipt_in_state(
            stored_message.pk, device_ids["alice-tablet"], ReceiptNotification.State.FAILED, DELIVERED_LEVEL
        ),
        timeout_seconds=GIVE_UP_TIMEOUT_SECONDS,
        description="the read receipt to the switched-off tablet to be given up",
    )
    await relay_worker.stop()

    assert bob_uplink.lost_texts == [read_request_text]
    assert bob_downlink.lost_texts == [read_reply_text]
    assert read_confirmation.state is RequestState.ANSWERED
    assert read_confirmation.retry_rounds >= 1
    assert bob.sent_texts("R").count(read_request_text) >= 2
    reply_packets = await in_database(read_packets, contact_id=device_ids["bob-phone"], text=read_reply_text)
    assert len(reply_packets) >= 2
    read_rows = await in_database(read_inbox_rows_from, device_ids["bob-phone"], read_request_text)
    assert read_rows, "no read request reached the relay"
    assert all(row.reply_summary == read_reply_text for row in read_rows)

    stored_message = await in_database(read_message, "alice", message_id)
    phone_delivery = await in_database(read_delivery, stored_message.pk, device_ids["bob-phone"])
    assert stored_message.read_at is not None
    assert phone_delivery.state == MessageDelivery.State.DELIVERED
    assert phone_delivery.read_at == stored_message.read_at

    delivered_receipt_text = f"HT1 s bob {message_id} D"
    read_receipt_text = f"HT1 s bob {message_id} R"
    phone_receipt = await in_database(read_receipt, stored_message.pk, device_ids["alice-phone"])
    phone_receipt_packets = await in_database(read_receipt_packets, phone_receipt.pk)
    assert phone_receipt.state == ReceiptNotification.State.CONFIRMED
    assert (phone_receipt.target_level, phone_receipt.confirmed_level) == (READ_LEVEL, READ_LEVEL)
    assert [packet.text for packet in phone_receipt_packets] == [delivered_receipt_text, read_receipt_text]
    first_read_receipt_prepared_at = phone_receipt_packets[1].prepared_at
    assert first_read_receipt_prepared_at is not None
    assert first_read_receipt_prepared_at - stored_message.read_at <= WORKER_REACTION_ALLOWANCE

    tablet_receipt = await in_database(read_receipt, stored_message.pk, device_ids["alice-tablet"])
    tablet_receipt_packets = await in_database(read_receipt_packets, tablet_receipt.pk)
    tablet_read_receipt_packets = [packet for packet in tablet_receipt_packets if packet.text == read_receipt_text]
    assert tablet_receipt.state == ReceiptNotification.State.FAILED
    assert (tablet_receipt.target_level, tablet_receipt.confirmed_level) == (READ_LEVEL, DELIVERED_LEVEL)
    assert tablet_receipt.attempt_count == MAXIMUM_ATTEMPTS
    assert [packet.text for packet in tablet_receipt_packets] == [delivered_receipt_text] + [read_receipt_text] * (
        MAXIMUM_ATTEMPTS
    )
    assert [packet.attempt_number for packet in tablet_read_receipt_packets] == list(range(1, MAXIMUM_ATTEMPTS + 1))
    assert describe_round_spacing(tablet_read_receipt_packets) == []

    assert message.status is OutgoingMessageStatus.READ
    assert list(dict.fromkeys(message.received_receipt_levels)) == [ReceiptLevel.DELIVERED, ReceiptLevel.READ]
    assert alice_phone.sent_texts("C") == [f"HT1 C bob {message_id} D", f"HT1 C bob {message_id} R"]
    assert alice_tablet.receipts_for_unknown_messages == [
        ReceiptPush(recipient_username="bob", message_id=message_id, receipt_level=ReceiptLevel.DELIVERED)
    ]
    await in_database(assert_all_invariants)


async def test_a_read_within_the_delivered_receipt_hold_back_sends_only_the_read_receipt(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "alice-tablet", "bob-phone"
    )
    alice_phone = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    alice_tablet = await start_signed_in_client(start_client, devices["alice-tablet"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    device_ids = {name: await in_database(read_contact_id, device.public_key) for name, device in devices.items()}

    message = alice_phone.send_message("bob", GREETING_TEXT)
    message_id = message.message_id
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    read_confirmation = bob.mark_read("alice", received_message.message_id)
    await read_confirmation.wait_until_finished()
    await message.wait_for_status(OutgoingMessageStatus.READ)
    await alice_tablet.wait_until(
        lambda: bool(alice_tablet.receipts_for_unknown_messages), description="the tablet to get the receipt"
    )
    stored_message = await in_database(read_message, "alice", message_id)
    assert stored_message.delivered_at is not None
    assert stored_message.read_at is not None
    assert stored_message.read_at - stored_message.delivered_at < DELIVERED_RECEIPT_HOLD_BACK, (
        "the read reached the relay only after the hold-back, so this run cannot show the scenario"
    )
    await wait_until_worker_clock_passes(relay_worker, stored_message.delivered_at + 2 * DELIVERED_RECEIPT_HOLD_BACK)
    await wait_until_relay_is_quiet(relay_worker, simulated_mesh, [alice_phone, alice_tablet, bob])
    await relay_worker.stop()

    read_receipt_text = f"HT1 s bob {message_id} R"
    for sender_device_name in ("alice-phone", "alice-tablet"):
        receipt = await in_database(read_receipt, stored_message.pk, device_ids[sender_device_name])
        receipt_packets = await in_database(read_receipt_packets, receipt.pk)
        assert receipt.state == ReceiptNotification.State.CONFIRMED, sender_device_name
        assert (receipt.target_level, receipt.confirmed_level) == (READ_LEVEL, READ_LEVEL)
        assert [packet.text for packet in receipt_packets] == [read_receipt_text], sender_device_name
    all_receipt_packets = await in_database(read_packets, receipt_notification__isnull=False)
    assert {packet.receipt_level for packet in all_receipt_packets} == {READ_LEVEL}
    assert message.received_receipt_levels == [ReceiptLevel.READ]
    assert alice_phone.sent_texts("C") == [f"HT1 C bob {message_id} R"]
    assert alice_tablet.receipts_for_unknown_messages == [
        ReceiptPush(recipient_username="bob", message_id=message_id, receipt_level=ReceiptLevel.READ)
    ]
    await in_database(assert_all_invariants)


async def test_every_round_to_a_switched_off_device_sends_all_parts_and_its_incomplete_status_brings_the_rest_soon(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices(
        relay_worker, fake_companion_firmware, simulated_mesh, "alice-phone", "bob-phone"
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    bob_device_id = await in_database(read_contact_id, devices["bob-phone"].public_key)
    devices["bob-phone"].switch_off()

    message = alice.send_message("bob", FOUR_PART_TEXT)
    message_id = message.message_id
    assert len(message.parts) == 4
    await message.wait_for_status(OutgoingMessageStatus.SENT)
    stored_message = await in_database(read_message, "alice", message_id)
    rounds_while_switched_off = 3
    await wait_for_database(
        lambda: has_delivery_completed_round(stored_message.pk, bob_device_id, rounds_while_switched_off),
        description=f"{rounds_while_switched_off} rounds to the switched-off device",
    )
    # Back on, the device gets only the last part of the next round; its status then asks for the other three.
    bob_downlink = DirectMessageLossPolicy(
        text_prefixes_to_lose=[f"HT1 m alice {message_id} {part_number}/4 " for part_number in (1, 2, 3)]
    )
    devices["bob-phone"].downlink = bob_downlink
    devices["bob-phone"].switch_on()
    [received_message] = await bob.wait_for_received_messages("alice", 1, timeout_seconds=10)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await relay_worker.stop()

    delivery = await in_database(read_delivery, stored_message.pk, bob_device_id)
    delivery_packets = await in_database(read_delivery_packets, delivery.pk)
    part_numbers_by_round: dict[int | None, list[int | None]] = {}
    for packet in delivery_packets:
        part_numbers_by_round.setdefault(packet.attempt_number, []).append(packet.part_number)
    assert part_numbers_by_round == {
        1: [1, 2, 3, 4],
        2: [1, 2, 3, 4],
        3: [1, 2, 3, 4],
        4: [1, 2, 3, 4],
        5: [1, 2, 3],
    }
    assert bob_downlink.text_prefixes_to_lose == []
    assert len(bob_downlink.lost_texts) == 3

    incomplete_status_text = f"HT1 K alice {message_id} 0001"
    bob_statuses = bob.sent_texts("K")
    assert bob_statuses[0] == incomplete_status_text
    assert bob_statuses[-1] == f"HT1 K alice {message_id} 1111"
    [incomplete_status_row] = await in_database(read_inbox_rows_from, bob_device_id, incomplete_status_text)
    first_missing_parts_packet = next(packet for packet in delivery_packets if packet.attempt_number == 5)
    fourth_round_last_packet = [packet for packet in delivery_packets if packet.attempt_number == 4][-1]
    assert first_missing_parts_packet.prepared_at is not None
    assert fourth_round_last_packet.queued_at is not None
    time_to_missing_parts = first_missing_parts_packet.prepared_at - incomplete_status_row.received_at
    assert MISSING_PARTS_ROUND_DELAY <= time_to_missing_parts <= MISSING_PARTS_ROUND_DELAY + WORKER_REACTION_ALLOWANCE
    assert first_missing_parts_packet.prepared_at - fourth_round_last_packet.queued_at < calculate_expected_retry_pause(
        4
    )

    assert delivery.state == MessageDelivery.State.DELIVERED
    assert delivery.attempt_count == 5
    assert delivery.parts_received_mask == 0b1111
    assert received_message.text == FOUR_PART_TEXT
    assert len(bob.displayed_messages) == 1
    await in_database(assert_all_invariants)
