"""An in-process MeshCore companion node: firmware v1.17.1 as built for the XIAO nRF52840 over USB.

The same class plays the relay's node, driven by the real meshcore library through a
`FakeNodeTransport`, and every simulated user device of a `SimulatedMesh`, driven by that device's
simulated app.

Host side: `receive_host_bytes` takes `3C len16` command frames; replies and pushes leave through
the attached `NodeHostLink`, and are lost while none is attached, as USB writes are when no host
reads them. One command is handled per main-loop pass, after `FirmwareTiming.command_processing_seconds`,
and a contact listing streams one contact per pass between commands.

Radio side: packets leave through the attached `RadioMedium` once the packet pool lets them in, and
arrive through `receive_radio_packet`.

Test controls (all synchronous, call them from the test's event loop):

- `drop_next_reply(command_code=None, count=1)`, `delay_next_reply(seconds, command_code=None)`,
  `lose_next_command(command_code=None, count=1)`, `drop_next_push(push_code=None, count=1)`;
- `message_sent_reply_order = MessageSentReplyOrder.AFTER_ACKNOWLEDGEMENT` writes a `MSG_SENT`
  only after the ACK push of that message (the unmatched-ACK race);
- `occupy_packet_pool(slot_count)` / `release_packet_pool()` make sends fail with `ERR_CODE_TABLE_FULL`;
- `reboot()`, `factory_reset()`, `power_off()`, `power_on()`, `stays_off_after_reboot`,
  `factory_reset_behaviour`;
- `private_key_export_enabled` and `private_key_import_enabled` off answer the key export and import
  with `RESP_CODE_DISABLED`, as a firmware built without `ENABLE_PRIVATE_KEY_EXPORT` or
  `ENABLE_PRIVATE_KEY_IMPORT` does; `exported_private_key_override` makes the export answer with
  other bytes than the node's own key, as a peer answering for another identity or a corrupted
  frame would; `private_key_import_save_fails` answers a valid import with `ERR_CODE_FILE_IO_ERROR`,
  as saveMainIdentity failing on a worn flash does;
- `receive_log_pushes_enabled` pushes `RX_LOG_DATA` for every packet the radio hears;
- `force_clock_time(unix_time)`, `add_or_update_contact(record)`.

Observations: `command_log`, `transmitted_packets`, `protocol_violations` (frames the real firmware
would accept but misread), `imported_card_outcomes`, `frames_lost_without_host`, `internal_errors`,
`contact_records()`, `offline_queue_frames()`, `expected_acknowledgement_codes()`, `clock_time()`,
`app_target_version`, `preferences`.
"""

import asyncio
import contextlib
import dataclasses
import heapq
import itertools
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, StrEnum
from typing import Protocol

from tests.worker.fake_node.contact_records import (
    ADD_UPDATE_CONTACT_WITH_LOCATION_BYTES,
    ContactRecord,
    contact_name_field,
    decode_path_hash_size,
)
from tests.worker.fake_node.firmware_state import (
    CLIENT_REPEAT_FREQUENCIES_KILOHERTZ,
    AdvertBlobStore,
    ChannelSlot,
    ChannelTable,
    ContactAddOutcome,
    ContactTable,
    ExpectedAcknowledgement,
    ExpectedAcknowledgementTable,
    FirmwareBuild,
    FirmwareCapacities,
    FirmwareTiming,
    NodePreferences,
    OfflineMessageQueue,
    PowerState,
    SeenPacketTable,
    VolatileClock,
)
from tests.worker.fake_node.frames import (
    ACKNOWLEDGEMENT_CODE_BYTES,
    CHANNEL_NAME_FIELD_BYTES,
    CHANNEL_SECRET_BYTES,
    FACTORY_RESET_COMMAND_SUFFIX,
    MAXIMUM_DIRECT_MESSAGE_TEXT_BYTES,
    MAXIMUM_DIRECT_MESSAGE_TEXT_BYTES_WITH_EXTENDED_ATTEMPT,
    MAXIMUM_FRAME_BYTES,
    NODE_NAME_MAXIMUM_BYTES,
    PUBLIC_KEY_BYTES,
    PUBLIC_KEY_PREFIX_BYTES,
    REBOOT_COMMAND_SUFFIX,
    CommandCode,
    FirmwareErrorCode,
    HostToNodeDeframer,
    NodeType,
    PushCode,
    ResponseCode,
    TextType,
    auto_add_configuration_frame,
    battery_and_storage_frame,
    channel_data_frame,
    channel_info_frame,
    channel_message_frame,
    contact_message_frame,
    contacts_full_push,
    contacts_start_frame,
    current_time_frame,
    decode_signed_32,
    decode_unsigned_32,
    device_info_frame,
    disabled_frame,
    end_of_contacts_frame,
    error_frame,
    export_contact_frame,
    message_sent_frame,
    messages_waiting_push,
    no_more_messages_frame,
    ok_frame,
    private_key_frame,
    public_key_push,
    receive_log_push,
    self_info_frame,
    send_confirmed_push,
    text_before_first_nul,
    tuning_parameters_frame,
)
from tests.worker.fake_node.node_identity import (
    RESERVED_PUBLIC_KEY_FIRST_BYTES,
    ROUTE_TYPE_FLOOD,
    AdvertLocation,
    NodeIdentity,
    ParsedAdvert,
    build_advert_app_data,
    build_advert_packet,
    build_advert_payload,
    build_self_card,
    is_clamped_scalar,
    parse_advert_payload,
    read_advert_packet_envelope,
)
from tests.worker.fake_node.radio_packets import (
    PATH_RETURN_RANDOM_FILLER_BYTES,
    AcknowledgementPacket,
    AcknowledgementPayload,
    AdvertPacket,
    ChannelDataPacket,
    ChannelMessagePacket,
    DirectMessagePacket,
    MultipartAcknowledgementPacket,
    PacketArrival,
    PacketRoute,
    PathReturnPacket,
    RadioPacket,
    calculate_expected_acknowledgement,
)

HIGHEST_ATTEMPT_WITHOUT_EXTENSION = 3
MINIMUM_TRANSMIT_POWER_DBM = -9
MINIMUM_FREQUENCY_KILOHERTZ = 150_000
MAXIMUM_FREQUENCY_KILOHERTZ = 2_500_000
MINIMUM_BANDWIDTH_HERTZ = 7_000
MAXIMUM_BANDWIDTH_HERTZ = 500_000
SPREADING_FACTOR_RANGE = range(5, 13)
CODING_RATE_RANGE = range(5, 9)
MAXIMUM_PATH_HASH_MODE = 2
MAXIMUM_AUTO_ADD_HOPS = 64
MAXIMUM_LATITUDE_MICRODEGREES = 90_000_000
MAXIMUM_LONGITUDE_MICRODEGREES = 180_000_000
# Frames that carry V3 message layouts once the app has declared at least this protocol version.
VERSION_3_APP_TARGET = 3
RADIO_PARAMETERS_WITHOUT_REPEAT_BYTES = 11
TUNING_PARAMETERS_BYTES = 9
# CMD_SET_CHANNEL with a 32-byte secret is answered "unsupported" (only 128-bit secrets are).
SET_CHANNEL_WITH_LONG_SECRET_BYTES = 2 + CHANNEL_NAME_FIELD_BYTES + 32
SET_CHANNEL_BYTES = 2 + CHANNEL_NAME_FIELD_BYTES + CHANNEL_SECRET_BYTES
# CMD_IMPORT_CONTACT needs more than 98 bytes (MyMesh::handleCmdFrame).
IMPORT_CONTACT_MINIMUM_BYTES = 99
# CMD_IMPORT_PRIVATE_KEY needs the code and the 64-byte key; a shorter frame is an unknown command.
IMPORT_PRIVATE_KEY_MINIMUM_BYTES = 65
MAXIMUM_CHANNEL_DATA_BYTES = MAXIMUM_FRAME_BYTES - 9
SIGNAL_TO_NOISE_QUARTERS_RANGE = (-20, 48)
RECEIVED_SIGNAL_STRENGTH_RANGE = (-120, -40)


class NodeHostLink(Protocol):
    """Where the node writes its frames: the USB link to the relay worker, or a device's app."""

    def deliver_frame_to_host(self, frame: bytes) -> None: ...

    def node_dropped_link(self, reason: str) -> None: ...


class RadioMedium(Protocol):
    def transmit(self, sender: FakeCompanionFirmware, packet: RadioPacket) -> None: ...


class MessageSentReplyOrder(Enum):
    BEFORE_ACKNOWLEDGEMENT = "before_acknowledgement"
    # MSG_SENT is held until the ACK push for that message has been written (or a time limit passed),
    # so the host sees the ACK before it learns the code: the unmatched-ACK race, made deterministic.
    AFTER_ACKNOWLEDGEMENT = "after_acknowledgement"


class FactoryResetBehaviour(Enum):
    PERFORMS_RESET = "performs_reset"
    # Neither a reply nor a reset: a firmware that expects another payload and says nothing.
    IGNORES_COMMAND = "ignores_command"
    # Formatting the file system failed: serial stays disabled until the node is power-cycled.
    FAILS_TO_FORMAT = "fails_to_format"


class ReceptionOutcome(StrEnum):
    ACCEPTED = "accepted"
    NODE_NOT_RUNNING = "node_not_running"
    PACKET_POOL_FULL = "packet_pool_full"
    ALREADY_SEEN = "already_seen"
    NOT_ADDRESSED_TO_THIS_NODE = "not_addressed_to_this_node"
    UNKNOWN_SENDER = "unknown_sender"
    UNSUPPORTED_TEXT_TYPE = "unsupported_text_type"
    ACKNOWLEDGEMENT_NOT_EXPECTED = "acknowledgement_not_expected"
    OWN_ADVERT = "own_advert"
    MALFORMED_ADVERT = "malformed_advert"
    FORGED_SIGNATURE = "forged_signature"
    ADVERT_WITHOUT_NAME = "advert_without_name"
    ADVERT_NOT_NEWER = "advert_not_newer"
    REPORTED_AS_NEW_CONTACT = "reported_as_new_contact"
    CONTACT_TABLE_FULL = "contact_table_full"
    UNKNOWN_CHANNEL = "unknown_channel"
    CHANNEL_DATA_TOO_LONG = "channel_data_too_long"


@dataclass(frozen=True, kw_only=True)
class ReceivedCommand:
    code: int
    frame: bytes
    received_at: float


@dataclass(frozen=True, kw_only=True)
class TextMessageQueued:
    sent_by_flood: bool
    expected_acknowledgement: bytes
    suggested_timeout_milliseconds: int


@dataclass(frozen=True, kw_only=True)
class TextMessageRejected:
    error_code: FirmwareErrorCode


type SendTextMessageResult = TextMessageQueued | TextMessageRejected


class FaultKind(Enum):
    DROP_REPLY = "drop_reply"
    DELAY_REPLY = "delay_reply"
    LOSE_COMMAND = "lose_command"
    DROP_PUSH = "drop_push"


@dataclass(frozen=True, kw_only=True)
class InjectedFault:
    kind: FaultKind
    # None matches any command (or push) code.
    code: int | None
    delay_seconds: float = 0.0

    def applies_to(self, kind: FaultKind, code: int) -> bool:
        return self.kind is kind and (self.code is None or self.code == code)


@dataclass(order=True)
class ScheduledTransmission:
    due_at: float
    sequence: int
    packet: RadioPacket = field(compare=False)


@dataclass(kw_only=True)
class ContactListing:
    next_index: int = 0
    since: int = 0
    most_recent_last_modified: int = 0
    replies_are_dropped: bool = False


@dataclass(kw_only=True)
class HeldMessageSentReply:
    frame: bytes
    timer: asyncio.TimerHandle


@dataclass(frozen=True, kw_only=True)
class CommandHandler:
    minimum_frame_length: int
    handle: Callable[[bytes], None]


class FakeCompanionFirmware:
    def __init__(
        self,
        *,
        label: str = "relay",
        node_name: str | None = None,
        seed: int = 0,
        random_generator: random.Random | None = None,
        identity: NodeIdentity | None = None,
        timing: FirmwareTiming | None = None,
        capacities: FirmwareCapacities | None = None,
        build: FirmwareBuild | None = None,
    ) -> None:
        self.label = label
        self.timing = timing or FirmwareTiming()
        self.capacities = capacities or FirmwareCapacities()
        self.build = build or FirmwareBuild()
        self._random = random_generator if random_generator is not None else random.Random(seed)

        self._identity = identity or NodeIdentity.generate(self._random)
        self._flash_was_erased = False
        self.preferences = NodePreferences.build_defaults(build=self.build, public_key=self._identity.public_key)
        if node_name is not None:
            self.preferences.node_name = node_name.encode()[:NODE_NAME_MAXIMUM_BYTES]
        self._contacts = self._create_contact_table()
        self._channels = ChannelTable(group_channels=self.capacities.group_channels)
        self._advert_blobs = self._create_advert_blob_store()

        self._offline_queue = OfflineMessageQueue(capacity=self.capacities.offline_queue_frames)
        self._expected_acknowledgements = ExpectedAcknowledgementTable(
            size=self.capacities.expected_acknowledgement_entries
        )
        self._seen_packets = SeenPacketTable(size=self.capacities.seen_packet_hashes)
        self._clock = VolatileClock(start_time=self.build.volatile_clock_boot_time, monotonic_time=time.monotonic)
        self._app_target_version = 0
        self._contact_listing: ContactListing | None = None
        self._transmit_queue: list[ScheduledTransmission] = []
        self._transmission_sequence = itertools.count()
        self._packet_is_on_air = False
        self._occupied_packet_pool_slots = 0
        self._held_message_sent_replies: dict[bytes, HeldMessageSentReply] = {}
        self._host_deframer = HostToNodeDeframer()
        self._serial_interface_enabled = True
        self._boot_generation = 0

        self.power_state = PowerState.OFF
        self._host_link: NodeHostLink | None = None
        self._radio: RadioMedium | None = None
        self._command_frames: asyncio.Queue[bytes] = asyncio.Queue()
        self._transmit_wakeup = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._timers: set[asyncio.TimerHandle] = set()
        self._current_reply_is_dropped = False
        self._command_handlers = self._build_command_handlers()

        self._faults: list[InjectedFault] = []
        self.message_sent_reply_order = MessageSentReplyOrder.BEFORE_ACKNOWLEDGEMENT
        self.receive_log_pushes_enabled = False
        self.factory_reset_behaviour = FactoryResetBehaviour.PERFORMS_RESET
        self.stays_off_after_reboot = False
        self.private_key_export_enabled = True
        self.private_key_import_enabled = True
        self.exported_private_key_override: bytes | None = None
        self.private_key_import_save_fails = False

        self.command_log: list[ReceivedCommand] = []
        self.transmitted_packets: list[RadioPacket] = []
        self.protocol_violations: list[str] = []
        self.imported_card_outcomes: list[ReceptionOutcome] = []
        self.frames_lost_without_host = 0
        # Exceptions raised inside the fake itself; the fixtures fail the test when any are left.
        self.internal_errors: list[Exception] = []

    def __repr__(self) -> str:
        return f"FakeCompanionFirmware(label={self.label!r}, public_key={self.public_key.hex()[:12]}…)"

    def _create_contact_table(self) -> ContactTable:
        return ContactTable(
            maximum_contacts=self.capacities.maximum_contacts,
            anonymous_slots=self.capacities.anonymous_contact_slots,
        )

    def _create_advert_blob_store(self) -> AdvertBlobStore:
        return AdvertBlobStore(
            capacity=self.capacities.advert_blobs, maximum_blob_bytes=self.capacities.maximum_advert_blob_bytes
        )

    # ----- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        """Power the node on; needs a running event loop."""
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._run_main_loop(), name=f"fake node {self.label} main loop"),
            asyncio.create_task(self._run_transmitter(), name=f"fake node {self.label} transmitter"),
        ]
        self._boot()

    async def stop(self) -> None:
        self._host_link = None
        self.power_state = PowerState.OFF
        for timer in list(self._timers):
            timer.cancel()
        self._timers.clear()
        for held_reply in self._held_message_sent_replies.values():
            held_reply.timer.cancel()
        self._held_message_sent_replies.clear()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    @property
    def is_running(self) -> bool:
        return self.power_state is PowerState.RUNNING

    @property
    def accepts_host_connections(self) -> bool:
        """Whether the USB device is there to be opened: not while the node reboots or is off."""
        return self.power_state is PowerState.RUNNING

    def reboot(self) -> None:
        """What `13 "reboot"` does: no reply, the link drops, RAM is lost, and the node boots again."""
        self._shut_down(next_power_state=PowerState.REBOOTING)
        if self.stays_off_after_reboot:
            self.power_state = PowerState.OFF
            return
        self._schedule_for_this_boot(self.timing.reboot_seconds, self._finish_reboot)

    def _finish_reboot(self) -> None:
        if self.power_state is PowerState.REBOOTING:
            self._boot()

    def factory_reset(self) -> None:
        """What `33 "reset"` does: serial is disabled first, so no reply ever arrives."""
        match self.factory_reset_behaviour:
            case FactoryResetBehaviour.IGNORES_COMMAND:
                return
            case FactoryResetBehaviour.FAILS_TO_FORMAT:
                self._serial_interface_enabled = False
            case FactoryResetBehaviour.PERFORMS_RESET:
                self._serial_interface_enabled = False
                self._erase_flash()
                self._schedule_for_this_boot(self.timing.factory_reset_format_seconds, self.reboot)

    def _erase_flash(self) -> None:
        """Identity, preferences, contacts, channels and cached adverts; the new ones appear at boot."""
        self._flash_was_erased = True
        self._contacts = self._create_contact_table()
        self._channels = ChannelTable(group_channels=self.capacities.group_channels)
        self._advert_blobs = self._create_advert_blob_store()

    def power_off(self) -> None:
        self._shut_down(next_power_state=PowerState.OFF)

    def power_on(self) -> None:
        if self.power_state is not PowerState.RUNNING:
            self._boot()

    def _shut_down(self, *, next_power_state: PowerState) -> None:
        self.power_state = next_power_state
        self._forget_volatile_state()
        self._drop_host_link("serial_disconnect")

    def _boot(self) -> None:
        if self._flash_was_erased:
            self._identity = NodeIdentity.generate(self._random)
            self.preferences = NodePreferences.build_defaults(build=self.build, public_key=self._identity.public_key)
            self._flash_was_erased = False
        self._forget_volatile_state()
        self._start_clock_from_contacts()
        self._serial_interface_enabled = True
        self._host_deframer.reset()
        self.power_state = PowerState.RUNNING

    def _start_clock_from_contacts(self) -> None:
        """BaseChatMesh::bootstrapRTCfromContacts: the newest lastmod plus one, when there is one."""
        self._clock = VolatileClock(start_time=self.build.volatile_clock_boot_time, monotonic_time=time.monotonic)
        newest_last_modified = self._contacts.maximum_last_modified()
        if newest_last_modified:
            self._clock.set_time(newest_last_modified + 1)

    def _forget_volatile_state(self) -> None:
        self._boot_generation += 1
        self._offline_queue.clear()
        self._expected_acknowledgements.clear()
        self._seen_packets.clear()
        self._app_target_version = 0
        self._contact_listing = None
        self._transmit_queue.clear()
        self._occupied_packet_pool_slots = 0
        self._contacts.forget_anonymous_contacts()
        for held_reply in self._held_message_sent_replies.values():
            held_reply.timer.cancel()
        self._held_message_sent_replies.clear()
        while not self._command_frames.empty():
            self._command_frames.get_nowait()

    def _schedule(self, delay_seconds: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        loop = asyncio.get_running_loop()
        timer_holder: list[asyncio.TimerHandle] = []

        def run_scheduled_callback() -> None:
            self._timers.discard(timer_holder[0])
            try:
                callback()
            except Exception as internal_error:
                self.internal_errors.append(internal_error)

        timer = loop.call_later(delay_seconds, run_scheduled_callback)
        timer_holder.append(timer)
        self._timers.add(timer)
        return timer

    def _schedule_for_this_boot(self, delay_seconds: float, callback: Callable[[], None]) -> None:
        """Run the callback later, unless the node has rebooted or powered off in between."""
        boot_generation = self._boot_generation

        def run_if_still_the_same_boot() -> None:
            if boot_generation == self._boot_generation:
                callback()

        self._schedule(delay_seconds, run_if_still_the_same_boot)

    # ----- identity, clock and tables ----------------------------------------------------------

    @property
    def identity(self) -> NodeIdentity:
        return self._identity

    @property
    def public_key(self) -> bytes:
        return self._identity.public_key

    @property
    def app_target_version(self) -> int:
        return self._app_target_version

    def record_app_protocol_version(self, app_target_version: int) -> None:
        """CMD_DEVICE_QUERY: from version 3 on, received messages are queued in V3 frames."""
        self._app_target_version = app_target_version

    def clock_time(self) -> int:
        return self._clock.current_time()

    def set_clock_time(self, new_time: int) -> bool:
        """CMD_SET_DEVICE_TIME, which refuses to move the clock backwards."""
        if new_time < self._clock.current_time():
            return False
        self._clock.set_time(new_time)
        return True

    def force_clock_time(self, new_time: int) -> None:
        self._clock.set_time(new_time)

    def contact_records(self) -> list[ContactRecord]:
        return self._contacts.records()

    def find_contact(self, public_key: bytes) -> ContactRecord | None:
        return self._contacts.find_by_public_key(public_key)

    def add_or_update_contact(self, record: ContactRecord) -> ContactAddOutcome:
        """What CMD_ADD_UPDATE_CONTACT does with a well-formed record: overwrite, or add."""
        if self._contacts.find_by_public_key(record.public_key) is not None:
            self._contacts.replace(record)
            return ContactAddOutcome(added=True)
        outcome = self._contacts.add(
            record, overwrite_oldest_when_full=self.preferences.overwrites_oldest_contact_when_full
        )
        if outcome.overwritten_public_key is not None:
            self._write_push(public_key_push(PushCode.CONTACT_DELETED, outcome.overwritten_public_key))
        return outcome

    def reset_route(self, public_key: bytes) -> bool:
        """CMD_RESET_PATH: the next message to that contact floods. The path bytes stay."""
        record = self._contacts.find_by_public_key(public_key)
        if record is None:
            return False
        self._contacts.replace(record.without_route())
        return True

    def channel(self, channel_index: int) -> ChannelSlot | None:
        return self._channels.get(channel_index)

    def offline_queue_frames(self) -> list[bytes]:
        return self._offline_queue.frames()

    @property
    def offline_queue_length(self) -> int:
        return len(self._offline_queue)

    def pop_offline_frame(self) -> bytes | None:
        """CMD_SYNC_NEXT_MESSAGE: the frame leaves the queue before anyone has read it."""
        return self._offline_queue.pop()

    def expected_acknowledgement_codes(self) -> list[bytes]:
        return self._expected_acknowledgements.codes()

    @property
    def has_pending_transmissions(self) -> bool:
        return bool(self._transmit_queue) or self._packet_is_on_air

    @property
    def packet_pool_occupancy(self) -> int:
        packets_being_sent = 1 if self._packet_is_on_air else 0
        return len(self._transmit_queue) + packets_being_sent + self._occupied_packet_pool_slots

    def _packet_pool_is_full(self) -> bool:
        return self.packet_pool_occupancy >= self.capacities.packet_pool_packets

    # ----- test controls -----------------------------------------------------------------------

    def drop_next_reply(self, *, command_code: int | None = None, count: int = 1) -> None:
        """The command still takes effect; only its reply frames never reach the host."""
        for _ in range(count):
            self._faults.append(InjectedFault(kind=FaultKind.DROP_REPLY, code=command_code))

    def delay_next_reply(self, delay_seconds: float, *, command_code: int | None = None) -> None:
        """The node is busy before it handles the command, so later commands wait behind it too."""
        self._faults.append(InjectedFault(kind=FaultKind.DELAY_REPLY, code=command_code, delay_seconds=delay_seconds))

    def lose_next_command(self, *, command_code: int | None = None, count: int = 1) -> None:
        for _ in range(count):
            self._faults.append(InjectedFault(kind=FaultKind.LOSE_COMMAND, code=command_code))

    def drop_next_push(self, *, push_code: int | None = None, count: int = 1) -> None:
        for _ in range(count):
            self._faults.append(InjectedFault(kind=FaultKind.DROP_PUSH, code=push_code))

    def occupy_packet_pool(self, slot_count: int) -> None:
        """Holds packet buffers as heavy mesh traffic would; sends then fail with ERR_CODE_TABLE_FULL."""
        self._occupied_packet_pool_slots = slot_count

    def release_packet_pool(self) -> None:
        self._occupied_packet_pool_slots = 0

    def _take_fault(self, kind: FaultKind, code: int) -> InjectedFault | None:
        for fault in self._faults:
            if fault.applies_to(kind, code):
                self._faults.remove(fault)
                return fault
        return None

    # ----- host link ---------------------------------------------------------------------------

    def attach_host_link(self, host_link: NodeHostLink) -> None:
        self._host_link = host_link

    def detach_host_link(self, host_link: NodeHostLink) -> None:
        if self._host_link is host_link:
            self._host_link = None

    @property
    def has_host_link(self) -> bool:
        return self._host_link is not None

    def _drop_host_link(self, reason: str) -> None:
        host_link = self._host_link
        self._host_link = None
        if host_link is not None:
            host_link.node_dropped_link(reason)

    def receive_host_bytes(self, data: bytes) -> None:
        if self.power_state is not PowerState.RUNNING:
            return
        for command_frame in self._host_deframer.feed(data):
            self._accept_command_frame(command_frame)

    def _accept_command_frame(self, command_frame: bytes) -> None:
        if not self._serial_interface_enabled:
            return
        if self._take_fault(FaultKind.LOSE_COMMAND, command_frame[0]) is not None:
            return
        self.command_log.append(
            ReceivedCommand(code=command_frame[0], frame=command_frame, received_at=time.monotonic())
        )
        self._command_frames.put_nowait(command_frame)

    def _write_reply(self, frame: bytes) -> None:
        if self._current_reply_is_dropped:
            return
        self._write_frame_to_host(frame)

    def _write_push(self, frame: bytes) -> None:
        if self._take_fault(FaultKind.DROP_PUSH, frame[0]) is not None:
            return
        self._write_frame_to_host(frame)

    def _write_frame_to_host(self, frame: bytes) -> None:
        if self.power_state is not PowerState.RUNNING or not self._serial_interface_enabled:
            return
        if len(frame) > MAXIMUM_FRAME_BYTES:
            return
        if self._host_link is None:
            self.frames_lost_without_host += 1
            return
        self._host_link.deliver_frame_to_host(frame)

    # ----- main loop ---------------------------------------------------------------------------

    async def _run_main_loop(self) -> None:
        while True:
            try:
                await self._run_main_loop_pass()
            except Exception as internal_error:
                self.internal_errors.append(internal_error)

    async def _run_main_loop_pass(self) -> None:
        command_frame = await self._next_command_frame()
        if command_frame is None:
            self._stream_next_contact()
        else:
            await self._process_command_frame(command_frame)

    async def _next_command_frame(self) -> bytes | None:
        """The next command, or None when a listing is running and its next contact is due."""
        if self._contact_listing is None:
            return await self._command_frames.get()
        if not self._command_frames.empty():
            return self._command_frames.get_nowait()
        await asyncio.sleep(self.timing.contact_listing_step_seconds)
        return None

    async def _process_command_frame(self, command_frame: bytes) -> None:
        boot_generation = self._boot_generation
        delay_fault = self._take_fault(FaultKind.DELAY_REPLY, command_frame[0])
        extra_delay_seconds = 0.0 if delay_fault is None else delay_fault.delay_seconds
        await asyncio.sleep(self.timing.command_processing_seconds + extra_delay_seconds)
        if boot_generation != self._boot_generation or not self._serial_interface_enabled:
            return
        self._current_reply_is_dropped = self._take_fault(FaultKind.DROP_REPLY, command_frame[0]) is not None
        try:
            self._handle_command_frame(command_frame)
        finally:
            self._current_reply_is_dropped = False

    def _handle_command_frame(self, command_frame: bytes) -> None:
        command_handler = self._command_handlers.get(command_frame[0])
        if command_handler is None or len(command_frame) < command_handler.minimum_frame_length:
            self._reply_error(FirmwareErrorCode.UNSUPPORTED_COMMAND)
            return
        command_handler.handle(command_frame)

    def _reply_ok(self) -> None:
        self._write_reply(ok_frame())

    def _reply_error(self, error_code: FirmwareErrorCode) -> None:
        self._write_reply(error_frame(error_code))

    def _record_protocol_violation(self, description: str) -> None:
        self.protocol_violations.append(description)

    def _build_command_handlers(self) -> dict[int, CommandHandler]:
        return {
            CommandCode.DEVICE_QUERY: CommandHandler(minimum_frame_length=2, handle=self._handle_device_query),
            CommandCode.APP_START: CommandHandler(minimum_frame_length=8, handle=self._handle_app_start),
            CommandCode.SEND_TEXT_MESSAGE: CommandHandler(
                minimum_frame_length=14, handle=self._handle_send_text_message
            ),
            CommandCode.SEND_CHANNEL_TEXT_MESSAGE: CommandHandler(
                minimum_frame_length=7, handle=self._handle_send_channel_text_message
            ),
            CommandCode.GET_CONTACTS: CommandHandler(minimum_frame_length=1, handle=self._handle_get_contacts),
            CommandCode.GET_DEVICE_TIME: CommandHandler(minimum_frame_length=1, handle=self._handle_get_device_time),
            CommandCode.SET_DEVICE_TIME: CommandHandler(minimum_frame_length=5, handle=self._handle_set_device_time),
            CommandCode.SEND_SELF_ADVERT: CommandHandler(minimum_frame_length=1, handle=self._handle_send_self_advert),
            CommandCode.SET_ADVERT_NAME: CommandHandler(minimum_frame_length=2, handle=self._handle_set_advert_name),
            CommandCode.SET_ADVERT_LATITUDE_LONGITUDE: CommandHandler(
                minimum_frame_length=9, handle=self._handle_set_advert_latitude_longitude
            ),
            CommandCode.RESET_PATH: CommandHandler(
                minimum_frame_length=1 + PUBLIC_KEY_BYTES, handle=self._handle_reset_path
            ),
            CommandCode.ADD_UPDATE_CONTACT: CommandHandler(
                minimum_frame_length=1 + PUBLIC_KEY_BYTES + 3, handle=self._handle_add_update_contact
            ),
            CommandCode.REMOVE_CONTACT: CommandHandler(minimum_frame_length=1, handle=self._handle_remove_contact),
            CommandCode.GET_CONTACT_BY_KEY: CommandHandler(
                minimum_frame_length=1, handle=self._handle_get_contact_by_key
            ),
            CommandCode.EXPORT_CONTACT: CommandHandler(minimum_frame_length=1, handle=self._handle_export_contact),
            CommandCode.IMPORT_CONTACT: CommandHandler(
                minimum_frame_length=IMPORT_CONTACT_MINIMUM_BYTES, handle=self._handle_import_contact
            ),
            CommandCode.SYNC_NEXT_MESSAGE: CommandHandler(
                minimum_frame_length=1, handle=self._handle_sync_next_message
            ),
            CommandCode.SET_RADIO_PARAMETERS: CommandHandler(
                minimum_frame_length=1, handle=self._handle_set_radio_parameters
            ),
            CommandCode.SET_RADIO_TRANSMIT_POWER: CommandHandler(
                minimum_frame_length=1, handle=self._handle_set_radio_transmit_power
            ),
            CommandCode.SET_TUNING_PARAMETERS: CommandHandler(
                minimum_frame_length=1, handle=self._handle_set_tuning_parameters
            ),
            CommandCode.GET_TUNING_PARAMETERS: CommandHandler(
                minimum_frame_length=1, handle=self._handle_get_tuning_parameters
            ),
            CommandCode.SET_OTHER_PARAMETERS: CommandHandler(
                minimum_frame_length=1, handle=self._handle_set_other_parameters
            ),
            CommandCode.SET_PATH_HASH_MODE: CommandHandler(
                minimum_frame_length=3, handle=self._handle_set_path_hash_mode
            ),
            CommandCode.REBOOT: CommandHandler(minimum_frame_length=1, handle=self._handle_reboot),
            CommandCode.FACTORY_RESET: CommandHandler(minimum_frame_length=1, handle=self._handle_factory_reset),
            CommandCode.GET_BATTERY_AND_STORAGE: CommandHandler(
                minimum_frame_length=1, handle=self._handle_get_battery_and_storage
            ),
            CommandCode.GET_CHANNEL: CommandHandler(minimum_frame_length=2, handle=self._handle_get_channel),
            CommandCode.SET_CHANNEL: CommandHandler(
                minimum_frame_length=SET_CHANNEL_BYTES, handle=self._handle_set_channel
            ),
            CommandCode.SET_AUTO_ADD_CONFIGURATION: CommandHandler(
                minimum_frame_length=1, handle=self._handle_set_auto_add_configuration
            ),
            CommandCode.GET_AUTO_ADD_CONFIGURATION: CommandHandler(
                minimum_frame_length=1, handle=self._handle_get_auto_add_configuration
            ),
            CommandCode.EXPORT_PRIVATE_KEY: CommandHandler(
                minimum_frame_length=1, handle=self._handle_export_private_key
            ),
            CommandCode.IMPORT_PRIVATE_KEY: CommandHandler(
                minimum_frame_length=IMPORT_PRIVATE_KEY_MINIMUM_BYTES, handle=self._handle_import_private_key
            ),
        }

    # ----- command handlers: session and device ------------------------------------------------

    def _handle_device_query(self, command_frame: bytes) -> None:
        self.record_app_protocol_version(command_frame[1])
        self._write_reply(
            device_info_frame(
                protocol_version_code=self.build.protocol_version_code,
                maximum_contacts=self.capacities.maximum_contacts,
                maximum_group_channels=self.capacities.group_channels,
                bluetooth_pin=self.preferences.bluetooth_pin,
                build_date=self.build.build_date,
                manufacturer_name=self.build.manufacturer_name,
                firmware_version=self.build.firmware_version,
                client_repeat_enabled=self.preferences.client_repeat_enabled,
                path_hash_mode=self.preferences.path_hash_mode,
            )
        )

    def _handle_app_start(self, _command_frame: bytes) -> None:
        self._contact_listing = None
        preferences = self.preferences
        self._write_reply(
            self_info_frame(
                transmit_power_dbm=preferences.transmit_power_dbm,
                maximum_transmit_power_dbm=self.build.maximum_transmit_power_dbm,
                public_key=self.public_key,
                latitude_microdegrees=preferences.latitude_microdegrees,
                longitude_microdegrees=preferences.longitude_microdegrees,
                multi_acknowledgements=preferences.multi_acknowledgements,
                advert_location_policy=preferences.advert_location_policy,
                telemetry_modes=preferences.telemetry_modes,
                manual_add_contacts=preferences.manual_add_contacts,
                frequency_kilohertz=preferences.frequency_kilohertz,
                bandwidth_hertz=preferences.bandwidth_hertz,
                spreading_factor=preferences.spreading_factor,
                coding_rate=preferences.coding_rate,
                node_name=preferences.node_name,
            )
        )

    def _handle_get_device_time(self, _command_frame: bytes) -> None:
        self._write_reply(current_time_frame(self.clock_time()))

    def _handle_set_device_time(self, command_frame: bytes) -> None:
        if self.set_clock_time(decode_unsigned_32(command_frame, 1)):
            self._reply_ok()
        else:
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)

    def _handle_get_battery_and_storage(self, _command_frame: bytes) -> None:
        self._write_reply(
            battery_and_storage_frame(
                battery_millivolts=self.build.battery_millivolts,
                used_kilobytes=self.build.storage_used_kilobytes,
                total_kilobytes=self.build.storage_total_kilobytes,
            )
        )

    def _handle_reboot(self, command_frame: bytes) -> None:
        if command_frame[1 : 1 + len(REBOOT_COMMAND_SUFFIX)] != REBOOT_COMMAND_SUFFIX:
            self._reply_error(FirmwareErrorCode.UNSUPPORTED_COMMAND)
            return
        self.reboot()

    def _handle_factory_reset(self, command_frame: bytes) -> None:
        if command_frame[1 : 1 + len(FACTORY_RESET_COMMAND_SUFFIX)] != FACTORY_RESET_COMMAND_SUFFIX:
            self._reply_error(FirmwareErrorCode.UNSUPPORTED_COMMAND)
            return
        self.factory_reset()

    def _handle_export_private_key(self, _command_frame: bytes) -> None:
        if not self.private_key_export_enabled:
            self._write_reply(disabled_frame())
            return
        exported_private_key = self.exported_private_key_override or self._identity.expanded_private_key
        self._write_reply(private_key_frame(exported_private_key))

    def _handle_import_private_key(self, command_frame: bytes) -> None:
        """LocalIdentity::validatePrivateKey, then saveMainIdentity: the identity changes at once and for good.

        The firmware then reloads its contacts to drop the key-exchange secrets of the old identity;
        the fake keeps no such secrets, so its contacts stay as they are.
        """
        if not self.private_key_import_enabled:
            self._write_reply(disabled_frame())
            return
        imported_private_key = command_frame[1:IMPORT_PRIVATE_KEY_MINIMUM_BYTES]
        imported_identity = NodeIdentity(imported_private_key)
        if imported_identity.public_key[0] in RESERVED_PUBLIC_KEY_FIRST_BYTES or not is_clamped_scalar(
            imported_private_key
        ):
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        if self.private_key_import_save_fails:
            self._reply_error(FirmwareErrorCode.FILE_INPUT_OUTPUT_ERROR)
            return
        self._identity = imported_identity
        self._reply_ok()

    # ----- command handlers: settings ----------------------------------------------------------

    def _handle_set_advert_name(self, command_frame: bytes) -> None:
        self.preferences.node_name = text_before_first_nul(command_frame[1 : 1 + NODE_NAME_MAXIMUM_BYTES])
        self._reply_ok()

    def _handle_set_advert_latitude_longitude(self, command_frame: bytes) -> None:
        latitude_microdegrees = decode_signed_32(command_frame, 1)
        longitude_microdegrees = decode_signed_32(command_frame, 5)
        if (
            abs(latitude_microdegrees) > MAXIMUM_LATITUDE_MICRODEGREES
            or abs(longitude_microdegrees) > MAXIMUM_LONGITUDE_MICRODEGREES
        ):
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.latitude_microdegrees = latitude_microdegrees
        self.preferences.longitude_microdegrees = longitude_microdegrees
        self._reply_ok()

    def _handle_set_radio_parameters(self, command_frame: bytes) -> None:
        if len(command_frame) < RADIO_PARAMETERS_WITHOUT_REPEAT_BYTES:
            self._record_protocol_violation(
                f"SET_RADIO_PARAMS of {len(command_frame)} bytes: the firmware reads 11 bytes whatever the length"
            )
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        frequency_kilohertz = decode_unsigned_32(command_frame, 1)
        bandwidth_hertz = decode_unsigned_32(command_frame, 5)
        spreading_factor = command_frame[9]
        coding_rate = command_frame[10]
        client_repeat = command_frame[11] if len(command_frame) > RADIO_PARAMETERS_WITHOUT_REPEAT_BYTES else 0
        if client_repeat and frequency_kilohertz not in CLIENT_REPEAT_FREQUENCIES_KILOHERTZ:
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        radio_parameters_are_valid = (
            MINIMUM_FREQUENCY_KILOHERTZ <= frequency_kilohertz <= MAXIMUM_FREQUENCY_KILOHERTZ
            and MINIMUM_BANDWIDTH_HERTZ <= bandwidth_hertz <= MAXIMUM_BANDWIDTH_HERTZ
            and spreading_factor in SPREADING_FACTOR_RANGE
            and coding_rate in CODING_RATE_RANGE
        )
        if not radio_parameters_are_valid:
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.store_radio_parameters(
            frequency_kilohertz=frequency_kilohertz,
            bandwidth_hertz=bandwidth_hertz,
            spreading_factor=spreading_factor,
            coding_rate=coding_rate,
        )
        self.preferences.client_repeat_enabled = client_repeat != 0
        self._reply_ok()

    def _handle_set_radio_transmit_power(self, command_frame: bytes) -> None:
        if len(command_frame) < 2:
            self._record_protocol_violation("SET_RADIO_TX_POWER without its power byte")
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        transmit_power_dbm = int.from_bytes(command_frame[1:2], "little", signed=True)
        if not MINIMUM_TRANSMIT_POWER_DBM <= transmit_power_dbm <= self.build.maximum_transmit_power_dbm:
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.transmit_power_dbm = transmit_power_dbm
        self._reply_ok()

    def _handle_set_tuning_parameters(self, command_frame: bytes) -> None:
        if len(command_frame) < TUNING_PARAMETERS_BYTES:
            self._record_protocol_violation("SET_TUNING_PARAMS shorter than 9 bytes")
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.receive_delay_base_thousandths = decode_unsigned_32(command_frame, 1)
        self.preferences.airtime_factor_thousandths = decode_unsigned_32(command_frame, 5)
        self._reply_ok()

    def _handle_get_tuning_parameters(self, _command_frame: bytes) -> None:
        self._write_reply(
            tuning_parameters_frame(
                receive_delay_base_thousandths=self.preferences.receive_delay_base_thousandths,
                airtime_factor_thousandths=self.preferences.airtime_factor_thousandths,
            )
        )

    def _handle_set_other_parameters(self, command_frame: bytes) -> None:
        """Bytes after the manual-add flag are optional, each only if all before it are present."""
        if len(command_frame) < 2:
            self._record_protocol_violation("SET_OTHER_PARAMS without its manual-add byte")
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.manual_add_contacts = command_frame[1]
        if len(command_frame) >= 3:
            self.preferences.store_telemetry_modes(command_frame[2])
        if len(command_frame) >= 4:
            self.preferences.advert_location_policy = command_frame[3]
        if len(command_frame) >= 5:
            self.preferences.multi_acknowledgements = command_frame[4]
        self._reply_ok()

    def _handle_set_path_hash_mode(self, command_frame: bytes) -> None:
        if command_frame[1] != 0:
            self._reply_error(FirmwareErrorCode.UNSUPPORTED_COMMAND)
            return
        if command_frame[2] > MAXIMUM_PATH_HASH_MODE:
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.path_hash_mode = command_frame[2]
        self._reply_ok()

    def _handle_set_auto_add_configuration(self, command_frame: bytes) -> None:
        if len(command_frame) < 2:
            self._record_protocol_violation("SET_AUTOADD_CONFIG without its configuration byte")
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        self.preferences.auto_add_configuration = command_frame[1]
        if len(command_frame) >= 3:
            self.preferences.auto_add_maximum_hops = min(command_frame[2], MAXIMUM_AUTO_ADD_HOPS)
        self._reply_ok()

    def _handle_get_auto_add_configuration(self, _command_frame: bytes) -> None:
        self._write_reply(
            auto_add_configuration_frame(
                auto_add_configuration=self.preferences.auto_add_configuration,
                auto_add_maximum_hops=self.preferences.auto_add_maximum_hops,
            )
        )

    def _handle_get_channel(self, command_frame: bytes) -> None:
        channel_slot = self._channels.get(command_frame[1])
        if channel_slot is None:
            self._reply_error(FirmwareErrorCode.NOT_FOUND)
            return
        self._write_reply(
            channel_info_frame(
                channel_index=command_frame[1], channel_name=channel_slot.name, channel_secret=channel_slot.secret
            )
        )

    def _handle_set_channel(self, command_frame: bytes) -> None:
        if len(command_frame) >= SET_CHANNEL_WITH_LONG_SECRET_BYTES:
            self._reply_error(FirmwareErrorCode.UNSUPPORTED_COMMAND)
            return
        channel_name = text_before_first_nul(command_frame[2 : 2 + CHANNEL_NAME_FIELD_BYTES])
        channel_secret = command_frame[2 + CHANNEL_NAME_FIELD_BYTES : SET_CHANNEL_BYTES]
        channel_slot = ChannelSlot(name=channel_name[: CHANNEL_NAME_FIELD_BYTES - 1], secret=channel_secret)
        if self._channels.set(command_frame[1], channel_slot):
            self._reply_ok()
        else:
            self._reply_error(FirmwareErrorCode.NOT_FOUND)

    # ----- command handlers: contacts ----------------------------------------------------------

    def _handle_get_contacts(self, command_frame: bytes) -> None:
        if self._contact_listing is not None:
            self._reply_error(FirmwareErrorCode.BAD_STATE)
            return
        since = decode_unsigned_32(command_frame, 1) if len(command_frame) >= 5 else 0
        self._write_reply(contacts_start_frame(len(self._contacts)))
        self._contact_listing = ContactListing(since=since, replies_are_dropped=self._current_reply_is_dropped)

    def _stream_next_contact(self) -> None:
        """One step of the contacts iterator; it reads the live table, so changes show mid-listing."""
        contact_listing = self._contact_listing
        if contact_listing is None:
            return
        record = self._contacts.record_at(contact_listing.next_index)
        if record is None:
            self._contact_listing = None
            self._write_listing_frame(contact_listing, end_of_contacts_frame(contact_listing.most_recent_last_modified))
            return
        contact_listing.next_index += 1
        if record.last_modified > contact_listing.since:
            self._write_listing_frame(contact_listing, record.to_frame(ResponseCode.CONTACT))
            contact_listing.most_recent_last_modified = max(
                contact_listing.most_recent_last_modified, record.last_modified
            )

    def _write_listing_frame(self, contact_listing: ContactListing, frame: bytes) -> None:
        if not contact_listing.replies_are_dropped:
            self._write_frame_to_host(frame)

    def _handle_reset_path(self, command_frame: bytes) -> None:
        if self.reset_route(command_frame[1 : 1 + PUBLIC_KEY_BYTES]):
            self._reply_ok()
        else:
            self._reply_error(FirmwareErrorCode.NOT_FOUND)

    def _handle_add_update_contact(self, command_frame: bytes) -> None:
        if len(command_frame) < ADD_UPDATE_CONTACT_WITH_LOCATION_BYTES:
            self._record_protocol_violation(
                f"ADD_UPDATE_CONTACT of {len(command_frame)} bytes: the firmware reads 136 bytes whatever "
                "the length, and a new contact gets garbage coordinates below 144"
            )
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        record = ContactRecord.from_add_update_frame(command_frame, fallback_last_modified=self.clock_time())
        if self.add_or_update_contact(record).added:
            self._reply_ok()
        else:
            self._reply_error(FirmwareErrorCode.TABLE_FULL)

    def _handle_remove_contact(self, command_frame: bytes) -> None:
        if len(command_frame) < 1 + PUBLIC_KEY_BYTES:
            self._record_protocol_violation("REMOVE_CONTACT shorter than a full public key")
            self._reply_error(FirmwareErrorCode.NOT_FOUND)
            return
        if self._contacts.remove(command_frame[1 : 1 + PUBLIC_KEY_BYTES]):
            self._reply_ok()
        else:
            self._reply_error(FirmwareErrorCode.NOT_FOUND)

    def _handle_get_contact_by_key(self, command_frame: bytes) -> None:
        record = self._contacts.find_by_public_key(command_frame[1 : 1 + PUBLIC_KEY_BYTES])
        if record is None:
            self._reply_error(FirmwareErrorCode.NOT_FOUND)
            return
        self._write_reply(record.to_frame(ResponseCode.CONTACT))

    def _handle_export_contact(self, command_frame: bytes) -> None:
        if len(command_frame) < 1 + PUBLIC_KEY_BYTES:
            if self._packet_pool_is_full():
                self._reply_error(FirmwareErrorCode.TABLE_FULL)
                return
            self._write_reply(export_contact_frame(self.export_self_card()))
            return
        public_key = command_frame[1 : 1 + PUBLIC_KEY_BYTES]
        advert_blob = self._advert_blobs.get(public_key)
        if self._contacts.find_by_public_key(public_key) is None or advert_blob is None:
            self._reply_error(FirmwareErrorCode.NOT_FOUND)
            return
        self._write_reply(export_contact_frame(advert_blob))

    def export_self_card(self) -> bytes:
        """A fresh advert signed now with the node's clock, as CMD_EXPORT_CONTACT returns it."""
        return build_self_card(
            identity=self._identity,
            timestamp=self.clock_time(),
            node_type=NodeType.CHAT,
            name=self.preferences.node_name,
            location=self._advert_location(),
        )

    def _advert_location(self) -> AdvertLocation | None:
        if self.preferences.advert_location_policy == 0:
            return None
        return AdvertLocation(
            latitude_microdegrees=self.preferences.latitude_microdegrees,
            longitude_microdegrees=self.preferences.longitude_microdegrees,
        )

    def _handle_import_contact(self, command_frame: bytes) -> None:
        """OK means "accepted for processing": the card is verified and handled on a later pass."""
        envelope = read_advert_packet_envelope(command_frame[1:])
        if envelope is None or self._packet_pool_is_full():
            self._reply_error(FirmwareErrorCode.ILLEGAL_ARGUMENT)
            return
        imported_packet = AdvertPacket(
            sender_public_key=envelope.advert_payload[:PUBLIC_KEY_BYTES],
            route=PacketRoute.flood(path_hash_size=1),
            advert_payload=envelope.advert_payload,
        )
        self._seen_packets.forget(imported_packet.packet_hash)
        self._reply_ok()
        arrival = PacketArrival.flood(path=envelope.path, path_hash_size=decode_path_hash_size(envelope.path_length))
        self._schedule_for_this_boot(
            self.timing.imported_contact_processing_seconds,
            lambda: self._process_imported_advert(imported_packet, arrival),
        )

    def _process_imported_advert(self, imported_packet: AdvertPacket, arrival: PacketArrival) -> None:
        """The card is handled as if the radio had heard it: verified, then stored or reported."""
        self.imported_card_outcomes.append(
            self._receive_advert_contents(imported_packet, arrival, bypasses_deduplication=False)
        )

    # ----- command handlers: messages ----------------------------------------------------------

    def _handle_send_text_message(self, command_frame: bytes) -> None:
        text_type = command_frame[1]
        send_result = self.send_text_message(
            text_type=text_type,
            attempt=command_frame[2],
            sender_timestamp=decode_unsigned_32(command_frame, 3),
            recipient_public_key_prefix=command_frame[7 : 7 + PUBLIC_KEY_PREFIX_BYTES],
            text=command_frame[7 + PUBLIC_KEY_PREFIX_BYTES :],
        )
        if isinstance(send_result, TextMessageRejected):
            self._reply_error(send_result.error_code)
            return
        reply = message_sent_frame(
            sent_by_flood=send_result.sent_by_flood,
            expected_acknowledgement=send_result.expected_acknowledgement,
            suggested_timeout_milliseconds=send_result.suggested_timeout_milliseconds,
        )
        holds_reply = self.message_sent_reply_order is MessageSentReplyOrder.AFTER_ACKNOWLEDGEMENT
        if holds_reply and text_type == TextType.PLAIN:
            self._hold_message_sent_reply(send_result.expected_acknowledgement, reply)
        else:
            self._write_reply(reply)

    def send_text_message(
        self,
        *,
        text_type: int,
        attempt: int,
        sender_timestamp: int,
        recipient_public_key_prefix: bytes,
        text: bytes,
    ) -> SendTextMessageResult:
        """CMD_SEND_TXT_MSG: queue a direct message for the radio, or say why not."""
        recipient = self._contacts.find_by_prefix(recipient_public_key_prefix)
        if recipient is None:
            return TextMessageRejected(error_code=FirmwareErrorCode.NOT_FOUND)
        if text_type not in (TextType.PLAIN, TextType.COMMAND_LINE_DATA):
            return TextMessageRejected(error_code=FirmwareErrorCode.UNSUPPORTED_COMMAND)
        message_text = text_before_first_nul(text)
        if text_type == TextType.COMMAND_LINE_DATA:
            sender_timestamp = self._clock.current_time_unique()
        if not self._text_fits_one_packet(message_text, attempt) or self._packet_pool_is_full():
            return TextMessageRejected(error_code=FirmwareErrorCode.TABLE_FULL)
        packet = DirectMessagePacket(
            sender_public_key=self.public_key,
            route=self._route_to(recipient),
            destination_public_key=recipient.public_key,
            sender_timestamp=sender_timestamp,
            attempt=attempt,
            text_type=text_type,
            text=message_text,
        )
        self._queue_transmission(packet)
        expected_acknowledgement = bytes(ACKNOWLEDGEMENT_CODE_BYTES)
        if text_type == TextType.PLAIN:
            expected_acknowledgement = calculate_expected_acknowledgement(
                sender_timestamp=sender_timestamp, attempt=attempt, text=message_text, sender_public_key=self.public_key
            )
            self._expected_acknowledgements.add(
                ExpectedAcknowledgement(
                    code=expected_acknowledgement,
                    sent_at=time.monotonic(),
                    contact_public_key=recipient.public_key,
                )
            )
        return TextMessageQueued(
            sent_by_flood=not recipient.has_known_route,
            expected_acknowledgement=expected_acknowledgement,
            suggested_timeout_milliseconds=self._suggested_timeout_milliseconds(recipient),
        )

    @staticmethod
    def _text_fits_one_packet(text: bytes, attempt: int) -> bool:
        if len(text) > MAXIMUM_DIRECT_MESSAGE_TEXT_BYTES:
            return False
        return attempt <= HIGHEST_ATTEMPT_WITHOUT_EXTENSION or (
            len(text) <= MAXIMUM_DIRECT_MESSAGE_TEXT_BYTES_WITH_EXTENDED_ATTEMPT
        )

    def _route_to(self, contact: ContactRecord) -> PacketRoute:
        if not contact.has_known_route:
            return PacketRoute.flood(path_hash_size=self.preferences.path_hash_mode + 1)
        return PacketRoute.direct(path=contact.route_path, path_hash_size=contact.path_hash_size)

    def _suggested_timeout_milliseconds(self, contact: ContactRecord) -> int:
        if not contact.has_known_route:
            return self.timing.flood_suggested_timeout_milliseconds
        return self.timing.direct_suggested_timeout_milliseconds(contact.hop_count)

    def _hold_message_sent_reply(self, expected_acknowledgement: bytes, reply: bytes) -> None:
        if self._current_reply_is_dropped:
            return
        timer = self._schedule(
            self.timing.held_message_sent_reply_limit_seconds,
            lambda: self._release_held_message_sent_reply(expected_acknowledgement),
        )
        self._held_message_sent_replies[expected_acknowledgement] = HeldMessageSentReply(frame=reply, timer=timer)

    def _release_held_message_sent_reply(self, expected_acknowledgement: bytes) -> None:
        held_reply = self._held_message_sent_replies.pop(expected_acknowledgement, None)
        if held_reply is None:
            return
        held_reply.timer.cancel()
        self._timers.discard(held_reply.timer)
        self._write_frame_to_host(held_reply.frame)

    def _handle_send_channel_text_message(self, command_frame: bytes) -> None:
        if command_frame[1] != TextType.PLAIN:
            self._reply_error(FirmwareErrorCode.UNSUPPORTED_COMMAND)
            return
        channel_slot = self._channels.get(command_frame[2])
        if channel_slot is None or self._packet_pool_is_full():
            self._reply_error(FirmwareErrorCode.NOT_FOUND)
            return
        self._queue_transmission(
            ChannelMessagePacket(
                sender_public_key=self.public_key,
                route=PacketRoute.flood(path_hash_size=self.preferences.path_hash_mode + 1),
                channel_secret=channel_slot.secret,
                sender_timestamp=decode_unsigned_32(command_frame, 3),
                text=self.preferences.node_name + b": " + command_frame[7:],
            )
        )
        self._reply_ok()

    def _handle_sync_next_message(self, _command_frame: bytes) -> None:
        queued_frame = self.pop_offline_frame()
        self._write_reply(no_more_messages_frame() if queued_frame is None else queued_frame)

    def _handle_send_self_advert(self, command_frame: bytes) -> None:
        sends_by_flood = len(command_frame) >= 2 and command_frame[1] == 1
        if self.send_self_advert(flood=sends_by_flood):
            self._reply_ok()
        else:
            self._reply_error(FirmwareErrorCode.TABLE_FULL)

    def send_self_advert(self, *, flood: bool) -> bool:
        """CMD_SEND_SELF_ADVERT: zero hop reaches only neighbours; a flood reaches everyone."""
        if self._packet_pool_is_full():
            return False
        app_data = build_advert_app_data(
            node_type=NodeType.CHAT, name=self.preferences.node_name, location=self._advert_location()
        )
        route = (
            PacketRoute.flood(path_hash_size=self.preferences.path_hash_mode + 1) if flood else PacketRoute.zero_hop()
        )
        self._queue_transmission(
            AdvertPacket(
                sender_public_key=self.public_key,
                route=route,
                advert_payload=build_advert_payload(
                    identity=self._identity, timestamp=self.clock_time(), app_data=app_data
                ),
            )
        )
        return True

    # ----- radio: transmitting -----------------------------------------------------------------

    def attach_radio(self, radio: RadioMedium | None) -> None:
        self._radio = radio

    def _queue_transmission(self, packet: RadioPacket, *, delay_seconds: float = 0.0) -> None:
        heapq.heappush(
            self._transmit_queue,
            ScheduledTransmission(
                due_at=time.monotonic() + delay_seconds,
                sequence=next(self._transmission_sequence),
                packet=packet,
            ),
        )
        self._transmit_wakeup.set()

    async def _run_transmitter(self) -> None:
        while True:
            try:
                await self._transmit_next_packet()
            except Exception as internal_error:
                self.internal_errors.append(internal_error)

    async def _transmit_next_packet(self) -> None:
        packet = await self._wait_for_due_transmission()
        boot_generation = self._boot_generation
        self._packet_is_on_air = True
        try:
            await asyncio.sleep(self.timing.transmit_seconds_per_packet)
        finally:
            self._packet_is_on_air = False
        if boot_generation == self._boot_generation and self.power_state is PowerState.RUNNING:
            self._hand_packet_to_radio(packet)

    async def _wait_for_due_transmission(self) -> RadioPacket:
        while True:
            if not self._transmit_queue:
                self._transmit_wakeup.clear()
                await self._transmit_wakeup.wait()
                continue
            seconds_until_due = self._transmit_queue[0].due_at - time.monotonic()
            if seconds_until_due <= 0:
                return heapq.heappop(self._transmit_queue).packet
            self._transmit_wakeup.clear()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(seconds_until_due):
                    await self._transmit_wakeup.wait()

    def _hand_packet_to_radio(self, packet: RadioPacket) -> None:
        self.transmitted_packets.append(packet)
        if self._radio is not None:
            self._radio.transmit(self, packet)

    # ----- radio: receiving --------------------------------------------------------------------

    def receive_radio_packet(
        self, packet: RadioPacket, arrival: PacketArrival, *, bypasses_deduplication: bool = False
    ) -> ReceptionOutcome:
        """A packet the radio heard. `bypasses_deduplication` models a copy that arrives after the
        160-entry packet-hash ring has forgotten the original."""
        if self.power_state is not PowerState.RUNNING:
            return ReceptionOutcome.NODE_NOT_RUNNING
        self._push_receive_log(packet, arrival)
        if self._packet_pool_is_full():
            return ReceptionOutcome.PACKET_POOL_FULL
        match packet:
            case DirectMessagePacket():
                return self._receive_direct_message(packet, arrival, bypasses_deduplication=bypasses_deduplication)
            case AcknowledgementPacket():
                return self._receive_acknowledgement(packet, arrival, bypasses_deduplication=bypasses_deduplication)
            case PathReturnPacket():
                return self._receive_path_return(packet, arrival, bypasses_deduplication=bypasses_deduplication)
            case AdvertPacket():
                return self._receive_advert_contents(packet, arrival, bypasses_deduplication=bypasses_deduplication)
            case ChannelMessagePacket():
                return self._receive_channel_message(packet, arrival, bypasses_deduplication=bypasses_deduplication)
            case ChannelDataPacket():
                return self._receive_channel_data(packet, arrival, bypasses_deduplication=bypasses_deduplication)
        return ReceptionOutcome.NOT_ADDRESSED_TO_THIS_NODE

    def _push_receive_log(self, packet: RadioPacket, arrival: PacketArrival) -> None:
        if not self.receive_log_pushes_enabled:
            return
        raw_packet = packet.raw_packet(arrival)
        if len(raw_packet) + 3 > MAXIMUM_FRAME_BYTES:
            return
        self._write_push(
            receive_log_push(
                signal_to_noise_quarters=self._draw_signal_to_noise_quarters(),
                received_signal_strength=self._random.randint(*RECEIVED_SIGNAL_STRENGTH_RANGE),
                raw_packet=raw_packet,
            )
        )

    def _draw_signal_to_noise_quarters(self) -> int:
        return self._random.randint(*SIGNAL_TO_NOISE_QUARTERS_RANGE)

    def _is_repeat(self, packet: RadioPacket, *, bypasses_deduplication: bool) -> bool:
        packet_hash = packet.packet_hash
        if self._seen_packets.was_seen(packet_hash) and not bypasses_deduplication:
            return True
        self._seen_packets.mark_seen(packet_hash)
        return False

    def _receive_direct_message(
        self, packet: DirectMessagePacket, arrival: PacketArrival, *, bypasses_deduplication: bool
    ) -> ReceptionOutcome:
        if self._is_repeat(packet, bypasses_deduplication=bypasses_deduplication):
            return ReceptionOutcome.ALREADY_SEEN
        if packet.destination_public_key != self.public_key:
            return ReceptionOutcome.NOT_ADDRESSED_TO_THIS_NODE
        sender = self._contacts.find_by_public_key(packet.sender_public_key)
        if sender is None:
            return ReceptionOutcome.UNKNOWN_SENDER
        if packet.text_type == TextType.PLAIN:
            self._accept_plain_text_message(sender, packet, arrival)
            return ReceptionOutcome.ACCEPTED
        if packet.text_type == TextType.COMMAND_LINE_DATA:
            self._queue_received_message(sender, packet, arrival)
            if arrival.arrived_by_flood:
                self._send_path_return(recipient=sender, arrival=arrival, acknowledgement=None)
            return ReceptionOutcome.ACCEPTED
        return ReceptionOutcome.UNSUPPORTED_TEXT_TYPE

    def _accept_plain_text_message(
        self, sender: ContactRecord, packet: DirectMessagePacket, arrival: PacketArrival
    ) -> None:
        """Queue it for the app, then acknowledge it, even when the queue had to drop it."""
        self._contacts.replace(dataclasses.replace(sender, last_modified=self.clock_time()))
        self._queue_received_message(sender, packet, arrival)
        acknowledgement = AcknowledgementPayload(
            code=calculate_expected_acknowledgement(
                sender_timestamp=packet.sender_timestamp,
                attempt=packet.attempt,
                text=packet.text,
                sender_public_key=sender.public_key,
            ),
            attempt_byte=packet.attempt if packet.attempt > HIGHEST_ATTEMPT_WITHOUT_EXTENSION else 0,
            random_byte=self._random.randrange(256),
        )
        if arrival.arrived_by_flood:
            self._send_path_return(recipient=sender, arrival=arrival, acknowledgement=acknowledgement)
        else:
            self._send_acknowledgement_to(sender, acknowledgement)

    def _queue_received_message(
        self, sender: ContactRecord, packet: DirectMessagePacket, arrival: PacketArrival
    ) -> None:
        self._add_to_offline_queue(
            contact_message_frame(
                uses_version_3_layout=self._app_target_version >= VERSION_3_APP_TARGET,
                signal_to_noise_quarters=self._draw_signal_to_noise_quarters(),
                sender_public_key_prefix=sender.public_key[:PUBLIC_KEY_PREFIX_BYTES],
                path_length=arrival.path_length,
                text_type=packet.text_type,
                sender_timestamp=packet.sender_timestamp,
                text=packet.text,
            )
        )

    def _add_to_offline_queue(self, frame: bytes) -> None:
        """MESSAGES_WAITING is pushed for every received message, also one the full queue dropped."""
        self._offline_queue.add(frame)
        self._write_push(messages_waiting_push())

    def _send_path_return(
        self, *, recipient: ContactRecord, arrival: PacketArrival, acknowledgement: AcknowledgementPayload | None
    ) -> None:
        """Answer a flood with the path it took (and the ACK), flooded back to the sender."""
        self._queue_transmission(
            PathReturnPacket(
                sender_public_key=self.public_key,
                route=PacketRoute.flood(path_hash_size=self.preferences.path_hash_mode + 1),
                destination_public_key=recipient.public_key,
                returned_path=arrival.path,
                returned_path_hash_size=arrival.path_hash_size,
                acknowledgement=acknowledgement,
            ),
            delay_seconds=self.timing.acknowledgement_delay_seconds,
        )

    def _send_acknowledgement_to(self, contact: ContactRecord, acknowledgement: AcknowledgementPayload) -> None:
        """BaseChatMesh::sendAckTo: flood without a route, else direct, preceded by a multipart copy
        when multi_acks is set."""
        delay_seconds = self.timing.acknowledgement_delay_seconds
        if not contact.has_known_route:
            self._queue_transmission(
                AcknowledgementPacket(
                    sender_public_key=self.public_key,
                    route=PacketRoute.flood(path_hash_size=self.preferences.path_hash_mode + 1),
                    acknowledged_sender_public_key=contact.public_key,
                    acknowledgement=acknowledgement,
                ),
                delay_seconds=delay_seconds,
            )
            return
        direct_route = PacketRoute.direct(path=contact.route_path, path_hash_size=contact.path_hash_size)
        if self.preferences.multi_acknowledgements > 0:
            self._queue_transmission(
                MultipartAcknowledgementPacket(
                    sender_public_key=self.public_key,
                    route=direct_route,
                    acknowledged_sender_public_key=contact.public_key,
                    acknowledgement=acknowledgement,
                ),
                delay_seconds=delay_seconds,
            )
            delay_seconds += self.timing.multipart_acknowledgement_spacing_seconds
        self._queue_transmission(
            AcknowledgementPacket(
                sender_public_key=self.public_key,
                route=direct_route,
                acknowledged_sender_public_key=contact.public_key,
                acknowledgement=acknowledgement,
            ),
            delay_seconds=delay_seconds,
        )

    def _receive_acknowledgement(
        self, packet: AcknowledgementPacket, arrival: PacketArrival, *, bypasses_deduplication: bool
    ) -> ReceptionOutcome:
        if self._is_repeat(packet, bypasses_deduplication=bypasses_deduplication):
            return ReceptionOutcome.ALREADY_SEEN
        matched_acknowledgement = self._process_acknowledgement(packet.acknowledgement.code)
        if matched_acknowledgement is None:
            return ReceptionOutcome.ACKNOWLEDGEMENT_NOT_EXPECTED
        if arrival.arrived_by_flood:
            contact = self._contacts.find_by_public_key(matched_acknowledgement.contact_public_key)
            if contact is not None and contact.has_known_route:
                self._send_return_path_retry(contact, arrival)
        return ReceptionOutcome.ACCEPTED

    def _process_acknowledgement(self, acknowledgement_code: bytes) -> ExpectedAcknowledgement | None:
        """MyMesh::processAck: one SEND_CONFIRMED push per expected code, with the round-trip time."""
        matched_acknowledgement = self._expected_acknowledgements.match(acknowledgement_code)
        if matched_acknowledgement is None:
            return None
        round_trip_milliseconds = int((time.monotonic() - matched_acknowledgement.sent_at) * 1000)
        self._write_push(
            send_confirmed_push(
                acknowledgement_code=acknowledgement_code, round_trip_milliseconds=round_trip_milliseconds
            )
        )
        self._release_held_message_sent_reply(acknowledgement_code)
        return matched_acknowledgement

    def _send_return_path_retry(self, contact: ContactRecord, arrival: PacketArrival) -> None:
        """A flood ACK while a direct route is stored: resend our path to the peer directly (3 s later)."""
        self._queue_transmission(
            PathReturnPacket(
                sender_public_key=self.public_key,
                route=PacketRoute.direct(path=contact.route_path, path_hash_size=contact.path_hash_size),
                destination_public_key=contact.public_key,
                returned_path=arrival.path,
                returned_path_hash_size=arrival.path_hash_size,
                random_filler=self._draw_path_return_random_filler(),
            ),
            delay_seconds=self.timing.return_path_retry_delay_seconds,
        )

    def _draw_path_return_random_filler(self) -> bytes:
        return self._random.randbytes(PATH_RETURN_RANDOM_FILLER_BYTES)

    def _receive_path_return(
        self, packet: PathReturnPacket, arrival: PacketArrival, *, bypasses_deduplication: bool
    ) -> ReceptionOutcome:
        """Store the returned path unconditionally, push PATH_UPDATE, process an embedded ACK, and
        answer a flooded path with a reciprocal one sent directly along the path just learned."""
        if self._is_repeat(packet, bypasses_deduplication=bypasses_deduplication):
            return ReceptionOutcome.ALREADY_SEEN
        if packet.destination_public_key != self.public_key:
            return ReceptionOutcome.NOT_ADDRESSED_TO_THIS_NODE
        sender = self._contacts.find_by_public_key(packet.sender_public_key)
        if sender is None:
            return ReceptionOutcome.UNKNOWN_SENDER
        self._contacts.replace(
            sender.with_route(
                route_path=packet.returned_path,
                path_hash_size=packet.returned_path_hash_size,
                last_modified=self.clock_time(),
            )
        )
        self._write_push(public_key_push(PushCode.PATH_UPDATED, sender.public_key))
        if packet.acknowledgement is not None:
            self._process_acknowledgement(packet.acknowledgement.code)
        if arrival.arrived_by_flood:
            self._queue_transmission(
                PathReturnPacket(
                    sender_public_key=self.public_key,
                    route=PacketRoute.direct(path=packet.returned_path, path_hash_size=packet.returned_path_hash_size),
                    destination_public_key=sender.public_key,
                    returned_path=arrival.path,
                    returned_path_hash_size=arrival.path_hash_size,
                    random_filler=self._draw_path_return_random_filler(),
                ),
                delay_seconds=self.timing.reciprocal_path_delay_seconds,
            )
        return ReceptionOutcome.ACCEPTED

    def _receive_advert_contents(
        self, packet: AdvertPacket, arrival: PacketArrival, *, bypasses_deduplication: bool
    ) -> ReceptionOutcome:
        advert = parse_advert_payload(
            packet.advert_payload, route_type=ROUTE_TYPE_FLOOD, path_length=arrival.path_length, path=arrival.path
        )
        if advert is None:
            return ReceptionOutcome.MALFORMED_ADVERT
        if advert.public_key == self.public_key:
            return ReceptionOutcome.OWN_ADVERT
        if self._is_repeat(packet, bypasses_deduplication=bypasses_deduplication):
            return ReceptionOutcome.ALREADY_SEEN
        if not advert.signature_is_valid:
            return ReceptionOutcome.FORGED_SIGNATURE
        return self._handle_verified_advert(advert, arrival)

    def _handle_verified_advert(self, advert: ParsedAdvert, arrival: PacketArrival) -> ReceptionOutcome:
        """BaseChatMesh::onAdvertRecv."""
        if not advert.name:
            return ReceptionOutcome.ADVERT_WITHOUT_NAME
        known_contact = self._contacts.find_by_public_key(advert.public_key)
        if known_contact is not None and advert.timestamp <= known_contact.last_advert_timestamp:
            return ReceptionOutcome.ADVERT_NOT_NEWER
        if known_contact is None:
            discovered_contact = self._contact_from_advert(advert)
            refusal = self._refuse_unknown_advert(discovered_contact, arrival)
            if refusal is not None:
                return refusal
            known_contact = discovered_contact
        self._advert_blobs.put(
            public_key=advert.public_key,
            packet=build_advert_packet(
                route_type=ROUTE_TYPE_FLOOD,
                path_length=arrival.path_length if arrival.arrived_by_flood else 0,
                path=arrival.path,
                advert_payload=advert.advert_payload,
            ),
            stored_at_node_time=self.clock_time(),
        )
        self._contacts.replace(self._contact_updated_from_advert(known_contact, advert))
        self._write_push(public_key_push(PushCode.ADVERT, advert.public_key))
        return ReceptionOutcome.ACCEPTED

    def _refuse_unknown_advert(
        self, discovered_contact: ContactRecord, arrival: PacketArrival
    ) -> ReceptionOutcome | None:
        """Report the node with NEW_CONTACT instead of storing it, or store it and return None."""
        maximum_hops = self.preferences.auto_add_maximum_hops
        if not self.preferences.auto_adds_node_type(discovered_contact.node_type) or (
            maximum_hops > 0 and arrival.hop_count >= maximum_hops
        ):
            self._write_push(discovered_contact.to_frame(PushCode.NEW_ADVERT))
            return ReceptionOutcome.REPORTED_AS_NEW_CONTACT
        add_outcome = self.add_or_update_contact(discovered_contact)
        if not add_outcome.added:
            self._write_push(discovered_contact.to_frame(PushCode.NEW_ADVERT))
            self._write_push(contacts_full_push())
            return ReceptionOutcome.CONTACT_TABLE_FULL
        return None

    def _contact_from_advert(self, advert: ParsedAdvert) -> ContactRecord:
        """BaseChatMesh::populateContactFromAdvert: no route, flags 0, the node's clock as lastmod."""
        location = advert.location
        return dataclasses.replace(
            ContactRecord.create(
                public_key=advert.public_key,
                name=advert.name[:NODE_NAME_MAXIMUM_BYTES],
                node_type=advert.node_type,
                last_advert_timestamp=advert.timestamp,
                last_modified=self.clock_time(),
            ),
            latitude_microdegrees=0 if location is None else location.latitude_microdegrees,
            longitude_microdegrees=0 if location is None else location.longitude_microdegrees,
        )

    def _contact_updated_from_advert(self, contact: ContactRecord, advert: ParsedAdvert) -> ContactRecord:
        """A newer advert refreshes name, type, location and timestamps, never the stored route."""
        location = advert.location
        return dataclasses.replace(
            contact,
            name_field=contact_name_field(advert.name[:NODE_NAME_MAXIMUM_BYTES]),
            node_type=advert.node_type,
            latitude_microdegrees=contact.latitude_microdegrees if location is None else location.latitude_microdegrees,
            longitude_microdegrees=(
                contact.longitude_microdegrees if location is None else location.longitude_microdegrees
            ),
            last_advert_timestamp=advert.timestamp,
            last_modified=self.clock_time(),
        )

    def _receive_channel_message(
        self, packet: ChannelMessagePacket, arrival: PacketArrival, *, bypasses_deduplication: bool
    ) -> ReceptionOutcome:
        if self._is_repeat(packet, bypasses_deduplication=bypasses_deduplication):
            return ReceptionOutcome.ALREADY_SEEN
        channel_index = self._channels.find_index_by_secret(packet.channel_secret)
        if channel_index is None:
            return ReceptionOutcome.UNKNOWN_CHANNEL
        self._add_to_offline_queue(
            channel_message_frame(
                uses_version_3_layout=self._app_target_version >= VERSION_3_APP_TARGET,
                signal_to_noise_quarters=self._draw_signal_to_noise_quarters(),
                channel_index=channel_index,
                path_length=arrival.path_length,
                sender_timestamp=packet.sender_timestamp,
                text=packet.text,
            )
        )
        return ReceptionOutcome.ACCEPTED

    def _receive_channel_data(
        self, packet: ChannelDataPacket, arrival: PacketArrival, *, bypasses_deduplication: bool
    ) -> ReceptionOutcome:
        if self._is_repeat(packet, bypasses_deduplication=bypasses_deduplication):
            return ReceptionOutcome.ALREADY_SEEN
        channel_index = self._channels.find_index_by_secret(packet.channel_secret)
        if channel_index is None:
            return ReceptionOutcome.UNKNOWN_CHANNEL
        if len(packet.data) > MAXIMUM_CHANNEL_DATA_BYTES:
            return ReceptionOutcome.CHANNEL_DATA_TOO_LONG
        self._add_to_offline_queue(
            channel_data_frame(
                signal_to_noise_quarters=self._draw_signal_to_noise_quarters(),
                channel_index=channel_index,
                path_length=arrival.path_length,
                data_type=packet.data_type,
                payload=packet.data,
            )
        )
        return ReceptionOutcome.ACCEPTED
