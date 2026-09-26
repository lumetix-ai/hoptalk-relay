"""The encrypted backup of the relay node's private key: encryption, storage beside the configuration, its state."""

import base64
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from Crypto.Signature import eddsa
from pytest_django import Settings

from node.models import NodeSetting
from node.node_identity_backups import (
    DAMAGED_BACKUP_REASON,
    MISMATCHED_KEY_PAIR_REASON,
    MISSING_BACKUP_REASON,
    UNDECRYPTABLE_BACKUP_REASON,
    NodeIdentityBackup,
    NodeIdentityBackupMissingError,
    NodeIdentityBackupStatus,
    NodeIdentityBackupUnreadableError,
    NodePrivateKey,
    decrypt_node_identity_backup,
    derive_public_key_hex,
    discard_node_identity_backup_of_other_identities,
    encrypt_node_identity_backup,
    load_private_key_for_restore,
    read_configured_node_identity_backup,
    read_node_identity_backup_state,
    store_node_identity_backup,
    write_node_identity_backup,
)
from node.node_settings import (
    OptionalNodeSettingKey,
    is_node_configured,
    load_node_configuration,
    replace_node_configuration,
)
from tests.node_key_pairs import encrypt_a_foreign_private_key, generate_node_key_pair
from tests.private_key_checks import mentions_private_key
from tests.services.node.node_builders import build_node_configuration
from tests.worker.fake_node.node_identity import NodeIdentity, expand_private_key_seed

pytestmark = pytest.mark.django_db

CONFIGURED_KEY_PAIR = generate_node_key_pair(20260926)
OTHER_KEY_PAIR = generate_node_key_pair(20260927)
CONFIGURED_PUBLIC_KEY = CONFIGURED_KEY_PAIR.public_key
OTHER_PUBLIC_KEY = OTHER_KEY_PAIR.public_key
BACKED_UP_AT = datetime(2026, 9, 26, 8, 30, tzinfo=UTC)
LATER = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
ORIGINAL_SECRET_KEY = "the secret key the backup was taken with, long enough for Django"
CHANGED_SECRET_KEY = "a different secret key, as after make generate-secret-key was run"


@pytest.fixture(autouse=True)
def use_original_secret_key(settings: Settings) -> None:
    settings.SECRET_KEY = ORIGINAL_SECRET_KEY


@pytest.fixture
def private_key() -> NodePrivateKey:
    return CONFIGURED_KEY_PAIR.private_key


def configure_node(public_key: str = CONFIGURED_PUBLIC_KEY) -> None:
    replace_node_configuration(build_node_configuration(public_key=public_key))


def read_stored_backup_document() -> dict[str, object]:
    stored_value = NodeSetting.objects.get(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value).value
    stored_document: dict[str, object] = json.loads(stored_value)
    return stored_document


def flip_one_bit(value: bytes, byte_index: int) -> bytes:
    changed_value = bytearray(value)
    changed_value[byte_index] ^= 0x01
    return bytes(changed_value)


def test_an_encrypted_backup_decrypts_to_the_same_private_key(private_key: NodePrivateKey) -> None:
    node_identity_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    assert decrypt_node_identity_backup(node_identity_backup) == private_key
    assert node_identity_backup.public_key == CONFIGURED_PUBLIC_KEY
    assert node_identity_backup.created_at == BACKED_UP_AT


def test_every_backup_gets_a_fresh_nonce(private_key: NodePrivateKey) -> None:
    first_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    second_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    assert first_backup.nonce != second_backup.nonce
    assert first_backup.ciphertext != second_backup.ciphertext


def tamper_with_encrypted_field(
    node_identity_backup: NodeIdentityBackup, field_name: str, byte_index: int
) -> NodeIdentityBackup:
    match field_name:
        case "nonce":
            return replace(node_identity_backup, nonce=flip_one_bit(node_identity_backup.nonce, byte_index))
        case "ciphertext":
            return replace(node_identity_backup, ciphertext=flip_one_bit(node_identity_backup.ciphertext, byte_index))
        case _:
            return replace(node_identity_backup, tag=flip_one_bit(node_identity_backup.tag, byte_index))


@pytest.mark.parametrize(("field_name", "field_length"), [("nonce", 12), ("ciphertext", 64), ("tag", 16)])
def test_a_change_to_any_byte_of_the_encrypted_fields_makes_the_backup_unreadable(
    private_key: NodePrivateKey, field_name: str, field_length: int
) -> None:
    node_identity_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    for byte_index in range(field_length):
        tampered_backup = tamper_with_encrypted_field(node_identity_backup, field_name, byte_index)
        with pytest.raises(NodeIdentityBackupUnreadableError, match="cannot be decrypted"):
            decrypt_node_identity_backup(tampered_backup)


def test_a_backup_moved_onto_another_identity_does_not_decrypt(private_key: NodePrivateKey) -> None:
    node_identity_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    public_key_bytes = bytes.fromhex(CONFIGURED_PUBLIC_KEY)

    for byte_index in range(len(public_key_bytes)):
        moved_backup = replace(node_identity_backup, public_key=flip_one_bit(public_key_bytes, byte_index).hex())
        with pytest.raises(NodeIdentityBackupUnreadableError):
            decrypt_node_identity_backup(moved_backup)


def test_a_changed_secret_key_makes_the_backup_unreadable(private_key: NodePrivateKey, settings: Settings) -> None:
    node_identity_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    settings.SECRET_KEY = CHANGED_SECRET_KEY

    with pytest.raises(NodeIdentityBackupUnreadableError) as unreadable_error:
        decrypt_node_identity_backup(node_identity_backup)
    assert str(unreadable_error.value) == UNDECRYPTABLE_BACKUP_REASON


def test_the_private_key_never_shows_in_a_representation_or_the_stored_document(private_key: NodePrivateKey) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    stored_value = NodeSetting.objects.get(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value).value
    node_identity_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    shown_texts = [repr(private_key), str(private_key), f"{private_key}", repr(node_identity_backup), stored_value]
    key_is_shown = any(mentions_private_key(shown_text, private_key) for shown_text in shown_texts)

    assert not key_is_shown
    assert repr(private_key) == "NodePrivateKey(<hidden>)"


def test_the_stored_document_has_the_documented_layout(private_key: NodePrivateKey) -> None:
    configure_node()

    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    stored_document = read_stored_backup_document()
    assert set(stored_document) == {"version", "public_key", "nonce", "ciphertext", "tag", "created_at"}
    assert stored_document["version"] == 1
    assert stored_document["public_key"] == CONFIGURED_PUBLIC_KEY
    assert stored_document["created_at"] == "2026-09-26T08:30:00+00:00"
    assert len(base64.b64decode(str(stored_document["nonce"]))) == 12
    assert len(base64.b64decode(str(stored_document["tag"]))) == 16


def test_a_backup_is_stored_only_for_the_configured_identity(private_key: NodePrivateKey) -> None:
    assert not store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    configure_node()

    assert not store_node_identity_backup(OTHER_PUBLIC_KEY, private_key, BACKED_UP_AT)
    assert read_node_identity_backup_state().status == NodeIdentityBackupStatus.ABSENT
    assert store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    assert read_node_identity_backup_state().is_stored_for(CONFIGURED_PUBLIC_KEY)


def test_a_new_backup_replaces_the_stored_one(private_key: NodePrivateKey) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    first_document = read_stored_backup_document()

    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, LATER)

    assert NodeSetting.objects.filter(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value).count() == 1
    assert read_stored_backup_document()["nonce"] != first_document["nonce"]
    assert load_private_key_for_restore(CONFIGURED_PUBLIC_KEY) == private_key
    assert read_node_identity_backup_state().created_at == LATER


def test_replacing_the_configuration_keeps_the_backup(private_key: NodePrivateKey) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    replace_node_configuration(build_node_configuration(public_key=CONFIGURED_PUBLIC_KEY, setup_run_id=2))

    assert load_private_key_for_restore(CONFIGURED_PUBLIC_KEY) == private_key
    loaded_configuration = load_node_configuration()
    assert loaded_configuration is not None
    assert loaded_configuration.setup_run_id == 2


def test_the_backup_alone_neither_configures_the_node_nor_makes_its_configuration_incomplete(
    private_key: NodePrivateKey,
) -> None:
    write_node_identity_backup(encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT))

    assert not is_node_configured()
    assert load_node_configuration() is None


def test_a_configured_node_without_a_backup_is_complete() -> None:
    configure_node()

    assert is_node_configured()
    assert load_node_configuration() is not None
    assert read_node_identity_backup_state().status == NodeIdentityBackupStatus.ABSENT


def test_the_state_of_an_absent_backup() -> None:
    configure_node()

    configured_backup = read_configured_node_identity_backup()

    assert configured_backup.backup_state.status == NodeIdentityBackupStatus.ABSENT
    assert configured_backup.configured_public_key == CONFIGURED_PUBLIC_KEY
    assert not configured_backup.is_restorable


def test_the_state_of_a_stored_backup_names_its_key_and_date(private_key: NodePrivateKey) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    configured_backup = read_configured_node_identity_backup()

    assert configured_backup.backup_state.status == NodeIdentityBackupStatus.STORED
    assert configured_backup.backup_state.public_key == CONFIGURED_PUBLIC_KEY
    assert configured_backup.backup_state.created_at == BACKED_UP_AT
    assert configured_backup.is_restorable


def test_a_backup_of_another_identity_is_stored_but_cannot_restore_the_configured_one() -> None:
    configure_node()
    write_node_identity_backup(encrypt_node_identity_backup(OTHER_PUBLIC_KEY, OTHER_KEY_PAIR.private_key, BACKED_UP_AT))

    configured_backup = read_configured_node_identity_backup()

    assert configured_backup.backup_state.status == NodeIdentityBackupStatus.STORED
    assert configured_backup.backup_state.public_key == OTHER_PUBLIC_KEY
    assert not configured_backup.is_restorable


def test_the_state_of_a_backup_taken_with_another_secret_key_is_unreadable_with_the_reason(
    private_key: NodePrivateKey, settings: Settings
) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    settings.SECRET_KEY = CHANGED_SECRET_KEY

    backup_state = read_node_identity_backup_state()

    assert backup_state.status == NodeIdentityBackupStatus.UNREADABLE
    assert backup_state.unreadable_reason == UNDECRYPTABLE_BACKUP_REASON
    assert "SECRET_KEY in src/.env" in backup_state.unreadable_reason
    assert backup_state.public_key == CONFIGURED_PUBLIC_KEY
    assert not read_configured_node_identity_backup().is_restorable


def build_document_text(**changed_fields: object) -> str:
    """A well-formed document with some fields replaced, as JSON text."""
    document: dict[str, object] = {
        "version": 1,
        "public_key": CONFIGURED_PUBLIC_KEY,
        "nonce": base64.b64encode(bytes(12)).decode(),
        "ciphertext": base64.b64encode(bytes(64)).decode(),
        "tag": base64.b64encode(bytes(16)).decode(),
        "created_at": BACKED_UP_AT.isoformat(),
    }
    document.update(changed_fields)
    return json.dumps(document)


@pytest.mark.parametrize(
    "damaged_value",
    [
        "not json",
        "[]",
        '"a string"',
        "1",
        "null",
        pytest.param("[" * 100_000 + "]" * 100_000, id="deeply_nested"),
        build_document_text(version=1.0),
        build_document_text(version=True),
        build_document_text(version="1"),
        build_document_text(created_at=1758875400),
        build_document_text(public_key=None),
        build_document_text(nonce=None),
        build_document_text(created_at="2026-09-26T08:30:00"),
        build_document_text().replace('"version": 1', '"version": 1e400'),
        pytest.param(
            build_document_text().replace('"version": 1', '"version": ' + "9" * 5000), id="version_with_5000_digits"
        ),
        json.dumps({"version": 1}),
        json.dumps(
            {
                "version": 2,
                "public_key": CONFIGURED_PUBLIC_KEY,
                "nonce": base64.b64encode(bytes(12)).decode(),
                "ciphertext": base64.b64encode(bytes(64)).decode(),
                "tag": base64.b64encode(bytes(16)).decode(),
                "created_at": BACKED_UP_AT.isoformat(),
            }
        ),
        json.dumps(
            {
                "version": 1,
                "public_key": CONFIGURED_PUBLIC_KEY,
                "nonce": "not base64!",
                "ciphertext": base64.b64encode(bytes(64)).decode(),
                "tag": base64.b64encode(bytes(16)).decode(),
                "created_at": BACKED_UP_AT.isoformat(),
            }
        ),
        json.dumps(
            {
                "version": 1,
                "public_key": "zz" * 32,
                "nonce": base64.b64encode(bytes(12)).decode(),
                "ciphertext": base64.b64encode(bytes(63)).decode(),
                "tag": base64.b64encode(bytes(16)).decode(),
                "created_at": BACKED_UP_AT.isoformat(),
            }
        ),
    ],
)
def test_a_damaged_document_is_unreadable_and_never_raises_for_the_panel(damaged_value: str) -> None:
    configure_node()
    NodeSetting.objects.create(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value, value=damaged_value)

    backup_state = read_node_identity_backup_state()

    assert backup_state.status == NodeIdentityBackupStatus.UNREADABLE
    assert backup_state.unreadable_reason == DAMAGED_BACKUP_REASON
    with pytest.raises(NodeIdentityBackupUnreadableError):
        load_private_key_for_restore(CONFIGURED_PUBLIC_KEY)


def test_the_restore_loads_the_key_of_the_expected_identity_only(private_key: NodePrivateKey) -> None:
    configure_node()
    with pytest.raises(NodeIdentityBackupMissingError, match=MISSING_BACKUP_REASON):
        load_private_key_for_restore(CONFIGURED_PUBLIC_KEY)

    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    assert load_private_key_for_restore(CONFIGURED_PUBLIC_KEY) == private_key
    with pytest.raises(NodeIdentityBackupMissingError, match=f"belongs to key {CONFIGURED_PUBLIC_KEY[:12]}"):
        load_private_key_for_restore(OTHER_PUBLIC_KEY)


def test_the_restore_refuses_a_backup_the_secret_key_no_longer_opens(
    private_key: NodePrivateKey, settings: Settings
) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)
    settings.SECRET_KEY = CHANGED_SECRET_KEY

    with pytest.raises(NodeIdentityBackupUnreadableError, match="SECRET_KEY"):
        load_private_key_for_restore(CONFIGURED_PUBLIC_KEY)


def test_only_a_backup_of_the_kept_identity_survives_a_discard(private_key: NodePrivateKey) -> None:
    configure_node()
    store_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    discard_node_identity_backup_of_other_identities(CONFIGURED_PUBLIC_KEY)
    assert read_node_identity_backup_state().is_stored_for(CONFIGURED_PUBLIC_KEY)

    discard_node_identity_backup_of_other_identities(OTHER_PUBLIC_KEY)
    assert read_node_identity_backup_state().status == NodeIdentityBackupStatus.ABSENT


def test_a_private_key_has_exactly_sixty_four_bytes() -> None:
    with pytest.raises(ValueError, match="64 bytes"):
        NodePrivateKey(bytes(32))


def test_a_backup_round_trips_through_its_document(private_key: NodePrivateKey) -> None:
    node_identity_backup = encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, private_key, BACKED_UP_AT)

    assert NodeIdentityBackup.from_json(node_identity_backup.to_json()) == node_identity_backup


def test_the_relay_derives_the_public_key_a_node_derives_from_its_private_key() -> None:
    """The node's key is ed25519_create_keypair's expansion of a seed; RFC 8032 derives the same public key."""
    for seed in range(8):
        private_key_seed = bytes([seed]) * 32
        standard_public_key = bytes(
            eddsa.import_private_key(private_key_seed).public_key().export_key(format="raw")
        ).hex()
        expanded_private_key = expand_private_key_seed(private_key_seed)

        assert derive_public_key_hex(expanded_private_key) == standard_public_key
        assert NodeIdentity(expanded_private_key).public_key.hex() == standard_public_key


def unclamp(private_key: NodePrivateKey, byte_index: int, bit_mask: int) -> NodePrivateKey:
    changed_key_bytes = bytearray(private_key.reveal_bytes())
    changed_key_bytes[byte_index] ^= bit_mask
    return NodePrivateKey(bytes(changed_key_bytes))


def test_a_private_key_belongs_only_to_its_own_public_key_and_only_with_a_clamped_scalar(
    private_key: NodePrivateKey,
) -> None:
    assert private_key.belongs_to(CONFIGURED_PUBLIC_KEY)
    assert not private_key.belongs_to(OTHER_PUBLIC_KEY)
    for byte_index, bit_mask in ((0, 0x01), (0, 0x04), (31, 0x80), (31, 0x40)):
        assert not unclamp(private_key, byte_index, bit_mask).belongs_to(CONFIGURED_PUBLIC_KEY)
    assert not NodePrivateKey(bytes(64)).belongs_to(CONFIGURED_PUBLIC_KEY)


def test_a_private_key_that_is_not_the_identitys_is_never_encrypted_or_stored() -> None:
    configure_node()

    with pytest.raises(ValueError, match="does not belong to key"):
        encrypt_node_identity_backup(CONFIGURED_PUBLIC_KEY, OTHER_KEY_PAIR.private_key, BACKED_UP_AT)
    with pytest.raises(ValueError, match="does not belong to key"):
        store_node_identity_backup(CONFIGURED_PUBLIC_KEY, OTHER_KEY_PAIR.private_key, BACKED_UP_AT)

    assert read_node_identity_backup_state().status == NodeIdentityBackupStatus.ABSENT


def test_a_backup_whose_key_is_not_its_identitys_is_unreadable_and_never_restored() -> None:
    configure_node()
    write_node_identity_backup(
        encrypt_a_foreign_private_key(CONFIGURED_PUBLIC_KEY, OTHER_KEY_PAIR.private_key, BACKED_UP_AT)
    )

    configured_backup = read_configured_node_identity_backup()

    assert configured_backup.backup_state.status == NodeIdentityBackupStatus.UNREADABLE
    assert configured_backup.backup_state.unreadable_reason == MISMATCHED_KEY_PAIR_REASON
    assert configured_backup.backup_state.public_key == CONFIGURED_PUBLIC_KEY
    assert not configured_backup.is_restorable
    with pytest.raises(NodeIdentityBackupUnreadableError, match="does not belong to the public key"):
        load_private_key_for_restore(CONFIGURED_PUBLIC_KEY)
