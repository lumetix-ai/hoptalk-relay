from datetime import UTC, datetime, timedelta

import pytest

from directory.models import Contact
from node.models import NodeSetting, NodeSetupRun, WorkerStatus
from node.node_settings import replace_node_configuration
from node.setup_runs import start_setup_run
from node.worker_status import (
    BannerKind,
    collect_banners,
    is_node_connected,
    is_worker_offline,
    read_worker_status,
    upsert_worker_status,
)
from tests.services.directory.row_builders import create_contact
from tests.services.node.node_builders import (
    ORIGINAL_NODE_PUBLIC_KEY,
    RESET_NODE_PUBLIC_KEY,
    build_node_configuration,
)

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
FOREIGN_NODE_PUBLIC_KEY = "99" * 32
RELAYING_CONTINUES = "Relaying continues until you confirm the factory reset."
RELAYING_STOPPED = "The relay does not deliver messages until it is completed."


def write_worker_status(
    heartbeat_at: datetime | None = NOW,
    relay_mode: WorkerStatus.RelayMode = WorkerStatus.RelayMode.RUNNING,
    connection_state: WorkerStatus.ConnectionState = WorkerStatus.ConnectionState.CONNECTED,
    **other_fields: object,
) -> WorkerStatus:
    worker_status = WorkerStatus(
        heartbeat_at=heartbeat_at, relay_mode=relay_mode, connection_state=connection_state, **other_fields
    )
    upsert_worker_status(worker_status)
    return worker_status


def collect_banner_kinds() -> list[BannerKind]:
    return [banner.kind for banner in collect_banners(NOW)]


def test_the_worker_is_offline_without_a_row_a_heartbeat_or_after_twenty_silent_seconds() -> None:
    assert is_worker_offline(None, NOW)
    assert is_worker_offline(WorkerStatus(heartbeat_at=None), NOW)
    assert not is_worker_offline(WorkerStatus(heartbeat_at=NOW - timedelta(seconds=20)), NOW)
    assert is_worker_offline(WorkerStatus(heartbeat_at=NOW - timedelta(seconds=21)), NOW)


def test_the_node_counts_as_connected_only_while_the_worker_is_online_and_connected() -> None:
    assert is_node_connected(WorkerStatus(heartbeat_at=NOW, connection_state="connected"), NOW)
    assert not is_node_connected(WorkerStatus(heartbeat_at=NOW, connection_state="handshaking"), NOW)
    assert not is_node_connected(
        WorkerStatus(heartbeat_at=NOW - timedelta(minutes=1), connection_state="connected"), NOW
    )


def test_the_upsert_keeps_a_single_row() -> None:
    write_worker_status(node_name="first")
    write_worker_status(node_name="second")

    assert WorkerStatus.objects.count() == 1
    worker_status = read_worker_status()
    assert worker_status is not None
    assert worker_status.node_name == "second"


def test_a_fresh_installation_shows_the_worker_offline_and_the_setup_required() -> None:
    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.WORKER_OFFLINE, BannerKind.SETUP_REQUIRED]
    assert "make relay-logs" in banners[0].detail


def test_a_running_configured_relay_shows_no_banner() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status()

    assert collect_banners(NOW) == []


def test_a_disconnected_node_hides_the_banners_that_need_a_connection() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status(
        relay_mode=WorkerStatus.RelayMode.DISCONNECTED,
        connection_state=WorkerStatus.ConnectionState.CONNECTING,
        transport_description="tcp host.docker.internal:5055",
        consecutive_connect_failures=4,
        last_error_message="Connection refused",
        node_protocol_version=12,
    )

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.RELAY_DISCONNECTED]
    assert "tcp host.docker.internal:5055" in banners[0].detail
    assert "Connection refused" in banners[0].detail


def test_the_disconnected_banner_stays_the_same_from_one_connection_attempt_to_the_next() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status(
        relay_mode=WorkerStatus.RelayMode.DISCONNECTED,
        connection_state=WorkerStatus.ConnectionState.CONNECTING,
        transport_description="tcp host.docker.internal:5055",
        consecutive_connect_failures=4,
        last_error_message="Connection refused",
    )
    banner_while_connecting = collect_banners(NOW)[0]

    write_worker_status(
        relay_mode=WorkerStatus.RelayMode.DISCONNECTED,
        connection_state=WorkerStatus.ConnectionState.DISCONNECTED,
        transport_description="tcp host.docker.internal:5055",
        consecutive_connect_failures=5,
        last_error_message="Connection refused",
    )

    assert collect_banners(NOW)[0] == banner_while_connecting


def test_an_identity_mismatch_names_both_nodes() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status(
        relay_mode=WorkerStatus.RelayMode.IDENTITY_MISMATCH,
        node_public_key=FOREIGN_NODE_PUBLIC_KEY,
        node_name="Stranger",
    )

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.IDENTITY_MISMATCH]
    assert FOREIGN_NODE_PUBLIC_KEY in banners[0].detail
    assert RESET_NODE_PUBLIC_KEY in banners[0].detail
    assert "HopTalk Relay" in banners[0].detail


def test_uncorrected_drift_and_another_firmware_protocol_are_reported() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status(
        settings_drift=[
            {"key": "radio.transmit_power_dbm", "expected": "22", "actual": "20", "corrected": False},
            {"key": "contacts.manual_add", "expected": "1", "actual": "0", "corrected": True},
        ],
        node_protocol_version=12,
    )

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.SETTINGS_DRIFT, BannerKind.FIRMWARE_PROTOCOL_MISMATCH]
    assert "radio.transmit_power_dbm" in banners[0].detail


def test_drift_that_was_corrected_at_once_is_not_a_banner() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status(settings_drift=[{"key": "contacts.manual_add", "corrected": True}])

    assert collect_banner_kinds() == []


def test_a_setup_run_in_progress_replaces_the_setup_required_banner() -> None:
    write_worker_status(relay_mode=WorkerStatus.RelayMode.NOT_CONFIGURED)
    start_setup_run(NOW)

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.SETUP_IN_PROGRESS]
    assert banners[0].detail == f"The setup wizard is at: reading the node. {RELAYING_STOPPED}"


@pytest.mark.parametrize(
    ("setup_run_state", "original_public_key", "new_public_key", "expected_relaying_description"),
    [
        (NodeSetupRun.State.READING_NODE, "", "", RELAYING_CONTINUES),
        (NodeSetupRun.State.AWAITING_RESET_CONFIRMATION, ORIGINAL_NODE_PUBLIC_KEY, "", RELAYING_CONTINUES),
        (NodeSetupRun.State.RESETTING, ORIGINAL_NODE_PUBLIC_KEY, "", RELAYING_STOPPED),
        (NodeSetupRun.State.READING_NODE, ORIGINAL_NODE_PUBLIC_KEY, "", RELAYING_STOPPED),
        (NodeSetupRun.State.AWAITING_CONFIGURATION, ORIGINAL_NODE_PUBLIC_KEY, RESET_NODE_PUBLIC_KEY, RELAYING_STOPPED),
        (NodeSetupRun.State.CONFIGURING, ORIGINAL_NODE_PUBLIC_KEY, RESET_NODE_PUBLIC_KEY, RELAYING_STOPPED),
    ],
)
def test_a_reconfiguration_keeps_relaying_until_the_factory_reset_is_confirmed(
    setup_run_state: NodeSetupRun.State,
    original_public_key: str,
    new_public_key: str,
    expected_relaying_description: str,
) -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status()
    setup_run = start_setup_run(NOW)
    NodeSetupRun.objects.filter(id=setup_run.pk).update(
        state=setup_run_state, original_public_key=original_public_key, new_public_key=new_public_key
    )

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.SETUP_IN_PROGRESS]
    assert banners[0].detail.endswith(expected_relaying_description)


def test_a_contact_that_failed_to_sync_is_reported_even_while_the_worker_is_offline() -> None:
    replace_node_configuration(build_node_configuration())
    create_contact(1, node_sync_state=Contact.NodeSyncState.ADD_FAILED)

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.WORKER_OFFLINE, BannerKind.CONTACT_SYNC_PROBLEM]
    assert banners[1].detail == "1 contact failed to sync; the Contacts page shows why."


def test_several_contacts_that_failed_to_sync_are_counted_in_the_plural() -> None:
    replace_node_configuration(build_node_configuration())
    write_worker_status()
    create_contact(1, node_sync_state=Contact.NodeSyncState.ADD_FAILED)
    create_contact(2, node_sync_state=Contact.NodeSyncState.ADD_FAILED)

    banners = collect_banners(NOW)

    assert [banner.kind for banner in banners] == [BannerKind.CONTACT_SYNC_PROBLEM]
    assert banners[0].detail == "2 contacts failed to sync; the Contacts page shows why."


def test_a_partial_node_setting_table_is_the_first_banner() -> None:
    replace_node_configuration(build_node_configuration())
    NodeSetting.objects.filter(key="node.name").delete()
    write_worker_status()

    banners = collect_banners(NOW)

    assert banners[0].kind == BannerKind.NODE_CONFIGURATION_INCOMPLETE
    assert "node.name missing" in banners[0].detail
