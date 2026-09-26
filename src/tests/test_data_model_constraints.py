from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from django.db import IntegrityError, transaction

from directory.models import Contact, User
from messaging.models import Message, ReceiptNotification
from node.models import NodeCommand, NodeSetupRun, WorkerStatus

pytestmark = pytest.mark.django_db

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def create_user(username: str) -> User:
    return User.objects.create(username=username, password_hash="unused", created_at=NOW)


def create_device(user: User, public_key_first_character: str = "a") -> Contact:
    return Contact.objects.create(
        public_key=public_key_first_character * 64,
        source=Contact.Source.CARD,
        added_at=NOW,
        user=user,
        linked_at=NOW,
    )


def create_message(sender: User, recipient: User, part_texts: list[str | None]) -> Message:
    return Message.objects.create(
        sender=sender,
        recipient=recipient,
        client_message_id=1790294400123456,
        part_count=2,
        part_texts=part_texts,
        created_at=NOW,
        last_part_at=NOW,
    )


def assert_violates_a_constraint(create_row: Callable[[], object]) -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        create_row()


def test_usernames_are_unique_case_insensitively_and_keep_their_case() -> None:
    create_user("Bob")

    assert User.objects.get(username_lookup="bob").username == "Bob"
    assert_violates_a_constraint(lambda: create_user("bob"))
    assert_violates_a_constraint(lambda: create_user("b_b"))


def test_two_contacts_may_not_share_a_six_byte_key_prefix() -> None:
    Contact.objects.create(public_key="ab" * 32, source=Contact.Source.CARD, added_at=NOW)

    assert_violates_a_constraint(
        lambda: Contact.objects.create(public_key="ab" * 6 + "cd" * 26, source=Contact.Source.CARD, added_at=NOW)
    )


def test_a_device_has_a_link_time_exactly_when_it_has_a_user() -> None:
    assert_violates_a_constraint(
        lambda: Contact.objects.create(public_key="c" * 64, source=Contact.Source.CARD, added_at=NOW, linked_at=NOW)
    )


def test_the_part_texts_array_has_part_count_entries() -> None:
    sender, recipient = create_user("ivan"), create_user("Bob")
    create_device(sender)

    assert create_message(sender, recipient, [None, None]).accepted_at is None
    assert_violates_a_constraint(lambda: create_message(sender, recipient, [None]))


def test_a_receipt_is_confirmed_exactly_when_its_levels_meet_unless_cancelled() -> None:
    sender, recipient = create_user("ivan"), create_user("Bob")
    device = create_device(sender)
    message = Message.objects.create(
        sender=sender,
        recipient=recipient,
        client_message_id=1,
        part_count=1,
        part_texts=["hello"],
        text="hello",
        created_at=NOW,
        last_part_at=NOW,
        accepted_at=NOW,
        delivered_at=NOW,
    )

    def create_receipt(state: str, confirmed_level: int) -> ReceiptNotification:
        return ReceiptNotification.objects.create(
            message=message,
            device=device,
            target_level=ReceiptNotification.TargetLevel.DELIVERED,
            confirmed_level=confirmed_level,
            state=state,
            maximum_attempts=6,
            next_attempt_at=NOW,
            created_at=NOW,
            updated_at=NOW,
        )

    assert_violates_a_constraint(lambda: create_receipt(ReceiptNotification.State.PENDING, confirmed_level=1))
    assert_violates_a_constraint(lambda: create_receipt(ReceiptNotification.State.CONFIRMED, confirmed_level=0))
    assert create_receipt(ReceiptNotification.State.CANCELLED, confirmed_level=1).pk is not None


def test_only_one_setup_run_is_active_and_a_final_run_is_inactive() -> None:
    NodeSetupRun.objects.create(purpose=NodeSetupRun.Purpose.INITIAL, started_at=NOW)

    assert_violates_a_constraint(
        lambda: NodeSetupRun.objects.create(purpose=NodeSetupRun.Purpose.RECONFIGURE, started_at=NOW)
    )
    assert_violates_a_constraint(
        lambda: NodeSetupRun.objects.create(
            purpose=NodeSetupRun.Purpose.INITIAL,
            state=NodeSetupRun.State.COMPLETED,
            started_at=NOW,
            is_active=True,
        )
    )


def test_only_one_node_command_runs_at_a_time() -> None:
    def create_running_command() -> NodeCommand:
        return NodeCommand.objects.create(
            kind=NodeCommand.Kind.SEND_ADVERT,
            state=NodeCommand.State.RUNNING,
            created_at=NOW,
            expires_at=NOW + timedelta(minutes=5),
        )

    create_running_command()

    assert_violates_a_constraint(create_running_command)


def test_worker_status_has_a_single_row() -> None:
    WorkerStatus.objects.create()

    assert_violates_a_constraint(lambda: WorkerStatus.objects.create(id=2))
