"""The operator adds, pairs and deletes devices and users on the admin panel while the relay carries traffic.

The panel is driven over HTTP as a browser drives it, each request on a thread and a database
connection of its own, so it runs concurrently with the worker as the web process does.
"""

import logging
import time
from datetime import datetime
from http import HTTPStatus

import pytest
from django.utils import timezone
from pytest_django import Settings

from directory.models import Contact
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket, RefreshSession
from node.models import HeardAdvert, PairingSession
from node.pairing_sessions import get_active_pairing_session
from tests.invariants import assert_all_invariants
from tests.panel_operator import PanelOperator
from tests.scenarios.refresh_lifecycle_admin_helpers import (
    HTMX_STOP_POLLING_STATUS,
    TEN_PART_TEXT,
    DirectMessageTrigger,
    OperatorBrowser,
    build_single_part_text,
    find_packets_awaiting_acknowledgement_at,
    give_up_on_devices_after_rounds,
    install_triggering_links,
    make_relay_node_suggest_radio_like_acknowledgement_waits,
    put_node_port_gate_before_worker,
    read_contact_of,
    read_deliveries_to,
    read_delivery,
    read_inbox_rows_from,
    read_packets_labelled_for,
    read_refresh_sessions_of,
    read_user,
    start_relay_with_devices_from_cards,
    start_signed_in_client,
    wait_until_the_relay_accepted,
    wall_clock_time_of,
)
from tests.scenarios.scenario_setup import ClientStarter
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.radio_packets import AdvertPacket
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import RelayWorkerHarness, in_database, wait_for_database
from tests.worker.simulated_hoptalk_client_records import OutgoingMessageStatus
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

PAIRING_DURATION_SECONDS = 60
PAIRING_ADVERT_INTERVAL_SECONDS = 10
DELIVERY_TIMEOUT_SECONDS = 10.0
ROUNDS_BEFORE_A_DEVICE_IS_GIVEN_UP = 3
# The worker failed to open the node this many times more after the card was added.
CONNECTION_ATTEMPTS_WHILE_THE_CARD_WAITS = 3


@pytest.fixture
def devices_are_given_up_after_three_rounds(use_scenario_relay_settings: None, settings: Settings) -> None:
    give_up_on_devices_after_rounds(settings, ROUNDS_BEFORE_A_DEVICE_IS_GIVEN_UP)


def count_adverts(firmware: FakeCompanionFirmware) -> int:
    return sum(1 for packet in firmware.transmitted_packets if isinstance(packet, AdvertPacket))


def read_adverts_sent(pairing_session_id: int) -> int:
    return PairingSession.objects.get(id=pairing_session_id).adverts_sent


async def wait_for_advert_count(
    firmware: FakeCompanionFirmware, pairing_session_id: int, expected_advert_count: int
) -> None:
    """Wait until the node sent the advert and the worker recorded it.

    The worker takes the time the next advert counts from after the node has sent this one, and
    records the advert after that: a clock moved forward before then would push the next one away.
    """
    await wait_until(
        lambda: count_adverts(firmware) == expected_advert_count, description=f"advert {expected_advert_count}"
    )
    await wait_for_database(
        lambda: read_adverts_sent(pairing_session_id) == expected_advert_count,
        description=f"the worker to record advert {expected_advert_count}",
    )


def contact_sync_state_of(device: SimulatedDevice) -> str | None:
    contact = read_contact_of(device)
    return contact.node_sync_state if contact is not None else None


def find_removal_time(firmware: FakeCompanionFirmware, device: SimulatedDevice) -> datetime:
    """When the worker asked the node to remove the device's contact."""
    [removal_command] = [
        received_command
        for received_command in firmware.command_log
        if received_command.code == CommandCode.REMOVE_CONTACT and device.public_key in received_command.frame
    ]
    return wall_clock_time_of(removal_command.received_at)


async def test_pairing_captures_a_new_device_the_operator_adds_and_which_then_signs_in_and_talks(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone"]
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    browser = await OperatorBrowser.sign_in(panel_operator)
    newcomer = simulated_mesh.add_device("dave-phone", relay_knows_device=False)

    start_response = await browser.post_form(
        "/contacts/pairing/start",
        {
            "duration_seconds": str(PAIRING_DURATION_SECONDS),
            "advert_interval_seconds": str(PAIRING_ADVERT_INTERVAL_SECONDS),
        },
    )
    await wait_for_database(
        lambda: get_active_pairing_session() is not None, description="the worker to start the pairing session"
    )
    pairing_session = await in_database(get_active_pairing_session)
    assert pairing_session is not None
    await wait_for_advert_count(fake_companion_firmware, pairing_session.pk, 1)
    for expected_advert_count in (2, 3):
        relay_worker.clock.advance(seconds=PAIRING_ADVERT_INTERVAL_SECONDS)
        await wait_for_advert_count(fake_companion_firmware, pairing_session.pk, expected_advert_count)
    newcomer.send_advert()
    await wait_for_database(
        lambda: HeardAdvert.objects.filter(pairing_session_id=pairing_session.pk).exists(),
        description="the newcomer's advert to be captured",
    )
    heard_advert = await in_database(HeardAdvert.objects.get, pairing_session_id=pairing_session.pk)
    pairing_panel_path = f"/contacts/pairing/{pairing_session.pk}/partials/panel"
    panel_while_active = await browser.get_partial(pairing_panel_path)
    add_response = await browser.post_form(f"/contacts/pairing/{pairing_session.pk}/adverts/{heard_advert.pk}/add")
    await wait_for_database(
        lambda: contact_sync_state_of(newcomer) == Contact.NodeSyncState.ON_NODE,
        description="the paired device to be put on the node",
    )
    relay_worker.clock.advance(seconds=PAIRING_DURATION_SECONDS)
    await wait_for_database(
        lambda: PairingSession.objects.get(id=pairing_session.pk).state == PairingSession.State.ENDED,
        description="the pairing session to end",
    )
    panel_after_the_end = await browser.get_partial(pairing_panel_path)

    assert start_response.status_code == HTTPStatus.FOUND
    assert add_response.status_code == HTTPStatus.FOUND
    assert heard_advert.public_key == newcomer.public_key.hex()
    assert heard_advert.name == "dave-phone"
    assert panel_while_active.status_code == HTTPStatus.OK
    assert newcomer.public_key.hex() in panel_while_active.content.decode()
    assert panel_after_the_end.status_code == HTMX_STOP_POLLING_STATUS
    ended_session = await in_database(PairingSession.objects.get, id=pairing_session.pk)
    assert ended_session.adverts_sent == 3
    assert count_adverts(fake_companion_firmware) == 3
    assert not any(
        packet.route.is_flood
        for packet in fake_companion_firmware.transmitted_packets
        if isinstance(packet, AdvertPacket)
    )
    paired_contact = await in_database(read_contact_of, newcomer)
    assert paired_contact is not None
    assert paired_contact.source == Contact.Source.PAIRING
    assert (await in_database(HeardAdvert.objects.get, id=heard_advert.pk)).added_contact_id == paired_contact.pk
    stored_record = fake_companion_firmware.find_contact(newcomer.public_key)
    assert stored_record is not None
    assert not stored_record.has_known_route

    dave = await start_signed_in_client(start_client, newcomer, "dave")
    message = dave.send_message("alice", "Hello from a paired device")
    [received_message] = await alice.wait_for_received_messages("dave", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    assert received_message.text == message.text
    await in_database(assert_all_invariants)


async def test_a_card_added_while_the_node_is_away_stays_pending_and_goes_on_the_node_when_it_is_back(
    relay_worker: RelayWorkerHarness,
    fake_node_connector: FakeNodeConnector,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    node_port_gate = put_node_port_gate_before_worker(relay_worker, fake_node_connector)
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone"]
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    browser = await OperatorBrowser.sign_in(panel_operator)
    node_port_gate.close()
    fake_node_connector.simulate_link_loss()
    await relay_worker.wait_for_relay_mode(RelayMode.DISCONNECTED)

    carol_device = simulated_mesh.add_device("carol-phone", relay_knows_device=False)
    add_response = await browser.post_form("/contacts/card/add", {"card_uri": carol_device.contact_card_uri()})
    refused_attempts_after_the_card = node_port_gate.refused_connection_attempts
    await wait_until(
        lambda: (
            node_port_gate.refused_connection_attempts
            >= refused_attempts_after_the_card + CONNECTION_ATTEMPTS_WHILE_THE_CARD_WAITS
        ),
        description="the worker to try the node a few more times",
    )
    state_while_away = await in_database(contact_sync_state_of, carol_device)
    contact_list_while_away = (await browser.get_partial("/contacts/partials/list")).content.decode()
    carol_on_the_node_while_away = fake_companion_firmware.find_contact(carol_device.public_key)

    node_port_gate.open()
    await relay_worker.wait_for_connection_generation(2)
    await wait_for_database(
        lambda: contact_sync_state_of(carol_device) == Contact.NodeSyncState.ON_NODE,
        description="the card's contact to be put on the node",
    )
    carol = await start_signed_in_client(start_client, carol_device, "carol")
    message = carol.send_message("alice", "Added while the node was away")
    [received_message] = await alice.wait_for_received_messages("carol", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert add_response.status_code == HTTPStatus.FOUND
    assert state_while_away == Contact.NodeSyncState.PENDING_ADD
    assert "carol-phone" in contact_list_while_away
    assert carol_on_the_node_while_away is None
    stored_record = fake_companion_firmware.find_contact(carol_device.public_key)
    assert stored_record is not None
    assert stored_record.name == b"carol-phone"
    assert received_message.text == message.text
    await in_database(assert_all_invariants)


async def test_a_device_deleted_while_a_part_to_it_awaits_its_firmware_ack_leaves_the_node_only_after_the_ack_window(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    make_relay_node_suggest_radio_like_acknowledgement_waits(fake_companion_firmware)
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    bob_device = devices["bob-phone"]
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    await start_signed_in_client(start_client, bob_device, "bob")
    browser = await OperatorBrowser.sign_in(panel_operator)
    bob_contact = await in_database(read_contact_of, bob_device)
    assert bob_contact is not None
    bob_device.switch_off()
    message = alice.send_message("bob", "On its way while the device is deleted")
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    def a_part_awaits_its_firmware_ack() -> bool:
        delivery = read_delivery("alice", message.message_id, bob_device)
        return OutboundPacket.objects.filter(
            message_delivery=delivery,
            state=OutboundPacket.State.QUEUED_ON_NODE,
            acknowledgement_deadline_at__gt=timezone.now(),
        ).exists()

    await wait_for_database(a_part_awaits_its_firmware_ack, description="a part to await its firmware ACK")
    [part_in_flight] = [
        packet
        for packet in await in_database(read_packets_labelled_for, bob_device)
        if packet.state == OutboundPacket.State.QUEUED_ON_NODE
    ]
    delete_response = await browser.post_form(f"/contacts/{bob_contact.pk}/delete")
    deletion_committed_at = timezone.now()
    bob_on_the_node_after_the_deletion = fake_companion_firmware.find_contact(bob_device.public_key)
    await wait_until(
        lambda: fake_companion_firmware.find_contact(bob_device.public_key) is None,
        description="the deleted device to be removed from the node",
    )
    removed_at = find_removal_time(fake_companion_firmware, bob_device)

    assert delete_response.status_code == HTTPStatus.FOUND
    assert part_in_flight.acknowledgement_deadline_at is not None
    assert part_in_flight.acknowledgement_deadline_at > deletion_committed_at
    assert bob_on_the_node_after_the_deletion is not None
    assert removed_at >= part_in_flight.acknowledgement_deadline_at
    assert await in_database(find_packets_awaiting_acknowledgement_at, removed_at) == []
    assert await in_database(read_contact_of, bob_device) is None
    assert await in_database(read_deliveries_to, bob_device) == []
    packet_after_the_deletion = await in_database(OutboundPacket.objects.get, id=part_in_flight.pk)
    assert packet_after_the_deletion.contact_id is None
    assert packet_after_the_deletion.message_delivery_id is None
    assert packet_after_the_deletion.contact_label == part_in_flight.contact_label
    stored_message = await in_database(Message.objects.get)
    assert stored_message.accepted_at is not None
    assert stored_message.delivered_at is None
    assert [
        packet
        for packet in await in_database(read_packets_labelled_for, bob_device)
        if packet.prepared_at is not None and packet.prepared_at > deletion_committed_at
    ] == []
    await in_database(assert_all_invariants)


def read_packets_prepared_after(moment: datetime) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(prepared_at__gt=moment).order_by("id"))


async def test_deleting_a_user_mid_delivery_and_mid_refresh_stops_every_packet_to_or_about_the_user(
    devices_are_given_up_after_three_rounds: None,
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone", "carol-phone"]
    )
    alice_device, carol_device = devices["alice-phone"], devices["carol-phone"]
    alice = await start_signed_in_client(start_client, alice_device, "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    carol = await start_signed_in_client(start_client, carol_device, "carol")
    browser = await OperatorBrowser.sign_in(panel_operator)
    carol_device.switch_off()
    messages_carol_missed = [alice.send_message("carol", build_single_part_text("alice", number)) for number in (1, 2)]
    for message in messages_carol_missed:
        await message.wait_for_status(OutgoingMessageStatus.SENT)
    await wait_for_database(
        lambda: [delivery.state for delivery in read_deliveries_to(carol_device)] == [MessageDelivery.State.FAILED] * 2,
        description="the relay to give up alice's messages to carol",
    )

    alice_device.switch_off()
    message_to_alice = bob.send_message("alice", "For alice, who is about to be deleted")
    await message_to_alice.wait_for_status(OutgoingMessageStatus.SENT)
    carol_uplink, _carol_downlink = install_triggering_links(carol_device)
    carol_uplink.add_trigger(DirectMessageTrigger(text_prefix="HT1 F alice", action=carol_device.switch_off))
    carol_device.switch_on()
    carol.open_conversation("alice")

    def delivery_and_refresh_are_under_way() -> bool:
        refresh_head = read_delivery("alice", messages_carol_missed[0].message_id, carol_device)
        delivery_to_alice = read_delivery("bob", message_to_alice.message_id, alice_device)
        return (
            refresh_head.state == MessageDelivery.State.PENDING
            and refresh_head.refresh_session_id is not None
            and refresh_head.attempt_count >= 1
            and delivery_to_alice.state == MessageDelivery.State.PENDING
            and delivery_to_alice.attempt_count >= 1
        )

    await wait_for_database(delivery_and_refresh_are_under_way, description="the delivery and the refresh to run")
    [refresh_session] = await in_database(read_refresh_sessions_of, carol_device)
    alice_user = await in_database(read_user, "alice")
    assert alice_user is not None
    delete_response = await browser.post_form(f"/users/{alice_user.pk}/delete", {"typed_username": "alice"})
    deletion_committed_at = timezone.now()
    await wait_until(
        lambda: fake_companion_firmware.find_contact(alice_device.public_key) is None,
        description="alice's device to be removed from the node",
    )
    removed_at = find_removal_time(fake_companion_firmware, alice_device)
    packets_while_carol_is_away = await in_database(read_packets_prepared_after, deletion_committed_at)
    carol_device.switch_on()
    carol_switched_on_at = timezone.now()
    await carol.wait_until(
        lambda: "HT1 e NO_SUCH_USER F alice" in carol.received_texts("e"),
        timeout_seconds=DELIVERY_TIMEOUT_SECONDS,
        description="carol's retried refresh to learn that alice is gone",
    )

    assert delete_response.status_code == HTTPStatus.FOUND
    assert refresh_session.state == RefreshSession.State.ACTIVE
    assert await in_database(read_user, "alice") is None
    assert await in_database(read_contact_of, alice_device) is None
    assert await in_database(Message.objects.count) == 0
    assert await in_database(RefreshSession.objects.filter(id=refresh_session.pk).exists) is False
    assert await in_database(read_deliveries_to, carol_device) == []
    assert await in_database(find_packets_awaiting_acknowledgement_at, removed_at) == []
    assert packets_while_carol_is_away == []
    for packet in await in_database(read_packets_prepared_after, carol_switched_on_at):
        assert packet.text == "HT1 e NO_SUCH_USER F alice", packet
        assert carol_device.public_key.hex()[:12] in packet.contact_label, packet
    assert not any(incoming.sender_username == "alice" for incoming in carol.displayed_messages)
    await in_database(assert_all_invariants)


async def test_deleting_a_device_while_it_sends_and_while_parts_to_it_go_out_neither_deadlocks_nor_restarts_the_worker(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    make_relay_node_suggest_radio_like_acknowledgement_waits(fake_companion_firmware)
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    bob_device = devices["bob-phone"]
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    browser = await OperatorBrowser.sign_in(panel_operator)
    bob_contact = await in_database(read_contact_of, bob_device)
    assert bob_contact is not None
    generation_before = relay_worker.runtime_status.connection_generation

    messages_to_bob = [alice.send_message("bob", TEN_PART_TEXT) for _ in range(2)]
    await wait_until_the_relay_accepted(messages_to_bob[0])
    messages_from_bob = [bob.send_message("alice", TEN_PART_TEXT) for _ in range(3)]

    def bob_sends_while_parts_to_bob_go_out() -> bool:
        bob_rows = read_inbox_rows_from(bob_device)
        return any(row.text.startswith("HT1 M ") for row in bob_rows) and any(
            packet.purpose == OutboundPacket.Purpose.DELIVERY for packet in read_packets_labelled_for(bob_device)
        )

    await wait_for_database(bob_sends_while_parts_to_bob_go_out, description="traffic in both directions")
    delete_response = await browser.post_form(f"/contacts/{bob_contact.pk}/delete")
    deletion_committed_at = timezone.now()
    await wait_until(
        lambda: fake_companion_firmware.find_contact(bob_device.public_key) is None,
        timeout_seconds=DELIVERY_TIMEOUT_SECONDS,
        description="the deleted device to be removed from the node",
    )
    removal_seen_at = time.monotonic()
    removed_at = find_removal_time(fake_companion_firmware, bob_device)
    direct_messages_bob_sent_before_the_removal = len(bob.sent_direct_messages)
    await bob.wait_until(
        lambda: len(bob.sent_direct_messages) >= direct_messages_bob_sent_before_the_removal + 3,
        timeout_seconds=DELIVERY_TIMEOUT_SECONDS,
        description="bob's app to keep sending its unfinished upload",
    )
    await messages_to_bob[1].wait_for_status(OutgoingMessageStatus.SENT)
    rows_from_bob = await in_database(read_inbox_rows_from, bob_device)
    timestamps_bob_sent_after_the_removal = {
        sent_direct_message.meshcore_timestamp
        for sent_direct_message in bob.sent_direct_messages
        if sent_direct_message.handed_to_node_at > removal_seen_at
    }

    assert delete_response.status_code == HTTPStatus.FOUND
    assert bob.is_signed_in
    assert messages_from_bob[-1].status is OutgoingMessageStatus.PENDING
    assert await in_database(find_packets_awaiting_acknowledgement_at, removed_at) == []
    assert [
        packet
        for packet in await in_database(read_packets_labelled_for, bob_device)
        if packet.prepared_at is not None and packet.prepared_at > deletion_committed_at
    ] == []
    assert timestamps_bob_sent_after_the_removal
    assert not timestamps_bob_sent_after_the_removal & {row.sender_timestamp for row in rows_from_bob}
    unknown_sender_rows = [
        row for row in rows_from_bob if row.classification == InboundDirectMessage.Classification.UNKNOWN_SENDER
    ]
    assert all(row.contact_id is None for row in unknown_sender_rows)
    assert all(row.reply_summary == "" for row in unknown_sender_rows)
    assert relay_worker.runtime_status.connection_generation == generation_before
    assert relay_worker.runtime_status.relay_mode == RelayMode.RUNNING
    task_failures = [record for record in caplog.records if record.name == "worker.task_supervision"]
    assert task_failures == []
    assert "Internal error" not in relay_worker.runtime_status.last_error_message
    assert all(
        message.accepted_at is not None
        for message in await in_database(lambda: list(Message.objects.filter(recipient__username_lookup="bob")))
    )
    assert await in_database(read_deliveries_to, bob_device) == []
    await in_database(assert_all_invariants)
