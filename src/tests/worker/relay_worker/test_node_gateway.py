"""The node gateway against the fake node: one command at a time, late replies, lost replies, full frames."""

import asyncio
from dataclasses import replace
from typing import Any

import pytest
from meshcore import EventType
from meshcore.events import Event

from messaging.inbound_log import ReceivedDirectMessageFrame
from messaging.models import OutboundPacket
from messaging.outbound_packets import PacketQueuedOnNode, PacketRejectedByNode
from node.contact_cards import truncate_contact_name
from tests.worker.fake_node.contact_records import ContactRecord
from tests.worker.fake_node.fake_companion_firmware import FakeCompanionFirmware
from tests.worker.fake_node.fake_node_transport import FakeNodeConnector
from tests.worker.fake_node.firmware_state import FirmwareTiming
from tests.worker.fake_node.frames import CommandCode, FirmwareErrorCode
from tests.worker.fake_node.simulated_mesh import SimulatedMesh
from tests.worker.fake_node.waiting import wait_until
from tests.worker.relay_worker.worker_harness import FAST_WORKER_TIMING, AdjustableClock
from worker.node_contact_records import (
    ADD_UPDATE_CONTACT_FRAME_BYTES,
    NodeContactRecord,
    build_add_update_contact_frame,
)
from worker.node_event_subscriptions import NodeEvent, NodeEventSubscriptions
from worker.node_gateway import (
    FactoryResetReply,
    NextMessageOutcome,
    NodeGateway,
    NodeNotConnectedError,
    NodeRejectedCommandError,
    NodeReplyLostError,
)
from worker.worker_state import WorkerSignals
from worker.worker_timing import WorkerTiming

GATEWAY_TIMING = replace(FAST_WORKER_TIMING, node_command_timeout_seconds=0.15, late_reply_grace_seconds=0.3)
PATH_HASH_MODE_OUT_OF_RANGE = 9


class ReconnectRequests:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def request_reconnect(self, reason: str) -> None:
        self.reasons.append(reason)


def build_gateway(meshcore_client: Any, timing: WorkerTiming = GATEWAY_TIMING) -> tuple[NodeGateway, ReconnectRequests]:
    reconnect_requests = ReconnectRequests()
    gateway = NodeGateway(timing=timing, request_reconnect=reconnect_requests.request_reconnect)
    gateway.attach(meshcore_client)
    return gateway, reconnect_requests


async def test_concurrent_commands_are_serialised_and_each_gets_its_own_reply(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    fake_companion_firmware.delay_next_reply(0.05, command_code=CommandCode.SET_ADVERT_NAME)

    name_result, path_hash_result = await asyncio.gather(
        gateway.set_node_name("relay one"),
        gateway.set_path_hash_size(PATH_HASH_MODE_OUT_OF_RANGE + 1),
        return_exceptions=True,
    )

    assert name_result is None
    assert isinstance(path_hash_result, NodeRejectedCommandError)
    assert path_hash_result.error_code == FirmwareErrorCode.ILLEGAL_ARGUMENT
    assert fake_companion_firmware.preferences.node_name == b"relay one"


async def test_a_late_reply_after_a_timeout_is_not_taken_for_the_next_commands_reply(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    fake_companion_firmware.delay_next_reply(0.2, command_code=CommandCode.SET_ADVERT_NAME)

    with pytest.raises(NodeReplyLostError):
        await gateway.set_node_name("late")

    with pytest.raises(NodeRejectedCommandError) as rejection:
        await gateway.set_path_hash_size(PATH_HASH_MODE_OUT_OF_RANGE + 1)
    assert rejection.value.error_code == FirmwareErrorCode.ILLEGAL_ARGUMENT


async def test_three_lost_replies_in_a_row_request_a_reconnect(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, reconnect_requests = build_gateway(meshcore_client)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.GET_DEVICE_TIME, count=3)

    for _ in range(3):
        with pytest.raises(NodeReplyLostError):
            await gateway.read_node_clock()

    assert len(reconnect_requests.reasons) == 1


async def test_a_node_push_between_lost_replies_starts_the_count_again(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    gateway, reconnect_requests = build_gateway(meshcore_client)
    signals = WorkerSignals()
    subscriptions = build_subscriptions(gateway, signals)
    subscriptions.subscribe(meshcore_client)
    device = simulated_mesh.add_device("tracker")
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.GET_DEVICE_TIME, count=3)

    for _ in range(2):
        with pytest.raises(NodeReplyLostError):
            await gateway.read_node_clock()
    device.send_direct_message("HT1 Q bob")
    await wait_until(signals.drain_requested.is_set, description="the MESSAGES_WAITING push")
    with pytest.raises(NodeReplyLostError):
        await gateway.read_node_clock()

    assert reconnect_requests.reasons == []
    subscriptions.unsubscribe()


def build_subscriptions(
    gateway: NodeGateway,
    signals: WorkerSignals,
    inbound_frame_queue: asyncio.Queue[ReceivedDirectMessageFrame] | None = None,
) -> NodeEventSubscriptions:
    node_event_queue: asyncio.Queue[NodeEvent] = asyncio.Queue()
    return NodeEventSubscriptions(
        gateway=gateway,
        inbound_frame_queue=inbound_frame_queue or asyncio.Queue(),
        node_event_queue=node_event_queue,
        signals=signals,
        clock=AdjustableClock(),
        report_disconnection=lambda reason: None,
    )


async def test_a_command_fails_fast_without_a_node() -> None:
    gateway = NodeGateway(timing=GATEWAY_TIMING, request_reconnect=lambda reason: None)

    with pytest.raises(NodeNotConnectedError):
        await gateway.read_node_clock()


async def test_a_command_fails_fast_once_the_link_is_lost(
    fake_node_connector: FakeNodeConnector, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)

    fake_node_connector.simulate_link_loss()
    await wait_until(lambda: not meshcore_client.is_connected, description="the client to notice the loss")

    with pytest.raises(NodeNotConnectedError):
        await gateway.read_node_clock()


async def test_a_command_in_flight_ends_at_once_when_the_client_is_detached(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    slow_timing = replace(GATEWAY_TIMING, node_command_timeout_seconds=5.0)
    gateway, _reconnect_requests = build_gateway(meshcore_client, timing=slow_timing)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.GET_DEVICE_TIME)

    clock_read = asyncio.ensure_future(gateway.read_node_clock())
    await asyncio.sleep(0.05)
    gateway.detach()

    with pytest.raises(NodeNotConnectedError):
        await asyncio.wait_for(clock_read, 1.0)


async def test_a_listing_returns_every_contact_the_node_holds(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    public_keys = add_contacts_to_firmware(fake_companion_firmware, 40)

    contacts_on_node = await gateway.list_contacts()

    assert set(contacts_on_node) == set(public_keys)
    assert all(listed_contact.out_path_length == -1 for listed_contact in contacts_on_node.values())


async def test_a_slow_listing_completes_because_every_contact_extends_its_deadline(fake_node_seed: int) -> None:
    slow_firmware = FakeCompanionFirmware(
        node_name="slow", seed=fake_node_seed, timing=FirmwareTiming(contact_listing_step_seconds=0.02)
    )
    slow_firmware.start()
    connector = FakeNodeConnector(slow_firmware, default_timeout=1.0, seed=fake_node_seed)
    try:
        meshcore_client = await connector()
        listing_timing = replace(GATEWAY_TIMING, contact_listing_activity_seconds=0.2)
        gateway, _reconnect_requests = build_gateway(meshcore_client, timing=listing_timing)
        public_keys = add_contacts_to_firmware(slow_firmware, 30)

        contacts_on_node = await gateway.list_contacts()

        assert set(contacts_on_node) == set(public_keys)
    finally:
        await connector.close()
        await slow_firmware.stop()


async def test_a_listing_that_stalls_fails_after_its_activity_deadline(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    add_contacts_to_firmware(fake_companion_firmware, 3)
    fake_companion_firmware.drop_next_reply(command_code=CommandCode.GET_CONTACTS)

    with pytest.raises(NodeReplyLostError):
        await gateway.list_contacts()


async def test_the_next_message_completes_on_channel_datagrams_without_a_timeout(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any, simulated_mesh: SimulatedMesh
) -> None:
    gateway, reconnect_requests = build_gateway(meshcore_client)
    device = simulated_mesh.add_device("tracker")
    device.send_direct_message("HT1 Q bob")
    await simulated_mesh.wait_until_idle()
    for datagram_number in range(3):
        simulated_mesh.inject_channel_datagram(data=bytes([datagram_number]) * 8)
    simulated_mesh.inject_channel_message(text="hello public")
    device.send_direct_message("HT1 Q carol")
    await simulated_mesh.wait_until_idle()

    outcomes = [await gateway.get_next_message() for _ in range(7)]

    assert outcomes == [
        NextMessageOutcome.DIRECT_MESSAGE,
        NextMessageOutcome.CHANNEL_TRAFFIC,
        NextMessageOutcome.CHANNEL_TRAFFIC,
        NextMessageOutcome.CHANNEL_TRAFFIC,
        NextMessageOutcome.CHANNEL_TRAFFIC,
        NextMessageOutcome.DIRECT_MESSAGE,
        NextMessageOutcome.NO_MORE_MESSAGES,
    ]
    assert reconnect_requests.reasons == []


async def test_settings_go_out_as_full_frames_the_firmware_reads_as_meant(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)

    await gateway.set_radio_parameters(
        frequency_kilohertz=916575, bandwidth_hertz=62500, spreading_factor=7, coding_rate=7, client_repeat=False
    )
    await gateway.set_transmit_power(-5)
    await gateway.set_path_hash_size(2)
    await gateway.set_other_parameters(
        manual_add_contacts=True, telemetry_modes=0, advert_location_policy=0, multi_acks=2
    )
    await gateway.set_auto_add_configuration(configuration=0, maximum_hops=0)
    self_information = await gateway.read_self_information()
    device_information = await gateway.query_device()
    auto_add_configuration = await gateway.read_auto_add_configuration()

    assert fake_companion_firmware.protocol_violations == []
    assert self_information.radio_frequency_kilohertz == 916575
    assert self_information.radio_bandwidth_hertz == 62500
    assert (self_information.radio_spreading_factor, self_information.radio_coding_rate) == (7, 7)
    assert self_information.transmit_power_dbm == -5
    assert self_information.manual_add_contacts
    assert self_information.multi_acks == 2
    assert device_information.path_hash_size == 2
    assert not device_information.client_repeat
    assert (auto_add_configuration.configuration, auto_add_configuration.maximum_hops) == (0, 0)


async def test_a_contact_is_added_with_a_full_record_and_no_route(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    record = NodeContactRecord(
        public_key="ab" * 32,
        name="Émilie's tracker with a very long name",
        advert_timestamp=1_790_000_000,
        latitude_microdegrees=-37_813_600,
        longitude_microdegrees=144_963_100,
    )

    await gateway.add_contact(record)

    stored_record = fake_companion_firmware.find_contact(bytes.fromhex(record.public_key))
    assert stored_record is not None
    assert not stored_record.has_known_route
    assert stored_record.name == truncate_contact_name(record.name).encode()
    assert len(stored_record.name) <= 31
    assert stored_record.latitude_microdegrees == record.latitude_microdegrees
    assert stored_record.longitude_microdegrees == record.longitude_microdegrees
    assert fake_companion_firmware.protocol_violations == []
    assert len(build_add_update_contact_frame(record)) == ADD_UPDATE_CONTACT_FRAME_BYTES


async def test_removing_or_resetting_an_unknown_contact_reports_it_as_absent(meshcore_client: Any) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)

    assert not await gateway.remove_contact("cd" * 32)
    assert not await gateway.reset_path("cd" * 32)


async def test_a_text_message_is_queued_or_rejected_with_the_nodes_error(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    known_key = add_contacts_to_firmware(fake_companion_firmware, 1)[0]

    queued_outcome = await gateway.send_text_message(known_key, "HT1 a bob", 1_800_000_000)
    rejected_outcome = await gateway.send_text_message("cd" * 32, "HT1 a bob", 1_800_000_001)

    assert isinstance(queued_outcome, PacketQueuedOnNode)
    assert queued_outcome.route == OutboundPacket.Route.FLOOD
    assert queued_outcome.expected_acknowledgement_code in {
        code.hex() for code in fake_companion_firmware.expected_acknowledgement_codes()
    }
    assert rejected_outcome == PacketRejectedByNode(node_error_code=FirmwareErrorCode.NOT_FOUND)


async def test_the_factory_reset_carries_its_suffix_and_gets_no_reply(
    fake_companion_firmware: FakeCompanionFirmware, meshcore_client: Any
) -> None:
    gateway, _reconnect_requests = build_gateway(meshcore_client)
    original_public_key = fake_companion_firmware.public_key

    try:
        reset_reply, _error_code = await gateway.factory_reset()
    except NodeNotConnectedError:
        reset_reply = FactoryResetReply.NO_REPLY

    assert reset_reply == FactoryResetReply.NO_REPLY
    await wait_until(lambda: fake_companion_firmware.is_running, description="the node to boot again")
    assert fake_companion_firmware.public_key != original_public_key


def add_contacts_to_firmware(firmware: FakeCompanionFirmware, contact_count: int) -> list[str]:
    """Contacts as the node stores them, each stamped with the node's clock."""
    public_keys = [number.to_bytes(2, "big") + bytes([0x5A]) * 30 for number in range(contact_count)]
    for public_key in public_keys:
        firmware.add_or_update_contact(
            ContactRecord.create(
                public_key=public_key, name=f"node {public_key[:2].hex()}", last_modified=firmware.clock_time()
            )
        )
    return [public_key.hex() for public_key in public_keys]


async def test_a_direct_message_with_a_nul_character_can_still_be_recorded(meshcore_client: Any) -> None:
    gateway = NodeGateway(timing=GATEWAY_TIMING, request_reconnect=lambda reason: None)
    inbound_frame_queue: asyncio.Queue[ReceivedDirectMessageFrame] = asyncio.Queue()
    subscriptions = build_subscriptions(gateway, WorkerSignals(), inbound_frame_queue)
    subscriptions.subscribe(meshcore_client)
    received_payload = {
        "pubkey_prefix": "AABBCCDDEEFF",
        "sender_timestamp": 1_800_000_000,
        "txt_type": 0,
        "text": "HT1 Q b\x00b",
        "path_len": 255,
    }

    await meshcore_client.dispatcher.dispatch(Event(EventType.CONTACT_MSG_RECV, received_payload))

    frame = await asyncio.wait_for(inbound_frame_queue.get(), 1.0)
    assert frame.text == "HT1 Q b\N{REPLACEMENT CHARACTER}b"
    assert frame.sender_public_key_prefix == "aabbccddeeff"
    subscriptions.unsubscribe()
