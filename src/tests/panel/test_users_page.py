import pytest
from django.test import Client

from directory.models import Contact, User
from messaging.models import Message
from tests.panel.panel_client import get_page, get_partial, post_form
from tests.services.directory.row_builders import create_accepted_message, create_contact, create_user

pytestmark = pytest.mark.django_db


def test_the_users_page_lists_users_with_their_devices_and_counts(signed_in_client: Client) -> None:
    ivan, bob = create_user("Ivan"), create_user("bob")
    ivans_device = create_contact(1, user=ivan, name="Ivan's tracker")
    create_accepted_message(ivan, bob, 1, sender_device=ivans_device)

    page = get_page(signed_in_client, "/users").content.decode()

    assert "2 users" in page
    assert "@Ivan" in page
    assert "Ivan&#x27;s tracker" in page
    assert "1 device" in page
    assert "1 sent · 0 received" in page
    assert "0 sent · 1 received" in page
    assert "no device" in page
    assert page.index("@bob") < page.index("@Ivan")


def test_without_users_the_page_explains_how_they_register(signed_in_client: Client) -> None:
    assert "No user has registered yet." in get_page(signed_in_client, "/users").content.decode()


def test_the_user_deletion_dialog_explains_what_goes_and_needs_the_username_typed(signed_in_client: Client) -> None:
    ivan, bob = create_user("ivan"), create_user("bob")
    create_contact(1, user=ivan)
    create_accepted_message(ivan, bob, 1)
    create_accepted_message(bob, ivan, 2)

    page = get_page(signed_in_client, "/users").content.decode()
    summary = get_partial(signed_in_client, f"/users/{ivan.pk}/partials/delete-summary").content.decode()

    assert 'data-confirmation-text="ivan"' in page
    assert "data-confirmation-submit" in page
    assert "its 1 device (removed from the node as well) and the 2 messages it sent or received" in summary


def test_a_user_is_deleted_only_with_the_username_typed_exactly(signed_in_client: Client) -> None:
    ivan, bob = create_user("Ivan"), create_user("bob")
    create_contact(1, user=ivan)
    create_accepted_message(bob, ivan, 1)

    refused = post_form(signed_in_client, f"/users/{ivan.pk}/delete", {"typed_username": "ivan"})
    assert refused["Location"] == "/users"
    assert User.objects.filter(id=ivan.pk).exists()
    assert "was not deleted" in get_page(signed_in_client, "/users").content.decode()

    deleted = post_form(signed_in_client, f"/users/{ivan.pk}/delete", {"typed_username": "Ivan"})
    assert deleted["Location"] == "/users"
    assert not User.objects.filter(id=ivan.pk).exists()
    assert not Contact.objects.exists()
    assert not Message.objects.exists()


def test_spaces_around_the_typed_username_are_ignored_as_the_delete_button_ignores_them(
    signed_in_client: Client,
) -> None:
    ivan = create_user("Ivan")

    deleted = post_form(signed_in_client, f"/users/{ivan.pk}/delete", {"typed_username": " Ivan "})

    assert deleted["Location"] == "/users"
    assert not User.objects.filter(id=ivan.pk).exists()


def test_a_device_is_deleted_from_the_users_page_through_the_contact_endpoint(signed_in_client: Client) -> None:
    device = create_contact(1, user=create_user("ivan"))

    page = get_page(signed_in_client, "/users").content.decode()
    assert f'action="/contacts/{device.pk}/delete"' in page
    assert 'name="next" value="/users"' in page


def test_the_device_table_keeps_its_hidden_labels_inside_its_scroll_area(signed_in_client: Client) -> None:
    create_contact(1, user=create_user("ivan"))

    page = get_page(signed_in_client, "/users").content.decode()

    assert '<div class="relative overflow-x-auto">' in page
