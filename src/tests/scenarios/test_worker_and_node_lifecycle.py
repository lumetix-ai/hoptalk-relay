"""The worker dies and restarts, the node reboots or is reset, the operator sets a node up: and nothing is lost."""

import asyncio
import time
from html import escape
from http import HTTPStatus

import pytest
from django.utils import timezone

from directory.models import Contact
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket
from node.contact_cards import parse_contact_card_uri
from node.models import NodeCommand, NodeSetupRun, WorkerStatus
from node.node_settings import load_node_configuration
from node.setup_runs import CONFIGURE_NODE_STEP_LABELS, ConfigureNodeStep, FactoryResetStep
from node.worker_status import BannerKind, collect_banners, read_worker_status
from tests.invariants import assert_all_invariants
from tests.panel_operator import PanelOperator
from tests.scenarios.accounts_and_messages_helpers import count_processed_inbox_rows_from
from tests.scenarios.refresh_lifecycle_admin_helpers import (
    DirectMessageTrigger,
    OperatorBrowser,
    install_triggering_links,
    kill_relay_worker,
    make_relay_node_suggest_radio_like_acknowledgement_waits,
    poll_partial_until_it_stops,
    put_node_port_gate_before_worker,
    read_delivery,
    read_delivery_packets,
    read_inbox_rows_from,
    run_setup_wizard,
    start_new_relay_worker_process,
    start_relay_with_devices_from_cards,
    start_signed_in_client,
)
from tests.scenarios.scenario_setup import ClientStarter, every_contact_is_on_node, sign_in
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    in_database,
    wait_for_database,
)
from tests.worker.simulated_hoptalk_client_records import OutgoingMessageStatus
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

# 238 bytes: more than two parts of 104 bytes hold.
THREE_PART_TEXT = "Three parts. " + "abcdefgh " * 25
DELIVERY_TIMEOUT_SECONDS = 10.0
NODE_CLOCK_BEHIND_AFTER_BOOT_SECONDS = 3600
# Farther apart than this, the handshake sets the node's clock.
NODE_CLOCK_TOLERANCE_SECONDS = 5


def count_node_commands(firmware: FakeCompanionFirmware, command_code: CommandCode) -> int:
    return sum(1 for received_command in firmware.command_log if received_command.code == command_code)


def every_packet_to_the_device_is_acknowledged(device: SimulatedDevice) -> bool:
    return not OutboundPacket.objects.filter(
        contact__public_key=device.public_key.hex(), acknowledged_at__isnull=True
    ).exists()


def node_commands_since(firmware: FakeCompanionFirmware, command_index: int) -> list[CommandCode]:
    return [CommandCode(received_command.code) for received_command in firmware.command_log[command_index:]]


async def test_a_part_whose_send_the_killed_worker_never_recorded_goes_again_with_a_new_timestamp_in_the_same_round(
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
    await wait_for_database(
        lambda: every_packet_to_the_device_is_acknowledged(bob_device),
        description="the firmware ACKs of the replies to bob's sign-in",
    )
    bob_device.switch_off()
    message = alice.send_message("bob", "Sent while the relay restarts")
    await message.wait_for_status(OutgoingMessageStatus.SENT)

    def first_round_is_complete() -> bool:
        delivery = read_delivery("alice", message.message_id, bob_device)
        return delivery.attempt_count == 1 and delivery.round_pending_parts_mask == 0

    await wait_for_database(first_round_is_complete, description="the first round to the switched-off device")
    sends_before = count_node_commands(fake_companion_firmware, CommandCode.SEND_TEXT_MESSAGE)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.SEND_TEXT_MESSAGE)
    await wait_until(
        lambda: count_node_commands(fake_companion_firmware, CommandCode.SEND_TEXT_MESSAGE) > sends_before,
        description="the second round's part to reach the node",
    )
    await kill_relay_worker(relay_worker)

    last_send_command = [
        received_command
        for received_command in fake_companion_firmware.command_log
        if received_command.code == CommandCode.SEND_TEXT_MESSAGE
    ][-1]
    assert f"HT1 m alice {message.message_id} ".encode() in last_send_command.frame
    delivery_at_the_kill = await in_database(read_delivery, "alice", message.message_id, bob_device)
    [unrecorded_packet] = [
        packet
        for packet in await in_database(read_delivery_packets, delivery_at_the_kill.pk)
        if packet.state == OutboundPacket.State.PREPARED
    ]
    assert delivery_at_the_kill.attempt_count == 2
    assert unrecorded_packet.attempt_number == 2
    bob_device.switch_on()
    start_new_relay_worker_process(relay_worker)
    [received_message] = await bob.wait_for_received_messages("alice", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert received_message.text == message.text
    assert len(bob.displayed_messages) == 1
    assert await in_database(Message.objects.count) == 1
    delivered = await in_database(read_delivery, "alice", message.message_id, bob_device)
    assert delivered.state == MessageDelivery.State.DELIVERED
    assert delivered.attempt_count == 2
    assert delivered.arm_generation == delivery_at_the_kill.arm_generation
    packets = await in_database(read_delivery_packets, delivered.pk)
    recovered_packet = next(packet for packet in packets if packet.pk == unrecorded_packet.pk)
    assert recovered_packet.state == OutboundPacket.State.OUTCOME_UNKNOWN
    [resent_packet] = [packet for packet in packets if packet.pk > unrecorded_packet.pk]
    assert resent_packet.text == unrecorded_packet.text
    assert resent_packet.part_number == unrecorded_packet.part_number
    assert resent_packet.attempt_number == unrecorded_packet.attempt_number == 2
    assert resent_packet.arm_generation == unrecorded_packet.arm_generation
    assert resent_packet.sender_timestamp > unrecorded_packet.sender_timestamp
    assert max(packet.attempt_number or 0 for packet in packets) == 2
    await in_database(assert_all_invariants)


async def test_a_request_recorded_but_not_processed_when_the_worker_died_is_processed_after_the_restart(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    killed_processor = relay_worker.worker.inbound_processor
    process_row_until_the_process_dies = killed_processor.process_row
    held_row_ids: list[int] = []

    async def process_rows_but_die_before_the_first_message_part(inbox_row_id: int) -> None:
        inbox_row = await in_database(InboundDirectMessage.objects.get, id=inbox_row_id)
        if inbox_row.text.startswith("HT1 M ") and not held_row_ids:
            held_row_ids.append(inbox_row_id)
            await asyncio.Event().wait()
        await process_row_until_the_process_dies(inbox_row_id)

    monkeypatch.setattr(killed_processor, "process_row", process_rows_but_die_before_the_first_message_part)
    message = alice.send_message("bob", "Recorded just before the relay died")
    await wait_until(lambda: bool(held_row_ids), description="the message part to be recorded")
    await kill_relay_worker(relay_worker)

    [held_row_id] = held_row_ids
    row_at_the_kill = await in_database(InboundDirectMessage.objects.get, id=held_row_id)
    assert row_at_the_kill.processing_state == InboundDirectMessage.ProcessingState.RECEIVED
    assert await in_database(Message.objects.count) == 0
    start_new_relay_worker_process(relay_worker)
    [received_message] = await bob.wait_for_received_messages("alice", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    # Alice confirms the delivered receipt as soon as it arrives, so the relay may not have processed that "C" yet.
    await wait_for_database(
        lambda: count_processed_inbox_rows_from(devices["alice-phone"], "HT1 C ") >= 1,
        description="alice's confirmation of the delivered receipt to be processed",
    )

    assert received_message.text == message.text
    processed_row = await in_database(InboundDirectMessage.objects.get, id=held_row_id)
    assert processed_row.processing_state == InboundDirectMessage.ProcessingState.PROCESSED
    assert processed_row.classification == InboundDirectMessage.Classification.REQUEST
    stored_message = await in_database(Message.objects.get)
    assert stored_message.text == message.text
    assert stored_message.created_at == processed_row.processed_at
    alice_rows = await in_database(read_inbox_rows_from, devices["alice-phone"])
    later_part_rows = [row for row in alice_rows if row.text.startswith("HT1 M ") and row.pk != held_row_id]
    assert all(row.pk > held_row_id for row in later_part_rows)
    assert all(row.processing_state == InboundDirectMessage.ProcessingState.PROCESSED for row in alice_rows)
    await in_database(assert_all_invariants)


def read_packets_dropped_with_the_link() -> list[OutboundPacket]:
    return list(
        OutboundPacket.objects.filter(route_reset_state=OutboundPacket.RouteResetState.DROPPED_BY_RESTART).order_by(
            "id"
        )
    )


async def test_after_a_node_reboot_the_worker_reconnects_restores_the_node_drains_it_and_delivers_on(
    relay_worker: RelayWorkerHarness,
    fake_node_connector: FakeNodeConnector,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    node_port_gate = put_node_port_gate_before_worker(relay_worker, fake_node_connector)
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    alice_device, bob_device = devices["alice-phone"], devices["bob-phone"]
    alice = await start_signed_in_client(start_client, alice_device, "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    # A part to the switched-off device then awaits its firmware ACK, which never comes, for most of a second.
    make_relay_node_suggest_radio_like_acknowledgement_waits(fake_companion_firmware)
    # The event loop keeps only a weak reference to a task.
    reboot_tasks: list[asyncio.Task[None]] = []

    def is_a_part_awaiting_its_firmware_ack() -> bool:
        return any(
            awaited_packet.purpose == OutboundPacket.Purpose.DELIVERY
            for awaited_packet in relay_worker.worker.acknowledgement_tracker.packets_awaiting_acknowledgement
        )

    async def reboot_the_node_while_its_port_is_gone() -> None:
        # The relay may send alice's complete status after the first part to bob. Lost with a reboot before it
        # left, alice would wait for it while the node's port stays gone.
        await message_to_bob.wait_for_status(OutgoingMessageStatus.SENT)
        await wait_until(is_a_part_awaiting_its_firmware_ack, description="a part to bob to await its firmware ACK")
        node_port_gate.close()
        fake_companion_firmware.reboot()

    def reboot_once_the_part_awaits_its_firmware_ack() -> None:
        reboot_tasks.append(asyncio.create_task(reboot_the_node_while_its_port_is_gone()))

    _bob_uplink, bob_downlink = install_triggering_links(bob_device)
    bob_downlink.add_trigger(
        DirectMessageTrigger(text_prefix="HT1 m alice ", action=reboot_once_the_part_awaits_its_firmware_ack)
    )
    bob_device.switch_off()
    message_to_bob = alice.send_message("bob", THREE_PART_TEXT)
    await message_to_bob.wait_for_status(OutgoingMessageStatus.SENT)
    await relay_worker.wait_for_relay_mode(RelayMode.DISCONNECTED)
    await wait_until(lambda: fake_companion_firmware.is_running, description="the node to boot again")
    assert fake_companion_firmware.app_target_version == 0
    # Without a real-time clock the node restarts from the newest contact time in its flash, which
    # on a relay that has run for a while lags the real time by the lazy contact writes.
    fake_companion_firmware.force_clock_time(int(time.time()) - NODE_CLOCK_BEHIND_AFTER_BOOT_SECONDS)

    bob_device.switch_on()
    sent_before_outage_message = len(bob.sent_direct_messages)
    message_to_alice = bob.send_message("alice", "Sent while the relay's node was rebooting")
    await wait_until(
        lambda: fake_companion_firmware.offline_queue_length >= 1, description="the node to queue bob's message"
    )
    timestamps_queued_while_disconnected = {
        sent.meshcore_timestamp for sent in bob.sent_direct_messages[sent_before_outage_message:]
    }
    command_count_before_reconnect = len(fake_companion_firmware.command_log)
    node_port_gate.open()
    await relay_worker.wait_for_connection_generation(2)
    reconnected_at = timezone.now()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await bob.wait_for_received_messages("alice", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await alice.wait_for_received_messages("bob", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await message_to_bob.wait_for_status(OutgoingMessageStatus.DELIVERED)
    await message_to_alice.wait_for_status(OutgoingMessageStatus.DELIVERED)

    reconnect_commands = node_commands_since(fake_companion_firmware, command_count_before_reconnect)
    assert reconnect_commands[:2] == [CommandCode.APP_START, CommandCode.DEVICE_QUERY]
    assert CommandCode.SET_DEVICE_TIME in reconnect_commands
    assert CommandCode.SYNC_NEXT_MESSAGE in reconnect_commands
    assert fake_companion_firmware.app_target_version >= 3
    assert abs(fake_companion_firmware.clock_time() - time.time()) <= NODE_CLOCK_TOLERANCE_SECONDS
    bob_rows = await in_database(read_inbox_rows_from, bob_device)
    drained_rows = [row for row in bob_rows if row.sender_timestamp in timestamps_queued_while_disconnected]
    assert {row.sender_timestamp for row in drained_rows} == timestamps_queued_while_disconnected
    rows_received_in_version_3_frames = [
        row
        for row in bob_rows
        if row.received_at > reconnected_at and row.sender_timestamp not in timestamps_queued_while_disconnected
    ]
    assert rows_received_in_version_3_frames, "bob's acknowledgement of alice's message"
    assert all(row.signal_to_noise_ratio is not None for row in rows_received_in_version_3_frames)
    dropped_packets = await in_database(read_packets_dropped_with_the_link)
    assert dropped_packets, "a part awaiting its firmware ACK when the node rebooted"
    assert all(packet.state == OutboundPacket.State.ACKNOWLEDGEMENT_TIMED_OUT for packet in dropped_packets)
    assert all(packet.connection_generation == 1 for packet in dropped_packets)
    assert bob.received_messages("alice")[0].text == THREE_PART_TEXT
    delivery_to_bob = await in_database(read_delivery, "alice", message_to_bob.message_id, bob_device)
    assert delivery_to_bob.attempt_count >= 2
    await in_database(assert_all_invariants)


def read_node_command_kinds_and_states() -> list[tuple[str, str]]:
    return list(NodeCommand.objects.order_by("id").values_list("kind", "state"))


def read_contact_sync_states() -> dict[str, str]:
    return dict(Contact.objects.values_list("name", "node_sync_state"))


def count_outbound_packets() -> int:
    return OutboundPacket.objects.count()


def read_latest_command(kind: NodeCommand.Kind) -> NodeCommand:
    return NodeCommand.objects.filter(kind=kind).latest("id")


async def test_a_node_that_reset_itself_stops_the_relay_untouched_until_a_setup_run_recovers_it(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    alice_device, bob_device = devices["alice-phone"], devices["bob-phone"]
    alice = await start_signed_in_client(start_client, alice_device, "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    configured_public_key = fake_companion_firmware.public_key.hex()
    configured_name = fake_companion_firmware.preferences.node_name.decode()
    bob_device.switch_off()
    waiting_message = alice.send_message("bob", "Waiting for the relay to recover")
    await waiting_message.wait_for_status(OutgoingMessageStatus.SENT)

    fake_companion_firmware.factory_reset()
    await relay_worker.wait_for_relay_mode(RelayMode.IDENTITY_MISMATCH)
    first_command_in_mismatch = len(fake_companion_firmware.command_log)
    packet_count_in_mismatch = await in_database(count_outbound_packets)
    attached_public_key = fake_companion_firmware.public_key.hex()
    attached_name = fake_companion_firmware.preferences.node_name.decode()
    stranger = simulated_mesh.add_device("stranger", relay_knows_device=False)
    stranger.send_advert()
    await wait_until(
        lambda: fake_companion_firmware.find_contact(stranger.public_key) is not None,
        description="the reset node to add the stranger by itself",
    )
    stranger.send_direct_message("hello, whoever you are")
    await wait_until(
        lambda: fake_companion_firmware.offline_queue_length == 1, description="the node to queue the stranger's DM"
    )
    browser = await OperatorBrowser.sign_in(panel_operator)
    carol_device = simulated_mesh.add_device("carol-phone", relay_knows_device=False)
    add_response = await browser.post_form("/contacts/card/add", {"card_uri": carol_device.contact_card_uri()})
    sync_response = await browser.post_form("/node/actions/sync-contacts")
    await wait_for_database(
        lambda: read_latest_command(NodeCommand.Kind.RECONCILE_CONTACTS).state in NodeCommand.TERMINAL_STATES,
        description="the operator's contact sync to finish",
    )
    await wait_for_database(
        lambda: read_worker_status_mode() == WorkerStatus.RelayMode.IDENTITY_MISMATCH,
        description="the worker status to show the mismatch",
    )
    worker_status_in_mismatch = await in_database(read_worker_status)
    banners = await in_database(collect_banners, timezone.now())
    banner_partial = (await browser.get_partial("/partials/banners")).content.decode()

    assert add_response.status_code == sync_response.status_code == HTTPStatus.FOUND
    assert attached_public_key != configured_public_key
    assert worker_status_in_mismatch is not None
    assert worker_status_in_mismatch.connection_state == WorkerStatus.ConnectionState.CONNECTED
    assert (worker_status_in_mismatch.node_public_key, worker_status_in_mismatch.node_name) == (
        attached_public_key,
        attached_name,
    )
    [mismatch_banner] = [banner for banner in banners if banner.kind == BannerKind.IDENTITY_MISMATCH]
    for expected_detail in (attached_public_key, attached_name, configured_public_key, configured_name):
        assert expected_detail in mismatch_banner.detail
        assert expected_detail in banner_partial
    sync_command = await in_database(read_latest_command, NodeCommand.Kind.RECONCILE_CONTACTS)
    assert sync_command.state == NodeCommand.State.FAILED
    commands_in_mismatch = node_commands_since(fake_companion_firmware, first_command_in_mismatch)
    for command_that_touches_traffic_or_contacts in (
        CommandCode.SEND_TEXT_MESSAGE,
        CommandCode.SYNC_NEXT_MESSAGE,
        CommandCode.ADD_UPDATE_CONTACT,
        CommandCode.REMOVE_CONTACT,
    ):
        assert command_that_touches_traffic_or_contacts not in commands_in_mismatch
    assert fake_companion_firmware.find_contact(stranger.public_key) is not None
    assert fake_companion_firmware.offline_queue_length == 1
    assert await in_database(count_outbound_packets) == packet_count_in_mismatch
    assert await in_database(read_contact_sync_states) == {
        "alice-phone": Contact.NodeSyncState.ON_NODE,
        "bob-phone": Contact.NodeSyncState.ON_NODE,
        "carol-phone": Contact.NodeSyncState.PENDING_ADD,
    }

    done_page = (await browser.get_page("/setup")).content.decode()
    assert "Set up the attached node" in done_page
    await run_setup_wizard(browser, attached_name)
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(every_contact_is_on_node, description="every contact to be added to the set-up node")
    await wait_until(
        lambda: fake_companion_firmware.find_contact(stranger.public_key) is None,
        description="the stranger to be removed from the set-up node",
    )
    recovered_public_key = fake_companion_firmware.public_key.hex()
    for client in (alice, bob):
        client.pin_server(fake_companion_firmware.public_key)
    bob_device.switch_on()
    [received_message] = await bob.wait_for_received_messages("alice", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await waiting_message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    reply = bob.send_message("alice", "Back in touch")
    await alice.wait_for_received_messages("bob", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await reply.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert received_message.text == waiting_message.text
    assert recovered_public_key not in (configured_public_key, attached_public_key)
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == recovered_public_key
    [setup_run] = await in_database(lambda: list(NodeSetupRun.objects.all()))
    assert setup_run.purpose == NodeSetupRun.Purpose.RECONFIGURE
    assert setup_run.state == NodeSetupRun.State.COMPLETED
    assert setup_run.original_public_key == attached_public_key
    assert setup_run.new_public_key == recovered_public_key
    banners_after_recovery = await in_database(collect_banners, timezone.now())
    assert [banner.kind for banner in banners_after_recovery] == []
    await in_database(assert_all_invariants)


def read_worker_status_mode() -> str | None:
    worker_status = WorkerStatus.objects.filter(id=WorkerStatus.SINGLE_ROW_ID).first()
    return worker_status.relay_mode if worker_status is not None else None


def progress_step_states(node_command: NodeCommand) -> dict[str, str]:
    return {step["step"]: step["state"] for step in node_command.progress}


async def test_the_setup_wizard_takes_a_fresh_node_through_reset_and_configuration_to_relaying(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    original_public_key = fake_companion_firmware.public_key.hex()
    original_name = fake_companion_firmware.preferences.node_name.decode()
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    browser = await OperatorBrowser.sign_in(panel_operator)
    root_response = await browser.get_page("/")
    start_page = (await browser.get_page("/setup")).content.decode()

    await run_setup_wizard(browser, original_name)
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    done_page = (await browser.get_page("/setup")).content.decode()
    node_configuration = await in_database(load_node_configuration)

    assert root_response.status_code == HTTPStatus.FOUND
    assert root_response["Location"] == "/setup"
    assert "Start setup" in start_page
    assert node_configuration is not None
    new_public_key = fake_companion_firmware.public_key.hex()
    assert new_public_key != original_public_key
    assert node_configuration.node_public_key == new_public_key
    assert node_configuration.node_name == "HopTalk Relay"
    assert node_configuration.radio_preset_title == "Australia (Narrow)"
    assert (node_configuration.radio_frequency_kilohertz, node_configuration.radio_bandwidth_hertz) == (916575, 62500)
    assert node_configuration.routing_path_hash_size == 2
    assert node_configuration.messaging_multi_acks == 2
    assert node_configuration.contacts_manual_add
    assert node_configuration.contacts_auto_add_configuration == 0
    assert parse_contact_card_uri(node_configuration.node_contact_card_uri).public_key == new_public_key
    assert "The node is configured" in done_page
    assert node_configuration.node_contact_card_uri in done_page
    preferences = fake_companion_firmware.preferences
    assert preferences.node_name == b"HopTalk Relay"
    assert (preferences.frequency_kilohertz, preferences.bandwidth_hertz) == (916575, 62500)
    assert (preferences.spreading_factor, preferences.coding_rate, preferences.transmit_power_dbm) == (7, 7, 20)
    assert preferences.path_hash_mode == 1
    assert (preferences.manual_add_contacts, preferences.multi_acknowledgements) == (1, 2)
    assert (preferences.auto_add_configuration, preferences.auto_add_maximum_hops) == (0, 0)
    [setup_run] = await in_database(lambda: list(NodeSetupRun.objects.all()))
    assert setup_run.purpose == NodeSetupRun.Purpose.INITIAL
    assert setup_run.state == NodeSetupRun.State.COMPLETED
    assert (setup_run.original_public_key, setup_run.new_public_key) == (original_public_key, new_public_key)
    # The run completes when the configuration is saved; the command still resumes relaying after that.
    await wait_for_database(
        lambda: read_latest_command(NodeCommand.Kind.CONFIGURE_NODE).state in NodeCommand.TERMINAL_STATES,
        description="the configuration command to finish resuming the relay",
    )
    assert await in_database(read_node_command_kinds_and_states) == [
        (NodeCommand.Kind.READ_NODE_INFORMATION, NodeCommand.State.SUCCEEDED),
        (NodeCommand.Kind.FACTORY_RESET, NodeCommand.State.SUCCEEDED),
        (NodeCommand.Kind.CONFIGURE_NODE, NodeCommand.State.SUCCEEDED),
    ]
    factory_reset_command = await in_database(read_latest_command, NodeCommand.Kind.FACTORY_RESET)
    assert list(progress_step_states(factory_reset_command)) == [step.value for step in FactoryResetStep]
    assert progress_step_states(factory_reset_command)[FactoryResetStep.SEND_RESET_FRAME] == "done"
    configure_command = await in_database(read_latest_command, NodeCommand.Kind.CONFIGURE_NODE)
    assert list(progress_step_states(configure_command)) == [step.value for step in ConfigureNodeStep]
    assert set(progress_step_states(configure_command).values()) <= {"done", "skipped"}
    progress_partial = await poll_partial_until_it_stops(browser, f"/commands/{configure_command.pk}/partials/progress")
    for step_label in CONFIGURE_NODE_STEP_LABELS.values():
        assert escape(step_label) in progress_partial.content.decode()

    alice_device = simulated_mesh.add_device("alice-phone", relay_knows_device=False)
    bob_device = simulated_mesh.add_device("bob-phone", relay_knows_device=False)
    for device in (alice_device, bob_device):
        add_response = await browser.post_form("/contacts/card/add", {"card_uri": device.contact_card_uri()})
        assert add_response.status_code == HTTPStatus.FOUND
    await wait_for_database(every_contact_is_on_node, description="both devices to be put on the set-up node")
    alice = await start_signed_in_client(start_client, alice_device, "alice")
    bob = await start_signed_in_client(start_client, bob_device, "bob")
    message = alice.send_message("bob", "The first message through the new relay")
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    assert received_message.text == message.text
    assert fake_companion_firmware.protocol_violations == []
    await in_database(assert_all_invariants)


async def test_a_reconfiguration_that_keeps_the_identity_lets_every_user_carry_on_without_a_new_card(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
    panel_operator: PanelOperator,
) -> None:
    devices = await start_relay_with_devices_from_cards(
        relay_worker, fake_companion_firmware, simulated_mesh, ["alice-phone", "bob-phone"]
    )
    alice = await start_signed_in_client(start_client, devices["alice-phone"], "alice")
    bob = await start_signed_in_client(start_client, devices["bob-phone"], "bob")
    message_before = alice.send_message("bob", "Before the reconfiguration")
    await bob.wait_for_received_messages("alice", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await message_before.wait_for_status(OutgoingMessageStatus.DELIVERED)
    relay_public_key = fake_companion_firmware.public_key.hex()
    browser = await OperatorBrowser.sign_in(panel_operator)
    dashboard = (await browser.get_page("/node")).content.decode()

    await run_setup_wizard(browser, fake_companion_firmware.preferences.node_name.decode(), keep_identity=True)
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(every_contact_is_on_node, description="every contact to be added to the node again")
    done_page = (await browser.get_page("/setup")).content.decode().replace("&#x27;", "'")
    for client, username in ((alice, "alice"), (bob, "bob")):
        await sign_in(client, username)
    message_after = bob.send_message("alice", "After the reconfiguration")
    await alice.wait_for_received_messages("bob", 1, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await message_after.wait_for_status(OutgoingMessageStatus.DELIVERED)
    reply = alice.send_message("bob", "Still the same relay")
    await bob.wait_for_received_messages("alice", 2, timeout_seconds=DELIVERY_TIMEOUT_SECONDS)
    await reply.wait_for_status(OutgoingMessageStatus.DELIVERED)

    assert "Identity backup" in dashboard
    assert "Stored" in dashboard
    assert "The relay kept its identity; users need to do nothing." in done_page
    assert fake_companion_firmware.public_key.hex() == relay_public_key
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == relay_public_key
    assert parse_contact_card_uri(node_configuration.node_contact_card_uri).public_key == relay_public_key
    [setup_run] = await in_database(lambda: list(NodeSetupRun.objects.all()))
    assert setup_run.purpose == NodeSetupRun.Purpose.RECONFIGURE
    assert setup_run.state == NodeSetupRun.State.COMPLETED
    assert setup_run.original_public_key == setup_run.restored_public_key == relay_public_key
    assert setup_run.new_public_key != relay_public_key
    assert [received.text for received in alice.received_messages("bob")] == ["After the reconfiguration"]
    assert [received.text for received in bob.received_messages("alice")] == [
        "Before the reconfiguration",
        "Still the same relay",
    ]
    await in_database(assert_all_invariants)
