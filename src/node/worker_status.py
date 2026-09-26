"""The worker's live status as the panel sees it: offline detection and the banner region.

Only the worker writes worker_status (an upsert of the single row every 5 seconds); the web
only reads it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.utils import timezone

from directory.contacts import CONTACT_CAPACITY
from directory.models import Contact
from node.models import NodeSetupRun, WorkerStatus
from node.node_identity_backups import read_configured_node_identity_backup
from node.node_settings import (
    IncompleteNodeConfigurationError,
    NodeSettingKey,
    load_node_configuration,
    read_node_setting_value,
)
from node.setup_runs import was_configured_identity_restored_without_configuration

WORKER_OFFLINE_AFTER_SECONDS = 20
EXPECTED_FIRMWARE_PROTOCOL_VERSION = 13


class BannerKind(StrEnum):
    """The banners of every panel page, from the highest priority down."""

    NODE_CONFIGURATION_INCOMPLETE = "node_configuration_incomplete"
    WORKER_OFFLINE = "worker_offline"
    RELAY_DISCONNECTED = "relay_disconnected"
    IDENTITY_MISMATCH = "identity_mismatch"
    SETUP_REQUIRED = "setup_required"
    SETUP_IN_PROGRESS = "setup_in_progress"
    SETTINGS_DRIFT = "settings_drift"
    FIRMWARE_PROTOCOL_MISMATCH = "firmware_protocol_mismatch"
    CONTACT_SYNC_PROBLEM = "contact_sync_problem"


@dataclass(frozen=True, kw_only=True)
class Banner:
    kind: BannerKind
    title: str
    detail: str


def read_worker_status() -> WorkerStatus | None:
    return WorkerStatus.objects.filter(id=WorkerStatus.SINGLE_ROW_ID).first()


def is_worker_offline(worker_status: WorkerStatus | None, now: datetime) -> bool:
    """No row, no heartbeat, or a heartbeat older than WORKER_OFFLINE_AFTER_SECONDS."""
    if worker_status is None or worker_status.heartbeat_at is None:
        return True
    return now - worker_status.heartbeat_at > timedelta(seconds=WORKER_OFFLINE_AFTER_SECONDS)


def is_node_connected(worker_status: WorkerStatus | None, now: datetime) -> bool:
    """The worker is online and has finished a handshake with the node."""
    return (
        worker_status is not None
        and not is_worker_offline(worker_status, now)
        and worker_status.connection_state == WorkerStatus.ConnectionState.CONNECTED
    )


def upsert_worker_status(worker_status: WorkerStatus) -> None:
    """Write the single row (id 1) with every field of the given instance (the worker's status reporter)."""
    worker_status.id = WorkerStatus.SINGLE_ROW_ID
    every_field_but_the_id = [field.name for field in WorkerStatus._meta.concrete_fields if not field.primary_key]
    WorkerStatus.objects.bulk_create(
        [worker_status], update_conflicts=True, unique_fields=["id"], update_fields=every_field_but_the_id
    )


def collect_banners(now: datetime) -> list[Banner]:
    """The banners that apply now, in BannerKind order.

    While the worker is offline its status row is stale, so only the banners that come from the
    database itself are shown besides the offline one.
    """
    worker_status = read_worker_status()
    worker_is_online = not is_worker_offline(worker_status, now)
    active_setup_run = NodeSetupRun.objects.filter(is_active=True).first()

    banners: list[Banner] = []
    node_is_configured = append_node_configuration_banners(banners, active_setup_run)
    if worker_status is not None and worker_is_online:
        append_worker_banners(banners, worker_status, node_is_configured, active_setup_run)
    else:
        banners.append(build_worker_offline_banner(worker_status))
    append_contact_sync_banner(banners)
    return sorted(banners, key=lambda banner: list(BannerKind).index(banner.kind))


def append_node_configuration_banners(banners: list[Banner], active_setup_run: NodeSetupRun | None) -> bool:
    """Adds the setup banners and returns whether node_setting holds a complete configuration."""
    try:
        node_configuration = load_node_configuration()
    except IncompleteNodeConfigurationError as incomplete_configuration_error:
        banners.append(
            Banner(
                kind=BannerKind.NODE_CONFIGURATION_INCOMPLETE,
                title="The node configuration is incomplete",
                detail=str(incomplete_configuration_error),
            )
        )
        return False

    node_is_configured = node_configuration is not None
    if active_setup_run is not None:
        node_relays_until_the_reset = node_configuration is not None and not (
            was_configured_identity_restored_without_configuration(node_configuration.node_public_key)
        )
        banners.append(build_setup_in_progress_banner(active_setup_run, node_relays_until_the_reset))
    elif not node_is_configured:
        banners.append(
            Banner(
                kind=BannerKind.SETUP_REQUIRED,
                title="The node needs its initial setup",
                detail="The relay does not deliver messages until the setup wizard has configured the node.",
            )
        )
    return node_is_configured


def build_setup_in_progress_banner(active_setup_run: NodeSetupRun, node_relays_until_the_reset: bool) -> Banner:
    if node_relays_until_the_reset and is_before_the_factory_reset_confirmation(active_setup_run):
        relaying_description = "Relaying continues until you confirm the factory reset."
    else:
        relaying_description = "The relay does not deliver messages until it is completed."
    return Banner(
        kind=BannerKind.SETUP_IN_PROGRESS,
        title="Setup is in progress",
        detail=f"The setup wizard is at: {active_setup_run.get_state_display().lower()}. {relaying_description}",
    )


def is_before_the_factory_reset_confirmation(setup_run: NodeSetupRun) -> bool:
    """A run that has read the node goes back to reading it only after a confirmed reset that may have reached it."""
    if setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION:
        return True
    return setup_run.state == NodeSetupRun.State.READING_NODE and not setup_run.original_public_key


def build_worker_offline_banner(worker_status: WorkerStatus | None) -> Banner:
    if worker_status is None or worker_status.heartbeat_at is None:
        heartbeat_description = "The relay worker has never reported its status."
    else:
        last_heartbeat_at = timezone.localtime(worker_status.heartbeat_at)
        heartbeat_description = f"Its last heartbeat was at {last_heartbeat_at:%Y-%m-%d %H:%M:%S %Z}."
    return Banner(
        kind=BannerKind.WORKER_OFFLINE,
        title="The relay worker is offline",
        detail=f"{heartbeat_description} Nothing is sent or received until it runs again; see make relay-logs.",
    )


def append_worker_banners(
    banners: list[Banner],
    worker_status: WorkerStatus,
    node_is_configured: bool,
    active_setup_run: NodeSetupRun | None,
) -> None:
    if worker_status.connection_state != WorkerStatus.ConnectionState.CONNECTED:
        banners.append(build_relay_disconnected_banner(worker_status))
        return

    if worker_status.relay_mode == WorkerStatus.RelayMode.IDENTITY_MISMATCH:
        banners.append(build_identity_mismatch_banner(worker_status))
    is_unconfigured_node_with_the_relays_identity = (
        worker_status.relay_mode == WorkerStatus.RelayMode.NOT_CONFIGURED and node_is_configured
    )
    if is_unconfigured_node_with_the_relays_identity and active_setup_run is None:
        banners.append(build_unconfigured_restored_node_banner())

    has_uncorrected_drift = any(not drift_entry.get("corrected", False) for drift_entry in worker_status.settings_drift)
    if node_is_configured and active_setup_run is None and has_uncorrected_drift:
        drifted_keys = ", ".join(str(drift_entry.get("key", "?")) for drift_entry in worker_status.settings_drift)
        banners.append(
            Banner(
                kind=BannerKind.SETTINGS_DRIFT,
                title="The node's settings differ from the configured ones",
                detail=f"Drifted: {drifted_keys}. Use Re-apply configured settings on the Node page.",
            )
        )

    protocol_version = worker_status.node_protocol_version
    if protocol_version is not None and protocol_version != EXPECTED_FIRMWARE_PROTOCOL_VERSION:
        banners.append(
            Banner(
                kind=BannerKind.FIRMWARE_PROTOCOL_MISMATCH,
                title="Unexpected firmware protocol version",
                detail=f"The node speaks companion protocol {protocol_version}; the relay was built and tested "
                f"for {EXPECTED_FIRMWARE_PROTOCOL_VERSION}. Relaying continues, but check the firmware.",
            )
        )


def build_relay_disconnected_banner(worker_status: WorkerStatus) -> Banner:
    """The same text from one connection attempt to the next, so the panel does not announce it again.

    The connection state and the count of failed attempts change on every attempt; the Node
    page shows them.
    """
    detail_parts = [
        f"The relay worker keeps trying to connect ({worker_status.transport_description or 'no transport'})."
    ]
    if worker_status.last_error_message:
        detail_parts.append(f"Last error: {worker_status.last_error_message}")
    return Banner(kind=BannerKind.RELAY_DISCONNECTED, title="The node is not connected", detail=" ".join(detail_parts))


def build_identity_mismatch_banner(worker_status: WorkerStatus) -> Banner:
    configured_backup = read_configured_node_identity_backup()
    configured_public_key = configured_backup.configured_public_key
    configured_name = read_node_setting_value(NodeSettingKey.NODE_NAME)
    detail = (
        f"Attached: {worker_status.node_name or 'unnamed'} ({worker_status.node_public_key}). "
        f"Configured: {configured_name or 'unnamed'} ({configured_public_key or 'none'}). "
        "Reconnect the configured node, or set up the attached one."
    )
    if configured_backup.is_restorable:
        detail += (
            " The relay's identity is backed up: set the attached node up with \"Keep the relay's identity\" and it "
            "takes the relay's place; users need to do nothing."
        )
    return Banner(kind=BannerKind.IDENTITY_MISMATCH, title="The attached node is not the configured one", detail=detail)


def build_unconfigured_restored_node_banner() -> Banner:
    """The relay mode is not configured while node_setting is: a cancelled setup run restored the identity."""
    return Banner(
        kind=BannerKind.SETUP_REQUIRED,
        title="The node holds the relay's identity but was never configured",
        detail=(
            "A setup run was cancelled after it gave the reset node the relay's identity back, so the node still "
            "has its factory settings and no contacts. The relay does not deliver messages until a setup run is "
            "completed; keep the relay's identity in it and users need to do nothing."
        ),
    )


def append_contact_sync_banner(banners: list[Banner]) -> None:
    failed_contact_count = Contact.objects.filter(node_sync_state=Contact.NodeSyncState.ADD_FAILED).count()
    contact_count = Contact.objects.count()
    if failed_contact_count:
        contact_word = "contact" if failed_contact_count == 1 else "contacts"
        banners.append(
            Banner(
                kind=BannerKind.CONTACT_SYNC_PROBLEM,
                title="Some contacts could not be added to the node",
                detail=f"{failed_contact_count} {contact_word} failed to sync; the Contacts page shows why.",
            )
        )
    elif contact_count >= CONTACT_CAPACITY:
        banners.append(
            Banner(
                kind=BannerKind.CONTACT_SYNC_PROBLEM,
                title="The node's contact table is full",
                detail=f"All {CONTACT_CAPACITY} places are taken; delete a contact before adding another.",
            )
        )
