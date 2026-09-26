"""The only code that talks to the node: one command at a time, each through a typed method.

meshcore matches replies to commands by event type alone and holds no lock, so two commands in
flight would take each other's replies, and a reply that arrives after its command timed out
would be taken as the answer to the next command expecting that type. The gateway therefore
runs every command under one lock and, after a lost reply, keeps the lock a little longer so a
late reply lands while nothing waits for it. Three lost replies in a row, with no node traffic
in between, ask the connection supervisor for a new connection.

Commands fail fast while no client is attached. When the connection is torn down, the command
in flight is cancelled rather than left to wait out its timeout, so the next connection's
handshake does not queue behind it.

Every frame is built here at its full length. The library's helpers are left out where they
read-modify-write settings in two commands, send short frames the firmware misreads, or mutate
the library's own contact cache before the node answered.

The node's private key crosses the link only in export_private_key and import_private_key; the
library's frame logging is held back meanwhile (worker.private_key_log_guard), and the key stays
wrapped in NodePrivateKey, which never shows it.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from meshcore import EventType

from messaging.models import OutboundPacket
from messaging.outbound_packets import NodeSendOutcome, PacketOutcomeUnknown, PacketQueuedOnNode, PacketRejectedByNode
from node.node_identity_backups import NodePrivateKey
from worker.node_contact_records import (
    ListedNodeContact,
    NodeContactRecord,
    build_add_update_contact_frame,
    parse_listed_contacts,
)
from worker.private_key_log_guard import install_library_frame_log_guard
from worker.worker_timing import WorkerTiming

logger = logging.getLogger(__name__)

PUBLIC_KEY_BYTES = 32
PUBLIC_KEY_PREFIX_BYTES = 6
CHANNEL_NAME_FIELD_BYTES = 32
CHANNEL_SECRET_BYTES = 16
NODE_NAME_MAXIMUM_BYTES = 31

# Bytes 1 to 7 of the app start are reserved; the app name follows them.
APP_START_FRAME = b"\x01\x03      mccli"
# Version 3 makes the node queue received messages in the V3 frames that carry the SNR.
DEVICE_QUERY_FRAME = b"\x16\x03"
GET_DEVICE_TIME_FRAME = b"\x05"
SYNC_NEXT_MESSAGE_FRAME = b"\x0a"
EXPORT_SELF_CONTACT_FRAME = b"\x11"
GET_AUTO_ADD_CONFIGURATION_FRAME = b"\x3b"
EXPORT_PRIVATE_KEY_FRAME = b"\x17"
# The firmware acts on these two only with their text suffix; a bare code is refused.
FACTORY_RESET_FRAME = b"\x33reset"
REBOOT_FRAME = b"\x13reboot"

SET_DEVICE_TIME_COMMAND_CODE = 0x06
SEND_SELF_ADVERT_COMMAND_CODE = 0x07
SET_ADVERT_NAME_COMMAND_CODE = 0x08
SEND_TEXT_MESSAGE_COMMAND_CODE = 0x02
SET_RADIO_PARAMETERS_COMMAND_CODE = 0x0B
SET_TRANSMIT_POWER_COMMAND_CODE = 0x0C
RESET_PATH_COMMAND_CODE = 0x0D
REMOVE_CONTACT_COMMAND_CODE = 0x0F
GET_CHANNEL_COMMAND_CODE = 0x1F
SET_CHANNEL_COMMAND_CODE = 0x20
SET_OTHER_PARAMETERS_COMMAND_CODE = 0x26
SET_AUTO_ADD_CONFIGURATION_COMMAND_CODE = 0x3A
SET_PATH_HASH_MODE_COMMAND_CODE = 0x3D
IMPORT_PRIVATE_KEY_COMMAND_CODE = 0x18

PLAIN_TEXT_TYPE = 0
# Every packet carries a fresh timestamp, so the MeshCore attempt is always 0.
FIRST_ATTEMPT = 0
FLOOD_ROUTE_TYPE = 1
FLOOD_ADVERT_FLAG = 0x01

ERR_CODE_NOT_FOUND = 2
ERR_CODE_BAD_STATE = 4

LOST_REPLY_REASON = "no_event_received"


class NodeGatewayError(Exception):
    """A node command did not succeed; the message says which command and why."""


class NodeNotConnectedError(NodeGatewayError):
    """No node is attached, or the link was lost while the command ran."""


class NodeReplyLostError(NodeGatewayError):
    """The node did not answer in time."""


class NodeRejectedCommandError(NodeGatewayError):
    def __init__(self, command_description: str, error_code: int) -> None:
        super().__init__(f"The node refused {command_description} with error {error_code}.")
        self.error_code = error_code


class UnexpectedNodeReplyError(NodeGatewayError):
    """The reply was of a kind the command does not expect."""


class NextMessageOutcome(StrEnum):
    DIRECT_MESSAGE = "direct_message"
    # A channel message or datagram; the relay has no use for it and it is dropped.
    CHANNEL_TRAFFIC = "channel_traffic"
    NO_MORE_MESSAGES = "no_more_messages"
    REPLY_LOST = "reply_lost"


@dataclass(frozen=True, kw_only=True)
class PrivateKeyExported:
    """The node's private key, and the public key it derives, which the node reported right after it."""

    private_key: NodePrivateKey
    public_key: str


@dataclass(frozen=True)
class PrivateKeyExportDisabled:
    """The firmware was built without ENABLE_PRIVATE_KEY_EXPORT."""


type PrivateKeyExportOutcome = PrivateKeyExported | PrivateKeyExportDisabled


@dataclass(frozen=True)
class PrivateKeyImported:
    """The node saved the identity to its flash and uses it at once; it survives reboots."""


@dataclass(frozen=True)
class PrivateKeyImportDisabled:
    """The firmware was built without ENABLE_PRIVATE_KEY_IMPORT."""


@dataclass(frozen=True, kw_only=True)
class PrivateKeyImportRefused:
    # 6: the key is not valid (its public key starts with 00 or FF, or the key-exchange check
    # failed); 5: the identity could not be saved to flash.
    error_code: int


type PrivateKeyImportOutcome = PrivateKeyImported | PrivateKeyImportDisabled | PrivateKeyImportRefused


class FactoryResetReply(StrEnum):
    # The expected case: the firmware disables its serial interface before it resets.
    NO_REPLY = "no_reply"
    ACCEPTED = "accepted"
    REFUSED = "refused"


@dataclass(frozen=True, kw_only=True)
class DeviceInformation:
    # The companion protocol version (13 for firmware v1.17.1).
    protocol_version: int
    firmware_version: str
    firmware_build: str
    model: str
    maximum_contacts: int
    client_repeat: bool
    # In bytes: the firmware's path hash mode plus one.
    path_hash_size: int


@dataclass(frozen=True, kw_only=True)
class SelfInformation:
    public_key: str
    name: str
    transmit_power_dbm: int
    maximum_transmit_power_dbm: int
    radio_frequency_kilohertz: int
    radio_bandwidth_hertz: int
    radio_spreading_factor: int
    radio_coding_rate: int
    manual_add_contacts: bool
    multi_acks: int
    advert_location_policy: int
    telemetry_modes: int


@dataclass(frozen=True, kw_only=True)
class AutoAddConfiguration:
    # The firmware's autoadd_config byte: type bits and the overwrite-oldest bit (0x01).
    configuration: int
    maximum_hops: int


@dataclass(frozen=True, kw_only=True)
class ChannelInformation:
    channel_index: int
    name: str
    secret: bytes


type NodeCommandStarter = Callable[[Any], Awaitable[Any]]


class NodeGateway:
    def __init__(self, *, timing: WorkerTiming, request_reconnect: Callable[[str], None]) -> None:
        self._timing = timing
        self._request_reconnect = request_reconnect
        self._meshcore_client: Any | None = None
        self._command_lock = asyncio.Lock()
        self._running_command_task: asyncio.Task[Any] | None = None
        self._consecutive_lost_replies = 0
        self.last_node_traffic_at = time.monotonic()
        self._library_frame_log_guard = install_library_frame_log_guard()
        self._library_log_holds_until_detach = 0

    # ----- the attached client --------------------------------------------------------------

    @property
    def meshcore_client(self) -> Any | None:
        return self._meshcore_client

    def attach(self, meshcore_client: Any) -> None:
        self._meshcore_client = meshcore_client
        self._consecutive_lost_replies = 0
        self.last_node_traffic_at = time.monotonic()

    def detach(self) -> None:
        self._meshcore_client = None
        if self._running_command_task is not None:
            self._running_command_task.cancel()
        while self._library_log_holds_until_detach:
            self._library_log_holds_until_detach -= 1
            self._library_frame_log_guard.release()

    def record_node_push(self) -> None:
        """Called for every event the node sends: the link is alive, whatever the last command did."""
        self._consecutive_lost_replies = 0
        self.last_node_traffic_at = time.monotonic()

    def seconds_since_node_traffic(self) -> float:
        return time.monotonic() - self.last_node_traffic_at

    def _require_connected_client(self) -> Any:
        meshcore_client = self._meshcore_client
        if meshcore_client is None or not meshcore_client.is_connected:
            raise NodeNotConnectedError("The node is not connected.")
        return meshcore_client

    # ----- running one command --------------------------------------------------------------

    async def run_node_command(self, start_node_command: NodeCommandStarter, *, reply_is_optional: bool = False) -> Any:
        """Run one command under the command lock and return the library's result event."""
        async with self._command_lock:
            return await self._run_node_command_holding_lock(start_node_command, reply_is_optional=reply_is_optional)

    async def _run_node_command_holding_lock(
        self, start_node_command: NodeCommandStarter, *, reply_is_optional: bool = False
    ) -> Any:
        meshcore_client = self._require_connected_client()
        result_event = await self._run_cancellable_command(start_node_command, meshcore_client)
        if is_lost_reply(result_event):
            if not reply_is_optional:
                self._count_lost_reply()
            await asyncio.sleep(self._timing.late_reply_grace_seconds)
        else:
            self.record_node_push()
        return result_event

    async def _run_cancellable_command(self, start_node_command: NodeCommandStarter, meshcore_client: Any) -> Any:
        command_task = asyncio.ensure_future(start_node_command(meshcore_client))
        self._running_command_task = command_task
        try:
            await asyncio.wait({command_task})
        except asyncio.CancelledError:
            command_task.cancel()
            raise
        finally:
            self._running_command_task = None
        if command_task.cancelled():
            raise NodeNotConnectedError("The link to the node was lost while a command was running.")
        return command_task.result()

    def _count_lost_reply(self) -> None:
        self._consecutive_lost_replies += 1
        if self._consecutive_lost_replies >= self._timing.lost_replies_before_reconnect:
            self._consecutive_lost_replies = 0
            self._request_reconnect(f"{self._timing.lost_replies_before_reconnect} node commands in a row got no reply")

    async def send_command_frame(
        self,
        frame: bytes,
        expected_event_types: list[Any],
        *,
        timeout_seconds: float | None = None,
        reply_is_optional: bool = False,
    ) -> Any:
        start_command = self._build_frame_command(frame, expected_event_types, timeout_seconds)
        return await self.run_node_command(start_command, reply_is_optional=reply_is_optional)

    def _build_frame_command(
        self, frame: bytes, expected_event_types: list[Any], timeout_seconds: float | None = None
    ) -> NodeCommandStarter:
        command_timeout_seconds = timeout_seconds or self._timing.node_command_timeout_seconds

        async def start_command(meshcore_client: Any) -> Any:
            return await meshcore_client.commands.send(frame, expected_event_types, command_timeout_seconds)

        return start_command

    async def _exchange_private_key_frame(
        self, frame: bytes, expected_event_types: list[Any], replies_that_end_the_exposure: frozenset[Any]
    ) -> Any:
        """Send one frame that carries, or asks for, the private key; the caller holds the command lock.

        meshcore matches replies by type alone, so the reply this command waited for may be a
        late one to an earlier command, with the frame that carries the key still to come. Only a
        reply in replies_that_end_the_exposure releases the library's frame logging; after any
        other outcome it stays held back until this link is torn down.
        """
        self._library_frame_log_guard.hold()
        exposure_has_ended = False
        try:
            result_event = await self._run_node_command_holding_lock(
                self._build_frame_command(frame, expected_event_types)
            )
            exposure_has_ended = result_event.type in replies_that_end_the_exposure and not is_lost_reply(result_event)
            return result_event
        finally:
            if exposure_has_ended or self._meshcore_client is None:
                self._library_frame_log_guard.release()
            else:
                self._library_log_holds_until_detach += 1

    async def send_frame_expecting_ok(self, frame: bytes, command_description: str) -> None:
        result_event = await self.send_command_frame(frame, [EventType.OK, EventType.ERROR])
        raise_unless_successful(result_event, command_description)

    # ----- session and device ---------------------------------------------------------------

    async def query_device(self) -> DeviceInformation:
        """Also switches the node to V3 message frames, which it forgets on every boot."""
        result_event = await self.send_command_frame(DEVICE_QUERY_FRAME, [EventType.DEVICE_INFO, EventType.ERROR])
        raise_unless_successful(result_event, "the device query")
        return parse_device_information(result_event.payload)

    async def read_self_information(self) -> SelfInformation:
        result_event = await self.send_command_frame(APP_START_FRAME, [EventType.SELF_INFO, EventType.ERROR])
        raise_unless_successful(result_event, "the app start")
        return parse_self_information(result_event.payload)

    async def read_node_clock(self) -> int:
        result_event = await self.send_command_frame(GET_DEVICE_TIME_FRAME, [EventType.CURRENT_TIME, EventType.ERROR])
        raise_unless_successful(result_event, "reading the clock")
        return int(result_event.payload["time"])

    async def set_node_clock(self, unix_time: int) -> None:
        frame = bytes([SET_DEVICE_TIME_COMMAND_CODE]) + unix_time.to_bytes(4, "little")
        await self.send_frame_expecting_ok(frame, "setting the clock")

    async def send_advert(self, *, flood: bool) -> None:
        frame = (
            bytes([SEND_SELF_ADVERT_COMMAND_CODE, FLOOD_ADVERT_FLAG])
            if flood
            else bytes([SEND_SELF_ADVERT_COMMAND_CODE])
        )
        await self.send_frame_expecting_ok(frame, "the advert")

    async def export_own_contact_card(self) -> str:
        result_event = await self.send_command_frame(
            EXPORT_SELF_CONTACT_FRAME, [EventType.CONTACT_URI, EventType.ERROR]
        )
        raise_unless_successful(result_event, "exporting the contact card")
        return str(result_event.payload["uri"])

    async def read_auto_add_configuration(self) -> AutoAddConfiguration:
        result_event = await self.send_command_frame(
            GET_AUTO_ADD_CONFIGURATION_FRAME, [EventType.AUTOADD_CONFIG, EventType.ERROR]
        )
        raise_unless_successful(result_event, "reading the auto-add configuration")
        return AutoAddConfiguration(
            configuration=int(result_event.payload["config"]),
            maximum_hops=int(result_event.payload.get("max_hops", 0)),
        )

    async def read_channel(self, channel_index: int) -> ChannelInformation:
        frame = bytes([GET_CHANNEL_COMMAND_CODE, channel_index])
        result_event = await self.send_command_frame(frame, [EventType.CHANNEL_INFO, EventType.ERROR])
        raise_unless_successful(result_event, f"reading channel {channel_index}")
        return ChannelInformation(
            channel_index=channel_index,
            name=str(result_event.payload.get("channel_name", "")),
            secret=bytes(result_event.payload.get("channel_secret", b"")),
        )

    async def set_channel(self, channel_index: int, channel_name: str, channel_secret: bytes) -> None:
        if len(channel_secret) != CHANNEL_SECRET_BYTES:
            raise ValueError(f"A channel secret has {CHANNEL_SECRET_BYTES} bytes, not {len(channel_secret)}.")
        name_field = channel_name.encode("utf-8")[: CHANNEL_NAME_FIELD_BYTES - 1].ljust(
            CHANNEL_NAME_FIELD_BYTES, b"\x00"
        )
        frame = bytes([SET_CHANNEL_COMMAND_CODE, channel_index]) + name_field + channel_secret
        await self.send_frame_expecting_ok(frame, f"setting channel {channel_index}")

    # ----- settings, as raw frames of full length ---------------------------------------------

    async def set_node_name(self, node_name: str) -> None:
        encoded_name = node_name.encode("utf-8")
        if not 1 <= len(encoded_name) <= NODE_NAME_MAXIMUM_BYTES:
            raise ValueError(f"A node name has 1 to {NODE_NAME_MAXIMUM_BYTES} bytes of UTF-8.")
        await self.send_frame_expecting_ok(bytes([SET_ADVERT_NAME_COMMAND_CODE]) + encoded_name, "setting the name")

    async def set_radio_parameters(
        self,
        *,
        frequency_kilohertz: int,
        bandwidth_hertz: int,
        spreading_factor: int,
        coding_rate: int,
        client_repeat: bool,
    ) -> None:
        """The repeat byte is always sent: a frame without it silently switches client repeat off."""
        frame = (
            bytes([SET_RADIO_PARAMETERS_COMMAND_CODE])
            + frequency_kilohertz.to_bytes(4, "little")
            + bandwidth_hertz.to_bytes(4, "little")
            + bytes([spreading_factor, coding_rate, 1 if client_repeat else 0])
        )
        await self.send_frame_expecting_ok(frame, "setting the radio")

    async def set_transmit_power(self, transmit_power_dbm: int) -> None:
        """One signed byte; the library's helper cannot send a negative power."""
        frame = bytes([SET_TRANSMIT_POWER_COMMAND_CODE]) + transmit_power_dbm.to_bytes(1, "little", signed=True)
        await self.send_frame_expecting_ok(frame, "setting the transmit power")

    async def set_path_hash_size(self, path_hash_size: int) -> None:
        frame = bytes([SET_PATH_HASH_MODE_COMMAND_CODE, 0, path_hash_size - 1])
        await self.send_frame_expecting_ok(frame, "setting the path hash size")

    async def set_other_parameters(
        self, *, manual_add_contacts: bool, telemetry_modes: int, advert_location_policy: int, multi_acks: int
    ) -> None:
        """All four values in one frame, so no setting is read back and written again in between."""
        frame = bytes(
            [
                SET_OTHER_PARAMETERS_COMMAND_CODE,
                1 if manual_add_contacts else 0,
                telemetry_modes,
                advert_location_policy,
                multi_acks,
            ]
        )
        await self.send_frame_expecting_ok(frame, "setting the other parameters")

    async def set_auto_add_configuration(self, *, configuration: int, maximum_hops: int) -> None:
        frame = bytes([SET_AUTO_ADD_CONFIGURATION_COMMAND_CODE, configuration, maximum_hops])
        await self.send_frame_expecting_ok(frame, "setting the auto-add configuration")

    async def factory_reset(self) -> tuple[FactoryResetReply, int | None]:
        """Send the reset and wait briefly for a reply; the refusal's error code comes with REFUSED."""
        result_event = await self.send_command_frame(
            FACTORY_RESET_FRAME,
            [EventType.OK, EventType.ERROR],
            timeout_seconds=self._timing.factory_reset_reply_seconds,
            reply_is_optional=True,
        )
        if is_lost_reply(result_event):
            return FactoryResetReply.NO_REPLY, None
        if result_event.type == EventType.ERROR:
            return FactoryResetReply.REFUSED, result_event.payload.get("error_code")
        return FactoryResetReply.ACCEPTED, None

    async def export_private_key(self) -> PrivateKeyExportOutcome:
        """The node's 64-byte private key and, read under the same lock, the public key it belongs to.

        The key must derive that public key: bytes from a node answering for another identity, or
        corrupted on the link, raise UnexpectedNodeReplyError and are never handed on.
        """
        async with self._command_lock:
            export_event = await self._exchange_private_key_frame(
                EXPORT_PRIVATE_KEY_FRAME,
                [EventType.PRIVATE_KEY, EventType.DISABLED, EventType.ERROR],
                replies_that_end_the_exposure=frozenset({EventType.PRIVATE_KEY, EventType.DISABLED}),
            )
            if export_event.type == EventType.DISABLED:
                return PrivateKeyExportDisabled()
            raise_unless_successful(export_event, "exporting the private key")
            if export_event.type != EventType.PRIVATE_KEY:
                raise UnexpectedNodeReplyError(f"Exporting the private key got {export_event.type}.")
            private_key = NodePrivateKey(bytes(export_event.payload["private_key"]))

            self_information_event = await self._run_node_command_holding_lock(
                self._build_frame_command(APP_START_FRAME, [EventType.SELF_INFO, EventType.ERROR])
            )
            raise_unless_successful(self_information_event, "the app start after the key export")
            public_key = parse_self_information(self_information_event.payload).public_key
            if not private_key.belongs_to(public_key):
                raise UnexpectedNodeReplyError(
                    f"The private key the node exported does not belong to the key {public_key[:12]} it reports."
                )
            return PrivateKeyExported(private_key=private_key, public_key=public_key)

    async def import_private_key(self, private_key: NodePrivateKey) -> PrivateKeyImportOutcome:
        """The raw frame: the library's helper waits for OK or ERROR only and times out on a DISABLED reply.

        A lost reply raises NodeReplyLostError; the node may or may not hold the key then.
        """
        frame = bytes([IMPORT_PRIVATE_KEY_COMMAND_CODE]) + private_key.reveal_bytes()
        # The key travels in the command frame, which the library logs before any reply can arrive.
        import_reply_types = [EventType.OK, EventType.ERROR, EventType.DISABLED]
        async with self._command_lock:
            result_event = await self._exchange_private_key_frame(
                frame, import_reply_types, replies_that_end_the_exposure=frozenset(import_reply_types)
            )
        if result_event.type == EventType.DISABLED:
            return PrivateKeyImportDisabled()
        if result_event.type == EventType.OK:
            return PrivateKeyImported()
        error_code = result_event.payload.get("error_code") if result_event.type == EventType.ERROR else None
        if error_code is not None:
            return PrivateKeyImportRefused(error_code=int(error_code))
        raise_unless_successful(result_event, "importing the private key")
        raise UnexpectedNodeReplyError(f"Importing the private key got {result_event.type}.")

    async def reboot(self) -> None:
        """The firmware never answers a reboot; it saves its contacts and drops the link."""

        async def start_reboot(meshcore_client: Any) -> Any:
            return await meshcore_client.commands.send(REBOOT_FRAME)

        await self.run_node_command(start_reboot)

    # ----- messages ---------------------------------------------------------------------------

    async def send_text_message(self, public_key: str, text: str, sender_timestamp: int) -> NodeSendOutcome:
        """A lost reply is an unknown outcome: the node may have queued the packet."""
        frame = build_text_message_frame(public_key, text, sender_timestamp)
        result_event = await self.send_command_frame(frame, [EventType.MSG_SENT, EventType.ERROR])
        if result_event.type == EventType.MSG_SENT:
            return build_packet_queued_outcome(result_event.payload)
        error_code = result_event.payload.get("error_code") if result_event.type == EventType.ERROR else None
        if error_code is not None:
            return PacketRejectedByNode(node_error_code=int(error_code))
        return PacketOutcomeUnknown()

    async def get_next_message(self) -> NextMessageOutcome:
        """Pop the head of the node's offline queue.

        Not the library's get_msg, which does not expect a channel datagram and would time out on
        one although the node already handed it over. A direct message reaches the worker through
        its CONTACT_MSG_RECV subscription, so only the kind of the reply matters here.
        """
        result_event = await self.send_command_frame(
            SYNC_NEXT_MESSAGE_FRAME,
            [
                EventType.CONTACT_MSG_RECV,
                EventType.CHANNEL_MSG_RECV,
                EventType.CHANNEL_DATA_RECV,
                EventType.NO_MORE_MSGS,
                EventType.ERROR,
            ],
            timeout_seconds=self._timing.get_next_message_timeout_seconds,
        )
        match result_event.type:
            case EventType.CONTACT_MSG_RECV:
                return NextMessageOutcome.DIRECT_MESSAGE
            case EventType.CHANNEL_MSG_RECV | EventType.CHANNEL_DATA_RECV:
                return NextMessageOutcome.CHANNEL_TRAFFIC
            case EventType.NO_MORE_MSGS:
                return NextMessageOutcome.NO_MORE_MESSAGES
        if is_lost_reply(result_event):
            return NextMessageOutcome.REPLY_LOST
        raise_unless_successful(result_event, "fetching the next waiting message")
        raise UnexpectedNodeReplyError(f"Fetching the next waiting message got {result_event.type}.")

    # ----- contacts ---------------------------------------------------------------------------

    async def reset_path(self, public_key: str) -> bool:
        """Forget the stored route, so the next DM floods; False when the node does not know the contact."""
        frame = bytes([RESET_PATH_COMMAND_CODE]) + decode_public_key(public_key)
        result_event = await self.send_command_frame(frame, [EventType.OK, EventType.ERROR])
        if is_node_error(result_event, ERR_CODE_NOT_FOUND):
            return False
        raise_unless_successful(result_event, "resetting a route")
        return True

    async def add_contact(self, record: NodeContactRecord) -> None:
        """Raises NodeRejectedCommandError with error 3 when the node's contact table is full."""
        await self.send_frame_expecting_ok(build_add_update_contact_frame(record), "adding a contact")

    async def remove_contact(self, public_key: str) -> bool:
        """False when the node did not hold the contact, which is as good as removing it."""
        frame = bytes([REMOVE_CONTACT_COMMAND_CODE]) + decode_public_key(public_key)
        result_event = await self.send_command_frame(frame, [EventType.OK, EventType.ERROR])
        if is_node_error(result_event, ERR_CODE_NOT_FOUND):
            return False
        raise_unless_successful(result_event, "removing a contact")
        return True

    async def list_contacts(self) -> dict[str, ListedNodeContact]:
        """The node's whole contact table, keyed by public key: the node's truth for the reconciler.

        Not the library's get_contacts, whose five-second deadline covers the whole listing and
        which any unrelated error ends. The lock is held for the whole listing, since another
        command meanwhile would get ERR_CODE_BAD_STATE or stop the listing.
        """
        async with self._command_lock:
            meshcore_client = self._require_connected_client()
            try:
                contacts_payload = await self._run_cancellable_command(self._stream_contact_listing, meshcore_client)
            except NodeRejectedCommandError as listing_error:
                if listing_error.error_code != ERR_CODE_BAD_STATE:
                    raise
                await asyncio.sleep(self._timing.contact_listing_busy_retry_seconds)
                meshcore_client = self._require_connected_client()
                contacts_payload = await self._run_cancellable_command(self._stream_contact_listing, meshcore_client)
            self.record_node_push()
            return parse_listed_contacts(contacts_payload)

    async def _stream_contact_listing(self, meshcore_client: Any) -> dict[str, Any]:
        """Subscribe first, then ask; every contact that arrives extends the deadline."""
        loop = asyncio.get_running_loop()
        listing_result: asyncio.Future[Any] = loop.create_future()
        last_activity_at = loop.time()

        def record_listed_contact(_event: Any) -> None:
            nonlocal last_activity_at
            last_activity_at = loop.time()

        def finish_listing(event: Any) -> None:
            if not listing_result.done():
                listing_result.set_result(event)

        subscriptions = [
            meshcore_client.subscribe(EventType.NEXT_CONTACT, record_listed_contact),
            meshcore_client.subscribe(EventType.CONTACTS, finish_listing),
            meshcore_client.subscribe(EventType.ERROR, finish_listing),
        ]
        overall_deadline = loop.time() + self._timing.contact_listing_overall_seconds
        try:
            await meshcore_client.commands.get_contacts_async()
            while not listing_result.done():
                deadline = min(last_activity_at + self._timing.contact_listing_activity_seconds, overall_deadline)
                if loop.time() >= deadline:
                    self._count_lost_reply()
                    raise NodeReplyLostError("The contact listing stalled before it was complete.")
                await asyncio.wait({listing_result}, timeout=deadline - loop.time())
        finally:
            for subscription in subscriptions:
                subscription.unsubscribe()
            if not listing_result.done():
                listing_result.cancel()

        result_event = listing_result.result()
        raise_unless_successful(result_event, "listing the contacts")
        return dict(result_event.payload)

    def flush_library_contact_caches(self) -> None:
        """The library keeps every NEW_CONTACT it saw in memory forever; the worker never reads that cache."""
        if self._meshcore_client is not None:
            self._meshcore_client.flush_pending_contacts()


def is_lost_reply(result_event: Any) -> bool:
    return bool(result_event.type == EventType.ERROR and result_event.payload.get("reason") == LOST_REPLY_REASON)


def is_node_error(result_event: Any, error_code: int) -> bool:
    return bool(result_event.type == EventType.ERROR and result_event.payload.get("error_code") == error_code)


def raise_unless_successful(result_event: Any, command_description: str) -> None:
    if result_event.type != EventType.ERROR:
        return
    if is_lost_reply(result_event):
        raise NodeReplyLostError(f"The node did not answer {command_description}.")
    error_code = result_event.payload.get("error_code")
    if error_code is not None:
        raise NodeRejectedCommandError(command_description, int(error_code))
    raise UnexpectedNodeReplyError(f"{command_description} failed: {result_event.payload}")


def decode_public_key(public_key: str) -> bytes:
    public_key_bytes = bytes.fromhex(public_key)
    if len(public_key_bytes) != PUBLIC_KEY_BYTES:
        raise ValueError(f"A public key has {PUBLIC_KEY_BYTES} bytes, not {len(public_key_bytes)}.")
    return public_key_bytes


def build_text_message_frame(public_key: str, text: str, sender_timestamp: int) -> bytes:
    """CMD_SEND_TXT_MSG: text type, attempt, timestamp, the recipient's six-byte prefix, then the text."""
    recipient_prefix = decode_public_key(public_key)[:PUBLIC_KEY_PREFIX_BYTES]
    return (
        bytes([SEND_TEXT_MESSAGE_COMMAND_CODE, PLAIN_TEXT_TYPE, FIRST_ATTEMPT])
        + sender_timestamp.to_bytes(4, "little")
        + recipient_prefix
        + text.encode("utf-8")
    )


def build_packet_queued_outcome(message_sent_payload: dict[str, Any]) -> PacketQueuedOnNode:
    route = (
        OutboundPacket.Route.FLOOD if message_sent_payload["type"] == FLOOD_ROUTE_TYPE else OutboundPacket.Route.DIRECT
    )
    return PacketQueuedOnNode(
        route=route,
        expected_acknowledgement_code=bytes(message_sent_payload["expected_ack"]).hex(),
        suggested_timeout_milliseconds=int(message_sent_payload["suggested_timeout"]),
    )


def parse_device_information(device_information_payload: dict[str, Any]) -> DeviceInformation:
    return DeviceInformation(
        protocol_version=int(device_information_payload.get("fw ver", 0)),
        firmware_version=str(device_information_payload.get("ver", "")),
        firmware_build=str(device_information_payload.get("fw_build", "")),
        model=str(device_information_payload.get("model", "")),
        maximum_contacts=int(device_information_payload.get("max_contacts", 0)),
        client_repeat=bool(device_information_payload.get("repeat", False)),
        path_hash_size=int(device_information_payload.get("path_hash_mode", 0)) + 1,
    )


def parse_self_information(self_information_payload: dict[str, Any]) -> SelfInformation:
    telemetry_modes = (
        (int(self_information_payload.get("telemetry_mode_env", 0)) << 4)
        | (int(self_information_payload.get("telemetry_mode_loc", 0)) << 2)
        | int(self_information_payload.get("telemetry_mode_base", 0))
    )
    return SelfInformation(
        public_key=str(self_information_payload["public_key"]).lower(),
        name=str(self_information_payload.get("name", "")),
        transmit_power_dbm=decode_signed_byte(int(self_information_payload.get("tx_power", 0))),
        maximum_transmit_power_dbm=decode_signed_byte(int(self_information_payload.get("max_tx_power", 0))),
        radio_frequency_kilohertz=round(float(self_information_payload.get("radio_freq", 0)) * 1000),
        radio_bandwidth_hertz=round(float(self_information_payload.get("radio_bw", 0)) * 1000),
        radio_spreading_factor=int(self_information_payload.get("radio_sf", 0)),
        radio_coding_rate=int(self_information_payload.get("radio_cr", 0)),
        manual_add_contacts=bool(self_information_payload.get("manual_add_contacts", False)),
        multi_acks=int(self_information_payload.get("multi_acks", 0)),
        advert_location_policy=int(self_information_payload.get("adv_loc_policy", 0)),
        telemetry_modes=telemetry_modes,
    )


def decode_signed_byte(unsigned_value: int) -> int:
    """The library reads the power bytes unsigned; the firmware writes them as int8."""
    return int.from_bytes(bytes([unsigned_value & 0xFF]), "little", signed=True)
