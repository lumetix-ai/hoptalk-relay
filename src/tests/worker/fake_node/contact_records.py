"""A contact as the companion firmware stores it, and its 148-byte RESP_CODE_CONTACT / PUSH_CODE_NEW_ADVERT frame."""

import dataclasses
from dataclasses import dataclass

from tests.worker.fake_node.frames import (
    CONTACT_NAME_FIELD_BYTES,
    MAXIMUM_PATH_BYTES,
    OUT_PATH_UNKNOWN,
    PUBLIC_KEY_BYTES,
    NodeType,
    decode_signed_32,
    decode_unsigned_32,
    encode_signed_32,
    encode_unsigned_32,
    fixed_width_text,
    text_before_first_nul,
)

HOP_COUNT_MASK = 0x3F
HASH_SIZE_SHIFT = 6
RESERVED_HASH_SIZE = 4
# Code, key, type, flags, path length, path, name and advert timestamp: the part of an
# ADD_UPDATE_CONTACT frame the firmware always reads, whatever the frame's length.
ADD_UPDATE_CONTACT_FIXED_PART_BYTES = 1 + PUBLIC_KEY_BYTES + 3 + MAXIMUM_PATH_BYTES + CONTACT_NAME_FIELD_BYTES + 4
ADD_UPDATE_CONTACT_WITH_LOCATION_BYTES = ADD_UPDATE_CONTACT_FIXED_PART_BYTES + 8
ADD_UPDATE_CONTACT_WITH_LAST_MODIFIED_BYTES = ADD_UPDATE_CONTACT_WITH_LOCATION_BYTES + 4


def encode_path_length(*, hop_count: int, path_hash_size: int) -> int:
    """The path length byte: bits 0-5 hop count, bits 6-7 hash size minus one."""
    return hop_count | ((path_hash_size - 1) << HASH_SIZE_SHIFT)


def decode_hop_count(path_length: int) -> int:
    return path_length & HOP_COUNT_MASK


def decode_path_hash_size(path_length: int) -> int:
    return (path_length >> HASH_SIZE_SHIFT) + 1


def path_length_is_valid(path_length: int) -> bool:
    path_hash_size = decode_path_hash_size(path_length)
    if path_hash_size == RESERVED_HASH_SIZE:
        return False
    return decode_hop_count(path_length) * path_hash_size <= MAXIMUM_PATH_BYTES


def path_byte_count(path_length: int) -> int:
    return decode_hop_count(path_length) * decode_path_hash_size(path_length)


def contact_name_field(name: bytes) -> bytes:
    return name[:CONTACT_NAME_FIELD_BYTES].ljust(CONTACT_NAME_FIELD_BYTES, b"\x00")


@dataclass(frozen=True, kw_only=True)
class ContactRecord:
    public_key: bytes
    node_type: int = NodeType.CHAT
    flags: int = 0
    # OUT_PATH_UNKNOWN (0xFF) means no stored route: the node floods to this contact.
    out_path_length: int = OUT_PATH_UNKNOWN
    # Always the full 64-byte field; a route reset changes only out_path_length.
    out_path: bytes = bytes(MAXIMUM_PATH_BYTES)
    # The raw 32-byte name field, NUL-padded.
    name_field: bytes = bytes(CONTACT_NAME_FIELD_BYTES)
    last_advert_timestamp: int = 0
    latitude_microdegrees: int = 0
    longitude_microdegrees: int = 0
    last_modified: int = 0

    @classmethod
    def create(
        cls,
        *,
        public_key: bytes,
        name: str | bytes,
        node_type: int = NodeType.CHAT,
        last_advert_timestamp: int = 0,
        last_modified: int = 0,
    ) -> ContactRecord:
        name_bytes = name.encode() if isinstance(name, str) else name
        return cls(
            public_key=public_key,
            node_type=node_type,
            name_field=contact_name_field(name_bytes),
            last_advert_timestamp=last_advert_timestamp,
            last_modified=last_modified,
        )

    @property
    def name(self) -> bytes:
        return text_before_first_nul(self.name_field)

    @property
    def has_known_route(self) -> bool:
        return self.out_path_length != OUT_PATH_UNKNOWN

    @property
    def hop_count(self) -> int:
        return decode_hop_count(self.out_path_length)

    @property
    def path_hash_size(self) -> int:
        return decode_path_hash_size(self.out_path_length)

    @property
    def route_path(self) -> bytes:
        """The stored route's repeater hashes, in the order from this node toward the contact."""
        if not self.has_known_route:
            return b""
        return self.out_path[: path_byte_count(self.out_path_length)]

    def with_route(self, *, route_path: bytes, path_hash_size: int, last_modified: int) -> ContactRecord:
        return dataclasses.replace(
            self,
            out_path_length=encode_path_length(
                hop_count=len(route_path) // path_hash_size, path_hash_size=path_hash_size
            ),
            out_path=route_path.ljust(MAXIMUM_PATH_BYTES, b"\x00"),
            last_modified=last_modified,
        )

    def without_route(self) -> ContactRecord:
        return dataclasses.replace(self, out_path_length=OUT_PATH_UNKNOWN)

    def to_frame(self, frame_code: int) -> bytes:
        return (
            bytes([frame_code])
            + self.public_key
            + bytes([self.node_type, self.flags, self.out_path_length])
            + self.out_path
            + fixed_width_text(self.name_field, CONTACT_NAME_FIELD_BYTES)
            + encode_unsigned_32(self.last_advert_timestamp)
            + encode_signed_32(self.latitude_microdegrees)
            + encode_signed_32(self.longitude_microdegrees)
            + encode_unsigned_32(self.last_modified)
        )

    @classmethod
    def from_add_update_frame(cls, frame: bytes, *, fallback_last_modified: int) -> ContactRecord:
        """Decode an ADD_UPDATE_CONTACT frame the way updateContactFromFrame does.

        Location needs a frame of at least 144 bytes and last_modified one of 148; without it the
        node's clock is used. The frame must hold the 136-byte fixed part.
        """
        offset = 1
        public_key = frame[offset : offset + PUBLIC_KEY_BYTES]
        offset += PUBLIC_KEY_BYTES
        node_type, flags, out_path_length = frame[offset], frame[offset + 1], frame[offset + 2]
        offset += 3
        out_path = frame[offset : offset + MAXIMUM_PATH_BYTES]
        offset += MAXIMUM_PATH_BYTES
        name_field = frame[offset : offset + CONTACT_NAME_FIELD_BYTES]
        offset += CONTACT_NAME_FIELD_BYTES
        last_advert_timestamp = decode_unsigned_32(frame, offset)
        latitude_microdegrees = 0
        longitude_microdegrees = 0
        last_modified = fallback_last_modified
        if len(frame) >= ADD_UPDATE_CONTACT_WITH_LOCATION_BYTES:
            latitude_microdegrees = decode_signed_32(frame, ADD_UPDATE_CONTACT_FIXED_PART_BYTES)
            longitude_microdegrees = decode_signed_32(frame, ADD_UPDATE_CONTACT_FIXED_PART_BYTES + 4)
        if len(frame) >= ADD_UPDATE_CONTACT_WITH_LAST_MODIFIED_BYTES:
            last_modified = decode_unsigned_32(frame, ADD_UPDATE_CONTACT_WITH_LOCATION_BYTES)
        return cls(
            public_key=public_key,
            node_type=node_type,
            flags=flags,
            out_path_length=out_path_length,
            out_path=out_path,
            name_field=name_field,
            last_advert_timestamp=last_advert_timestamp,
            latitude_microdegrees=latitude_microdegrees,
            longitude_microdegrees=longitude_microdegrees,
            last_modified=last_modified,
        )
