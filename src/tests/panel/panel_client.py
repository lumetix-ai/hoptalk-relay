"""A signed-in test client for the panel, helpers that send what a browser or htmx would send, and page readers."""

import re
from typing import TYPE_CHECKING

from django.test import Client

from tests.panel_operator import PanelOperator

if TYPE_CHECKING:
    from django.test.client import _MonkeyPatchedWSGIResponse as TestClientResponse

PANEL_ORIGIN = "https://testserver"
HTMX_HEADERS = {"HX-Request": "true"}


def sign_in_test_client(panel_operator: PanelOperator) -> Client:
    client = Client(enforce_csrf_checks=True)
    client.get("/login", secure=True)
    response = post_form(client, "/login", {"username": panel_operator.username, "password": panel_operator.password})
    assert response.status_code == 302
    return client


def get_page(client: Client, path: str, htmx_target: str = "") -> TestClientResponse:
    headers = {}
    if htmx_target:
        headers = {**HTMX_HEADERS, "HX-Target": htmx_target}
    return client.get(path, secure=True, headers=headers)


def get_partial(client: Client, path: str) -> TestClientResponse:
    return client.get(path, secure=True, headers=HTMX_HEADERS)


def post_form(
    client: Client, path: str, form_data: dict[str, str] | None = None, htmx: bool = False
) -> TestClientResponse:
    headers = {"Origin": PANEL_ORIGIN}
    if htmx:
        headers.update(HTMX_HEADERS)
    return client.post(
        path,
        {**(form_data or {}), "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
        secure=True,
        headers=headers,
    )


def find_button_opening_tag(page_section: str, button_label: str) -> str:
    """The opening tag of the only button in page_section whose content contains button_label."""
    button_elements: list[str] = re.findall(r"<button[^>]*>(?:(?!</button>).)*</button>", page_section, flags=re.DOTALL)
    matching_elements = [button_element for button_element in button_elements if button_label in button_element]
    assert len(matching_elements) == 1, button_label
    return matching_elements[0].split(">", 1)[0]
