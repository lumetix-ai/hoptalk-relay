"""Real node key pairs for tests: the relay backs up and restores a private key only when it derives its public key."""

import random
from dataclasses import dataclass
from datetime import datetime

from Crypto.Cipher import AES
from Crypto.Random import get_random_bytes

from node.node_identity_backups import (
    BACKUP_FORMAT_VERSION,
    NONCE_BYTES,
    TAG_BYTES,
    NodeIdentityBackup,
    NodePrivateKey,
    derive_backup_encryption_key,
)
from tests.worker.fake_node.node_identity import NodeIdentity


@dataclass(frozen=True, kw_only=True)
class NodeKeyPair:
    public_key: str
    private_key: NodePrivateKey


def generate_node_key_pair(seed: int) -> NodeKeyPair:
    """The same pair for the same seed, in the firmware's form."""
    node_identity = NodeIdentity.generate(random.Random(seed))
    return NodeKeyPair(
        public_key=node_identity.public_key.hex(), private_key=NodePrivateKey(node_identity.expanded_private_key)
    )


def encrypt_a_foreign_private_key(public_key: str, private_key: NodePrivateKey, now: datetime) -> NodeIdentityBackup:
    """A backup that decrypts but holds a key that is not public_key's, as a corrupted export would leave it.

    encrypt_node_identity_backup refuses such a key, so the document is encrypted here directly.
    """
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
