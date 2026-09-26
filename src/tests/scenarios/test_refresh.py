"""Refreshing a conversation: a device that missed messages asks for them and gets them oldest first, one at a time.

A device that stays switched off is given up after three rounds here, about two seconds, instead
of the six rounds of the default strategy: these scenarios wait for that more than once.
"""

from datetime import datetime
from itertools import pairwise

import pytest
from pytest_django import Settings

from messaging.models import Message, MessageDelivery, OutboundPacket, RefreshSession
from protocol.constants import REFRESH_ALL_PEERS_TARGET
from tests.invariants import assert_all_invariants
from tests.scenarios.refresh_lifecycle_admin_helpers import (
    DirectMessageTrigger,
    build_single_part_text,
    find_first_packet_prepared_at,
    give_up_on_devices_after_rounds,
    install_triggering_links,
    read_deliveries_to,
    read_delivery,
    read_delivery_packets,
    read_refresh_sessions_of,
    start_relay_with_devices_from_cards,
    start_signed_in_client,
    wait_for_refresh_answer,
    wait_until_the_relay_accepted,
)
from tests.scenarios.scenario_setup import ClientStarter
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database, wait_for_database
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from tests.worker.simulated_hoptalk_client_records import (
    ConversationRefresh,
    OutgoingMessage,
    OutgoingMessageStatus,
)

pytestmark = pytest.mark.django_db(transaction=True)

ROUNDS_BEFORE_A_DEVICE_IS_GIVEN_UP = 3
# Three rounds end after 0.3 + 0.6 + 1.2 s of pauses; the rest is headroom for a loaded machine.
GIVE_UP_TIMEOUT_SECONDS = 10.0
DELIVERY_TIMEOUT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def give_up_on_a_device_after_three_rounds(use_scenario_relay_settings: None, settings: Settings) -> None:
    give_up_on_devices_after_rounds(settings, ROUNDS_BEFORE_A_DEVICE_IS_GIVEN_UP)


def delivery_states_to(device: SimulatedDevice) -> list[str]:
    return [delivery.state for delivery in read_deliveries_to(device)]


async def send_messages_the_device_misses(
    sender: SimulatedHopTalkClient,
    recipient_username: str,
    recipient_device: SimulatedDevice,
    message_count: int,
) -> list[OutgoingMessage]:
    """Messages sent while the recipient's only device is off, until the relay gives each of them up."""
    recipient_device.switch_off()
    sender_username = sender.signed_in_username
    assert sender_username is not None
    messages = [
        sender.send_message(recipient_username, build_single_part_text(sender_username, message_number))
        for message_number in range(1, message_count + 1)
    ]
    for message in messages:
        await message.wait_for_status(OutgoingMessageStatus.SENT)
    await wait_for_database(
        lambda: delivery_states_to(recipient_device) == [MessageDelivery.State.FAILED] * message_count,
        timeout_seconds=GIVE_UP_TIMEOUT_SECONDS,
        description=f"the relay to give up every message to {recipient_device.name}",
    )
    return messages


async def open_conversation_and_wait_for_count(
    client: SimulatedHopTalkClient, peer_username: str
) -> tuple[ConversationRefresh, int]:
    refresh = client.open_conversation(peer_username)
    return refresh, await wait_for_refresh_answer(client, refresh)


def packets_prepared_since(delivery_id: int, moment: datetime) -> list[OutboundPacket]:
    return [
        packet
        for packet in read_delivery_packets(delivery_id)
        if packet.prepared_at is not None and packet.prepared_at >= moment
    ]


def assert_sent_one_after_another(deliveries: list[MessageDelivery], refresh_requested_at: datetime) -> None:
    """Since the refresh was requested, no part of a message went out before the previous message was delivered."""
    for earlier_delivery, later_delivery in pairwise(deliveries):
        assert earlier_delivery.delivered_at is not None, earlier_delivery
        later_packets = packets_prepared_since(later_delivery.pk, refresh_requested_at)
        assert find_first_packet_prepared_at(later_packets) > earlier_delivery.delivered_at, (
            f"message {later_delivery.message.client_message_id} was sent before "
            f"message {earlier_delivery.message.client_message_id} was delivered"
        )


def displayed_texts_from(client: SimulatedHopTalkClient, peer_username: str) -> list[str]:
    return [
        incoming_message.text
        for incoming_message in client.displayed_messages
        if incoming_message.sender_username.lower() == peer_username.lower()
    ]


async def test_a_refresh_delivers_the_missed_messages_oldest_first_and_a_lost_acknowledgement_costs_one_round(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    missed_messages = await send_messages_the_device_misses(alice, "bob", devices["bob-phone"], 3)
    bob_uplink, _bob_downlink = install_triggering_links(devices["bob-phone"])
    lost_acknowledgement = bob_uplink.add_trigger(
        DirectMessageTrigger(text_prefix=f"HT1 K alice {missed_messages[1].message_id} ", drops_the_message=True)
    )
    devices["bob-phone"].switch_on()

    _refresh, reported_count = await open_conversation_and_wait_for_count(bob, "alice")
    await bob.wait_for_received_messages("alice", 3, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    for message in missed_messages:
        await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert reported_count == 3
    assert lost_acknowledgement.has_fired
    assert displayed_texts_from(bob, "alice") == [message.text for message in missed_messages]
    assert [incoming.part_copies_received for incoming in bob.received_messages("alice")] == [1, 2, 1]
    deliveries = await in_database(read_deliveries_to, devices["bob-phone"])
    [refresh_session] = await in_database(read_refresh_sessions_of, devices["bob-phone"])
    assert refresh_session.state == RefreshSession.State.COMPLETED
    assert refresh_session.messages_total == 3
    assert not refresh_session.requested_for_all_peers
    assert [delivery.message.client_message_id for delivery in deliveries] == [
        message.message_id for message in missed_messages
    ]
    assert all(delivery.state == MessageDelivery.State.DELIVERED for delivery in deliveries)
    assert all(delivery.refresh_session_id == refresh_session.pk for delivery in deliveries)
    assert [delivery.attempt_count for delivery in deliveries] == [1, 2, 1]
    await in_database(assert_sent_one_after_another, deliveries, refresh_session.requested_at)
    await in_database(assert_all_invariants)


async def setup_a_refresh_whose_device_goes_off_after_the_first_message(
    alice: SimulatedHopTalkClient,
    bob: SimulatedHopTalkClient,
    bob_device: SimulatedDevice,
) -> tuple[list[OutgoingMessage], RefreshSession]:
    """Three missed messages; bob refreshes, gets the first, and his device goes off as its "K" leaves."""
    missed_messages = await send_messages_the_device_misses(alice, "bob", bob_device, 3)
    bob_uplink, _bob_downlink = install_triggering_links(bob_device)
    switch_off_after_first_acknowledgement = bob_uplink.add_trigger(
        DirectMessageTrigger(text_prefix=f"HT1 K alice {missed_messages[0].message_id} ", action=bob_device.switch_off)
    )
    bob_device.switch_on()

    _refresh, reported_count = await open_conversation_and_wait_for_count(bob, "alice")
    assert reported_count == 3
    await wait_for_database(
        lambda: (
            read_delivery("alice", missed_messages[0].message_id, bob_device).state == MessageDelivery.State.DELIVERED
        ),
        description="the first missed message to be delivered",
    )
    assert switch_off_after_first_acknowledgement.has_fired
    assert not bob_device.is_switched_on
    [refresh_session] = await in_database(read_refresh_sessions_of, bob_device)
    return missed_messages, refresh_session


async def test_an_exhausted_head_stops_the_refresh_without_sending_the_rest_and_a_new_refresh_starts_afresh(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    bob_device = devices["bob-phone"]
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    missed_messages, first_session = await setup_a_refresh_whose_device_goes_off_after_the_first_message(
        alice, bob, bob_device
    )
    third_message_id = missed_messages[2].message_id

    await wait_for_database(
        lambda: RefreshSession.objects.get(id=first_session.pk).state == RefreshSession.State.STOPPED,
        timeout_seconds=GIVE_UP_TIMEOUT_SECONDS,
        description="the refresh to stop once its head is exhausted",
    )
    stopped_head = await in_database(read_delivery, "alice", missed_messages[1].message_id, bob_device)
    unsent_delivery = await in_database(read_delivery, "alice", third_message_id, bob_device)
    assert stopped_head.state == MessageDelivery.State.FAILED
    assert stopped_head.failure_reason == MessageDelivery.FailureReason.ATTEMPTS_EXHAUSTED
    assert stopped_head.attempt_count == ROUNDS_BEFORE_A_DEVICE_IS_GIVEN_UP
    assert unsent_delivery.state == MessageDelivery.State.FAILED
    assert unsent_delivery.failure_reason == MessageDelivery.FailureReason.REFRESH_STOPPED
    assert unsent_delivery.refresh_session_id == first_session.pk
    assert await in_database(packets_prepared_since, unsent_delivery.pk, first_session.requested_at) == []

    bob_device.switch_on()
    bob.close_conversation("alice")
    _second_refresh, second_count = await open_conversation_and_wait_for_count(bob, "alice")
    await bob.wait_for_received_messages("alice", 3, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    for message in missed_messages:
        await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert second_count == 2
    assert displayed_texts_from(bob, "alice") == [message.text for message in missed_messages]
    first_session_now, second_session = await in_database(read_refresh_sessions_of, bob_device)
    assert first_session_now.state == RefreshSession.State.STOPPED
    assert second_session.state == RefreshSession.State.COMPLETED
    assert second_session.messages_total == 2
    restarted_head = await in_database(read_delivery, "alice", missed_messages[1].message_id, bob_device)
    last_delivery = await in_database(read_delivery, "alice", third_message_id, bob_device)
    assert restarted_head.refresh_session_id == last_delivery.refresh_session_id == second_session.pk
    assert restarted_head.arm_generation > stopped_head.arm_generation
    assert restarted_head.attempt_count == 1
    assert restarted_head.maximum_attempts == ROUNDS_BEFORE_A_DEVICE_IS_GIVEN_UP
    assert last_delivery.attempt_count == 1
    await in_database(assert_sent_one_after_another, [restarted_head, last_delivery], second_session.requested_at)
    await in_database(assert_all_invariants)


async def test_reopening_the_conversation_sends_a_waiting_head_again_at_once_with_fresh_counters(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    bob_device = devices["bob-phone"]
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    missed_messages, refresh_session = await setup_a_refresh_whose_device_goes_off_after_the_first_message(
        alice, bob, bob_device
    )
    head_message_id = missed_messages[1].message_id

    def head_waits_after_its_second_round() -> bool:
        head = read_delivery("alice", head_message_id, bob_device)
        return head.attempt_count == 2 and head.round_pending_parts_mask == 0

    await wait_for_database(head_waits_after_its_second_round, description="the head to wait between rounds")
    waiting_head = await in_database(read_delivery, "alice", head_message_id, bob_device)
    assert waiting_head.next_attempt_at is not None
    bob_device.switch_on()
    bob.close_conversation("alice")
    _reopened_refresh, reported_count = await open_conversation_and_wait_for_count(bob, "alice")
    await bob.wait_for_received_messages("alice", 3, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    for message in missed_messages:
        await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert reported_count == 2
    assert displayed_texts_from(bob, "alice") == [message.text for message in missed_messages]
    restarted_head = await in_database(read_delivery, "alice", head_message_id, bob_device)
    assert restarted_head.state == MessageDelivery.State.DELIVERED
    assert restarted_head.arm_generation == waiting_head.arm_generation + 1
    assert restarted_head.attempt_count == 1
    head_packets = await in_database(read_delivery_packets, restarted_head.pk)
    [packet_of_the_fresh_round] = [
        packet for packet in head_packets if packet.arm_generation == restarted_head.arm_generation
    ]
    assert packet_of_the_fresh_round.prepared_at is not None
    assert packet_of_the_fresh_round.prepared_at < waiting_head.next_attempt_at
    [only_session] = await in_database(read_refresh_sessions_of, bob_device)
    assert only_session.pk == refresh_session.pk
    assert only_session.state == RefreshSession.State.COMPLETED
    await in_database(assert_all_invariants)


async def test_a_message_accepted_during_a_refresh_joins_it_and_is_delivered_after_the_older_ones(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    bob_device = devices["bob-phone"]
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    missed_messages = await send_messages_the_device_misses(alice, "bob", bob_device, 3)
    bob_uplink, _bob_downlink = install_triggering_links(bob_device)
    bob_uplink.add_trigger(
        DirectMessageTrigger(text_prefix=f"HT1 K alice {missed_messages[0].message_id} ", drops_the_message=True)
    )
    bob_device.switch_on()
    _refresh, reported_count = await open_conversation_and_wait_for_count(bob, "alice")
    [refresh_session] = await in_database(read_refresh_sessions_of, bob_device)

    later_message = alice.send_message("bob", build_single_part_text("alice", 4))
    await wait_until_the_relay_accepted(later_message)
    appended_delivery = await in_database(read_delivery, "alice", later_message.message_id, bob_device)
    session_while_active = await in_database(RefreshSession.objects.get, id=refresh_session.pk)
    await bob.wait_for_received_messages("alice", 4, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await later_message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert reported_count == 3
    assert appended_delivery.state == MessageDelivery.State.QUEUED_FOR_REFRESH
    assert appended_delivery.refresh_session_id == refresh_session.pk
    assert session_while_active.state == RefreshSession.State.ACTIVE
    assert session_while_active.messages_total == 4
    all_messages = [*missed_messages, later_message]
    assert displayed_texts_from(bob, "alice") == [message.text for message in all_messages]
    deliveries = await in_database(read_deliveries_to, bob_device)
    assert [delivery.message.client_message_id for delivery in deliveries] == [
        message.message_id for message in all_messages
    ]
    assert all(delivery.refresh_session_id == refresh_session.pk for delivery in deliveries)
    await in_database(assert_sent_one_after_another, deliveries, refresh_session.requested_at)
    finished_session = await in_database(RefreshSession.objects.get, id=refresh_session.pk)
    assert finished_session.state == RefreshSession.State.COMPLETED
    assert finished_session.messages_total == 4
    await in_database(assert_all_invariants)


async def test_refreshing_every_conversation_on_a_new_device_brings_what_no_device_of_the_user_received(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker,
        fake_companion_firmware,
        simulated_mesh,
        ["alice-phone", "carol-phone", "bob-phone", "bob-tablet"],
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    carol = await start_signed_in_client(start_client, devices["carol-phone"], "carol")
    bob_on_phone = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    first_messages = [
        alice.send_message("bob", build_single_part_text("alice", 1)),
        carol.send_message("bob", build_single_part_text("carol", 1)),
    ]
    await bob_on_phone.wait_for_received_messages("alice", 1)
    await bob_on_phone.wait_for_received_messages("carol", 1)
    for message in first_messages:
        await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    devices["bob-phone"].switch_off()
    missed_messages_by_sender: dict[str, list[OutgoingMessage]] = {"alice": [], "carol": []}
    for message_number in (2, 3):
        for sender_username, sender in (("alice", alice), ("carol", carol)):
            missed_messages_by_sender[sender_username].append(
                sender.send_message("bob", build_single_part_text(sender_username, message_number))
            )
    for missed_messages in missed_messages_by_sender.values():
        for message in missed_messages:
            await message.wait_for_status(OutgoingMessageStatus.SENT)

    bob_on_tablet = await start_signed_in_client(start_client, devices["bob-tablet"], "bob")
    [refresh_of_everything] = [
        request
        for request in bob_on_tablet.storage.finished_requests
        if isinstance(request, ConversationRefresh) and request.refresh_target == REFRESH_ALL_PEERS_TARGET
    ]
    reported_count = refresh_of_everything.reported_message_count
    for sender_username in ("alice", "carol"):
        await bob_on_tablet.wait_for_received_messages(sender_username, 2, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    for missed_messages in missed_messages_by_sender.values():
        for message in missed_messages:
            await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert reported_count == 4
    for sender_username, missed_messages in missed_messages_by_sender.items():
        assert displayed_texts_from(bob_on_tablet, sender_username) == [message.text for message in missed_messages]
    tablet_sessions = await in_database(read_refresh_sessions_of, devices["bob-tablet"])
    assert sorted(session.peer.username for session in tablet_sessions) == ["alice", "carol"]
    assert all(session.requested_for_all_peers for session in tablet_sessions)
    assert all(session.state == RefreshSession.State.COMPLETED for session in tablet_sessions)
    assert all(session.messages_total == 2 for session in tablet_sessions)
    tablet_deliveries = await in_database(read_deliveries_to, devices["bob-tablet"])
    delivered_message_ids = {delivery.message.client_message_id for delivery in tablet_deliveries}
    assert delivered_message_ids == {
        message.message_id for missed_messages in missed_messages_by_sender.values() for message in missed_messages
    }
    assert not delivered_message_ids & {message.message_id for message in first_messages}
    for session in tablet_sessions:
        session_deliveries = [delivery for delivery in tablet_deliveries if delivery.refresh_session_id == session.pk]
        assert all(delivery.message.sender_id == session.peer_id for delivery in session_deliveries)
        await in_database(assert_sent_one_after_another, session_deliveries, session.requested_at)
    assert await in_database(Message.objects.count) == 6
    await in_database(assert_all_invariants)
