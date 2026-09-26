"""Ed25519 node identities and signed adverts: what a contact card (`meshcore://<hex>`) is made of.

The card layout is `header | path_len | path | public key | timestamp | signature | app data`, where
the signature covers `public key | timestamp | app data` (Mesh::createAdvert).

A node keeps its identity as the firmware's ed25519 library does (orlp/ed25519): a 64-byte private
key, the clamped scalar followed by the nonce prefix, both from the SHA-512 of a 32-byte seed. That
is also the form CMD_EXPORT_PRIVATE_KEY returns and CMD_IMPORT_PRIVATE_KEY takes, and an imported
key comes without its seed, so the public key is derived and adverts are signed from the expanded
key itself, with pycryptodome's Ed25519 point arithmetic. The signatures are the RFC 8032 ones.
"""

import hashlib
import random
from dataclasses import dataclass

from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa

from tests.worker.fake_node.contact_records import path_byte_count, path_length_is_valid
from tests.worker.fake_node.frames import (
    NODE_NAME_MAXIMUM_BYTES,
    PUBLIC_KEY_BYTES,
    SIGNATURE_BYTES,
    decode_signed_32,
    decode_unsigned_32,
    encode_signed_32,
    encode_unsigned_32,
)

CONTACT_CARD_URI_PREFIX = "meshcore://"
PRIVATE_KEY_SEED_BYTES = 32
MAXIMUM_ADVERT_APP_DATA_BYTES = 32
ADVERT_LOCATION_BYTES = 8
ADVERT_FIXED_PAYLOAD_BYTES = PUBLIC_KEY_BYTES + 4 + SIGNATURE_BYTES

ADVERT_FLAG_HAS_LOCATION = 0x10
ADVERT_FLAG_HAS_FEATURE_1 = 0x20
ADVERT_FLAG_HAS_FEATURE_2 = 0x40
ADVERT_FLAG_HAS_NAME = 0x80
ADVERT_NODE_TYPE_MASK = 0x0F

PAYLOAD_TYPE_ADVERT = 0x04
PAYLOAD_TYPE_SHIFT = 2
PAYLOAD_TYPE_MASK = 0x0F
ROUTE_TYPE_MASK = 0x03
ROUTE_TYPE_TRANSPORT_FLOOD = 0x00
ROUTE_TYPE_FLOOD = 0x01
ROUTE_TYPE_DIRECT = 0x02
ROUTE_TYPE_TRANSPORT_DIRECT = 0x03
TRANSPORT_CODES_BYTES = 4
# The firmware draws a new key while the first byte is one of these reserved hashes (MyMesh::begin).
RESERVED_PUBLIC_KEY_FIRST_BYTES = frozenset({0x00, 0xFF})
MAXIMUM_IDENTITY_DRAWS = 10

EXPANDED_PRIVATE_KEY_BYTES = 64
SCALAR_BYTES = 32
ED25519_CURVE_NAME = "Ed25519"
# The base point and the group order of Ed25519 (RFC 8032, section 5.1).
ED25519_BASE_POINT_X = 15112221349535400772501151409588531511454012693041857206046113283949847762202
ED25519_BASE_POINT_Y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
ED25519_GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493


def clamp_scalar(private_key: bytes) -> bytes:
    """The clamping ed25519_create_keypair and ed25519_key_exchange apply to the scalar half of the key."""
    clamped_private_key = bytearray(private_key)
    clamped_private_key[0] &= 248
    clamped_private_key[31] &= 63
    clamped_private_key[31] |= 64
    return bytes(clamped_private_key)


def expand_private_key_seed(private_key_seed: bytes) -> bytes:
    """ed25519_create_keypair: the SHA-512 of the seed, its scalar half clamped."""
    return clamp_scalar(hashlib.sha512(private_key_seed).digest())


def is_clamped_scalar(expanded_private_key: bytes) -> bool:
    """LocalIdentity::validatePrivateKey's key-exchange check passes only for a clamped scalar.

    ed25519_key_exchange clamps the scalar before it multiplies while ed25519_derive_pub does
    not, so the two shared secrets the check compares differ for any other scalar.
    """
    return expanded_private_key == clamp_scalar(expanded_private_key)


def multiply_base_point(scalar: int) -> ECC.EccPoint:
    base_point = ECC.EccPoint(ED25519_BASE_POINT_X, ED25519_BASE_POINT_Y, curve=ED25519_CURVE_NAME)
    return base_point * scalar


def encode_point(point: ECC.EccPoint) -> bytes:
    return bytes(ECC.EccKey(curve=ED25519_CURVE_NAME, point=point).export_key(format="raw"))


def derive_public_key(expanded_private_key: bytes) -> bytes:
    """ed25519_derive_pub: the scalar times the base point."""
    scalar = int.from_bytes(expanded_private_key[:SCALAR_BYTES], "little")
    return encode_point(multiply_base_point(scalar % ED25519_GROUP_ORDER))


class NodeIdentity:
    """An Ed25519 key pair in the firmware's form, drawn from a seeded random generator so tests are reproducible."""

    def __init__(self, expanded_private_key: bytes) -> None:
        if len(expanded_private_key) != EXPANDED_PRIVATE_KEY_BYTES:
            raise ValueError(f"A private key has {EXPANDED_PRIVATE_KEY_BYTES} bytes.")
        self.expanded_private_key = bytes(expanded_private_key)
        self.public_key = derive_public_key(self.expanded_private_key)

    def __repr__(self) -> str:
        return f"NodeIdentity(public_key={self.public_key.hex()[:12]}…)"

    @classmethod
    def from_seed(cls, private_key_seed: bytes) -> NodeIdentity:
        return cls(expand_private_key_seed(private_key_seed))

    @classmethod
    def generate(cls, random_generator: random.Random) -> NodeIdentity:
        identity = cls.from_seed(random_generator.randbytes(PRIVATE_KEY_SEED_BYTES))
        draws = 1
        while identity.public_key[0] in RESERVED_PUBLIC_KEY_FIRST_BYTES and draws < MAXIMUM_IDENTITY_DRAWS:
            identity = cls.from_seed(random_generator.randbytes(PRIVATE_KEY_SEED_BYTES))
            draws += 1
        return identity

    def sign(self, message: bytes) -> bytes:
        """ed25519_sign with the expanded key: r from the nonce prefix, then S = r + H(R, A, M) * scalar."""
        scalar = int.from_bytes(self.expanded_private_key[:SCALAR_BYTES], "little")
        nonce_prefix = self.expanded_private_key[SCALAR_BYTES:]
        nonce = int.from_bytes(hashlib.sha512(nonce_prefix + message).digest(), "little") % ED25519_GROUP_ORDER
        encoded_nonce_point = encode_point(multiply_base_point(nonce))
        challenge = (
            int.from_bytes(hashlib.sha512(encoded_nonce_point + self.public_key + message).digest(), "little")
            % ED25519_GROUP_ORDER
        )
        signature_scalar = (nonce + challenge * scalar) % ED25519_GROUP_ORDER
        return encoded_nonce_point + signature_scalar.to_bytes(SCALAR_BYTES, "little")


def signature_is_valid(*, public_key: bytes, message: bytes, signature: bytes) -> bool:
    try:
        verifier = eddsa.new(eddsa.import_public_key(public_key), "rfc8032")
        verifier.verify(message, signature)
    except ValueError:
        return False
    return True


def longest_valid_utf8_prefix(text: bytes, maximum_bytes: int) -> bytes:
    """The advert encoder keeps the longest prefix that is valid UTF-8 (validUtf8PrefixLength)."""
    candidate = text[:maximum_bytes]
    while candidate:
        try:
            candidate.decode("utf-8")
        except UnicodeDecodeError:
            candidate = candidate[:-1]
            continue
        return candidate
    return b""


@dataclass(frozen=True, kw_only=True)
class AdvertLocation:
    latitude_microdegrees: int
    longitude_microdegrees: int


def build_advert_app_data(*, node_type: int, name: bytes, location: AdvertLocation | None) -> bytes:
    flags = node_type
    app_data = bytearray()
    if location is not None:
        flags |= ADVERT_FLAG_HAS_LOCATION
        app_data += encode_signed_32(location.latitude_microdegrees)
        app_data += encode_signed_32(location.longitude_microdegrees)
    name_bytes = longest_valid_utf8_prefix(name.split(b"\x00", 1)[0], MAXIMUM_ADVERT_APP_DATA_BYTES - 1 - len(app_data))
    if name_bytes:
        flags |= ADVERT_FLAG_HAS_NAME
        app_data += name_bytes
    return bytes([flags]) + bytes(app_data)


def build_advert_payload(*, identity: NodeIdentity, timestamp: int, app_data: bytes) -> bytes:
    timestamp_bytes = encode_unsigned_32(timestamp)
    signature = identity.sign(identity.public_key + timestamp_bytes + app_data)
    return identity.public_key + timestamp_bytes + signature + app_data


def build_advert_packet(*, route_type: int, path_length: int, path: bytes, advert_payload: bytes) -> bytes:
    header = (PAYLOAD_TYPE_ADVERT << PAYLOAD_TYPE_SHIFT) | route_type
    return bytes([header, path_length]) + path + advert_payload


def build_self_card(
    *, identity: NodeIdentity, timestamp: int, node_type: int, name: bytes, location: AdvertLocation | None
) -> bytes:
    """What CMD_EXPORT_CONTACT returns for the node itself: a fresh flood advert with an empty path."""
    app_data = build_advert_app_data(node_type=node_type, name=name[:NODE_NAME_MAXIMUM_BYTES], location=location)
    advert_payload = build_advert_payload(identity=identity, timestamp=timestamp, app_data=app_data)
    return build_advert_packet(route_type=ROUTE_TYPE_FLOOD, path_length=0, path=b"", advert_payload=advert_payload)


def contact_card_uri(card: bytes) -> str:
    return CONTACT_CARD_URI_PREFIX + card.hex()


@dataclass(frozen=True, kw_only=True)
class ParsedAdvert:
    """An advert packet, read the way Packet::readFrom and AdvertDataParser read it."""

    route_type: int
    path_length: int
    path: bytes
    public_key: bytes
    timestamp: int
    signature: bytes
    # Clamped to 32 bytes, as the firmware does before verifying.
    app_data: bytes
    advert_payload: bytes

    @property
    def signed_message(self) -> bytes:
        return self.public_key + encode_unsigned_32(self.timestamp) + self.app_data

    @property
    def signature_is_valid(self) -> bool:
        return signature_is_valid(public_key=self.public_key, message=self.signed_message, signature=self.signature)

    @property
    def flags(self) -> int:
        return self.app_data[0] if self.app_data else 0

    @property
    def node_type(self) -> int:
        return self.flags & ADVERT_NODE_TYPE_MASK

    @property
    def location(self) -> AdvertLocation | None:
        if not self.flags & ADVERT_FLAG_HAS_LOCATION or len(self.app_data) < 1 + ADVERT_LOCATION_BYTES:
            return None
        return AdvertLocation(
            latitude_microdegrees=decode_signed_32(self.app_data, 1),
            longitude_microdegrees=decode_signed_32(self.app_data, 5),
        )

    @property
    def name(self) -> bytes:
        """Empty when the advert carries no name; such adverts are dropped by every node."""
        name_offset = 1
        if self.flags & ADVERT_FLAG_HAS_LOCATION:
            name_offset += ADVERT_LOCATION_BYTES
        if self.flags & ADVERT_FLAG_HAS_FEATURE_1:
            name_offset += 2
        if self.flags & ADVERT_FLAG_HAS_FEATURE_2:
            name_offset += 2
        if not self.flags & ADVERT_FLAG_HAS_NAME or len(self.app_data) < name_offset:
            return b""
        return self.app_data[name_offset:].split(b"\x00", 1)[0]


@dataclass(frozen=True, kw_only=True)
class AdvertPacketEnvelope:
    """What Packet::readFrom accepts: an advert header, a valid path and a non-empty payload."""

    route_type: int
    path_length: int
    path: bytes
    advert_payload: bytes


def read_advert_packet_envelope(packet: bytes) -> AdvertPacketEnvelope | None:
    if len(packet) < 2:
        return None
    header = packet[0]
    route_type = header & ROUTE_TYPE_MASK
    if (header >> PAYLOAD_TYPE_SHIFT) & PAYLOAD_TYPE_MASK != PAYLOAD_TYPE_ADVERT:
        return None
    offset = 1
    if route_type in (ROUTE_TYPE_TRANSPORT_FLOOD, ROUTE_TYPE_TRANSPORT_DIRECT):
        offset += TRANSPORT_CODES_BYTES
    if offset >= len(packet) or not path_length_is_valid(packet[offset]):
        return None
    path_length = packet[offset]
    offset += 1
    path = packet[offset : offset + path_byte_count(path_length)]
    offset += len(path)
    if offset >= len(packet):
        return None
    return AdvertPacketEnvelope(
        route_type=route_type, path_length=path_length, path=path, advert_payload=packet[offset:]
    )


def parse_advert_payload(
    advert_payload: bytes, *, route_type: int = ROUTE_TYPE_FLOOD, path_length: int = 0, path: bytes = b""
) -> ParsedAdvert | None:
    """None when the payload is too short to hold a key, a timestamp and a signature."""
    if len(advert_payload) < ADVERT_FIXED_PAYLOAD_BYTES:
        return None
    return ParsedAdvert(
        route_type=route_type,
        path_length=path_length,
        path=path,
        public_key=advert_payload[:PUBLIC_KEY_BYTES],
        timestamp=decode_unsigned_32(advert_payload, PUBLIC_KEY_BYTES),
        signature=advert_payload[PUBLIC_KEY_BYTES + 4 : ADVERT_FIXED_PAYLOAD_BYTES],
        app_data=advert_payload[
            ADVERT_FIXED_PAYLOAD_BYTES : ADVERT_FIXED_PAYLOAD_BYTES + MAXIMUM_ADVERT_APP_DATA_BYTES
        ],
        advert_payload=advert_payload,
    )


def parse_advert_packet(packet: bytes) -> ParsedAdvert | None:
    """None when the bytes are not a well-formed advert packet."""
    envelope = read_advert_packet_envelope(packet)
    if envelope is None:
        return None
    return parse_advert_payload(
        envelope.advert_payload,
        route_type=envelope.route_type,
        path_length=envelope.path_length,
        path=envelope.path,
    )


def parse_contact_card_uri(card_uri: str) -> ParsedAdvert | None:
    if not card_uri.lower().startswith(CONTACT_CARD_URI_PREFIX):
        return None
    try:
        card = bytes.fromhex(card_uri[len(CONTACT_CARD_URI_PREFIX) :].strip())
    except ValueError:
        return None
    return parse_advert_packet(card)
