"""Byte layouts of the MeshCore companion protocol, as companion firmware v1.17.1 writes and reads them.

Every builder returns the frame payload without the serial header; `frame_node_to_host` and
`HostToNodeDeframer` add and remove the `3E len16` and `3C len16` headers.
"""

from enum import IntEnum

MAXIMUM_FRAME_BYTES = 176
HOST_TO_NODE_FRAME_MARKER = 0x3C
NODE_TO_HOST_FRAME_MARKER = 0x3E

PUBLIC_KEY_BYTES = 32
PRIVATE_KEY_BYTES = 64
PUBLIC_KEY_PREFIX_BYTES = 6
SIGNATURE_BYTES = 64
ACKNOWLEDGEMENT_CODE_BYTES = 4
MAXIMUM_PATH_BYTES = 64
CONTACT_NAME_FIELD_BYTES = 32
NODE_NAME_MAXIMUM_BYTES = 31
CHANNEL_NAME_FIELD_BYTES = 32
CHANNEL_SECRET_BYTES = 16
MAXIMUM_DIRECT_MESSAGE_TEXT_BYTES = 160
# An attempt number above 3 is appended to the plaintext, which costs two bytes of text.
MAXIMUM_DIRECT_MESSAGE_TEXT_BYTES_WITH_EXTENDED_ATTEMPT = 158

# Stored as a contact's out_path_len: no route is known, so the node floods.
OUT_PATH_UNKNOWN = 0xFF
# Written as the path length of a received message that arrived over a direct route.
DIRECT_ARRIVAL_PATH_LENGTH = 0xFF

BUILD_DATE_FIELD_BYTES = 12
MANUFACTURER_NAME_FIELD_BYTES = 40
FIRMWARE_VERSION_FIELD_BYTES = 20

# The Public channel every node pre-configures in slot 0 (PUBLIC_GROUP_PSK, base64 "izOH6cXN6mrJ5e26oRXNcg==").
PUBLIC_CHANNEL_NAME = b"Public"
PUBLIC_CHANNEL_SECRET = bytes.fromhex("8b3387e9c5cdea6ac9e5edbaa115cd72")

REBOOT_COMMAND_SUFFIX = b"reboot"
FACTORY_RESET_COMMAND_SUFFIX = b"reset"


class CommandCode(IntEnum):
    APP_START = 0x01
    SEND_TEXT_MESSAGE = 0x02
    SEND_CHANNEL_TEXT_MESSAGE = 0x03
    GET_CONTACTS = 0x04
    GET_DEVICE_TIME = 0x05
    SET_DEVICE_TIME = 0x06
    SEND_SELF_ADVERT = 0x07
    SET_ADVERT_NAME = 0x08
    ADD_UPDATE_CONTACT = 0x09
    SYNC_NEXT_MESSAGE = 0x0A
    SET_RADIO_PARAMETERS = 0x0B
    SET_RADIO_TRANSMIT_POWER = 0x0C
    RESET_PATH = 0x0D
    SET_ADVERT_LATITUDE_LONGITUDE = 0x0E
    REMOVE_CONTACT = 0x0F
    EXPORT_CONTACT = 0x11
    IMPORT_CONTACT = 0x12
    REBOOT = 0x13
    GET_BATTERY_AND_STORAGE = 0x14
    SET_TUNING_PARAMETERS = 0x15
    DEVICE_QUERY = 0x16
    EXPORT_PRIVATE_KEY = 0x17
    IMPORT_PRIVATE_KEY = 0x18
    GET_CONTACT_BY_KEY = 0x1E
    GET_CHANNEL = 0x1F
    SET_CHANNEL = 0x20
    SET_OTHER_PARAMETERS = 0x26
    GET_TUNING_PARAMETERS = 0x2B
    FACTORY_RESET = 0x33
    SET_AUTO_ADD_CONFIGURATION = 0x3A
    GET_AUTO_ADD_CONFIGURATION = 0x3B
    SET_PATH_HASH_MODE = 0x3D


class ResponseCode(IntEnum):
    OK = 0x00
    ERROR = 0x01
    CONTACTS_START = 0x02
    CONTACT = 0x03
    END_OF_CONTACTS = 0x04
    SELF_INFO = 0x05
    SENT = 0x06
    CONTACT_MESSAGE_RECEIVED = 0x07
    CHANNEL_MESSAGE_RECEIVED = 0x08
    CURRENT_TIME = 0x09
    NO_MORE_MESSAGES = 0x0A
    EXPORT_CONTACT = 0x0B
    BATTERY_AND_STORAGE = 0x0C
    DEVICE_INFO = 0x0D
    PRIVATE_KEY = 0x0E
    DISABLED = 0x0F
    CONTACT_MESSAGE_RECEIVED_VERSION_3 = 0x10
    CHANNEL_MESSAGE_RECEIVED_VERSION_3 = 0x11
    CHANNEL_INFO = 0x12
    TUNING_PARAMETERS = 0x17
    AUTO_ADD_CONFIGURATION = 0x19
    CHANNEL_DATA_RECEIVED = 0x1B


class PushCode(IntEnum):
    ADVERT = 0x80
    PATH_UPDATED = 0x81
    SEND_CONFIRMED = 0x82
    MESSAGES_WAITING = 0x83
    LOG_RECEIVED_DATA = 0x88
    NEW_ADVERT = 0x8A
    CONTACT_DELETED = 0x8F
    CONTACTS_FULL = 0x90


class FirmwareErrorCode(IntEnum):
    UNSUPPORTED_COMMAND = 1
    NOT_FOUND = 2
    TABLE_FULL = 3
    BAD_STATE = 4
    FILE_INPUT_OUTPUT_ERROR = 5
    ILLEGAL_ARGUMENT = 6


class TextType(IntEnum):
    PLAIN = 0
    COMMAND_LINE_DATA = 1
    SIGNED_PLAIN = 2


class NodeType(IntEnum):
    NONE = 0
    CHAT = 1
    REPEATER = 2
    ROOM = 3
    SENSOR = 4


CHANNEL_FRAME_CODES = frozenset(
    {
        ResponseCode.CHANNEL_MESSAGE_RECEIVED,
        ResponseCode.CHANNEL_MESSAGE_RECEIVED_VERSION_3,
        ResponseCode.CHANNEL_DATA_RECEIVED,
    }
)
DIRECT_MESSAGE_FRAME_CODES = frozenset(
    {ResponseCode.CONTACT_MESSAGE_RECEIVED, ResponseCode.CONTACT_MESSAGE_RECEIVED_VERSION_3}
)


def encode_unsigned_16(value: int) -> bytes:
    return value.to_bytes(2, "little", signed=False)


def encode_unsigned_32(value: int) -> bytes:
    return value.to_bytes(4, "little", signed=False)


def encode_signed_32(value: int) -> bytes:
    return value.to_bytes(4, "little", signed=True)


def encode_signed_8(value: int) -> bytes:
    return value.to_bytes(1, "little", signed=True)


def decode_unsigned_32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little", signed=False)


def decode_signed_32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little", signed=True)


def fixed_width_text(text: bytes, field_bytes: int) -> bytes:
    """A NUL-terminated text in a fixed field, as the firmware's strzcpy writes it."""
    terminated_text = text.split(b"\x00", 1)[0][: field_bytes - 1]
    return terminated_text.ljust(field_bytes, b"\x00")


def text_before_first_nul(data: bytes) -> bytes:
    return data.split(b"\x00", 1)[0]


def frame_node_to_host(frame: bytes) -> bytes:
    return bytes([NODE_TO_HOST_FRAME_MARKER]) + encode_unsigned_16(len(frame)) + frame


def frame_host_to_node(frame: bytes) -> bytes:
    return bytes([HOST_TO_NODE_FRAME_MARKER]) + encode_unsigned_16(len(frame)) + frame


class HostToNodeDeframer:
    """The node's reader of `3C len16 payload` frames (ArduinoSerialInterface::checkRecvFrame).

    It skips bytes until `<`, has no inter-byte timeout, and keeps only the first 176 bytes of a
    longer frame while still consuming the declared length.
    """

    def __init__(self) -> None:
        self._is_inside_frame = False
        self._header_bytes = bytearray()
        self._declared_length = 0
        self._received_payload = bytearray()
        self._received_payload_length = 0

    def reset(self) -> None:
        self._is_inside_frame = False
        self._header_bytes.clear()
        self._declared_length = 0
        self._received_payload.clear()
        self._received_payload_length = 0

    def feed(self, data: bytes) -> list[bytes]:
        completed_frames: list[bytes] = []
        for byte in data:
            completed_frame = self._accept_byte(byte)
            if completed_frame is not None:
                completed_frames.append(completed_frame)
        return completed_frames

    def _accept_byte(self, byte: int) -> bytes | None:
        if not self._is_inside_frame:
            if byte == HOST_TO_NODE_FRAME_MARKER:
                self._is_inside_frame = True
                self._header_bytes.clear()
            return None

        if len(self._header_bytes) < 2:
            self._header_bytes.append(byte)
            if len(self._header_bytes) == 2:
                self._start_payload()
            return None

        return self._accept_payload_byte(byte)

    def _start_payload(self) -> None:
        self._declared_length = int.from_bytes(self._header_bytes, "little")
        self._received_payload.clear()
        self._received_payload_length = 0
        if self._declared_length == 0:
            self._is_inside_frame = False

    def _accept_payload_byte(self, byte: int) -> bytes | None:
        if self._received_payload_length < MAXIMUM_FRAME_BYTES:
            self._received_payload.append(byte)
        self._received_payload_length += 1
        if self._received_payload_length < self._declared_length:
            return None
        self._is_inside_frame = False
        return bytes(self._received_payload)


def ok_frame() -> bytes:
    return bytes([ResponseCode.OK])


def error_frame(error_code: FirmwareErrorCode) -> bytes:
    return bytes([ResponseCode.ERROR, error_code])


def private_key_frame(private_key: bytes) -> bytes:
    return bytes([ResponseCode.PRIVATE_KEY]) + private_key


def disabled_frame() -> bytes:
    """The answer of a firmware built without the command (ENABLE_PRIVATE_KEY_EXPORT or _IMPORT)."""
    return bytes([ResponseCode.DISABLED])


def contacts_start_frame(total_contact_count: int) -> bytes:
    return bytes([ResponseCode.CONTACTS_START]) + encode_unsigned_32(total_contact_count)


def end_of_contacts_frame(most_recent_last_modified: int) -> bytes:
    return bytes([ResponseCode.END_OF_CONTACTS]) + encode_unsigned_32(most_recent_last_modified)


def message_sent_frame(
    *, sent_by_flood: bool, expected_acknowledgement: bytes, suggested_timeout_milliseconds: int
) -> bytes:
    return (
        bytes([ResponseCode.SENT, 1 if sent_by_flood else 0])
        + expected_acknowledgement
        + encode_unsigned_32(suggested_timeout_milliseconds)
    )


def current_time_frame(current_time: int) -> bytes:
    return bytes([ResponseCode.CURRENT_TIME]) + encode_unsigned_32(current_time)


def no_more_messages_frame() -> bytes:
    return bytes([ResponseCode.NO_MORE_MESSAGES])


def export_contact_frame(advert_packet: bytes) -> bytes:
    return bytes([ResponseCode.EXPORT_CONTACT]) + advert_packet


def auto_add_configuration_frame(*, auto_add_configuration: int, auto_add_maximum_hops: int) -> bytes:
    return bytes([ResponseCode.AUTO_ADD_CONFIGURATION, auto_add_configuration, auto_add_maximum_hops])


def tuning_parameters_frame(*, receive_delay_base_thousandths: int, airtime_factor_thousandths: int) -> bytes:
    return (
        bytes([ResponseCode.TUNING_PARAMETERS])
        + encode_unsigned_32(receive_delay_base_thousandths)
        + encode_unsigned_32(airtime_factor_thousandths)
    )


def battery_and_storage_frame(*, battery_millivolts: int, used_kilobytes: int, total_kilobytes: int) -> bytes:
    return (
        bytes([ResponseCode.BATTERY_AND_STORAGE])
        + encode_unsigned_16(battery_millivolts)
        + encode_unsigned_32(used_kilobytes)
        + encode_unsigned_32(total_kilobytes)
    )


def channel_info_frame(*, channel_index: int, channel_name: bytes, channel_secret: bytes) -> bytes:
    return (
        bytes([ResponseCode.CHANNEL_INFO, channel_index])
        + fixed_width_text(channel_name, CHANNEL_NAME_FIELD_BYTES)
        + channel_secret
    )


def device_info_frame(
    *,
    protocol_version_code: int,
    maximum_contacts: int,
    maximum_group_channels: int,
    bluetooth_pin: int,
    build_date: str,
    manufacturer_name: str,
    firmware_version: str,
    client_repeat_enabled: bool,
    path_hash_mode: int,
) -> bytes:
    return (
        bytes([ResponseCode.DEVICE_INFO, protocol_version_code, maximum_contacts // 2, maximum_group_channels])
        + encode_unsigned_32(bluetooth_pin)
        + fixed_width_text(build_date.encode(), BUILD_DATE_FIELD_BYTES)
        + fixed_width_text(manufacturer_name.encode(), MANUFACTURER_NAME_FIELD_BYTES)
        + fixed_width_text(firmware_version.encode(), FIRMWARE_VERSION_FIELD_BYTES)
        + bytes([1 if client_repeat_enabled else 0, path_hash_mode])
    )


def self_info_frame(
    *,
    transmit_power_dbm: int,
    maximum_transmit_power_dbm: int,
    public_key: bytes,
    latitude_microdegrees: int,
    longitude_microdegrees: int,
    multi_acknowledgements: int,
    advert_location_policy: int,
    telemetry_modes: int,
    manual_add_contacts: int,
    frequency_kilohertz: int,
    bandwidth_hertz: int,
    spreading_factor: int,
    coding_rate: int,
    node_name: bytes,
) -> bytes:
    return (
        bytes([ResponseCode.SELF_INFO, NodeType.CHAT])
        + encode_signed_8(transmit_power_dbm)
        + bytes([maximum_transmit_power_dbm])
        + public_key
        + encode_signed_32(latitude_microdegrees)
        + encode_signed_32(longitude_microdegrees)
        + bytes([multi_acknowledgements, advert_location_policy, telemetry_modes, manual_add_contacts])
        + encode_unsigned_32(frequency_kilohertz)
        + encode_unsigned_32(bandwidth_hertz)
        + bytes([spreading_factor, coding_rate])
        + node_name
    )


def truncate_to_frame(frame_header: bytes, text: bytes) -> bytes:
    """The node cuts a received text byte-wise so that the frame fits 176 bytes."""
    return frame_header + text[: MAXIMUM_FRAME_BYTES - len(frame_header)]


def contact_message_frame(
    *,
    uses_version_3_layout: bool,
    signal_to_noise_quarters: int,
    sender_public_key_prefix: bytes,
    path_length: int,
    text_type: int,
    sender_timestamp: int,
    text: bytes,
) -> bytes:
    if uses_version_3_layout:
        frame_header = (
            bytes([ResponseCode.CONTACT_MESSAGE_RECEIVED_VERSION_3])
            + encode_signed_8(signal_to_noise_quarters)
            + bytes(2)
        )
    else:
        frame_header = bytes([ResponseCode.CONTACT_MESSAGE_RECEIVED])
    frame_header += sender_public_key_prefix + bytes([path_length, text_type]) + encode_unsigned_32(sender_timestamp)
    return truncate_to_frame(frame_header, text)


def channel_message_frame(
    *,
    uses_version_3_layout: bool,
    signal_to_noise_quarters: int,
    channel_index: int,
    path_length: int,
    sender_timestamp: int,
    text: bytes,
) -> bytes:
    if uses_version_3_layout:
        frame_header = (
            bytes([ResponseCode.CHANNEL_MESSAGE_RECEIVED_VERSION_3])
            + encode_signed_8(signal_to_noise_quarters)
            + bytes(2)
        )
    else:
        frame_header = bytes([ResponseCode.CHANNEL_MESSAGE_RECEIVED])
    frame_header += bytes([channel_index, path_length, TextType.PLAIN]) + encode_unsigned_32(sender_timestamp)
    return truncate_to_frame(frame_header, text)


def channel_data_frame(
    *,
    signal_to_noise_quarters: int,
    channel_index: int,
    path_length: int,
    data_type: int,
    payload: bytes,
) -> bytes:
    return (
        bytes([ResponseCode.CHANNEL_DATA_RECEIVED])
        + encode_signed_8(signal_to_noise_quarters)
        + bytes([0, 0, channel_index, path_length])
        + encode_unsigned_16(data_type)
        + bytes([len(payload)])
        + payload
    )


def public_key_push(push_code: PushCode, public_key: bytes) -> bytes:
    return bytes([push_code]) + public_key


def send_confirmed_push(*, acknowledgement_code: bytes, round_trip_milliseconds: int) -> bytes:
    return bytes([PushCode.SEND_CONFIRMED]) + acknowledgement_code + encode_unsigned_32(round_trip_milliseconds)


def messages_waiting_push() -> bytes:
    return bytes([PushCode.MESSAGES_WAITING])


def contacts_full_push() -> bytes:
    return bytes([PushCode.CONTACTS_FULL])


def receive_log_push(*, signal_to_noise_quarters: int, received_signal_strength: int, raw_packet: bytes) -> bytes:
    return (
        bytes([PushCode.LOG_RECEIVED_DATA])
        + encode_signed_8(signal_to_noise_quarters)
        + encode_signed_8(received_signal_strength)
        + raw_packet
    )
