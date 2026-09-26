"""Recording and processing inbound direct messages: persistence first, failures contained, route resets."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from django.utils import timezone

from directory.accounts import user_exists
from directory.models import Contact
from hoptalk_relay.relay_settings import get_relay_settings
from messaging.inbound_log import find_unprocessed_inbox_row_ids
from messaging.models import InboundDirectMessage
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware, TextMessageQueued
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.simulated_mesh import SimulatedDevice, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    DeviceInbox,
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    create_user,
    in_database,
    read_contact,
    wait_for_database,
)
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

HELD_BACK_RECIPROCAL_PATH_DELAY_SECONDS = 3600.0


async def prepare_signed_in_device(
    fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh, device_name: str = "tracker"
) -> tuple[SimulatedDevice, Contact]:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device(device_name)
    user = await in_database(create_user, "alice")
    contact = await in_database(create_contact_for_device, device, user=user)
    return device, contact


def read_inbox_rows() -> list[InboundDirectMessage]:
    return list(InboundDirectMessage.objects.order_by("id"))


def count_reset_path_commands(firmware: FakeCompanionFirmware) -> int:
    return sum(1 for received_command in firmware.command_log if received_command.code == CommandCode.RESET_PATH)


def list_command_codes(firmware: FakeCompanionFirmware) -> list[int]:
    return [received_command.code for received_command in firmware.command_log]


def hold_back_reciprocal_paths(device: SimulatedDevice) -> None:
    """The device's node answers a flooded returned path with its own path to the relay only after the test.

    Sent at once, that path gives the relay's node a route to the device milliseconds after a
    flooded DM arrived, and a slow worker can record the PATH_UPDATE before it processes the DM:
    a route learned that recently is rightly kept instead of reset.
    """
    device.firmware.timing = replace(
        device.firmware.timing, reciprocal_path_delay_seconds=HELD_BACK_RECIPROCAL_PATH_DELAY_SECONDS
    )


def make_learned_route_no_longer_recent(contact_id: int) -> None:
    """The route the relay's node learned to the contact is too old to keep a flood arrival from resetting it."""
    recent_path_update_seconds = get_relay_settings().engine_timing.recent_path_update_seconds
    Contact.objects.filter(id=contact_id).update(
        last_path_update_at=timezone.now() - timedelta(seconds=recent_path_update_seconds + 1)
    )


async def test_a_frame_is_recorded_before_it_has_any_effect_and_processed_after_a_restart(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, _contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    inbox = DeviceInbox(device)
    processing_never_ends = asyncio.Event()

    async def process_nothing(_inbox_row_id: int) -> None:
        await processing_never_ends.wait()

    monkeypatch.setattr(relay_worker.worker.inbound_processor, "process_row", process_nothing)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    device.send_direct_message("HT1 Q bob")
    await wait_for_database(lambda: len(find_unprocessed_inbox_row_ids()) == 1, description="the recorded row")
    assert inbox.texts == []

    await relay_worker.restart()

    await inbox.wait_for_text("HT1 q bob 0")
    inbox_rows = await in_database(read_inbox_rows)
    assert [inbox_row.processing_state for inbox_row in inbox_rows] == [InboundDirectMessage.ProcessingState.PROCESSED]


async def test_an_exception_in_processing_fails_that_row_and_the_next_row_is_processed(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, _contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    inbox = DeviceInbox(device)
    failures_left = [1]

    def fail_once(*arguments: Any) -> Any:
        if failures_left:
            failures_left.pop()
            raise RuntimeError("an injected failure")
        return user_exists(*arguments)

    monkeypatch.setattr("messaging.request_processing.user_exists", fail_once)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    device.send_direct_message("HT1 Q bob")
    await wait_for_database(
        lambda: [row.processing_state for row in read_inbox_rows()] == [InboundDirectMessage.ProcessingState.FAILED],
        description="the first row to fail",
    )
    device.send_direct_message("HT1 Q carol")

    await inbox.wait_for_text("HT1 q carol 0")
    assert "HT1 q bob 0" not in inbox.texts
    assert relay_worker.runtime_status.connection_generation == 1


async def test_an_exception_escaping_a_task_restarts_that_task_alone(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device, _contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    inbox = DeviceInbox(device)
    failures_left = [1]

    def fail_once(limit: int) -> list[int]:
        if failures_left:
            failures_left.pop()
            raise RuntimeError("the database went away")
        return find_unprocessed_inbox_row_ids(limit)

    monkeypatch.setattr("worker.inbound_processor.find_unprocessed_inbox_row_ids", fail_once)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    device.send_direct_message("HT1 Q bob")

    await inbox.wait_for_text("HT1 q bob 0")
    assert "inbound_processor" in relay_worker.runtime_status.last_error_message
    assert relay_worker.runtime_status.connection_generation == 1


async def test_a_flood_arrival_resets_the_route_before_the_reply(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device, _contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    hold_back_reciprocal_paths(device)
    inbox = DeviceInbox(device)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    device.send_direct_message("HT1 Q bob")

    await inbox.wait_for_text("HT1 q bob 0")
    inbox_rows = await in_database(read_inbox_rows)
    assert inbox_rows[0].path_length != 255
    assert inbox_rows[0].route_reset_performed
    assert count_reset_path_commands(fake_companion_firmware) == 1
    command_codes = list_command_codes(fake_companion_firmware)
    assert command_codes.index(CommandCode.RESET_PATH) < command_codes.index(CommandCode.SEND_TEXT_MESSAGE)


async def test_a_flood_arrival_keeps_a_route_learned_in_the_last_thirty_seconds(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device, contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    await in_database(
        Contact.objects.filter(id=contact.pk).update, last_path_update_at=timezone.now() - timedelta(seconds=10)
    )
    inbox = DeviceInbox(device)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    device.send_direct_message("HT1 Q bob")

    await inbox.wait_for_text("HT1 q bob 0")
    inbox_rows = await in_database(read_inbox_rows)
    assert not inbox_rows[0].route_reset_performed
    assert count_reset_path_commands(fake_companion_firmware) == 0


async def test_a_firmware_repeat_is_counted_and_neither_resets_nor_answers(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    device, contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    hold_back_reciprocal_paths(device)
    inbox = DeviceInbox(device)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    first_send = device.send_direct_message("HT1 Q bob")
    assert isinstance(first_send, TextMessageQueued)
    await inbox.wait_for_text("HT1 q bob 0")
    assert count_reset_path_commands(fake_companion_firmware) == 1
    # The repeat floods and no recent route keeps it from a reset, so a repeat taken for a new DM would reset.
    await wait_for_database(
        lambda: read_contact(contact.pk).last_path_update_at is not None,
        description="the route the relay's node learned from its flooded reply",
    )
    await in_database(make_learned_route_no_longer_recent, contact.pk)
    first_sender_timestamp = (await in_database(read_inbox_rows))[0].sender_timestamp
    assert device.reset_route_to_relay()
    repeat_send = device.send_direct_message("HT1 Q bob", sender_timestamp=first_sender_timestamp, attempt=1)
    assert isinstance(repeat_send, TextMessageQueued)
    assert repeat_send.sent_by_flood

    await wait_for_database(
        lambda: read_inbox_rows()[0].duplicate_count == 1, description="the repeat counted on the first row"
    )
    await relay_worker.clock.sleep(0.3)
    assert inbox.texts == ["HT1 q bob 0"]
    assert len(await in_database(read_inbox_rows)) == 1
    assert count_reset_path_commands(fake_companion_firmware) == 1


@pytest.mark.parametrize(
    ("text", "expected_reply"),
    [
        ("hello there", None),
        ("HT1 k bob 1 1", None),
        ("HT1 K bob 1 1", None),
        ("HT2 A bob", "HT1 e VERSION ? 2"),
        ("HT1 X something", "HT1 e UNSUPPORTED X"),
        ("HT1 Q", "HT1 e SYNTAX Q"),
    ],
)
async def test_only_requests_are_answered(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    text: str,
    expected_reply: str | None,
) -> None:
    device, _contact = await prepare_signed_in_device(fake_companion_firmware, simulated_mesh)
    inbox = DeviceInbox(device)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)

    device.send_direct_message(text)
    await wait_for_database(
        lambda: [row.processing_state for row in read_inbox_rows()] == [InboundDirectMessage.ProcessingState.PROCESSED],
        description="the row processed",
    )

    if expected_reply is None:
        await relay_worker.clock.sleep(0.3)
        assert inbox.texts == []
    else:
        await inbox.wait_for_text(expected_reply)


async def test_a_message_from_a_node_the_contacts_table_does_not_know_triggers_a_reconciliation(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    stranger = simulated_mesh.add_device("stranger")
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_until(
        lambda: stranger.public_key not in {record.public_key for record in fake_companion_firmware.contact_records()},
        description="the first reconciliation to remove the stranger",
    )

    inbox_rows = await in_database(read_inbox_rows)
    assert inbox_rows == []
