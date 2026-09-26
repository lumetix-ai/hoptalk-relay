"""Contact records as the node stores them: the ADD_UPDATE_CONTACT frame, and the contacts a listing returns.

The worker writes the frame itself at its full length of 144 bytes. The firmware reads 136
bytes whatever the length, and a shorter frame gives a new contact garbage coordinates. A
contact is always added without a route (out_path_len 0xFF): the first DM floods and the reply
teaches both nodes the route, while a stale route copied from elsewhere would be used blindly.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from directory.node_sync import ContactForNode
from node.contact_cards import truncate_contact_name

ADD_UPDATE_CONTACT_COMMAND_CODE = 0x09
PUBLIC_KEY_BYTES = 32
CHAT_NODE_TYPE = 1
NO_FLAGS = 0
NO_KNOWN_ROUTE = 0xFF
OUT_PATH_FIELD_BYTES = 64
NAME_FIELD_BYTES = 32
ADD_UPDATE_CONTACT_FRAME_BYTES = 144
# How the library reports a contact without a known route.
LISTED_FLOOD_ROUTE_LENGTH = -1


@dataclass(frozen=True, kw_only=True)
class NodeContactRecord:
    public_key: str
    name: str
    advert_timestamp: int
    latitude_microdegrees: int
    longitude_microdegrees: int


@dataclass(frozen=True, kw_only=True)
class ListedNodeContact:
    """One contact of the node's own table, from a CONTACTS listing."""

    public_key: str
    name: str
    # -1: no known route, the node floods; 0 or more: the hop count of the stored route.
    out_path_length: int


def build_node_contact_record(contact: ContactForNode) -> NodeContactRecord:
    return NodeContactRecord(
        public_key=contact.public_key,
        name=contact.name,
        advert_timestamp=contact.advert_timestamp,
        latitude_microdegrees=contact.latitude_microdegrees,
        longitude_microdegrees=contact.longitude_microdegrees,
    )


def build_add_update_contact_frame(record: NodeContactRecord) -> bytes:
    public_key = bytes.fromhex(record.public_key)
    if len(public_key) != PUBLIC_KEY_BYTES:
        raise ValueError(f"A contact's public key has {PUBLIC_KEY_BYTES} bytes, not {len(public_key)}.")

    frame = (
        bytes([ADD_UPDATE_CONTACT_COMMAND_CODE])
        + public_key
        + bytes([CHAT_NODE_TYPE, NO_FLAGS, NO_KNOWN_ROUTE])
        + bytes(OUT_PATH_FIELD_BYTES)
        + encode_name_field(record.name)
        + (record.advert_timestamp & 0xFFFFFFFF).to_bytes(4, "little")
        + record.latitude_microdegrees.to_bytes(4, "little", signed=True)
        + record.longitude_microdegrees.to_bytes(4, "little", signed=True)
    )
    if len(frame) != ADD_UPDATE_CONTACT_FRAME_BYTES:
        raise AssertionError(f"ADD_UPDATE_CONTACT must be {ADD_UPDATE_CONTACT_FRAME_BYTES} bytes, not {len(frame)}.")
    return frame


def encode_name_field(name: str) -> bytes:
    """At most 31 bytes of UTF-8, cut at a character boundary, padded with NUL bytes."""
    return truncate_contact_name(name).encode("utf-8").ljust(NAME_FIELD_BYTES, b"\x00")


def parse_listed_contacts(contacts_payload: Mapping[str, Mapping[str, Any]]) -> dict[str, ListedNodeContact]:
    """The library's CONTACTS payload, keyed by lower-case public key."""
    listed_contacts: dict[str, ListedNodeContact] = {}
    for public_key, contact_fields in contacts_payload.items():
        normalized_public_key = str(public_key).lower()
        listed_contacts[normalized_public_key] = ListedNodeContact(
            public_key=normalized_public_key,
            name=str(contact_fields.get("adv_name", "")),
            out_path_length=int(contact_fields.get("out_path_len", LISTED_FLOOD_ROUTE_LENGTH)),
        )
    return listed_contacts
