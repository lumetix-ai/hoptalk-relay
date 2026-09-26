"""The sender loop's pacing: packets awaiting ACKs, the reply place, the gap, the full packet pool, replies first."""

import asyncio
import itertools
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from pytest_django import Settings

from directory.models import Contact
from messaging.models import MessageDelivery, OutboundPacket
from messaging.outbound_scheduling import prepare_next_packet
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    FAST_PACING,
    FAST_RETRY_STRATEGY,
    FAST_WORKER_TIMING,
    DeviceInbox,
    RelayWorkerHarness,
    accept_messages,
    configure_relay_node,
    create_contact_for_device,
    create_sending_device,
    create_user,
    in_database,
    wait_for_database,
)
from worker.contact_reconciler import ReconciliationSummary
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

SENDING_DEVICE_NUMBER = 901


async def prepare_deliveries_to_bob(
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    *,
    message_count: int,
    part_count: int = 1,
    bob_device_uplink: LinkPolicy | None = None,
    bob_sync_state: Contact.NodeSyncState = Contact.NodeSyncState.ON_NODE,
) -> tuple[SimulatedDevice, Contact]:
    """Alice's messages to Bob, accepted and due: their deliveries go to Bob's simulated device."""
    await configure_relay_node(fake_companion_firmware)
    bob_device = simulated_mesh.add_device("bob", uplink=bob_device_uplink)
    bob = await in_database(create_user, "bob")
    alice = await in_database(create_user, "alice")
    bob_contact = await in_database(create_contact_for_device, bob_device, user=bob, node_sync_state=bob_sync_state)
    alice_contact = await in_database(create_sending_device, SENDING_DEVICE_NUMBER, alice)
    await in_database(accept_messages, alice_contact, "bob", message_count=message_count, part_count=part_count)
    return bob_device, bob_contact


def read_packets() -> list[OutboundPacket]:
    return list(OutboundPacket.objects.order_by("id"))


def count_packets(**filters: Any) -> int:
    return OutboundPacket.objects.filter(**filters).count()


def use_pacing(settings: Settings, relay_worker: RelayWorkerHarness, **pacing_changes: Any) -> None:
    settings.RELAY_SETTINGS = replace(settings.RELAY_SETTINGS, pacing=replace(FAST_PACING, **pacing_changes))
    relay_worker.worker = relay_worker.build_worker()


async def test_never_more_packets_await_an_ack_than_allowed_and_never_all_of_them_other_than_replies(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await prepare_deliveries_to_bob(
        fake_companion_firmware,
        simulated_mesh,
        message_count=3,
        part_count=4,
        bob_device_uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
    )
    tracker = relay_worker.worker.acknowledgement_tracker
    samples: list[tuple[int, int]] = []

    async def sample_packets_awaiting_acknowledgement() -> None:
        while True:
            awaited_packets = tracker.packets_awaiting_acknowledgement
            non_replies = [packet for packet in awaited_packets if packet.purpose != OutboundPacket.Purpose.REPLY]
            samples.append((len(awaited_packets), len(non_replies)))
            await asyncio.sleep(0.001)

    sampler = asyncio.create_task(sample_packets_awaiting_acknowledgement())
    relay_worker.start()
    try:
        await wait_for_database(
            lambda: count_packets(state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT) >= 12,
            description="twelve packets without their ACK",
        )
    finally:
        sampler.cancel()

    maximum = FAST_PACING.maximum_packets_awaiting_node_acknowledgement
    assert max(awaited_count for awaited_count, _ in samples) == maximum - 1
    assert max(non_reply_count for _, non_reply_count in samples) <= maximum - 1


async def test_the_reply_place_lets_a_reply_out_while_other_packets_fill_the_rest(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    bob_device, _bob_contact = await prepare_deliveries_to_bob(
        fake_companion_firmware,
        simulated_mesh,
        message_count=3,
        part_count=4,
        bob_device_uplink=LinkPolicy(acknowledgement_loss_probability=1.0),
    )
    tracker = relay_worker.worker.acknowledgement_tracker
    maximum = FAST_PACING.maximum_packets_awaiting_node_acknowledgement
    relay_worker.start()
    await wait_until(
        lambda: tracker.count_packets_awaiting_acknowledgement() == maximum - 1,
        description="every place but the reply's taken",
    )
    inbox = DeviceInbox(bob_device)

    bob_device.send_direct_message("HT1 Q alice")

    await inbox.wait_for_text("HT1 q alice 1")


async def test_consecutive_sends_keep_the_minimum_gap(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    minimum_gap_seconds = 0.15
    use_pacing(settings, relay_worker, minimum_seconds_between_sends=minimum_gap_seconds)
    await prepare_deliveries_to_bob(fake_companion_firmware, simulated_mesh, message_count=3, part_count=2)

    relay_worker.start()
    await wait_for_database(lambda: count_packets() >= 5, description="five packets")

    packets = await in_database(read_packets)
    for previous_packet, next_packet in itertools.pairwise(packets):
        assert previous_packet.queued_at is not None
        assert next_packet.prepared_at is not None
        assert next_packet.prepared_at - previous_packet.queued_at >= timedelta(seconds=minimum_gap_seconds * 0.95)


async def test_a_full_packet_pool_backs_off_without_spending_an_attempt_and_everything_goes_out_later(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await prepare_deliveries_to_bob(fake_companion_firmware, simulated_mesh, message_count=2)
    fake_companion_firmware.occupy_packet_pool(fake_companion_firmware.capacities.packet_pool_packets)

    relay_worker.start()
    await wait_for_database(
        lambda: count_packets(state=OutboundPacket.State.REJECTED_BY_NODE, node_error_code=3) >= 3,
        description="sends refused by the full packet pool",
    )
    deliveries = await in_database(lambda: list(MessageDelivery.objects.order_by("id")))
    assert max(delivery.attempt_count for delivery in deliveries) <= 1
    refused_packets = await in_database(read_packets)
    refusal_times = [packet.prepared_at for packet in refused_packets if packet.prepared_at is not None]
    refusal_gaps = [(later - earlier).total_seconds() for earlier, later in itertools.pairwise(refusal_times)]
    assert all(gap >= FAST_WORKER_TIMING.table_full_backoff_initial_seconds * 0.9 for gap in refusal_gaps)

    fake_companion_firmware.release_packet_pool()

    await wait_for_database(
        lambda: (
            count_packets(state=OutboundPacket.State.QUEUED_ON_NODE)
            + count_packets(state=OutboundPacket.State.NODE_ACKNOWLEDGED)
            + count_packets(state=OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT)
            >= 2
        ),
        description="both deliveries sent once the pool has room",
    )
    deliveries = await in_database(lambda: list(MessageDelivery.objects.order_by("id")))
    assert [delivery.attempt_count for delivery in deliveries] == [1, 1]


async def test_a_waiting_reply_goes_before_due_deliveries(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    bob_device, _bob_contact = await prepare_deliveries_to_bob(fake_companion_firmware, simulated_mesh, message_count=3)
    worker_state = relay_worker.worker.worker_state

    async with worker_state.pause_sending("the test holds the sender"):
        relay_worker.start()
        await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
        bob_device.send_direct_message("HT1 Q alice")
        await wait_until(lambda: len(relay_worker.worker.reply_queue) == 1, description="the reply queued")

    await wait_for_database(lambda: count_packets() >= 2, description="the first packets")
    packets = await in_database(read_packets)
    assert packets[0].purpose == OutboundPacket.Purpose.REPLY
    assert packets[0].text == "HT1 q alice 1"


async def test_work_held_back_by_a_failed_contact_does_not_keep_the_sender_busy(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prepare_deliveries_to_bob(
        fake_companion_firmware, simulated_mesh, message_count=2, bob_sync_state=Contact.NodeSyncState.ADD_FAILED
    )
    relay_worker.timing = replace(FAST_WORKER_TIMING, minimum_sender_sleep_seconds=0.1)
    relay_worker.worker = relay_worker.build_worker()
    monkeypatch.setattr(relay_worker.worker.contact_reconciler, "reconcile_contacts", reconcile_nothing)
    call_count = [0]

    def count_prepare_calls(*arguments: Any) -> Any:
        call_count[0] += 1
        return prepare_next_packet(*arguments)

    monkeypatch.setattr("worker.sender_loop.prepare_next_packet", count_prepare_calls)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    calls_before = call_count[0]

    await asyncio.sleep(1.0)

    assert call_count[0] - calls_before <= 12
    assert await in_database(count_packets) == 0


async def reconcile_nothing() -> ReconciliationSummary:
    """Keeps the failed contact failed: a real pass would find it on the node and mark it so."""
    return ReconciliationSummary()


async def test_work_held_back_by_the_per_device_cap_does_not_keep_the_sender_busy(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_pacing(settings, relay_worker, maximum_active_deliveries_per_device=1)
    await prepare_deliveries_to_bob(fake_companion_firmware, simulated_mesh, message_count=3)
    relay_worker.timing = replace(FAST_WORKER_TIMING, minimum_sender_sleep_seconds=0.1)
    relay_worker.worker = relay_worker.build_worker()
    call_count = [0]

    def count_prepare_calls(*arguments: Any) -> Any:
        call_count[0] += 1
        return prepare_next_packet(*arguments)

    monkeypatch.setattr("worker.sender_loop.prepare_next_packet", count_prepare_calls)
    relay_worker.start()
    await wait_for_database(lambda: count_packets() >= 1, description="the first delivery sent")
    calls_before = call_count[0]

    await asyncio.sleep(0.25)

    assert call_count[0] - calls_before <= 6
    deliveries_in_progress = await in_database(
        lambda: MessageDelivery.objects.filter(round_started_at__isnull=False).count()
    )
    assert deliveries_in_progress == 1


async def test_a_status_with_missing_parts_wakes_the_sender_for_the_next_round_at_once(
    settings: Settings,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
) -> None:
    """The retry pause and the sender's longest sleep are far longer than the missing-parts delay.

    Parts 1 and 3 can only arrive within the timeout if the status itself wakes the sender.
    """
    long_seconds = 30.0
    settings.RELAY_SETTINGS = replace(
        settings.RELAY_SETTINGS,
        retry_strategy=replace(
            FAST_RETRY_STRATEGY, initial_pause_seconds=long_seconds, maximum_pause_seconds=long_seconds
        ),
    )
    relay_worker.timing = replace(FAST_WORKER_TIMING, maximum_sender_sleep_seconds=long_seconds)
    relay_worker.worker = relay_worker.build_worker()
    bob_device, _bob_contact = await prepare_deliveries_to_bob(
        fake_companion_firmware, simulated_mesh, message_count=1, part_count=3
    )
    inbox = DeviceInbox(bob_device)
    part_texts = [f"HT1 m alice 1 {part_number}/3 part {part_number} of 1" for part_number in (1, 2, 3)]
    relay_worker.start()
    for part_text in part_texts:
        await inbox.wait_for_text(part_text)
    await wait_for_database(
        lambda: MessageDelivery.objects.filter(attempt_count=1, round_pending_parts_mask=0).exists(),
        description="the first round complete",
    )

    bob_device.send_direct_message("HT1 K alice 1 010")

    await wait_until(
        lambda: inbox.texts.count(part_texts[0]) == 2 and inbox.texts.count(part_texts[2]) == 2,
        timeout_seconds=long_seconds / 10,
        description=f"parts 1 and 3 sent again (so far {inbox.texts})",
    )
    assert inbox.texts.count(part_texts[1]) == 1
