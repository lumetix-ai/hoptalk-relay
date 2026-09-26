"""The fake companion firmware's configuration and the tables it keeps, in flash or in RAM.

The sizes and rules are those of the XIAO nRF52840 USB companion build of firmware v1.17.1:
350 contacts, 40 channels, a 256-frame offline queue, 8 expected ACKs, 16 packets and 160 packet hashes.
"""

import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.frames import (
    CHANNEL_FRAME_CODES,
    CHANNEL_SECRET_BYTES,
    PUBLIC_CHANNEL_NAME,
    PUBLIC_CHANNEL_SECRET,
    NodeType,
)

# Client repeat may be switched on only on these frequencies, in kHz (MyMesh::isValidClientRepeatFreq).
CLIENT_REPEAT_FREQUENCIES_KILOHERTZ = frozenset({433000, 869495, 918000})
AUTO_ADD_OVERWRITE_OLDEST = 0x01
AUTO_ADD_TYPE_BITS: dict[int, int] = {
    NodeType.CHAT: 0x02,
    NodeType.REPEATER: 0x04,
    NodeType.ROOM: 0x08,
    NodeType.SENSOR: 0x10,
}
FAVOURITE_CONTACT_FLAG = 0x01
ADVERT_BLOB_KEY_PREFIX_BYTES = 7


@dataclass(frozen=True, kw_only=True)
class FirmwareBuild:
    """What the node reports about itself and the defaults a factory reset returns to."""

    protocol_version_code: int = 13
    build_date: str = "14 Aug 2026"
    manufacturer_name: str = "Seeed Xiao-nrf52"
    firmware_version: str = "v1.17.1-d929643"
    maximum_transmit_power_dbm: int = 22
    default_frequency_megahertz: float = 869.618
    default_bandwidth_kilohertz: float = 62.5
    default_spreading_factor: int = 8
    default_coding_rate: int = 5
    default_transmit_power_dbm: int = 22
    # Where the volatile clock of a board without an RTC starts at every boot.
    volatile_clock_boot_time: int = 1715770351
    battery_millivolts: int = 4100
    storage_used_kilobytes: int = 64
    storage_total_kilobytes: int = 2048


@dataclass(frozen=True, kw_only=True)
class FirmwareCapacities:
    maximum_contacts: int = 350
    anonymous_contact_slots: int = 8
    group_channels: int = 40
    offline_queue_frames: int = 256
    expected_acknowledgement_entries: int = 8
    packet_pool_packets: int = 16
    seen_packet_hashes: int = 160
    advert_blobs: int = 100
    maximum_advert_blob_bytes: int = 166


@dataclass(frozen=True, kw_only=True)
class FirmwareTiming:
    """Delays of the fake node, a hundredth of the real ones so that tests stay fast."""

    # Before the node answers a command: the reply comes from a background task, never inline.
    command_processing_seconds: float = 0.001
    # The node streams one contact per main-loop pass; zero still lets commands in between.
    contact_listing_step_seconds: float = 0.0
    # Airtime of one packet; the packet pool stays occupied until the packet has been sent.
    transmit_seconds_per_packet: float = 0.0005
    acknowledgement_delay_seconds: float = 0.002
    reciprocal_path_delay_seconds: float = 0.005
    return_path_retry_delay_seconds: float = 0.03
    multipart_acknowledgement_spacing_seconds: float = 0.003
    imported_contact_processing_seconds: float = 0.001
    reboot_seconds: float = 0.05
    factory_reset_format_seconds: float = 0.03
    flood_suggested_timeout_milliseconds: int = 50
    direct_suggested_timeout_base_milliseconds: int = 10
    direct_suggested_timeout_per_hop_milliseconds: int = 20
    # With MessageSentReplyOrder.AFTER_ACKNOWLEDGEMENT, how long MSG_SENT waits for its ACK push.
    held_message_sent_reply_limit_seconds: float = 0.5

    def direct_suggested_timeout_milliseconds(self, hop_count: int) -> int:
        return self.direct_suggested_timeout_base_milliseconds + self.direct_suggested_timeout_per_hop_milliseconds * (
            hop_count + 1
        )


class PowerState(Enum):
    RUNNING = "running"
    REBOOTING = "rebooting"
    OFF = "off"


def round_to_float32(value: float) -> float:
    """The firmware keeps radio settings as 32-bit floats; reading them back can lose a unit."""
    rounded_value: float = struct.unpack("<f", struct.pack("<f", value))[0]
    return rounded_value


@dataclass(kw_only=True)
class NodePreferences:
    """The node's settings as /prefs.json holds them; every setting command changes them at once."""

    node_name: bytes
    frequency_megahertz: float
    bandwidth_kilohertz: float
    spreading_factor: int
    coding_rate: int
    transmit_power_dbm: int
    client_repeat_enabled: bool = False
    manual_add_contacts: int = 0
    telemetry_mode_base: int = 0
    telemetry_mode_location: int = 0
    telemetry_mode_environment: int = 0
    advert_location_policy: int = 0
    multi_acknowledgements: int = 0
    path_hash_mode: int = 0
    auto_add_configuration: int = 0
    auto_add_maximum_hops: int = 0
    receive_delay_base_thousandths: int = 0
    airtime_factor_thousandths: int = 1000
    latitude_microdegrees: int = 0
    longitude_microdegrees: int = 0
    bluetooth_pin: int = 0

    @classmethod
    def build_defaults(cls, *, build: FirmwareBuild, public_key: bytes) -> NodePreferences:
        """The preferences of a node without /prefs.json: named after its key's first four bytes."""
        return cls(
            node_name=public_key[:4].hex().upper().encode(),
            frequency_megahertz=round_to_float32(build.default_frequency_megahertz),
            bandwidth_kilohertz=round_to_float32(build.default_bandwidth_kilohertz),
            spreading_factor=build.default_spreading_factor,
            coding_rate=build.default_coding_rate,
            transmit_power_dbm=build.default_transmit_power_dbm,
        )

    @property
    def frequency_kilohertz(self) -> int:
        return int(round_to_float32(self.frequency_megahertz * 1000))

    @property
    def bandwidth_hertz(self) -> int:
        return int(round_to_float32(self.bandwidth_kilohertz * 1000))

    def store_radio_parameters(
        self, *, frequency_kilohertz: int, bandwidth_hertz: int, spreading_factor: int, coding_rate: int
    ) -> None:
        self.frequency_megahertz = round_to_float32(frequency_kilohertz / 1000.0)
        self.bandwidth_kilohertz = round_to_float32(bandwidth_hertz / 1000.0)
        self.spreading_factor = spreading_factor
        self.coding_rate = coding_rate

    @property
    def telemetry_modes(self) -> int:
        return (self.telemetry_mode_environment << 4) | (self.telemetry_mode_location << 2) | self.telemetry_mode_base

    def store_telemetry_modes(self, telemetry_modes: int) -> None:
        self.telemetry_mode_base = telemetry_modes & 0x03
        self.telemetry_mode_location = (telemetry_modes >> 2) & 0x03
        self.telemetry_mode_environment = (telemetry_modes >> 4) & 0x03

    def auto_adds_node_type(self, node_type: int) -> bool:
        """MyMesh::shouldAutoAddContactType: everything without manual add, else only the enabled types."""
        if not self.manual_add_contacts & 0x01:
            return True
        type_bit = AUTO_ADD_TYPE_BITS.get(node_type)
        return type_bit is not None and bool(self.auto_add_configuration & type_bit)

    @property
    def overwrites_oldest_contact_when_full(self) -> bool:
        return bool(self.auto_add_configuration & AUTO_ADD_OVERWRITE_OLDEST)


@dataclass(frozen=True, kw_only=True)
class ContactAddOutcome:
    added: bool
    # Set when the table was full and the oldest non-favourite contact made room.
    overwritten_public_key: bytes | None = None


class ContactTable:
    """The contacts array: 8 transient slots for type-0 records first, then up to 350 contacts.

    Lookups scan the transient slots first and return the first match in array order; a removal
    shifts the later contacts down, as removeContact does.
    """

    def __init__(self, *, maximum_contacts: int, anonymous_slots: int) -> None:
        self.maximum_contacts = maximum_contacts
        self._contacts: list[ContactRecord] = []
        self._anonymous_contacts: list[ContactRecord | None] = [None] * anonymous_slots

    def __len__(self) -> int:
        return len(self._contacts)

    def records(self) -> list[ContactRecord]:
        return list(self._contacts)

    def record_at(self, index: int) -> ContactRecord | None:
        if index < len(self._contacts):
            return self._contacts[index]
        return None

    def _records_in_array_order(self) -> list[ContactRecord]:
        anonymous_records = [record for record in self._anonymous_contacts if record is not None]
        return anonymous_records + self._contacts

    def find_by_prefix(self, public_key_prefix: bytes) -> ContactRecord | None:
        for record in self._records_in_array_order():
            if record.public_key.startswith(public_key_prefix):
                return record
        return None

    def find_by_public_key(self, public_key: bytes) -> ContactRecord | None:
        for record in self._records_in_array_order():
            if record.public_key == public_key:
                return record
        return None

    def replace(self, updated_record: ContactRecord) -> None:
        for index, record in enumerate(self._contacts):
            if record.public_key == updated_record.public_key:
                self._contacts[index] = updated_record
                return
        for index, anonymous_record in enumerate(self._anonymous_contacts):
            if anonymous_record is not None and anonymous_record.public_key == updated_record.public_key:
                self._anonymous_contacts[index] = updated_record
                return

    def add(self, record: ContactRecord, *, overwrite_oldest_when_full: bool) -> ContactAddOutcome:
        """BaseChatMesh::addContact and allocateContactSlot."""
        if record.node_type == NodeType.NONE:
            self._store_in_anonymous_slot(record)
            return ContactAddOutcome(added=True)
        if len(self._contacts) < self.maximum_contacts:
            self._contacts.append(record)
            return ContactAddOutcome(added=True)
        if not overwrite_oldest_when_full:
            return ContactAddOutcome(added=False)
        return self._overwrite_oldest_non_favourite(record)

    def _store_in_anonymous_slot(self, record: ContactRecord) -> None:
        def slot_age(slot_index: int) -> int:
            slot_record = self._anonymous_contacts[slot_index]
            return 0 if slot_record is None else slot_record.last_modified

        oldest_slot_index = min(range(len(self._anonymous_contacts)), key=slot_age)
        self._anonymous_contacts[oldest_slot_index] = record

    def _overwrite_oldest_non_favourite(self, record: ContactRecord) -> ContactAddOutcome:
        candidate_indexes = [
            index for index, contact in enumerate(self._contacts) if not contact.flags & FAVOURITE_CONTACT_FLAG
        ]
        if not candidate_indexes:
            return ContactAddOutcome(added=False)
        oldest_index = min(candidate_indexes, key=lambda index: self._contacts[index].last_modified)
        overwritten_public_key = self._contacts[oldest_index].public_key
        self._contacts[oldest_index] = record
        return ContactAddOutcome(added=True, overwritten_public_key=overwritten_public_key)

    def remove(self, public_key: bytes) -> bool:
        for index, record in enumerate(self._contacts):
            if record.public_key == public_key:
                del self._contacts[index]
                return True
        return False

    def maximum_last_modified(self) -> int:
        return max((record.last_modified for record in self._contacts), default=0)

    def forget_anonymous_contacts(self) -> None:
        """Transient slots are never saved (save_filter), so a reboot empties them."""
        self._anonymous_contacts = [None] * len(self._anonymous_contacts)


@dataclass(frozen=True, kw_only=True)
class ChannelSlot:
    name: bytes = b""
    secret: bytes = bytes(CHANNEL_SECRET_BYTES)


class ChannelTable:
    """The group channels; slot 0 holds the pre-configured Public channel until it is replaced."""

    def __init__(self, *, group_channels: int) -> None:
        self._slots = [ChannelSlot() for _ in range(group_channels)]
        self._slots[0] = ChannelSlot(name=PUBLIC_CHANNEL_NAME, secret=PUBLIC_CHANNEL_SECRET)

    def get(self, channel_index: int) -> ChannelSlot | None:
        if 0 <= channel_index < len(self._slots):
            return self._slots[channel_index]
        return None

    def set(self, channel_index: int, slot: ChannelSlot) -> bool:
        if 0 <= channel_index < len(self._slots):
            self._slots[channel_index] = slot
            return True
        return False

    def find_index_by_secret(self, channel_secret: bytes) -> int | None:
        for channel_index, slot in enumerate(self._slots):
            if slot.secret == channel_secret:
                return channel_index
        return None


class OfflineMessageQueue:
    """Received messages waiting for CMD_SYNC_NEXT_MESSAGE, in RAM only.

    When it is full, the oldest channel frame makes room for the new frame; a queue full of direct
    messages drops the new frame instead (MyMesh::addToOfflineQueue).
    """

    def __init__(self, *, capacity: int) -> None:
        self.capacity = capacity
        self._frames: deque[bytes] = deque()

    def __len__(self) -> int:
        return len(self._frames)

    def frames(self) -> list[bytes]:
        return list(self._frames)

    def add(self, frame: bytes) -> bool:
        if len(self._frames) < self.capacity:
            self._frames.append(frame)
            return True
        for index, queued_frame in enumerate(self._frames):
            if queued_frame[0] in CHANNEL_FRAME_CODES:
                del self._frames[index]
                self._frames.append(frame)
                return True
        return False

    def pop(self) -> bytes | None:
        if not self._frames:
            return None
        return self._frames.popleft()

    def clear(self) -> None:
        self._frames.clear()


@dataclass(frozen=True, kw_only=True)
class ExpectedAcknowledgement:
    code: bytes
    sent_at: float
    contact_public_key: bytes


class ExpectedAcknowledgementTable:
    """The circular table of ACK codes the node waits for; a ninth send overwrites the oldest entry."""

    def __init__(self, *, size: int) -> None:
        self._entries: list[ExpectedAcknowledgement | None] = [None] * size
        self._next_index = 0

    def add(self, entry: ExpectedAcknowledgement) -> None:
        self._entries[self._next_index] = entry
        self._next_index = (self._next_index + 1) % len(self._entries)

    def match(self, code: bytes) -> ExpectedAcknowledgement | None:
        """The first matching entry, which is then cleared: the same ACK can arrive several times."""
        for index, entry in enumerate(self._entries):
            if entry is not None and entry.code == code:
                self._entries[index] = None
                return entry
        return None

    def codes(self) -> list[bytes]:
        return [entry.code for entry in self._entries if entry is not None]

    def clear(self) -> None:
        self._entries = [None] * len(self._entries)
        self._next_index = 0


class SeenPacketTable:
    """The ring of recently seen packet hashes; a packet whose hash is still in it is dropped unread."""

    def __init__(self, *, size: int) -> None:
        self._hashes: deque[bytes] = deque(maxlen=size)

    def was_seen(self, packet_hash: bytes) -> bool:
        return packet_hash in self._hashes

    def mark_seen(self, packet_hash: bytes) -> None:
        self._hashes.append(packet_hash)

    def forget(self, packet_hash: bytes) -> None:
        while packet_hash in self._hashes:
            self._hashes.remove(packet_hash)

    def clear(self) -> None:
        self._hashes.clear()


class VolatileClock:
    """The RTC of a board without a clock chip: it starts over at every boot and never persists."""

    def __init__(self, *, start_time: int, monotonic_time: Callable[[], float]) -> None:
        self._monotonic_time = monotonic_time
        self._base_time = start_time
        self._base_set_at = monotonic_time()
        self._last_unique_time = 0

    def current_time(self) -> int:
        return self._base_time + int(self._monotonic_time() - self._base_set_at)

    def current_time_unique(self) -> int:
        current_time = self.current_time()
        if current_time <= self._last_unique_time:
            self._last_unique_time += 1
        else:
            self._last_unique_time = current_time
        return self._last_unique_time

    def set_time(self, new_time: int) -> None:
        self._base_time = new_time
        self._base_set_at = self._monotonic_time()


@dataclass(frozen=True, kw_only=True)
class AdvertBlob:
    packet: bytes
    stored_at_node_time: int


@dataclass(kw_only=True)
class AdvertBlobStore:
    """The raw adverts kept for exporting contacts, matched by a 7-byte key prefix (DataStore /adv_blobs)."""

    capacity: int
    maximum_blob_bytes: int
    _blobs: dict[bytes, AdvertBlob] = field(default_factory=dict)

    def put(self, *, public_key: bytes, packet: bytes, stored_at_node_time: int) -> None:
        if len(packet) > self.maximum_blob_bytes:
            return
        key_prefix = public_key[:ADVERT_BLOB_KEY_PREFIX_BYTES]
        if key_prefix not in self._blobs and len(self._blobs) >= self.capacity:
            oldest_prefix = min(self._blobs, key=lambda prefix: self._blobs[prefix].stored_at_node_time)
            del self._blobs[oldest_prefix]
        self._blobs[key_prefix] = AdvertBlob(packet=packet, stored_at_node_time=stored_at_node_time)

    def get(self, public_key: bytes) -> bytes | None:
        blob = self._blobs.get(public_key[:ADVERT_BLOB_KEY_PREFIX_BYTES])
        return None if blob is None else blob.packet
