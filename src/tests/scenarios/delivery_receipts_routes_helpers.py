"""Shared steps and observations of the delivery, receipt, route and pacing scenarios.

- Starting the relay with devices the operator added from their cards, and signed-in clients.
- Losing chosen direct messages on the mesh, picked by their text, so a scenario can lose "the
  first read request" instead of a random share of the traffic.
- Routes both nodes already know, so a scenario starts from direct sends whatever the sign-in's
  flood exchanges happened to leave behind.
- What the relay stored and what its node was told, read after the fact: packets, deliveries,
  receipts, the node's command log, and how many packets overlapped in time.
"""

import asyncio
import itertools
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import pytest

import worker.acknowledgement_tracker as acknowledgement_tracker_module
from directory.models import Contact
from messaging.models import InboundDirectMessage, Message, MessageDelivery, OutboundPacket, ReceiptNotification
from messaging.outbound_packets import AcknowledgedPacket, record_node_acknowledgement
from protocol.usernames import normalize_username_for_lookup
from tests.scenarios.scenario_settings import SCENARIO_RETRY_STRATEGY, SCENARIO_WORKER_TIMING
from tests.scenarios.scenario_setup import ClientStarter, add_device_from_its_card, every_contact_is_on_node, sign_in
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware, ReceptionOutcome
from tests.worker.fake_node.frames import PUBLIC_KEY_BYTES, CommandCode, decode_unsigned_32
from tests.worker.fake_node.radio_packets import DirectMessagePacket, RadioPacket
from tests.worker.fake_node.simulated_mesh import DeliveryOutcome, LinkPolicy, SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import WAIT_POLL_INTERVAL_SECONDS, wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    in_database,
    wait_for_database,
)
from tests.worker.simulated_hoptalk_client import SimulatedHopTalkClient
from worker.node_gateway import NextMessageOutcome, NodeGateway
from worker.worker_state import RelayMode

CERTAIN_LOSS_PROBABILITY = 1.0
# The relay's node and the devices keep the default path hash mode: one byte per repeater.
PATH_HASH_SIZE = 1
# CMD_SEND_TXT_MSG: code, text type, attempt, four timestamp bytes, the recipient's six-byte key prefix, the text.
SEND_TEXT_COMMAND_TIMESTAMP_OFFSET = 3
SEND_TEXT_COMMAND_TEXT_OFFSET = 13
# A device the worker gives up on is tried for about fifteen seconds at the scenario speed.
GIVE_UP_TIMEOUT_SECONDS = 30.0
# How late a retry round may start after its pause: the worker's sender sleeps in short steps.
ROUND_START_TOLERANCE = timedelta(seconds=1)
# How soon the worker acts on work that has just become due, ten seconds at production speed: it processes a
# direct message in milliseconds, and its sender, woken by every change, sleeps at least one production second.
WORKER_REACTION_ALLOWANCE = timedelta(seconds=0.1)


# ----- starting the relay and its clients ------------------------------------------------------


async def start_relay_with_devices(
    relay_worker: RelayWorkerHarness,
    relay_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    *device_names: str,
) -> dict[str, SimulatedDevice]:
    """The configured relay node, a device per name added from its card, and the worker running with all on the node."""
    await configure_relay_node(relay_firmware)
    devices = {device_name: await add_device_from_its_card(simulated_mesh, device_name) for device_name in device_names}
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(every_contact_is_on_node, description="every device to be put on the relay's node")
    return devices


async def start_signed_in_client(
    start_client: ClientStarter, device: SimulatedDevice, username: str
) -> SimulatedHopTalkClient:
    client = start_client(device)
    await sign_in(client, username)
    return client


def is_any_inbox_row_or_packet_unsettled() -> bool:
    """A frame waits to be processed, or a packet is being sent or awaits its firmware ACK."""
    is_any_inbox_row_waiting = InboundDirectMessage.objects.filter(
        processing_state=InboundDirectMessage.ProcessingState.RECEIVED
    ).exists()
    is_any_packet_unsettled = OutboundPacket.objects.filter(
        state__in=[OutboundPacket.State.PREPARED, OutboundPacket.State.QUEUED_ON_NODE]
    ).exists()
    return is_any_inbox_row_waiting or is_any_packet_unsettled


async def wait_until_relay_is_quiet(
    relay_worker: RelayWorkerHarness,
    simulated_mesh: SimulatedMesh,
    clients: Iterable[SimulatedHopTalkClient],
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Nothing on the air, nothing a client still has to send, nothing the worker still has to answer, send or await."""
    client_list = list(clients)
    worker = relay_worker.worker

    def is_quiet_in_memory() -> bool:
        return (
            simulated_mesh.is_idle
            and all(client.is_idle for client in client_list)
            and len(worker.reply_queue) == 0
            and worker.acknowledgement_tracker.count_packets_awaiting_acknowledgement() == 0
            and worker.inbound_frame_queue.empty()
            and not worker.worker_state.sending_gate.send_step_lock.locked()
        )

    deadline = time.monotonic() + timeout_seconds
    while not (is_quiet_in_memory() and not await in_database(is_any_inbox_row_or_packet_unsettled)):
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out after {timeout_seconds} s waiting for the relay to fall quiet")
        await asyncio.sleep(WAIT_POLL_INTERVAL_SECONDS)


async def wait_until_worker_clock_passes(relay_worker: RelayWorkerHarness, moment: datetime) -> None:
    """For a check that something did not happen by a moment the worker's own schedule sets."""
    await wait_until(
        lambda: relay_worker.clock.now() > moment,
        timeout_seconds=max((moment - relay_worker.clock.now()).total_seconds(), 0.0) + 5.0,
        description=f"the worker's clock to pass {moment}",
    )


# ----- routes ------------------------------------------------------------------------------


def give_direct_routes_between_relay_and(relay_firmware: FakeCompanionFirmware, device: SimulatedDevice) -> None:
    """Both nodes store the current route to each other, as a completed flood exchange leaves them."""
    relay_side_record = relay_firmware.find_contact(device.public_key)
    device_side_record = device.stored_relay_contact
    assert relay_side_record is not None, f"the relay's node does not know {device.name}"
    assert device_side_record is not None, f"{device.name} does not know the relay"
    path_toward_device = b"".join(hop[:PATH_HASH_SIZE] for hop in reversed(device.route_to_relay))
    path_toward_relay = b"".join(hop[:PATH_HASH_SIZE] for hop in device.route_to_relay)
    relay_firmware.add_or_update_contact(
        relay_side_record.with_route(
            route_path=path_toward_device, path_hash_size=PATH_HASH_SIZE, last_modified=relay_firmware.clock_time()
        )
    )
    device.firmware.add_or_update_contact(
        device_side_record.with_route(
            route_path=path_toward_relay, path_hash_size=PATH_HASH_SIZE, last_modified=device.firmware.clock_time()
        )
    )


def relay_knows_route_to(relay_firmware: FakeCompanionFirmware, device: SimulatedDevice) -> bool:
    relay_side_record = relay_firmware.find_contact(device.public_key)
    return relay_side_record is not None and relay_side_record.has_known_route


# ----- losing chosen direct messages ---------------------------------------------------------


@dataclass(kw_only=True)
class DirectMessageLossPolicy(LinkPolicy):
    """Loses, once for each prefix, the first direct message whose text starts with that prefix.

    Everything else on the link follows the ordinary LinkPolicy fields.
    """

    text_prefixes_to_lose: list[str] = field(default_factory=list)
    lost_texts: list[str] = field(default_factory=list)

    def loss_probability_for(self, packet: RadioPacket) -> float:
        if isinstance(packet, DirectMessagePacket):
            text = packet.text.decode("utf-8", "replace")
            for text_prefix in self.text_prefixes_to_lose:
                if text.startswith(text_prefix):
                    self.text_prefixes_to_lose.remove(text_prefix)
                    self.lost_texts.append(text)
                    return CERTAIN_LOSS_PROBABILITY
        return super().loss_probability_for(packet)


# ----- what the relay stored ------------------------------------------------------------------


def read_contact(public_key: bytes) -> Contact:
    return Contact.objects.get(public_key=public_key.hex())


def read_contact_id(public_key: bytes) -> int:
    return read_contact(public_key).pk


def read_message(sender_username: str, client_message_id: int) -> Message:
    return Message.objects.get(
        sender__username_lookup=normalize_username_for_lookup(sender_username), client_message_id=client_message_id
    )


def read_delivery(message_id: int, device_id: int) -> MessageDelivery:
    return MessageDelivery.objects.get(message_id=message_id, device_id=device_id)


def find_delivery(message_id: int, device_id: int) -> MessageDelivery | None:
    return MessageDelivery.objects.filter(message_id=message_id, device_id=device_id).first()


def has_delivery_completed_round(message_id: int, device_id: int, attempt_count: int) -> bool:
    """The delivery started this many rounds and has sent every part of the last one."""
    delivery = find_delivery(message_id, device_id)
    return delivery is not None and delivery.attempt_count == attempt_count and delivery.round_pending_parts_mask == 0


def read_receipt(message_id: int, device_id: int) -> ReceiptNotification:
    return ReceiptNotification.objects.get(message_id=message_id, device_id=device_id)


def find_receipt(message_id: int, device_id: int) -> ReceiptNotification | None:
    return ReceiptNotification.objects.filter(message_id=message_id, device_id=device_id).first()


def read_packets(**filters: object) -> list[OutboundPacket]:
    return list(OutboundPacket.objects.filter(**filters).order_by("id"))


def read_delivery_packets(delivery_id: int) -> list[OutboundPacket]:
    return read_packets(message_delivery_id=delivery_id)


def read_receipt_packets(receipt_id: int) -> list[OutboundPacket]:
    return read_packets(receipt_notification_id=receipt_id)


def read_highest_packet_id() -> int:
    highest_packet = OutboundPacket.objects.order_by("-id").first()
    return 0 if highest_packet is None else highest_packet.pk


def read_inbox_rows_from(device_id: int, text_prefix: str = "") -> list[InboundDirectMessage]:
    return list(InboundDirectMessage.objects.filter(contact_id=device_id, text__startswith=text_prefix).order_by("id"))


# ----- the retry strategy and the pacing ------------------------------------------------------


def calculate_expected_retry_pause(attempt_number: int) -> timedelta:
    """The pause after an attempt: the initial pause, multiplied for every further attempt, at most the maximum."""
    pause_seconds = SCENARIO_RETRY_STRATEGY.initial_pause_seconds * (
        SCENARIO_RETRY_STRATEGY.backoff_multiplier ** (attempt_number - 1)
    )
    return timedelta(seconds=min(pause_seconds, SCENARIO_RETRY_STRATEGY.maximum_pause_seconds))


def calculate_expected_packet_pool_back_off(consecutive_full_packet_pools: int) -> timedelta:
    back_off_seconds = SCENARIO_WORKER_TIMING.table_full_backoff_initial_seconds * (
        2 ** (consecutive_full_packet_pools - 1)
    )
    return timedelta(seconds=min(back_off_seconds, SCENARIO_WORKER_TIMING.table_full_backoff_maximum_seconds))


def describe_round_spacing(packets: list[OutboundPacket]) -> list[str]:
    """For one packet per round: every round that started before its pause, or long after it."""
    problems: list[str] = []
    for previous_packet, next_packet in itertools.pairwise(packets):
        assert previous_packet.attempt_number is not None
        assert previous_packet.queued_at is not None
        assert next_packet.prepared_at is not None
        earliest_start = previous_packet.queued_at + calculate_expected_retry_pause(previous_packet.attempt_number)
        if not earliest_start <= next_packet.prepared_at <= earliest_start + ROUND_START_TOLERANCE:
            problems.append(
                f"attempt {next_packet.attempt_number} started {next_packet.prepared_at - previous_packet.queued_at} "
                f"after attempt {previous_packet.attempt_number}, "
                f"whose pause is {calculate_expected_retry_pause(previous_packet.attempt_number)}"
            )
    return problems


def calculate_acknowledgement_wait_end(packet: OutboundPacket) -> datetime:
    """When a packet stopped holding one of the places for packets awaiting a firmware ACK."""
    assert packet.acknowledgement_deadline_at is not None
    if packet.acknowledged_at is not None and packet.acknowledged_at < packet.acknowledgement_deadline_at:
        return packet.acknowledged_at
    return packet.acknowledgement_deadline_at


def count_packets_awaiting_acknowledgement_at(packets: Iterable[OutboundPacket], moment: datetime) -> int:
    """How many of the packets held a place for packets awaiting a firmware ACK at that moment."""
    return sum(
        1
        for packet in packets
        if packet.queued_at is not None and packet.queued_at <= moment < calculate_acknowledgement_wait_end(packet)
    )


def find_largest_overlap(time_spans: Iterable[tuple[datetime, datetime]]) -> int:
    """The most spans that were open at one moment; a span that ends when another starts does not overlap it."""
    # Sorted by time, and at the same moment an end (0) before a start (1).
    span_end_marker = 0
    span_start_marker = 1
    span_list = list(time_spans)
    boundaries = sorted(
        [(span_start, span_start_marker) for span_start, _ in span_list]
        + [(span_end, span_end_marker) for _, span_end in span_list]
    )
    open_span_count = 0
    largest_overlap = 0
    for _, boundary_marker in boundaries:
        open_span_count += 1 if boundary_marker == span_start_marker else -1
        largest_overlap = max(largest_overlap, open_span_count)
    return largest_overlap


# ----- what the relay's node was told -----------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class SentTextCommand:
    # The fake node's monotonic time when the command frame arrived.
    received_at: float
    sender_timestamp: int
    text: str


def list_sent_text_commands(relay_firmware: FakeCompanionFirmware) -> list[SentTextCommand]:
    return [
        SentTextCommand(
            received_at=received_command.received_at,
            sender_timestamp=decode_unsigned_32(received_command.frame, SEND_TEXT_COMMAND_TIMESTAMP_OFFSET),
            text=received_command.frame[SEND_TEXT_COMMAND_TEXT_OFFSET:].decode("utf-8", "replace"),
        )
        for received_command in relay_firmware.command_log
        if received_command.code == CommandCode.SEND_TEXT_MESSAGE
    ]


def list_route_reset_times(relay_firmware: FakeCompanionFirmware, device: SimulatedDevice) -> list[float]:
    """When the relay's node was told to forget its route to the device (the fake node's monotonic time)."""
    return [
        received_command.received_at
        for received_command in relay_firmware.command_log
        if received_command.code == CommandCode.RESET_PATH
        and received_command.frame[1 : 1 + PUBLIC_KEY_BYTES] == device.public_key
    ]


def count_route_resets(relay_firmware: FakeCompanionFirmware) -> int:
    return sum(1 for received_command in relay_firmware.command_log if received_command.code == CommandCode.RESET_PATH)


def find_sent_text_command(relay_firmware: FakeCompanionFirmware, packet: OutboundPacket) -> SentTextCommand:
    matching_commands = [
        sent_text_command
        for sent_text_command in list_sent_text_commands(relay_firmware)
        if sent_text_command.sender_timestamp == packet.sender_timestamp
    ]
    assert len(matching_commands) == 1, f"the node was told to send packet {packet.pk} {len(matching_commands)} times"
    return matching_commands[0]


# ----- watching the worker ---------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class AcknowledgementLookup:
    code: str
    matched_packet_ids: tuple[int, ...]


def record_acknowledgement_lookups(monkeypatch: pytest.MonkeyPatch) -> list[AcknowledgementLookup]:
    """Every firmware ACK code the worker looked up in the database, in order, with the packets it matched.

    A lookup that matched nothing is an ACK the worker kept aside for a packet whose MSG_SENT it had not recorded yet.
    """
    acknowledgement_lookups: list[AcknowledgementLookup] = []

    def look_up_and_record_acknowledgement(
        acknowledgement_code: str, round_trip_milliseconds: int | None, now: datetime
    ) -> tuple[AcknowledgedPacket, ...]:
        acknowledged_packets = record_node_acknowledgement(acknowledgement_code, round_trip_milliseconds, now)
        acknowledgement_lookups.append(
            AcknowledgementLookup(
                code=acknowledgement_code,
                matched_packet_ids=tuple(acknowledged_packet.packet_id for acknowledged_packet in acknowledged_packets),
            )
        )
        return acknowledged_packets

    monkeypatch.setattr(
        acknowledgement_tracker_module, "record_node_acknowledgement", look_up_and_record_acknowledgement
    )
    return acknowledgement_lookups


class DrainOutcomeRecorder:
    """What each request for the node's next waiting message brought, in order."""

    def __init__(self, gateway: NodeGateway) -> None:
        self.outcomes: list[NextMessageOutcome] = []
        self._get_next_message = gateway.get_next_message

    async def get_next_message(self) -> NextMessageOutcome:
        next_message_outcome = await self._get_next_message()
        self.outcomes.append(next_message_outcome)
        return next_message_outcome

    def list_drain_passes(self) -> list[list[NextMessageOutcome]]:
        """The outcomes split into passes, each ending when the node had nothing left."""
        drain_passes: list[list[NextMessageOutcome]] = [[]]
        for next_message_outcome in self.outcomes:
            if next_message_outcome == NextMessageOutcome.NO_MORE_MESSAGES:
                drain_passes.append([])
            else:
                drain_passes[-1].append(next_message_outcome)
        return [drain_pass for drain_pass in drain_passes if drain_pass]


def record_drain_outcomes(monkeypatch: pytest.MonkeyPatch, relay_worker: RelayWorkerHarness) -> DrainOutcomeRecorder:
    gateway = relay_worker.worker.gateway
    drain_outcome_recorder = DrainOutcomeRecorder(gateway)
    monkeypatch.setattr(gateway, "get_next_message", drain_outcome_recorder.get_next_message)
    return drain_outcome_recorder


# ----- strangers on the Public channel ----------------------------------------------------------


def count_direct_messages_taken_by_relay_node(simulated_mesh: SimulatedMesh, device: SimulatedDevice) -> int:
    return sum(
        1
        for traffic_record in simulated_mesh.traffic(
            sender=device.name, recipient=simulated_mesh.relay_firmware.label, packet_type=DirectMessagePacket
        )
        if traffic_record.outcome == DeliveryOutcome.DELIVERED and traffic_record.reception == ReceptionOutcome.ACCEPTED
    )


async def inject_channel_traffic_after_each_direct_message(
    simulated_mesh: SimulatedMesh,
    device: SimulatedDevice,
    *,
    direct_message_count: int,
    datagrams_per_direct_message: int,
    timeout_seconds: float = 5.0,
) -> list[ReceptionOutcome]:
    """Strangers' traffic on the Public channel right after each direct message of the device reaches the relay's node.

    Each direct message is followed by a few datagrams and one text message, so the node's queue alternates between
    direct messages and channel traffic.
    """
    receptions: list[ReceptionOutcome] = []
    answered_direct_message_count = 0
    async with asyncio.timeout(timeout_seconds):
        while answered_direct_message_count < direct_message_count:
            arrived_direct_message_count = count_direct_messages_taken_by_relay_node(simulated_mesh, device)
            while answered_direct_message_count < min(arrived_direct_message_count, direct_message_count):
                answered_direct_message_count += 1
                for datagram_number in range(datagrams_per_direct_message):
                    receptions.append(
                        simulated_mesh.inject_channel_datagram(
                            data=bytes([answered_direct_message_count, datagram_number]) * 6
                        )
                    )
                receptions.append(
                    simulated_mesh.inject_channel_message(text=f"Public chatter {answered_direct_message_count}")
                )
            await asyncio.sleep(WAIT_POLL_INTERVAL_SECONDS)
    return receptions
