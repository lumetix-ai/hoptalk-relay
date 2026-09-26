import re

import pytest
from django.test import Client

from tests.panel.panel_client import get_page, post_form
from tests.services.directory.row_builders import create_accepted_message, create_contact, create_user

pytestmark = pytest.mark.django_db

GET_PATHS = [
    "/",
    "/setup",
    "/setup/partials/step",
    "/setup/partials/radio-fields",
    "/node",
    "/node/partials/status",
    "/commands/1/partials/progress",
    "/contacts",
    "/contacts/partials/list",
    "/contacts/1/partials/delete-summary",
    "/contacts/pairing/1/partials/panel",
    "/users",
    "/users/1/partials/delete-summary",
    "/messages",
    "/messages/traffic",
    "/messages/refresh-sessions",
    "/messages/partials/table",
    "/messages/1/partials/details",
    "/messages/traffic/partials/rows",
    "/partials/banners",
]
POST_PATHS = [
    "/setup/start",
    "/setup/retry",
    "/setup/reset",
    "/setup/configure",
    "/setup/cancel",
    "/node/actions/advert",
    "/node/actions/reboot",
    "/node/actions/reapply-settings",
    "/node/actions/regenerate-card",
    "/node/actions/sync-contacts",
    "/node/actions/reconfigure",
    "/contacts/card/preview",
    "/contacts/card/add",
    "/contacts/1/delete",
    "/contacts/pairing/start",
    "/contacts/pairing/1/stop",
    "/contacts/pairing/1/adverts/1/add",
    "/users/1/delete",
]
PAGES = ["/setup", "/node", "/contacts", "/users", "/messages", "/messages/traffic", "/messages/refresh-sessions"]


@pytest.mark.parametrize("path", GET_PATHS + POST_PATHS)
def test_every_panel_address_needs_a_signed_in_operator(path: str) -> None:
    response = Client().get(path, secure=True)

    assert response.status_code == 302
    assert response["Location"].startswith("/login?next=")


@pytest.mark.parametrize("path", GET_PATHS)
def test_an_htmx_request_without_a_session_is_sent_to_the_sign_in_page_by_htmx(path: str) -> None:
    response = Client().get(path, secure=True, headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response["HX-Redirect"] == "/login"


@pytest.mark.parametrize("path", POST_PATHS)
def test_every_form_post_needs_the_csrf_token(signed_in_client: Client, path: str) -> None:
    response = signed_in_client.post(path, {}, secure=True, headers={"Origin": "https://testserver"})

    assert response.status_code == 403


@pytest.mark.parametrize("path", POST_PATHS)
def test_nothing_changes_on_a_get_to_an_action(signed_in_client: Client, path: str) -> None:
    assert signed_in_client.get(path, secure=True).status_code == 405


@pytest.mark.parametrize("path", PAGES)
def test_every_page_keeps_to_the_content_security_policy(signed_in_client: Client, path: str) -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    create_accepted_message(ivan, bob, 1790294400123456, sender_device=create_contact(1, user=ivan))

    response = get_page(signed_in_client, path)
    page = response.content.decode()

    assert response.status_code == 200
    assert " style=" not in page
    assert "<style" not in page
    inline_scripts = [script for script in re.findall(r"<script[^>]*>", page) if "src=" not in script]
    assert inline_scripts == []
    assert "hx-on" not in page


@pytest.mark.parametrize("path", PAGES)
def test_every_page_polls_the_banner_region_and_loads_the_dialog_and_polling_scripts(
    signed_in_client: Client, path: str
) -> None:
    page = get_page(signed_in_client, path).content.decode()

    assert 'hx-get="/partials/banners"' in page
    assert 'hx-trigger="load, every 5s"' in page
    assert "confirmation_dialogs.js" in page
    assert "polled_regions.js" in page


@pytest.mark.parametrize("path", PAGES)
def test_every_htmx_form_disables_its_buttons_while_it_is_submitted(signed_in_client: Client, path: str) -> None:
    create_contact(1)
    page = get_page(signed_in_client, path).content.decode()

    htmx_post_forms = re.findall(r"<form[^>]*hx-post=[^>]*>", page, flags=re.DOTALL)
    for htmx_post_form in htmx_post_forms:
        assert "hx-disabled-elt" in htmx_post_form


def test_the_root_leads_to_setup_before_the_node_is_configured(signed_in_client: Client) -> None:
    response = get_page(signed_in_client, "/")

    assert response.status_code == 302
    assert response["Location"] == "/setup"


def test_signing_out_needs_a_post_with_the_csrf_token(signed_in_client: Client) -> None:
    assert post_form(signed_in_client, "/logout").status_code == 302
    assert get_page(signed_in_client, "/node").status_code == 302
