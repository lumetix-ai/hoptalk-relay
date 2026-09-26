import re
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone
from pytest_django import Settings

from node.models import NodeCommand, NodeSetupRun, WorkerStatus
from node.node_commands import (
    ProgressStep,
    ProgressStepState,
    claim_next_node_command,
    finish_node_command,
    record_node_command_progress,
)
from node.node_identity_backups import store_node_identity_backup
from node.node_settings import load_node_configuration, replace_node_configuration
from node.setup_runs import (
    ConfigureNodeStep,
    FactoryResetStep,
    RequestedNodeConfiguration,
    cancel_setup_run,
    get_active_setup_run,
    record_configuration_completed,
    record_factory_reset_succeeded,
    record_identity_restore_started,
    record_node_information_read,
    start_setup_run,
    submit_node_configuration,
)
from node.worker_status import upsert_worker_status
from tests.panel.panel_client import get_page, get_partial, post_form
from tests.services.node.node_builders import (
    ORIGINAL_NODE_KEY_PAIR,
    ORIGINAL_NODE_PUBLIC_KEY,
    RESET_NODE_PUBLIC_KEY,
    SAMPLE_CONTACT_CARD_URI,
    build_node_configuration,
    build_node_information,
)

pytestmark = pytest.mark.django_db

HTMX_STOP_POLLING = 286
WORKER_INSTANCE_ID = uuid4()
VALID_CONFIGURATION_FORM = {
    "node_name": "HopTalk Relay",
    "radio_preset": "Australia (Narrow)",
    "frequency_megahertz": "916.575",
    "bandwidth_kilohertz": "62.5",
    "spreading_factor": "7",
    "coding_rate": "7",
    "path_hash_size": "2",
    "transmit_power_dbm": "20",
}


def report_worker(connected: bool = True) -> None:
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.NOT_CONFIGURED if connected else WorkerStatus.RelayMode.DISCONNECTED,
            connection_state=(
                WorkerStatus.ConnectionState.CONNECTED if connected else WorkerStatus.ConnectionState.CONNECTING
            ),
            transport_description="tcp host.docker.internal:5055",
            last_error_message="" if connected else "Connection refused",
        )
    )


def claim_setup_command() -> NodeCommand:
    node_command = claim_next_node_command(WORKER_INSTANCE_ID, timezone.now())
    assert node_command is not None
    return node_command


def prepare_run_awaiting_reset_confirmation() -> NodeSetupRun:
    setup_run = start_setup_run(timezone.now())
    read_command = claim_setup_command()
    record_node_information_read(setup_run.pk, build_node_information().to_json(), timezone.now())
    finish_node_command(read_command.pk, NodeCommand.State.SUCCEEDED, timezone.now())
    setup_run.refresh_from_db()
    return setup_run


def prepare_run_awaiting_configuration() -> NodeSetupRun:
    setup_run = prepare_run_awaiting_reset_confirmation()
    NodeSetupRun.objects.filter(id=setup_run.pk).update(state=NodeSetupRun.State.RESETTING)
    record_factory_reset_succeeded(setup_run.pk, RESET_NODE_PUBLIC_KEY, timezone.now())
    setup_run.refresh_from_db()
    return setup_run


def test_before_any_setup_the_wizard_offers_to_start_and_does_not_poll(signed_in_client: Client) -> None:
    page = get_page(signed_in_client, "/setup").content.decode()
    step_response = get_partial(signed_in_client, "/setup/partials/step")

    assert "Start setup" in page
    assert "The relay worker is offline" in page
    assert 'hx-trigger="every 2s"' not in page
    assert step_response.status_code == HTMX_STOP_POLLING


def test_starting_setup_creates_the_run_and_waits_for_the_worker_while_it_is_offline(signed_in_client: Client) -> None:
    response = post_form(signed_in_client, "/setup/start")

    assert response.status_code == 302
    assert response["Location"] == "/setup"
    setup_run = get_active_setup_run()
    assert setup_run is not None
    assert setup_run.node_commands.get().kind == NodeCommand.Kind.READ_NODE_INFORMATION
    page = get_page(signed_in_client, "/setup").content.decode()
    assert "Waiting for the relay worker and the node" in page
    assert 'hx-trigger="every 2s"' in page
    assert "Cancel setup" in page
    step_response = get_partial(signed_in_client, "/setup/partials/step")
    assert step_response.status_code == 200
    assert 'hx-trigger="every 2s"' in step_response.content.decode()


def test_step_zero_names_the_transport_and_the_last_error_of_a_disconnected_worker(signed_in_client: Client) -> None:
    report_worker(connected=False)
    start_setup_run(timezone.now())

    page = get_page(signed_in_client, "/setup").content.decode()

    assert "tcp host.docker.internal:5055" in page
    assert "Connection refused" in page


def test_a_read_in_progress_keeps_polling(signed_in_client: Client) -> None:
    report_worker()
    start_setup_run(timezone.now())

    step_response = get_partial(signed_in_client, "/setup/partials/step")

    assert step_response.status_code == 200
    assert "reading the node" in step_response.content.decode()


def test_a_failed_read_shows_the_error_and_retry_reads_again(signed_in_client: Client) -> None:
    report_worker()
    setup_run = start_setup_run(timezone.now())
    read_command = claim_setup_command()
    finish_node_command(read_command.pk, NodeCommand.State.FAILED, timezone.now(), error_message="No answer.")

    step_response = get_partial(signed_in_client, "/setup/partials/step")
    assert step_response.status_code == HTMX_STOP_POLLING
    assert "Reading the node failed: No answer." in step_response.content.decode()
    assert "Retry" in step_response.content.decode()

    assert post_form(signed_in_client, "/setup/retry").status_code == 302
    assert setup_run.node_commands.filter(state=NodeCommand.State.PENDING).count() == 1


def test_the_node_as_read_is_shown_with_the_factory_reset_dialog(signed_in_client: Client) -> None:
    report_worker()
    prepare_run_awaiting_reset_confirmation()

    step_response = get_partial(signed_in_client, "/setup/partials/step")
    step = step_response.content.decode()

    assert step_response.status_code == HTMX_STOP_POLLING
    assert "Old relay" in step
    assert build_node_information().public_key in step
    assert "Canada" in step
    assert 'command="show-modal"' in step
    assert 'data-confirmation-text="Old relay"' in step
    assert "every user must add the server's new contact card" in step.lower()


def test_the_reset_confirmation_step_does_not_poll_while_the_worker_is_offline(signed_in_client: Client) -> None:
    prepare_run_awaiting_reset_confirmation()

    assert 'hx-trigger="every 2s"' not in get_page(signed_in_client, "/setup").content.decode()
    assert get_partial(signed_in_client, "/setup/partials/step").status_code == HTMX_STOP_POLLING


def test_the_configuration_form_and_its_review_do_not_poll_while_the_worker_is_offline(
    signed_in_client: Client,
) -> None:
    prepare_run_awaiting_configuration()

    form_page = get_page(signed_in_client, "/setup").content.decode()
    review_page = post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "review"})

    assert "Configure the node" in form_page
    assert 'hx-trigger="every 2s"' not in form_page
    assert "Apply and reboot" in review_page.content.decode()
    assert 'hx-trigger="every 2s"' not in review_page.content.decode()
    assert get_partial(signed_in_client, "/setup/partials/step").status_code == HTMX_STOP_POLLING


def test_a_wrongly_typed_name_changes_nothing(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_reset_confirmation()

    response = post_form(signed_in_client, "/setup/reset", {"typed_confirmation": "Old Relay"})

    assert response.status_code == 302
    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert "Type the node&#x27;s current name, Old relay" in get_page(signed_in_client, "/setup").content.decode()


def test_the_typed_name_starts_the_factory_reset_and_its_progress_is_shown(signed_in_client: Client) -> None:
    report_worker()
    setup_run = prepare_run_awaiting_reset_confirmation()

    post_form(signed_in_client, "/setup/reset", {"typed_confirmation": "Old relay"})

    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.RESETTING
    reset_command = claim_setup_command()
    record_node_command_progress(
        reset_command.pk,
        ProgressStep(
            step=FactoryResetStep.SEND_RESET_FRAME, state=ProgressStepState.DONE, detail="", at=timezone.now()
        ),
    )
    step_response = get_partial(signed_in_client, "/setup/partials/step")
    step = step_response.content.decode()
    assert step_response.status_code == 200
    assert "Send the factory reset" in step
    assert "Wait for the node to come back" in step
    assert "Factory resetting the node" in step


def test_a_reset_command_past_its_expiry_is_expired_when_the_wizard_is_rendered(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_reset_confirmation()
    post_form(signed_in_client, "/setup/reset", {"typed_confirmation": "Old relay"})
    NodeCommand.objects.filter(kind=NodeCommand.Kind.FACTORY_RESET).update(
        created_at=timezone.now() - timedelta(minutes=5), expires_at=timezone.now() - timedelta(minutes=4)
    )

    page = get_page(signed_in_client, "/setup").content.decode()

    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.AWAITING_RESET_CONFIRMATION
    assert "The factory reset did not start" in page


def test_the_configuration_form_starts_from_the_node_as_it_was_read(signed_in_client: Client) -> None:
    report_worker()
    prepare_run_awaiting_configuration()

    step_response = get_partial(signed_in_client, "/setup/partials/step")
    step = step_response.content.decode()

    assert step_response.status_code == HTMX_STOP_POLLING
    assert RESET_NODE_PUBLIC_KEY in step
    assert 'value="HopTalk Relay"' in step
    assert "Canada — 910.525MHz / SF7 / BW62.5 / CR5 / 3B (current)" in step
    assert "EU/UK (Deprecated) — 869.525MHz / SF11 / BW250 / CR5 — deprecated" in step
    assert 'hx-get="/setup/partials/radio-fields"' in step
    assert 'max="22"' in step
    assert "Always set by the relay" in step


def test_choosing_a_preset_fills_the_radio_values_and_its_path_hash_size(signed_in_client: Client) -> None:
    prepare_run_awaiting_configuration()

    fieldset = get_partial(signed_in_client, "/setup/partials/radio-fields?radio_preset=Czech+Republic+%28Narrow%29")
    fieldset_html = fieldset.content.decode()

    assert fieldset.status_code == 200
    assert fieldset_html.strip().startswith('<fieldset id="radio-fields"')
    assert re.search(r'name="frequency_megahertz"\s+value="869.432"', fieldset_html)
    assert re.search(r'name="bandwidth_kilohertz"\s+value="62.5"', fieldset_html)
    assert '<option value="2" selected>' in fieldset_html


def test_manual_entry_keeps_the_values_the_operator_had(signed_in_client: Client) -> None:
    prepare_run_awaiting_configuration()

    fieldset_html = get_partial(
        signed_in_client,
        "/setup/partials/radio-fields?radio_preset=manual&frequency_megahertz=869.618&bandwidth_kilohertz=125"
        "&spreading_factor=9&coding_rate=5&path_hash_size=1",
    ).content.decode()

    assert 'type="text" name="frequency_megahertz" value="869.618"' in fieldset_html
    assert '<option value="125" selected>' in fieldset_html
    assert 'value="9"' in fieldset_html


def test_review_shows_every_value_and_changes_nothing(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_configuration()

    review = post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "review"})
    review_html = review.content.decode()

    assert review.status_code == 200
    assert "Review the configuration" in review_html
    assert "916.575 MHz" in review_html
    assert "Apply and reboot" in review_html
    assert re.search(r'type="hidden"\s+name="radio_preset"\s+value="Australia \(Narrow\)"', review_html)
    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION


def test_back_returns_to_the_form_with_the_values_kept(signed_in_client: Client) -> None:
    prepare_run_awaiting_configuration()

    form_html = post_form(
        signed_in_client,
        "/setup/configure",
        {**VALID_CONFIGURATION_FORM, "node_name": "Hilltop relay", "stage": "edit"},
    ).content.decode()

    assert "Configure the node" in form_html
    assert 'value="Hilltop relay"' in form_html


def test_apply_starts_the_configuration_with_the_presets_values(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_configuration()

    response = post_form(
        signed_in_client,
        "/setup/configure",
        {**VALID_CONFIGURATION_FORM, "frequency_megahertz": "123", "stage": "apply", "replace_public_channel": "on"},
    )

    assert response.status_code == 302
    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.CONFIGURING
    assert (
        setup_run.requested_configuration
        == RequestedNodeConfiguration(
            node_name="HopTalk Relay",
            radio_preset_title="Australia (Narrow)",
            radio_frequency_kilohertz=916575,
            radio_bandwidth_hertz=62500,
            radio_spreading_factor=7,
            radio_coding_rate=7,
            path_hash_size=2,
            transmit_power_dbm=20,
            replace_public_channel=True,
        ).to_json()
    )
    assert setup_run.node_commands.filter(kind=NodeCommand.Kind.CONFIGURE_NODE).exists()


@pytest.mark.parametrize(
    ("changed_fields", "expected_error"),
    [
        pytest.param({"node_name": "Relay: one"}, "must not contain :", id="forbidden character"),
        pytest.param({"node_name": "Я" * 16}, "1 to 31 bytes", id="name too long in bytes"),
        pytest.param({"transmit_power_dbm": "23"}, "less than or equal to 22", id="above the node maximum"),
        pytest.param({"transmit_power_dbm": "-10"}, "greater than or equal to -9", id="below minus nine"),
        pytest.param(
            {"radio_preset": "manual", "frequency_megahertz": "916.5751"}, "at most 3 decimals", id="four decimals"
        ),
        pytest.param(
            {"radio_preset": "manual", "frequency_megahertz": "2500.001"}, "from 150.000 to 2500.000", id="range"
        ),
        pytest.param({"radio_preset": "manual", "coding_rate": ""}, "Required for manual entry", id="manual missing"),
        pytest.param({"radio_preset": "manual", "spreading_factor": "13"}, "less than or equal to 12", id="sf"),
        pytest.param({"path_hash_size": "4"}, "Select a valid choice", id="path hash size"),
    ],
)
def test_an_invalid_configuration_returns_to_the_form_with_the_error(
    signed_in_client: Client, changed_fields: dict[str, str], expected_error: str
) -> None:
    setup_run = prepare_run_awaiting_configuration()

    response = post_form(
        signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, **changed_fields, "stage": "apply"}
    )

    assert response.status_code == 200
    assert expected_error in response.content.decode().replace("&#x27;", "'")
    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION


def test_the_configuration_steps_are_listed_while_the_node_is_configured(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_configuration()
    submit_node_configuration(
        setup_run.pk,
        RequestedNodeConfiguration(
            node_name="HopTalk Relay",
            radio_preset_title="manual",
            radio_frequency_kilohertz=916575,
            radio_bandwidth_hertz=62500,
            radio_spreading_factor=7,
            radio_coding_rate=7,
            path_hash_size=2,
            transmit_power_dbm=22,
            replace_public_channel=False,
        ),
        timezone.now(),
    )
    configure_command = claim_setup_command()
    record_node_command_progress(
        configure_command.pk,
        ProgressStep(step=ConfigureNodeStep.SET_NAME, state=ProgressStepState.RUNNING, detail="", at=timezone.now()),
    )

    step_response = get_partial(signed_in_client, "/setup/partials/step")
    step = step_response.content.decode()

    assert step_response.status_code == 200
    assert "Configuring the node" in step
    assert step.count('<li class="flex items-start gap-3">') == 16
    assert "Restore the relay's identity" in step.replace("&#x27;", "'")
    assert "Back up the new identity" in step
    assert "running:" in step


def test_a_failed_configuration_shows_the_error_above_the_form(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_configuration()
    post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "apply"})
    configure_command = claim_setup_command()
    finish_node_command(configure_command.pk, NodeCommand.State.FAILED, timezone.now(), error_message="radio: ERR 6")

    page = get_page(signed_in_client, "/setup").content.decode()

    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION
    assert "Configuring the node failed: radio: ERR 6" in page
    assert 'value="20"' in page


def test_the_last_step_shows_the_card_and_the_root_then_leads_to_the_dashboard(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_configuration()
    post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "apply"})
    claim_setup_command()
    record_configuration_completed(setup_run.pk, build_node_configuration(setup_run_id=setup_run.pk), timezone.now())

    page = get_page(signed_in_client, "/setup").content.decode()

    assert "The node is configured" in page
    assert "Users must add this card" in page
    assert SAMPLE_CONTACT_CARD_URI in page
    assert "<svg" in page
    assert 'viewBox="0 0' in page
    assert get_page(signed_in_client, "/")["Location"] == "/node"


def test_cancel_on_the_configuration_step_warns_first_and_then_abandons_the_run(signed_in_client: Client) -> None:
    setup_run = prepare_run_awaiting_configuration()

    page = get_page(signed_in_client, "/setup").content.decode()
    assert "The node has already been factory reset. It has a new identity and no contacts." in page

    post_form(signed_in_client, "/setup/cancel")

    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.ABANDONED
    assert get_page(signed_in_client, "/")["Location"] == "/setup"


def test_an_identity_mismatch_after_setup_offers_to_set_up_the_attached_node(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.IDENTITY_MISMATCH,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            node_public_key="99" * 32,
        )
    )

    page = get_page(signed_in_client, "/setup").content.decode()

    assert "Set up the attached node" in page
    assert "99" * 32 in page
    assert load_node_configuration() is not None


# ----- keeping the relay's identity ----------------------------------------------------------------

CONFIGURED_PRIVATE_KEY = ORIGINAL_NODE_KEY_PAIR.private_key
BACKED_UP_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
RESTORE_IDENTITY_CHECKBOX_PATTERN = re.compile(r'<input type="checkbox" name="restore_identity"[^>]*>')


def prepare_reconfiguration_awaiting_configuration(*, with_backup: bool = True) -> NodeSetupRun:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    if with_backup:
        store_node_identity_backup(ORIGINAL_NODE_PUBLIC_KEY, CONFIGURED_PRIVATE_KEY, BACKED_UP_AT)
    return prepare_run_awaiting_configuration()


def read_page_text(client: Client, url: str) -> str:
    return get_page(client, url).content.decode().replace("&#x27;", "'").replace("&quot;", '"')


def test_a_reconfiguration_with_a_readable_backup_offers_to_keep_the_identity_ticked(
    signed_in_client: Client,
) -> None:
    prepare_reconfiguration_awaiting_configuration()

    page = read_page_text(signed_in_client, "/setup")

    checkbox = RESTORE_IDENTITY_CHECKBOX_PATTERN.search(page)
    assert checkbox is not None
    assert "checked" in checkbox.group()
    assert "Keep the relay's identity" in page
    assert "users need to do nothing" in page
    assert "every user must add the new contact card" in page


def test_an_initial_setup_offers_no_identity_to_keep(signed_in_client: Client) -> None:
    prepare_run_awaiting_configuration()

    page = read_page_text(signed_in_client, "/setup")

    assert 'name="restore_identity"' not in page
    assert "Keep the relay's identity" not in page


def test_a_backup_the_secret_key_no_longer_opens_is_explained_instead_of_offered(
    signed_in_client: Client, settings: Settings
) -> None:
    current_secret_key = settings.SECRET_KEY
    settings.SECRET_KEY = "the secret key src/.env held when the backup was taken"
    prepare_reconfiguration_awaiting_configuration()
    settings.SECRET_KEY = current_secret_key

    page = read_page_text(signed_in_client, "/setup")

    assert 'type="checkbox" name="restore_identity"' not in page
    assert "The relay's identity cannot be kept." in page
    assert "SECRET_KEY in src/.env changed since it was taken" in page


def test_a_reconfiguration_without_a_backup_says_that_the_identity_changes(signed_in_client: Client) -> None:
    prepare_reconfiguration_awaiting_configuration(with_backup=False)

    page = read_page_text(signed_in_client, "/setup")

    assert 'type="checkbox" name="restore_identity"' not in page
    assert "No backup of the relay's identity is stored" in page


def test_keeping_the_identity_is_reviewed_and_then_requested(signed_in_client: Client) -> None:
    setup_run = prepare_reconfiguration_awaiting_configuration()
    kept_identity_form = {**VALID_CONFIGURATION_FORM, "restore_identity": "on"}

    review = post_form(signed_in_client, "/setup/configure", {**kept_identity_form, "stage": "review"})
    review_html = review.content.decode().replace("&#x27;", "'")
    apply_response = post_form(signed_in_client, "/setup/configure", {**kept_identity_form, "stage": "apply"})

    assert "Kept: the node gets the relay's key" in review_html
    assert ORIGINAL_NODE_PUBLIC_KEY in review_html
    assert "first gives the node the relay's identity back" in review_html
    assert re.search(r'type="hidden"\s+name="restore_identity"\s+value="True"', review_html)
    assert apply_response.status_code == 302
    setup_run.refresh_from_db()
    assert setup_run.requested_configuration is not None
    assert setup_run.requested_configuration["restore_identity"] is True


def test_unticking_the_identity_reviews_and_requests_the_new_key(signed_in_client: Client) -> None:
    setup_run = prepare_reconfiguration_awaiting_configuration()

    review = post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "review"})
    post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "apply"})

    review_html = review.content.decode()
    assert "New: the node keeps the key" in review_html
    assert RESET_NODE_PUBLIC_KEY in review_html
    assert "every user must add the new contact card" in review_html
    setup_run.refresh_from_db()
    assert setup_run.requested_configuration is not None
    assert setup_run.requested_configuration["restore_identity"] is False


def test_keeping_an_identity_without_a_usable_backup_is_refused(signed_in_client: Client) -> None:
    setup_run = prepare_reconfiguration_awaiting_configuration(with_backup=False)

    response = post_form(
        signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "restore_identity": "on", "stage": "apply"}
    )

    assert response.status_code == 200
    assert "cannot be kept" in response.content.decode()
    setup_run.refresh_from_db()
    assert setup_run.state == NodeSetupRun.State.AWAITING_CONFIGURATION


def test_the_factory_reset_dialog_says_the_identity_can_be_kept(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    store_node_identity_backup(ORIGINAL_NODE_PUBLIC_KEY, CONFIGURED_PRIVATE_KEY, BACKED_UP_AT)
    prepare_run_awaiting_reset_confirmation()

    page = read_page_text(signed_in_client, "/setup")

    assert "The relay's identity is backed up: keep it in the configuration step" in page
    assert "must add the server's new contact card" not in page


KEPT_IDENTITY_TEXT = "The relay kept its identity; users need to do nothing."
NEW_IDENTITY_TEXT = "The node has a new identity, so every user adds this new card as well."


@pytest.mark.parametrize(
    ("began_restoring", "final_public_key", "expected_text", "unexpected_text"),
    [
        (True, ORIGINAL_NODE_PUBLIC_KEY, KEPT_IDENTITY_TEXT, NEW_IDENTITY_TEXT),
        (False, RESET_NODE_PUBLIC_KEY, NEW_IDENTITY_TEXT, KEPT_IDENTITY_TEXT),
        (True, RESET_NODE_PUBLIC_KEY, NEW_IDENTITY_TEXT, KEPT_IDENTITY_TEXT),
    ],
)
def test_the_last_step_says_whether_users_need_the_new_card(
    signed_in_client: Client, began_restoring: bool, final_public_key: str, expected_text: str, unexpected_text: str
) -> None:
    """Whichever attempt restored the identity, and whatever the last form said, the final key decides."""
    setup_run = prepare_reconfiguration_awaiting_configuration()
    post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "apply"})
    claim_setup_command()
    if began_restoring:
        record_identity_restore_started(setup_run.pk, ORIGINAL_NODE_PUBLIC_KEY)
    record_configuration_completed(
        setup_run.pk, build_node_configuration(public_key=final_public_key, setup_run_id=setup_run.pk), timezone.now()
    )

    page = read_page_text(signed_in_client, "/setup")

    assert expected_text in page
    assert unexpected_text not in page


def report_worker_attached_to(public_key: str, relay_mode: WorkerStatus.RelayMode) -> None:
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=relay_mode,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            node_public_key=public_key,
        )
    )


def prepare_run_whose_node_already_holds_the_relays_identity() -> NodeSetupRun:
    """An earlier attempt imported the relay's key and then failed; the run waits for its configuration again."""
    setup_run = prepare_reconfiguration_awaiting_configuration()
    NodeSetupRun.objects.filter(id=setup_run.pk).update(
        state=NodeSetupRun.State.AWAITING_CONFIGURATION, restored_public_key=ORIGINAL_NODE_PUBLIC_KEY
    )
    report_worker_attached_to(ORIGINAL_NODE_PUBLIC_KEY, WorkerStatus.RelayMode.SETUP_IN_PROGRESS)
    setup_run.refresh_from_db()
    return setup_run


@pytest.mark.parametrize("backup_is_readable", [True, False])
def test_a_node_that_already_holds_the_relays_identity_goes_on_with_it_without_a_checkbox(
    signed_in_client: Client, settings: Settings, backup_is_readable: bool
) -> None:
    current_secret_key = settings.SECRET_KEY
    if not backup_is_readable:
        settings.SECRET_KEY = "the secret key src/.env held when the backup was taken"
    prepare_run_whose_node_already_holds_the_relays_identity()
    settings.SECRET_KEY = current_secret_key

    page = read_page_text(signed_in_client, "/setup")

    assert 'type="checkbox" name="restore_identity"' not in page
    assert "The node already holds the relay's identity" in page
    assert "setup goes on with it and users need to do nothing" in page
    assert "The relay's identity cannot be kept." not in page


def test_the_review_of_a_node_that_already_holds_the_relays_identity_says_it_is_kept(
    signed_in_client: Client,
) -> None:
    prepare_run_whose_node_already_holds_the_relays_identity()

    review = post_form(signed_in_client, "/setup/configure", {**VALID_CONFIGURATION_FORM, "stage": "review"})
    review_html = review.content.decode().replace("&#x27;", "'")

    assert "Kept: the node already holds the relay's key" in review_html
    assert "New: the node keeps the key" not in review_html


def test_the_cancel_dialog_after_a_restore_says_the_relay_stays_stopped_even_with_the_relays_identity(
    signed_in_client: Client,
) -> None:
    prepare_run_whose_node_already_holds_the_relays_identity()

    page = read_page_text(signed_in_client, "/setup")

    assert "an earlier attempt began giving it the relay's identity back" in page
    assert "the relay stays stopped until a setup run is completed, even while the node holds" in page
    assert "It has a new identity and no contacts" not in page


def test_after_cancelling_a_restore_the_wizard_asks_to_set_the_node_up_again(signed_in_client: Client) -> None:
    setup_run = prepare_run_whose_node_already_holds_the_relays_identity()
    cancel_setup_run(setup_run.pk, timezone.now())
    report_worker_attached_to(ORIGINAL_NODE_PUBLIC_KEY, WorkerStatus.RelayMode.NOT_CONFIGURED)

    page = read_page_text(signed_in_client, "/setup")

    assert "The attached node holds the relay's identity but was never configured" in page
    assert "Set up the node" in page


def test_an_identity_mismatch_with_a_backup_says_the_attached_node_can_take_the_relays_place(
    signed_in_client: Client,
) -> None:
    replace_node_configuration(build_node_configuration(public_key=ORIGINAL_NODE_PUBLIC_KEY))
    store_node_identity_backup(ORIGINAL_NODE_PUBLIC_KEY, CONFIGURED_PRIVATE_KEY, BACKED_UP_AT)
    upsert_worker_status(
        WorkerStatus(
            heartbeat_at=timezone.now(),
            relay_mode=WorkerStatus.RelayMode.IDENTITY_MISMATCH,
            connection_state=WorkerStatus.ConnectionState.CONNECTED,
            node_public_key="99" * 32,
        )
    )

    page = read_page_text(signed_in_client, "/setup")

    assert "set the attached node up with \"Keep the relay's identity\" and it takes the relay's place" in page
