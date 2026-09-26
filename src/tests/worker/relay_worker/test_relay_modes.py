"""The relay mode for every combination of its inputs, and the node commands each mode allows."""

import itertools
from dataclasses import replace

import pytest

from node.models import NodeCommand, NodeSetupRun, WorkerStatus
from worker.relay_modes import (
    NOT_ALLOWED_BEFORE_SETUP,
    NOT_ALLOWED_DURING_SETUP,
    NOT_ALLOWED_ON_ANOTHER_NODE,
    NOT_ALLOWED_OUTSIDE_SETUP,
    NOT_ALLOWED_WHILE_DISCONNECTED,
    NOT_ALLOWED_WHILE_RELAYING,
    ActiveSetupRunSummary,
    ConfiguredNode,
    decide_relay_mode,
    find_node_command_refusal,
)

RelayMode = WorkerStatus.RelayMode
KEY_OF_THE_CONFIGURED_NODE = "aa" * 32
KEY_OF_A_RESET_NODE = "bb" * 32
KEY_OF_A_STRANGER = "cc" * 32
ACTIVE_SETUP_RUN_ID = 7

CONNECTED_NODE_KEYS = [None, KEY_OF_THE_CONFIGURED_NODE, KEY_OF_A_RESET_NODE, KEY_OF_A_STRANGER]
CONFIGURED_NODE = ConfiguredNode(is_configured=True, public_key=KEY_OF_THE_CONFIGURED_NODE)
CONFIGURED_NODE_RESTORED_WITHOUT_CONFIGURATION = replace(CONFIGURED_NODE, identity_restored_without_configuration=True)
CONFIGURED_NODES = [
    ConfiguredNode(is_configured=False, public_key=""),
    CONFIGURED_NODE,
    CONFIGURED_NODE_RESTORED_WITHOUT_CONFIGURATION,
    ConfiguredNode(is_configured=True, public_key="", configuration_error="node_setting is incomplete"),
]
ACTIVE_RUN_STATES = [*NodeSetupRun.State]
NEW_PUBLIC_KEYS = ["", KEY_OF_A_RESET_NODE]
RESTORED_PUBLIC_KEYS = ["", KEY_OF_THE_CONFIGURED_NODE]
ACTIVE_SETUP_RUNS: list[ActiveSetupRunSummary | None] = [
    None,
    *(
        ActiveSetupRunSummary(
            setup_run_id=ACTIVE_SETUP_RUN_ID,
            state=state,
            new_public_key=new_public_key,
            restored_public_key=restored_public_key,
        )
        for state, new_public_key, restored_public_key in itertools.product(
            ACTIVE_RUN_STATES, NEW_PUBLIC_KEYS, RESTORED_PUBLIC_KEYS
        )
        if new_public_key or not restored_public_key
    ),
]


def expected_relay_mode(
    connected_node_public_key: str | None,
    configured_node: ConfiguredNode,
    active_setup_run: ActiveSetupRunSummary | None,
) -> WorkerStatus.RelayMode:
    """The specification's table, written out independently of the implementation."""
    if connected_node_public_key is None:
        return RelayMode.DISCONNECTED
    if active_setup_run is not None and (
        active_setup_run.state in (NodeSetupRun.State.RESETTING, NodeSetupRun.State.CONFIGURING)
        or (active_setup_run.new_public_key != "" and active_setup_run.new_public_key == connected_node_public_key)
        or (
            active_setup_run.restored_public_key != ""
            and active_setup_run.restored_public_key == connected_node_public_key
        )
    ):
        return RelayMode.SETUP_IN_PROGRESS
    if not configured_node.is_configured:
        return RelayMode.NOT_CONFIGURED
    if connected_node_public_key == configured_node.public_key:
        if configured_node.identity_restored_without_configuration:
            return RelayMode.NOT_CONFIGURED
        return RelayMode.RUNNING
    return RelayMode.IDENTITY_MISMATCH


@pytest.mark.parametrize(
    ("connected_node_public_key", "configured_node", "active_setup_run"),
    list(itertools.product(CONNECTED_NODE_KEYS, CONFIGURED_NODES, ACTIVE_SETUP_RUNS)),
)
def test_the_relay_mode_follows_the_first_matching_rule(
    connected_node_public_key: str | None,
    configured_node: ConfiguredNode,
    active_setup_run: ActiveSetupRunSummary | None,
) -> None:
    relay_mode = decide_relay_mode(
        connected_node_public_key=connected_node_public_key,
        configured_node=configured_node,
        active_setup_run=active_setup_run,
    )

    assert relay_mode == expected_relay_mode(connected_node_public_key, configured_node, active_setup_run)


def test_a_node_without_a_finished_handshake_is_disconnected_whatever_else_holds() -> None:
    assert (
        decide_relay_mode(
            connected_node_public_key=None,
            configured_node=ConfiguredNode(is_configured=True, public_key=KEY_OF_THE_CONFIGURED_NODE),
            active_setup_run=ActiveSetupRunSummary(
                setup_run_id=1, state=NodeSetupRun.State.RESETTING, new_public_key=""
            ),
        )
        == RelayMode.DISCONNECTED
    )


def test_the_reset_node_waiting_for_its_configuration_is_not_a_mismatch_until_the_run_is_abandoned() -> None:
    configured_node = ConfiguredNode(is_configured=True, public_key=KEY_OF_THE_CONFIGURED_NODE)
    waiting_run = ActiveSetupRunSummary(
        setup_run_id=1, state=NodeSetupRun.State.AWAITING_CONFIGURATION, new_public_key=KEY_OF_A_RESET_NODE
    )

    assert (
        decide_relay_mode(
            connected_node_public_key=KEY_OF_A_RESET_NODE, configured_node=configured_node, active_setup_run=waiting_run
        )
        == RelayMode.SETUP_IN_PROGRESS
    )
    assert (
        decide_relay_mode(
            connected_node_public_key=KEY_OF_A_RESET_NODE, configured_node=configured_node, active_setup_run=None
        )
        == RelayMode.IDENTITY_MISMATCH
    )


def test_a_reset_node_that_may_hold_the_restored_identity_does_not_relay_until_the_run_ends() -> None:
    configured_node = ConfiguredNode(is_configured=True, public_key=KEY_OF_THE_CONFIGURED_NODE)
    run_after_a_failed_restore = ActiveSetupRunSummary(
        setup_run_id=1,
        state=NodeSetupRun.State.AWAITING_CONFIGURATION,
        new_public_key=KEY_OF_A_RESET_NODE,
        restored_public_key=KEY_OF_THE_CONFIGURED_NODE,
    )
    run_without_a_restore = replace(run_after_a_failed_restore, restored_public_key="")

    for attached_public_key in (KEY_OF_A_RESET_NODE, KEY_OF_THE_CONFIGURED_NODE):
        assert (
            decide_relay_mode(
                connected_node_public_key=attached_public_key,
                configured_node=configured_node,
                active_setup_run=run_after_a_failed_restore,
            )
            == RelayMode.SETUP_IN_PROGRESS
        )
    assert (
        decide_relay_mode(
            connected_node_public_key=KEY_OF_THE_CONFIGURED_NODE,
            configured_node=configured_node,
            active_setup_run=run_without_a_restore,
        )
        == RelayMode.RUNNING
    )


def test_a_node_given_the_relays_identity_by_a_cancelled_run_does_not_relay_until_a_run_completes() -> None:
    for attached_public_key, expected_relay_mode_after_the_cancel in (
        (KEY_OF_THE_CONFIGURED_NODE, RelayMode.NOT_CONFIGURED),
        (KEY_OF_A_RESET_NODE, RelayMode.IDENTITY_MISMATCH),
    ):
        assert (
            decide_relay_mode(
                connected_node_public_key=attached_public_key,
                configured_node=CONFIGURED_NODE_RESTORED_WITHOUT_CONFIGURATION,
                active_setup_run=None,
            )
            == expected_relay_mode_after_the_cancel
        )


def test_an_incomplete_configuration_never_relays() -> None:
    broken_configuration = ConfiguredNode(is_configured=True, public_key="", configuration_error="incomplete")

    assert (
        decide_relay_mode(connected_node_public_key="", configured_node=broken_configuration, active_setup_run=None)
        == RelayMode.IDENTITY_MISMATCH
    )


def build_command(kind: NodeCommand.Kind, setup_run_id: int | None = None) -> NodeCommand:
    return NodeCommand(kind=kind, setup_run_id=setup_run_id)


EVERY_KIND_BUT_SETUP = [
    kind for kind in NodeCommand.Kind if kind not in (NodeCommand.Kind.FACTORY_RESET, NodeCommand.Kind.CONFIGURE_NODE)
]


@pytest.mark.parametrize("kind", list(NodeCommand.Kind))
def test_no_command_runs_while_the_node_is_disconnected(kind: NodeCommand.Kind) -> None:
    assert find_node_command_refusal(build_command(kind), RelayMode.DISCONNECTED, None) == (
        NOT_ALLOWED_WHILE_DISCONNECTED
    )


@pytest.mark.parametrize("kind", EVERY_KIND_BUT_SETUP)
def test_every_command_but_the_setup_ones_runs_in_relay_mode_running(kind: NodeCommand.Kind) -> None:
    assert find_node_command_refusal(build_command(kind), RelayMode.RUNNING, None) is None


@pytest.mark.parametrize("kind", [NodeCommand.Kind.FACTORY_RESET, NodeCommand.Kind.CONFIGURE_NODE])
def test_the_setup_commands_never_run_while_the_node_relays(kind: NodeCommand.Kind) -> None:
    active_run = ActiveSetupRunSummary(
        setup_run_id=ACTIVE_SETUP_RUN_ID, state=NodeSetupRun.State.RESETTING, new_public_key=""
    )
    command = build_command(kind, setup_run_id=ACTIVE_SETUP_RUN_ID)

    assert find_node_command_refusal(command, RelayMode.RUNNING, active_run) == NOT_ALLOWED_WHILE_RELAYING


@pytest.mark.parametrize(
    ("relay_mode", "expected_refusal"),
    [
        (RelayMode.NOT_CONFIGURED, NOT_ALLOWED_BEFORE_SETUP),
        (RelayMode.IDENTITY_MISMATCH, NOT_ALLOWED_ON_ANOTHER_NODE),
        (RelayMode.SETUP_IN_PROGRESS, NOT_ALLOWED_DURING_SETUP),
    ],
)
@pytest.mark.parametrize(
    "kind",
    [
        NodeCommand.Kind.APPLY_CONFIGURED_SETTINGS,
        NodeCommand.Kind.SEND_ADVERT,
        NodeCommand.Kind.EXPORT_CONTACT_CARD,
        NodeCommand.Kind.START_PAIRING,
        NodeCommand.Kind.RECONCILE_CONTACTS,
    ],
)
def test_commands_for_a_relaying_node_are_refused_in_the_other_modes(
    kind: NodeCommand.Kind, relay_mode: WorkerStatus.RelayMode, expected_refusal: str
) -> None:
    assert find_node_command_refusal(build_command(kind), relay_mode, None) == expected_refusal


@pytest.mark.parametrize(
    "relay_mode",
    [RelayMode.RUNNING, RelayMode.NOT_CONFIGURED, RelayMode.IDENTITY_MISMATCH, RelayMode.SETUP_IN_PROGRESS],
)
@pytest.mark.parametrize("kind", [NodeCommand.Kind.READ_NODE_INFORMATION, NodeCommand.Kind.STOP_PAIRING])
def test_reading_the_node_and_stopping_pairing_run_in_every_connected_mode(
    kind: NodeCommand.Kind, relay_mode: WorkerStatus.RelayMode
) -> None:
    assert find_node_command_refusal(build_command(kind), relay_mode, None) is None


@pytest.mark.parametrize(
    ("relay_mode", "is_allowed"),
    [
        (RelayMode.RUNNING, True),
        (RelayMode.NOT_CONFIGURED, True),
        (RelayMode.IDENTITY_MISMATCH, True),
        (RelayMode.SETUP_IN_PROGRESS, False),
    ],
)
def test_a_reboot_is_refused_only_during_setup(relay_mode: WorkerStatus.RelayMode, is_allowed: bool) -> None:
    refusal = find_node_command_refusal(build_command(NodeCommand.Kind.REBOOT_NODE), relay_mode, None)

    assert (refusal is None) == is_allowed


@pytest.mark.parametrize(
    ("kind", "run_state", "command_run_id", "is_allowed"),
    [
        (NodeCommand.Kind.FACTORY_RESET, NodeSetupRun.State.RESETTING, ACTIVE_SETUP_RUN_ID, True),
        (NodeCommand.Kind.FACTORY_RESET, NodeSetupRun.State.CONFIGURING, ACTIVE_SETUP_RUN_ID, False),
        (NodeCommand.Kind.FACTORY_RESET, NodeSetupRun.State.RESETTING, ACTIVE_SETUP_RUN_ID + 1, False),
        (NodeCommand.Kind.FACTORY_RESET, NodeSetupRun.State.RESETTING, None, False),
        (NodeCommand.Kind.CONFIGURE_NODE, NodeSetupRun.State.CONFIGURING, ACTIVE_SETUP_RUN_ID, True),
        (NodeCommand.Kind.CONFIGURE_NODE, NodeSetupRun.State.RESETTING, ACTIVE_SETUP_RUN_ID, False),
        (NodeCommand.Kind.CONFIGURE_NODE, NodeSetupRun.State.AWAITING_CONFIGURATION, ACTIVE_SETUP_RUN_ID, False),
    ],
)
def test_a_setup_command_runs_only_for_the_active_run_in_the_state_that_created_it(
    kind: NodeCommand.Kind, run_state: NodeSetupRun.State, command_run_id: int | None, is_allowed: bool
) -> None:
    active_run = ActiveSetupRunSummary(setup_run_id=ACTIVE_SETUP_RUN_ID, state=run_state, new_public_key="")

    refusal = find_node_command_refusal(
        build_command(kind, setup_run_id=command_run_id), RelayMode.SETUP_IN_PROGRESS, active_run
    )

    assert (refusal is None) == is_allowed
    if not is_allowed:
        assert refusal == NOT_ALLOWED_OUTSIDE_SETUP


@pytest.mark.parametrize("relay_mode", [RelayMode.NOT_CONFIGURED, RelayMode.IDENTITY_MISMATCH])
def test_a_setup_command_needs_setup_in_progress(relay_mode: WorkerStatus.RelayMode) -> None:
    active_run = ActiveSetupRunSummary(
        setup_run_id=ACTIVE_SETUP_RUN_ID, state=NodeSetupRun.State.RESETTING, new_public_key=""
    )
    command = build_command(NodeCommand.Kind.FACTORY_RESET, setup_run_id=ACTIVE_SETUP_RUN_ID)

    assert find_node_command_refusal(command, relay_mode, active_run) == NOT_ALLOWED_OUTSIDE_SETUP
