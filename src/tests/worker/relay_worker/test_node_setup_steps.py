"""The setup wizard's node work against the fake node, driven through the panel's own setup services."""

import asyncio
import logging
from dataclasses import replace
from typing import Any

import pytest
from django.core import serializers
from django.utils import timezone
from pytest_django import Settings

from directory.models import Contact
from messaging.models import InboundDirectMessage, OutboundPacket
from node.contact_cards import parse_contact_card_uri
from node.models import NodeCommand, NodeSetting, NodeSetupRun, WorkerStatus
from node.node_identity_backups import (
    MISMATCHED_KEY_PAIR_REASON,
    NodeIdentityBackupStatus,
    NodePrivateKey,
    load_private_key_for_restore,
    read_configured_node_identity_backup,
    read_node_identity_backup_state,
    write_node_identity_backup,
)
from node.node_settings import NodeConfiguration, OptionalNodeSettingKey, load_node_configuration
from node.setup_runs import (
    ConfigureNodeStep,
    FactoryResetStep,
    RequestedNodeConfiguration,
    cancel_setup_run,
    confirm_factory_reset,
    find_expected_factory_reset_confirmation,
    start_setup_run,
    submit_node_configuration,
)
from tests.invariants import assert_all_invariants
from tests.node_key_pairs import encrypt_a_foreign_private_key, generate_node_key_pair
from tests.private_key_checks import mentions_private_key
from tests.worker.fake_node.fake_companion_firmware import FactoryResetBehaviour, FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.frames import PUBLIC_CHANNEL_SECRET, CommandCode
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    FAST_WORKER_TIMING,
    DeviceInbox,
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    create_packet_awaiting_acknowledgement_until,
    in_database,
    read_contact,
    wait_for_database,
)
from worker.command_progress import NodeCommandFailedError
from worker.node_setup_steps import (
    IDENTITY_UNCHANGED_ERROR,
    NODE_DID_NOT_RETURN_AFTER_RESET_ERROR,
    NODE_IGNORED_RESET_ERROR,
    NODE_RECONNECTED_BEFORE_RESET_ERROR,
    NodeSetupSteps,
)
from worker.private_key_log_guard import LIBRARY_LOGGER_NAME
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

REQUESTED_CONFIGURATION = RequestedNodeConfiguration(
    node_name="HopTalk Relay",
    radio_preset_title="Australia (Narrow)",
    radio_frequency_kilohertz=916575,
    radio_bandwidth_hertz=62500,
    radio_spreading_factor=7,
    radio_coding_rate=7,
    path_hash_size=2,
    transmit_power_dbm=20,
    replace_public_channel=False,
)


def read_setup_run(setup_run_id: int) -> NodeSetupRun:
    return NodeSetupRun.objects.get(id=setup_run_id)


def read_latest_command_of(setup_run_id: int) -> NodeCommand:
    return NodeCommand.objects.filter(setup_run_id=setup_run_id).latest("id")


async def wait_for_run_state(setup_run_id: int, state: NodeSetupRun.State, *, timeout_seconds: float = 5.0) -> None:
    await wait_for_database(
        lambda: read_setup_run(setup_run_id).state == state,
        timeout_seconds=timeout_seconds,
        description=f"setup run {setup_run_id} to reach {state}",
    )


async def start_and_read_the_node(relay_worker: RelayWorkerHarness) -> NodeSetupRun:
    setup_run = await in_database(start_setup_run, timezone.now())
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION)
    return await in_database(read_setup_run, setup_run.pk)


async def confirm_the_reset(setup_run: NodeSetupRun) -> NodeCommand:
    typed_confirmation = await in_database(find_expected_factory_reset_confirmation, setup_run)
    return await in_database(confirm_factory_reset, setup_run.pk, typed_confirmation, timezone.now())


async def run_the_reset(relay_worker: RelayWorkerHarness) -> NodeSetupRun:
    setup_run = await start_and_read_the_node(relay_worker)
    await confirm_the_reset(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    return await in_database(read_setup_run, setup_run.pk)


async def submit_the_configuration(
    setup_run: NodeSetupRun, requested_configuration: RequestedNodeConfiguration = REQUESTED_CONFIGURATION
) -> None:
    await in_database(submit_node_configuration, setup_run.pk, requested_configuration, timezone.now())


def progress_states(node_command: NodeCommand) -> dict[str, str]:
    return {step["step"]: step["state"] for step in node_command.progress}


def read_step_state(node_command_id: int, step: str) -> str | None:
    return progress_states(NodeCommand.objects.get(id=node_command_id)).get(step)


async def test_a_reconfiguration_resets_and_configures_the_node_and_moves_every_contact_to_the_new_identity(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    contact = await in_database(create_contact_for_device, device)
    original_public_key = fake_companion_firmware.public_key.hex()
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    setup_run = await run_the_reset(relay_worker)
    new_public_key = fake_companion_firmware.public_key.hex()
    assert setup_run.new_public_key == new_public_key != original_public_key
    assert fake_companion_firmware.find_contact(device.public_key) is None
    assert (await in_database(read_contact, contact.pk)).node_sync_state == Contact.NodeSyncState.PENDING_ADD
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)

    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == new_public_key
    assert node_configuration.node_name == "HopTalk Relay"
    assert node_configuration.messaging_multi_acks == 2
    assert node_configuration.contacts_manual_add
    assert node_configuration.contacts_auto_add_configuration == 0
    preferences = fake_companion_firmware.preferences
    assert preferences.node_name == b"HopTalk Relay"
    assert (preferences.frequency_kilohertz, preferences.bandwidth_hertz) == (916575, 62500)
    assert (preferences.spreading_factor, preferences.coding_rate, preferences.transmit_power_dbm) == (7, 7, 20)
    assert preferences.path_hash_mode == 1
    assert (preferences.manual_add_contacts, preferences.multi_acknowledgements) == (1, 2)
    assert (preferences.auto_add_configuration, preferences.auto_add_maximum_hops) == (0, 0)
    channel_zero = fake_companion_firmware.channel(0)
    assert channel_zero is not None
    assert channel_zero.secret == PUBLIC_CHANNEL_SECRET
    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact added to the new identity",
    )
    assert fake_companion_firmware.find_contact(device.public_key) is not None
    await wait_for_database(
        lambda: read_latest_command_of(setup_run.pk).state == NodeCommand.State.SUCCEEDED,
        description="the configuration command to finish, relaying resumed",
    )
    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    assert set(progress_states(configure_command).values()) <= {"done", "skipped"}
    assert list(progress_states(configure_command)) == [step.value for step in ConfigureNodeStep]
    assert fake_companion_firmware.protocol_violations == []


async def test_the_public_channel_is_replaced_only_when_the_operator_asked(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    setup_run = await run_the_reset(relay_worker)

    await submit_the_configuration(setup_run, replace(REQUESTED_CONFIGURATION, replace_public_channel=True))
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    channel_zero = fake_companion_firmware.channel(0)
    assert channel_zero is not None
    assert channel_zero.name == b"Private"
    assert channel_zero.secret != PUBLIC_CHANNEL_SECRET
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.channels_public_channel_replaced


async def test_a_node_that_ignores_the_reset_fails_it_and_the_run_reads_the_node_again(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    fake_companion_firmware.factory_reset_behaviour = FactoryResetBehaviour.IGNORES_COMMAND
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    setup_run = await start_and_read_the_node(relay_worker)
    reset_command = await confirm_the_reset(setup_run)

    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION, timeout_seconds=10)

    failed_reset = await in_database(NodeCommand.objects.get, id=reset_command.pk)
    assert failed_reset.state == NodeCommand.State.FAILED
    assert failed_reset.error_message == NODE_IGNORED_RESET_ERROR
    assert progress_states(failed_reset)[FactoryResetStep.SEND_RESET_FRAME] == "done"
    reread_command = await in_database(read_latest_command_of, setup_run.pk)
    assert reread_command.kind == NodeCommand.Kind.READ_NODE_INFORMATION
    assert reread_command.pk > failed_reset.pk
    assert reread_command.state == NodeCommand.State.SUCCEEDED


async def test_a_node_that_does_not_come_back_fails_the_reset_with_the_unplug_advice(
    fake_node_connector: FakeNodeConnector,
    fake_companion_firmware: FakeCompanionFirmware,
    close_sync_to_async_thread_connections: None,
) -> None:
    impatient_harness = RelayWorkerHarness(
        connector=fake_node_connector, timing=replace(FAST_WORKER_TIMING, reconnect_after_restart_wait_seconds=1.0)
    )
    fake_companion_firmware.stays_off_after_reboot = True
    impatient_harness.start()
    try:
        await impatient_harness.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
        setup_run = await start_and_read_the_node(impatient_harness)
        reset_command = await confirm_the_reset(setup_run)

        await wait_for_database(
            lambda: NodeCommand.objects.get(id=reset_command.pk).state == NodeCommand.State.FAILED,
            description="the reset to fail",
        )
        failed_reset = await in_database(NodeCommand.objects.get, id=reset_command.pk)
        assert failed_reset.error_message == NODE_DID_NOT_RETURN_AFTER_RESET_ERROR
        await wait_for_run_state(setup_run.pk, NodeSetupRun.State.READING_NODE)
    finally:
        await impatient_harness.stop()


async def test_a_worker_restart_after_the_reset_frame_reads_the_node_and_continues_with_the_new_key(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def die_before_the_node_disappears(*_arguments: Any) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(NodeSetupSteps, "_wait_for_node_to_disappear", die_before_the_node_disappears)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    setup_run = await start_and_read_the_node(relay_worker)
    original_public_key = setup_run.original_public_key
    reset_command = await confirm_the_reset(setup_run)
    await wait_until(
        lambda: fake_companion_firmware.public_key.hex() != original_public_key, description="the node to reset"
    )

    monkeypatch.undo()
    await relay_worker.restart()

    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    interrupted_reset = await in_database(NodeCommand.objects.get, id=reset_command.pk)
    assert interrupted_reset.state == NodeCommand.State.INTERRUPTED
    continued_run = await in_database(read_setup_run, setup_run.pk)
    assert continued_run.new_public_key == fake_companion_firmware.public_key.hex()
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)


async def test_a_read_back_mismatch_fails_the_configuration_and_leaves_node_setting_alone(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, monkeypatch: pytest.MonkeyPatch
) -> None:
    await configure_relay_node(fake_companion_firmware)
    previous_configuration = await in_database(load_node_configuration)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    setup_run = await run_the_reset(relay_worker)
    reboot_and_wait = NodeSetupSteps._reboot_and_wait

    async def reboot_and_lose_the_transmit_power(self: NodeSetupSteps, progress: Any) -> None:
        await reboot_and_wait(self, progress)
        fake_companion_firmware.preferences.transmit_power_dbm = 5

    monkeypatch.setattr(NodeSetupSteps, "_reboot_and_wait", reboot_and_lose_the_transmit_power)

    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)

    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    assert configure_command.state == NodeCommand.State.FAILED
    assert "radio.transmit_power_dbm is 5, requested 20" in configure_command.error_message
    assert progress_states(configure_command)[ConfigureNodeStep.READ_BACK] == "failed"
    assert await in_database(load_node_configuration) == previous_configuration


async def attach_another_board(relay_worker: RelayWorkerHarness, firmware: FakeCompanionFirmware) -> None:
    """The board is replaced by one with an identity of its own, and the worker is connected to it."""
    previous_public_key = firmware.public_key
    previous_connection_generation = relay_worker.runtime_status.connection_generation
    firmware.factory_reset()
    await wait_until(
        lambda: firmware.is_running and firmware.public_key != previous_public_key, description="another board"
    )
    await relay_worker.wait_for_connection_generation(previous_connection_generation + 1)


async def test_the_reset_is_refused_when_another_board_was_attached_after_the_node_was_read(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    setup_run = await start_and_read_the_node(relay_worker)
    await attach_another_board(relay_worker, fake_companion_firmware)
    other_board_public_key = fake_companion_firmware.public_key

    reset_command = await confirm_the_reset(setup_run)

    await wait_for_database(
        lambda: NodeCommand.objects.get(id=reset_command.pk).state == NodeCommand.State.FAILED,
        description="the reset to fail",
    )
    failed_reset = await in_database(NodeCommand.objects.get, id=reset_command.pk)
    assert failed_reset.error_message.startswith("A different node is attached")
    assert FactoryResetStep.SEND_RESET_FRAME not in progress_states(failed_reset)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION)
    await relay_worker.clock.sleep(0.3)
    assert fake_companion_firmware.public_key == other_board_public_key


async def test_the_reset_is_not_sent_when_another_board_was_attached_while_the_messages_were_received(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    await in_database(create_packet_awaiting_acknowledgement_until, 30.0)
    relay_worker.timing = replace(relay_worker.timing, node_restart_quiet_wait_seconds=20.0)
    relay_worker.worker = relay_worker.build_worker()
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    setup_run = await start_and_read_the_node(relay_worker)
    reset_command = await confirm_the_reset(setup_run)
    await wait_for_database(
        lambda: read_step_state(reset_command.pk, FactoryResetStep.RECEIVE_WAITING_MESSAGES) == "running",
        description="the reset to receive the waiting messages",
    )

    await attach_another_board(relay_worker, fake_companion_firmware)
    other_board_public_key = fake_companion_firmware.public_key
    relay_worker.clock.advance(seconds=31)

    await wait_for_database(
        lambda: NodeCommand.objects.get(id=reset_command.pk).state == NodeCommand.State.FAILED,
        description="the reset to fail",
    )
    failed_reset = await in_database(NodeCommand.objects.get, id=reset_command.pk)
    assert failed_reset.error_message == NODE_RECONNECTED_BEFORE_RESET_ERROR
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION)
    await relay_worker.clock.sleep(0.3)
    assert fake_companion_firmware.public_key == other_board_public_key


async def test_a_board_swapped_after_the_reset_fails_the_configuration(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    setup_run = await run_the_reset(relay_worker)
    await attach_another_board(relay_worker, fake_companion_firmware)

    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)

    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    assert configure_command.state == NodeCommand.State.FAILED
    assert configure_command.error_message.startswith("A different node is attached")
    assert await in_database(load_node_configuration) is None


async def test_cancelling_after_the_reset_shows_the_identity_mismatch_without_reconnecting(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    setup_run = await run_the_reset(relay_worker)
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)
    generation_after_the_reset = relay_worker.runtime_status.connection_generation

    await in_database(cancel_setup_run, setup_run.pk, timezone.now())

    await relay_worker.wait_for_relay_mode(RelayMode.IDENTITY_MISMATCH)
    assert relay_worker.runtime_status.connection_generation == generation_after_the_reset


async def test_the_reset_is_refused_when_the_node_keeps_its_identity(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_companion_firmware.factory_reset_behaviour = FactoryResetBehaviour.IGNORES_COMMAND

    async def the_link_drops_anyway(self: NodeSetupSteps, *_arguments: Any) -> None:
        fake_companion_firmware.reboot()

    monkeypatch.setattr(NodeSetupSteps, "_wait_for_node_to_disappear", the_link_drops_anyway)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    setup_run = await start_and_read_the_node(relay_worker)
    reset_command = await confirm_the_reset(setup_run)

    await wait_for_database(
        lambda: NodeCommand.objects.get(id=reset_command.pk).state == NodeCommand.State.FAILED,
        description="the reset to fail",
    )
    failed_reset = await in_database(NodeCommand.objects.get, id=reset_command.pk)
    assert failed_reset.error_message == IDENTITY_UNCHANGED_ERROR


# ----- keeping the relay's identity ----------------------------------------------------------------


def private_key_of(firmware: FakeCompanionFirmware) -> NodePrivateKey:
    return NodePrivateKey(firmware.identity.expanded_private_key)


def count_key_imports(firmware: FakeCompanionFirmware) -> int:
    return sum(
        1 for received_command in firmware.command_log if received_command.code == CommandCode.IMPORT_PRIVATE_KEY
    )


async def backup_restores(public_key: str, private_key: NodePrivateKey) -> bool:
    return await in_database(load_private_key_for_restore, public_key) == private_key


async def start_a_running_relay_with_a_backed_up_identity(
    relay_worker: RelayWorkerHarness, firmware: FakeCompanionFirmware
) -> str:
    """The configured node, its identity backed up by the worker's first handshake; returns its key."""
    await configure_relay_node(firmware)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert (await in_database(read_node_identity_backup_state)).is_stored_for(firmware.public_key.hex())
    return firmware.public_key.hex()


KEEPING_THE_IDENTITY = replace(REQUESTED_CONFIGURATION, restore_identity=True)
FOREIGN_IDENTITY_SEED = 99


async def fail_to_set_the_radio(*_arguments: Any) -> None:
    raise NodeCommandFailedError("The radio could not be set.")


async def test_a_reconfiguration_that_keeps_the_identity_gives_the_reset_node_the_relays_key_back(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker")
    contact = await in_database(create_contact_for_device, device)
    original_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    original_private_key = private_key_of(fake_companion_firmware)
    backup_taken_at = (await in_database(read_node_identity_backup_state)).created_at
    setup_run = await run_the_reset(relay_worker)
    assert fake_companion_firmware.public_key.hex() == setup_run.new_public_key != original_public_key

    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert fake_companion_firmware.public_key.hex() == original_public_key
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == original_public_key
    assert parse_contact_card_uri(node_configuration.node_contact_card_uri).public_key == original_public_key
    backup_state = await in_database(read_node_identity_backup_state)
    assert backup_state.is_stored_for(original_public_key)
    assert backup_state.created_at == backup_taken_at
    assert await backup_restores(original_public_key, original_private_key)
    completed_run = await in_database(read_setup_run, setup_run.pk)
    assert completed_run.restored_public_key == original_public_key
    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact added to the node again",
    )
    await wait_for_database(
        lambda: read_latest_command_of(setup_run.pk).state == NodeCommand.State.SUCCEEDED,
        description="the configuration command to finish, relaying resumed",
    )
    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    step_states = progress_states(configure_command)
    assert list(step_states) == [step.value for step in ConfigureNodeStep]
    assert step_states[ConfigureNodeStep.RESTORE_IDENTITY] == "done"
    assert step_states[ConfigureNodeStep.BACK_UP_IDENTITY] == "skipped"
    assert configure_command.result == {
        "node_public_key": original_public_key,
        "contact_card_uri": node_configuration.node_contact_card_uri,
        "identity_restored": True,
    }
    assert relay_worker.runtime_status.node_identity_backup_state == WorkerStatus.NodeIdentityBackupState.STORED
    inbox = DeviceInbox(device)
    device.send_direct_message("HT1 Q bob")
    await inbox.wait_for_text("HT1 e NOT_SIGNED_IN Q bob")
    await in_database(assert_all_invariants)


async def test_a_reconfiguration_with_a_new_identity_backs_the_new_identity_up(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    original_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    setup_run = await run_the_reset(relay_worker)
    new_private_key = private_key_of(fake_companion_firmware)

    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == setup_run.new_public_key != original_public_key
    assert await backup_restores(setup_run.new_public_key, new_private_key)
    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    assert progress_states(configure_command)[ConfigureNodeStep.RESTORE_IDENTITY] == "skipped"
    assert progress_states(configure_command)[ConfigureNodeStep.BACK_UP_IDENTITY] == "done"
    assert count_key_imports(fake_companion_firmware) == 0
    await in_database(assert_all_invariants)


async def test_a_firmware_without_the_export_still_completes_a_new_identity_without_a_backup(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    setup_run = await run_the_reset(relay_worker)
    fake_companion_firmware.private_key_export_enabled = False

    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    back_up_step = next(
        step for step in configure_command.progress if step["step"] == ConfigureNodeStep.BACK_UP_IDENTITY
    )
    assert back_up_step["state"] == "skipped"
    assert "does not allow exporting its private key" in back_up_step["detail"]
    assert (await in_database(read_node_identity_backup_state)).status == NodeIdentityBackupStatus.ABSENT
    await wait_until(
        lambda: (
            relay_worker.runtime_status.node_identity_backup_state
            == WorkerStatus.NodeIdentityBackupState.EXPORT_DISABLED
        ),
        description="the worker to record that the firmware refuses the export",
    )


async def assert_the_restore_failed_and_changed_nothing(
    setup_run: NodeSetupRun, previous_configuration: NodeConfiguration | None, expected_reason: str
) -> NodeCommand:
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    assert configure_command.state == NodeCommand.State.FAILED
    assert expected_reason in configure_command.error_message
    assert progress_states(configure_command)[ConfigureNodeStep.RESTORE_IDENTITY] == "failed"
    assert ConfigureNodeStep.SET_CLOCK not in progress_states(configure_command)
    assert expected_reason in (await in_database(read_setup_run, setup_run.pk)).last_error
    assert await in_database(load_node_configuration) == previous_configuration
    return configure_command


async def test_a_key_the_node_cannot_save_fails_the_restore_and_leaves_node_setting_alone(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    configured_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    previous_configuration = await in_database(load_node_configuration)
    setup_run = await run_the_reset(relay_worker)
    fake_companion_firmware.private_key_import_save_fails = True

    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)

    await assert_the_restore_failed_and_changed_nothing(
        setup_run, previous_configuration, "The node could not save the relay's private key to its flash (error 5)."
    )
    assert fake_companion_firmware.public_key.hex() == setup_run.new_public_key
    assert (await in_database(read_setup_run, setup_run.pk)).restored_public_key == configured_public_key
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)


async def test_a_backup_whose_key_is_not_the_relays_fails_the_restore_before_the_node_is_touched(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    configured_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    previous_configuration = await in_database(load_node_configuration)
    setup_run = await run_the_reset(relay_worker)
    foreign_key_pair = generate_node_key_pair(FOREIGN_IDENTITY_SEED)
    await in_database(
        write_node_identity_backup,
        encrypt_a_foreign_private_key(configured_public_key, foreign_key_pair.private_key, timezone.now()),
    )

    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)

    await assert_the_restore_failed_and_changed_nothing(setup_run, previous_configuration, MISMATCHED_KEY_PAIR_REASON)
    assert count_key_imports(fake_companion_firmware) == 0
    assert fake_companion_firmware.public_key.hex() == setup_run.new_public_key
    assert (await in_database(read_setup_run, setup_run.pk)).restored_public_key == ""


async def test_a_firmware_without_the_import_fails_the_restore_and_the_setup_can_go_on_with_a_new_identity(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    previous_configuration = await in_database(load_node_configuration)
    fake_companion_firmware.private_key_import_enabled = False
    setup_run = await run_the_reset(relay_worker)

    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)
    await assert_the_restore_failed_and_changed_nothing(
        setup_run, previous_configuration, "does not allow importing a private key"
    )
    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == setup_run.new_public_key
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)


async def test_a_backup_the_secret_key_no_longer_opens_fails_the_restore_before_the_node_is_touched(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, settings: Settings
) -> None:
    await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    previous_configuration = await in_database(load_node_configuration)
    setup_run = await run_the_reset(relay_worker)
    settings.SECRET_KEY = "a secret key generated again after the backup was taken"

    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)

    await assert_the_restore_failed_and_changed_nothing(setup_run, previous_configuration, "SECRET_KEY in src/.env")
    assert count_key_imports(fake_companion_firmware) == 0
    assert (await in_database(read_setup_run, setup_run.pk)).restored_public_key == ""


async def test_a_worker_restart_during_the_restore_keeps_the_relay_stopped_and_the_setup_goes_on_with_the_key(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker dies after the node took the key and before the import was confirmed."""
    restore_confirmation_started = asyncio.Event()

    async def die_before_the_import_is_confirmed(*_arguments: Any) -> None:
        restore_confirmation_started.set()
        await asyncio.Event().wait()

    device = simulated_mesh.add_device("tracker")
    contact = await in_database(create_contact_for_device, device)
    original_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    setup_run = await run_the_reset(relay_worker)
    monkeypatch.setattr(NodeSetupSteps, "_require_restored_identity", die_before_the_import_is_confirmed)
    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)
    await wait_until(restore_confirmation_started.is_set, description="the configuration to reach the confirmation")
    assert fake_companion_firmware.public_key.hex() == original_public_key

    await relay_worker.restart()
    monkeypatch.undo()

    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    interrupted_command = await in_database(read_latest_command_of, setup_run.pk)
    assert interrupted_command.state == NodeCommand.State.INTERRUPTED
    assert progress_states(interrupted_command)[ConfigureNodeStep.RESTORE_IDENTITY] == "running"
    assert ConfigureNodeStep.SET_CLOCK not in progress_states(interrupted_command)
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)
    await relay_worker.clock.sleep(0.3)
    assert relay_worker.runtime_status.relay_mode == RelayMode.SETUP_IN_PROGRESS
    assert (await in_database(read_contact, contact.pk)).node_sync_state == Contact.NodeSyncState.PENDING_ADD

    await submit_the_configuration(setup_run)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert fake_companion_firmware.public_key.hex() == original_public_key
    assert count_key_imports(fake_companion_firmware) == 1
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == original_public_key
    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact added to the node again",
    )
    await wait_for_database(
        lambda: read_latest_command_of(setup_run.pk).state == NodeCommand.State.SUCCEEDED,
        description="the configuration command to finish, relaying resumed",
    )
    completed_command = await in_database(read_latest_command_of, setup_run.pk)
    assert completed_command.result is not None
    assert completed_command.result["identity_restored"] is True
    assert progress_states(completed_command)[ConfigureNodeStep.RESTORE_IDENTITY] == "done"
    await in_database(assert_all_invariants)


@pytest.mark.parametrize("requested_configuration", [KEEPING_THE_IDENTITY, REQUESTED_CONFIGURATION])
async def test_a_node_that_already_holds_the_relays_key_goes_on_with_it_when_the_backup_no_longer_opens(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    requested_configuration: RequestedNodeConfiguration,
) -> None:
    original_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    original_private_key = private_key_of(fake_companion_firmware)
    setup_run = await run_the_reset(relay_worker)
    monkeypatch.setattr(NodeSetupSteps, "_run_setting_steps", fail_to_set_the_radio)
    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    assert fake_companion_firmware.public_key.hex() == original_public_key
    monkeypatch.undo()
    settings.SECRET_KEY = "a secret key generated again while the setup was waiting"
    assert not (await in_database(read_configured_node_identity_backup)).is_restorable

    await submit_the_configuration(setup_run, requested_configuration)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.COMPLETED)

    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    node_configuration = await in_database(load_node_configuration)
    assert node_configuration is not None
    assert node_configuration.node_public_key == original_public_key
    assert count_key_imports(fake_companion_firmware) == 1
    assert await backup_restores(original_public_key, original_private_key)
    configure_command = await in_database(read_latest_command_of, setup_run.pk)
    assert progress_states(configure_command)[ConfigureNodeStep.RESTORE_IDENTITY] == "done"
    assert progress_states(configure_command)[ConfigureNodeStep.BACK_UP_IDENTITY] == "done"


def serialize_rows_the_relay_writes() -> str:
    """Every row a key could leak into besides the encrypted backup, as text."""
    querysets = [
        NodeCommand.objects.all(),
        NodeSetupRun.objects.all(),
        WorkerStatus.objects.all(),
        InboundDirectMessage.objects.all(),
        OutboundPacket.objects.all(),
        NodeSetting.objects.exclude(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value),
    ]
    return "\n".join(serializers.serialize("json", queryset) for queryset in querysets)


async def test_no_private_key_reaches_a_log_record_a_command_the_worker_status_or_the_traffic_log(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=LIBRARY_LOGGER_NAME)
    await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    private_keys = [private_key_of(fake_companion_firmware)]
    kept_identity_run = await run_the_reset(relay_worker)
    private_keys.append(private_key_of(fake_companion_firmware))
    await submit_the_configuration(kept_identity_run, KEEPING_THE_IDENTITY)
    await wait_for_run_state(kept_identity_run.pk, NodeSetupRun.State.COMPLETED)
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    new_identity_run = await run_the_reset(relay_worker)
    private_keys.append(private_key_of(fake_companion_firmware))
    await submit_the_configuration(new_identity_run)
    await wait_for_run_state(new_identity_run.pk, NodeSetupRun.State.COMPLETED)
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(
        lambda: read_latest_command_of(new_identity_run.pk).state == NodeCommand.State.SUCCEEDED,
        description="the second configuration command to finish",
    )
    await relay_worker.stop()

    logged_texts = [caplog.text]
    for record in caplog.records:
        logged_texts.append(record.getMessage())
        if record.exc_info is not None:
            logged_texts.append(logging.Formatter().formatException(record.exc_info))
    stored_text = await in_database(serialize_rows_the_relay_writes)
    key_is_logged = any(
        mentions_private_key(logged_text, private_key) for logged_text in logged_texts for private_key in private_keys
    )
    key_is_stored = any(mentions_private_key(stored_text, private_key) for private_key in private_keys)

    assert not key_is_logged
    assert not key_is_stored
    assert any(record.name == LIBRARY_LOGGER_NAME for record in caplog.records)
    assert count_key_imports(fake_companion_firmware) == 1


async def test_cancelling_a_setup_whose_node_already_holds_the_relays_key_keeps_the_relay_stopped(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = simulated_mesh.add_device("tracker")
    contact = await in_database(create_contact_for_device, device)
    original_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    setup_run = await run_the_reset(relay_worker)
    monkeypatch.setattr(NodeSetupSteps, "_run_setting_steps", fail_to_set_the_radio)
    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)
    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    monkeypatch.undo()
    assert fake_companion_firmware.public_key.hex() == original_public_key
    assert fake_companion_firmware.preferences.frequency_kilohertz != REQUESTED_CONFIGURATION.radio_frequency_kilohertz
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)
    generation_before_cancelling = relay_worker.runtime_status.connection_generation

    await in_database(cancel_setup_run, setup_run.pk, timezone.now())

    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    await relay_worker.clock.sleep(0.3)
    assert relay_worker.runtime_status.relay_mode == RelayMode.NOT_CONFIGURED
    assert relay_worker.runtime_status.connection_generation == generation_before_cancelling
    assert (await in_database(read_contact, contact.pk)).node_sync_state == Contact.NodeSyncState.PENDING_ADD

    another_setup_run = await run_the_reset(relay_worker)
    await submit_the_configuration(another_setup_run, KEEPING_THE_IDENTITY)
    await wait_for_run_state(another_setup_run.pk, NodeSetupRun.State.COMPLETED)

    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    assert fake_companion_firmware.public_key.hex() == original_public_key
    assert fake_companion_firmware.preferences.frequency_kilohertz == REQUESTED_CONFIGURATION.radio_frequency_kilohertz
    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact added to the node again",
    )


async def test_a_lost_reply_to_the_import_reconnects_so_that_the_relay_mode_uses_the_key_the_node_holds(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    original_public_key = await start_a_running_relay_with_a_backed_up_identity(relay_worker, fake_companion_firmware)
    setup_run = await run_the_reset(relay_worker)
    generation_before_the_import = relay_worker.runtime_status.connection_generation
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.IMPORT_PRIVATE_KEY)

    await submit_the_configuration(setup_run, KEEPING_THE_IDENTITY)

    await wait_for_run_state(setup_run.pk, NodeSetupRun.State.AWAITING_CONFIGURATION)
    failed_command = await in_database(read_latest_command_of, setup_run.pk)
    assert "did not answer importing the private key" in failed_command.error_message
    assert fake_companion_firmware.public_key.hex() == original_public_key
    await relay_worker.wait_for_connection_generation(generation_before_the_import + 1)
    connected_node = relay_worker.runtime_status.connected_node
    assert connected_node is not None
    assert connected_node.public_key == original_public_key
    await relay_worker.wait_for_relay_mode(RelayMode.SETUP_IN_PROGRESS)

    await in_database(cancel_setup_run, setup_run.pk, timezone.now())

    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
