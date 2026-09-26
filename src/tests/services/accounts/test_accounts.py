import threading
from datetime import datetime

import pytest
from django.contrib.auth.hashers import check_password

from directory import accounts
from directory.accounts import register_or_sign_in, relink_device, user_exists
from directory.models import Contact, User
from messaging.models import MessageDelivery, ReceiptNotification, RefreshSession
from protocol.formatting import format_server_message
from protocol.message_types import AccountRequest, QueryRequest
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import TEST_PASSWORD, RelayHarness, create_device, create_user
from tests.services.node.database_threads import run_concurrently

WRONG_PASSWORD = "incorrect horse battery"


def sign_in(device: Contact, username: str, password: str, now: datetime) -> str:
    account_outcome = register_or_sign_in(device, AccountRequest(username=username, password=password), now)
    return format_server_message(account_outcome.reply)


def read_failed_sign_in_count(device: Contact) -> int:
    return Contact.objects.values_list("failed_sign_in_count", flat=True).get(id=device.pk)


def refuse_to_hash(password: str, encoded: str) -> bool:
    raise AssertionError("A throttled device must not cost a password check.")


@pytest.mark.django_db
def test_a_new_username_creates_the_account_and_links_the_device(
    relay: RelayHarness, manual_clock: ManualClock
) -> None:
    device = create_device(1, manual_clock.now())

    assert relay.receive_replies(device, f"HT1 A Ivan {TEST_PASSWORD}") == ["HT1 a Ivan"]

    user = User.objects.get()
    assert user.username == "Ivan"
    assert user.username_lookup == "ivan"
    assert check_password(TEST_PASSWORD, user.password_hash)
    device.refresh_from_db()
    assert device.user_id == user.pk
    assert device.linked_at == manual_clock.now()
    assert_all_invariants()


@pytest.mark.django_db
def test_a_retry_after_a_lost_account_reply_answers_again_and_changes_nothing(manual_clock: ManualClock) -> None:
    device = create_device(1, manual_clock.now())
    assert sign_in(device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 a ivan"
    linked_at = Contact.objects.values_list("linked_at", flat=True).get(id=device.pk)

    manual_clock.advance(seconds=20)
    assert sign_in(device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 a ivan"

    assert User.objects.count() == 1
    assert Contact.objects.values_list("linked_at", flat=True).get(id=device.pk) == linked_at
    assert_all_invariants()


@pytest.mark.django_db
def test_a_second_device_signs_in_and_is_linked_to_the_same_user(manual_clock: ManualClock) -> None:
    ivan = create_user("ivan", manual_clock.now())
    first_device = create_device(1, manual_clock.now(), user=ivan)
    second_device = create_device(2, manual_clock.now())

    assert sign_in(second_device, "IVAN", TEST_PASSWORD, manual_clock.now()) == "HT1 a ivan"

    assert set(ivan.devices.values_list("id", flat=True)) == {first_device.pk, second_device.pk}
    assert_all_invariants()


@pytest.mark.django_db
def test_usernames_collide_case_insensitively_and_keep_their_canonical_case(manual_clock: ManualClock) -> None:
    create_user("Bob", manual_clock.now())
    device = create_device(1, manual_clock.now())

    assert sign_in(device, "bob", TEST_PASSWORD, manual_clock.now()) == "HT1 a Bob"
    assert sign_in(device, "BOB", "another good password", manual_clock.now()) == "HT1 e WRONG_PASSWORD A BOB"
    assert User.objects.count() == 1


@pytest.mark.django_db
def test_a_wrong_password_is_refused_and_its_retry_counts_again(manual_clock: ManualClock) -> None:
    create_user("ivan", manual_clock.now())
    device = create_device(1, manual_clock.now())

    assert sign_in(device, "ivan", WRONG_PASSWORD, manual_clock.now()) == "HT1 e WRONG_PASSWORD A ivan"
    manual_clock.advance(seconds=20)
    assert sign_in(device, "ivan", WRONG_PASSWORD, manual_clock.now()) == "HT1 e WRONG_PASSWORD A ivan"

    assert read_failed_sign_in_count(device) == 2
    assert Contact.objects.get(id=device.pk).user is None


@pytest.mark.django_db
def test_five_wrong_passwords_throttle_the_device_without_checking_passwords(
    manual_clock: ManualClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    create_user("ivan", manual_clock.now())
    device = create_device(1, manual_clock.now())
    for _ in range(5):
        assert sign_in(device, "ivan", WRONG_PASSWORD, manual_clock.now()) == "HT1 e WRONG_PASSWORD A ivan"
        manual_clock.advance(minutes=1)

    monkeypatch.setattr(accounts, "check_password", refuse_to_hash)
    assert sign_in(device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 e RATE_LIMITED A ivan"
    manual_clock.advance(minutes=9, seconds=59)
    assert sign_in(device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 e RATE_LIMITED A ivan"


@pytest.mark.django_db
def test_the_throttle_window_starts_at_the_first_failure_and_the_count_starts_again_after_it(
    manual_clock: ManualClock,
) -> None:
    create_user("ivan", manual_clock.now())
    device = create_device(1, manual_clock.now())
    first_failure_at = manual_clock.now()
    for _ in range(5):
        sign_in(device, "ivan", WRONG_PASSWORD, manual_clock.now())
        manual_clock.advance(minutes=2)

    manual_clock.current_time = first_failure_at
    manual_clock.advance(minutes=15)
    assert sign_in(device, "ivan", WRONG_PASSWORD, manual_clock.now()) == "HT1 e WRONG_PASSWORD A ivan"
    assert read_failed_sign_in_count(device) == 1
    assert Contact.objects.get(id=device.pk).failed_sign_in_window_started_at == manual_clock.now()


@pytest.mark.django_db
def test_a_right_password_clears_the_count_of_wrong_ones(manual_clock: ManualClock) -> None:
    create_user("ivan", manual_clock.now())
    device = create_device(1, manual_clock.now())
    for _ in range(3):
        sign_in(device, "ivan", WRONG_PASSWORD, manual_clock.now())

    assert sign_in(device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 a ivan"

    device.refresh_from_db()
    assert device.failed_sign_in_count == 0
    assert device.failed_sign_in_window_started_at is None


@pytest.mark.django_db
def test_while_throttled_even_a_retry_of_a_successful_sign_in_is_rate_limited_until_the_window_ends(
    manual_clock: ManualClock,
) -> None:
    create_user("bob", manual_clock.now())
    device = create_device(1, manual_clock.now())
    for _ in range(5):
        sign_in(device, "bob", WRONG_PASSWORD, manual_clock.now())

    assert sign_in(device, "carol", TEST_PASSWORD, manual_clock.now()) == "HT1 a carol"
    manual_clock.advance(seconds=20)
    assert sign_in(device, "carol", TEST_PASSWORD, manual_clock.now()) == "HT1 e RATE_LIMITED A carol"

    manual_clock.advance(minutes=15)
    assert sign_in(device, "carol", TEST_PASSWORD, manual_clock.now()) == "HT1 a carol"
    assert_all_invariants()


@pytest.mark.django_db
def test_another_device_of_the_same_user_is_not_affected_by_a_throttled_one(manual_clock: ManualClock) -> None:
    ivan = create_user("ivan", manual_clock.now())
    throttled_device = create_device(1, manual_clock.now(), user=ivan)
    other_device = create_device(2, manual_clock.now(), user=ivan)
    for _ in range(5):
        sign_in(throttled_device, "ivan", WRONG_PASSWORD, manual_clock.now())

    assert sign_in(throttled_device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 e RATE_LIMITED A ivan"
    assert sign_in(other_device, "ivan", TEST_PASSWORD, manual_clock.now()) == "HT1 a ivan"


@pytest.mark.django_db
@pytest.mark.parametrize(
    "request_text",
    [
        "HT1 A ivan short",
        "HT1 A ivan  leading space",
        "HT1 A ivan trailing space ",
        "HT1 A ivan pass\tword with a tab",
        "HT1 A ivan " + "x" * 65,
        "HT1 A ivan " + "Ж" * 33,
    ],
)
def test_a_password_that_breaks_the_rules_is_refused_before_anything_else(
    relay: RelayHarness, manual_clock: ManualClock, request_text: str
) -> None:
    device = create_device(1, manual_clock.now())

    assert relay.receive_replies(device, request_text) == ["HT1 e PASSWORD_INVALID A ivan"]
    assert not User.objects.exists()


@pytest.mark.django_db
def test_passwords_are_compared_in_unicode_normal_form(manual_clock: ManualClock) -> None:
    device = create_device(1, manual_clock.now())
    decomposed_password = "cafe\N{COMBINING ACUTE ACCENT} au lait"
    composed_password = "caf\N{LATIN SMALL LETTER E WITH ACUTE} au lait"

    assert sign_in(device, "ivan", decomposed_password, manual_clock.now()) == "HT1 a ivan"
    assert sign_in(device, "ivan", composed_password, manual_clock.now()) == "HT1 a ivan"


@pytest.mark.django_db
def test_a_query_answers_the_canonical_name_the_name_as_sent_or_not_signed_in(
    relay: RelayHarness, manual_clock: ManualClock
) -> None:
    ivan = create_user("ivan", manual_clock.now())
    create_user("Bob", manual_clock.now())
    signed_in_device = create_device(1, manual_clock.now(), user=ivan)
    unlinked_device = create_device(2, manual_clock.now())

    assert relay.receive_replies(signed_in_device, "HT1 Q bob") == ["HT1 q Bob 1"]
    assert relay.receive_replies(signed_in_device, "HT1 Q Carol") == ["HT1 q Carol 0"]
    assert relay.receive_replies(signed_in_device, "HT1 Q IVAN") == ["HT1 q ivan 1"]
    assert relay.receive_replies(unlinked_device, "HT1 Q bob") == ["HT1 e NOT_SIGNED_IN Q bob"]


@pytest.mark.django_db
def test_user_exists_reads_the_link_from_the_database(manual_clock: ManualClock) -> None:
    ivan = create_user("ivan", manual_clock.now())
    device = create_device(1, manual_clock.now())
    Contact.objects.filter(id=device.pk).update(user=ivan, linked_at=manual_clock.now())

    query_outcome = user_exists(device, QueryRequest(username="ivan"))

    assert format_server_message(query_outcome.reply) == "HT1 q ivan 1"


@pytest.mark.django_db
def test_signing_in_as_another_user_relinks_the_device_and_cancels_the_previous_users_work(
    relay: RelayHarness, manual_clock: ManualClock
) -> None:
    ivan, bob = create_user("ivan", manual_clock.now()), create_user("bob", manual_clock.now())
    create_user("carol", manual_clock.now())
    ivans_device = create_device(1, manual_clock.now(), user=ivan)
    bobs_device = create_device(2, manual_clock.now(), user=bob)
    relay.receive_replies(bobs_device, "HT1 M ivan 1 1/1 first")
    relay.receive_replies(bobs_device, "HT1 M ivan 2 1/1 second")
    relay.receive_replies(ivans_device, "HT1 M bob 7 1/1 from ivan")
    relay.receive_replies(bobs_device, "HT1 K ivan 7 1")
    relay.receive_replies(ivans_device, "HT1 F bob")
    assert RefreshSession.objects.filter(device=ivans_device, state=RefreshSession.State.ACTIVE).exists()
    assert ReceiptNotification.objects.filter(device=ivans_device, state=ReceiptNotification.State.PENDING).exists()

    assert relay.receive_replies(ivans_device, f"HT1 A carol {TEST_PASSWORD}") == ["HT1 a carol"]

    assert set(MessageDelivery.objects.filter(device=ivans_device).values_list("state", flat=True)) == {
        MessageDelivery.State.CANCELLED
    }
    assert set(ReceiptNotification.objects.filter(device=ivans_device).values_list("state", flat=True)) == {
        ReceiptNotification.State.CANCELLED
    }
    assert set(RefreshSession.objects.filter(device=ivans_device).values_list("state", flat=True)) == {
        RefreshSession.State.CANCELLED
    }
    assert Contact.objects.get(id=ivans_device.pk).user is not None
    assert relay.send_due_texts(ivans_device) == []
    assert_all_invariants()


@pytest.mark.django_db
def test_relinking_keeps_a_receipt_the_device_confirmed_at_read(manual_clock: ManualClock) -> None:
    ivan, bob, carol = (create_user(username, manual_clock.now()) for username in ("ivan", "bob", "carol"))
    device = create_device(1, manual_clock.now(), user=ivan)
    create_device(2, manual_clock.now(), user=bob)
    ReceiptNotification.objects.create(
        message=ivan.sent_messages.create(
            recipient=bob,
            client_message_id=1,
            part_count=1,
            part_texts=["hi"],
            text="hi",
            created_at=manual_clock.now(),
            last_part_at=manual_clock.now(),
            accepted_at=manual_clock.now(),
            delivered_at=manual_clock.now(),
            read_at=manual_clock.now(),
        ),
        device=device,
        target_level=2,
        confirmed_level=2,
        state=ReceiptNotification.State.CONFIRMED,
        maximum_attempts=6,
        next_attempt_at=manual_clock.now(),
        created_at=manual_clock.now(),
        updated_at=manual_clock.now(),
    )

    locked_device = Contact.objects.select_for_update().get(id=device.pk)
    relink_device(locked_device, carol, manual_clock.now())

    assert ReceiptNotification.objects.get().state == ReceiptNotification.State.CONFIRMED
    assert Contact.objects.get(id=device.pk).user == carol


@pytest.mark.django_db(transaction=True)
def test_two_devices_registering_the_same_username_at_once_create_one_user(manual_clock: ManualClock) -> None:
    first_device = create_device(1, manual_clock.now())
    second_device = create_device(2, manual_clock.now())
    start_together = threading.Barrier(2)

    def register(device: Contact, password: str) -> str:
        start_together.wait(timeout=10)
        return sign_in(device, "ivan", password, manual_clock.now())

    replies = run_concurrently(
        lambda: register(first_device, TEST_PASSWORD),
        lambda: register(second_device, WRONG_PASSWORD),
    )

    assert sorted(replies) == ["HT1 a ivan", "HT1 e WRONG_PASSWORD A ivan"]
    ivan = User.objects.get()
    winning_password = TEST_PASSWORD if replies[0] == "HT1 a ivan" else WRONG_PASSWORD
    assert check_password(winning_password, ivan.password_hash)
    assert ivan.devices.count() == 1
    assert_all_invariants()
