from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from django.contrib.auth.hashers import make_password
from django.test import Client
from django.utils import timezone
from pytest_django import Settings

from hoptalk_relay.relay_settings import OperatorCredentials, RelaySettings
from panel.operator_authentication import SESSION_AUTHENTICATED_AT_KEY
from tests.panel_operator import PanelOperator

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestClientResponse

pytestmark = pytest.mark.django_db

PANEL_ORIGIN = "https://testserver"


def post_with_csrf_token(
    client: Client, path: str, form_data: dict[str, str], client_address: str = ""
) -> TestClientResponse:
    headers = {"Origin": PANEL_ORIGIN}
    if client_address:
        headers["X-Forwarded-For"] = client_address
    return client.post(
        path,
        {**form_data, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
        secure=True,
        headers=headers,
    )


def open_login_page(client: Client) -> None:
    login_page = client.get("/login", secure=True)
    assert login_page.status_code == 200


def sign_in(client: Client, username: str, password: str, client_address: str = "192.0.2.10") -> TestClientResponse:
    open_login_page(client)
    return post_with_csrf_token(
        client,
        "/login?next=/node",
        {"username": username, "password": password},
        client_address=client_address,
    )


def test_the_operator_signs_in_reaches_a_page_and_signs_out(panel_operator: PanelOperator) -> None:
    client = Client(enforce_csrf_checks=True)

    sign_in_response = sign_in(client, panel_operator.username, panel_operator.password)
    assert sign_in_response.status_code == 302
    assert sign_in_response["Location"] == "/node"

    node_page = client.get("/node", secure=True)
    assert node_page.status_code == 200
    assert b"<h1" in node_page.content

    sign_out_response = post_with_csrf_token(client, "/logout", {})
    assert sign_out_response.status_code == 302
    assert client.get("/node", secure=True).status_code == 302


def test_a_page_without_a_session_redirects_to_the_sign_in_page() -> None:
    response = Client().get("/node", secure=True)

    assert response.status_code == 302
    assert response["Location"] == "/login?next=%2Fnode"


def test_an_htmx_request_without_a_session_is_redirected_by_htmx() -> None:
    response = Client().get("/node", secure=True, headers={"HX-Request": "true"})

    assert response.status_code == 200
    assert response["HX-Redirect"] == "/login"


def test_a_wrong_password_gets_the_generic_message(panel_operator: PanelOperator) -> None:
    response = sign_in(Client(enforce_csrf_checks=True), panel_operator.username, "not the password")

    assert response.status_code == 200
    assert b"Wrong username or password." in response.content


def test_more_than_five_failures_throttle_only_that_address(panel_operator: PanelOperator) -> None:
    client = Client(enforce_csrf_checks=True)
    for _attempt in range(6):
        sign_in(client, panel_operator.username, "not the password", client_address="192.0.2.20")

    throttled_response = sign_in(client, panel_operator.username, panel_operator.password, client_address="192.0.2.20")
    assert throttled_response.status_code == 429
    assert b"Too many attempts, try again in 15 minutes." in throttled_response.content

    other_address_response = sign_in(
        Client(enforce_csrf_checks=True),
        panel_operator.username,
        panel_operator.password,
        client_address="192.0.2.21",
    )
    assert other_address_response.status_code == 302


def test_changed_credentials_sign_every_browser_out(panel_operator: PanelOperator, settings: Settings) -> None:
    client = Client(enforce_csrf_checks=True)
    sign_in(client, panel_operator.username, panel_operator.password)
    assert client.get("/node", secure=True).status_code == 200

    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(
        relay_settings,
        operator_credentials=OperatorCredentials(
            username=panel_operator.username,
            password_hash=make_password("a new password"),
        ),
    )

    assert client.get("/node", secure=True).status_code == 302


def set_sign_in_age(client: Client, sign_in_age: timedelta) -> None:
    session = client.session
    session[SESSION_AUTHENTICATED_AT_KEY] = (timezone.now() - sign_in_age).isoformat()
    session.save()


def test_a_session_signed_in_more_than_twelve_hours_ago_is_sent_to_the_sign_in_page(signed_in_client: Client) -> None:
    set_sign_in_age(signed_in_client, timedelta(hours=12, seconds=1))

    page_response = signed_in_client.get("/node", secure=True)
    assert page_response.status_code == 302
    assert page_response["Location"] == "/login?next=%2Fnode"

    htmx_response = signed_in_client.get("/node/partials/status", secure=True, headers={"HX-Request": "true"})
    assert htmx_response["HX-Redirect"] == "/login"


def test_a_session_signed_in_just_under_twelve_hours_ago_is_still_served(signed_in_client: Client) -> None:
    set_sign_in_age(signed_in_client, timedelta(hours=11, minutes=59))

    assert signed_in_client.get("/node", secure=True).status_code == 200


def test_pages_behind_the_session_are_never_cached(signed_in_client: Client) -> None:
    for path in ("/messages", "/messages/traffic", "/users", "/node/partials/status"):
        cache_control = signed_in_client.get(path, secure=True)["Cache-Control"]
        assert "no-store" in cache_control, path
        assert "private" in cache_control, path


def test_the_redirect_for_a_missing_session_and_static_files_keep_their_caching() -> None:
    redirect_response = Client().get("/messages", secure=True)
    assert redirect_response.status_code == 302
    assert not redirect_response.has_header("Cache-Control")

    static_response = Client().get("/static/does-not-exist.css", secure=True)
    assert "no-store" not in static_response.get("Cache-Control", "")


def test_the_sign_in_page_is_never_cached_and_signing_out_clears_the_browser_cache(
    signed_in_client: Client,
) -> None:
    assert "no-store" in Client().get("/login", secure=True)["Cache-Control"]

    sign_out_response = post_with_csrf_token(signed_in_client, "/logout", {})
    assert sign_out_response["Clear-Site-Data"] == '"cache"'


def test_the_htmx_script_carries_the_nonce_of_the_content_security_policy() -> None:
    response = Client().get("/login", secure=True)
    content_security_policy = response["Content-Security-Policy"]
    nonce = content_security_policy.split("'nonce-", 1)[1].split("'", 1)[0]

    assert f'nonce="{nonce}"' in response.content.decode()
    assert "style-src 'self'" in content_security_policy


def test_the_sign_in_form_is_built_from_the_components_that_follow_the_colour_scheme() -> None:
    page = Client().get("/login", secure=True).content.decode()
    sign_in_form = page[page.index("<form") : page.index("</form>")]

    assert 'class="card space-y-5 p-6"' in sign_in_form
    assert sign_in_form.count('class="label"') == 2
    assert sign_in_form.count('class="input"') == 2
    assert "dark:text-white" in sign_in_form
    assert "bg-white" not in sign_in_form


def test_the_skip_link_stays_readable_when_it_appears_in_the_dark_scheme(signed_in_client: Client) -> None:
    page = signed_in_client.get("/node", secure=True).content.decode()
    skip_link = page[page.index('<a href="#main-content"') :].split(">", 1)[0]

    assert "focus:text-slate-900" in skip_link
    assert "dark:focus:bg-slate-800" in skip_link
    assert "dark:focus:text-white" in skip_link
