"""The node's key pair in the firmware's form, and its export and import, as companion firmware v1.17.1 has them."""

import random
from typing import Any

import pytest
from Crypto.Signature import eddsa
from meshcore import EventType

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.frames import FirmwareErrorCode
from tests.worker.fake_node.meshcore_events import MeshCoreEventRecorder, is_error_with_code
from tests.worker.fake_node.node_identity import (
    RESERVED_PUBLIC_KEY_FIRST_BYTES,
    NodeIdentity,
    expand_private_key_seed,
    parse_contact_card_uri,
    signature_is_valid,
)
from tests.worker.fake_node.waiting import wait_until

OK_ERROR_OR_DISABLED = [EventType.OK, EventType.ERROR, EventType.DISABLED]
EXPORT_PRIVATE_KEY_FRAME = b"\x17"
IMPORT_PRIVATE_KEY_CODE = b"\x18"
SEED_BYTES = 32
IDENTITY_SEED = 20260926


def generate_identity(random_generator: random.Random) -> NodeIdentity:
    return NodeIdentity.generate(random_generator)


def find_identity_with_reserved_public_key_prefix() -> NodeIdentity:
    random_generator = random.Random(IDENTITY_SEED)
    while True:
        identity = NodeIdentity.from_seed(random_generator.randbytes(SEED_BYTES))
        if identity.public_key[0] in RESERVED_PUBLIC_KEY_FIRST_BYTES:
            return identity


async def import_private_key(meshcore_client: Any, private_key: bytes) -> Any:
    return await meshcore_client.commands.send(IMPORT_PRIVATE_KEY_CODE + private_key, OK_ERROR_OR_DISABLED)


async def read_reported_public_key(meshcore_client: Any) -> str:
    self_information = await meshcore_client.commands.send_appstart()
    return str(self_information.payload["public_key"])


def test_a_key_pair_from_a_seed_matches_the_standard_ed25519_one() -> None:
    random_generator = random.Random(IDENTITY_SEED)
    for _ in range(20):
        seed = random_generator.randbytes(SEED_BYTES)
        message = random_generator.randbytes(48)
        reference_key = eddsa.import_private_key(seed)

        identity = NodeIdentity.from_seed(seed)

        assert identity.public_key == reference_key.public_key().export_key(format="raw")
        assert identity.sign(message) == eddsa.new(reference_key, "rfc8032").sign(message)


def test_an_identity_rebuilt_from_its_expanded_key_alone_is_the_same_identity() -> None:
    original_identity = generate_identity(random.Random(IDENTITY_SEED))

    rebuilt_identity = NodeIdentity(original_identity.expanded_private_key)

    assert rebuilt_identity.public_key == original_identity.public_key
    assert signature_is_valid(
        public_key=rebuilt_identity.public_key, message=b"an advert", signature=rebuilt_identity.sign(b"an advert")
    )


def test_the_expanded_key_is_the_clamped_sha512_of_the_seed() -> None:
    expanded_private_key = expand_private_key_seed(bytes(SEED_BYTES))

    assert len(expanded_private_key) == 64
    assert expanded_private_key[0] & 0x07 == 0
    assert expanded_private_key[31] & 0xC0 == 0x40


async def test_the_export_returns_the_identitys_sixty_four_byte_key(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    export_result = await meshcore_client.commands.export_private_key()

    exported_key_is_the_identitys = (
        export_result.payload["private_key"] == fake_companion_firmware.identity.expanded_private_key
    )

    assert export_result.type == EventType.PRIVATE_KEY
    assert exported_key_is_the_identitys


async def test_an_import_switches_the_identity_at_once_and_signs_with_it(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    imported_identity = generate_identity(random.Random(IDENTITY_SEED + 1))
    fake_companion_firmware.add_or_update_contact(
        ContactRecord.create(public_key=bytes([0x42]) * 32, name="kept contact")
    )

    import_result = await import_private_key(meshcore_client, imported_identity.expanded_private_key)
    card_result = await meshcore_client.commands.export_contact()

    assert import_result.type == EventType.OK
    assert fake_companion_firmware.public_key == imported_identity.public_key
    assert await read_reported_public_key(meshcore_client) == imported_identity.public_key.hex()
    parsed_card = parse_contact_card_uri(card_result.payload["uri"])
    assert parsed_card is not None
    assert parsed_card.public_key == imported_identity.public_key
    assert parsed_card.signature_is_valid
    assert fake_companion_firmware.find_contact(bytes([0x42]) * 32) is not None


async def test_an_imported_identity_survives_a_reboot_and_a_factory_reset_draws_a_new_one(
    fake_companion_firmware: FakeCompanionFirmware, fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    imported_identity = generate_identity(random.Random(IDENTITY_SEED + 2))
    await import_private_key(meshcore_client, imported_identity.expanded_private_key)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.DISCONNECTED)

    await meshcore_client.commands.reboot()
    await recorder.wait_for_event(EventType.DISCONNECTED)
    await fake_node_connector.wait_until_node_accepts_connections()
    reconnected_client = await fake_node_connector()
    assert reconnected_client is not None

    assert await read_reported_public_key(reconnected_client) == imported_identity.public_key.hex()
    fake_companion_firmware.factory_reset()
    await wait_until(
        lambda: (
            fake_companion_firmware.is_running and fake_companion_firmware.public_key != imported_identity.public_key
        ),
        description="the node to boot with a new random identity",
    )


async def test_a_key_whose_public_key_starts_with_a_reserved_byte_is_refused(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    original_public_key = fake_companion_firmware.public_key
    reserved_identity = find_identity_with_reserved_public_key_prefix()

    import_result = await import_private_key(meshcore_client, reserved_identity.expanded_private_key)

    assert is_error_with_code(import_result, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert fake_companion_firmware.public_key == original_public_key


async def test_a_key_with_an_unclamped_scalar_fails_the_key_exchange_check(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    original_public_key = fake_companion_firmware.public_key
    unclamped_private_key = bytearray(generate_identity(random.Random(IDENTITY_SEED + 3)).expanded_private_key)
    unclamped_private_key[0] |= 0x01

    import_result = await import_private_key(meshcore_client, bytes(unclamped_private_key))

    assert is_error_with_code(import_result, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert fake_companion_firmware.public_key == original_public_key


async def test_an_import_without_the_whole_key_is_an_unknown_command(meshcore_client: Any) -> None:
    import_result = await import_private_key(meshcore_client, bytes(63))

    assert is_error_with_code(import_result, FirmwareErrorCode.UNSUPPORTED_COMMAND)


@pytest.mark.parametrize("command", ["export", "import"])
async def test_a_firmware_built_without_the_command_answers_disabled(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, command: str
) -> None:
    original_public_key = fake_companion_firmware.public_key
    fake_companion_firmware.private_key_export_enabled = False
    fake_companion_firmware.private_key_import_enabled = False

    if command == "export":
        result = await meshcore_client.commands.send(
            EXPORT_PRIVATE_KEY_FRAME, [EventType.PRIVATE_KEY, EventType.DISABLED, EventType.ERROR]
        )
    else:
        imported_identity = generate_identity(random.Random(IDENTITY_SEED + 4))
        result = await import_private_key(meshcore_client, imported_identity.expanded_private_key)

    assert result.type == EventType.DISABLED
    assert fake_companion_firmware.public_key == original_public_key
