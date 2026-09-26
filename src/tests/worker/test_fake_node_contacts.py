"""The contact table, contact listings, adverts and cards behave as in firmware v1.17.1."""

from typing import Any

from meshcore import EventType

from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware, ReceptionOutcome
from tests.worker.fake_node.frames import FirmwareErrorCode, NodeType
from tests.worker.fake_node.meshcore_events import MeshCoreEventRecorder, is_error_with_code
from tests.worker.fake_node.radio_packets import AdvertPacket
from tests.worker.fake_node.simulated_mesh import DeliveryOutcome, SimulatedMesh
from tests.worker.fake_node.waiting import wait_until

OK_OR_ERROR = [EventType.OK, EventType.ERROR]
MANUAL_ADD_ON_FRAME = b"\x26\x01\x00\x00\x02"
FIRST_LAST_MODIFIED = 1_800_000_000


def numbered_public_key(number: int) -> bytes:
    return number.to_bytes(2, "big") + bytes([0xAB]) * 30


def add_numbered_contacts(firmware: FakeCompanionFirmware, contact_count: int) -> list[ContactRecord]:
    records = [
        ContactRecord.create(
            public_key=numbered_public_key(number),
            name=f"contact {number}",
            last_modified=FIRST_LAST_MODIFIED + number,
        )
        for number in range(contact_count)
    ]
    for record in records:
        firmware.add_or_update_contact(record)
    return records


def library_contact(public_key: bytes, name: str) -> dict[str, Any]:
    """A contact dict as meshcore's add_contact takes it, with no route."""
    return {
        "public_key": public_key.hex(),
        "type": NodeType.CHAT,
        "flags": 0,
        "out_path": "",
        "out_path_len": -1,
        "out_path_hash_mode": -1,
        "adv_name": name,
        "last_advert": 1_790_000_000,
        "adv_lat": 0.0,
        "adv_lon": 0.0,
    }


async def test_a_listing_streams_contacts_modified_after_since(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    records = add_numbered_contacts(fake_companion_firmware, 5)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEXT_CONTACT, EventType.CONTACTS)

    await meshcore_client.commands.get_contacts_async(lastmod=FIRST_LAST_MODIFIED + 2)
    contacts_event = await recorder.wait_for_event(EventType.CONTACTS)

    assert list(contacts_event.payload) == [records[3].public_key.hex(), records[4].public_key.hex()]
    assert contacts_event.attributes == {"lastmod": FIRST_LAST_MODIFIED + 4}
    assert len(recorder.of_type(EventType.NEXT_CONTACT)) == 2


async def test_a_full_table_of_350_contacts_streams_in_one_listing(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    add_numbered_contacts(fake_companion_firmware, 350)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.CONTACTS)

    await meshcore_client.commands.get_contacts_async()
    contacts_event = await recorder.wait_for_event(EventType.CONTACTS)

    assert len(contacts_event.payload) == 350
    assert contacts_event.attributes == {"lastmod": FIRST_LAST_MODIFIED + 349}


async def test_a_second_listing_while_one_is_streaming_is_refused_with_bad_state(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    add_numbered_contacts(fake_companion_firmware, 60)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.CONTACTS)

    await meshcore_client.commands.get_contacts_async()
    second_listing = await meshcore_client.commands.send(b"\x04", [EventType.ERROR, EventType.CONTACTS])
    first_listing = await recorder.wait_for_event(EventType.CONTACTS)

    assert is_error_with_code(second_listing, FirmwareErrorCode.BAD_STATE)
    assert len(first_listing.payload) == 60


async def test_an_app_start_abandons_a_running_listing(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    add_numbered_contacts(fake_companion_firmware, 60)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.CONTACTS)

    await meshcore_client.commands.get_contacts_async()
    await meshcore_client.commands.send_appstart()
    # Replies come in order, so once this one is here the node has finished everything before it.
    await meshcore_client.commands.get_time()

    assert recorder.of_type(EventType.CONTACTS) == []


async def test_contact_frames_carry_the_path_hash_size_in_the_top_bits_of_the_path_length(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    routed_contact = ContactRecord.create(public_key=numbered_public_key(1), name="routed").with_route(
        route_path=bytes.fromhex("a1b2c3d4"), path_hash_size=2, last_modified=FIRST_LAST_MODIFIED
    )
    fake_companion_firmware.add_or_update_contact(routed_contact)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.CONTACTS)

    await meshcore_client.commands.get_contacts_async()
    listed_contact = (await recorder.wait_for_event(EventType.CONTACTS)).payload[routed_contact.public_key.hex()]

    assert routed_contact.out_path_length == 0x42
    assert listed_contact["out_path_len"] == 2
    assert listed_contact["out_path_hash_mode"] == 1
    assert listed_contact["out_path"] == "a1b2c3d4"


async def test_add_contact_stores_the_library_record_and_refuses_frames_below_144_bytes(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    added = await meshcore_client.commands.add_contact(library_contact(numbered_public_key(7), "Seven"))
    short_frame = await meshcore_client.commands.send(b"\x09" + numbered_public_key(8) + bytes(103), OK_OR_ERROR)

    stored_contact = fake_companion_firmware.find_contact(numbered_public_key(7))
    assert added.type == EventType.OK
    assert stored_contact is not None
    assert stored_contact.name == b"Seven"
    assert stored_contact.last_advert_timestamp == 1_790_000_000
    assert not stored_contact.has_known_route
    assert is_error_with_code(short_frame, FirmwareErrorCode.ILLEGAL_ARGUMENT)
    assert fake_companion_firmware.find_contact(numbered_public_key(8)) is None
    assert len(fake_companion_firmware.protocol_violations) == 1


async def test_the_351st_contact_is_refused_unless_the_oldest_may_be_overwritten(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    records = add_numbered_contacts(fake_companion_firmware, 350)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.CONTACT_DELETED)

    refused = await meshcore_client.commands.add_contact(library_contact(numbered_public_key(1000), "late"))
    await meshcore_client.commands.set_autoadd_config(0x01)
    overwriting = await meshcore_client.commands.add_contact(library_contact(numbered_public_key(1000), "late"))
    deleted_event = await recorder.wait_for_event(EventType.CONTACT_DELETED)

    assert is_error_with_code(refused, FirmwareErrorCode.TABLE_FULL)
    assert overwriting.type == EventType.OK
    assert deleted_event.payload == {"pubkey": records[0].public_key.hex()}
    assert fake_companion_firmware.find_contact(records[0].public_key) is None
    assert len(fake_companion_firmware.contact_records()) == 350


async def test_reset_path_and_remove_contact_of_an_unknown_key_answer_not_found(meshcore_client: Any) -> None:
    reset_result = await meshcore_client.commands.reset_path(numbered_public_key(3))
    remove_result = await meshcore_client.commands.remove_contact(numbered_public_key(3))

    assert is_error_with_code(reset_result, FirmwareErrorCode.NOT_FOUND)
    assert is_error_with_code(remove_result, FirmwareErrorCode.NOT_FOUND)


async def test_reset_path_forgets_the_route_but_keeps_its_bytes(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    routed_contact = ContactRecord.create(public_key=numbered_public_key(4), name="routed").with_route(
        route_path=b"\x5a\x6b", path_hash_size=1, last_modified=FIRST_LAST_MODIFIED
    )
    fake_companion_firmware.add_or_update_contact(routed_contact)

    reset_result = await meshcore_client.commands.reset_path(routed_contact.public_key)

    stored_contact = fake_companion_firmware.find_contact(routed_contact.public_key)
    assert reset_result.type == EventType.OK
    assert stored_contact is not None
    assert not stored_contact.has_known_route
    assert stored_contact.out_path[:2] == b"\x5a\x6b"


async def test_in_manual_mode_an_unknown_nodes_advert_is_reported_as_a_new_contact(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    await meshcore_client.commands.send(MANUAL_ADD_ON_FRAME, OK_OR_ERROR)
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEW_CONTACT, EventType.ADVERTISEMENT)

    device.send_advert()
    new_contact = await recorder.wait_for_event(EventType.NEW_CONTACT)

    assert new_contact.payload["public_key"] == device.public_key.hex()
    assert new_contact.payload["adv_name"] == "tracker"
    assert new_contact.payload["type"] == NodeType.CHAT
    assert new_contact.payload["out_path_len"] == -1
    assert 0 <= device.firmware.clock_time() - new_contact.payload["last_advert"] <= 1
    assert fake_companion_firmware.find_contact(device.public_key) is None
    assert recorder.of_type(EventType.ADVERTISEMENT) == []


async def test_a_reported_new_contact_can_be_added_back_and_its_next_advert_is_an_advertisement(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    await meshcore_client.commands.send(MANUAL_ADD_ON_FRAME, OK_OR_ERROR)
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEW_CONTACT, EventType.ADVERTISEMENT)
    device.send_advert()
    new_contact = await recorder.wait_for_event(EventType.NEW_CONTACT)

    added = await meshcore_client.commands.add_contact(new_contact.payload)
    first_advert_timestamp = new_contact.payload["last_advert"]
    device.firmware.force_clock_time(first_advert_timestamp - 10)
    device.send_advert()
    await simulated_mesh.wait_until_idle()
    device.firmware.force_clock_time(first_advert_timestamp + 60)
    device.send_advert()
    advertisement = await recorder.wait_for_event(EventType.ADVERTISEMENT)

    assert added.type == EventType.OK
    assert advertisement.payload == {"public_key": device.public_key.hex()}
    advert_receptions = [
        record.reception for record in simulated_mesh.traffic(sender="tracker", packet_type=AdvertPacket)
    ]
    assert advert_receptions == [
        ReceptionOutcome.REPORTED_AS_NEW_CONTACT,
        ReceptionOutcome.ADVERT_NOT_NEWER,
        ReceptionOutcome.ACCEPTED,
    ]
    assert len(recorder.of_type(EventType.NEW_CONTACT)) == 1


async def test_without_manual_add_an_unknown_advert_is_stored_and_reported_as_an_advertisement(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEW_CONTACT, EventType.ADVERTISEMENT)

    device.send_advert()
    await recorder.wait_for_event(EventType.ADVERTISEMENT)

    stored_contact = fake_companion_firmware.find_contact(device.public_key)
    assert stored_contact is not None
    assert stored_contact.name == b"tracker"
    assert recorder.of_type(EventType.NEW_CONTACT) == []


async def test_in_manual_mode_the_chat_type_bit_stores_chat_nodes(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    await meshcore_client.commands.send(MANUAL_ADD_ON_FRAME, OK_OR_ERROR)
    await meshcore_client.commands.set_autoadd_config(0x02)
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ADVERTISEMENT)

    device.send_advert()
    await recorder.wait_for_event(EventType.ADVERTISEMENT)

    assert fake_companion_firmware.find_contact(device.public_key) is not None


async def test_a_node_at_the_hop_limit_is_reported_as_a_new_contact(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    await meshcore_client.commands.send(b"\x3a\x00\x02", OK_OR_ERROR)
    distant_device = simulated_mesh.add_device("distant", repeaters_to_relay=2, relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEW_CONTACT)

    distant_device.send_advert(flood=True)
    await recorder.wait_for_event(EventType.NEW_CONTACT)

    assert fake_companion_firmware.find_contact(distant_device.public_key) is None


async def test_with_a_full_contact_table_an_advert_is_reported_with_contacts_full(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    add_numbered_contacts(fake_companion_firmware, 350)
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEW_CONTACT, EventType.CONTACTS_FULL)

    device.send_advert()
    await recorder.wait_for_event(EventType.CONTACTS_FULL)

    assert recorder.types() == [EventType.NEW_CONTACT, EventType.CONTACTS_FULL]


async def test_a_zero_hop_advert_does_not_reach_a_relay_behind_repeaters(
    simulated_mesh: SimulatedMesh,
) -> None:
    device = simulated_mesh.add_device("distant", repeaters_to_relay=1, relay_knows_device=False)

    device.send_advert(flood=False)
    await simulated_mesh.wait_until_idle()

    assert [record.outcome for record in simulated_mesh.traffic(sender="distant")] == [
        DeliveryOutcome.LOST_OUT_OF_RANGE
    ]


async def test_an_imported_card_in_manual_mode_is_only_reported_as_a_new_contact(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    await meshcore_client.commands.send(MANUAL_ADD_ON_FRAME, OK_OR_ERROR)
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.NEW_CONTACT)
    card = bytes.fromhex(device.contact_card_uri().removeprefix("meshcore://"))

    imported = await meshcore_client.commands.import_contact(card)
    new_contact = await recorder.wait_for_event(EventType.NEW_CONTACT)

    assert imported.type == EventType.OK
    assert new_contact.payload["public_key"] == device.public_key.hex()
    assert fake_companion_firmware.find_contact(device.public_key) is None


async def test_a_forged_card_is_accepted_for_processing_and_then_dropped(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    device = simulated_mesh.add_device("tracker", relay_knows_device=False)
    card = bytearray.fromhex(device.contact_card_uri().removeprefix("meshcore://"))
    signature_offset = 2 + 32 + 4
    card[signature_offset] ^= 0x01

    imported = await meshcore_client.commands.import_contact(bytes(card))
    await wait_until(
        lambda: bool(fake_companion_firmware.imported_card_outcomes), description="the card to be processed"
    )

    assert imported.type == EventType.OK
    assert fake_companion_firmware.imported_card_outcomes == [ReceptionOutcome.FORGED_SIGNATURE]
    assert fake_companion_firmware.find_contact(device.public_key) is None


async def test_a_contact_exports_its_last_advert_and_a_contact_never_heard_exports_nothing(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    heard_device = simulated_mesh.add_device("heard", relay_knows_device=False)
    unheard_device = simulated_mesh.add_device("unheard", relay_knows_device=True)
    recorder = MeshCoreEventRecorder(meshcore_client, EventType.ADVERTISEMENT)
    heard_device.send_advert()
    await recorder.wait_for_event(EventType.ADVERTISEMENT)

    heard_export = await meshcore_client.commands.export_contact(heard_device.public_key)
    unheard_export = await meshcore_client.commands.export_contact(unheard_device.public_key)

    advert_packet = simulated_mesh.traffic(sender="heard", packet_type=AdvertPacket)[0].packet
    assert isinstance(advert_packet, AdvertPacket)
    assert heard_export.payload["uri"] == "meshcore://" + (b"\x11\x00" + advert_packet.advert_payload).hex()
    assert is_error_with_code(unheard_export, FirmwareErrorCode.NOT_FOUND)
