"""The node dashboard: the relay's live state, the node, traffic gauges and the node actions."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from directory.contacts import CONTACT_CAPACITY
from directory.models import Contact, User
from hoptalk_relay.relay_settings import describe_effective_configuration, get_relay_settings
from messaging.models import Message, MessageDelivery
from node.models import NodeCommand, WorkerStatus
from node.node_commands import create_node_command
from node.node_identity_backups import (
    ConfiguredNodeIdentityBackup,
    NodeIdentityBackupStatus,
    read_configured_node_identity_backup,
)
from node.node_settings import IncompleteNodeConfigurationError, NodeConfiguration, load_node_configuration
from node.setup_runs import SetupRunTransitionError, start_setup_run
from node.worker_status import is_worker_offline, read_worker_status
from panel.node_action_availability import NodeActionAvailability, read_node_action_availability
from panel.presenters import DisplayedProgressStep, build_displayed_progress_steps, humanize_step_name
from panel.views.redirects import redirect_to_next_page

NODE_TEMPLATE = "panel/node.html"
RECENT_COMMAND_COUNT = 10
# The worker may be away or refuse a command, so the request is confirmed, not its outcome.
COMMAND_OUTCOME_HINT = "Recent commands on the Node page shows how it went."


@dataclass(frozen=True, kw_only=True)
class DisplayedCommand:
    node_command: NodeCommand
    displayed_steps: list[DisplayedProgressStep]


@dataclass(frozen=True, kw_only=True)
class ConfigurationValue:
    label: str
    value: str


@dataclass(frozen=True, kw_only=True)
class ConfigurationRow:
    key: str
    label: str
    unit: str


# The keys the worker reports in worker_status.effective_configuration, in the order the card
# shows them. A key the worker reports that is missing here is still shown, after these.
CONFIGURATION_ROWS = (
    ConfigurationRow(key="attempts_per_delivery", label="Attempts per delivery", unit=""),
    ConfigurationRow(key="first_pause_seconds", label="First pause", unit=" s"),
    ConfigurationRow(key="pause_multiplier", label="Pause multiplier", unit=""),
    ConfigurationRow(key="longest_pause_seconds", label="Longest pause", unit=" s"),
    ConfigurationRow(key="delivered_receipt_hold_back_seconds", label="Delivered receipt hold-back", unit=" s"),
    ConfigurationRow(key="packets_awaiting_a_firmware_ack", label="Packets awaiting a firmware ACK", unit=""),
    ConfigurationRow(key="gap_between_sends_seconds", label="Gap between sends", unit=" s"),
    ConfigurationRow(key="deliveries_in_progress_per_device", label="Deliveries in progress per device", unit=""),
    ConfigurationRow(key="traffic_log_kept_for_days", label="Traffic log kept for", unit=" days"),
)


@dataclass(frozen=True, kw_only=True)
class IdentityBackupSummary:
    """The dashboard's "Identity backup" line: never the key, only whether and when it is kept."""

    is_restorable: bool
    status_label: str
    # A badge tone of panel.presenters.
    tone: str
    taken_at: datetime | None
    detail: str


@dataclass(frozen=True, kw_only=True)
class NodeDashboard:
    worker_status: WorkerStatus | None
    worker_is_online: bool
    heartbeat_age: timedelta | None
    connected_for: timedelta | None
    node_configuration: NodeConfiguration | None
    node_configuration_error: str
    contact_count: int
    contact_capacity: int
    pending_contact_count: int
    user_count: int
    device_count: int
    messages_accepted_today: int
    undelivered_message_count: int
    failed_deliveries_last_day: int
    settings_drift: list[dict[str, Any]]
    effective_configuration: list[ConfigurationValue]
    effective_configuration_is_reported: bool
    recent_commands: list[DisplayedCommand]
    node_action_availability: NodeActionAvailability
    identity_backup: IdentityBackupSummary


def read_node_dashboard(now: datetime) -> NodeDashboard:
    worker_status = read_worker_status()
    try:
        node_configuration = load_node_configuration()
        node_configuration_error = ""
    except IncompleteNodeConfigurationError as incomplete_configuration_error:
        node_configuration = None
        node_configuration_error = str(incomplete_configuration_error)

    reported_configuration = worker_status.effective_configuration if worker_status else {}
    worker_is_online = not is_worker_offline(worker_status, now)
    start_of_today = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
    return NodeDashboard(
        worker_status=worker_status,
        worker_is_online=worker_is_online,
        heartbeat_age=now - worker_status.heartbeat_at if worker_status and worker_status.heartbeat_at else None,
        connected_for=calculate_connected_for(worker_status, worker_is_online, now),
        node_configuration=node_configuration,
        node_configuration_error=node_configuration_error,
        contact_count=Contact.objects.count(),
        contact_capacity=CONTACT_CAPACITY,
        pending_contact_count=Contact.objects.exclude(node_sync_state=Contact.NodeSyncState.ON_NODE).count(),
        user_count=User.objects.count(),
        device_count=Contact.objects.filter(user__isnull=False).count(),
        messages_accepted_today=Message.objects.filter(accepted_at__gte=start_of_today).count(),
        undelivered_message_count=Message.objects.filter(accepted_at__isnull=False, delivered_at__isnull=True).count(),
        failed_deliveries_last_day=MessageDelivery.objects.filter(failed_at__gte=now - timedelta(days=1)).count(),
        settings_drift=list(worker_status.settings_drift) if worker_status else [],
        effective_configuration=describe_configuration_values(
            reported_configuration or describe_effective_configuration(get_relay_settings())
        ),
        effective_configuration_is_reported=bool(reported_configuration),
        recent_commands=[
            DisplayedCommand(node_command=node_command, displayed_steps=build_displayed_progress_steps(node_command))
            for node_command in NodeCommand.objects.order_by("-id")[:RECENT_COMMAND_COUNT]
        ],
        node_action_availability=read_node_action_availability(node_is_configured=node_configuration is not None),
        identity_backup=summarize_identity_backup(read_configured_node_identity_backup(), worker_status),
    )


def summarize_identity_backup(
    configured_backup: ConfiguredNodeIdentityBackup, worker_status: WorkerStatus | None
) -> IdentityBackupSummary:
    """What the database holds comes first; only the worker knows why none could be taken."""
    backup_state = configured_backup.backup_state
    if configured_backup.is_restorable:
        return IdentityBackupSummary(
            is_restorable=True,
            status_label="Stored",
            tone="success",
            taken_at=backup_state.created_at,
            detail="Encrypted with SECRET_KEY. A reconfiguration, or a replacement board, keeps the relay's identity.",
        )
    if backup_state.status == NodeIdentityBackupStatus.UNREADABLE:
        return IdentityBackupSummary(
            is_restorable=False,
            status_label="Unreadable",
            tone="danger",
            taken_at=None,
            detail=f"{backup_state.unreadable_reason} The worker takes a new one when the configured node is attached.",
        )

    worker_backup_state = worker_status.node_identity_backup_state if worker_status else ""
    if worker_backup_state == WorkerStatus.NodeIdentityBackupState.EXPORT_DISABLED:
        detail = (
            "This node's firmware does not allow exporting its private key, so a reconfiguration gives the relay "
            "a new identity."
        )
    elif worker_backup_state == WorkerStatus.NodeIdentityBackupState.FAILED:
        detail = (
            "The last attempt to take it failed (see make relay-logs); the worker tries again at the next connection."
        )
    else:
        detail = "The worker takes it when the configured node is connected."
    return IdentityBackupSummary(
        is_restorable=False, status_label="Not stored", tone="warning", taken_at=None, detail=detail
    )


def calculate_connected_for(
    worker_status: WorkerStatus | None, worker_is_online: bool, now: datetime
) -> timedelta | None:
    """None while the worker is offline: a worker that crashed never wrote that the connection ended."""
    if worker_status is None or worker_status.connected_since is None or not worker_is_online:
        return None
    return now - worker_status.connected_since


def describe_configuration_values(configuration_values: Mapping[str, Any]) -> list[ConfigurationValue]:
    known_keys = {configuration_row.key for configuration_row in CONFIGURATION_ROWS}
    described_values = [
        ConfigurationValue(
            label=configuration_row.label,
            value=format_configuration_value(configuration_values[configuration_row.key], configuration_row.unit),
        )
        for configuration_row in CONFIGURATION_ROWS
        if configuration_row.key in configuration_values
    ]
    described_values.extend(
        ConfigurationValue(label=humanize_step_name(key), value=format_configuration_value(value, unit=""))
        for key, value in configuration_values.items()
        if key not in known_keys
    )
    return described_values


def format_configuration_value(value: Any, unit: str) -> str:
    """2.0 becomes "2" and 30.0 seconds "30 s", as the values are written in src/.env."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return f"{value}{unit}"
    return f"{value:g}{unit}"


@require_GET
def show_node_dashboard(request: HttpRequest) -> HttpResponse:
    return render(request, NODE_TEMPLATE, {"dashboard": read_node_dashboard(timezone.now())})


@require_GET
def show_node_status(request: HttpRequest) -> HttpResponse:
    return render(request, f"{NODE_TEMPLATE}#node_status", {"dashboard": read_node_dashboard(timezone.now())})


def queue_node_command(
    request: HttpRequest, kind: NodeCommand.Kind, arguments: dict[str, Any], confirmation: str
) -> HttpResponse:
    create_node_command(kind, arguments, timezone.now())
    messages.success(request, f"{confirmation} {COMMAND_OUTCOME_HINT}")
    return redirect_to_next_page(request, "panel:node_dashboard")


@require_POST
def send_advert(request: HttpRequest) -> HttpResponse:
    advert_flood = request.POST.get("flood") == "1"
    route_description = "flood" if advert_flood else "zero-hop"
    return queue_node_command(
        request,
        NodeCommand.Kind.SEND_ADVERT,
        {"flood": advert_flood},
        f"A {route_description} advert was requested from the relay worker.",
    )


@require_POST
def reboot_node(request: HttpRequest) -> HttpResponse:
    return queue_node_command(
        request, NodeCommand.Kind.REBOOT_NODE, {}, "The reboot was requested; it runs within a minute or not at all."
    )


@require_POST
def reapply_settings(request: HttpRequest) -> HttpResponse:
    return queue_node_command(
        request,
        NodeCommand.Kind.APPLY_CONFIGURED_SETTINGS,
        {},
        "Re-applying the configured settings was requested from the relay worker.",
    )


@require_POST
def regenerate_contact_card(request: HttpRequest) -> HttpResponse:
    return queue_node_command(
        request, NodeCommand.Kind.EXPORT_CONTACT_CARD, {}, "A new contact card was requested from the node."
    )


@require_POST
def sync_contacts(request: HttpRequest) -> HttpResponse:
    return queue_node_command(
        request,
        NodeCommand.Kind.RECONCILE_CONTACTS,
        {},
        "Synchronising the contacts was requested from the relay worker.",
    )


@require_POST
def reconfigure_node(request: HttpRequest) -> HttpResponse:
    try:
        start_setup_run(timezone.now())
    except SetupRunTransitionError as transition_error:
        messages.error(request, str(transition_error))
    return redirect("panel:setup")
