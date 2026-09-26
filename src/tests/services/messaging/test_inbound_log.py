import hashlib
from datetime import timedelta

import pytest

from directory.models import Contact, User
from messaging.inbound_log import (
    ReceivedDirectMessageFrame,
    find_unprocessed_inbox_row_ids,
    record_flood_arrival_route_reset,
    record_inbound_frame,
    redact_stored_text,
)
from messaging.models import InboundDirectMessage
from messaging.request_processing import process_inbound_direct_message
from tests.invariants import assert_all_invariants
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import TEST_PASSWORD, RelayHarness, create_device, create_user


@pytest.fixture
def ivan(manual_clock: ManualClock) -> User:
    return create_user("ivan", manual_clock.now())


@pytest.fixture
def ivans_device(ivan: User, manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=ivan)


def build_frame(
    manual_clock: ManualClock,
    text: str,
    sender_public_key_prefix: str,
    sender_timestamp: int = 1_790_000_000,
    path_length: int = 255,
) -> ReceivedDirectMessageFrame:
    return ReceivedDirectMessageFrame(
        sender_public_key_prefix=sender_public_key_prefix,
        sender_timestamp=sender_timestamp,
        text=text,
        text_type=0,
        path_length=path_length,
        signal_to_noise_ratio=9.25,
        received_at=manual_clock.now(),
    )


@pytest.mark.django_db
def test_a_frame_is_recorded_as_a_received_inbox_row_before_anything_else_happens(
    manual_clock: ManualClock, ivans_device: Contact
) -> None:
    frame = build_frame(manual_clock, "HT1 Q bob", ivans_device.public_key[:12], path_length=2)

    recorded_frame = record_inbound_frame(frame, manual_clock.now())

    assert (recorded_frame.contact_id, recorded_frame.is_firmware_repeat, recorded_frame.arrived_by_flood) == (
        ivans_device.pk,
        False,
        True,
    )
    inbox_row = InboundDirectMessage.objects.get(id=recorded_frame.inbox_row_id)
    assert inbox_row.processing_state == InboundDirectMessage.ProcessingState.RECEIVED
    assert inbox_row.classification == InboundDirectMessage.Classification.UNCLASSIFIED
    assert (inbox_row.text, inbox_row.path_length, inbox_row.signal_to_noise_ratio) == ("HT1 Q bob", 2, 9.25)
    assert inbox_row.contact_label == str(ivans_device)
    assert inbox_row.text_sha256 == hashlib.sha256(b"HT1 Q bob").hexdigest()
    assert Contact.objects.get(id=ivans_device.pk).last_heard_at == manual_clock.now()
    assert find_unprocessed_inbox_row_ids() == [recorded_frame.inbox_row_id]


@pytest.mark.django_db
def test_a_firmware_level_repeat_is_counted_and_never_processed_or_answered(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    first_result = relay.receive(ivans_device, "HT1 Q ivan", sender_timestamp=1_790_000_500)
    assert first_result is not None
    manual_clock.advance(seconds=8)

    assert relay.receive(ivans_device, "HT1 Q ivan", sender_timestamp=1_790_000_500) is None

    original_row = InboundDirectMessage.objects.get()
    assert (original_row.duplicate_count, original_row.last_duplicate_at) == (1, manual_clock.now())
    assert Contact.objects.get(id=ivans_device.pk).last_heard_at == manual_clock.now()
    assert find_unprocessed_inbox_row_ids() == []
    assert_all_invariants()


@pytest.mark.django_db
def test_the_same_text_with_a_new_timestamp_is_a_client_retry_and_is_answered_again(
    relay: RelayHarness, ivans_device: Contact
) -> None:
    assert relay.receive_replies(ivans_device, "HT1 Q ivan") == ["HT1 q ivan 1"]
    assert relay.receive_replies(ivans_device, "HT1 Q ivan") == ["HT1 q ivan 1"]
    assert InboundDirectMessage.objects.count() == 2


@pytest.mark.django_db
def test_a_repeat_after_a_day_is_recorded_and_processed_again(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact
) -> None:
    relay.receive(ivans_device, "HT1 Q ivan", sender_timestamp=1_790_000_500)
    manual_clock.advance(hours=24, seconds=1)

    processing_result = relay.receive(ivans_device, "HT1 Q ivan", sender_timestamp=1_790_000_500)

    assert processing_result is not None
    assert [reply.text for reply in processing_result.replies] == ["HT1 q ivan 1"]


@pytest.mark.django_db
def test_a_sign_in_request_is_stored_without_its_password_and_hashed_as_stored(
    relay: RelayHarness, manual_clock: ManualClock
) -> None:
    device = create_device(2, manual_clock.now())

    assert relay.receive_replies(device, f"HT1 A ivan {TEST_PASSWORD}") == ["HT1 a ivan"]

    inbox_row = InboundDirectMessage.objects.get()
    assert inbox_row.text == "HT1 A ivan ********"
    assert inbox_row.text_sha256 == hashlib.sha256(b"HT1 A ivan ********").hexdigest()
    assert TEST_PASSWORD not in inbox_row.outcome_summary + inbox_row.reply_summary


@pytest.mark.parametrize(
    ("received_text", "stored_text"),
    [
        ("HT1 A ivan correct horse battery", "HT1 A ivan ********"),
        ("HT1 A ab short", "HT1 A ab ********"),
        ("HT2 A ivan secret", "HT2 A ivan ********"),
        ("HT1 A  ivan secret", "HT1 A  ********"),
        ("HT1 A ivan", "HT1 A ivan"),
        ("HT1 M bob 5 1/1 HT1 A ivan secret", "HT1 M bob 5 1/1 HT1 A ivan secret"),
        ("hello HT1 A ivan secret", "hello HT1 A ivan secret"),
    ],
)
def test_only_a_text_that_starts_like_a_sign_in_request_loses_what_follows_its_username(
    received_text: str, stored_text: str
) -> None:
    assert redact_stored_text(received_text) == stored_text


@pytest.mark.django_db
def test_a_frame_from_an_unknown_prefix_is_recorded_without_a_contact_and_never_deduplicated(
    manual_clock: ManualClock,
) -> None:
    frame = build_frame(manual_clock, "HT1 Q bob", "abcdefabcdef")

    first_recording = record_inbound_frame(frame, manual_clock.now())
    second_recording = record_inbound_frame(frame, manual_clock.now())

    assert first_recording.contact_id is None
    assert not second_recording.is_firmware_repeat
    assert InboundDirectMessage.objects.filter(contact__isnull=True).count() == 2


@pytest.mark.django_db
def test_a_sign_in_recorded_before_a_restart_is_not_answered_because_its_password_was_never_stored(
    manual_clock: ManualClock,
) -> None:
    device = create_device(2, manual_clock.now())
    recorded_frame = record_inbound_frame(
        build_frame(manual_clock, f"HT1 A ivan {TEST_PASSWORD}", device.public_key[:12]), manual_clock.now()
    )

    processing_result = process_inbound_direct_message(recorded_frame.inbox_row_id, manual_clock.now())

    assert processing_result.replies == ()
    assert not User.objects.exists()
    inbox_row = InboundDirectMessage.objects.get()
    assert inbox_row.processing_state == InboundDirectMessage.ProcessingState.PROCESSED
    assert "not answered" in inbox_row.outcome_summary


@pytest.mark.django_db
def test_a_row_is_processed_once_even_when_it_is_processed_again(
    manual_clock: ManualClock, ivans_device: Contact
) -> None:
    recorded_frame = record_inbound_frame(
        build_frame(manual_clock, "HT1 Q ivan", ivans_device.public_key[:12]), manual_clock.now()
    )
    first_result = process_inbound_direct_message(recorded_frame.inbox_row_id, manual_clock.now())
    manual_clock.advance(seconds=1)

    second_result = process_inbound_direct_message(recorded_frame.inbox_row_id, manual_clock.now())

    assert [reply.text for reply in first_result.replies] == ["HT1 q ivan 1"]
    assert second_result.was_already_processed
    assert second_result.replies == ()
    assert InboundDirectMessage.objects.get().processed_at == manual_clock.now() - timedelta(seconds=1)


@pytest.mark.django_db
def test_the_worker_records_a_route_reset_it_made_for_a_flood_arrival(
    manual_clock: ManualClock, ivans_device: Contact
) -> None:
    recorded_frame = record_inbound_frame(
        build_frame(manual_clock, "HT1 Q ivan", ivans_device.public_key[:12], path_length=3), manual_clock.now()
    )

    record_flood_arrival_route_reset(recorded_frame.inbox_row_id)

    assert InboundDirectMessage.objects.get().route_reset_performed
