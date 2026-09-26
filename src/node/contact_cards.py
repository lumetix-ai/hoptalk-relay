"""MeshCore contact cards: "meshcore://" followed by a signed ADVERT packet in hex.

The relay verifies the Ed25519 signature itself (pycryptodome) before a pasted card may become
a contact, and checks that its own exported card carries the configured key.

Card layout (firmware v1.17.1, little-endian): header (payload type in bits 2-5, version in
bits 6-7, route type in bits 0-1); four transport-code bytes for the two transport route types;
path_len (hop count in bits 0-5, hash size minus one in bits 6-7) and the path; then the
advert: public key (32), timestamp (4), signature (64) over key, timestamp and app data, and
at most 32 bytes of app data: flags (node type in the low nibble), an optional location, two
optional feature words and the name.
"""

import re
from dataclasses import dataclass
from enum import IntEnum

from Crypto.Signature import eddsa

CONTACT_CARD_URI_PREFIX = "meshcore://"
CONTACT_NAME_MAXIMUM_BYTES = 31

PAYLOAD_TYPE_ADVERT = 0x04
SUPPORTED_PAYLOAD_VERSION = 0
ROUTE_TYPES_WITH_TRANSPORT_CODES = frozenset({0x00, 0x03})
TRANSPORT_CODES_SIZE = 4
MAXIMUM_PATH_SIZE = 64
# Path hash sizes are 1 to 3 bytes; the fourth encoding is reserved.
RESERVED_PATH_HASH_SIZE = 4

PUBLIC_KEY_SIZE = 32
TIMESTAMP_SIZE = 4
SIGNATURE_SIZE = 64
# Receivers clamp app data to this size before they verify the signature.
MAXIMUM_APP_DATA_SIZE = 32
SIGNATURE_OFFSET = PUBLIC_KEY_SIZE + TIMESTAMP_SIZE
APP_DATA_OFFSET = SIGNATURE_OFFSET + SIGNATURE_SIZE

FLAG_HAS_LOCATION = 0x10
FLAG_HAS_FEATURE_1 = 0x20
FLAG_HAS_FEATURE_2 = 0x40
FLAG_HAS_NAME = 0x80
NODE_TYPE_MASK = 0x0F
LOCATION_SIZE = 8
FEATURE_SIZE = 2

WHITESPACE_PATTERN = re.compile(r"\s+")


class MeshCoreNodeType(IntEnum):
    """The type in an advert's flags; only a chat node can be a contact of the relay."""

    NONE = 0
    CHAT = 1
    REPEATER = 2
    ROOM = 3
    SENSOR = 4


NODE_TYPE_DESCRIPTIONS = {
    MeshCoreNodeType.NONE: "node without a type",
    MeshCoreNodeType.CHAT: "chat node",
    MeshCoreNodeType.REPEATER: "repeater",
    MeshCoreNodeType.ROOM: "room server",
    MeshCoreNodeType.SENSOR: "sensor",
}


class InvalidContactCardError(ValueError):
    """The text is not a well-formed card or its signature does not verify; the message says which."""


@dataclass(frozen=True, kw_only=True)
class ContactCard:
    # Lower-case hex of the 32-byte key.
    public_key: str
    advert_timestamp: int
    node_type: int
    # Empty when the advert carries no name.
    name: str
    latitude_microdegrees: int
    longitude_microdegrees: int
    # The card as pasted without whitespace, kept on the contact for reference.
    card_uri: str


def describe_node_type(node_type: int) -> str:
    try:
        return NODE_TYPE_DESCRIPTIONS[MeshCoreNodeType(node_type)]
    except ValueError:
        return f"node of unknown type {node_type}"


def parse_contact_card_uri(contact_card_uri: str) -> ContactCard:
    """Decode the card and verify its signature, raising InvalidContactCardError otherwise.

    A card of another node type parses; whether it may become a contact is for the contact
    services to decide (directory.contacts).
    """
    normalized_card_uri = WHITESPACE_PATTERN.sub("", contact_card_uri)
    card_bytes = decode_card_bytes(normalized_card_uri)
    advert_payload = extract_advert_payload(card_bytes)

    public_key = advert_payload[:PUBLIC_KEY_SIZE]
    timestamp_bytes = advert_payload[PUBLIC_KEY_SIZE:SIGNATURE_OFFSET]
    signature = advert_payload[SIGNATURE_OFFSET:APP_DATA_OFFSET]
    app_data = advert_payload[APP_DATA_OFFSET : APP_DATA_OFFSET + MAXIMUM_APP_DATA_SIZE]
    verify_advert_signature(public_key, timestamp_bytes + app_data, signature)

    advert_details = parse_app_data(app_data)
    return ContactCard(
        public_key=public_key.hex(),
        advert_timestamp=int.from_bytes(timestamp_bytes, "little"),
        node_type=advert_details.node_type,
        name=advert_details.name,
        latitude_microdegrees=advert_details.latitude_microdegrees,
        longitude_microdegrees=advert_details.longitude_microdegrees,
        card_uri=normalized_card_uri,
    )


def decode_card_bytes(normalized_card_uri: str) -> bytes:
    if not normalized_card_uri.lower().startswith(CONTACT_CARD_URI_PREFIX):
        raise InvalidContactCardError(f"A contact card starts with {CONTACT_CARD_URI_PREFIX}.")

    card_hex = normalized_card_uri[len(CONTACT_CARD_URI_PREFIX) :]
    try:
        card_bytes = bytes.fromhex(card_hex)
    except ValueError as decoding_error:
        raise InvalidContactCardError(
            f"The text after {CONTACT_CARD_URI_PREFIX} must be hexadecimal digits."
        ) from decoding_error

    if not card_bytes:
        raise InvalidContactCardError("The card is empty.")
    return card_bytes


def extract_advert_payload(card_bytes: bytes) -> bytes:
    header = card_bytes[0]
    route_type = header & 0x03
    payload_type = (header >> 2) & 0x0F
    payload_version = header >> 6
    if payload_type != PAYLOAD_TYPE_ADVERT:
        raise InvalidContactCardError(f"This is a MeshCore packet of type {payload_type}, not an advert.")
    if payload_version != SUPPORTED_PAYLOAD_VERSION:
        raise InvalidContactCardError(f"Adverts of payload version {payload_version} are not supported.")

    offset = 1
    if route_type in ROUTE_TYPES_WITH_TRANSPORT_CODES:
        offset += TRANSPORT_CODES_SIZE
    if len(card_bytes) <= offset:
        raise InvalidContactCardError("The card ends before its path length.")

    encoded_path_length = card_bytes[offset]
    offset += 1
    path_hash_size = (encoded_path_length >> 6) + 1
    path_hash_count = encoded_path_length & 0x3F
    path_size = path_hash_size * path_hash_count
    if path_hash_size == RESERVED_PATH_HASH_SIZE or path_size > MAXIMUM_PATH_SIZE:
        raise InvalidContactCardError("The card's path length is not valid.")
    offset += path_size

    advert_payload = card_bytes[offset:]
    if len(advert_payload) < APP_DATA_OFFSET + 1:
        raise InvalidContactCardError("The card is too short to hold a signed advert.")
    return advert_payload


def verify_advert_signature(public_key: bytes, signed_fields: bytes, signature: bytes) -> None:
    try:
        verifier = eddsa.new(eddsa.import_public_key(public_key), "rfc8032")
        verifier.verify(public_key + signed_fields, signature)
    except ValueError as verification_error:
        raise InvalidContactCardError(
            "The card's signature does not verify: it was changed or copied incompletely."
        ) from verification_error


@dataclass(frozen=True, kw_only=True)
class AdvertDetails:
    node_type: int
    name: str
    latitude_microdegrees: int
    longitude_microdegrees: int


def parse_app_data(app_data: bytes) -> AdvertDetails:
    flags = app_data[0]
    offset = 1
    latitude_microdegrees = 0
    longitude_microdegrees = 0
    if flags & FLAG_HAS_LOCATION:
        if len(app_data) < offset + LOCATION_SIZE:
            raise InvalidContactCardError("The card announces a location but does not hold one.")
        latitude_microdegrees = int.from_bytes(app_data[offset : offset + 4], "little", signed=True)
        longitude_microdegrees = int.from_bytes(app_data[offset + 4 : offset + 8], "little", signed=True)
        offset += LOCATION_SIZE
    if flags & FLAG_HAS_FEATURE_1:
        offset += FEATURE_SIZE
    if flags & FLAG_HAS_FEATURE_2:
        offset += FEATURE_SIZE

    name = ""
    if flags & FLAG_HAS_NAME:
        name_bytes = app_data[offset:].split(b"\x00", 1)[0]
        # Bytes that are not valid UTF-8 are dropped, as the companion library does.
        name = name_bytes.decode("utf-8", errors="ignore")

    return AdvertDetails(
        node_type=flags & NODE_TYPE_MASK,
        name=truncate_contact_name(name),
        latitude_microdegrees=latitude_microdegrees,
        longitude_microdegrees=longitude_microdegrees,
    )


def truncate_contact_name(name: str) -> str:
    """At most CONTACT_NAME_MAXIMUM_BYTES of UTF-8, cut at a character boundary."""
    encoded_name = name.encode("utf-8")[:CONTACT_NAME_MAXIMUM_BYTES]
    return encoded_name.decode("utf-8", errors="ignore")
