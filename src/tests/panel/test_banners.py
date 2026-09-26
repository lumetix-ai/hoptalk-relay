from datetime import UTC, datetime

import pytest
from django.test import Client
from django.utils import timezone

from node.models import NodeSetupRun, WorkerStatus
from node.node_identity_backups import store_node_identity_backup
from node.node_settings import replace_node_configuration
from node.worker_status import upsert_worker_status
from tests.panel.panel_client import get_partial
from tests.services.node.node_builders import RESET_NODE_KEY_PAIR, RESET_NODE_PUBLIC_KEY, build_node_configuration

pytestmark = pytest.mark.django_db


def test_the_banner_region_shows_the_worker_offline_and_the_way_to_setup(signed_in_client: Client) -> None:
    response = get_partial(signed_in_client, "/partials/banners")
    banners = response.content.decode()

    assert response.status_code == 200
    assert "The relay worker is offline" in banners
    assert 'role="alert"' in banners
    assert "The node needs its initial setup" in banners
    assert 'href="/setup"' in banners
    assert banners.index("offline") < banners.index("initial setup")


def test_an_identity_mismatch_banner_offers_to_set_up_the_attached_node(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.IDENTITY_MISMATCH,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            node_public_key="99" * 32,
            node_name="Stranger",
        )
    )

    banners = get_partial(signed_in_client, "/partials/banners").content.decode()

    assert "The attached node is not the configured one" in banners
    assert "Set up this node" in banners
    assert "The relay&#x27;s identity is backed up" not in banners


def test_an_identity_mismatch_banner_says_a_backed_up_identity_can_move_to_the_attached_node(
    signed_in_client: Client,
) -> None:
    replace_node_configuration(build_node_configuration())
    store_node_identity_backup(
        RESET_NODE_PUBLIC_KEY, RESET_NODE_KEY_PAIR.private_key, datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    )
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.IDENTITY_MISMATCH,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            node_public_key="99" * 32,
            node_name="Replacement board",
        )
    )

    banners = get_partial(signed_in_client, "/partials/banners").content.decode().replace("&#x27;", "'")

    assert "The relay's identity is backed up" in banners
    assert "users need to do nothing" in banners


def test_a_healthy_relay_has_an_empty_banner_region(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.RUNNING,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
        )
    )

    assert get_partial(signed_in_client, "/partials/banners").content.decode().strip() == ""


def record_an_unconfigured_node_with_the_relays_identity() -> None:
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.NOT_CONFIGURED,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            node_public_key=RESET_NODE_PUBLIC_KEY,
            node_name="C8C2622C",
        )
    )


def test_a_node_left_with_the_relays_identity_by_a_cancelled_setup_asks_for_setup(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    record_an_unconfigured_node_with_the_relays_identity()

    banners = get_partial(signed_in_client, "/partials/banners").content.decode().replace("&#x27;", "'")

    assert "The node holds the relay's identity but was never configured" in banners
    assert "does not deliver messages until a setup run is completed" in banners
    assert "Start setup" in banners
    assert "The attached node is not the configured one" not in banners


def test_a_new_setup_run_on_that_node_does_not_claim_the_relay_keeps_delivering(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    NodeSetupRun.objects.create(
        purpose=NodeSetupRun.Purpose.RECONFIGURE,
        state=NodeSetupRun.State.ABANDONED,
        started_at=timezone.now(),
        finished_at=timezone.now(),
        is_active=False,
        new_public_key="33" * 32,
        restored_public_key=RESET_NODE_PUBLIC_KEY,
    )
    NodeSetupRun.objects.create(
        purpose=NodeSetupRun.Purpose.RECONFIGURE,
        state=NodeSetupRun.State.AWAITING_RESET_CONFIRMATION,
        started_at=timezone.now(),
        is_active=True,
    )
    record_an_unconfigured_node_with_the_relays_identity()

    banners = get_partial(signed_in_client, "/partials/banners").content.decode()

    assert "Setup is in progress" in banners
    assert "Relaying continues until you confirm the factory reset" not in banners
    assert "The relay does not deliver messages until it is completed" in banners
    assert "was never configured" not in banners
