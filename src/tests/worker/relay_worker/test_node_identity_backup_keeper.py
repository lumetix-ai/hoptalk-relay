"""The worker backs up the configured node's private key at a handshake in relay mode running, and only when needed."""

import logging
from datetime import UTC, datetime

import pytest
from pytest_django import Settings

from node.models import NodeSetting, WorkerStatus
from node.node_identity_backups import (
    NodeIdentityBackupState,
    NodeIdentityBackupStatus,
    NodePrivateKey,
    encrypt_node_identity_backup,
    load_private_key_for_restore,
    read_node_identity_backup_state,
    store_node_identity_backup,
    write_node_identity_backup,
)
from node.node_settings import OptionalNodeSettingKey
from tests.node_key_pairs import generate_node_key_pair
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import CommandCode
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    in_database,
    point_node_setting_at_another_node,
    wait_for_database,
)
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

BackupState = WorkerStatus.NodeIdentityBackupState
EARLIER_BACKUP_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
PREVIOUS_SECRET_KEY = "the secret key src/.env held before it was generated again"
OTHER_KEY_PAIR = generate_node_key_pair(3)


def private_key_of(firmware: FakeCompanionFirmware) -> NodePrivateKey:
    return NodePrivateKey(firmware.identity.expanded_private_key)


def count_key_exports(firmware: FakeCompanionFirmware) -> int:
    return sum(
        1 for received_command in firmware.command_log if received_command.code == CommandCode.EXPORT_PRIVATE_KEY
    )


def read_worker_status_backup_state() -> str:
    return WorkerStatus.objects.get(id=WorkerStatus.SINGLE_ROW_ID).node_identity_backup_state


async def read_backup_state() -> NodeIdentityBackupState:
    return await in_database(read_node_identity_backup_state)


async def backup_restores_the_node(firmware: FakeCompanionFirmware) -> bool:
    restored_private_key = await in_database(load_private_key_for_restore, firmware.public_key.hex())
    return restored_private_key == private_key_of(firmware)


async def test_a_running_handshake_backs_up_the_configured_identity_when_none_is_stored(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    backup_state = await read_backup_state()
    assert backup_state.is_stored_for(fake_companion_firmware.public_key.hex())
    assert await backup_restores_the_node(fake_companion_firmware)
    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.STORED
    await wait_for_database(
        lambda: read_worker_status_backup_state() == BackupState.STORED,
        description="the worker status to show the stored backup",
    )


async def test_a_stored_backup_is_not_taken_again(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    public_key = fake_companion_firmware.public_key.hex()
    await in_database(
        store_node_identity_backup, public_key, private_key_of(fake_companion_firmware), EARLIER_BACKUP_TIME
    )

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await relay_worker.clock.sleep(0.2)

    assert count_key_exports(fake_companion_firmware) == 0
    assert (await read_backup_state()).created_at == EARLIER_BACKUP_TIME
    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.STORED


def store_backup_of_another_identity() -> None:
    write_node_identity_backup(
        encrypt_node_identity_backup(OTHER_KEY_PAIR.public_key, OTHER_KEY_PAIR.private_key, EARLIER_BACKUP_TIME)
    )


def store_damaged_backup(stored_value: str) -> None:
    NodeSetting.objects.create(key=OptionalNodeSettingKey.NODE_IDENTITY_BACKUP.value, value=stored_value)


# Python's json reads 1e400 as an infinite float, which no integer conversion survives.
DOCUMENT_WITH_AN_INFINITE_VERSION = '{"version": 1e400}'


@pytest.mark.parametrize(
    "stored_backup",
    ["taken_with_another_secret_key", "of_another_identity", "damaged", "with_an_infinite_version", "deeply_nested"],
)
async def test_a_backup_that_cannot_restore_the_configured_identity_is_taken_again(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    settings: Settings,
    stored_backup: str,
) -> None:
    await configure_relay_node(fake_companion_firmware)
    public_key = fake_companion_firmware.public_key.hex()
    match stored_backup:
        case "taken_with_another_secret_key":
            current_secret_key = settings.SECRET_KEY
            settings.SECRET_KEY = PREVIOUS_SECRET_KEY
            await in_database(
                store_node_identity_backup, public_key, private_key_of(fake_companion_firmware), EARLIER_BACKUP_TIME
            )
            settings.SECRET_KEY = current_secret_key
            assert (await read_backup_state()).status == NodeIdentityBackupStatus.UNREADABLE
        case "of_another_identity":
            await in_database(store_backup_of_another_identity)
        case "damaged":
            await in_database(store_damaged_backup, "{not json")
        case "with_an_infinite_version":
            await in_database(store_damaged_backup, DOCUMENT_WITH_AN_INFINITE_VERSION)
        case "deeply_nested":
            await in_database(store_damaged_backup, "[" * 100_000 + "]" * 100_000)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    backup_state = await read_backup_state()
    assert backup_state.is_stored_for(public_key)
    assert backup_state.created_at != EARLIER_BACKUP_TIME
    assert await backup_restores_the_node(fake_companion_firmware)
    assert count_key_exports(fake_companion_firmware) == 1


async def test_a_firmware_without_the_export_is_recorded_and_asked_once_per_connection(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="worker.node_identity_backup_keeper")
    fake_companion_firmware.private_key_export_enabled = False
    await configure_relay_node(fake_companion_firmware)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await relay_worker.clock.sleep(0.3)

    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.EXPORT_DISABLED
    assert (await read_backup_state()).status == NodeIdentityBackupStatus.ABSENT
    assert count_key_exports(fake_companion_firmware) == relay_worker.runtime_status.connection_generation
    await wait_for_database(
        lambda: read_worker_status_backup_state() == BackupState.EXPORT_DISABLED,
        description="the worker status to show that the firmware refuses the export",
    )

    generation_before_reboot = relay_worker.runtime_status.connection_generation
    fake_companion_firmware.reboot()
    await relay_worker.wait_for_connection_generation(generation_before_reboot + 1)
    await relay_worker.clock.sleep(0.3)

    connection_count = relay_worker.runtime_status.connection_generation
    assert count_key_exports(fake_companion_firmware) == connection_count
    export_warnings = [
        record for record in caplog.records if "does not allow exporting its private key" in record.getMessage()
    ]
    assert len(export_warnings) == connection_count
    assert all(record.levelno == logging.WARNING for record in export_warnings)


async def test_a_node_that_is_not_the_configured_one_is_never_asked_for_its_key(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    await in_database(point_node_setting_at_another_node)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.IDENTITY_MISMATCH)
    await relay_worker.clock.sleep(0.2)

    assert count_key_exports(fake_companion_firmware) == 0
    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.MISSING
    assert (await read_backup_state()).status == NodeIdentityBackupStatus.ABSENT


async def test_a_node_that_was_never_set_up_is_not_backed_up(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.NOT_CONFIGURED)
    await relay_worker.clock.sleep(0.2)

    assert count_key_exports(fake_companion_firmware) == 0
    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.NOT_CHECKED


async def test_an_export_that_gets_no_reply_is_recorded_as_failed_and_the_node_relays_all_the_same(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.EXPORT_PRIVATE_KEY)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.FAILED
    assert (await read_backup_state()).status == NodeIdentityBackupStatus.ABSENT
    assert count_key_exports(fake_companion_firmware) == relay_worker.runtime_status.connection_generation


async def test_an_exported_key_that_does_not_derive_the_nodes_public_key_is_never_stored(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    fake_companion_firmware.exported_private_key_override = OTHER_KEY_PAIR.private_key.reveal_bytes()

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    assert relay_worker.runtime_status.node_identity_backup_state == BackupState.FAILED
    assert (await read_backup_state()).status == NodeIdentityBackupStatus.ABSENT
    assert count_key_exports(fake_companion_firmware) == relay_worker.runtime_status.connection_generation
