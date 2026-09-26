import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import Client
from django.utils import timezone
from pytest_django import Settings

from directory.models import Contact
from hoptalk_relay.relay_settings import describe_effective_configuration
from node.models import NodeCommand, NodeSetting, NodeSetupRun, WorkerStatus
from node.node_commands import (
    ProgressStep,
    ProgressStepState,
    claim_next_node_command,
    create_node_command,
    finish_node_command,
    record_node_command_progress,
)
from node.node_identity_backups import store_node_identity_backup
from node.node_settings import OptionalNodeSettingKey, replace_node_configuration
from node.setup_runs import start_setup_run
from node.worker_status import upsert_worker_status
from tests.panel.panel_client import find_button_opening_tag, get_page, get_partial, post_form
from tests.private_key_checks import mentions_private_key
from tests.services.directory.row_builders import create_contact, create_user
from tests.services.node.node_builders import RESET_NODE_KEY_PAIR, RESET_NODE_PUBLIC_KEY, build_node_configuration

pytestmark = pytest.mark.django_db

HTMX_STOP_POLLING = 286
RELAY_ACTION_BUTTON_LABELS = (
    "Send advert (zero-hop)",
    "Send advert (flood)",
    "Regenerate contact card",
    "Sync contacts now",
    "Re-apply configured settings",
)


def report_running_worker(**other_fields: object) -> None:
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.RUNNING,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            connected_since=timezone.now(),
            transport_description="tcp host.docker.internal:5055",
            node_public_key=RESET_NODE_PUBLIC_KEY,
            node_contact_count=2,
            node_clock_offset_seconds=3,
            node_radio_summary="916.575 MHz / SF7 / BW62.5 / CR7",
            packets_awaiting_node_acknowledgement=3,
            replies_queued=1,
            **other_fields,
        )
    )


def test_the_dashboard_explains_an_offline_worker_and_a_node_that_is_not_set_up(signed_in_client: Client) -> None:
    response = get_page(signed_in_client, "/node")
    page = response.content.decode()

    assert response.status_code == 200
    assert "Worker offline" in page
    assert "never reported" in page
    assert "The node has not been set up yet." in page
    assert "No command has been sent to the node yet." in page
    assert "from src/.env" in page


def test_the_dashboard_shows_the_relay_the_node_and_the_traffic(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    report_running_worker(effective_configuration={"retry_maximum_attempts": 6})
    create_contact(1, user=create_user("ivan"))
    create_contact(2, node_sync_state=Contact.NodeSyncState.PENDING_ADD)

    page = get_page(signed_in_client, "/node").content.decode()

    assert "Running" in page
    assert RESET_NODE_PUBLIC_KEY in page
    assert "916.575 MHz / SF7 / BW62.5 / CR7" in page
    assert "2 on the node · 2 in the database · capacity 350" in page
    assert "1 not on the node" in page
    assert "3 s from the server" in page
    assert "Retry maximum attempts" in page
    assert "as the worker runs it" in page


def test_settings_drift_is_listed_with_the_reapply_button(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    report_running_worker(
        settings_drift=[{"key": "radio.transmit_power_dbm", "expected": "22", "actual": "20", "corrected": False}]
    )

    page = get_page(signed_in_client, "/node").content.decode()

    assert "Settings that differ from the configuration" in page
    assert "radio.transmit_power_dbm" in page


def test_the_status_cards_are_polled_every_three_seconds_for_as_long_as_the_page_is_open(
    signed_in_client: Client,
) -> None:
    status_response = get_partial(signed_in_client, "/node/partials/status")

    assert status_response.status_code == 200
    assert 'hx-trigger="every 3s"' in status_response.content.decode()
    assert "<html" not in status_response.content.decode()


@pytest.mark.parametrize(
    ("action_path", "form_data", "expected_kind", "expected_arguments"),
    [
        pytest.param("/node/actions/advert", {"flood": "0"}, "send_advert", {"flood": False}, id="zero-hop advert"),
        pytest.param("/node/actions/advert", {"flood": "1"}, "send_advert", {"flood": True}, id="flood advert"),
        pytest.param("/node/actions/reboot", {}, "reboot_node", {}, id="reboot"),
        pytest.param("/node/actions/reapply-settings", {}, "apply_configured_settings", {}, id="reapply"),
        pytest.param("/node/actions/regenerate-card", {}, "export_contact_card", {}, id="regenerate card"),
        pytest.param("/node/actions/sync-contacts", {}, "reconcile_contacts", {}, id="sync contacts"),
    ],
)
def test_every_action_queues_one_command_and_redirects_back_to_the_dashboard(
    signed_in_client: Client,
    action_path: str,
    form_data: dict[str, str],
    expected_kind: str,
    expected_arguments: dict[str, object],
) -> None:
    response = post_form(signed_in_client, action_path, form_data)

    assert response.status_code == 302
    assert response["Location"] == "/node"
    node_command = NodeCommand.objects.get()
    assert node_command.kind == expected_kind
    assert node_command.arguments == expected_arguments


def test_the_dashboard_confirms_the_request_and_points_to_where_its_outcome_appears(signed_in_client: Client) -> None:
    post_form(signed_in_client, "/node/actions/advert", {"flood": "1"})

    page = get_page(signed_in_client, "/node").content.decode()
    assert (
        "A flood advert was requested from the relay worker. Recent commands on the Node page shows how it went."
        in page
    )


def test_an_action_returns_to_the_panel_page_it_came_from_but_never_elsewhere(signed_in_client: Client) -> None:
    assert (
        post_form(signed_in_client, "/node/actions/regenerate-card", {"next": "/contacts"})["Location"] == "/contacts"
    )
    assert (
        post_form(signed_in_client, "/node/actions/regenerate-card", {"next": "https://example.org/"})["Location"]
        == "/node"
    )


def test_reconfigure_starts_a_setup_run_and_opens_the_wizard(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())

    response = post_form(signed_in_client, "/node/actions/reconfigure")

    assert response["Location"] == "/setup"
    assert NodeSetupRun.objects.get().purpose == NodeSetupRun.Purpose.RECONFIGURE


def test_the_dashboard_offers_the_actions_with_confirmation_dialogs_for_reboot_and_reconfigure(
    signed_in_client: Client,
) -> None:
    page = get_page(signed_in_client, "/node").content.decode()

    assert 'commandfor="reboot-dialog"' in page
    assert '<dialog id="reboot-dialog"' in page
    assert 'commandfor="reconfigure-dialog"' in page
    assert "every user must add the new contact card" in page


def test_a_running_command_shows_its_steps_among_the_recent_commands(signed_in_client: Client) -> None:
    report_running_worker()
    create_node_command(NodeCommand.Kind.REBOOT_NODE, {}, timezone.now())
    running_command = claim_next_node_command(uuid4(), timezone.now())
    assert running_command is not None
    record_node_command_progress(
        running_command.pk,
        ProgressStep(step="wait_for_reconnect", state=ProgressStepState.RUNNING, detail="", at=timezone.now()),
    )

    status = get_partial(signed_in_client, "/node/partials/status").content.decode()

    assert "Reboot node" in status
    assert "Wait for reconnect" in status


def test_command_progress_is_polled_until_the_command_is_over(signed_in_client: Client) -> None:
    node_command = create_node_command(NodeCommand.Kind.SEND_ADVERT, {"flood": False}, timezone.now())
    progress_path = f"/commands/{node_command.pk}/partials/progress"

    pending_response = get_partial(signed_in_client, progress_path)
    assert pending_response.status_code == 200
    assert 'hx-trigger="every 1s"' in pending_response.content.decode()

    claim_next_node_command(uuid4(), timezone.now())
    finish_node_command(node_command.pk, NodeCommand.State.FAILED, timezone.now(), error_message="Not connected.")

    finished_response = get_partial(signed_in_client, progress_path)
    assert finished_response.status_code == HTMX_STOP_POLLING
    assert 'hx-trigger="every 1s"' not in finished_response.content.decode()
    assert "Not connected." in finished_response.content.decode()
    assert "HX-Refresh" not in finished_response


def test_a_finished_command_can_reload_the_page_that_waited_for_it(signed_in_client: Client) -> None:
    node_command = create_node_command(NodeCommand.Kind.START_PAIRING, {}, timezone.now())
    claim_next_node_command(uuid4(), timezone.now())
    finish_node_command(node_command.pk, NodeCommand.State.SUCCEEDED, timezone.now())

    response = get_partial(
        signed_in_client, f"/commands/{node_command.pk}/partials/progress?refresh_page_when_finished=1"
    )

    assert response.status_code == HTMX_STOP_POLLING
    assert response["HX-Refresh"] == "true"


def test_the_progress_of_an_unknown_command_is_not_found(signed_in_client: Client) -> None:
    assert get_partial(signed_in_client, "/commands/999/partials/progress").status_code == 404


def find_action_button(page: str, button_label: str) -> str:
    return find_button_opening_tag(page[page.index('id="actions-card-title"') :], button_label)


def test_the_configuration_card_shows_the_reported_values_in_order_with_their_units(signed_in_client: Client) -> None:
    reported_values = describe_effective_configuration(settings.RELAY_SETTINGS)
    report_running_worker(effective_configuration=dict(reversed(list(reported_values.items()))))

    page = get_page(signed_in_client, "/node").content.decode()
    configuration_card = page[page.index('id="configuration-card-title"') : page.index('id="commands-card-title"')]
    shown_labels = re.findall(r"<dt>(.*?)</dt>", configuration_card)

    assert shown_labels == [
        "Attempts per delivery",
        "First pause",
        "Pause multiplier",
        "Longest pause",
        "Delivered receipt hold-back",
        "Packets awaiting a firmware ACK",
        "Gap between sends",
        "Deliveries in progress per device",
        "Traffic log kept for",
    ]
    shown_values = re.findall(r"<dd[^>]*>\s*(.*?)\s*</dd>", configuration_card, flags=re.DOTALL)
    retry_strategy = settings.RELAY_SETTINGS.retry_strategy
    assert shown_values[1] == f"{retry_strategy.initial_pause_seconds:g} s"
    assert shown_values[2] == f"{retry_strategy.backoff_multiplier:g}"
    assert not any(".0" in shown_value for shown_value in shown_values)


def test_before_the_worker_reports_the_configuration_card_shows_the_src_env_values(signed_in_client: Client) -> None:
    page = get_page(signed_in_client, "/node").content.decode()
    configuration_card = page[page.index('id="configuration-card-title"') : page.index('id="commands-card-title"')]
    shown_values = re.findall(r"<dd[^>]*>\s*(.*?)\s*</dd>", configuration_card, flags=re.DOTALL)

    assert "from src/.env" in configuration_card
    assert shown_values[0] == str(settings.RELAY_SETTINGS.retry_strategy.maximum_attempts)
    assert len(shown_values) == len(describe_effective_configuration(settings.RELAY_SETTINGS))


def test_a_worker_that_went_offline_while_connected_shows_no_connection_time(signed_in_client: Client) -> None:
    report_running_worker()
    WorkerStatus.objects.update(
        heartbeat_at=timezone.now() - timedelta(minutes=7),
        connected_since=timezone.now() - timedelta(hours=2, minutes=48),
    )

    status = get_partial(signed_in_client, "/node/partials/status").content.decode()

    assert "Worker offline" in status
    assert "unknown: the worker is offline" in status
    assert "for 2 h" not in status


def test_a_running_command_of_an_offline_worker_shows_no_spinning_step(signed_in_client: Client) -> None:
    create_node_command(NodeCommand.Kind.REBOOT_NODE, {}, timezone.now())
    running_command = claim_next_node_command(uuid4(), timezone.now())
    assert running_command is not None
    record_node_command_progress(
        running_command.pk,
        ProgressStep(step="wait_for_reconnect", state=ProgressStepState.RUNNING, detail="", at=timezone.now()),
    )

    status = get_partial(signed_in_client, "/node/partials/status").content.decode()

    assert "Waiting for the relay worker, which is offline." in status
    assert "Wait for reconnect" not in status


def test_before_setup_only_the_reboot_is_offered(signed_in_client: Client) -> None:
    page = get_page(signed_in_client, "/node").content.decode()

    assert "Available once the node is set up." in page
    for button_label in RELAY_ACTION_BUTTON_LABELS:
        assert " disabled" in find_action_button(page, button_label), button_label
    assert " disabled" not in find_action_button(page, "Reboot node")


def test_while_setup_owns_the_node_no_action_is_offered(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    setup_run = start_setup_run(timezone.now())
    NodeSetupRun.objects.filter(id=setup_run.pk).update(state=NodeSetupRun.State.RESETTING)

    page = get_page(signed_in_client, "/node").content.decode()

    assert "Not available while setup is in progress." in page
    for button_label in (*RELAY_ACTION_BUTTON_LABELS, "Reboot node"):
        assert " disabled" in find_action_button(page, button_label), button_label


def test_a_configured_node_offers_every_action_even_before_a_reconfiguration_resets_it(
    signed_in_client: Client,
) -> None:
    replace_node_configuration(build_node_configuration())
    start_setup_run(timezone.now())

    page = get_page(signed_in_client, "/node").content.decode()

    for button_label in (*RELAY_ACTION_BUTTON_LABELS, "Reboot node"):
        assert " disabled" not in find_action_button(page, button_label), button_label


def test_the_reapply_button_of_the_polled_drift_card_keeps_its_id_for_the_focus(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    report_running_worker(
        settings_drift=[{"key": "radio.transmit_power_dbm", "expected": "22", "actual": "20", "corrected": False}]
    )

    assert 'id="reapply-settings-button"' in get_partial(signed_in_client, "/node/partials/status").content.decode()


# ----- the identity backup -------------------------------------------------------------------------

CONFIGURED_PRIVATE_KEY = RESET_NODE_KEY_PAIR.private_key
BACKED_UP_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def read_dashboard_text(client: Client) -> str:
    return get_page(client, "/node").content.decode().replace("&#x27;", "'")


def configure_node_with_a_backup() -> None:
    replace_node_configuration(build_node_configuration())
    store_node_identity_backup(RESET_NODE_PUBLIC_KEY, CONFIGURED_PRIVATE_KEY, BACKED_UP_AT)


def test_a_stored_identity_backup_is_shown_with_its_date_and_never_with_the_key(signed_in_client: Client) -> None:
    configure_node_with_a_backup()
    report_running_worker(node_identity_backup_state=WorkerStatus.NodeIdentityBackupState.STORED)

    page = read_dashboard_text(signed_in_client)
    status_partial = get_partial(signed_in_client, "/node/partials/status").content.decode()

    assert "Identity backup" in page
    assert "Stored" in page
    assert "taken 20 Sep 2026" in page
    assert "Encrypted with SECRET_KEY" in page
    key_is_shown = mentions_private_key(page, CONFIGURED_PRIVATE_KEY) or mentions_private_key(
        status_partial, CONFIGURED_PRIVATE_KEY
    )
    assert not key_is_shown


def test_with_a_stored_backup_the_reconfiguration_says_users_need_to_do_nothing(signed_in_client: Client) -> None:
    configure_node_with_a_backup()
    report_running_worker()

    page = read_dashboard_text(signed_in_client)

    assert "The relay's identity is backed up, so the node can get it back and users need to do nothing." in page
    assert "users need to do nothing</strong>" in page
    assert "every user must add the new contact card" not in page


def test_without_a_backup_the_reconfiguration_warns_that_every_user_adds_the_new_card(
    signed_in_client: Client,
) -> None:
    replace_node_configuration(build_node_configuration())
    report_running_worker()

    page = read_dashboard_text(signed_in_client)

    assert "it gets a new identity and every user must add the new contact card" in page
    assert "every user must add the new contact card</strong>" in page


@pytest.mark.parametrize(
    ("worker_backup_state", "expected_detail"),
    [
        (WorkerStatus.NodeIdentityBackupState.EXPORT_DISABLED, "does not allow exporting its private key"),
        (WorkerStatus.NodeIdentityBackupState.FAILED, "The last attempt to take it failed"),
        (WorkerStatus.NodeIdentityBackupState.NOT_CHECKED, "The worker takes it when the configured node is connected"),
    ],
)
def test_a_missing_backup_is_shown_with_what_the_worker_knows_about_it(
    signed_in_client: Client, worker_backup_state: WorkerStatus.NodeIdentityBackupState, expected_detail: str
) -> None:
    replace_node_configuration(build_node_configuration())
    report_running_worker(node_identity_backup_state=worker_backup_state)

    page = read_dashboard_text(signed_in_client)

    assert "Not stored" in page
    assert expected_detail in page


def test_an_unreadable_backup_is_shown_with_the_reason(signed_in_client: Client, settings: Settings) -> None:
    current_secret_key = settings.SECRET_KEY
    settings.SECRET_KEY = "the secret key src/.env held when the backup was taken"
    configure_node_with_a_backup()
    settings.SECRET_KEY = current_secret_key
    report_running_worker()

    page = read_dashboard_text(signed_in_client)

    assert "Unreadable" in page
    assert "SECRET_KEY in src/.env changed since it was taken" in page
    assert "The worker takes a new one when the configured node is attached." in page


@pytest.mark.parametrize(
    "damaged_value",
    [
        pytest.param('{"version": 1e400}', id="infinite_version"),
        pytest.param("[" * 100_000 + "]" * 100_000, id="deeply_nested"),
    ],
)
def test_a_damaged_backup_is_shown_as_unreadable_instead_of_breaking_the_page(
    signed_in_client: Client, damaged_value: str
) -> None:
    replace_node_configuration(build_node_configuration())
    NodeSetting.objects.create(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value, value=damaged_value)
    report_running_worker()

    response = get_page(signed_in_client, "/node")

    assert response.status_code == 200
    assert "Unreadable" in response.content.decode()
    assert "The stored identity backup is damaged." in response.content.decode()
