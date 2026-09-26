"""Converging the node's contact table to the contacts table, against the fake node."""

from typing import Any

import pytest
from django.utils import timezone

from directory.models import Contact
from messaging.models import OutboundPacket
from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.frames import CommandCode
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import (
    RelayWorkerHarness,
    configure_relay_node,
    create_contact_for_device,
    create_packet_awaiting_acknowledgement_until,
    in_database,
    point_node_setting_at_another_node,
    read_contact,
    wait_for_database,
)
from worker.node_event_subscriptions import NodeDeletedContact
from worker.worker_state import RelayMode

pytestmark = pytest.mark.django_db(transaction=True)

NODE_CONTACT_CAPACITY = 350


def build_numbered_public_key(number: int) -> str:
    """Distinct in the first six bytes, as contacts must be."""
    return f"{number:012x}" + "ab" * 26


def create_database_contact(
    public_key: str, *, node_sync_state: Contact.NodeSyncState = Contact.NodeSyncState.ON_NODE, **fields: Any
) -> Contact:
    return Contact.objects.create(
        public_key=public_key,
        name=fields.pop("name", f"node {public_key[:6]}"),
        source=Contact.Source.CARD,
        added_at=timezone.now(),
        node_sync_state=node_sync_state,
        **fields,
    )


def add_record_to_node(firmware: FakeCompanionFirmware, public_key: str, name: str = "on the node") -> None:
    firmware.add_or_update_contact(
        ContactRecord.create(public_key=bytes.fromhex(public_key), name=name, last_modified=firmware.clock_time())
    )


def node_holds(firmware: FakeCompanionFirmware, public_key: str) -> bool:
    return firmware.find_contact(bytes.fromhex(public_key)) is not None


def count_add_contact_commands_for(firmware: FakeCompanionFirmware, public_key: str) -> int:
    return sum(
        1
        for received_command in firmware.command_log
        if received_command.code == CommandCode.ADD_UPDATE_CONTACT and received_command.frame[1:33].hex() == public_key
    )


async def test_a_missing_contact_is_added_with_its_full_record_and_no_route(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    public_key = build_numbered_public_key(1)
    contact = await in_database(
        create_database_contact,
        public_key,
        node_sync_state=Contact.NodeSyncState.PENDING_ADD,
        name="Zoë's tracker",
        advert_timestamp=1_790_000_000,
        latitude_microdegrees=-37_813_600,
        longitude_microdegrees=144_963_100,
    )

    relay_worker.start()

    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact on the node",
    )
    record = fake_companion_firmware.find_contact(bytes.fromhex(public_key))
    assert record is not None
    assert not record.has_known_route
    assert record.name == "Zoë's tracker".encode()
    assert record.last_advert_timestamp == 1_790_000_000
    assert (record.latitude_microdegrees, record.longitude_microdegrees) == (-37_813_600, 144_963_100)
    assert fake_companion_firmware.protocol_violations == []
    synced_contact = await in_database(read_contact, contact.pk)
    assert synced_contact.node_synced_at is not None
    assert synced_contact.node_out_path_length == -1


async def test_an_extra_contact_is_removed_only_once_no_packet_awaits_an_ack_even_from_before_a_restart(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    extra_public_key = build_numbered_public_key(2)
    add_record_to_node(fake_companion_firmware, extra_public_key)
    await in_database(create_packet_awaiting_acknowledgement_until, 5.0)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await relay_worker.clock.sleep(1.0)
    assert node_holds(fake_companion_firmware, extra_public_key)

    relay_worker.clock.advance(seconds=relay_worker.timing.postponed_removal_retry_seconds + 5)

    await wait_until(
        lambda: not node_holds(fake_companion_firmware, extra_public_key), description="the extra contact removed"
    )


async def test_a_removal_waits_out_the_latest_ack_deadline_in_one_pause_while_the_sender_starts_nothing(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    extra_public_key = build_numbered_public_key(3)
    add_record_to_node(fake_companion_firmware, extra_public_key)
    device = simulated_mesh.add_device("tracker")
    await in_database(create_contact_for_device, device)
    await in_database(create_packet_awaiting_acknowledgement_until, 3.0)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    sending_gate = relay_worker.worker.worker_state.sending_gate
    await wait_until(lambda: sending_gate.is_paused, description="the removal to pause sending")

    device.send_direct_message("HT1 Q bob")
    await wait_until(lambda: len(relay_worker.worker.reply_queue) == 1, description="the reply queued")
    await relay_worker.clock.sleep(relay_worker.timing.minimum_removal_quiet_wait_seconds + 0.5)
    assert sending_gate.is_paused
    assert await in_database(count_reply_packets) == 0
    assert node_holds(fake_companion_firmware, extra_public_key)

    await wait_for_database(lambda: count_reply_packets() == 1, description="the reply once the removal is done")
    assert not node_holds(fake_companion_firmware, extra_public_key)
    assert not sending_gate.is_paused


def count_reply_packets() -> int:
    """Replies this worker sent; the packet from before the restart has no contact."""
    return OutboundPacket.objects.filter(purpose=OutboundPacket.Purpose.REPLY, contact__isnull=False).count()


async def test_removals_that_waited_are_not_made_on_another_board_attached_meanwhile(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    extra_public_key = build_numbered_public_key(8)
    add_record_to_node(fake_companion_firmware, extra_public_key)
    await in_database(create_packet_awaiting_acknowledgement_until, 60.0)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    sending_gate = relay_worker.worker.worker_state.sending_gate
    await wait_until(lambda: sending_gate.is_paused, description="the removal to wait for the ACK")

    await replace_the_board_with_one_that_holds(fake_companion_firmware, extra_public_key)
    await relay_worker.wait_for_relay_mode(RelayMode.IDENTITY_MISMATCH)
    relay_worker.clock.advance(seconds=61)

    await wait_until(lambda: not sending_gate.is_paused, description="the removal pass to end")
    assert node_holds(fake_companion_firmware, extra_public_key)


async def replace_the_board_with_one_that_holds(firmware: FakeCompanionFirmware, public_key: str) -> None:
    """Another board, with an identity of its own, that happens to know the same contact."""
    original_public_key = firmware.public_key
    firmware.factory_reset()
    await wait_until(
        lambda: firmware.is_running and firmware.public_key != original_public_key, description="another board"
    )
    add_record_to_node(firmware, public_key)


@pytest.mark.parametrize("relay_mode", [RelayMode.IDENTITY_MISMATCH, RelayMode.NOT_CONFIGURED])
async def test_nothing_is_added_or_removed_while_the_node_is_not_the_configured_one(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, relay_mode: RelayMode
) -> None:
    if relay_mode == RelayMode.IDENTITY_MISMATCH:
        await configure_relay_node(fake_companion_firmware)
        await in_database(point_node_setting_at_another_node)
    extra_public_key = build_numbered_public_key(4)
    add_record_to_node(fake_companion_firmware, extra_public_key)
    pending_public_key = build_numbered_public_key(5)
    await in_database(create_database_contact, pending_public_key, node_sync_state=Contact.NodeSyncState.PENDING_ADD)

    relay_worker.start()
    await relay_worker.wait_for_relay_mode(relay_mode)
    relay_worker.worker.signals.reconcile_requested.set()
    await relay_worker.clock.sleep(0.3)

    assert node_holds(fake_companion_firmware, extra_public_key)
    assert not node_holds(fake_companion_firmware, pending_public_key)
    assert CommandCode.GET_CONTACTS not in [command.code for command in fake_companion_firmware.command_log]


def fill_database_and_node_to_capacity(firmware: FakeCompanionFirmware) -> tuple[str, str]:
    """349 contacts on both sides, X only on the node (it was deleted), Y only in the database (it was added)."""
    shared_public_keys = [build_numbered_public_key(number) for number in range(10, 10 + NODE_CONTACT_CAPACITY - 1)]
    Contact.objects.bulk_create(
        Contact(
            public_key=public_key,
            name=f"node {number}",
            source=Contact.Source.CARD,
            added_at=timezone.now(),
            node_sync_state=Contact.NodeSyncState.ON_NODE,
        )
        for number, public_key in enumerate(shared_public_keys)
    )
    for public_key in shared_public_keys:
        add_record_to_node(firmware, public_key)
    deleted_public_key = build_numbered_public_key(1000)
    add_record_to_node(firmware, deleted_public_key)
    added_public_key = build_numbered_public_key(1001)
    create_database_contact(added_public_key, node_sync_state=Contact.NodeSyncState.PENDING_ADD)
    return deleted_public_key, added_public_key


async def test_at_capacity_the_new_contact_is_added_in_the_same_pass_once_the_deleted_one_is_removed(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    deleted_public_key, added_public_key = await in_database(
        fill_database_and_node_to_capacity, fake_companion_firmware
    )
    assert len(fake_companion_firmware.contact_records()) == NODE_CONTACT_CAPACITY

    relay_worker.start()

    await wait_until(
        lambda: node_holds(fake_companion_firmware, added_public_key), timeout_seconds=10, description="Y added"
    )
    assert not node_holds(fake_companion_firmware, deleted_public_key)
    assert count_add_contact_commands_for(fake_companion_firmware, added_public_key) == 2
    await wait_for_database(
        lambda: Contact.objects.get(public_key=added_public_key).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="Y marked on the node",
    )
    added_contact = await in_database(Contact.objects.get, public_key=added_public_key)
    assert added_contact.node_sync_error == ""
    assert relay_worker.runtime_status.last_error_message == ""


async def test_a_contact_already_on_the_node_is_never_written_again(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    stored_record = fake_companion_firmware.find_contact(device.public_key)
    assert stored_record is not None
    fake_companion_firmware.add_or_update_contact(
        stored_record.with_route(
            route_path=b"\x42", path_hash_size=1, last_modified=fake_companion_firmware.clock_time()
        )
    )
    contact = await in_database(create_contact_for_device, device, node_sync_state=Contact.NodeSyncState.PENDING_ADD)

    relay_worker.start()

    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact marked on the node",
    )
    record_after_the_pass = fake_companion_firmware.find_contact(device.public_key)
    assert record_after_the_pass is not None
    assert record_after_the_pass.route_path == b"\x42"
    assert count_add_contact_commands_for(fake_companion_firmware, device.public_key.hex()) == 0
    assert (await in_database(read_contact, contact.pk)).node_out_path_length == 1


async def test_a_pass_that_cannot_list_the_node_changes_nothing(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware
) -> None:
    await configure_relay_node(fake_companion_firmware)
    extra_public_key = build_numbered_public_key(6)
    add_record_to_node(fake_companion_firmware, extra_public_key)
    pending_contact = await in_database(
        create_database_contact, build_numbered_public_key(7), node_sync_state=Contact.NodeSyncState.PENDING_ADD
    )
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.GET_CONTACTS)

    relay_worker.start()
    await wait_until(
        lambda: CommandCode.GET_CONTACTS in [command.code for command in fake_companion_firmware.command_log],
        description="the listing",
    )
    await relay_worker.clock.sleep(relay_worker.timing.contact_listing_activity_seconds + 0.2)

    assert node_holds(fake_companion_firmware, extra_public_key)
    assert (await in_database(read_contact, pending_contact.pk)).node_sync_state == Contact.NodeSyncState.PENDING_ADD


async def test_a_contact_the_node_deleted_by_itself_is_reported_and_reconciled(
    relay_worker: RelayWorkerHarness, fake_companion_firmware: FakeCompanionFirmware, simulated_mesh: SimulatedMesh
) -> None:
    await configure_relay_node(fake_companion_firmware)
    device = simulated_mesh.add_device("tracker")
    contact = await in_database(create_contact_for_device, device)
    relay_worker.start()
    await relay_worker.wait_for_relay_mode(RelayMode.RUNNING)
    await wait_until(lambda: count_listings(fake_companion_firmware) == 1, description="the first reconciliation")

    relay_worker.worker.node_event_queue.put_nowait(NodeDeletedContact(public_key=device.public_key.hex()))

    await wait_until(lambda: count_listings(fake_companion_firmware) == 2, description="a reconciliation pass")
    assert "deleted contact" in relay_worker.runtime_status.last_error_message
    await wait_for_database(
        lambda: read_contact(contact.pk).node_sync_state == Contact.NodeSyncState.ON_NODE,
        description="the contact found on the node again",
    )


def count_listings(firmware: FakeCompanionFirmware) -> int:
    return sum(1 for received_command in firmware.command_log if received_command.code == CommandCode.GET_CONTACTS)
