"""The gateway's private key export and import against the fake node, and the library's frame logging held back."""

import logging
import random
from dataclasses import replace
from typing import Any

import pytest
from meshcore import EventType

from node.node_identity_backups import NodePrivateKey
from tests.private_key_checks import mentions_private_key
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.node_identity import NodeIdentity
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import FAST_WORKER_TIMING
from worker.node_gateway import (
    NodeGateway,
    NodeRejectedCommandError,
    NodeReplyLostError,
    PrivateKeyExportDisabled,
    PrivateKeyExported,
    PrivateKeyImportDisabled,
    PrivateKeyImported,
    PrivateKeyImportRefused,
    UnexpectedNodeReplyError,
    is_lost_reply,
)
from worker.private_key_log_guard import LIBRARY_LOGGER_NAME, install_library_frame_log_guard

GATEWAY_TIMING = replace(FAST_WORKER_TIMING, node_command_timeout_seconds=0.15, late_reply_grace_seconds=0.01)
IMPORTED_IDENTITY_SEED = 7
ERR_CODE_ILLEGAL_ARGUMENT = 6
# The earlier command gives up long before the node answers it, and the export waits long after.
PATIENT_GATEWAY_TIMING = replace(FAST_WORKER_TIMING, node_command_timeout_seconds=5.0, late_reply_grace_seconds=0.01)
IMPATIENT_COMMAND_TIMEOUT_SECONDS = 0.1
NODE_BUSY_SECONDS = 1.0
UNKNOWN_CONTACT_PUBLIC_KEY = bytes(32)


class ReconnectRequests:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def request_reconnect(self, reason: str) -> None:
        self.reasons.append(reason)


def build_gateway(meshcore_client: Any, timing: Any = GATEWAY_TIMING) -> tuple[NodeGateway, ReconnectRequests]:
    reconnect_requests = ReconnectRequests()
    gateway = NodeGateway(timing=timing, request_reconnect=reconnect_requests.request_reconnect)
    gateway.attach(meshcore_client)
    return gateway, reconnect_requests


def generate_private_key(seed: int) -> NodePrivateKey:
    return NodePrivateKey(NodeIdentity.generate(random.Random(seed)).expanded_private_key)


def commands_sent(firmware: FakeCompanionFirmware) -> list[int]:
    return [received_command.code for received_command in firmware.command_log]


async def test_the_export_returns_the_key_and_the_public_key_read_right_after_it(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    commands_before = len(fake_companion_firmware.command_log)

    export_outcome = await gateway.export_private_key()

    assert isinstance(export_outcome, PrivateKeyExported)
    exported_key_is_the_nodes = export_outcome.private_key == NodePrivateKey(
        fake_companion_firmware.identity.expanded_private_key
    )
    assert exported_key_is_the_nodes
    assert export_outcome.public_key == fake_companion_firmware.public_key.hex()
    assert commands_sent(fake_companion_firmware)[commands_before:] == [
        CommandCode.EXPORT_PRIVATE_KEY,
        CommandCode.APP_START,
    ]
    assert fake_companion_firmware.command_log[commands_before].frame == b"\x17"
    assert "<hidden>" in repr(export_outcome)


async def test_the_import_sends_the_whole_key_in_one_frame_and_the_node_takes_the_identity(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    private_key = generate_private_key(IMPORTED_IDENTITY_SEED)

    import_outcome = await gateway.import_private_key(private_key)

    assert import_outcome == PrivateKeyImported()
    import_frame_is_the_key = fake_companion_firmware.command_log[-1].frame == b"\x18" + private_key.reveal_bytes()
    assert import_frame_is_the_key
    self_information = await gateway.read_self_information()
    assert self_information.public_key == NodeIdentity(private_key.reveal_bytes()).public_key.hex()
    assert fake_companion_firmware.protocol_violations == []


async def test_an_invalid_key_is_refused_with_error_six_and_the_identity_stays(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    original_public_key = fake_companion_firmware.public_key
    unclamped_key_bytes = bytearray(generate_private_key(IMPORTED_IDENTITY_SEED).reveal_bytes())
    unclamped_key_bytes[0] |= 0x07

    import_outcome = await gateway.import_private_key(NodePrivateKey(bytes(unclamped_key_bytes)))

    assert import_outcome == PrivateKeyImportRefused(error_code=ERR_CODE_ILLEGAL_ARGUMENT)
    assert fake_companion_firmware.public_key == original_public_key


async def test_a_firmware_without_the_commands_answers_disabled_to_both(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, reconnect_requests = build_gateway(meshcore_client)
    fake_companion_firmware.private_key_export_enabled = False
    fake_companion_firmware.private_key_import_enabled = False

    export_outcome = await gateway.export_private_key()
    import_outcome = await gateway.import_private_key(generate_private_key(IMPORTED_IDENTITY_SEED))

    assert export_outcome == PrivateKeyExportDisabled()
    assert import_outcome == PrivateKeyImportDisabled()
    assert CommandCode.APP_START not in commands_sent(fake_companion_firmware)[-2:]
    assert reconnect_requests.reasons == []


async def test_a_lost_export_reply_fails_and_keeps_the_library_quiet_until_the_link_is_torn_down(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    library_frame_log_guard = install_library_frame_log_guard()
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.EXPORT_PRIVATE_KEY)

    with pytest.raises(NodeReplyLostError):
        await gateway.export_private_key()

    assert library_frame_log_guard.is_held
    gateway.detach()
    assert not library_frame_log_guard.is_held


async def test_the_library_logs_no_frame_that_carries_the_key_at_any_level(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=LIBRARY_LOGGER_NAME)
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    imported_key = generate_private_key(IMPORTED_IDENTITY_SEED)

    export_outcome = await gateway.export_private_key()
    await gateway.import_private_key(imported_key)
    await gateway.read_self_information()

    assert isinstance(export_outcome, PrivateKeyExported)
    logged_texts = [caplog.text, *(record.getMessage() for record in caplog.records)]
    key_is_logged = any(
        mentions_private_key(logged_text, private_key)
        for logged_text in logged_texts
        for private_key in (export_outcome.private_key, imported_key)
    )
    assert not key_is_logged
    library_frames_are_logged_otherwise = any(
        record.name == LIBRARY_LOGGER_NAME and record.getMessage().startswith("Sending raw data: 01")
        for record in caplog.records
    )
    assert library_frames_are_logged_otherwise


async def test_an_exported_key_that_does_not_derive_the_reported_public_key_is_refused(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    foreign_private_key = generate_private_key(IMPORTED_IDENTITY_SEED)
    fake_companion_firmware.exported_private_key_override = foreign_private_key.reveal_bytes()

    with pytest.raises(UnexpectedNodeReplyError, match="does not belong to the key"):
        await gateway.export_private_key()


def record_private_key_events(meshcore_client: Any) -> list[Any]:
    private_key_events: list[Any] = []

    async def record_private_key_event(event: Any) -> None:
        private_key_events.append(event)

    meshcore_client.subscribe(EventType.PRIVATE_KEY, record_private_key_event)
    return private_key_events


async def test_a_late_error_to_an_earlier_command_keeps_the_library_quiet_until_the_key_has_come_and_gone(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger=LIBRARY_LOGGER_NAME)
    gateway, _reconnect_requests = build_gateway(meshcore_client, PATIENT_GATEWAY_TIMING)
    library_frame_log_guard = install_library_frame_log_guard()
    node_private_key = NodePrivateKey(fake_companion_firmware.identity.expanded_private_key)
    private_key_events = record_private_key_events(meshcore_client)
    fake_companion_firmware.delay_next_reply(NODE_BUSY_SECONDS, command_code=CommandCode.RESET_PATH)
    reset_path_reply = await gateway.send_command_frame(
        bytes([CommandCode.RESET_PATH]) + UNKNOWN_CONTACT_PUBLIC_KEY,
        [EventType.OK, EventType.ERROR],
        timeout_seconds=IMPATIENT_COMMAND_TIMEOUT_SECONDS,
    )
    assert is_lost_reply(reset_path_reply)

    with pytest.raises(NodeRejectedCommandError):
        await gateway.export_private_key()
    await wait_until(lambda: bool(private_key_events), description="the node's key reply to reach the library")

    key_is_logged = any(mentions_private_key(record.getMessage(), node_private_key) for record in caplog.records)
    assert not key_is_logged
    assert library_frame_log_guard.is_held
    gateway.detach()
    assert not library_frame_log_guard.is_held
