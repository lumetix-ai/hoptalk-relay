"""Turning stored values into what the panel shows: masks, levels, durations, ids as times, key prefixes."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from node.models import NodeCommand
from node.node_commands import read_progress_steps
from node.setup_runs import (
    CONFIGURE_NODE_STEP_LABELS,
    FACTORY_RESET_STEP_LABELS,
    ConfigureNodeStep,
    FactoryResetStep,
)

PUBLIC_KEY_PREFIX_LENGTH = 12
DIRECT_ARRIVAL_PATH_LENGTH = 255
FLOOD_ROUTE_PATH_LENGTH = -1
RECEIPT_LEVEL_NAMES = {0: "nothing", 1: "delivered", 2: "read"}
# Client message ids are microseconds since the Unix epoch when the client follows the
# recommendation; anything outside these years is shown as a plain number.
EARLIEST_PLAUSIBLE_MESSAGE_ID_TIME = datetime(2020, 1, 1, tzinfo=UTC)
LATEST_PLAUSIBLE_MESSAGE_ID_TIME = datetime(2100, 1, 1, tzinfo=UTC)
MICROSECONDS_PER_SECOND = 1_000_000


def format_parts_mask(parts_mask: int, part_count: int) -> str:
    """Bit n-1 is part n, written left to right like a received-set: 0b0101 of 4 parts is "1010"."""
    return "".join("1" if parts_mask & (1 << part_index) else "0" for part_index in range(part_count))


def format_receipt_level(receipt_level: int) -> str:
    return RECEIPT_LEVEL_NAMES.get(receipt_level, str(receipt_level))


def format_duration(duration: timedelta) -> str:
    """Whole seconds, minutes, hours and days, at most the two largest units: "3 min 5 s", "2 h 10 min"."""
    total_seconds = max(0, int(duration.total_seconds()))
    days, remaining_seconds = divmod(total_seconds, 86_400)
    hours, remaining_seconds = divmod(remaining_seconds, 3600)
    minutes, seconds = divmod(remaining_seconds, 60)
    units = [(days, "d"), (hours, "h"), (minutes, "min"), (seconds, "s")]
    shown_units = [f"{amount} {unit}" for amount, unit in units if amount]
    return " ".join(shown_units[:2]) or "0 s"


def convert_message_id_to_time(client_message_id: int) -> datetime | None:
    """The time a client message id encodes, or None when the id is not a plausible microsecond timestamp."""
    try:
        encoded_time = datetime.fromtimestamp(client_message_id / MICROSECONDS_PER_SECOND, tz=UTC)
    except OverflowError, OSError, ValueError:
        return None
    if EARLIEST_PLAUSIBLE_MESSAGE_ID_TIME <= encoded_time < LATEST_PLAUSIBLE_MESSAGE_ID_TIME:
        return encoded_time
    return None


def shorten_public_key(public_key: str) -> str:
    return public_key[:PUBLIC_KEY_PREFIX_LENGTH]


def describe_contact_route(node_out_path_length: int | None) -> str:
    """The node's stored route to a contact: flood, direct, or the hop count."""
    if node_out_path_length is None:
        return "unknown"
    if node_out_path_length == FLOOD_ROUTE_PATH_LENGTH:
        return "flood"
    if node_out_path_length == 0:
        return "direct"
    if node_out_path_length == 1:
        return "1 hop"
    return f"{node_out_path_length} hops"


def describe_inbound_route(path_length: int) -> str:
    """How a direct message arrived: over a stored route, or by flood over some hops."""
    if path_length == DIRECT_ARRIVAL_PATH_LENGTH:
        return "direct"
    if path_length == 1:
        return "flood, 1 hop"
    return f"flood, {path_length} hops"


def humanize_step_name(step_name: str) -> str:
    return step_name.replace("_", " ").capitalize()


@dataclass(frozen=True, kw_only=True)
class DisplayedProgressStep:
    label: str
    # A ProgressStepState value, or "waiting" for a known step that has not started yet.
    state: str
    detail: str
    at: datetime | None


WAITING_STEP_STATE = "waiting"


def build_displayed_progress_steps(node_command: NodeCommand) -> list[DisplayedProgressStep]:
    """Every step of the command: the known steps of a setup command in order, waiting ones included."""
    recorded_steps = {progress_step.step: progress_step for progress_step in read_progress_steps(node_command)}
    known_step_labels = find_known_step_labels(NodeCommand.Kind(node_command.kind))

    displayed_steps = []
    for step_name, step_label in known_step_labels.items():
        recorded_step = recorded_steps.pop(step_name, None)
        displayed_steps.append(
            DisplayedProgressStep(
                label=step_label,
                state=recorded_step.state.value if recorded_step else WAITING_STEP_STATE,
                detail=recorded_step.detail if recorded_step else "",
                at=recorded_step.at if recorded_step else None,
            )
        )
    for recorded_step in recorded_steps.values():
        displayed_steps.append(
            DisplayedProgressStep(
                label=humanize_step_name(recorded_step.step),
                state=recorded_step.state.value,
                detail=recorded_step.detail,
                at=recorded_step.at,
            )
        )
    return displayed_steps


def find_known_step_labels(kind: NodeCommand.Kind) -> dict[str, str]:
    if kind == NodeCommand.Kind.FACTORY_RESET:
        return {step.value: FACTORY_RESET_STEP_LABELS[step] for step in FactoryResetStep}
    if kind == NodeCommand.Kind.CONFIGURE_NODE:
        return {step.value: CONFIGURE_NODE_STEP_LABELS[step] for step in ConfigureNodeStep}
    return {}


BADGE_TONES_BY_STATE = {
    "success": {
        "succeeded",
        "done",
        "delivered",
        "confirmed",
        "on_node",
        "completed",
        "connected",
        "node_acknowledged",
        "processed",
        "performed",
        "request",
        "acknowledgement",
    },
    "warning": {
        "pending",
        "pending_add",
        "queued_for_refresh",
        "awaiting_reset_confirmation",
        "awaiting_configuration",
        "connecting",
        "handshaking",
        "setup_in_progress",
        "acknowledgement_timed_out",
        "outcome_unknown",
        "interrupted",
        "expired",
        "stopped",
        "received",
        "skipped",
        "not_configured",
        "not_protocol",
        "unsupported_version",
        "server_type_ignored",
        "unknown_sender",
        "unsupported_text_type",
        "unclassified",
    },
    "danger": {
        "failed",
        "add_failed",
        "rejected_by_node",
        "identity_mismatch",
        "syntax_error",
    },
    "information": {
        "running",
        "active",
        "reading_node",
        "resetting",
        "configuring",
        "prepared",
        "queued_on_node",
    },
}
NEUTRAL_BADGE_TONE = "neutral"


def find_badge_tone(state: str) -> str:
    """The colour family of a state badge: success, warning, danger, information or neutral."""
    for badge_tone, states in BADGE_TONES_BY_STATE.items():
        if state in states:
            return badge_tone
    return NEUTRAL_BADGE_TONE


RELAY_MODE_TONES = {
    "running": "success",
    "setup_in_progress": "information",
    "not_configured": "warning",
    "identity_mismatch": "danger",
    "disconnected": NEUTRAL_BADGE_TONE,
}


def find_relay_mode_tone(relay_mode: str) -> str:
    return RELAY_MODE_TONES.get(relay_mode, NEUTRAL_BADGE_TONE)
