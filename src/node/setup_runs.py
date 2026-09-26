"""The setup wizard's state machine: node_setup_runs.

Every operator confirmation is one transaction that moves the run and creates its command, so
run and command never disagree, and every transition notifies relay_setup_changed. The worker
reports command outcomes back through the functions at the end of this module.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Max

from directory.models import Contact
from node import node_commands
from node.models import NodeCommand, NodeSetupRun
from node.node_identity_backups import (
    NodeIdentityBackup,
    discard_node_identity_backup_of_other_identities,
    write_node_identity_backup,
)
from node.node_information import NodeInformation
from node.node_settings import NodeConfiguration, is_node_configured, replace_node_configuration
from node.notification_channels import NotificationChannel, notify_relay_worker

DEFAULT_NODE_NAME = "HopTalk Relay"
# Typed instead of the node's name when the node has none.
UNNAMED_NODE_CONFIRMATION_KEY_DIGITS = 8

CANCELLABLE_STATES = frozenset(
    {
        NodeSetupRun.State.READING_NODE,
        NodeSetupRun.State.AWAITING_RESET_CONFIRMATION,
        NodeSetupRun.State.AWAITING_CONFIGURATION,
    }
)
# The states in which the wizard waits for the worker rather than for the operator.
WORKER_STATES = frozenset(
    {NodeSetupRun.State.READING_NODE, NodeSetupRun.State.RESETTING, NodeSetupRun.State.CONFIGURING}
)


class FactoryResetStep(StrEnum):
    """The progress steps of a factory_reset command, in order."""

    RECEIVE_WAITING_MESSAGES = "receive_waiting_messages"
    SEND_RESET_FRAME = "send_reset_frame"
    WAIT_FOR_DISCONNECT = "wait_for_disconnect"
    WAIT_FOR_RECONNECT = "wait_for_reconnect"
    READ_NEW_IDENTITY = "read_new_identity"


FACTORY_RESET_STEP_LABELS: Mapping[FactoryResetStep, str] = {
    FactoryResetStep.RECEIVE_WAITING_MESSAGES: "Receive the messages waiting on the node",
    FactoryResetStep.SEND_RESET_FRAME: "Send the factory reset",
    FactoryResetStep.WAIT_FOR_DISCONNECT: "Wait for the node to disappear",
    FactoryResetStep.WAIT_FOR_RECONNECT: "Wait for the node to come back",
    FactoryResetStep.READ_NEW_IDENTITY: "Read the new identity",
}


class ConfigureNodeStep(StrEnum):
    """The progress steps of a configure_node command, in order."""

    CHECK_IDENTITY = "check_identity"
    RESTORE_IDENTITY = "restore_identity"
    SET_CLOCK = "set_clock"
    SET_NAME = "set_name"
    SET_RADIO = "set_radio"
    SET_TRANSMIT_POWER = "set_transmit_power"
    SET_PATH_HASH_SIZE = "set_path_hash_size"
    SET_OTHER_PARAMETERS = "set_other_parameters"
    SET_AUTO_ADD_CONFIGURATION = "set_auto_add_configuration"
    REPLACE_PUBLIC_CHANNEL = "replace_public_channel"
    REBOOT = "reboot"
    READ_BACK = "read_back"
    BACK_UP_IDENTITY = "back_up_identity"
    EXPORT_CONTACT_CARD = "export_contact_card"
    PERSIST = "persist"
    RESUME = "resume"


CONFIGURE_NODE_STEP_LABELS: Mapping[ConfigureNodeStep, str] = {
    ConfigureNodeStep.CHECK_IDENTITY: "Check that the reset node is attached",
    ConfigureNodeStep.RESTORE_IDENTITY: "Restore the relay's identity",
    ConfigureNodeStep.SET_CLOCK: "Set the clock",
    ConfigureNodeStep.SET_NAME: "Set the name",
    ConfigureNodeStep.SET_RADIO: "Set the radio",
    ConfigureNodeStep.SET_TRANSMIT_POWER: "Set the transmit power",
    ConfigureNodeStep.SET_PATH_HASH_SIZE: "Set the path hash size",
    ConfigureNodeStep.SET_OTHER_PARAMETERS: "Manual add on, telemetry denied, location private, multi-acks 2",
    ConfigureNodeStep.SET_AUTO_ADD_CONFIGURATION: "Turn automatic adding of contacts off",
    ConfigureNodeStep.REPLACE_PUBLIC_CHANNEL: "Replace the Public channel",
    ConfigureNodeStep.REBOOT: "Reboot the node",
    ConfigureNodeStep.READ_BACK: "Read every value back",
    ConfigureNodeStep.BACK_UP_IDENTITY: "Back up the new identity",
    ConfigureNodeStep.EXPORT_CONTACT_CARD: "Export the contact card",
    ConfigureNodeStep.PERSIST: "Save the configuration",
    ConfigureNodeStep.RESUME: "Resume relaying",
}


class SetupRunTransitionError(Exception):
    """The run is not in a state that allows the requested step; the message is shown to the operator."""


@dataclass(frozen=True, kw_only=True)
class RequestedNodeConfiguration:
    """The validated configuration form of the wizard's step 2, stored as requested_configuration."""

    node_name: str
    # A bundled preset's title, or node_settings.MANUAL_RADIO_PRESET_TITLE.
    radio_preset_title: str
    radio_frequency_kilohertz: int
    radio_bandwidth_hertz: int
    radio_spreading_factor: int
    radio_coding_rate: int
    path_hash_size: int
    transmit_power_dbm: int
    replace_public_channel: bool
    # Import the configured identity's backed-up key into the reset node, so users keep the relay's card.
    restore_identity: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, stored_json: Mapping[str, Any]) -> RequestedNodeConfiguration:
        return cls(
            node_name=str(stored_json["node_name"]),
            radio_preset_title=str(stored_json["radio_preset_title"]),
            radio_frequency_kilohertz=int(stored_json["radio_frequency_kilohertz"]),
            radio_bandwidth_hertz=int(stored_json["radio_bandwidth_hertz"]),
            radio_spreading_factor=int(stored_json["radio_spreading_factor"]),
            radio_coding_rate=int(stored_json["radio_coding_rate"]),
            path_hash_size=int(stored_json["path_hash_size"]),
            transmit_power_dbm=int(stored_json["transmit_power_dbm"]),
            replace_public_channel=bool(stored_json["replace_public_channel"]),
            restore_identity=bool(stored_json.get("restore_identity", False)),
        )


def get_active_setup_run() -> NodeSetupRun | None:
    return NodeSetupRun.objects.filter(is_active=True).first()


def get_latest_completed_setup_run() -> NodeSetupRun | None:
    return NodeSetupRun.objects.filter(state=NodeSetupRun.State.COMPLETED).order_by("-finished_at", "-id").first()


def was_configured_identity_restored_without_configuration(configured_public_key: str) -> bool:
    """A run abandoned after it began importing this key into a reset node, and no run completed since.

    The node that took the key then reports the configured identity with its factory settings.
    Runs start one at a time, so a completed run with a higher id finished after the abandoned one.
    """
    if not configured_public_key:
        return False
    latest_completed_run_id = NodeSetupRun.objects.filter(state=NodeSetupRun.State.COMPLETED).aggregate(
        latest_id=Max("id")
    )["latest_id"]
    return NodeSetupRun.objects.filter(
        state=NodeSetupRun.State.ABANDONED,
        restored_public_key=configured_public_key,
        id__gt=latest_completed_run_id or 0,
    ).exists()


def find_latest_setup_run_command(setup_run: NodeSetupRun) -> NodeCommand | None:
    return setup_run.node_commands.order_by("-id").first()


def read_original_node_information(setup_run: NodeSetupRun) -> NodeInformation | None:
    if setup_run.original_node_information is None:
        return None
    return NodeInformation.from_json(setup_run.original_node_information)


def read_requested_configuration(setup_run: NodeSetupRun) -> RequestedNodeConfiguration | None:
    if setup_run.requested_configuration is None:
        return None
    return RequestedNodeConfiguration.from_json(setup_run.requested_configuration)


def start_setup_run(now: datetime) -> NodeSetupRun:
    """Create the run (purpose initial when node_setting is empty) and its read_node_information command.

    Raises SetupRunTransitionError when another run is active (node_setup_single_active_run).
    """
    purpose = NodeSetupRun.Purpose.RECONFIGURE if is_node_configured() else NodeSetupRun.Purpose.INITIAL
    with transaction.atomic():
        try:
            with transaction.atomic():
                setup_run = NodeSetupRun.objects.create(
                    purpose=purpose,
                    state=NodeSetupRun.State.READING_NODE,
                    started_at=now,
                    is_active=True,
                )
        except IntegrityError as integrity_error:
            raise SetupRunTransitionError("Another setup run is already in progress.") from integrity_error

        node_commands.create_node_command(NodeCommand.Kind.READ_NODE_INFORMATION, {}, now, setup_run=setup_run)
        notify_relay_worker(NotificationChannel.SETUP_CHANGED, str(setup_run.pk))
    return setup_run


def retry_reading_node(setup_run_id: int, now: datetime) -> NodeCommand:
    """The "Retry" button after a failed read: a new read_node_information command for a run in reading_node."""
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        require_setup_run_state(setup_run, NodeSetupRun.State.READING_NODE)
        if setup_run.node_commands.filter(state=NodeCommand.State.RUNNING).exists():
            raise SetupRunTransitionError("The node is being read right now.")

        cancel_pending_setup_run_commands(setup_run, now)
        setup_run.last_error = ""
        setup_run.save(update_fields=["last_error"])
        read_command = node_commands.create_node_command(
            NodeCommand.Kind.READ_NODE_INFORMATION, {}, now, setup_run=setup_run
        )
        notify_relay_worker(NotificationChannel.SETUP_CHANGED, str(setup_run.pk))
    return read_command


def find_expected_factory_reset_confirmation(setup_run: NodeSetupRun) -> str:
    """The node's current name, or the first 8 hex digits of its key when it has none."""
    node_information = read_original_node_information(setup_run)
    if node_information is None:
        return ""
    return node_information.name or node_information.public_key[:UNNAMED_NODE_CONFIRMATION_KEY_DIGITS]


def confirm_factory_reset(setup_run_id: int, typed_confirmation: str, now: datetime) -> NodeCommand:
    """Check the typed node name, move the run to resetting and create its factory_reset command.

    The node's current name is expected, or the first 8 hex digits of its key when it has none.
    """
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        require_setup_run_state(setup_run, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION)
        expected_confirmation = find_expected_factory_reset_confirmation(setup_run)
        if not expected_confirmation or typed_confirmation.strip() != expected_confirmation:
            raise SetupRunTransitionError(
                f"Type the node's current name, {expected_confirmation}, exactly to confirm the factory reset."
            )

        move_setup_run(setup_run, NodeSetupRun.State.RESETTING, now, last_error="")
        return node_commands.create_node_command(
            NodeCommand.Kind.FACTORY_RESET, {"setup_run_id": setup_run.pk}, now, setup_run=setup_run
        )


def submit_node_configuration(
    setup_run_id: int,
    requested_configuration: RequestedNodeConfiguration,
    now: datetime,
) -> NodeCommand:
    """Store the configuration, move the run to configuring and create its configure_node command."""
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        require_setup_run_state(setup_run, NodeSetupRun.State.AWAITING_CONFIGURATION)
        setup_run.requested_configuration = requested_configuration.to_json()
        setup_run.save(update_fields=["requested_configuration"])
        move_setup_run(setup_run, NodeSetupRun.State.CONFIGURING, now, last_error="")
        return node_commands.create_node_command(
            NodeCommand.Kind.CONFIGURE_NODE, {"setup_run_id": setup_run.pk}, now, setup_run=setup_run
        )


def cancel_setup_run(setup_run_id: int, now: datetime) -> None:
    """Abandon a run in reading_node, awaiting_reset_confirmation or awaiting_configuration.

    A run that began restoring the configured identity keeps its restored_public_key: the node
    may hold that key without its settings, and it must not relay until a run completes.
    """
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        if setup_run.state not in CANCELLABLE_STATES:
            raise SetupRunTransitionError(
                f"Setup cannot be cancelled while it is {setup_run.get_state_display().lower()}."
            )
        cancel_pending_setup_run_commands(setup_run, now)
        move_setup_run(setup_run, NodeSetupRun.State.ABANDONED, now)


def record_node_information_read(setup_run_id: int, node_information: Mapping[str, Any], now: datetime) -> None:
    """A read succeeded: awaiting_reset_confirmation, or awaiting_configuration when the key changed after a reset.

    A run that is no longer reading the node (the operator cancelled it meanwhile) is left alone.
    """
    read_information = NodeInformation.from_json(node_information)
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        if setup_run.state != NodeSetupRun.State.READING_NODE:
            return

        is_reread_after_reset = bool(setup_run.original_public_key)
        if is_reread_after_reset and read_information.public_key != setup_run.original_public_key:
            complete_factory_reset(setup_run, read_information.public_key, now)
            return

        setup_run.original_node_information = read_information.to_json()
        setup_run.original_public_key = read_information.public_key
        setup_run.save(update_fields=["original_node_information", "original_public_key"])
        move_setup_run(setup_run, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION, now, last_error="")


def record_factory_reset_succeeded(setup_run_id: int, new_public_key: str, now: datetime) -> None:
    """Move the run to awaiting_configuration with the new key and set every contact to pending_add."""
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        require_setup_run_state(setup_run, NodeSetupRun.State.RESETTING)
        if new_public_key == setup_run.original_public_key:
            raise SetupRunTransitionError("The node kept its identity, so it was not reset.")
        complete_factory_reset(setup_run, new_public_key, now)


def record_identity_restore_started(setup_run_id: int, restored_public_key: str) -> None:
    """Note, before the import frame is sent, which identity the reset node may hold from now on."""
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        require_setup_run_state(setup_run, NodeSetupRun.State.CONFIGURING)
        setup_run.restored_public_key = restored_public_key
        setup_run.save(update_fields=["restored_public_key"])


def record_configuration_completed(
    setup_run_id: int,
    node_configuration: NodeConfiguration,
    now: datetime,
    node_identity_backup: NodeIdentityBackup | None = None,
) -> None:
    """Replace node_setting, store the identity backup and complete the run in one transaction.

    configure_node calls it once the node's values were read back and its contact card exported.
    Without a new backup, the stored one stays only if it belongs to the final key (a restored
    identity); a backup of any other identity could never be restored again and is deleted.
    """
    if node_identity_backup is not None and node_identity_backup.public_key != node_configuration.node_public_key:
        raise ValueError("The identity backup written with a configuration must be the configured key's.")
    with transaction.atomic():
        setup_run = lock_setup_run(setup_run_id)
        require_setup_run_state(setup_run, NodeSetupRun.State.CONFIGURING)
        replace_node_configuration(node_configuration)
        if node_identity_backup is not None:
            write_node_identity_backup(node_identity_backup)
        else:
            discard_node_identity_backup_of_other_identities(node_configuration.node_public_key)
        move_setup_run(setup_run, NodeSetupRun.State.COMPLETED, now, last_error="")


def fall_back_after_unsuccessful_command(node_command: NodeCommand, now: datetime) -> None:
    """Move the run of a failed, interrupted or expired command back, with last_error set.

    A factory_reset whose progress shows the reset frame was sent goes back to reading_node
    with a new read_node_information command; everything else goes to its awaiting state.
    A command that is no longer the run's latest one, or whose run already moved on, changes
    nothing, so calling this twice for the same command is harmless.
    """
    if node_command.setup_run_id is None:
        return

    with transaction.atomic():
        setup_run = lock_setup_run(node_command.setup_run_id)
        if setup_run.node_commands.filter(id__gt=node_command.pk).exists():
            return

        last_error = describe_unsuccessful_setup_command(node_command)
        match node_command.kind:
            case NodeCommand.Kind.READ_NODE_INFORMATION if setup_run.state == NodeSetupRun.State.READING_NODE:
                setup_run.last_error = last_error
                setup_run.save(update_fields=["last_error"])
                notify_relay_worker(NotificationChannel.SETUP_CHANGED, str(setup_run.pk))
            case NodeCommand.Kind.FACTORY_RESET if setup_run.state == NodeSetupRun.State.RESETTING:
                fall_back_after_unsuccessful_factory_reset(setup_run, node_command, last_error, now)
            case NodeCommand.Kind.CONFIGURE_NODE if setup_run.state == NodeSetupRun.State.CONFIGURING:
                move_setup_run(setup_run, NodeSetupRun.State.AWAITING_CONFIGURATION, now, last_error=last_error)
            case _:
                return


def fall_back_after_unsuccessful_factory_reset(
    setup_run: NodeSetupRun, node_command: NodeCommand, last_error: str, now: datetime
) -> None:
    if not factory_reset_frame_may_have_been_sent(node_command):
        move_setup_run(setup_run, NodeSetupRun.State.AWAITING_RESET_CONFIRMATION, now, last_error=last_error)
        return

    # The node may have been reset, so its identity is read again before the wizard shows anything.
    move_setup_run(
        setup_run,
        NodeSetupRun.State.READING_NODE,
        now,
        last_error=f"{last_error} The node is being read again to find out whether it was reset.",
    )
    node_commands.create_node_command(NodeCommand.Kind.READ_NODE_INFORMATION, {}, now, setup_run=setup_run)


def factory_reset_frame_may_have_been_sent(node_command: NodeCommand) -> bool:
    """True once the command's progress reached the reset frame, whatever that step's state."""
    return any(stored_step.get("step") == FactoryResetStep.SEND_RESET_FRAME for stored_step in node_command.progress)


def describe_unsuccessful_setup_command(node_command: NodeCommand) -> str:
    action_descriptions_by_kind = {
        NodeCommand.Kind.READ_NODE_INFORMATION: "Reading the node",
        NodeCommand.Kind.FACTORY_RESET: "The factory reset",
        NodeCommand.Kind.CONFIGURE_NODE: "Configuring the node",
    }
    action_description = action_descriptions_by_kind.get(
        NodeCommand.Kind(node_command.kind), node_command.get_kind_display()
    )
    outcome_descriptions_by_state = {
        NodeCommand.State.FAILED: "failed",
        NodeCommand.State.INTERRUPTED: "was interrupted",
        NodeCommand.State.EXPIRED: "did not start",
    }
    outcome_description = outcome_descriptions_by_state.get(NodeCommand.State(node_command.state), "did not succeed")
    reason = node_command.error_message or "No reason was reported."
    return f"{action_description} {outcome_description}: {reason}"


def complete_factory_reset(setup_run: NodeSetupRun, new_public_key: str, now: datetime) -> None:
    # The wiped node holds no contacts; the final reconciliation adds every one of them again.
    Contact.objects.update(node_sync_state=Contact.NodeSyncState.PENDING_ADD, node_sync_error="")
    setup_run.new_public_key = new_public_key
    setup_run.save(update_fields=["new_public_key"])
    move_setup_run(setup_run, NodeSetupRun.State.AWAITING_CONFIGURATION, now, last_error="")


def lock_setup_run(setup_run_id: int) -> NodeSetupRun:
    try:
        return NodeSetupRun.objects.select_for_update().get(id=setup_run_id)
    except NodeSetupRun.DoesNotExist as missing_run_error:
        raise SetupRunTransitionError("This setup run does not exist.") from missing_run_error


def require_setup_run_state(setup_run: NodeSetupRun, required_state: NodeSetupRun.State) -> None:
    if setup_run.state != required_state:
        raise SetupRunTransitionError(
            f"This step is not possible while setup is {setup_run.get_state_display().lower()}. "
            "Reload the page to see where it stands."
        )


def move_setup_run(
    setup_run: NodeSetupRun,
    new_state: NodeSetupRun.State,
    now: datetime,
    last_error: str | None = None,
) -> None:
    """Change the state together with is_active and finished_at, and wake the worker at commit.

    last_error None keeps the error the run already shows.
    """
    is_final_state = new_state in NodeSetupRun.FINAL_STATES
    setup_run.state = new_state
    setup_run.is_active = not is_final_state
    setup_run.finished_at = now if is_final_state else None
    if last_error is not None:
        setup_run.last_error = last_error
    setup_run.save(update_fields=["state", "is_active", "finished_at", "last_error"])
    notify_relay_worker(NotificationChannel.SETUP_CHANGED, str(setup_run.pk))


def cancel_pending_setup_run_commands(setup_run: NodeSetupRun, now: datetime) -> None:
    pending_command_ids = setup_run.node_commands.filter(state=NodeCommand.State.PENDING).values_list("id", flat=True)
    for pending_command_id in pending_command_ids:
        node_commands.cancel_node_command(pending_command_id, now)
