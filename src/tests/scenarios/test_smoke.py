"""The whole relay once, end to end: two reference clients talk through the real worker, the fake node and the mesh."""

import pytest

from messaging.models import Message
from protocol.constants import ReceiptLevel
from tests.invariants import assert_all_invariants
from tests.scenarios.scenario_setup import (
    ClientStarter,
    add_device_from_its_card,
    every_contact_is_on_node,
    sign_in,
)
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    in_database,
    wait_for_database,
)
from tests.worker.simulated_hoptalk_client_records import OutgoingMessageStatus
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

# 164 bytes, since a Cyrillic letter takes two: more than one 104-byte part holds.
TWO_PART_CYRILLIC_TEXT = "Привет, Боб! Пишу тебе через ретранслятор: сообщение длинное, поэтому уйдёт двумя частями."


def read_only_message() -> Message:
    return Message.objects.get()


async def test_a_two_part_cyrillic_message_reaches_the_recipient_and_the_sender_sees_it_delivered_then_read(
    relay_worker: RelayWorkerHarness,
    fake_companion_firmware: FakeCompanionFirmware,
    simulated_mesh: SimulatedMesh,
    start_client: ClientStarter,
) -> None:
    await configure_relay_node(fake_companion_firmware)
    alice_device = await add_device_from_its_card(simulated_mesh, "alice-phone")
    bob_device = await add_device_from_its_card(simulated_mesh, "bob-phone")
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_for_database(every_contact_is_on_node, description="both devices to be put on the relay's node")

    alice = start_client(alice_device)
    bob = start_client(bob_device)
    await sign_in(alice, "alice")
    await sign_in(bob, "bob")

    message = alice.send_message("bob", TWO_PART_CYRILLIC_TEXT)
    [received_message] = await bob.wait_for_received_messages("alice", 1)
    await message.wait_for_status(OutgoingMessageStatus.DELIVERED)
    read_confirmation = bob.mark_read("alice", received_message.message_id)
    await read_confirmation.wait_until_finished()
    await message.wait_for_status(OutgoingMessageStatus.READ)

    await relay_worker.stop()
    assert len(message.parts) == 2
    assert received_message.text == TWO_PART_CYRILLIC_TEXT
    assert received_message.message_id == message.message_id
    assert list(dict.fromkeys(message.received_receipt_levels)) == [ReceiptLevel.DELIVERED, ReceiptLevel.READ]
    stored_message = await in_database(read_only_message)
    assert stored_message.text == TWO_PART_CYRILLIC_TEXT
    assert stored_message.delivered_at is not None
    assert stored_message.read_at is not None
    await in_database(assert_all_invariants)
