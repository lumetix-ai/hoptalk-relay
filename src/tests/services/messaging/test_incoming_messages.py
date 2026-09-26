import itertools
import threading
from datetime import datetime

import pytest

from directory.models import Contact, User
from messaging import incoming_messages
from messaging.incoming_messages import accept_message_part, delete_expired_incomplete_messages
from messaging.models import InboundDirectMessage, Message, MessageDelivery, RefreshSession
from messaging.request_processing import QueuedReply, ReplyReadiness
from protocol.formatting import format_server_message
from protocol.message_types import MessagePartRequest
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, create_device, create_user
from tests.services.node.database_threads import run_concurrently


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("Bob", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


@pytest.fixture
def bobs_device(bob: User, manual_clock: ManualClock) -> Contact:
    return create_device(2, manual_clock.now(), user=bob)


@pytest.mark.django_db
def test_a_single_part_message_is_accepted_at_once_with_its_deliveries(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    processing_result = relay.receive(ivans_device, "HT1 M bob 1790294400123456 1/1 Привет, Боб!")

    assert processing_result is not None
    [reply] = processing_result.replies
    assert reply.text == "HT1 k Bob 1790294400123456 1"
    assert reply.readiness == ReplyReadiness.IMMEDIATE
    assert reply.reply_key == "M:bob:1790294400123456"
    message = Message.objects.get()
    assert message.text == "Привет, Боб!"
    assert message.accepted_at == manual_clock.now()
    assert message.sender_device == ivans_device
    assert list(message.deliveries.values_list("device_id", "state")) == [
        (bobs_device.pk, MessageDelivery.State.PENDING)
    ]
    assert_all_invariants()


@pytest.mark.django_db
@pytest.mark.parametrize("part_order", list(itertools.permutations([1, 2, 3])))
def test_three_parts_in_any_order_make_one_message_and_only_the_last_status_is_complete(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact, part_order: tuple[int, ...]
) -> None:
    part_texts = {1: "one ", 2: "two ", 3: "three"}
    replies: list[QueuedReply] = []
    for part_number in part_order:
        processing_result = relay.receive(ivans_device, f"HT1 M Bob 5 {part_number}/3 {part_texts[part_number]}")
        assert processing_result is not None
        replies.extend(processing_result.replies)

    assert [reply.readiness for reply in replies] == [
        ReplyReadiness.COALESCED_INCOMPLETE_STATUS,
        ReplyReadiness.COALESCED_INCOMPLETE_STATUS,
        ReplyReadiness.IMMEDIATE,
    ]
    assert replies[-1].text == "HT1 k Bob 5 111"
    assert Message.objects.get().text == "one two three"
    assert MessageDelivery.objects.count() == 1
    assert_all_invariants()


@pytest.mark.django_db
def test_an_incomplete_status_reports_exactly_the_held_parts(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 3/3 three") == ["HT1 k Bob 5 001"]
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 1/3 one") == ["HT1 k Bob 5 101"]


@pytest.mark.django_db
def test_a_repeated_part_stores_nothing_and_answers_the_current_set(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one")
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one") == ["HT1 k Bob 5 10"]
    relay.receive_replies(ivans_device, "HT1 M Bob 5 2/2 two")
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 2/2 two") == ["HT1 k Bob 5 11"]
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one") == ["HT1 k Bob 5 11"]

    assert Message.objects.get().part_texts == ["one", "two"]
    assert MessageDelivery.objects.count() == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "conflicting_part",
    [
        "HT1 M carol 5 1/2 one",
        "HT1 M Bob 5 1/3 one",
        "HT1 M Bob 5 1/2 another text",
    ],
    ids=["another recipient", "another part count", "another text of a held part"],
)
def test_the_same_id_for_different_content_is_an_id_conflict_and_stores_nothing(
    relay: RelayHarness,
    manual_clock: ManualClock,
    ivans_device: Contact,
    bobs_device: Contact,
    conflicting_part: str,
) -> None:
    create_user("carol", manual_clock.now())
    relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one")

    reply = relay.receive_replies(ivans_device, conflicting_part)

    reference = conflicting_part.split(" ")[2]
    assert reply == [f"HT1 e ID_CONFLICT M {reference} 5"]
    assert Message.objects.get().part_texts == ["one", None]


@pytest.mark.django_db
def test_another_device_of_the_sender_may_repeat_a_held_part_but_not_add_one(
    relay: RelayHarness, manual_clock: ManualClock, ivan: User, ivans_device: Contact, bobs_device: Contact
) -> None:
    ivans_second_device = create_device(3, manual_clock.now(), user=ivan)
    relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one")

    assert relay.receive_replies(ivans_second_device, "HT1 M Bob 5 1/2 one") == ["HT1 k Bob 5 10"]
    assert relay.receive_replies(ivans_second_device, "HT1 M Bob 5 2/2 two") == ["HT1 e ID_CONFLICT M Bob 5"]
    assert relay.receive_replies(ivans_second_device, "HT1 M Bob 5 1/2 uno") == ["HT1 e ID_CONFLICT M Bob 5"]
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 2/2 two") == ["HT1 k Bob 5 11"]
    assert relay.receive_replies(ivans_second_device, "HT1 M Bob 5 2/2 two") == ["HT1 k Bob 5 11"]
    assert_all_invariants()


@pytest.mark.django_db
def test_an_incomplete_message_is_deleted_a_day_after_its_last_part_and_a_repeated_part_keeps_it(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one")
    relay.receive_replies(ivans_device, "HT1 M Bob 6 1/2 one")
    manual_clock.advance(hours=20)
    relay.receive_replies(ivans_device, "HT1 M Bob 6 1/2 one")
    manual_clock.advance(hours=4)

    assert delete_expired_incomplete_messages(manual_clock.now()) == 1

    assert list(Message.objects.values_list("client_message_id", flat=True)) == [6]
    manual_clock.advance(hours=20)
    assert delete_expired_incomplete_messages(manual_clock.now()) == 1
    assert relay.receive_replies(ivans_device, "HT1 M Bob 6 2/2 two") == ["HT1 k Bob 6 01"]


@pytest.mark.django_db
def test_a_part_of_104_bytes_is_accepted_and_one_of_105_bytes_is_invalid(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    assert relay.receive_replies(ivans_device, "HT1 M Bob 5 1/1 " + "Ж" * 52) == ["HT1 k Bob 5 1"]
    assert relay.receive_replies(ivans_device, "HT1 M Bob 6 1/1 " + "a" * 105) == ["HT1 e PART_INVALID M Bob 6"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "invalid_part", ["HT1 M Bob 5 2/1 x", "HT1 M Bob 5 1/11 x", "HT1 M Bob 5 1/1 ", "HT1 M Bob 5 1/1 a\rb"]
)
def test_part_numbers_counts_and_texts_that_break_the_value_rules_are_invalid(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact, invalid_part: str
) -> None:
    assert relay.receive_replies(ivans_device, invalid_part) == ["HT1 e PART_INVALID M Bob 5"]
    assert not Message.objects.exists()


@pytest.mark.django_db
def test_parts_for_oneself_for_nobody_or_from_an_unlinked_device_are_refused(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bobs_device: Contact
) -> None:
    unlinked_device = create_device(3, manual_clock.now())

    assert relay.receive_replies(ivans_device, "HT1 M IVAN 5 1/1 me") == ["HT1 e SELF M IVAN 5"]
    assert relay.receive_replies(ivans_device, "HT1 M carol 5 1/1 hi") == ["HT1 e NO_SUCH_USER M carol 5"]
    assert relay.receive_replies(unlinked_device, "HT1 M Bob 5 1/1 hi") == ["HT1 e NOT_SIGNED_IN M Bob 5"]
    assert not Message.objects.exists()


@pytest.mark.django_db
def test_a_message_to_a_user_without_devices_is_accepted_without_deliveries(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    create_user("carol", manual_clock.now())

    assert relay.receive_replies(ivans_device, "HT1 M carol 5 1/1 hi") == ["HT1 k carol 5 1"]

    assert Message.objects.get().accepted_at is not None
    assert not MessageDelivery.objects.exists()


@pytest.mark.django_db
def test_a_failure_while_creating_deliveries_leaves_the_message_incomplete_and_without_deliveries(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact, monkeypatch: pytest.MonkeyPatch
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one")

    def fail_to_create_deliveries(message: Message, now: datetime) -> int:
        MessageDelivery.objects.create(
            message=message, device=bobs_device, maximum_attempts=6, next_attempt_at=now, created_at=now
        )
        raise RuntimeError("fan-out failed")

    monkeypatch.setattr(incoming_messages, "create_deliveries_for_accepted_message", fail_to_create_deliveries)
    processing_result = relay.receive(ivans_device, "HT1 M Bob 5 2/2 two")

    assert processing_result is not None
    assert processing_result.replies == ()
    assert processing_result.processing_state == InboundDirectMessage.ProcessingState.FAILED
    message = Message.objects.get()
    assert message.accepted_at is None
    assert message.part_texts == ["one", None]
    assert not MessageDelivery.objects.exists()
    assert "fan-out failed" in InboundDirectMessage.objects.get(id=processing_result.inbox_row_id).processing_error
    assert_all_invariants()


@pytest.mark.django_db
def test_an_incomplete_message_is_found_by_no_read_acknowledgement_or_refresh(
    relay: RelayHarness, ivans_device: Contact, bobs_device: Contact
) -> None:
    relay.receive_replies(ivans_device, "HT1 M Bob 5 1/2 one")

    assert relay.receive_replies(bobs_device, "HT1 R ivan 5") == ["HT1 e NOT_FOUND R ivan 5"]
    assert relay.receive_replies(bobs_device, "HT1 K ivan 5 11") == []
    assert relay.receive_replies(bobs_device, "HT1 F ivan") == ["HT1 f ivan 0"]
    assert relay.receive_replies(bobs_device, "HT1 F *") == ["HT1 f * 0"]
    assert not RefreshSession.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_two_devices_of_the_sender_sending_the_first_parts_at_once_create_one_message(
    manual_clock: ManualClock,
) -> None:
    ivan, bob = create_user("ivan", manual_clock.now()), create_user("bob", manual_clock.now())
    first_device = create_device(1, manual_clock.now(), user=ivan)
    second_device = create_device(2, manual_clock.now(), user=ivan)
    create_device(3, manual_clock.now(), user=bob)
    start_together = threading.Barrier(2)

    def send_first_part(device: Contact) -> str:
        start_together.wait(timeout=10)
        part = MessagePartRequest(recipient_username="bob", message_id=5, part_number=1, part_count=2, part_text="one")
        return format_server_message(accept_message_part(device, part, manual_clock.now()).reply)

    replies = run_concurrently(lambda: send_first_part(first_device), lambda: send_first_part(second_device))

    assert replies == ["HT1 k bob 5 10", "HT1 k bob 5 10"]
    assert Message.objects.count() == 1
    assert_all_invariants()
