"""Rows and a small driver for the delivery engine's service tests.

RelayHarness drives the messaging services the way the worker does, on a manual clock: a DM
from a device is recorded and processed, and the sender loop prepares every due packet and
records that the node queued it.
"""

from dataclasses import dataclass, replace
from datetime import datetime

from django.contrib.auth.hashers import make_password
from pytest_django import Settings

from directory.models import Contact, User
from hoptalk_relay.relay_settings import PacingSettings, RelaySettings, RetryStrategy
from messaging.inbound_log import DIRECT_ARRIVAL_PATH_LENGTH, ReceivedDirectMessageFrame, record_inbound_frame
from messaging.models import OutboundPacket
from messaging.outbound_packets import NodeSendOutcome, PacketQueuedOnNode, record_send_outcome
from messaging.outbound_scheduling import PacketDescriptor, prepare_next_packet
from messaging.request_processing import InboundProcessingResult, process_inbound_direct_message
from tests.manual_clock import ManualClock

DEFAULT_RETRY_STRATEGY = RetryStrategy(
    maximum_attempts=6,
    initial_pause_seconds=30,
    backoff_multiplier=2.0,
    maximum_pause_seconds=600,
    delivered_receipt_delay_seconds=15,
)
DEFAULT_PACING = PacingSettings(
    maximum_packets_awaiting_node_acknowledgement=4,
    minimum_seconds_between_sends=2.0,
    maximum_active_deliveries_per_device=3,
)
TEST_PASSWORD = "correct horse battery"
SUGGESTED_TIMEOUT_MILLISECONDS = 4000
FIRST_SENDER_TIMESTAMP = 1_790_000_000
MAXIMUM_PACKETS_PER_PASS = 200


def configure_engine_settings(
    settings: Settings,
    *,
    retry_strategy: RetryStrategy = DEFAULT_RETRY_STRATEGY,
    pacing: PacingSettings = DEFAULT_PACING,
) -> None:
    """Pin the retry strategy and pacing, whatever src/.env holds."""
    relay_settings: RelaySettings = settings.RELAY_SETTINGS
    settings.RELAY_SETTINGS = replace(relay_settings, retry_strategy=retry_strategy, pacing=pacing)


def build_public_key(device_number: int) -> str:
    """A distinct key for every number, with a distinct six-byte prefix."""
    return f"{device_number:012x}" + "cd" * 26


def create_user(username: str, created_at: datetime, password: str = TEST_PASSWORD) -> User:
    return User.objects.create(username=username, password_hash=make_password(password), created_at=created_at)


def create_device(
    device_number: int,
    added_at: datetime,
    user: User | None = None,
    node_sync_state: Contact.NodeSyncState = Contact.NodeSyncState.ON_NODE,
) -> Contact:
    return Contact.objects.create(
        public_key=build_public_key(device_number),
        name=f"tracker {device_number}",
        source=Contact.Source.CARD,
        added_at=added_at,
        user=user,
        linked_at=added_at if user is not None else None,
        node_sync_state=node_sync_state,
    )


@dataclass(frozen=True, kw_only=True)
class SentPacket:
    packet_id: int
    contact_id: int
    text: str
    purpose: OutboundPacket.Purpose


class RelayHarness:
    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.next_sender_timestamp = FIRST_SENDER_TIMESTAMP
        self.connection_generation = 1

    def receive(
        self,
        device: Contact,
        text: str,
        *,
        path_length: int = DIRECT_ARRIVAL_PATH_LENGTH,
        text_type: int = 0,
        sender_timestamp: int | None = None,
    ) -> InboundProcessingResult | None:
        """Record and process one DM from the device; None when it was a firmware-level repeat."""
        if sender_timestamp is None:
            sender_timestamp = self.next_sender_timestamp
            self.next_sender_timestamp += 1
        frame = ReceivedDirectMessageFrame(
            sender_public_key_prefix=device.public_key[:12],
            sender_timestamp=sender_timestamp,
            text=text,
            text_type=text_type,
            path_length=path_length,
            signal_to_noise_ratio=6.5,
            received_at=self.clock.now(),
        )
        recorded_frame = record_inbound_frame(frame, self.clock.now())
        if recorded_frame.is_firmware_repeat:
            return None
        return process_inbound_direct_message(recorded_frame.inbox_row_id, self.clock.now(), original_text=text)

    def receive_replies(self, device: Contact, text: str) -> list[str]:
        processing_result = self.receive(device, text)
        assert processing_result is not None
        return [queued_reply.text for queued_reply in processing_result.replies]

    def prepare_next_packet(self) -> PacketDescriptor | None:
        return prepare_next_packet(self.clock.now(), self.connection_generation)

    def record_queued_on_node(self, packet_descriptor: PacketDescriptor) -> None:
        self.record_outcome(
            packet_descriptor,
            PacketQueuedOnNode(
                route=OutboundPacket.Route.DIRECT,
                expected_acknowledgement_code=f"{packet_descriptor.sender_timestamp & 0xFFFFFFFF:08x}",
                suggested_timeout_milliseconds=SUGGESTED_TIMEOUT_MILLISECONDS,
            ),
        )

    def prepare_next_packet_and_queue(self) -> PacketDescriptor:
        packet_descriptor = self.prepare_next_packet()
        assert packet_descriptor is not None
        self.record_queued_on_node(packet_descriptor)
        return packet_descriptor

    def record_outcome(self, packet_descriptor: PacketDescriptor, outcome: NodeSendOutcome) -> None:
        record_send_outcome(packet_descriptor.packet_id, outcome, self.clock.now())

    def send_due_packets(self) -> list[SentPacket]:
        """Everything the sender loop would send now, each packet queued by the node at once."""
        sent_packets: list[SentPacket] = []
        while len(sent_packets) < MAXIMUM_PACKETS_PER_PASS:
            packet_descriptor = self.prepare_next_packet()
            if packet_descriptor is None:
                return sent_packets
            self.record_queued_on_node(packet_descriptor)
            sent_packets.append(
                SentPacket(
                    packet_id=packet_descriptor.packet_id,
                    contact_id=packet_descriptor.contact_id,
                    text=packet_descriptor.text,
                    purpose=packet_descriptor.purpose,
                )
            )
        raise AssertionError("The sender never ran out of due packets.")

    def send_due_texts(self, device: Contact | None = None) -> list[str]:
        """The texts sent now, to one device or to all."""
        return [
            sent_packet.text
            for sent_packet in self.send_due_packets()
            if device is None or sent_packet.contact_id == device.pk
        ]
