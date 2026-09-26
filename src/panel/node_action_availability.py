"""Which node actions the panel offers, judged from what the database says about setup.

The worker refuses every action but a reboot before the node is set up, and every action while a
setup run owns the node. The panel greys those buttons out instead of queueing commands that can
only fail. It does not try to predict an identity mismatch: only the worker's live status knows
which node is attached, and that status is stale whenever the worker is offline.
"""

from dataclasses import dataclass

from node.models import NodeSetupRun
from node.setup_runs import get_active_setup_run

NOT_AVAILABLE_BEFORE_SETUP = "Available once the node is set up."
NOT_AVAILABLE_DURING_SETUP = "Not available while setup is in progress."

# Before its factory reset a run leaves the configured node relaying, so the actions still work.
SETUP_RUN_STATES_OWNING_THE_NODE = frozenset({NodeSetupRun.State.RESETTING, NodeSetupRun.State.CONFIGURING})


@dataclass(frozen=True, kw_only=True)
class NodeActionAvailability:
    """Why an action is unavailable, or "" when the worker would run it."""

    relay_action_unavailable_reason: str
    reboot_unavailable_reason: str

    @property
    def relay_actions_are_available(self) -> bool:
        return not self.relay_action_unavailable_reason

    @property
    def reboot_is_available(self) -> bool:
        return not self.reboot_unavailable_reason


def is_node_owned_by_setup(active_setup_run: NodeSetupRun | None) -> bool:
    """From the factory reset on, including the pause between a finished reset and the configuration."""
    if active_setup_run is None:
        return False
    if active_setup_run.state in SETUP_RUN_STATES_OWNING_THE_NODE:
        return True
    return bool(active_setup_run.new_public_key)


def read_node_action_availability(node_is_configured: bool) -> NodeActionAvailability:
    if is_node_owned_by_setup(get_active_setup_run()):
        return NodeActionAvailability(
            relay_action_unavailable_reason=NOT_AVAILABLE_DURING_SETUP,
            reboot_unavailable_reason=NOT_AVAILABLE_DURING_SETUP,
        )
    if not node_is_configured:
        return NodeActionAvailability(
            relay_action_unavailable_reason=NOT_AVAILABLE_BEFORE_SETUP,
            reboot_unavailable_reason="",
        )
    return NodeActionAvailability(relay_action_unavailable_reason="", reboot_unavailable_reason="")
