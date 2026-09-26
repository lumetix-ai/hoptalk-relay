"""Looking for a node's private key in text the relay writes, without ever printing the key.

A test compares the result with a plain boolean, so a failing assertion shows neither the key nor
the text that leaked it.
"""

import base64

from node.node_identity_backups import NodePrivateKey


def describe_private_key_forms(private_key: NodePrivateKey) -> list[str]:
    """Hex in both cases, base64, and the body of a bytes repr, which a logged frame would contain."""
    key_bytes = private_key.reveal_bytes()
    return [
        key_bytes.hex(),
        key_bytes.hex().upper(),
        base64.b64encode(key_bytes).decode("ascii"),
        repr(key_bytes)[2:-1],
    ]


def mentions_private_key(text: str, private_key: NodePrivateKey) -> bool:
    return any(key_form in text for key_form in describe_private_key_forms(private_key))
