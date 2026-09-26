"""A simulated LoRa mesh between the relay's fake node and simulated user devices.

Every device is a `FakeCompanionFirmware` of its own (identity, contacts with a stored route to the
relay, 8-entry ACK table, 16-packet pool, offline queue) driven by a small app-facing API that a
simulated HopTalk client uses instead of a phone app. Devices talk only to the relay.

Routing follows the firmware: a flood always finds the way and arrives carrying the repeater
hashes it passed; a direct send arrives only when the sender's stored path equals the current
route, so after `change_route_to_relay` both sides' stored routes are stale and direct sends are
lost until a route reset makes the next send flood. Path learning (PATH returns carrying the ACK,
reciprocal paths, PATH_UPDATE pushes) happens inside the firmware.

Each device has two `LinkPolicy` objects, `uplink` (device to relay) and `downlink` (relay to
device), applied per packet with the mesh's seeded random generator: loss (separately for packets
that carry a firmware ACK), duplication, delay and reordering. They can be replaced at any time.
Duplicated copies are byte-identical, so the receiving node drops them by packet hash unless the
policy says the copy comes after the node forgot the hash.

Device conditions: `switch_off()`/`switch_on()` (a switched-off node hears nothing and loses its RAM),
`phone_leaves()`/`phone_returns()` (the node keeps receiving, acknowledging and queueing, but the app
can neither read nor send), `change_route_to_relay(...)`.
"""

import asyncio
import dataclasses
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import (
    FakeCompanionFirmware,
    ReceptionOutcome,
    SendTextMessageResult,
)
from tests.worker.fake_node.firmware_state import FirmwareTiming
from tests.worker.fake_node.frames import (
    ACKNOWLEDGEMENT_CODE_BYTES,
    DIRECT_ARRIVAL_PATH_LENGTH,
    DIRECT_MESSAGE_FRAME_CODES,
    PUBLIC_CHANNEL_SECRET,
    PUBLIC_KEY_BYTES,
    PUBLIC_KEY_PREFIX_BYTES,
    PushCode,
    ResponseCode,
    TextType,
    decode_unsigned_32,
)
from tests.worker.fake_node.node_identity import contact_card_uri
from tests.worker.fake_node.radio_packets import (
    ChannelDataPacket,
    ChannelMessagePacket,
    DirectMessagePacket,
    PacketArrival,
    PacketRoute,
    RadioPacket,
)
from tests.worker.fake_node.waiting import wait_until

REPEATER_IDENTIFIER_BYTES = 3
# The app a simulated device runs asks its node for V3 message frames, as the stock app does.
DEVICE_APP_PROTOCOL_VERSION = 3
POLL_INTERVAL_SECONDS = 0.002
SIGNAL_TO_NOISE_QUARTERS_PER_DECIBEL = 4
STRANGER_LABEL = "stranger"
UNKNOWN_RECIPIENT_LABEL = "unknown"


@dataclass(kw_only=True)
class LinkPolicy:
    """What happens to packets in one direction between one device and the relay."""

    loss_probability: float = 0.0
    # For packets carrying a firmware ACK (ACKs and returned paths with an ACK); None: loss_probability.
    acknowledgement_loss_probability: float | None = None
    duplicate_probability: float = 0.0
    # The duplicate arrives after the receiving node's 160-entry packet-hash ring forgot the
    # original (heavy traffic or a reboot in between), so the node takes it again.
    duplicates_bypass_deduplication: bool = False
    minimum_delay_seconds: float = 0.001
    maximum_delay_seconds: float = 0.003
    reorder_probability: float = 0.0
    # Added to a packet picked for reordering, so that packets sent after it overtake it.
    reorder_delay_seconds: float = 0.05

    def loss_probability_for(self, packet: RadioPacket) -> float:
        if packet.carries_acknowledgement and self.acknowledgement_loss_probability is not None:
            return self.acknowledgement_loss_probability
        return self.loss_probability


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    LOST_BY_POLICY = "lost_by_policy"
    LOST_ON_STALE_ROUTE = "lost_on_stale_route"
    LOST_OUT_OF_RANGE = "lost_out_of_range"
    LOST_NO_SUCH_NODE = "lost_no_such_node"


@dataclass(frozen=True, kw_only=True)
class TrafficRecord:
    transmitted_at: float
    # When the packet reached the recipient, or when it was lost.
    recorded_at: float
    sender: str
    recipient: str
    packet: RadioPacket
    outcome: DeliveryOutcome
    # What the recipient's firmware did with a delivered packet.
    reception: ReceptionOutcome | None = None
    is_duplicate: bool = False


@dataclass(frozen=True, kw_only=True)
class ReceivedDirectMessage:
    sender_public_key_prefix: bytes
    sender_timestamp: int
    text_type: int
    text_bytes: bytes
    # The byte the node reported: the encoded flood path length, or 0xFF for a direct arrival.
    path_length: int
    signal_to_noise_ratio: float | None

    @property
    def text(self) -> str:
        return self.text_bytes.decode("utf-8", "replace")

    @property
    def arrived_by_flood(self) -> bool:
        return self.path_length != DIRECT_ARRIVAL_PATH_LENGTH


@dataclass(frozen=True, kw_only=True)
class FirmwareAcknowledgement:
    code: bytes
    round_trip_milliseconds: int
    received_at: float


class DeviceUnreachableError(RuntimeError):
    """The app cannot talk to its node: the phone is away or the node is switched off."""


def parse_received_direct_message(frame: bytes) -> ReceivedDirectMessage | None:
    """Decode a queued RESP_CODE_CONTACT_MSG_RECV(_V3) frame; None for any other frame."""
    if not frame or frame[0] not in DIRECT_MESSAGE_FRAME_CODES:
        return None
    signal_to_noise_ratio: float | None = None
    offset = 1
    if frame[0] == ResponseCode.CONTACT_MESSAGE_RECEIVED_VERSION_3:
        signal_to_noise_ratio = int.from_bytes(frame[1:2], "little", signed=True) / SIGNAL_TO_NOISE_QUARTERS_PER_DECIBEL
        offset = 4
    sender_public_key_prefix = frame[offset : offset + PUBLIC_KEY_PREFIX_BYTES]
    offset += PUBLIC_KEY_PREFIX_BYTES
    path_length, text_type = frame[offset], frame[offset + 1]
    sender_timestamp = decode_unsigned_32(frame, offset + 2)
    offset += 6
    if text_type == TextType.SIGNED_PLAIN:
        offset += 4
    return ReceivedDirectMessage(
        sender_public_key_prefix=sender_public_key_prefix,
        sender_timestamp=sender_timestamp,
        text_type=text_type,
        text_bytes=frame[offset:],
        path_length=path_length,
        signal_to_noise_ratio=signal_to_noise_ratio,
    )


class DeviceAppLink:
    """The device's node writes its pushes here while the phone is connected."""

    def __init__(self, device: SimulatedDevice) -> None:
        self._device = device

    def deliver_frame_to_host(self, frame: bytes) -> None:
        self._device.receive_push_from_node(frame)

    def node_dropped_link(self, reason: str) -> None:
        """The node rebooted or went off; the app reconnects when it comes back (`switch_on`)."""


class SimulatedDevice:
    def __init__(
        self,
        *,
        name: str,
        mesh: SimulatedMesh,
        firmware: FakeCompanionFirmware,
        route_to_relay: tuple[bytes, ...],
        relay_public_key: bytes,
        uplink: LinkPolicy,
        downlink: LinkPolicy,
    ) -> None:
        self.name = name
        self.firmware = firmware
        self.relay_public_key = relay_public_key
        self.uplink = uplink
        self.downlink = downlink
        self._mesh = mesh
        self._route_to_relay = route_to_relay
        self._phone_is_connected = True
        self._app_link = DeviceAppLink(self)
        self._last_sender_timestamp = 0
        self.firmware_acknowledgements: list[FirmwareAcknowledgement] = []
        self.path_update_times: list[float] = []
        self.messages_waiting_notifications = 0

    def __repr__(self) -> str:
        return f"SimulatedDevice(name={self.name!r}, public_key={self.public_key.hex()[:12]}…)"

    # ----- identity ----------------------------------------------------------------------------

    @property
    def public_key(self) -> bytes:
        return self.firmware.public_key

    @property
    def public_key_prefix(self) -> bytes:
        return self.public_key[:PUBLIC_KEY_PREFIX_BYTES]

    def contact_card_uri(self) -> str:
        """The device's signed card, as its app would share it for the operator to add."""
        return contact_card_uri(self.firmware.export_self_card())

    def contact_record(self) -> ContactRecord:
        """The record the relay's node stores for this device when it is added from its card."""
        return ContactRecord.create(
            public_key=self.public_key,
            name=self.firmware.preferences.node_name,
            last_advert_timestamp=self.firmware.clock_time(),
        )

    def trust_relay(self, relay_public_key: bytes, *, relay_name: bytes = b"relay") -> None:
        """Pin a new relay key (a new card after the relay was reconfigured) and store it as a contact."""
        self.firmware.add_or_update_contact(ContactRecord.create(public_key=relay_public_key, name=relay_name))
        self.relay_public_key = relay_public_key

    # ----- the app-facing API ------------------------------------------------------------------

    @property
    def app_can_reach_node(self) -> bool:
        return self._phone_is_connected and self.firmware.is_running

    def _require_app_can_reach_node(self) -> None:
        if not self.app_can_reach_node:
            raise DeviceUnreachableError(f"The app of {self.name} cannot reach its node")

    def send_direct_message(
        self, text: str | bytes, *, sender_timestamp: int | None = None, attempt: int = 0
    ) -> SendTextMessageResult:
        """Hand a DM to the relay to the device's node, which answers as to CMD_SEND_TXT_MSG.

        Without a timestamp the next one is max(now, previous + 1); give the previous timestamp
        and a higher attempt to make the node's own firmware-level repeat, as the stock app does.
        """
        self._require_app_can_reach_node()
        if sender_timestamp is None:
            sender_timestamp = max(int(time.time()), self._last_sender_timestamp + 1)
        self._last_sender_timestamp = max(self._last_sender_timestamp, sender_timestamp)
        text_bytes = text.encode() if isinstance(text, str) else text
        return self.firmware.send_text_message(
            text_type=TextType.PLAIN,
            attempt=attempt,
            sender_timestamp=sender_timestamp,
            recipient_public_key_prefix=self.relay_public_key[:PUBLIC_KEY_PREFIX_BYTES],
            text=text_bytes,
        )

    def receive_direct_messages(self) -> list[ReceivedDirectMessage]:
        """Drain the node's offline queue; channel traffic in it is read and skipped."""
        self._require_app_can_reach_node()
        received_messages: list[ReceivedDirectMessage] = []
        while (queued_frame := self.firmware.pop_offline_frame()) is not None:
            received_message = parse_received_direct_message(queued_frame)
            if received_message is not None:
                received_messages.append(received_message)
        return received_messages

    def reset_route_to_relay(self) -> bool:
        """CMD_RESET_PATH for the relay contact: the next DM to the relay floods."""
        self._require_app_can_reach_node()
        return self.firmware.reset_route(self.relay_public_key)

    def send_advert(self, *, flood: bool = False) -> bool:
        self._require_app_can_reach_node()
        return self.firmware.send_self_advert(flood=flood)

    @property
    def stored_relay_contact(self) -> ContactRecord | None:
        """The relay contact on the node, with the route the node would use (`has_known_route`)."""
        return self.firmware.find_contact(self.relay_public_key)

    @property
    def waiting_frame_count(self) -> int:
        return self.firmware.offline_queue_length

    @property
    def last_path_update_at(self) -> float | None:
        """When the node last reported a new route to the relay (monotonic time)."""
        return self.path_update_times[-1] if self.path_update_times else None

    def acknowledgement_for(self, expected_acknowledgement: bytes) -> FirmwareAcknowledgement | None:
        for acknowledgement in self.firmware_acknowledgements:
            if acknowledgement.code == expected_acknowledgement:
                return acknowledgement
        return None

    async def wait_for_acknowledgement(
        self, expected_acknowledgement: bytes, *, timeout_seconds: float
    ) -> FirmwareAcknowledgement | None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            acknowledgement = self.acknowledgement_for(expected_acknowledgement)
            if acknowledgement is not None:
                return acknowledgement
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        return self.acknowledgement_for(expected_acknowledgement)

    def receive_push_from_node(self, frame: bytes) -> None:
        push_code = frame[0]
        if push_code == PushCode.SEND_CONFIRMED:
            self.firmware_acknowledgements.append(
                FirmwareAcknowledgement(
                    code=frame[1 : 1 + ACKNOWLEDGEMENT_CODE_BYTES],
                    round_trip_milliseconds=decode_unsigned_32(frame, 1 + ACKNOWLEDGEMENT_CODE_BYTES),
                    received_at=time.monotonic(),
                )
            )
        elif push_code == PushCode.PATH_UPDATED and frame[1 : 1 + PUBLIC_KEY_BYTES] == self.relay_public_key:
            self.path_update_times.append(time.monotonic())
        elif push_code == PushCode.MESSAGES_WAITING:
            self.messages_waiting_notifications += 1

    # ----- conditions --------------------------------------------------------------------------

    @property
    def route_to_relay(self) -> tuple[bytes, ...]:
        """Repeater identifiers from the device toward the relay; empty means direct neighbours."""
        return self._route_to_relay

    def change_route_to_relay(self, repeaters: int | Sequence[bytes]) -> None:
        """Move the device: routes both nodes stored before are stale from now on."""
        self._route_to_relay = self._mesh.build_repeater_route(repeaters)

    @property
    def is_switched_on(self) -> bool:
        return self.firmware.is_running

    @property
    def phone_is_connected(self) -> bool:
        return self._phone_is_connected

    def switch_off(self) -> None:
        self.firmware.power_off()

    def switch_on(self) -> None:
        self.firmware.power_on()
        if self._phone_is_connected:
            self.connect_app_to_node()

    def phone_leaves(self) -> None:
        self._phone_is_connected = False
        self.firmware.detach_host_link(self._app_link)

    def phone_returns(self) -> None:
        self._phone_is_connected = True
        if self.firmware.is_running:
            self.connect_app_to_node()

    def connect_app_to_node(self) -> None:
        """What the app does on connecting: attach, ask for V3 frames, set the node's clock."""
        self.firmware.attach_host_link(self._app_link)
        self.firmware.record_app_protocol_version(DEVICE_APP_PROTOCOL_VERSION)
        self.firmware.set_clock_time(int(time.time()))


@dataclass(frozen=True, kw_only=True)
class PendingDelivery:
    sender: FakeCompanionFirmware
    recipient: FakeCompanionFirmware
    packet: RadioPacket
    arrival: PacketArrival
    transmitted_at: float
    is_duplicate: bool = False
    bypasses_deduplication: bool = False


class SimulatedMesh:
    """The radio between the relay's node and the devices; see the module docstring."""

    def __init__(
        self,
        relay_firmware: FakeCompanionFirmware,
        *,
        seed: int = 0,
        device_firmware_timing: FirmwareTiming | None = None,
    ) -> None:
        self.seed = seed
        self.relay_firmware = relay_firmware
        self.devices: dict[str, SimulatedDevice] = {}
        self.traffic_log: list[TrafficRecord] = []
        self._random = random.Random(seed)
        self._device_firmware_timing = device_firmware_timing or relay_firmware.timing
        self._delivery_timers: set[asyncio.TimerHandle] = set()
        self._delivery_errors: list[Exception] = []
        relay_firmware.attach_radio(self)

    def __repr__(self) -> str:
        return f"SimulatedMesh(seed={self.seed}, devices={list(self.devices)})"

    def add_device(
        self,
        name: str,
        *,
        repeaters_to_relay: int | Sequence[bytes] = 0,
        relay_knows_device: bool = True,
        uplink: LinkPolicy | None = None,
        downlink: LinkPolicy | None = None,
    ) -> SimulatedDevice:
        """A switched-on device whose phone is connected and whose node has the relay as a contact.

        With `relay_knows_device` the relay's node gets the device's record at once; otherwise
        the device reaches the relay only after the worker added it (from `contact_card_uri()`).
        """
        firmware = FakeCompanionFirmware(
            label=name,
            node_name=name,
            random_generator=random.Random(self._random.getrandbits(64)),
            timing=self._device_firmware_timing,
        )
        firmware.attach_radio(self)
        firmware.start()
        firmware.force_clock_time(int(time.time()))
        device = SimulatedDevice(
            name=name,
            mesh=self,
            firmware=firmware,
            route_to_relay=self.build_repeater_route(repeaters_to_relay),
            relay_public_key=self.relay_firmware.public_key,
            uplink=uplink or LinkPolicy(),
            downlink=downlink or LinkPolicy(),
        )
        device.trust_relay(self.relay_firmware.public_key, relay_name=self.relay_firmware.preferences.node_name)
        device.connect_app_to_node()
        if relay_knows_device:
            # Stamped with the relay's clock as CMD_ADD_UPDATE_CONTACT stamps it, so listings include it.
            self.relay_firmware.add_or_update_contact(
                dataclasses.replace(device.contact_record(), last_modified=self.relay_firmware.clock_time())
            )
        self.devices[name] = device
        return device

    def device(self, name: str) -> SimulatedDevice:
        return self.devices[name]

    def build_repeater_route(self, repeaters: int | Sequence[bytes]) -> tuple[bytes, ...]:
        if isinstance(repeaters, int):
            return tuple(self._random.randbytes(REPEATER_IDENTIFIER_BYTES) for _ in range(repeaters))
        return tuple(bytes(repeater) for repeater in repeaters)

    def internal_errors(self) -> list[Exception]:
        """Exceptions raised inside the mesh or the devices' nodes, which would otherwise pass unnoticed."""
        internal_errors = list(self._delivery_errors)
        for device in self.devices.values():
            internal_errors.extend(device.firmware.internal_errors)
        return internal_errors

    async def stop(self) -> None:
        for timer in self._delivery_timers:
            timer.cancel()
        self._delivery_timers.clear()
        for device in self.devices.values():
            await device.firmware.stop()
        self.relay_firmware.attach_radio(None)

    # ----- observation -------------------------------------------------------------------------

    @property
    def is_idle(self) -> bool:
        """No packet in flight and no node with a packet waiting to be sent."""
        nodes = [self.relay_firmware, *(device.firmware for device in self.devices.values())]
        return not self._delivery_timers and not any(node.has_pending_transmissions for node in nodes)

    async def wait_until_idle(self, *, timeout_seconds: float = 5.0) -> None:
        await wait_until(lambda: self.is_idle, timeout_seconds=timeout_seconds, description="the mesh to fall idle")

    def traffic(
        self,
        *,
        sender: str | None = None,
        recipient: str | None = None,
        packet_type: type[RadioPacket] | None = None,
    ) -> list[TrafficRecord]:
        def matches(record: TrafficRecord) -> bool:
            return (
                (sender is None or record.sender == sender)
                and (recipient is None or record.recipient == recipient)
                and (packet_type is None or isinstance(record.packet, packet_type))
            )

        return [record for record in self.traffic_log if matches(record)]

    # ----- the radio ---------------------------------------------------------------------------

    def transmit(self, sender: FakeCompanionFirmware, packet: RadioPacket) -> None:
        if packet.recipient_public_key is None:
            for recipient in self._nodes_in_range_of(sender):
                self._send_over_link(sender, recipient, packet)
            return
        addressed_recipient = self._node_with_public_key(packet.recipient_public_key)
        if addressed_recipient is None:
            self._record(
                sender_label=sender.label,
                recipient_label=UNKNOWN_RECIPIENT_LABEL,
                packet=packet,
                outcome=DeliveryOutcome.LOST_NO_SUCH_NODE,
            )
            return
        self._send_over_link(sender, addressed_recipient, packet)

    def _nodes_in_range_of(self, sender: FakeCompanionFirmware) -> list[FakeCompanionFirmware]:
        if sender is self.relay_firmware:
            return [device.firmware for device in self.devices.values()]
        return [self.relay_firmware]

    def _node_with_public_key(self, public_key: bytes) -> FakeCompanionFirmware | None:
        if self.relay_firmware.public_key == public_key:
            return self.relay_firmware
        for device in self.devices.values():
            if device.public_key == public_key:
                return device.firmware
        return None

    def _device_between(
        self, sender: FakeCompanionFirmware, recipient: FakeCompanionFirmware
    ) -> SimulatedDevice | None:
        for device in self.devices.values():
            linked_nodes = {id(device.firmware), id(self.relay_firmware)}
            if {id(sender), id(recipient)} == linked_nodes:
                return device
        return None

    def _send_over_link(
        self, sender: FakeCompanionFirmware, recipient: FakeCompanionFirmware, packet: RadioPacket
    ) -> None:
        device = self._device_between(sender, recipient)
        if device is None:
            self._record_loss(sender, recipient, packet, DeliveryOutcome.LOST_OUT_OF_RANGE)
            return
        sent_by_device = sender is device.firmware
        hops = device.route_to_relay if sent_by_device else tuple(reversed(device.route_to_relay))
        if is_zero_hop(packet.route) and hops:
            self._record_loss(sender, recipient, packet, DeliveryOutcome.LOST_OUT_OF_RANGE)
            return
        arrival = arrival_over_route(hops, packet.route)
        if arrival is None:
            self._record_loss(sender, recipient, packet, DeliveryOutcome.LOST_ON_STALE_ROUTE)
            return
        policy = device.uplink if sent_by_device else device.downlink
        if self._random.random() < policy.loss_probability_for(packet):
            self._record_loss(sender, recipient, packet, DeliveryOutcome.LOST_BY_POLICY)
            return
        pending_delivery = PendingDelivery(
            sender=sender, recipient=recipient, packet=packet, arrival=arrival, transmitted_at=time.monotonic()
        )
        self._schedule_delivery(pending_delivery, delay_seconds=self._draw_delay_seconds(policy))
        if self._random.random() < policy.duplicate_probability:
            duplicate_delivery = dataclasses.replace(
                pending_delivery,
                is_duplicate=True,
                bypasses_deduplication=policy.duplicates_bypass_deduplication,
            )
            self._schedule_delivery(duplicate_delivery, delay_seconds=self._draw_delay_seconds(policy))

    def _draw_delay_seconds(self, policy: LinkPolicy) -> float:
        delay_seconds = self._random.uniform(policy.minimum_delay_seconds, policy.maximum_delay_seconds)
        if self._random.random() < policy.reorder_probability:
            delay_seconds += policy.reorder_delay_seconds
        return delay_seconds

    def _schedule_delivery(self, pending_delivery: PendingDelivery, *, delay_seconds: float) -> None:
        loop = asyncio.get_running_loop()
        timer_holder: list[asyncio.TimerHandle] = []

        def deliver() -> None:
            self._delivery_timers.discard(timer_holder[0])
            try:
                self._deliver(pending_delivery)
            except Exception as delivery_error:
                self._delivery_errors.append(delivery_error)

        timer = loop.call_later(delay_seconds, deliver)
        timer_holder.append(timer)
        self._delivery_timers.add(timer)

    def _deliver(self, pending_delivery: PendingDelivery) -> None:
        reception = pending_delivery.recipient.receive_radio_packet(
            pending_delivery.packet,
            pending_delivery.arrival,
            bypasses_deduplication=pending_delivery.bypasses_deduplication,
        )
        self._record(
            sender_label=pending_delivery.sender.label,
            recipient_label=pending_delivery.recipient.label,
            packet=pending_delivery.packet,
            outcome=DeliveryOutcome.DELIVERED,
            reception=reception,
            is_duplicate=pending_delivery.is_duplicate,
            transmitted_at=pending_delivery.transmitted_at,
        )

    def _record_loss(
        self,
        sender: FakeCompanionFirmware,
        recipient: FakeCompanionFirmware,
        packet: RadioPacket,
        outcome: DeliveryOutcome,
    ) -> None:
        self._record(sender_label=sender.label, recipient_label=recipient.label, packet=packet, outcome=outcome)

    def _record(
        self,
        *,
        sender_label: str,
        recipient_label: str,
        packet: RadioPacket,
        outcome: DeliveryOutcome,
        reception: ReceptionOutcome | None = None,
        is_duplicate: bool = False,
        transmitted_at: float | None = None,
    ) -> None:
        recorded_at = time.monotonic()
        self.traffic_log.append(
            TrafficRecord(
                transmitted_at=recorded_at if transmitted_at is None else transmitted_at,
                recorded_at=recorded_at,
                sender=sender_label,
                recipient=recipient_label,
                packet=packet,
                outcome=outcome,
                reception=reception,
                is_duplicate=is_duplicate,
            )
        )

    # ----- traffic from strangers, heard only by the relay -------------------------------------

    def inject_channel_message(
        self,
        *,
        text: str,
        channel_secret: bytes = PUBLIC_CHANNEL_SECRET,
        sender_timestamp: int | None = None,
        hop_count: int = 1,
    ) -> ReceptionOutcome:
        """A group message some other node flooded; the relay's node queues it if it has the channel."""
        packet = ChannelMessagePacket(
            sender_public_key=self._random.randbytes(PUBLIC_KEY_BYTES),
            route=PacketRoute.flood(path_hash_size=1),
            channel_secret=channel_secret,
            sender_timestamp=int(time.time()) if sender_timestamp is None else sender_timestamp,
            text=text.encode(),
        )
        return self._deliver_stranger_packet(packet, hop_count=hop_count)

    def inject_channel_datagram(
        self,
        *,
        data: bytes,
        data_type: int = 1,
        channel_secret: bytes = PUBLIC_CHANNEL_SECRET,
        hop_count: int = 1,
    ) -> ReceptionOutcome:
        """A group datagram (queued as a code-27 frame, which meshcore's get_msg does not expect)."""
        packet = ChannelDataPacket(
            sender_public_key=self._random.randbytes(PUBLIC_KEY_BYTES),
            route=PacketRoute.flood(path_hash_size=1),
            channel_secret=channel_secret,
            data_type=data_type,
            data=data,
        )
        return self._deliver_stranger_packet(packet, hop_count=hop_count)

    def inject_foreign_traffic(self, packet_count: int) -> list[ReceptionOutcome]:
        """Direct messages between other nodes that the relay's radio hears (RX_LOG_DATA noise)."""
        receptions: list[ReceptionOutcome] = []
        for _ in range(packet_count):
            packet = DirectMessagePacket(
                sender_public_key=self._random.randbytes(PUBLIC_KEY_BYTES),
                route=PacketRoute.flood(path_hash_size=1),
                destination_public_key=self._random.randbytes(PUBLIC_KEY_BYTES),
                sender_timestamp=int(time.time()),
                attempt=0,
                text=self._random.randbytes(self._random.randint(1, 60)),
            )
            receptions.append(self._deliver_stranger_packet(packet, hop_count=self._random.randint(0, 3)))
        return receptions

    def _deliver_stranger_packet(self, packet: RadioPacket, *, hop_count: int) -> ReceptionOutcome:
        arrival = PacketArrival.flood(path=self._random.randbytes(hop_count), path_hash_size=1)
        reception = self.relay_firmware.receive_radio_packet(packet, arrival)
        self._record(
            sender_label=STRANGER_LABEL,
            recipient_label=self.relay_firmware.label,
            packet=packet,
            outcome=DeliveryOutcome.DELIVERED,
            reception=reception,
        )
        return reception


def is_zero_hop(route: PacketRoute) -> bool:
    return not route.is_flood and not route.path


def arrival_over_route(hops: Sequence[bytes], route: PacketRoute) -> PacketArrival | None:
    """How a packet sent with `route` over the current `hops` arrives, or None when a direct send
    follows a path that is no longer the route (it is lost on the way)."""
    current_path = b"".join(hop[: route.path_hash_size] for hop in hops)
    if route.is_flood:
        return PacketArrival.flood(path=current_path, path_hash_size=route.path_hash_size)
    if route.path == current_path:
        return PacketArrival.direct()
    return None
