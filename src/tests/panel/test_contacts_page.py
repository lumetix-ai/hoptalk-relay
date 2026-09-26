import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from directory.contacts import CONTACT_CAPACITY
from directory.models import Contact
from node.models import NodeCommand, NodeSetupRun, PairingSession, WorkerStatus
from node.node_commands import create_node_command
from node.node_settings import replace_node_configuration
from node.pairing_sessions import record_heard_advert, start_pairing_session, stop_pairing_session
from node.setup_runs import start_setup_run
from tests.panel.panel_client import find_button_opening_tag, get_page, get_partial, post_form
from tests.services.directory.row_builders import build_public_key, create_contact, create_user
from tests.services.node.node_builders import (
    SAMPLE_CONTACT_CARD_PUBLIC_KEY,
    SAMPLE_CONTACT_CARD_URI,
    ContactCardSigner,
    build_node_configuration,
)

pytestmark = pytest.mark.django_db

HTMX_STOP_POLLING = 286


def preview_card(client: Client, card_uri: str) -> str:
    response = post_form(client, "/contacts/card/preview", {"card_uri": card_uri}, htmx=True)
    assert response.status_code == 200
    return response.content.decode()


def start_session_with_adverts() -> PairingSession:
    pairing_session = start_pairing_session(
        duration_seconds=120, advert_interval_seconds=30, advert_flood=False, start_command=None, now=timezone.now()
    )
    record_heard_advert(
        pairing_session.pk,
        {"public_key": build_public_key(7), "type": 1, "adv_name": "Bob's tracker", "last_advert": 1_790_000_000},
        timezone.now(),
    )
    record_heard_advert(
        pairing_session.pk,
        {"public_key": build_public_key(8), "type": 2, "adv_name": "Hilltop", "last_advert": 1_790_000_000},
        timezone.now(),
    )
    return pairing_session


def test_the_relays_card_is_shown_as_a_qr_code_and_copyable_text(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())

    page = get_page(signed_in_client, "/contacts").content.decode()

    assert '<svg viewBox="0 0' in page
    assert SAMPLE_CONTACT_CARD_URI in page
    assert 'data-copy-target="server-contact-card"' in page
    assert "Regenerate card" in page
    assert 'name="next" value="/contacts"' in page


def test_before_setup_the_page_says_there_is_no_card_yet(signed_in_client: Client) -> None:
    page = get_page(signed_in_client, "/contacts").content.decode()

    assert "has no card to share" in page
    assert "No contacts yet." in page


def test_the_contact_list_shows_each_contacts_state_and_polls_only_while_one_is_being_added(
    signed_in_client: Client,
) -> None:
    create_contact(1, user=create_user("ivan"), name="Ivan's tracker")
    failed_contact = create_contact(2, node_sync_state=Contact.NodeSyncState.ADD_FAILED)
    Contact.objects.filter(id=failed_contact.pk).update(node_sync_error="the node's contact table is full")

    settled_list = get_partial(signed_in_client, "/contacts/partials/list").content.decode()

    assert 'hx-trigger="contacts-changed from:body"' in settled_list
    assert "@ivan" in settled_list
    assert "not registered" in settled_list
    assert "on the node" in settled_list
    assert "the node&#x27;s contact table is full" in settled_list
    assert "2 of 350 places used." in settled_list

    create_contact(3, node_sync_state=Contact.NodeSyncState.PENDING_ADD)
    pending_list = get_partial(signed_in_client, "/contacts/partials/list").content.decode()

    assert 'hx-trigger="contacts-changed from:body, every 5s"' in pending_list
    assert "being added" in pending_list


def test_a_valid_card_is_previewed_with_its_signature_and_can_be_added(signed_in_client: Client) -> None:
    preview = preview_card(signed_in_client, SAMPLE_CONTACT_CARD_URI)

    assert "signature valid" in preview
    assert "Liam Cottle 🤠" in preview
    assert SAMPLE_CONTACT_CARD_PUBLIC_KEY in preview
    assert "chat node" in preview
    assert "Add contact" in preview
    assert "<html" not in preview


def test_adding_a_card_creates_a_contact_the_worker_adds_and_redirects_to_the_list(signed_in_client: Client) -> None:
    response = post_form(signed_in_client, "/contacts/card/add", {"card_uri": SAMPLE_CONTACT_CARD_URI})

    assert response.status_code == 302
    assert response["Location"] == "/contacts"
    contact = Contact.objects.get()
    assert contact.node_sync_state == Contact.NodeSyncState.PENDING_ADD
    page = get_page(signed_in_client, "/contacts").content.decode()
    assert "was added; the relay worker now adds it to the node." in page
    assert "every 5s" in page


def test_a_card_added_twice_is_refused_with_a_message(signed_in_client: Client) -> None:
    post_form(signed_in_client, "/contacts/card/add", {"card_uri": SAMPLE_CONTACT_CARD_URI})
    post_form(signed_in_client, "/contacts/card/add", {"card_uri": SAMPLE_CONTACT_CARD_URI})

    assert Contact.objects.count() == 1
    assert "The contact was not added." in get_page(signed_in_client, "/contacts").content.decode()


def test_text_that_is_not_a_card_is_explained_in_the_preview(signed_in_client: Client) -> None:
    preview = preview_card(signed_in_client, "meshcore://11zz")

    assert "must be hexadecimal digits" in preview
    assert "Add contact" not in preview


@pytest.mark.parametrize(
    "problem",
    ["not_a_chat_node", "already_a_contact", "prefix_collision", "relay_node_itself", "capacity_reached"],
)
def test_every_problem_that_blocks_a_card_is_named_in_the_preview(signed_in_client: Client, problem: str) -> None:
    contact_card_signer = ContactCardSigner()
    card_uri = contact_card_signer.build_card_uri()
    if problem == "not_a_chat_node":
        card_uri = contact_card_signer.build_card_uri(flags=0x82)
        expected_message = "This is a repeater, and only a chat node can be a contact."
    elif problem == "already_a_contact":
        post_form(signed_in_client, "/contacts/card/add", {"card_uri": card_uri})
        expected_message = "This node is already a contact"
    elif problem == "prefix_collision":
        Contact.objects.create(
            public_key=contact_card_signer.public_key[:12] + "0" * 52,
            source=Contact.Source.CARD,
            added_at=timezone.now(),
        )
        expected_message = "The first six bytes of this key equal those of contact"
    elif problem == "relay_node_itself":
        WorkerStatus.objects.create(node_public_key=contact_card_signer.public_key)
        expected_message = "This is the relay&#x27;s own node."
    else:
        Contact.objects.bulk_create(
            Contact(public_key=build_public_key(number), source=Contact.Source.CARD, added_at=timezone.now())
            for number in range(CONTACT_CAPACITY)
        )
        expected_message = "The node already holds 350 contacts"

    preview = preview_card(signed_in_client, card_uri)

    assert expected_message in preview
    assert "Add contact" not in preview


def test_without_javascript_the_preview_is_part_of_the_whole_page(signed_in_client: Client) -> None:
    response = post_form(signed_in_client, "/contacts/card/preview", {"card_uri": SAMPLE_CONTACT_CARD_URI})

    assert "<html" in response.content.decode()
    assert "signature valid" in response.content.decode()


def test_deleting_a_device_explains_what_goes_with_it_and_returns_to_the_page_it_came_from(
    signed_in_client: Client,
) -> None:
    device = create_contact(1, user=create_user("ivan"), name="Ivan's tracker")

    page = get_page(signed_in_client, "/contacts").content.decode()
    assert f'hx-get="/contacts/{device.pk}/partials/delete-summary"' in page
    assert 'hx-trigger="intersect once"' in page

    summary = get_partial(signed_in_client, f"/contacts/{device.pk}/partials/delete-summary").content.decode()
    assert "It is a device of <strong>@ivan</strong>: its 0 pending deliveries and 0 pending receipts" in summary

    response = post_form(signed_in_client, f"/contacts/{device.pk}/delete", {"next": "/users"})
    assert response["Location"] == "/users"
    assert not Contact.objects.exists()


def test_the_pairing_form_starts_a_start_pairing_command(signed_in_client: Client) -> None:
    page = get_page(signed_in_client, "/contacts").content.decode()
    assert "Start pairing" in page
    assert 'value="120"' in page

    response = post_form(
        signed_in_client,
        "/contacts/pairing/start",
        {"duration_seconds": "180", "advert_interval_seconds": "20", "advert_flood": "on"},
    )

    assert response["Location"] == "/contacts#pairing"
    start_command = NodeCommand.objects.get()
    assert start_command.kind == NodeCommand.Kind.START_PAIRING
    assert start_command.arguments == {"duration_seconds": 180, "advert_interval_seconds": 20, "advert_flood": True}
    starting_page = get_page(signed_in_client, "/contacts").content.decode()
    assert f"/commands/{start_command.pk}/partials/progress?refresh_page_when_finished=1" in starting_page
    assert "Start pairing</" not in starting_page.replace("\n", "").replace(" ", "")


@pytest.mark.parametrize(
    "form_data",
    [
        pytest.param({"duration_seconds": "30", "advert_interval_seconds": "30"}, id="too short"),
        pytest.param({"duration_seconds": "120", "advert_interval_seconds": "5"}, id="adverts too often"),
    ],
)
def test_pairing_limits_are_enforced(signed_in_client: Client, form_data: dict[str, str]) -> None:
    post_form(signed_in_client, "/contacts/pairing/start", form_data)

    assert not NodeCommand.objects.exists()
    assert (
        "Pairing needs a duration of 60 to 600 seconds and an advert every 10 to 120 seconds."
        in get_page(signed_in_client, "/contacts").content.decode()
    )


def test_a_second_pairing_start_is_refused_while_one_is_starting(signed_in_client: Client) -> None:
    pairing_form = {"duration_seconds": "120", "advert_interval_seconds": "30"}
    post_form(signed_in_client, "/contacts/pairing/start", pairing_form)
    post_form(signed_in_client, "/contacts/pairing/start", pairing_form)

    assert NodeCommand.objects.count() == 1
    assert "Pairing is already starting." in get_page(signed_in_client, "/contacts").content.decode()


def test_an_active_session_counts_down_and_lists_the_heard_nodes(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()

    panel_response = get_partial(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/partials/panel")
    panel = panel_response.content.decode()

    assert panel_response.status_code == 200
    assert 'hx-trigger="every 2s"' in panel
    assert "data-ends-at=" in panel
    assert "data-server-now=" in panel
    assert "Bob&#x27;s tracker" in panel
    assert build_public_key(7) in panel
    assert "Do you really want to add this contact?" in panel
    assert "Yes, add" in panel
    assert "A repeater cannot be a contact." in panel


def test_a_heard_node_is_added_from_its_dialog_and_the_contact_list_is_told(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    heard_advert = pairing_session.heard_adverts.get(node_type=1)

    response = post_form(
        signed_in_client, f"/contacts/pairing/{pairing_session.pk}/adverts/{heard_advert.pk}/add", htmx=True
    )

    assert response.status_code == 200
    assert json.loads(response["HX-Trigger"]) == {"contacts-changed": {}}
    assert "was added; the relay worker now adds it to the node." in response.content.decode()
    assert ">Added<" in response.content.decode()
    contact = Contact.objects.get()
    assert contact.source == Contact.Source.PAIRING


def test_a_heard_node_that_cannot_be_added_says_why(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    repeater_advert = pairing_session.heard_adverts.get(node_type=2)

    response = post_form(
        signed_in_client, f"/contacts/pairing/{pairing_session.pk}/adverts/{repeater_advert.pk}/add", htmx=True
    )

    assert "was not added. This is a repeater" in response.content.decode()
    assert not Contact.objects.exists()


def test_a_heard_node_added_earlier_from_its_card_is_not_shown_as_added_from_pairing(
    signed_in_client: Client,
) -> None:
    pairing_session = start_session_with_adverts()
    create_contact(7)

    panel = get_partial(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/partials/panel").content.decode()

    assert "Already a contact." in panel
    assert ">Added<" not in panel


def test_stopping_pairing_ends_the_session_at_once_and_wakes_the_worker(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()

    response = post_form(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/stop", htmx=True)

    assert response.status_code == 200
    assert 'hx-trigger="every 2s"' not in response.content.decode()
    assert "Heard nodes can be added until" in response.content.decode()
    pairing_session.refresh_from_db()
    assert pairing_session.state == PairingSession.State.STOPPED
    stop_command = NodeCommand.objects.get()
    assert stop_command.kind == NodeCommand.Kind.STOP_PAIRING
    assert stop_command.arguments == {"pairing_session_id": pairing_session.pk}


def test_the_panel_of_an_ended_session_stops_the_polling(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    stop_pairing_session(pairing_session.pk, timezone.now())

    response = get_partial(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/partials/panel")

    assert response.status_code == HTMX_STOP_POLLING


def test_a_session_that_ended_long_ago_is_no_longer_shown(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    stop_pairing_session(pairing_session.pk, timezone.now() - timedelta(minutes=20))

    page = get_page(signed_in_client, "/contacts").content.decode()

    assert "Bob&#x27;s tracker" not in page
    assert "Start pairing" in page


def fill_the_contact_table() -> None:
    Contact.objects.bulk_create(
        Contact(public_key=build_public_key(number), source=Contact.Source.CARD, added_at=timezone.now())
        for number in range(100, 100 + CONTACT_CAPACITY)
    )


def test_a_heard_node_the_table_has_no_room_for_offers_no_add_button(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    chat_advert = pairing_session.heard_adverts.get(node_type=1)
    fill_the_contact_table()

    panel = get_partial(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/partials/panel").content.decode()

    assert "The node already holds 350 contacts, its maximum." in panel
    assert f'commandfor="add-advert-{chat_advert.pk}"' not in panel


def test_a_heard_node_whose_key_prefix_collides_offers_no_add_button(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    chat_advert = pairing_session.heard_adverts.get(node_type=1)
    Contact.objects.create(
        public_key=chat_advert.public_key[:12] + "f" * 52, source=Contact.Source.CARD, added_at=timezone.now()
    )

    panel = get_partial(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/partials/panel").content.decode()

    assert "The first six bytes of this key equal those of contact" in panel
    assert f'commandfor="add-advert-{chat_advert.pk}"' not in panel


def test_the_notice_of_an_add_sits_outside_the_polled_panel_and_outlives_its_polls(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()
    repeater_advert = pairing_session.heard_adverts.get(node_type=2)

    page = get_page(signed_in_client, "/contacts").content.decode()
    add_response = post_form(
        signed_in_client, f"/contacts/pairing/{pairing_session.pk}/adverts/{repeater_advert.pk}/add", htmx=True
    ).content.decode()
    next_poll = get_partial(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/partials/panel")

    assert page.index('id="pairing-notice"') < page.index('id="pairing-panel"')
    panel_part, notice_part = add_response.split('<div id="pairing-notice"', 1)
    assert "was not added" not in panel_part
    assert 'hx-swap-oob="innerHTML"' in notice_part
    assert "was not added. This is a repeater" in notice_part
    assert "pairing-notice" not in next_poll.content.decode()


def test_stopping_pairing_reports_it_outside_the_panel(signed_in_client: Client) -> None:
    pairing_session = start_session_with_adverts()

    response = post_form(signed_in_client, f"/contacts/pairing/{pairing_session.pk}/stop", htmx=True)

    panel_part, notice_part = response.content.decode().split('<div id="pairing-notice"', 1)
    assert "Pairing was stopped." not in panel_part
    assert "Pairing was stopped." in notice_part


def test_a_start_command_past_its_expiry_is_expired_and_the_form_comes_back(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    start_command = create_node_command(NodeCommand.Kind.START_PAIRING, {}, timezone.now() - timedelta(minutes=3))

    page = get_page(signed_in_client, "/contacts").content.decode()

    assert "Pairing did not start (expired)." in page
    assert 'action="/contacts/pairing/start"' in page
    assert f"/commands/{start_command.pk}/partials/progress" not in page
    start_command.refresh_from_db()
    assert start_command.state == NodeCommand.State.EXPIRED


def test_the_progress_of_a_command_past_its_expiry_stops_polling(signed_in_client: Client) -> None:
    start_command = create_node_command(NodeCommand.Kind.START_PAIRING, {}, timezone.now() - timedelta(minutes=3))

    response = get_partial(
        signed_in_client, f"/commands/{start_command.pk}/partials/progress?refresh_page_when_finished=1"
    )

    assert response.status_code == HTMX_STOP_POLLING
    assert response["HX-Refresh"] == "true"
    assert "Waiting for the relay worker" not in response.content.decode()


def test_pairing_and_a_new_card_wait_for_the_node_to_be_set_up(signed_in_client: Client) -> None:
    page = get_page(signed_in_client, "/contacts").content.decode()

    assert " disabled" in find_button_opening_tag(page, "Start pairing")
    assert "Available once the node is set up." in page


def test_pairing_and_a_new_card_are_offered_for_a_configured_node(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())

    page = get_page(signed_in_client, "/contacts").content.decode()

    assert " disabled" not in find_button_opening_tag(page, "Start pairing")
    assert " disabled" not in find_button_opening_tag(page, "Regenerate card")


def test_pairing_and_a_new_card_are_not_offered_while_setup_owns_the_node(signed_in_client: Client) -> None:
    replace_node_configuration(build_node_configuration())
    setup_run = start_setup_run(timezone.now())
    NodeSetupRun.objects.filter(id=setup_run.pk).update(state=NodeSetupRun.State.CONFIGURING)

    page = get_page(signed_in_client, "/contacts").content.decode()

    assert " disabled" in find_button_opening_tag(page, "Start pairing")
    assert " disabled" in find_button_opening_tag(page, "Regenerate card")
    assert "Not available while setup is in progress." in page


def test_the_buttons_of_polled_regions_keep_their_ids_and_the_table_keeps_its_labels_inside(
    signed_in_client: Client,
) -> None:
    replace_node_configuration(build_node_configuration())
    create_contact(1)
    pairing_session = start_session_with_adverts()
    chat_advert = pairing_session.heard_adverts.get(node_type=1)

    page = get_page(signed_in_client, "/contacts").content.decode()

    assert f'id="add-advert-button-{chat_advert.pk}"' in page
    assert 'id="pairing-stop-button"' in page
    assert 'id="server-contact-card-copy-button"' in page
    assert '<div class="relative overflow-x-auto">' in page
