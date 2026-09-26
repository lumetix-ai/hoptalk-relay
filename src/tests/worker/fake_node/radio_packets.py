"""Packets the simulated radio carries between fake nodes.

Nothing is really encrypted: a packet keeps its fields in the clear and only its packet hash and
its raw bytes (for RX_LOG_DATA pushes) are derived the way the firmware derives them. The packet
hash follows the firmware's rule that matters for deduplication: the same (sender, recipient,
timestamp, attempt, text) always gives the same hash, and any change gives another.
"""

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from tests.worker.fake_node.contact_records import encode_path_length
from tests.worker.fake_node.frames import (
    ACKNOWLEDGEMENT_CODE_BYTES,
    DIRECT_ARRIVAL_PATH_LENGTH,
    TextType,
    encode_unsigned_16,
    encode_unsigned_32,
)

PACKET_HASH_BYTES = 8
CIPHER_BLOCK_BYTES = 16
MESSAGE_AUTHENTICATION_CODE_BYTES = 2
HIGHEST_ATTEMPT_IN_FLAGS = 3

PAYLOAD_TYPE_TEXT_MESSAGE = 0x02
PAYLOAD_TYPE_ACKNOWLEDGEMENT = 0x03
PAYLOAD_TYPE_ADVERT = 0x04
PAYLOAD_TYPE_GROUP_TEXT = 0x05
PAYLOAD_TYPE_GROUP_DATA = 0x06
PAYLOAD_TYPE_PATH = 0x08
PAYLOAD_TYPE_MULTIPART = 0x0A
# Mesh::createPathReturn: what fills a returned path that carries no ACK.
PATH_RETURN_FILLER_PAYLOAD_TYPE = 0xFF
PATH_RETURN_RANDOM_FILLER_BYTES = 4
PAYLOAD_TYPE_SHIFT = 2
ROUTE_TYPE_FLOOD_BITS = 0x01
ROUTE_TYPE_DIRECT_BITS = 0x02


def calculate_expected_acknowledgement(
    *, sender_timestamp: int, attempt: int, text: bytes, sender_public_key: bytes
) -> bytes:
    """sha256(timestamp | attempt & 3 | text | sender public key), first 4 bytes (BaseChatMesh::composeMsgPacket)."""
    hashed_data = (
        encode_unsigned_32(sender_timestamp) + bytes([attempt & HIGHEST_ATTEMPT_IN_FLAGS]) + text + sender_public_key
    )
    return hashlib.sha256(hashed_data).digest()[:ACKNOWLEDGEMENT_CODE_BYTES]


def pseudo_random_bytes(seed: bytes, length: int) -> bytes:
    generated = bytearray()
    counter = 0
    while len(generated) < length:
        generated += hashlib.sha256(seed + counter.to_bytes(4, "little")).digest()
        counter += 1
    return bytes(generated[:length])


def padded_cipher_length(plaintext_length: int) -> int:
    block_count = (plaintext_length + CIPHER_BLOCK_BYTES - 1) // CIPHER_BLOCK_BYTES
    return block_count * CIPHER_BLOCK_BYTES


class RouteType(Enum):
    FLOOD = "flood"
    DIRECT = "direct"


@dataclass(frozen=True, kw_only=True)
class PacketRoute:
    """How the sender sends: flood, or direct along the path it stored for the recipient."""

    route_type: RouteType
    # For a direct route, the repeater hashes toward the recipient; empty means neighbours only.
    path: bytes = b""
    # For a flood, the hash size repeaters append; for a direct route, the stored path's hash size.
    path_hash_size: int = 1

    @classmethod
    def flood(cls, *, path_hash_size: int) -> PacketRoute:
        return cls(route_type=RouteType.FLOOD, path_hash_size=path_hash_size)

    @classmethod
    def direct(cls, *, path: bytes, path_hash_size: int) -> PacketRoute:
        return cls(route_type=RouteType.DIRECT, path=path, path_hash_size=path_hash_size)

    @classmethod
    def zero_hop(cls) -> PacketRoute:
        return cls(route_type=RouteType.DIRECT)

    @property
    def is_flood(self) -> bool:
        return self.route_type is RouteType.FLOOD


@dataclass(frozen=True, kw_only=True)
class PacketArrival:
    """How a packet reached its recipient."""

    arrived_by_flood: bool
    # For a flood, the repeater hashes the packet collected on its way from the sender.
    path: bytes = b""
    path_hash_size: int = 1

    @classmethod
    def direct(cls) -> PacketArrival:
        return cls(arrived_by_flood=False)

    @classmethod
    def flood(cls, *, path: bytes, path_hash_size: int) -> PacketArrival:
        return cls(arrived_by_flood=True, path=path, path_hash_size=path_hash_size)

    @property
    def path_length(self) -> int:
        """The byte a received-message frame carries: the encoded flood path, or 0xFF for direct."""
        if not self.arrived_by_flood:
            return DIRECT_ARRIVAL_PATH_LENGTH
        return encode_path_length(hop_count=self.hop_count, path_hash_size=self.path_hash_size)

    @property
    def hop_count(self) -> int:
        return len(self.path) // self.path_hash_size


@dataclass(frozen=True, kw_only=True)
class RadioPacket(ABC):
    payload_type: ClassVar[int]

    sender_public_key: bytes
    route: PacketRoute

    @property
    def recipient_public_key(self) -> bytes | None:
        return None

    @property
    def carries_acknowledgement(self) -> bool:
        return False

    @abstractmethod
    def hash_material(self) -> bytes:
        """The bytes the packet hash covers: what the firmware's payload would be."""

    @abstractmethod
    def raw_payload(self) -> bytes:
        """The payload as a radio would hear it."""

    @property
    def packet_hash(self) -> bytes:
        return hashlib.sha256(bytes([self.payload_type]) + self.hash_material()).digest()[:PACKET_HASH_BYTES]

    def raw_packet(self, arrival: PacketArrival) -> bytes:
        """The packet as the receiving radio heard it (Packet::writeTo), for RX_LOG_DATA pushes."""
        route_bits = ROUTE_TYPE_FLOOD_BITS if arrival.arrived_by_flood else ROUTE_TYPE_DIRECT_BITS
        header = (self.payload_type << PAYLOAD_TYPE_SHIFT) | route_bits
        if arrival.arrived_by_flood:
            path_length = arrival.path_length
            path = arrival.path
        else:
            path_length = 0
            path = b""
        return bytes([header, path_length]) + path + self.raw_payload()


def pseudo_encrypted_payload(*, recipient_public_key: bytes, sender_public_key: bytes, plaintext: bytes) -> bytes:
    """Destination hash, source hash, 2-byte MAC and ciphertext, sized like the real encryption."""
    seed = recipient_public_key + sender_public_key + plaintext
    cipher_length = padded_cipher_length(len(plaintext))
    return (
        recipient_public_key[:1]
        + sender_public_key[:1]
        + pseudo_random_bytes(seed, MESSAGE_AUTHENTICATION_CODE_BYTES + cipher_length)
    )


@dataclass(frozen=True, kw_only=True)
class DirectMessagePacket(RadioPacket):
    payload_type: ClassVar[int] = PAYLOAD_TYPE_TEXT_MESSAGE

    destination_public_key: bytes
    sender_timestamp: int
    attempt: int
    text_type: int = TextType.PLAIN
    text: bytes

    @property
    def recipient_public_key(self) -> bytes | None:
        return self.destination_public_key

    @property
    def plaintext(self) -> bytes:
        flags = (self.text_type << 2) | (self.attempt & HIGHEST_ATTEMPT_IN_FLAGS)
        plaintext = encode_unsigned_32(self.sender_timestamp) + bytes([flags]) + self.text
        if self.attempt > HIGHEST_ATTEMPT_IN_FLAGS:
            plaintext += bytes([0, self.attempt])
        return plaintext

    def hash_material(self) -> bytes:
        return self.destination_public_key + self.sender_public_key + self.plaintext

    def raw_payload(self) -> bytes:
        return pseudo_encrypted_payload(
            recipient_public_key=self.destination_public_key,
            sender_public_key=self.sender_public_key,
            plaintext=self.plaintext,
        )


@dataclass(frozen=True, kw_only=True)
class AcknowledgementPayload:
    """The 6 bytes a node sends back: the 4-byte code, the extended attempt byte, and a random byte."""

    code: bytes
    attempt_byte: int
    random_byte: int

    def to_bytes(self) -> bytes:
        return self.code + bytes([self.attempt_byte, self.random_byte])


@dataclass(frozen=True, kw_only=True)
class AcknowledgementPacket(RadioPacket):
    payload_type: ClassVar[int] = PAYLOAD_TYPE_ACKNOWLEDGEMENT

    # The node whose message is acknowledged; ACK packets carry no address, the mesh uses it for routing.
    acknowledged_sender_public_key: bytes
    acknowledgement: AcknowledgementPayload

    @property
    def recipient_public_key(self) -> bytes | None:
        return self.acknowledged_sender_public_key

    @property
    def carries_acknowledgement(self) -> bool:
        return True

    def hash_material(self) -> bytes:
        return self.acknowledgement.to_bytes()

    def raw_payload(self) -> bytes:
        return self.acknowledgement.to_bytes()


@dataclass(frozen=True, kw_only=True)
class MultipartAcknowledgementPacket(AcknowledgementPacket):
    """The extra ACK copy a node with multi_acks set sends first over a direct route."""

    payload_type: ClassVar[int] = PAYLOAD_TYPE_MULTIPART

    def hash_material(self) -> bytes:
        return bytes([PAYLOAD_TYPE_ACKNOWLEDGEMENT]) + self.acknowledgement.to_bytes()

    def raw_payload(self) -> bytes:
        return bytes([PAYLOAD_TYPE_ACKNOWLEDGEMENT << 4]) + self.acknowledgement.to_bytes()


@dataclass(frozen=True, kw_only=True)
class PathReturnPacket(RadioPacket):
    """A returned path: the route the recipient should store toward the sender, maybe with an ACK."""

    payload_type: ClassVar[int] = PAYLOAD_TYPE_PATH

    destination_public_key: bytes
    returned_path: bytes
    returned_path_hash_size: int
    acknowledgement: AcknowledgementPayload | None = None
    # Without an ACK the firmware appends random bytes, so that two returns of the same path still
    # hash differently and neither is dropped as a repeat.
    random_filler: bytes = b""

    def __post_init__(self) -> None:
        if self.acknowledgement is None and len(self.random_filler) != PATH_RETURN_RANDOM_FILLER_BYTES:
            raise ValueError(
                f"A path return without an ACK carries {PATH_RETURN_RANDOM_FILLER_BYTES} random bytes, "
                f"not {len(self.random_filler)}."
            )

    @property
    def recipient_public_key(self) -> bytes | None:
        return self.destination_public_key

    @property
    def carries_acknowledgement(self) -> bool:
        return self.acknowledgement is not None

    @property
    def plaintext(self) -> bytes:
        hop_count = len(self.returned_path) // self.returned_path_hash_size
        path_length = encode_path_length(hop_count=hop_count, path_hash_size=self.returned_path_hash_size)
        plaintext = bytes([path_length]) + self.returned_path
        if self.acknowledgement is None:
            return plaintext + bytes([PATH_RETURN_FILLER_PAYLOAD_TYPE]) + self.random_filler
        return plaintext + bytes([PAYLOAD_TYPE_ACKNOWLEDGEMENT]) + self.acknowledgement.to_bytes()

    def hash_material(self) -> bytes:
        return self.destination_public_key + self.sender_public_key + self.plaintext

    def raw_payload(self) -> bytes:
        return pseudo_encrypted_payload(
            recipient_public_key=self.destination_public_key,
            sender_public_key=self.sender_public_key,
            plaintext=self.plaintext,
        )


@dataclass(frozen=True, kw_only=True)
class AdvertPacket(RadioPacket):
    payload_type: ClassVar[int] = PAYLOAD_TYPE_ADVERT

    # Public key, timestamp, signature and app data, signed by the sender.
    advert_payload: bytes

    def hash_material(self) -> bytes:
        return self.advert_payload

    def raw_payload(self) -> bytes:
        return self.advert_payload


def channel_hash(channel_secret: bytes) -> bytes:
    return hashlib.sha256(channel_secret).digest()[:1]


@dataclass(frozen=True, kw_only=True)
class ChannelMessagePacket(RadioPacket):
    payload_type: ClassVar[int] = PAYLOAD_TYPE_GROUP_TEXT

    channel_secret: bytes
    sender_timestamp: int
    text: bytes

    @property
    def plaintext(self) -> bytes:
        return encode_unsigned_32(self.sender_timestamp) + bytes([TextType.PLAIN]) + self.text

    def hash_material(self) -> bytes:
        return self.channel_secret + self.plaintext

    def raw_payload(self) -> bytes:
        cipher_length = padded_cipher_length(len(self.plaintext))
        return channel_hash(self.channel_secret) + pseudo_random_bytes(
            self.hash_material(), MESSAGE_AUTHENTICATION_CODE_BYTES + cipher_length
        )


@dataclass(frozen=True, kw_only=True)
class ChannelDataPacket(RadioPacket):
    payload_type: ClassVar[int] = PAYLOAD_TYPE_GROUP_DATA

    channel_secret: bytes
    data_type: int
    data: bytes

    @property
    def plaintext(self) -> bytes:
        return encode_unsigned_16(self.data_type) + bytes([len(self.data)]) + self.data

    def hash_material(self) -> bytes:
        return self.channel_secret + self.plaintext

    def raw_payload(self) -> bytes:
        cipher_length = padded_cipher_length(len(self.plaintext))
        return channel_hash(self.channel_secret) + pseudo_random_bytes(
            self.hash_material(), MESSAGE_AUTHENTICATION_CODE_BYTES + cipher_length
        )
