from dataclasses import replace

import pytest
from pytest_django import Settings

from directory.models import Contact, User
from hoptalk_relay.relay_settings import RelaySettings, RetentionSettings
from messaging.maintenance import prune_traffic_log, run_messaging_maintenance
from messaging.models import InboundDirectMessage, Message, OutboundPacket, RefreshSession
from tests.manual_clock import ManualClock
from tests.services.messaging.engine_builders import RelayHarness, create_device, create_user


@pytest.fixture
def ivans_device(manual_clock: ManualClock) -> Contact:
    return create_device(1, manual_clock.now(), user=create_user("ivan", manual_clock.now()))


@pytest.fixture
def bob(manual_clock: ManualClock) -> User:
    return create_user("bob", manual_clock.now())


@pytest.mark.django_db
def test_the_traffic_log_is_pruned_after_its_retention_but_an_unprocessed_row_is_kept(
    relay: RelayHarness, manual_clock: ManualClock, ivans_device: Contact, bob: User
) -> None:
    create_device(2, manual_clock.now(), user=bob)
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/1 old")
    assert len(relay.send_due_texts()) == 1
    InboundDirectMessage.objects.create(
        received_at=manual_clock.now(),
        sender_public_key_prefix=ivans_device.public_key[:12],
        contact=ivans_device,
        sender_timestamp=1,
        text_type=0,
        path_length=255,
        text="HT1 Q bob",
        text_sha256="0" * 64,
    )
    manual_clock.advance(hours=30 * 24, seconds=1)
    relay.receive_replies(ivans_device, "HT1 Q bob")

    pruned_inbox_rows, pruned_packets = prune_traffic_log(manual_clock.now(), retention_days=30)

    assert (pruned_inbox_rows, pruned_packets) == (1, 1)
    remaining_states = sorted(InboundDirectMessage.objects.values_list("processing_state", flat=True))
    assert remaining_states == [
        InboundDirectMessage.ProcessingState.PROCESSED,
        InboundDirectMessage.ProcessingState.RECEIVED,
    ]
    assert not OutboundPacket.objects.exists()
    assert Message.objects.count() == 1


@pytest.mark.django_db
def test_maintenance_expires_incomplete_uploads_and_prunes_by_the_configured_retention(
    relay: RelayHarness, manual_clock: ManualClock, settings: Settings, ivans_device: Contact, bob: User
) -> None:
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(relay_settings, retention=RetentionSettings(log_retention_days=1))
    bobs_device = create_device(2, manual_clock.now(), user=bob)
    relay.receive_replies(ivans_device, "HT1 M bob 5 1/2 never finished")
    relay.receive_replies(ivans_device, "HT1 M bob 6 1/1 finished")
    relay.receive_replies(bobs_device, "HT1 F ivan")
    assert RefreshSession.objects.filter(requested_by_inbound__isnull=False).exists()
    manual_clock.advance(hours=24, seconds=1)

    maintenance_summary = run_messaging_maintenance(manual_clock.now())

    assert maintenance_summary.expired_incomplete_messages == 1
    assert maintenance_summary.pruned_inbox_rows == 3
    assert list(Message.objects.values_list("client_message_id", flat=True)) == [6]
    assert RefreshSession.objects.get().requested_by_inbound is None
