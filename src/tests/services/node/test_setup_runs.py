from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from directory.models import Contact
from node.models import NodeCommand, NodeSetupRun
from node.node_commands import (
    ProgressStep,
    ProgressStepState,
    claim_next_node_command,
    expire_pending_node_commands,
    finish_node_command,
    interrupt_running_node_commands,
    record_node_command_progress,
)
from node.node_identity_backups import (
    NodeIdentityBackupStatus,
    encrypt_node_identity_backup,
    load_private_key_for_restore,
    read_node_identity_backup_state,
    store_node_identity_backup,
    write_node_identity_backup,
)
from node.node_settings import load_node_configuration, replace_node_configuration
from node.setup_runs import (
    FactoryResetStep,
    RequestedNodeConfiguration,
    SetupRunTransitionError,
    cancel_setup_run,
    confirm_factory_reset,
    fall_back_after_unsuccessful_command,
    find_expected_factory_reset_confirmation,
    get_active_setup_run,
    record_configuration_completed,
    record_factory_reset_succeeded,
    record_identity_restore_started,
    record_node_information_read,
    retry_reading_node,
    start_setup_run,
    submit_node_configuration,
    was_configured_identity_restored_without_configuration,
)
from tests.services.directory.row_builders import create_contact
from tests.services.node.node_builders import (
    ORIGINAL_NODE_KEY_PAIR,
    ORIGINAL_NODE_PUBLIC_KEY,
    RESET_NODE_KEY_PAIR,
    RESET_NODE_PUBLIC_KEY,
    build_node_configuration,
    build_node_information,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
WORKER_INSTANCE_ID = uuid4()
REQUESTED_CONFIGURATION = RequestedNodeConfiguration(
    node_name="HopTalk Relay",
    radio_preset_title="Australia (Narrow)",
    radio_frequency_kilohertz=916575,
    radio_bandwidth_hertz=62500,
    radio_spreading_factor=7,
    radio_coding_rate=7,
    path_hash_size=2,
    transmit_power_dbm=22,
    replace_public_channel=False,
)


def latest_command(setup_run: NodeSetupRun) -> NodeCommand:
    node_command = setup_run.node_commands.order_by("-id").first()
    assert node_command is not None
    return node_command


def claim_the_setup_command(setup_run: NodeSetupRun) -> NodeCommand:
    claimed_command = claim_next_node_command(WORKER_INSTANCE_ID, NOW)
    assert claimed_command is not None
    assert claimed_command.setup_run_id == setup_run.pk
    return claimed_command


def reload(setup_run: NodeSetupRun) -> NodeSetupRun:
    setup_run.refresh_from_db()
    return setup_run


def run_awaiting_reset_confirmation(node_name: str = "Old relay") -> NodeSetupRun:
    setup_run = start_setup_run(NOW)
    claimed_read = claim_the_setup_command(setup_run)
    record_node_information_read(setup_run.pk, build_node_information(name=node_name).to_json(), NOW)
    finish_node_command(claimed_read.pk, NodeCommand.State.SUCCEEDED, NOW)
    return reload(setup_run)


def run_resetting() -> NodeSetupRun:
    setup_run = run_awaiting_reset_confirmation()
    confirm_factory_reset(setup_run.pk, "Old relay", NOW)
    return reload(setup_run)


def run_awaiting_configuration() -> NodeSetupRun:
    setup_run = run_resetting()
    claimed_reset = claim_the_setup_command(setup_run)
    record_factory_reset_succeeded(setup_run.pk, RESET_NODE_PUBLIC_KEY, NOW)
    finish_node_command(claimed_reset.pk, NodeCommand.State.SUCCEEDED, NOW, {"new_public_key": RESET_NODE_PUBLIC_KEY})
    return reload(setup_run)


def run_configuring() -> NodeSetupRun:
    setup_run = run_awaiting_configuration()
    submit_node_configuration(setup_run.pk, REQUESTED_CONFIGURATION, NOW)
    return reload(setup_run)


def record_reset_frame_sent(node_command: NodeCommand) -> None:
    record_node_command_progress(
        node_command.pk,
        ProgressStep(step=FactoryResetStep.SEND_RESET_FRAME, state=ProgressStepState.DONE, detail="", at=NOW),
    )


def test_a_run_on_an_empty_node_setting_is_the_initial_setup_and_reads_the_node_first() -> None:
    setup_run = start_setup_run(NOW)

    assert setup_run.purpose == NodeSetupRun.Purpose.INITIAL
    assert setup_run.state == NodeSetupRun.State.READING_NODE
    assert setup_run.is_active
    assert get_active_setup_run() == setup_run
    read_command = latest_command(setup_run)
    assert read_command.kind == NodeCommand.Kind.READ_NODE_INFORMATION
    assert read_command.state == NodeCommand.State.PENDING


def test_a_run_on_a_configured_node_is_a_reconfiguration() -> None:
    replace_node_configuration(build_node_configuration())

    assert start_setup_run(NOW).purpose == NodeSetupRun.Purpose.RECONFIGURE


def test_a_second_active_run_is_refused() -> None:
    first_run = start_setup_run(NOW)

    with pytest.raises(SetupRunTransitionError, match="already in progress"):
        start_setup_run(NOW)

    assert list(NodeSetupRun.objects.all()) == [first_run]
    assert NodeCommand.objects.count() == 1


def test_a_successful_read_waits_for_the_reset_confirmation_and_keeps_what_it_read() -> None:
    setup_run = run_awaiting_reset_confirmation()

    assert setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert setup_run.original_public_key == ORIGINAL_NODE_PUBLIC_KEY
    assert setup_run.original_node_information == build_node_information().to_json()


def test_a_failed_read_stays_in_reading_with_the_error_until_retry_reads_again() -> None:
    setup_run = start_setup_run(NOW)
    claimed_read = claim_the_setup_command(setup_run)
    finish_node_command(claimed_read.pk, NodeCommand.State.FAILED, NOW, error_message="The node did not answer.")

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.READING_NODE
    assert setup_run.last_error == "Reading the node failed: The node did not answer."

    retried_read = retry_reading_node(setup_run.pk, NOW)

    assert retried_read.kind == NodeCommand.Kind.READ_NODE_INFORMATION
    assert reload(setup_run).last_error == ""


def test_a_read_the_worker_never_started_expires_with_an_explanation() -> None:
    setup_run = start_setup_run(NOW)

    expire_pending_node_commands(NOW + timedelta(minutes=6))

    assert reload(setup_run).last_error.startswith("Reading the node did not start: The relay worker did not start")


def test_retry_is_refused_while_the_node_is_being_read() -> None:
    setup_run = start_setup_run(NOW)
    claim_the_setup_command(setup_run)

    with pytest.raises(SetupRunTransitionError, match="being read right now"):
        retry_reading_node(setup_run.pk, NOW)


def test_retry_replaces_a_read_that_is_still_waiting() -> None:
    setup_run = start_setup_run(NOW)
    waiting_read = latest_command(setup_run)

    retry_reading_node(setup_run.pk, NOW)

    waiting_read.refresh_from_db()
    assert waiting_read.state == NodeCommand.State.CANCELLED
    assert setup_run.node_commands.filter(state=NodeCommand.State.PENDING).count() == 1


def test_the_factory_reset_needs_the_node_name_typed_exactly() -> None:
    setup_run = run_awaiting_reset_confirmation()

    with pytest.raises(SetupRunTransitionError, match="Type the node's current name, Old relay"):
        confirm_factory_reset(setup_run.pk, "old relay", NOW)

    assert reload(setup_run).state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert not setup_run.node_commands.filter(kind=NodeCommand.Kind.FACTORY_RESET).exists()


def test_a_confirmed_reset_moves_the_run_and_creates_its_command_together() -> None:
    setup_run = run_awaiting_reset_confirmation()

    reset_command = confirm_factory_reset(setup_run.pk, "  Old relay ", NOW)

    assert reload(setup_run).state == NodeSetupRun.State.RESETTING
    assert reset_command.kind == NodeCommand.Kind.FACTORY_RESET
    assert reset_command.arguments == {"setup_run_id": setup_run.pk}
    assert reset_command.expires_at == NOW + timedelta(seconds=60)


def test_an_unnamed_node_is_confirmed_with_the_first_eight_digits_of_its_key() -> None:
    setup_run = run_awaiting_reset_confirmation(node_name="")

    assert find_expected_factory_reset_confirmation(setup_run) == ORIGINAL_NODE_PUBLIC_KEY[:8]
    confirm_factory_reset(setup_run.pk, ORIGINAL_NODE_PUBLIC_KEY[:8], NOW)
    assert reload(setup_run).state == NodeSetupRun.State.RESETTING


def test_a_successful_reset_awaits_the_configuration_and_every_contact_awaits_its_add() -> None:
    on_node_contact = create_contact(1)
    failed_contact = create_contact(2, node_sync_state=Contact.NodeSyncState.ADD_FAILED)

    setup_run = run_awaiting_configuration()

    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION
    assert setup_run.new_public_key == RESET_NODE_PUBLIC_KEY
    for contact in (on_node_contact, failed_contact):
        contact.refresh_from_db()
        assert contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD
        assert contact.node_sync_error == ""


def test_a_reset_that_kept_the_identity_is_not_a_success() -> None:
    setup_run = run_resetting()

    with pytest.raises(SetupRunTransitionError, match="kept its identity"):
        record_factory_reset_succeeded(setup_run.pk, ORIGINAL_NODE_PUBLIC_KEY, NOW)


def test_a_reset_that_failed_before_its_frame_goes_back_to_the_confirmation() -> None:
    setup_run = run_resetting()
    claimed_reset = claim_the_setup_command(setup_run)

    finish_node_command(claimed_reset.pk, NodeCommand.State.FAILED, NOW, error_message="Not allowed now.")

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert setup_run.last_error == "The factory reset failed: Not allowed now."


def test_a_reset_that_expired_goes_back_to_the_confirmation() -> None:
    setup_run = run_resetting()

    expire_pending_node_commands(NOW + timedelta(seconds=61))

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert "did not start" in setup_run.last_error


def test_a_reset_that_failed_after_its_frame_reads_the_node_again_and_continues_with_the_new_key() -> None:
    contact = create_contact(1)
    setup_run = run_resetting()
    claimed_reset = claim_the_setup_command(setup_run)
    record_reset_frame_sent(claimed_reset)

    finish_node_command(claimed_reset.pk, NodeCommand.State.FAILED, NOW, error_message="The node did not come back.")

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.READING_NODE
    assert "read again" in setup_run.last_error
    reread_command = claim_the_setup_command(setup_run)
    assert reread_command.kind == NodeCommand.Kind.READ_NODE_INFORMATION

    record_node_information_read(
        setup_run.pk, build_node_information(public_key=RESET_NODE_PUBLIC_KEY, name="").to_json(), NOW
    )

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION
    assert setup_run.new_public_key == RESET_NODE_PUBLIC_KEY
    assert setup_run.original_public_key == ORIGINAL_NODE_PUBLIC_KEY
    contact.refresh_from_db()
    assert contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD


def test_a_reread_that_finds_the_old_identity_shows_the_node_again_for_confirmation() -> None:
    setup_run = run_resetting()
    claimed_reset = claim_the_setup_command(setup_run)
    record_reset_frame_sent(claimed_reset)
    finish_node_command(claimed_reset.pk, NodeCommand.State.FAILED, NOW, error_message="The node ignored the reset.")
    claim_the_setup_command(setup_run)

    reread_information = replace(build_node_information(), contact_count=3)
    record_node_information_read(setup_run.pk, reread_information.to_json(), NOW)

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert setup_run.original_node_information == reread_information.to_json()


def test_a_reset_interrupted_by_a_worker_restart_after_its_frame_reads_the_node_again() -> None:
    setup_run = run_resetting()
    claimed_reset = claim_the_setup_command(setup_run)
    record_reset_frame_sent(claimed_reset)

    interrupt_running_node_commands(NOW)

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.READING_NODE
    assert latest_command(setup_run).kind == NodeCommand.Kind.READ_NODE_INFORMATION


def test_a_submitted_configuration_is_stored_with_its_configure_command() -> None:
    setup_run = run_awaiting_configuration()

    configure_command = submit_node_configuration(setup_run.pk, REQUESTED_CONFIGURATION, NOW)

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.CONFIGURING
    assert setup_run.requested_configuration == REQUESTED_CONFIGURATION.to_json()
    assert RequestedNodeConfiguration.from_json(setup_run.requested_configuration) == REQUESTED_CONFIGURATION
    assert configure_command.kind == NodeCommand.Kind.CONFIGURE_NODE
    assert configure_command.expires_at == NOW + timedelta(seconds=60)


def test_a_failed_configuration_goes_back_to_the_form_with_the_values_kept() -> None:
    setup_run = run_configuring()
    claimed_configure = claim_the_setup_command(setup_run)

    finish_node_command(claimed_configure.pk, NodeCommand.State.FAILED, NOW, error_message="radio: ERR 6")

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION
    assert setup_run.last_error == "Configuring the node failed: radio: ERR 6"
    assert setup_run.requested_configuration == REQUESTED_CONFIGURATION.to_json()
    assert load_node_configuration() is None


def test_a_completed_configuration_replaces_node_setting_and_ends_the_run() -> None:
    setup_run = run_configuring()
    claim_the_setup_command(setup_run)
    node_configuration = build_node_configuration(setup_run_id=setup_run.pk)

    record_configuration_completed(setup_run.pk, node_configuration, NOW)

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.COMPLETED
    assert not setup_run.is_active
    assert setup_run.finished_at == NOW
    assert load_node_configuration() == node_configuration
    assert get_active_setup_run() is None
    assert start_setup_run(NOW).purpose == NodeSetupRun.Purpose.RECONFIGURE


PREVIOUS_PRIVATE_KEY = ORIGINAL_NODE_KEY_PAIR.private_key
NEW_PRIVATE_KEY = RESET_NODE_KEY_PAIR.private_key


def run_configuring_a_configured_node_with_a_backup() -> NodeSetupRun:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    store_node_identity_backup(ORIGINAL_NODE_PUBLIC_KEY, PREVIOUS_PRIVATE_KEY, NOW)
    setup_run = run_configuring()
    claim_the_setup_command(setup_run)
    return setup_run


def test_a_configuration_with_a_new_identity_saves_its_backup_in_the_same_transaction() -> None:
    setup_run = run_configuring_a_configured_node_with_a_backup()
    new_backup = encrypt_node_identity_backup(RESET_NODE_PUBLIC_KEY, NEW_PRIVATE_KEY, NOW)

    record_configuration_completed(
        setup_run.pk, build_node_configuration(public_key=RESET_NODE_PUBLIC_KEY), NOW, new_backup
    )

    assert load_private_key_for_restore(RESET_NODE_PUBLIC_KEY) == NEW_PRIVATE_KEY


def test_a_configuration_that_kept_the_identity_keeps_its_backup() -> None:
    setup_run = run_configuring_a_configured_node_with_a_backup()

    record_configuration_completed(setup_run.pk, build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY), NOW)

    assert load_private_key_for_restore(ORIGINAL_NODE_PUBLIC_KEY) == PREVIOUS_PRIVATE_KEY


def test_a_new_identity_without_a_backup_deletes_the_backup_of_the_old_one() -> None:
    setup_run = run_configuring_a_configured_node_with_a_backup()

    record_configuration_completed(setup_run.pk, build_node_configuration(public_key=RESET_NODE_PUBLIC_KEY), NOW)

    assert read_node_identity_backup_state().status == NodeIdentityBackupStatus.ABSENT


def test_a_backup_of_another_key_than_the_configured_one_is_refused_and_nothing_changes() -> None:
    setup_run = run_configuring_a_configured_node_with_a_backup()
    mismatched_backup = encrypt_node_identity_backup(ORIGINAL_NODE_PUBLIC_KEY, PREVIOUS_PRIVATE_KEY, NOW)

    with pytest.raises(ValueError, match="configured key"):
        record_configuration_completed(
            setup_run.pk, build_node_configuration(public_key=RESET_NODE_PUBLIC_KEY), NOW, mismatched_backup
        )

    assert reload(setup_run).state == NodeSetupRun.State.CONFIGURING
    assert load_private_key_for_restore(ORIGINAL_NODE_PUBLIC_KEY) == PREVIOUS_PRIVATE_KEY


def test_the_run_notes_the_identity_it_began_to_restore_and_keeps_it_after_a_failure() -> None:
    setup_run = run_configuring_a_configured_node_with_a_backup()
    configure_command = latest_command(setup_run)

    record_identity_restore_started(setup_run.pk, ORIGINAL_NODE_PUBLIC_KEY)
    finish_node_command(configure_command.pk, NodeCommand.State.FAILED, NOW, error_message="import refused")

    setup_run = reload(setup_run)
    assert setup_run.restored_public_key == ORIGINAL_NODE_PUBLIC_KEY
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION
    with pytest.raises(SetupRunTransitionError):
        record_identity_restore_started(setup_run.pk, ORIGINAL_NODE_PUBLIC_KEY)


def test_a_configuration_stored_before_the_identity_could_be_kept_does_not_keep_it() -> None:
    stored_json = REQUESTED_CONFIGURATION.to_json()
    del stored_json["restore_identity"]

    assert RequestedNodeConfiguration.from_json(stored_json).restore_identity is False
    assert RequestedNodeConfiguration.from_json({**stored_json, "restore_identity": True}).restore_identity


def test_a_stored_backup_alone_does_not_make_the_next_run_a_reconfiguration() -> None:
    write_node_identity_backup(encrypt_node_identity_backup(ORIGINAL_NODE_PUBLIC_KEY, PREVIOUS_PRIVATE_KEY, NOW))

    assert start_setup_run(NOW).purpose == NodeSetupRun.Purpose.INITIAL


def run_reading_node() -> NodeSetupRun:
    return start_setup_run(NOW)


RUN_BUILDERS_BY_CANCELLABLE_STATE: dict[str, Callable[[], NodeSetupRun]] = {
    "reading_node": run_reading_node,
    "awaiting_reset_confirmation": run_awaiting_reset_confirmation,
    "awaiting_configuration": run_awaiting_configuration,
}


@pytest.mark.parametrize("cancellable_state", RUN_BUILDERS_BY_CANCELLABLE_STATE)
def test_setup_can_be_cancelled_while_the_operator_or_the_read_is_awaited(cancellable_state: str) -> None:
    setup_run = RUN_BUILDERS_BY_CANCELLABLE_STATE[cancellable_state]()

    cancel_setup_run(setup_run.pk, NOW)

    setup_run = reload(setup_run)
    assert setup_run.state == NodeSetupRun.State.ABANDONED
    assert not setup_run.is_active
    assert not setup_run.node_commands.filter(state=NodeCommand.State.PENDING).exists()


def test_setup_cannot_be_cancelled_while_the_node_is_being_reset() -> None:
    setup_run = run_resetting()

    with pytest.raises(SetupRunTransitionError, match="cannot be cancelled while it is resetting"):
        cancel_setup_run(setup_run.pk, NOW)


def test_a_read_that_finishes_after_the_run_was_cancelled_changes_nothing() -> None:
    setup_run = start_setup_run(NOW)
    claim_the_setup_command(setup_run)
    NodeSetupRun.objects.filter(id=setup_run.pk).update(
        state=NodeSetupRun.State.ABANDONED, is_active=False, finished_at=NOW
    )

    record_node_information_read(setup_run.pk, build_node_information().to_json(), NOW)

    assert reload(setup_run).state == NodeSetupRun.State.ABANDONED


def test_a_step_that_does_not_fit_the_state_is_refused_with_a_hint() -> None:
    setup_run = start_setup_run(NOW)

    with pytest.raises(SetupRunTransitionError, match="Reload the page"):
        submit_node_configuration(setup_run.pk, REQUESTED_CONFIGURATION, NOW)


def test_falling_back_twice_or_for_an_older_command_changes_nothing() -> None:
    setup_run = run_configuring()
    claimed_configure = claim_the_setup_command(setup_run)
    finish_node_command(claimed_configure.pk, NodeCommand.State.FAILED, NOW, error_message="first")
    submit_node_configuration(setup_run.pk, REQUESTED_CONFIGURATION, NOW)
    claimed_configure.refresh_from_db()

    fall_back_after_unsuccessful_command(claimed_configure, NOW)

    assert reload(setup_run).state == NodeSetupRun.State.CONFIGURING


def abandon_a_run_that_began_restoring(restored_public_key: str) -> NodeSetupRun:
    setup_run = run_configuring()
    configure_command = claim_the_setup_command(setup_run)
    record_identity_restore_started(setup_run.pk, restored_public_key)
    finish_node_command(configure_command.pk, NodeCommand.State.FAILED, NOW, error_message="radio")
    cancel_setup_run(setup_run.pk, NOW)
    return reload(setup_run)


def test_a_run_cancelled_after_it_began_restoring_the_identity_leaves_it_on_an_unconfigured_node() -> None:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    assert not was_configured_identity_restored_without_configuration(ORIGINAL_NODE_PUBLIC_KEY)

    abandoned_run = abandon_a_run_that_began_restoring(ORIGINAL_NODE_PUBLIC_KEY)

    assert abandoned_run.state == NodeSetupRun.State.ABANDONED
    assert abandoned_run.restored_public_key == ORIGINAL_NODE_PUBLIC_KEY
    assert was_configured_identity_restored_without_configuration(ORIGINAL_NODE_PUBLIC_KEY)
    assert not was_configured_identity_restored_without_configuration(RESET_NODE_PUBLIC_KEY)
    assert not was_configured_identity_restored_without_configuration("")


def test_a_later_run_cancelled_before_its_reset_does_not_hide_the_unconfigured_node() -> None:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    abandon_a_run_that_began_restoring(ORIGINAL_NODE_PUBLIC_KEY)

    cancel_setup_run(run_reading_node().pk, NOW)

    assert was_configured_identity_restored_without_configuration(ORIGINAL_NODE_PUBLIC_KEY)


def test_a_completed_run_configures_the_node_that_holds_the_restored_identity() -> None:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    abandon_a_run_that_began_restoring(ORIGINAL_NODE_PUBLIC_KEY)
    completing_run = run_configuring()
    claim_the_setup_command(completing_run)

    record_configuration_completed(
        completing_run.pk, build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY), NOW
    )

    assert not was_configured_identity_restored_without_configuration(ORIGINAL_NODE_PUBLIC_KEY)
