"""Relay modes: what the worker may do with the attached node.

The mode is a pure function of three inputs: the node's key from the last finished handshake,
the configured node in node_setting, and the active setup run. It is recomputed after every
handshake, at teardown, after every setup-run change and node command, before every command
claim and on the periodic sweep, so a cancelled setup run shows its true consequence at once.

A setup run cancelled after it began giving the reset node the configured identity back may
leave a node that reports the configured key with nothing but its factory settings: another
radio, no contacts. Until a setup run completes, such a node is not configured, and never relays.
"""

import logging
from dataclasses import dataclass

from node.models import NodeCommand, NodeSetupRun, WorkerStatus
from node.node_settings import (
    IncompleteNodeConfigurationError,
    NodeSettingKey,
    is_node_configured,
    load_node_configuration,
    read_node_setting_value,
)
from node.setup_runs import get_active_setup_run, was_configured_identity_restored_without_configuration

logger = logging.getLogger(__name__)

RelayMode = WorkerStatus.RelayMode

SETUP_RUN_STATES_OWNING_THE_NODE = frozenset({NodeSetupRun.State.RESETTING, NodeSetupRun.State.CONFIGURING})

NOT_ALLOWED_WHILE_DISCONNECTED = "The node is not connected."
NOT_ALLOWED_ON_ANOTHER_NODE = "Not allowed while the attached node is not the configured one."
NOT_ALLOWED_BEFORE_SETUP = "Not allowed before the node has been set up."
NOT_ALLOWED_DURING_SETUP = "Not allowed while setup is in progress."
NOT_ALLOWED_OUTSIDE_SETUP = "Only the setup wizard's active run can do this."
NOT_ALLOWED_WHILE_RELAYING = "Not allowed while the node relays messages; start the setup wizard instead."


@dataclass(frozen=True, kw_only=True)
class ActiveSetupRunSummary:
    setup_run_id: int
    state: NodeSetupRun.State
    # The key the successful factory reset produced; "" before it.
    new_public_key: str
    # The configured key configure_node began to import into the reset node; "" when it did not.
    restored_public_key: str = ""


@dataclass(frozen=True, kw_only=True)
class ConfiguredNode:
    """node_setting as the relay mode sees it: empty, or holding a key (which may be "" in a broken table)."""

    is_configured: bool
    public_key: str
    configuration_error: str = ""
    # A cancelled setup run may have left this identity on a reset node it never configured.
    identity_restored_without_configuration: bool = False


@dataclass(frozen=True, kw_only=True)
class RelayModeInputs:
    configured_node: ConfiguredNode
    active_setup_run: ActiveSetupRunSummary | None


def decide_relay_mode(
    *,
    connected_node_public_key: str | None,
    configured_node: ConfiguredNode,
    active_setup_run: ActiveSetupRunSummary | None,
) -> WorkerStatus.RelayMode:
    """First match wins. connected_node_public_key is None until a handshake has finished."""
    if connected_node_public_key is None:
        return RelayMode.DISCONNECTED
    if is_setup_in_progress(connected_node_public_key, active_setup_run):
        return RelayMode.SETUP_IN_PROGRESS
    if not configured_node.is_configured:
        return RelayMode.NOT_CONFIGURED
    if configured_node.public_key and connected_node_public_key == configured_node.public_key:
        if configured_node.identity_restored_without_configuration:
            return RelayMode.NOT_CONFIGURED
        return RelayMode.RUNNING
    return RelayMode.IDENTITY_MISMATCH


def is_setup_in_progress(connected_node_public_key: str, active_setup_run: ActiveSetupRunSummary | None) -> bool:
    """A run resetting or configuring owns the node; so does a run whose reset or restore produced the attached key.

    The second case keeps the time between the reset and the configuration from looking like an
    identity mismatch. After a failed or interrupted restore the reset node may already hold the
    configured key, and it must not relay unconfigured as if it were the configured node.
    """
    if active_setup_run is None:
        return False
    if active_setup_run.state in SETUP_RUN_STATES_OWNING_THE_NODE:
        return True
    keys_the_run_gave_the_node = {active_setup_run.new_public_key, active_setup_run.restored_public_key} - {""}
    return connected_node_public_key in keys_the_run_gave_the_node


def load_relay_mode_inputs() -> RelayModeInputs:
    return RelayModeInputs(configured_node=load_configured_node(), active_setup_run=load_active_setup_run_summary())


def load_configured_node() -> ConfiguredNode:
    """An incomplete node_setting is a fatal configuration error; its key alone can never make the mode running."""
    if not is_node_configured():
        return ConfiguredNode(is_configured=False, public_key="")
    try:
        node_configuration = load_node_configuration()
    except IncompleteNodeConfigurationError as incomplete_configuration_error:
        return ConfiguredNode(
            is_configured=True,
            public_key="",
            configuration_error=str(incomplete_configuration_error),
        )
    if node_configuration is None:
        return ConfiguredNode(is_configured=False, public_key="")
    return ConfiguredNode(
        is_configured=True,
        public_key=node_configuration.node_public_key,
        identity_restored_without_configuration=was_configured_identity_restored_without_configuration(
            node_configuration.node_public_key
        ),
    )


def load_active_setup_run_summary() -> ActiveSetupRunSummary | None:
    active_setup_run = get_active_setup_run()
    if active_setup_run is None:
        return None
    return ActiveSetupRunSummary(
        setup_run_id=active_setup_run.pk,
        state=NodeSetupRun.State(active_setup_run.state),
        new_public_key=active_setup_run.new_public_key,
        restored_public_key=active_setup_run.restored_public_key,
    )


def read_configured_public_key() -> str:
    return read_node_setting_value(NodeSettingKey.NODE_PUBLIC_KEY)


def find_node_command_refusal(
    node_command: NodeCommand,
    relay_mode: WorkerStatus.RelayMode,
    active_setup_run: ActiveSetupRunSummary | None,
) -> str | None:
    """Why the command may not run in this mode, or None when it may."""
    kind = NodeCommand.Kind(node_command.kind)
    if relay_mode == RelayMode.DISCONNECTED:
        return NOT_ALLOWED_WHILE_DISCONNECTED
    if kind in (NodeCommand.Kind.READ_NODE_INFORMATION, NodeCommand.Kind.STOP_PAIRING):
        return None
    if kind == NodeCommand.Kind.FACTORY_RESET:
        return find_setup_command_refusal(node_command, relay_mode, active_setup_run, NodeSetupRun.State.RESETTING)
    if kind == NodeCommand.Kind.CONFIGURE_NODE:
        return find_setup_command_refusal(node_command, relay_mode, active_setup_run, NodeSetupRun.State.CONFIGURING)
    if kind == NodeCommand.Kind.REBOOT_NODE and relay_mode in (
        RelayMode.RUNNING,
        RelayMode.NOT_CONFIGURED,
        RelayMode.IDENTITY_MISMATCH,
    ):
        return None
    if relay_mode == RelayMode.RUNNING:
        return None
    return describe_mode_refusal(relay_mode)


def find_setup_command_refusal(
    node_command: NodeCommand,
    relay_mode: WorkerStatus.RelayMode,
    active_setup_run: ActiveSetupRunSummary | None,
    required_run_state: NodeSetupRun.State,
) -> str | None:
    """factory_reset and configure_node belong to the active run, in the state that created them."""
    if relay_mode == RelayMode.RUNNING:
        return NOT_ALLOWED_WHILE_RELAYING
    if relay_mode != RelayMode.SETUP_IN_PROGRESS or active_setup_run is None:
        return NOT_ALLOWED_OUTSIDE_SETUP
    is_active_run_command = node_command.setup_run_id == active_setup_run.setup_run_id
    if not is_active_run_command or active_setup_run.state != required_run_state:
        return NOT_ALLOWED_OUTSIDE_SETUP
    return None


def describe_mode_refusal(relay_mode: WorkerStatus.RelayMode) -> str:
    match relay_mode:
        case RelayMode.IDENTITY_MISMATCH:
            return NOT_ALLOWED_ON_ANOTHER_NODE
        case RelayMode.NOT_CONFIGURED:
            return NOT_ALLOWED_BEFORE_SETUP
        case RelayMode.SETUP_IN_PROGRESS:
            return NOT_ALLOWED_DURING_SETUP
        case _:
            return NOT_ALLOWED_WHILE_DISCONNECTED
