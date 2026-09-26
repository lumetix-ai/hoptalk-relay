"""The encrypted backup of the relay node's private key: the optional node_setting key node.identity_backup.

A factory reset gives the node a new key pair, and every user would have to add the relay's new
contact card. With the private key kept here, a reconfiguration imports it into the reset node
(or into a replacement board), and the relay keeps its identity.

The value is a JSON document:

    {"version": 1, "public_key": "<64 hex>", "nonce": "<base64>", "ciphertext": "<base64>",
     "tag": "<base64>", "created_at": "<ISO UTC>"}

The private key is encrypted with AES-256-GCM under a key derived from Django's SECRET_KEY by
HKDF-SHA256, with a fresh nonce for every backup and the public key as associated data, so a
backup cannot be passed off as another identity's. A changed SECRET_KEY, or an altered document,
makes the backup unreadable until the worker takes a new one from the attached node.

A private key is only ever encrypted, and a decrypted one only ever used, when it belongs to the
public key next to it: its scalar is clamped, as the firmware requires of an imported key, and
derives that public key. Bytes that came from a node answering for another identity, or were
corrupted on the link, are therefore never stored, offered or imported as the relay's key.

The worker holds the private key in clear only to take a backup or to restore one, wrapped in
NodePrivateKey, whose representation never shows it. The panel decrypts a backup only to tell
whether it is still readable, and drops the key at once; nothing shows, logs or stores the key in
any other form than this encrypted one.
"""

import base64
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.PublicKey import ECC
from Crypto.Random import get_random_bytes
from django.conf import settings
from django.db import transaction

from node.models import NodeSetting
from node.node_settings import NodeSettingKey, OptionalNodeSettingKey, read_node_setting_value

BACKUP_FORMAT_VERSION = 1
PRIVATE_KEY_BYTES = 64
PUBLIC_KEY_HEX_LENGTH = 64
ENCRYPTION_KEY_BYTES = 32
NONCE_BYTES = 12
TAG_BYTES = 16
KEY_DERIVATION_CONTEXT = b"hoptalk-relay node identity backup, version 1"
PUBLIC_KEY_PREFIX_LENGTH = 12
SCALAR_BYTES = 32
ED25519_CURVE_NAME = "Ed25519"
# The base point and the group order of Ed25519 (RFC 8032, section 5.1).
ED25519_BASE_POINT_X = 15112221349535400772501151409588531511454012693041857206046113283949847762202
ED25519_BASE_POINT_Y = 46316835694926478169428394003475163141307993866256225615783033603165251855960
ED25519_GROUP_ORDER = 2**252 + 27742317777372353535851937790883648493

DAMAGED_BACKUP_REASON = "The stored identity backup is damaged."
UNDECRYPTABLE_BACKUP_REASON = (
    "The stored identity backup cannot be decrypted: SECRET_KEY in src/.env changed since it was taken, "
    "or the backup was altered."
)
MISSING_BACKUP_REASON = "No identity backup is stored."
MISMATCHED_KEY_PAIR_REASON = (
    "The stored identity backup is damaged: the private key in it does not belong to the public key it names."
)
# Whatever a damaged document makes the JSON parser or the field conversions raise; deeply
# nested JSON exhausts the parser's recursion.
PARSING_ERRORS = (KeyError, TypeError, ValueError, OverflowError, RecursionError)


class NodePrivateKey:
    """The node's 64-byte private key in MeshCore's form; its repr and str never show the bytes."""

    __slots__ = ("_key_bytes",)

    def __init__(self, key_bytes: bytes) -> None:
        if len(key_bytes) != PRIVATE_KEY_BYTES:
            raise ValueError(f"A node's private key has {PRIVATE_KEY_BYTES} bytes, not {len(key_bytes)}.")
        self._key_bytes = bytes(key_bytes)

    def reveal_bytes(self) -> bytes:
        return self._key_bytes

    def belongs_to(self, public_key: str) -> bool:
        """A key the firmware would import, and after which it would report this public key."""
        return has_clamped_scalar(self._key_bytes) and derive_public_key_hex(self._key_bytes) == public_key

    def __repr__(self) -> str:
        return "NodePrivateKey(<hidden>)"

    def __str__(self) -> str:
        return repr(self)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NodePrivateKey):
            return NotImplemented
        return hmac.compare_digest(self._key_bytes, other._key_bytes)


def has_clamped_scalar(private_key_bytes: bytes) -> bool:
    """LocalIdentity::validatePrivateKey accepts no other scalar.

    Its key-exchange test clamps the scalar while ed25519_derive_pub does not, so the two sides
    of the test agree only when the scalar is clamped already.
    """
    return (
        private_key_bytes[0] & 0b0000_0111 == 0
        and private_key_bytes[SCALAR_BYTES - 1] & 0b1000_0000 == 0
        and private_key_bytes[SCALAR_BYTES - 1] & 0b0100_0000 != 0
    )


def derive_public_key_hex(private_key_bytes: bytes) -> str:
    """ed25519_derive_pub: the little-endian scalar half of the key times the base point, as 64 hex digits."""
    scalar = int.from_bytes(private_key_bytes[:SCALAR_BYTES], "little")
    base_point = ECC.EccPoint(ED25519_BASE_POINT_X, ED25519_BASE_POINT_Y, curve=ED25519_CURVE_NAME)
    public_point = base_point * (scalar % ED25519_GROUP_ORDER)
    return bytes(ECC.EccKey(curve=ED25519_CURVE_NAME, point=public_point).export_key(format="raw")).hex()


class NodeIdentityBackupError(Exception):
    """The backup cannot give back the private key; the message is shown to the operator."""


class NodeIdentityBackupMissingError(NodeIdentityBackupError):
    """No backup is stored, or the stored one belongs to another identity."""


class NodeIdentityBackupUnreadableError(NodeIdentityBackupError):
    """The document is damaged, it does not decrypt with the current SECRET_KEY, or its key is not its identity's."""


@dataclass(frozen=True, kw_only=True)
class NodeIdentityBackup:
    """The stored document: everything in it but the public key and the time is encrypted or random."""

    version: int
    public_key: str
    nonce: bytes
    ciphertext: bytes
    tag: bytes
    created_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "public_key": self.public_key,
            "nonce": encode_base64(self.nonce),
            "ciphertext": encode_base64(self.ciphertext),
            "tag": encode_base64(self.tag),
            "created_at": self.created_at.astimezone(UTC).isoformat(),
        }

    @classmethod
    def from_json(cls, stored_json: Any) -> NodeIdentityBackup:
        """Raises NodeIdentityBackupUnreadableError for anything but a well-formed version 1 document."""
        try:
            node_identity_backup = cls(
                version=read_typed_field(stored_json, "version", int),
                public_key=read_typed_field(stored_json, "public_key", str),
                nonce=decode_base64(read_typed_field(stored_json, "nonce", str)),
                ciphertext=decode_base64(read_typed_field(stored_json, "ciphertext", str)),
                tag=decode_base64(read_typed_field(stored_json, "tag", str)),
                created_at=datetime.fromisoformat(read_typed_field(stored_json, "created_at", str)),
            )
        except PARSING_ERRORS as parsing_error:
            raise NodeIdentityBackupUnreadableError(DAMAGED_BACKUP_REASON) from parsing_error
        if not node_identity_backup.has_valid_layout():
            raise NodeIdentityBackupUnreadableError(DAMAGED_BACKUP_REASON)
        return node_identity_backup

    def has_valid_layout(self) -> bool:
        return (
            self.version == BACKUP_FORMAT_VERSION
            and is_public_key_hex(self.public_key)
            and len(self.nonce) == NONCE_BYTES
            and len(self.ciphertext) == PRIVATE_KEY_BYTES
            and len(self.tag) == TAG_BYTES
            and self.created_at.tzinfo is not None
        )


class NodeIdentityBackupStatus(StrEnum):
    ABSENT = "absent"
    STORED = "stored"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, kw_only=True)
class NodeIdentityBackupState:
    status: NodeIdentityBackupStatus
    # The identity the document names; "" when there is none or it is damaged.
    public_key: str = ""
    created_at: datetime | None = None
    unreadable_reason: str = ""

    def is_stored_for(self, public_key: str) -> bool:
        """A backup that decrypts and belongs to this identity: the only kind a restore can use."""
        return self.status == NodeIdentityBackupStatus.STORED and bool(public_key) and self.public_key == public_key


@dataclass(frozen=True, kw_only=True)
class ConfiguredNodeIdentityBackup:
    # node.public_key; "" before the initial setup.
    configured_public_key: str
    backup_state: NodeIdentityBackupState

    @property
    def is_restorable(self) -> bool:
        return self.backup_state.is_stored_for(self.configured_public_key)


def read_typed_field[FieldType](stored_json: Any, field_name: str, field_type: type[FieldType]) -> FieldType:
    """The field, when it has exactly that JSON type: a float or a bool is no version number."""
    if not isinstance(stored_json, dict):
        raise TypeError("A backup document is a JSON object.")
    field_value = stored_json[field_name]
    if type(field_value) is not field_type:
        raise TypeError(f"The backup's {field_name} is not a {field_type.__name__}.")
    return field_value


def encode_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode_base64(encoded_value: str) -> bytes:
    return base64.b64decode(encoded_value, validate=True)


def is_public_key_hex(public_key: str) -> bool:
    if len(public_key) != PUBLIC_KEY_HEX_LENGTH or public_key != public_key.lower():
        return False
    try:
        bytes.fromhex(public_key)
    except ValueError:
        return False
    return True


def derive_backup_encryption_key() -> bytes:
    derived_key = HKDF(
        settings.SECRET_KEY.encode("utf-8"),
        ENCRYPTION_KEY_BYTES,
        salt=b"",
        hashmod=SHA256,
        context=KEY_DERIVATION_CONTEXT,
    )
    assert isinstance(derived_key, bytes)
    return derived_key


def encrypt_node_identity_backup(public_key: str, private_key: NodePrivateKey, now: datetime) -> NodeIdentityBackup:
    if not is_public_key_hex(public_key):
        raise ValueError("A backup needs the identity's public key as 64 lower-case hex digits.")
    if not private_key.belongs_to(public_key):
        raise ValueError(
            f"The private key does not belong to key {public_key[:PUBLIC_KEY_PREFIX_LENGTH]}, so it is not backed up."
        )
    nonce = get_random_bytes(NONCE_BYTES)
    cipher = AES.new(derive_backup_encryption_key(), AES.MODE_GCM, nonce=nonce, mac_len=TAG_BYTES)
    cipher.update(bytes.fromhex(public_key))
    ciphertext, tag = cipher.encrypt_and_digest(private_key.reveal_bytes())
    return NodeIdentityBackup(
        version=BACKUP_FORMAT_VERSION,
        public_key=public_key,
        nonce=nonce,
        ciphertext=ciphertext,
        tag=tag,
        created_at=now,
    )


def decrypt_node_identity_backup(node_identity_backup: NodeIdentityBackup) -> NodePrivateKey:
    """Raises NodeIdentityBackupUnreadableError when the tag does not verify or the key is not the named identity's."""
    cipher = AES.new(derive_backup_encryption_key(), AES.MODE_GCM, nonce=node_identity_backup.nonce, mac_len=TAG_BYTES)
    cipher.update(bytes.fromhex(node_identity_backup.public_key))
    try:
        private_key_bytes = cipher.decrypt_and_verify(node_identity_backup.ciphertext, node_identity_backup.tag)
    except ValueError as verification_error:
        raise NodeIdentityBackupUnreadableError(UNDECRYPTABLE_BACKUP_REASON) from verification_error
    private_key = NodePrivateKey(private_key_bytes)
    if not private_key.belongs_to(node_identity_backup.public_key):
        raise NodeIdentityBackupUnreadableError(MISMATCHED_KEY_PAIR_REASON)
    return private_key


def serialize_node_identity_backup(node_identity_backup: NodeIdentityBackup) -> str:
    return json.dumps(node_identity_backup.to_json(), separators=(",", ":"))


def parse_node_identity_backup(stored_value: str) -> NodeIdentityBackup:
    try:
        stored_json = json.loads(stored_value)
    except PARSING_ERRORS as parsing_error:
        raise NodeIdentityBackupUnreadableError(DAMAGED_BACKUP_REASON) from parsing_error
    return NodeIdentityBackup.from_json(stored_json)


def read_stored_backup_value() -> str | None:
    return (
        NodeSetting.objects.filter(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value)
        .values_list("value", flat=True)
        .first()
    )


def read_node_identity_backup_state() -> NodeIdentityBackupState:
    """Absent, stored for the key it names (it decrypts), or unreadable with the reason; it never raises for these."""
    stored_value = read_stored_backup_value()
    if stored_value is None:
        return NodeIdentityBackupState(status=NodeIdentityBackupStatus.ABSENT)
    try:
        node_identity_backup = parse_node_identity_backup(stored_value)
    except NodeIdentityBackupUnreadableError as damaged_error:
        return NodeIdentityBackupState(status=NodeIdentityBackupStatus.UNREADABLE, unreadable_reason=str(damaged_error))

    try:
        decrypt_node_identity_backup(node_identity_backup)
    except NodeIdentityBackupUnreadableError as decryption_error:
        return NodeIdentityBackupState(
            status=NodeIdentityBackupStatus.UNREADABLE,
            public_key=node_identity_backup.public_key,
            created_at=node_identity_backup.created_at,
            unreadable_reason=str(decryption_error),
        )
    return NodeIdentityBackupState(
        status=NodeIdentityBackupStatus.STORED,
        public_key=node_identity_backup.public_key,
        created_at=node_identity_backup.created_at,
    )


def read_configured_node_identity_backup() -> ConfiguredNodeIdentityBackup:
    return ConfiguredNodeIdentityBackup(
        configured_public_key=read_node_setting_value(NodeSettingKey.NODE_PUBLIC_KEY),
        backup_state=read_node_identity_backup_state(),
    )


def load_private_key_for_restore(expected_public_key: str) -> NodePrivateKey:
    """The decrypted key of expected_public_key; raises NodeIdentityBackupError with the operator's reason."""
    stored_value = read_stored_backup_value()
    if stored_value is None:
        raise NodeIdentityBackupMissingError(MISSING_BACKUP_REASON)
    node_identity_backup = parse_node_identity_backup(stored_value)
    if node_identity_backup.public_key != expected_public_key:
        raise NodeIdentityBackupMissingError(
            f"The stored identity backup belongs to key {node_identity_backup.public_key[:PUBLIC_KEY_PREFIX_LENGTH]}, "
            f"not to the relay's key {expected_public_key[:PUBLIC_KEY_PREFIX_LENGTH]}."
        )
    return decrypt_node_identity_backup(node_identity_backup)


def store_node_identity_backup(public_key: str, private_key: NodePrivateKey, now: datetime) -> bool:
    """Encrypt and store (or replace) the backup of the configured identity.

    False, with nothing written, when node_setting no longer names this key: a setup run
    replaced the configuration meanwhile. Raises ValueError when the private key is not that
    key's.
    """
    with transaction.atomic():
        configured_public_key = (
            NodeSetting.objects.select_for_update()
            .filter(key=NodeSettingKey.NODE_PUBLIC_KEY.value)
            .values_list("value", flat=True)
            .first()
        )
        if not configured_public_key or configured_public_key != public_key:
            return False
        write_node_identity_backup(encrypt_node_identity_backup(public_key, private_key, now))
    return True


def write_node_identity_backup(node_identity_backup: NodeIdentityBackup) -> None:
    """Insert or replace the row; a setup run calls it inside the transaction that completes it."""
    NodeSetting.objects.update_or_create(
        key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value,
        defaults={"value": serialize_node_identity_backup(node_identity_backup)},
    )


def discard_node_identity_backup_of_other_identities(public_key: str) -> None:
    """Delete a backup that does not name this key, damaged ones included: only this identity can be restored."""
    stored_value = read_stored_backup_value()
    if stored_value is None:
        return
    try:
        backed_up_public_key = parse_node_identity_backup(stored_value).public_key
    except NodeIdentityBackupUnreadableError:
        backed_up_public_key = ""
    if backed_up_public_key != public_key:
        NodeSetting.objects.filter(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value).delete()
