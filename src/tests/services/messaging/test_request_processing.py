from datetime import timedelta

import pytest

from directory.models import Contact, User
from messaging.inbound_log import ReceivedDirectMessageFrame, record_inbound_frame
from messaging.models import InboundDirectMessage
from messaging.request_processing import process_inbound_direct_message
from messaging.route_reset_evidence import record_path_update
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, create_device, create_user

Classification = InboundDirectMessage.Classification


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


def read_inbox_row(inbox_row_id: int) -> InboundDirectMessage:
    return InboundDirectMessage.objects.get(id=inbox_row_id)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("text", "classification", "request_type", "expected_replies"),
    [
        ("hello there", Classification.NOT_PROTOCOL, "", []),
        ("HT1", Classification.NOT_PROTOCOL, "", []),
        ("HT2 Q bob", Classification.UNSUPPORTED_VERSION, "", ["HT1 e VERSION ? 2"]),
        ("HT10 F *", Classification.UNSUPPORTED_VERSION, "", ["HT1 e VERSION ? 10"]),
        ("HT2 e SYNTAX ?", Classification.UNSUPPORTED_VERSION, "", []),
        ("HT2 ", Classification.UNSUPPORTED_VERSION, "", []),
        ("HT1 a ivan", Classification.SERVER_TYPE_IGNORED, "a", []),
        ("HT1 m ivan 5 1/1 hello", Classification.SERVER_TYPE_IGNORED, "m", []),
        ("HT1 e SYNTAX ?", Classification.SERVER_TYPE_IGNORED, "e", []),
        ("HT1 e NOT_A_KNOWN_CODE Q", Classification.SERVER_TYPE_IGNORED, "e", []),
        ("HT1 k", Classification.SERVER_TYPE_IGNORED, "k", []),
        ("HT1 z something new", Classification.SERVER_TYPE_IGNORED, "z", []),
        ("HT1 K ivan 5 1", Classification.ACKNOWLEDGEMENT, "K", []),
        ("HT1 C ivan 5 D", Classification.ACKNOWLEDGEMENT, "C", []),
        ("HT1 K ivan 05 1", Classification.ACKNOWLEDGEMENT, "K", []),
        ("HT1 C ivan 5 X", Classification.ACKNOWLEDGEMENT, "C", []),
        ("HT1 Q ivan", Classification.REQUEST, "Q", ["HT1 q ivan 1"]),
        ("HT1 Q iv", Classification.SYNTAX_ERROR, "Q", ["HT1 e SYNTAX Q"]),
        ("HT1 A ab hunter2222", Classification.SYNTAX_ERROR, "A", ["HT1 e SYNTAX A"]),
        ("HT1 M Bob 01 1/1 x", Classification.SYNTAX_ERROR, "M", ["HT1 e SYNTAX M"]),
        ("HT1 M Bob 5 1/100 x", Classification.SYNTAX_ERROR, "M", ["HT1 e SYNTAX M Bob 5"]),
        ("HT1 R ivan 5 extra", Classification.SYNTAX_ERROR, "R", ["HT1 e SYNTAX R ivan 5"]),
        ("HT1 F ivan ", Classification.SYNTAX_ERROR, "F", ["HT1 e SYNTAX F ivan"]),
        ("HT1 X foo", Classification.REQUEST, "X", ["HT1 e UNSUPPORTED X"]),
        ("HT1  Q bob", Classification.SYNTAX_ERROR, "", ["HT1 e SYNTAX ?"]),
        ("HT1 ", Classification.SYNTAX_ERROR, "", ["HT1 e SYNTAX ?"]),
        ("HT1 5", Classification.SYNTAX_ERROR, "", ["HT1 e SYNTAX ?"]),
        ("HT1 Qbob", Classification.SYNTAX_ERROR, "Q", ["HT1 e SYNTAX Q"]),
    ],
)
def test_every_direct_message_is_classified_and_only_requests_are_answered(
    relay: RelayHarness,
    ivans_device: Contact,
    text: str,
    classification: InboundDirectMessage.Classification,
    request_type: str,
    expected_replies: list[str],
) -> None:
    processing_result = relay.receive(ivans_device, text)

    assert processing_result is not None
    assert [reply.text for reply in processing_result.replies] == expected_replies
    inbox_row = read_inbox_row(processing_result.inbox_row_id)
    assert (inbox_row.classification, inbox_row.request_type) == (classification, request_type)
    assert inbox_row.processing_state == InboundDirectMessage.ProcessingState.PROCESSED
    assert inbox_row.reply_summary == " / ".join(expected_replies)
    assert inbox_row.outcome_summary


@pytest.mark.django_db
def test_a_frame_from_an_unknown_sender_is_dropped_and_asks_for_a_reconciliation(manual_clock: ManualClock) -> None:
    recorded_frame = record_inbound_frame(
        ReceivedDirectMessageFrame(
            sender_public_key_prefix="abcdefabcdef",
            sender_timestamp=1_790_000_000,
            text="HT1 A ivan correct horse battery",
            text_type=0,
            path_length=1,
            signal_to_noise_ratio=None,
            received_at=manual_clock.now(),
        ),
        manual_clock.now(),
    )

    processing_result = process_inbound_direct_message(
        recorded_frame.inbox_row_id, manual_clock.now(), original_text="HT1 A ivan correct horse battery"
    )

    assert processing_result.replies == ()
    assert processing_result.reconciliation_needed
    assert not processing_result.flood_arrival_route_reset_needed
    assert processing_result.classification == Classification.UNKNOWN_SENDER
    assert not User.objects.exists()


@pytest.mark.django_db
def test_a_text_type_other_than_plain_text_is_dropped(relay: RelayHarness, ivans_device: Contact) -> None:
    processing_result = relay.receive(ivans_device, "HT1 Q ivan", text_type=1)

    assert processing_result is not None
    assert processing_result.replies == ()
    assert processing_result.classification == Classification.UNSUPPORTED_TEXT_TYPE


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("text", "expected_replies"),
    [
        ("HT1 A ivan correct horse battery", ["HT1 a ivan"]),
        ("HT1 Q ivan", ["HT1 e NOT_SIGNED_IN Q ivan"]),
        ("HT1 M ivan 5 1/1 hi", ["HT1 e NOT_SIGNED_IN M ivan 5"]),
        ("HT1 R ivan 5", ["HT1 e NOT_SIGNED_IN R ivan 5"]),
        ("HT1 F *", ["HT1 e NOT_SIGNED_IN F *"]),
        ("HT1 K ivan 5 1", []),
        ("HT1 C ivan 5 D", []),
    ],
)
def test_a_contact_that_has_not_signed_in_may_only_sign_in(
    relay: RelayHarness, manual_clock: ManualClock, text: str, expected_replies: list[str]
) -> None:
    unlinked_device = create_device(2, manual_clock.now())

    assert relay.receive_replies(unlinked_device, text) == expected_replies


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("text", "reply_key"),
    [
        ("HT1 A IVAN correct horse battery", "A:ivan"),
        ("HT1 Q Bob", "Q:bob"),
        ("HT1 M Bob 1790294400123456 1/1 hi", "M:bob:1790294400123456"),
        ("HT1 R Bob 5", "R:bob:5"),
        ("HT1 F Bob", "F:bob"),
        ("HT1 F *", "F:*"),
        ("HT1 M Bob 5 1/100 x", "M:bob:5"),
    ],
)
def test_replies_are_keyed_by_the_request_they_answer(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, text: str, reply_key: str
) -> None:
    create_user("Bob", manual_clock.now())

    processing_result = relay.receive(ivans_device, text)

    assert processing_result is not None
    assert [reply.reply_key for reply in processing_result.replies] == [reply_key]


@pytest.mark.django_db
@pytest.mark.parametrize("text", ["HT1  Q bob", "HT2 Q bob", "HT1 X foo"])
def test_a_reply_without_a_reference_is_keyed_by_its_inbox_row(
    relay: RelayHarness, ivans_device: Contact, text: str
) -> None:
    processing_result = relay.receive(ivans_device, text)

    assert processing_result is not None
    assert [reply.reply_key for reply in processing_result.replies] == [f"?:{processing_result.inbox_row_id}"]


@pytest.mark.django_db
def test_a_new_flood_arrival_asks_for_a_route_reset_unless_a_route_was_just_learned(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    flood_result = relay.receive(ivans_device, "HT1 Q ivan", path_length=2)
    direct_result = relay.receive(ivans_device, "HT1 Q ivan")
    record_path_update(ivans_device.public_key, manual_clock.now())
    manual_clock.advance(seconds=10)
    flood_after_path_update = relay.receive(ivans_device, "HT1 K ivan 5 1", path_length=4)

    assert flood_result is not None
    assert flood_result.flood_arrival_route_reset_needed
    assert direct_result is not None
    assert not direct_result.flood_arrival_route_reset_needed
    assert flood_after_path_update is not None
    assert not flood_after_path_update.flood_arrival_route_reset_needed


@pytest.mark.django_db
def test_a_flood_arrival_processed_long_after_it_arrived_asks_for_no_route_reset(
    manual_clock: ManualClock, ivans_device: Contact
) -> None:
    recorded_frame = record_inbound_frame(
        ReceivedDirectMessageFrame(
            sender_public_key_prefix=ivans_device.public_key[:12],
            sender_timestamp=1_790_000_000,
            text="HT1 Q ivan",
            text_type=0,
            path_length=2,
            signal_to_noise_ratio=None,
            received_at=manual_clock.now(),
        ),
        manual_clock.now(),
    )

    processing_result = process_inbound_direct_message(
        recorded_frame.inbox_row_id, manual_clock.now() + timedelta(seconds=61)
    )

    assert [reply.text for reply in processing_result.replies] == ["HT1 q ivan 1"]
    assert not processing_result.flood_arrival_route_reset_needed
